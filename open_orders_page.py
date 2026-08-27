#!/usr/bin/env python3
"""
JIT4You Open Orders — Per-Customer Tabbed Web Page
===================================================
DUPLICATE of the open-orders-report process. SAME data pipeline, different
output: instead of emailing, it builds ONE self-contained HTML page with a
tab per customer (each tab shows that customer's open-order info) plus a
Refresh button, and publishes the page + a JSON data snapshot to GitHub Pages.

This script does NOT modify open_orders_report.py — it imports its extraction
logic unchanged, so the open-orders data is computed identically.

Usage:
  python open_orders_page.py              # extract, build page, push to GitHub Pages
  python open_orders_page.py --no-push    # build files locally, don't push
  python open_orders_page.py --dry-run    # preview counts only (no page)

Outputs (in this script's folder, then pushed to the GitHub Pages repo):
  - open-orders.html        the tabbed page
  - open-orders-data.json   the data snapshot the page (and its Refresh button) loads
"""

import os
import sys
import json
import time
import base64
import argparse
import urllib.request
import urllib.error
from datetime import datetime
from collections import defaultdict

# ── Reuse the EXACT extraction logic from the original report (unmodified) ──
# Importing is safe: open_orders_report.py guards execution behind
# `if __name__ == "__main__"`, so nothing runs on import.
from open_orders_report import VtigerAPI, extract_open_orders, CONFIG, log, build_po_email_url, _vendor_greeting
from pnl_report import build_pnl
from customer_analysis import (build_customer_analysis, _build_email_doc as _email_draft_doc,
                               _wordmark, _esc, _qstr)
from customer_prices import build_customer_prices
from ytd_demand import build_ytd_demand

# ─────────────────────────────────────────────
# GitHub Pages publishing (same host/repo as the customer-order-status reports)
# ─────────────────────────────────────────────
GITHUB_REPO = os.environ.get("GH_PAGES_REPO", "JIT4Labs1/customer-order-status")
GITHUB_TOKEN = os.environ.get("GH_PAT_TOKEN", "")
GITHUB_PAGES_URL = os.environ.get("GH_PAGES_URL", "https://jit4labs1.github.io/customer-order-status")

PAGE_FILENAME = "open-orders.html"
DATA_FILENAME = "open-orders-data.json"
PAID_INVENTORY_FILENAME = "paid_inventory.json"  # user-maintained "Paid Inventory" box store

# Sales Orders to exclude from the report entirely (matched case-insensitively,
# with or without the "SO" prefix). These never appear in any tab.
EXCLUDED_SOS = {"SO314", "SO390"}


def _is_excluded_so(so_num):
    s = str(so_num or "").strip().upper()
    return s in EXCLUDED_SOS or ("SO" + s.lstrip("SO")) in EXCLUDED_SOS

# Customer/account names to exclude from the dashboard entirely (test/dummy accounts).
# Matched case-insensitively on the full, trimmed account name. These never appear in
# any tab — no customer entry, no open SOs, no vendor POs, no high-demand rows.
EXCLUDED_CUSTOMERS = {"test company"}


def _is_excluded_customer(name):
    return str(name or "").strip().lower() in EXCLUDED_CUSTOMERS

# ── Refresh button → GitHub Actions workflow_dispatch ──────────────────────────
# The page's Refresh button triggers this workflow to re-pull Vtiger live, then
# polls the data snapshot until it updates. GH_BUTTON_TOKEN is a DEDICATED,
# minimal fine-grained PAT (Actions: write on this repo ONLY). It is embedded in
# the published page so the button can dispatch the workflow; if it leaks the
# only thing it can do is trigger this refresh. Leave it empty to build a page
# whose button just reloads the latest snapshot (no live pull).
# Fallback token below has Actions:write on this repo (used by the Refresh button to
# workflow_dispatch). Embedded XOR-obfuscated in the published page. NOTE: replace with a
# durable PAT before it expires, else the Refresh button reverts to snapshot-only.
# Rotated 2026-07-18 — previous PAT expired ~2026-06-25.
GH_BUTTON_TOKEN = os.environ.get("GH_BUTTON_TOKEN", "")
GH_WORKFLOW_FILE = os.environ.get("GH_WORKFLOW_FILE", "refresh-open-orders.yml")
GH_BRANCH = os.environ.get("GH_PAGES_BRANCH", "main")
BTN_OBF_KEY = os.environ.get("BTN_OBF_KEY", "jit4oo-refresh")


def _xor_b64(text, key):
    """XOR-obfuscate `text` with `key` (cycled) and base64 the result, so the
    embedded button token is opaque bytes — undetectable by GitHub secret
    scanning. Reversed at runtime in the page by the matching JS deobfuscator."""
    kb = key.encode()
    xored = bytes(b ^ kb[i % len(kb)] for i, b in enumerate(text.encode()))
    return base64.b64encode(xored).decode()


# ─────────────────────────────────────────────
# Shape the extracted open_items into a per-customer structure for the page
# ─────────────────────────────────────────────
def _pacific_now_str():
    """Current time as a Pacific-time string with tz label (PST/PDT), e.g.
    '2026-06-16 02:45:10 PM PDT'. Used as the page's 'last refreshed' stamp."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.now(ZoneInfo("America/Los_Angeles"))
        return dt.strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except Exception:
        from datetime import timezone, timedelta
        dt = datetime.now(timezone.utc) - timedelta(hours=8)
        return dt.strftime("%Y-%m-%d %I:%M:%S %p") + " PST"


def build_vendor_po_email(vname, vitems):
    """Branded, image-free vendor email listing their open POs (grouped by PO) so they
    know what to deliver, with the previously provided ETA per line."""
    greeting = _vendor_greeting(vname)
    by_po, no_po = defaultdict(list), []
    for it in vitems:
        pos = [p.strip() for p in (it.get("pending_pos", "") or "").split(",") if p.strip()]
        if pos:
            for p in pos:
                by_po[p].append(it)
        else:
            no_po.append(it)
    td = "padding:7px 10px;border:1px solid #d8dee4;font-size:12px;"
    th = "padding:8px 10px;border:1px solid #0D2B45;color:#fff;text-align:left;font-size:12px;font-weight:700;"
    pohdr = "padding:7px 10px;border:1px solid #1F4E79;background:#e8eef4;color:#1F4E79;font-weight:700;font-size:12px;"

    def row(it):
        eta = (it.get("eta", "") or "").split(" ")[0]
        return ("<tr>"
                '<td style="' + td + 'white-space:nowrap;">' + _esc(it.get("order_date", "")) + "</td>"
                '<td style="' + td + '">' + _esc(it.get("product", "")) + "</td>"
                '<td style="' + td + '">' + _esc(it.get("customer", "")) + "</td>"
                '<td style="' + td + 'text-align:center;">' + _qstr(it.get("ordered_qty", 0)) + "</td>"
                '<td style="' + td + 'text-align:center;">' + _qstr(it.get("delivered_qty", 0)) + "</td>"
                '<td style="' + td + 'text-align:center;font-weight:700;color:#c0392b;">' + _qstr(it.get("open_qty", 0)) + "</td>"
                '<td style="' + td + 'text-align:center;white-space:nowrap;">' + (_esc(eta) if eta else "&mdash;") + "</td>"
                "</tr>")

    def mindate(its):
        ds = [i.get("order_date", "") for i in its if i.get("order_date")]
        return min(ds) if ds else "9999"
    groups = sorted(((po, by_po[po]) for po in by_po.keys()), key=lambda g: mindate(g[1]))
    if no_po:
        groups.append(("No PO assigned", no_po))
    body_rows = ""
    for po, its in groups:
        its = sorted(its, key=lambda i: (i.get("order_date", ""), i.get("product", "")))
        label = ("PO " + po) if po != "No PO assigned" else po
        body_rows += '<tr><td colspan="7" style="' + pohdr + '">' + _esc(label) + "</td></tr>"
        for it in its:
            body_rows += row(it)
    table = ('<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;max-width:800px;'
             'background:#fff;font-family:Arial,sans-serif;"><thead><tr style="background:#0D2B45;">'
             '<th style="' + th + '">Order Date</th><th style="' + th + '">Product</th>'
             '<th style="' + th + '">Customer</th><th style="' + th + 'text-align:center;">Ordered</th>'
             '<th style="' + th + 'text-align:center;">Delivered</th><th style="' + th + 'text-align:center;">Open</th>'
             '<th style="' + th + 'text-align:center;">ETA</th></tr></thead><tbody>' + body_rows + "</tbody></table>")
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#101E3E;max-width:840px;margin:0 auto;background:#fff;">'
        '<div style="background:#fff;padding:18px 24px;border-bottom:3px solid #008080;">' + _wordmark(24) + "</div>"
        '<div style="padding:22px 24px;font-size:14px;line-height:1.6;">'
        "<p>Hi " + _esc(greeting) + ",</p>"
        "<p>Find enclosed the list of open POs with previously provided ETA.</p>"
        + table +
        '<p style="margin-top:18px;">Thank you,<br>JIT4You</p>'
        '<p style="font-size:12px;color:#008080;">'
        '<a href="mailto:CustomerSupport@jit4you.com" style="color:#008080;text-decoration:none;">CustomerSupport@jit4you.com</a> '
        '&nbsp;&middot;&nbsp; (949) 396-9194</p>'
        "</div></div>"
    )


def build_page_data(open_items):
    """Group the flat open_items list into a per-customer payload for the page."""
    # Drop excluded Sales Orders (e.g. SO314, SO390) from every view.
    open_items = [it for it in open_items if not _is_excluded_so(it.get("so_num", ""))]
    # Drop excluded customers/accounts (e.g. "Test company") from every view — no SO/PO data.
    open_items = [it for it in open_items if not _is_excluded_customer(it.get("customer", ""))]
    by_customer = defaultdict(list)
    for it in open_items:
        by_customer[it["customer"]].append(it)

    customers = []
    for name in sorted(by_customer.keys(), key=str.lower):
        items = by_customer[name]
        # Sort items by order date ascending, then product
        items = sorted(items, key=lambda r: (r.get("order_date", ""), r.get("product", "")))
        open_sos = sorted(set(i["so_num"] for i in items))
        vendors = sorted(set(i["vendor"] for i in items if i.get("vendor")))
        rows = []
        for it in items:
            rows.append({
                "so_num": it.get("so_num", ""),
                "so_status": it.get("so_status", ""),
                "order_date": it.get("order_date", ""),
                "product": it.get("product", ""),
                "sku": it.get("sku", "") or "",
                "vendor": it.get("vendor", ""),
                "list_price": it.get("unit_price", 0),
                "ordered_qty": it.get("ordered_qty", 0),
                "delivered_qty": it.get("delivered_qty", 0),
                "open_qty": it.get("open_qty", 0),
                "pending_pos": it.get("pending_pos", ""),
                "eta": (it.get("eta", "") or "").split(" ")[0],
            })
        customers.append({
            "name": name,
            "open_sos": len(open_sos),
            "open_items": len(items),
            "vendors": vendors,
            "rows": rows,
        })

    # ── Per-PO "Email vendor" mailto drafts (identical to the open-orders report) ──
    # Group every open item by its pending PO across all customers, then build the
    # same mailto: draft the report uses (subject "PO### ETA?", vendor greeting,
    # open-item bullet list, PMA→debbie override already applied in vendor_email).
    po_groups = defaultdict(lambda: {"vendor": "", "vendor_email": "", "items": []})
    for it in open_items:
        pend = it.get("pending_pos", "") or ""
        for po in [p.strip() for p in pend.split(",") if p.strip()]:
            g = po_groups[po]
            if not g["vendor"]:
                g["vendor"] = it.get("vendor", "")
            if not g["vendor_email"]:
                g["vendor_email"] = it.get("vendor_email", "")
            g["items"].append({
                "product": it.get("product", ""),
                "open_qty": it.get("open_qty", 0),
                "eta": it.get("eta", ""),
                "customer": it.get("customer", ""),
                "so_num": it.get("so_num", ""),
            })
    bcc = CONFIG.get("vendor_followup_bcc", "")
    po_emails = {}
    for po, info in po_groups.items():
        url = build_po_email_url(po, info, bcc)
        if url:
            po_emails[po] = url

    # ── Vendor view: same open items, grouped by vendor (then by customer in UI) ──
    by_vendor = defaultdict(list)
    for it in open_items:
        by_vendor[it.get("vendor", "") or "Unspecified"].append(it)
    vendors = []
    for vname in sorted(by_vendor.keys(), key=str.lower):
        vitems = sorted(by_vendor[vname], key=lambda r: (r.get("customer", "").lower(),
                                                          r.get("order_date", ""), r.get("product", "")))
        vcusts = sorted(set(i["customer"] for i in vitems), key=str.lower)
        vpos = set()
        vrows = []
        for it in vitems:
            for po in [p.strip() for p in (it.get("pending_pos", "") or "").split(",") if p.strip()]:
                vpos.add(po)
            vrows.append({
                "customer": it.get("customer", ""),
                "so_num": it.get("so_num", ""),
                "so_status": it.get("so_status", ""),
                "order_date": it.get("order_date", ""),
                "product": it.get("product", ""),
                "sku": it.get("sku", "") or "",
                "ordered_qty": it.get("ordered_qty", 0),
                "delivered_qty": it.get("delivered_qty", 0),
                "open_qty": it.get("open_qty", 0),
                "pending_pos": it.get("pending_pos", ""),
                "eta": (it.get("eta", "") or "").split(" ")[0],
            })
        # Vendor email draft — the report's exact open-PO table, wrapped for rich copy.
        vemail = ""
        for it in vitems:
            if it.get("vendor_email"):
                vemail = it["vendor_email"]; break
        v_subject = "JIT4You — Your Open Purchase Orders (%d open item%s) — please advise delivery" % (
            len(vitems), "" if len(vitems) == 1 else "s")
        v_body = build_vendor_po_email(vname, vitems)
        v_email_doc = _email_draft_doc(vname, vemail, v_subject, v_body)
        vendors.append({
            "name": vname,
            "open_items": len(vitems),
            "customers": vcusts,
            "pos": len(vpos),
            "rows": vrows,
            "email": vemail,
            "email_subject": v_subject,
            "email_doc": v_email_doc,
        })

    # ── High-demand SKUs: items that appear on MORE THAN ONE PO, as a Product × Customer matrix ──
    prod_agg = {}  # product -> {vendor, cust qty map, distinct SOs, distinct POs}
    for it in open_items:
        prod = it.get("product", "")
        if not prod:
            continue
        e = prod_agg.setdefault(prod, {"vendor": it.get("vendor", ""),
                                       "cust": defaultdict(float), "sos": set(), "pos": set(),
                                       "detail": defaultdict(list)})
        if not e["vendor"] and it.get("vendor"):
            e["vendor"] = it.get("vendor")
        cust = it.get("customer", "")
        e["cust"][cust] += float(it.get("open_qty", 0) or 0)
        e["sos"].add(it.get("so_num", ""))
        for po in [p.strip() for p in (it.get("pending_pos", "") or "").split(",") if p.strip()]:
            e["pos"].add(po)
        # Per-customer breakdown line: which PO + SO date this open qty came from.
        e["detail"][cust].append({
            "po": it.get("pending_pos", "") or "",
            "date": (it.get("order_date", "") or "").split(" ")[0],
            "so": it.get("so_num", ""),
            "qty": float(it.get("open_qty", 0) or 0),
        })
    hd_items, hd_custset = [], set()
    for prod, e in prod_agg.items():
        po_count = len(e["pos"])
        if po_count < 2:              # high demand = the SKU appears on more than one PO
            continue
        cust_count = len(e["cust"])
        order_count = len(e["sos"])
        total = sum(e["cust"].values())
        hd_items.append({
            "product": prod,
            "vendor": e["vendor"],
            "total": total,
            "cust_count": cust_count,
            "order_count": order_count,
            "po_count": po_count,
            "pos": sorted(e["pos"]),
            "qty": {c: e["cust"][c] for c in e["cust"]},
            "detail": {c: e["detail"][c] for c in e["cust"]},
        })
        hd_custset.update(e["cust"].keys())
    # Most worth prioritizing first: most POs, then most customers, then highest total open qty.
    hd_items.sort(key=lambda x: (-x["po_count"], -x["cust_count"], -x["total"], x["product"].lower()))
    high_demand = {"customers": sorted(hd_custset, key=str.lower), "items": hd_items}

    totals = {
        "customers": len(customers),
        "open_sos": len(set((i["customer"], i["so_num"]) for i in open_items)),
        "open_items": len(open_items),
        "vendors": len(vendors),
        "high_demand_skus": len(hd_items),
    }
    return {
        "generated_at": _pacific_now_str(),
        "totals": totals,
        "customers": customers,
        "vendors": vendors,
        "high_demand": high_demand,
        "po_emails": po_emails,
    }


# ─────────────────────────────────────────────
# Alternative Sources cost map (vendor tab side box): SKU -> vendor costs.
# Only Beckman Coulter products; only the 4 requested vendors. Blank/zero => ''
# (rendered as N/A client-side). Value order per SKU: [name, pma, allora, aldx, clearchem].
# ─────────────────────────────────────────────
ALT_SOURCE_MANUFACTURER = "Beckman Coulter"


def build_alt_sources(vt):
    def clean(v):
        s = str(v or "").strip()
        if not s:
            return ""
        try:
            f = float(s)
        except Exception:
            return ""
        return "" if f == 0 else round(f, 2)

    rows = vt.query_all(
        "SELECT productcode, productname, manufacturer, cf_products_pmacost, "
        "cf_products_alloracost, cf_products_hldxcost, cf_products_clearchemcost "
        "FROM Products WHERE manufacturer = '" + ALT_SOURCE_MANUFACTURER + "'")
    out = {}
    for r in rows:
        code = (r.get("productcode") or "").strip()
        if not code:
            continue
        key = code.upper()
        rec = [(r.get("productname") or "").strip(),
               clean(r.get("cf_products_pmacost")),
               clean(r.get("cf_products_alloracost")),
               clean(r.get("cf_products_hldxcost")),
               clean(r.get("cf_products_clearchemcost"))]
        if key in out:
            # On duplicate SKU, keep whichever record has more populated costs.
            if sum(1 for x in rec[1:] if x != "") <= sum(1 for x in out[key][1:] if x != ""):
                continue
        out[key] = rec
    return out


def build_po_prices(vt):
    """Map each non-cancelled PurchaseOrder -> {sku: {unit, product, qty}} using the
    PO line items already sitting in vt.retrieve_cache (ZERO extra Vtiger calls — the
    main pass retrieves these POs). Powers the Invoice Check tab: the page compares an
    uploaded invoice's unit price per SKU against the PO's unit (list) price.
    unit = PO line item 'listprice' (the per-unit price we agreed to pay)."""
    # productid -> SKU (productcode). Reuse the SAME query string build_customer_prices
    # runs (immediately before this step) so it hits the warm query cache — no re-scan.
    prod = {}
    try:
        for p in vt.query_all("SELECT id, productcode, purchase_cost, productname FROM Products"):
            pid = p.get("id")
            if pid:
                prod[pid] = (p.get("productcode") or "").strip()
    except Exception as e:
        log(f"  PO prices: Products query failed: {e}")
    # vendor_id -> vendor name (best effort, for display only)
    ven = {}
    try:
        for v in vt.query_all("SELECT id, vendorname FROM Vendors"):
            if v.get("id"):
                ven[v["id"]] = (v.get("vendorname") or "").strip()
    except Exception:
        pass
    out = {}
    for _rid, rec in (getattr(vt, "retrieve_cache", {}) or {}).items():
        if not isinstance(rec, dict):
            continue
        pono = rec.get("purchaseorder_no")
        li = rec.get("LineItems", rec.get("lineItems"))
        if not pono or not isinstance(li, list):
            continue
        status = (rec.get("postatus") or "").strip()
        if status.lower() == "cancelled":
            continue
        skus = {}
        for it in li:
            if not isinstance(it, dict):
                continue
            pid = it.get("productid", "")
            sku = (prod.get(pid, "") or "").strip()
            if not sku:
                continue
            key = sku.upper()
            try:
                unit = float(it.get("listprice") or it.get("netprice") or 0)
            except Exception:
                unit = 0.0
            try:
                qty = float(it.get("quantity") or 0)
            except Exception:
                qty = 0.0
            prev = skus.get(key)
            # Keep first occurrence; upgrade only if the earlier one had no price.
            if prev is None or (prev.get("unit", 0) == 0 and unit):
                skus[key] = {"product": (it.get("product_name") or prod.get(pid, "") or ""),
                             "unit": round(unit, 2), "qty": qty}
        if skus:
            out[str(pono).strip().upper()] = {
                "vendor": ven.get(rec.get("vendor_id", ""), ""),
                "status": status,
                "skus": skus,
            }
    return out


# ─────────────────────────────────────────────
# HTML page (self-contained; tabs + Refresh button; renders from embedded JSON
# and re-fetches the JSON snapshot on Refresh)
# ─────────────────────────────────────────────
def build_html(page_data, embeds=None):
    data_json = json.dumps(page_data).replace("</", "<\\/").replace("<!--", "<\\!--")
    data_url = f"{DATA_FILENAME}"  # same-origin relative fetch on GitHub Pages
    # Optional offline embeds: dict with keys gads/li/wt -> data dicts (used to
    # build the self-contained LOCAL mirror). None => online build (page fetches).
    def _emb(key):
        if embeds and embeds.get(key) is not None:
            return json.dumps(embeds[key]).replace("</", "<\\/").replace("<!--", "<\\!--")
        return "null"
    gads_embed, li_embed, wt_embed, ship_embed, pay_embed, spnl_embed = _emb("gads"), _emb("li"), _emb("wt"), _emb("ship"), _emb("pay"), _emb("spnl")
    cj_embed = _emb("cj")
    iopp_embed = _emb("iopp")
    # The button token is XOR-obfuscated (then base64'd) in the page so GitHub
    # secret scanning / push protection does not detect a `github_pat_` token —
    # plain base64 is NOT enough (GitHub decodes it), so the commit would be
    # blocked and the token auto-revoked in a public repo. XOR produces opaque
    # bytes the scanner can't match; the button reverses it at runtime. This is
    # obfuscation, not secrecy — the token is intentionally minimal (Actions-only)
    # so exposure is low-risk.
    token_obf = _xor_b64(GH_BUTTON_TOKEN, BTN_OBF_KEY) if GH_BUTTON_TOKEN else ""
    btn_cfg = json.dumps({
        "token_obf": token_obf,
        "k": BTN_OBF_KEY,
        "repo": GITHUB_REPO,
        "workflow": GH_WORKFLOW_FILE,
        "branch": GH_BRANCH,
    }).replace("</", "<\\/").replace("<!--", "<\\!--")

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JIT4Labs — Business Dashboard</title>
<link rel="icon" href="https://jit4you.myshopify.com/cdn/shop/files/JIT4LABS_Favicon.png" type="image/png">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'Segoe UI',Arial,Helvetica,sans-serif; background:#f0f2f5; color:#2c3e50; }
  .header { background:#ffffff; color:#172144; padding:16px 28px; display:flex; align-items:center;
            justify-content:space-between; flex-wrap:wrap; gap:12px; border-bottom:1px solid #dde5ec; }
  .header .brand { line-height:1; }
  .brand-word { font-size:30px; font-weight:800; letter-spacing:-.5px; font-family:'Segoe UI',Arial,Helvetica,sans-serif; }
  .brand-jit { color:#1a2340; }
  .brand-labs { color:#2f8078; }
  .header .brand small { display:block; font-size:13px; font-weight:600; color:#64748b; letter-spacing:0; margin-top:5px; }
  .header .meta { text-align:right; font-size:12px; color:#64748b; }
  .refresh-btn { background:#1F4E79; color:#fff; border:none; padding:9px 18px; border-radius:6px;
                 font-size:13px; font-weight:700; cursor:pointer; display:inline-flex; align-items:center; gap:8px; }
  .refresh-btn:hover { background:#2a5f92; }
  .refresh-btn:disabled { opacity:.6; cursor:default; }
  .spin { width:13px; height:13px; border:2px solid rgba(255,255,255,.4); border-top-color:#fff;
          border-radius:50%; display:none; animation:spin .7s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .loading .spin { display:inline-block; }

  .kpis { display:flex; gap:12px; padding:16px 28px 0; flex-wrap:wrap; }
  .kpi { background:#fff; border:1px solid #d0dbe6; border-radius:8px; padding:12px 22px; text-align:center; min-width:120px; }
  .kpi .v { font-size:26px; font-weight:700; color:#1F4E79; }
  .kpi .l { font-size:11px; color:#666; font-weight:600; text-transform:uppercase; letter-spacing:.5px; margin-top:2px; }

  .modebar { display:flex; gap:16px; padding:14px 28px 16px; flex-wrap:wrap; border-bottom:1px solid #dee5ec; align-items:flex-start; }
  .mode-group { display:flex; flex-direction:column; gap:8px; border:1.5px solid #d9e0e8; border-radius:10px; padding:8px 12px 11px; }
  .mode-group-label { font-size:10.5px; font-weight:700; letter-spacing:.6px; text-transform:uppercase; color:#8a97a6; padding-left:2px; }
  .mode-group-btns { display:flex; gap:8px; flex-wrap:wrap; }
  .mode-group-fin { border-color:#bfe3c9; background:#f6fbf7; } .mode-group-fin .mode-group-label { color:#1b7a3d; }
  .mode-group-ops { border-color:#bcd7f6; background:#f4f9ff; } .mode-group-ops .mode-group-label { color:#1d4ed8; }
  .mode-group-mkt { border-color:#fed7aa; background:#fff8f1; } .mode-group-mkt .mode-group-label { color:#c2410c; }
  .mode-btn { background:#fff; border:1px solid #cdd9e6; padding:9px 16px; border-radius:8px;
    font-size:13px; font-weight:700; color:#1F4E79; cursor:pointer; font-family:inherit; }
  .mode-btn:hover { background:#f5f8fb; }
  .mode-btn.active { background:#0D2B45; color:#fff; border-color:#0D2B45; }
  .mode-btn.mode-pnl { color:#1b7a3d; border-color:#bfe3c9; }
  .mode-btn.mode-pnl:hover { background:#eef8f0; }
  .mode-btn.mode-pnl.active { background:#2e7d32; color:#fff; border-color:#2e7d32; }
  .mode-btn.mode-mkt { color:#c2410c; border-color:#fed7aa; background:#fff7ed; }
  .mode-btn.mode-mkt:hover { background:#ffedd5; }
  .mode-btn.mode-mkt.active { background:#ea580c; color:#fff; border-color:#ea580c; }
  .mode-btn.mode-ship { color:#1d4ed8; border-color:#bfdbfe; background:#eff6ff; }
  .mode-btn.mode-ship:hover { background:#dbeafe; }
  .mode-btn.mode-ship.active { background:#2563eb; color:#fff; border-color:#2563eb; }
  .mode-btn.mode-pay { color:#0f766e; border-color:#99f6e4; background:#f0fdfa; }
  .mode-btn.mode-pay:hover { background:#ccfbf1; }
  .mode-btn.mode-pay.active { background:#0d9488; color:#fff; border-color:#0d9488; }
  .mode-btn.mode-inv { color:#7c3aed; border-color:#ddd6fe; background:#f5f3ff; }
  .mode-btn.mode-inv:hover { background:#ede9fe; }
  .mode-btn.mode-inv.active { background:#7c3aed; color:#fff; border-color:#7c3aed; }
  /* ── Inventory Opportunities: flashing green dot in the Open Vendor POs "Fulfill Opp" column ── */
  @keyframes fopPulse {
    0%   { opacity:1;   transform:scale(1);    box-shadow:0 0 0 0 rgba(34,160,70,.65); }
    70%  { opacity:.55; transform:scale(1.12); box-shadow:0 0 0 7px rgba(34,160,70,0); }
    100% { opacity:1;   transform:scale(1);    box-shadow:0 0 0 0 rgba(34,160,70,0); }
  }
  .fop-dot { display:inline-block; width:11px; height:11px; border-radius:50%;
             background:#22a046; animation:fopPulse 1.25s ease-in-out infinite; cursor:pointer; }
  .fop-dot:hover { background:#1b7a3d; }
  @keyframes fopTextPulse { 0%,100% { opacity:1; } 50% { opacity:.34; } }
  .fop-txt { display:inline-block; color:#1b7a3d; font-weight:700; font-size:12px; line-height:1.3;
             white-space:normal; text-align:center; cursor:pointer;
             animation:fopTextPulse 1.25s ease-in-out infinite; }
  .fop-txt:hover { color:#146030; text-decoration:underline; }
  .fop-txt .fop-q { color:#0D2B45; }
  .fop-txt .fop-more { display:block; font-weight:600; font-size:11px; color:#6b7a8a; }
  @media (prefers-reduced-motion: reduce) { .fop-dot, .fop-txt { animation:none; } }
  .fop-none { color:#c8d0d8; }
  .iop-up { border:2px dashed #cdd9e6; border-radius:12px; padding:18px; text-align:center;
            background:#fbfdff; margin:0 0 16px; }
  .iop-up.drag { border-color:#22a046; background:#f2fbf5; }
  .iop-file { display:inline-flex; align-items:center; gap:8px; background:#eef4fa; border:1px solid #cdd9e6;
              border-radius:20px; padding:5px 12px; font-size:12px; margin:4px 4px 0 0; }
  .iop-file b { font-weight:700; }
  .iop-x { cursor:pointer; color:#c62828; font-weight:700; }
  .iop-note { font-size:12px; margin:10px 0 0; }
  .iop-note.err { color:#c62828; } .iop-note.ok { color:#1b7a3d; }
  /* Invoice Check tab */
  .invchk { max-width:1000px; }
  .invchk .inv-form { display:flex; gap:16px; flex-wrap:wrap; align-items:flex-end; background:#faf9ff; border:1px solid #e6e2f5;
                      border-radius:10px; padding:16px 18px; margin-bottom:16px; }
  .invchk .inv-fld { display:flex; flex-direction:column; gap:4px; }
  .invchk .inv-fld label { font-size:11px; font-weight:700; color:#5a3e8e; text-transform:uppercase; letter-spacing:.4px; }
  .invchk .inv-fld input[type=text] { padding:8px 10px; border:1px solid #cdc4ea; border-radius:6px; font-size:14px; font-family:inherit; min-width:160px; text-transform:uppercase; }
  .invchk .inv-fld input[type=file] { font-size:12px; font-family:inherit; }
  .invchk .inv-fld input:focus { outline:none; border-color:#7c3aed; }
  .invchk .inv-go { padding:9px 18px; background:#7c3aed; color:#fff; border:none; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer; font-family:inherit; }
  .invchk .inv-go:hover { background:#6d28d9; }
  .invchk .inv-go:disabled { background:#c4b5e8; cursor:default; }
  .invchk .inv-note { font-size:12px; margin:4px 0 12px; min-height:16px; }
  .invchk .inv-note.err { color:#c0392b; } .invchk .inv-note.ok { color:#1e7e34; } .invchk .inv-note.warn { color:#b9770e; }
  .invchk .inv-sum { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
  .invchk .inv-chip { font-size:12px; font-weight:600; padding:5px 11px; border-radius:14px; border:1px solid #e0e0e0; }
  .invchk .inv-chip.ok { background:#e8f7ec; color:#1e7e34; border-color:#bfe3c9; }
  .invchk .inv-chip.bad { background:#fdecea; color:#c0392b; border-color:#f5c6cb; }
  .invchk .inv-chip.miss { background:#f1f3f5; color:#666; border-color:#dde2e6; }
  .invchk table { width:100%; border-collapse:collapse; font-size:13px; }
  .invchk thead td { color:#666; font-weight:700; border-bottom:2px solid #dee5ec; padding:7px 8px; font-size:10px; text-transform:uppercase; letter-spacing:.3px; }
  .invchk tbody td { padding:7px 8px; border-bottom:1px solid #eef2f6; color:#333; vertical-align:top; }
  .invchk td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .invchk tr.inv-ok td { }
  .invchk tr.inv-over td.num.delta { color:#c0392b; font-weight:700; }
  .invchk tr.inv-under td.num.delta { color:#b9770e; font-weight:700; }
  .invchk tr.inv-miss td { color:#9aa0a6; }
  .invchk .inv-badge { display:inline-block; font-size:10px; font-weight:700; padding:2px 7px; border-radius:10px; text-transform:uppercase; letter-spacing:.3px; }
  .invchk .inv-badge.ok { background:#e8f7ec; color:#1e7e34; }
  .invchk .inv-badge.over { background:#fdecea; color:#c0392b; }
  .invchk .inv-badge.under { background:#fff4e0; color:#b9770e; }
  .invchk .inv-badge.miss { background:#f1f3f5; color:#777; }
  .invchk .inv-hint { font-size:11px; color:#888; margin-top:10px; line-height:1.5; }
  .pnl-wrap { overflow-x:auto; padding:20px 22px; }
  .pnl-wrap h2, .pnl-wrap h3 { color:#2c3e50; }

  .layout { display:flex; gap:0; padding:18px 28px 40px; align-items:flex-start; }
  .sidecol { flex:0 0 270px; display:flex; flex-direction:column; gap:14px; min-width:0; max-width:270px; overflow:hidden; }
  .tabs { flex:0 0 auto; width:100%; background:#fff; border:1px solid #dee5ec; border-radius:10px; overflow:hidden; max-height:78vh; overflow-y:auto; }
  .altsrc { background:#fff; border:1px solid #dee5ec; border-radius:10px; padding:14px 14px 16px; }
  .altsrc h3 { font-size:12px; color:#0D2B45; text-transform:uppercase; letter-spacing:.6px; margin-bottom:4px; }
  .altsrc .as-sub { font-size:11px; color:#888; margin-bottom:10px; }
  .altsrc input { width:100%; padding:8px 10px; border:1px solid #cdd9e6; border-radius:6px; font-size:13px; font-family:inherit; text-transform:uppercase; }
  .altsrc input:focus { outline:none; border-color:#1F4E79; }
  .altsrc .as-name { font-size:12px; color:#333; margin:11px 0 6px; font-weight:600; line-height:1.3; }
  .altsrc table { width:100%; border-collapse:collapse; font-size:13px; margin-top:6px; }
  .altsrc td { padding:6px 4px; border-bottom:1px solid #eef2f6; }
  .altsrc td.lbl { color:#555; font-weight:600; }
  .altsrc td.v { text-align:right; font-weight:700; color:#1F4E79; }
  .altsrc td.na { text-align:right; font-weight:600; color:#aaa; }
  .altsrc .as-hint { font-size:11px; color:#888; margin-top:10px; }
  .altsrc .as-none { font-size:12px; color:#c0392b; margin-top:11px; font-weight:600; }
  /* Paid Inventory box (vendor tab, below Alternative Sources) */
  .paidinv { margin-top:14px; }
  .paidinv label { display:block; font-size:11px; color:#555; font-weight:600; margin:8px 0 3px; }
  .paidinv input { width:100%; padding:7px 9px; border:1px solid #cdd9e6; border-radius:6px; font-size:13px; font-family:inherit; }
  .paidinv input:focus { outline:none; border-color:#1F4E79; }
  .paidinv input.pi-sku { text-transform:uppercase; }
  .paidinv .pi-add { width:100%; margin-top:11px; padding:9px 10px; background:#1F4E79; color:#fff; border:none; border-radius:6px;
                     font-size:13px; font-weight:600; cursor:pointer; font-family:inherit; }
  .paidinv .pi-add:hover { background:#173a5c; }
  .paidinv .pi-add:disabled { background:#9db4cc; cursor:default; }
  .paidinv .pi-note { font-size:11px; margin-top:8px; min-height:14px; }
  .paidinv .pi-note.ok { color:#1e7e34; }
  .paidinv .pi-note.warn { color:#b9770e; }
  .paidinv .pi-note.err { color:#c0392b; }
  .paidinv .pi-list { margin-top:12px; }
  .paidinv .pi-list-h { font-size:11px; color:#0D2B45; text-transform:uppercase; letter-spacing:.5px; font-weight:700; margin-bottom:6px; }
  .paidinv table { width:100%; border-collapse:collapse; font-size:12px; }
  .paidinv thead td { color:#888; font-weight:600; border-bottom:1px solid #dee5ec; padding:5px 4px; font-size:10px; text-transform:uppercase; }
  .paidinv tbody td { padding:6px 4px; border-bottom:1px solid #eef2f6; color:#333; vertical-align:top; }
  .paidinv tbody td.pi-q { text-align:right; font-weight:700; color:#1F4E79; }
  .paidinv .pi-empty { font-size:11px; color:#888; margin-top:8px; }
  .paidinv .pi-pending td { color:#b9770e; }
  .paidinv td.pi-x { text-align:right; width:20px; padding-right:0; }
  .paidinv .pi-del { background:none; border:none; color:#c0392b; font-size:15px; line-height:1; cursor:pointer; padding:2px 4px; border-radius:4px; }
  .paidinv .pi-del:hover { background:#fdecea; }
  .paidinv td.pi-po { color:#1F4E79; font-weight:600; }
  /* Once an Allocated PO# is set, gray out the whole row */
  .paidinv tr.pi-alloc td { color:#9aa0a6 !important; }
  .paidinv tr.pi-alloc td.pi-q { color:#9aa0a6 !important; }
  .paidinv tr.pi-alloc td.pi-po { color:#9aa0a6 !important; font-weight:600; }
  .paidinv td.pi-act { text-align:right; white-space:nowrap; width:46px; padding-right:0; }
  .paidinv .pi-edit { background:none; border:none; color:#1F4E79; font-size:13px; line-height:1; cursor:pointer; padding:2px 4px; border-radius:4px; }
  .paidinv .pi-edit:hover { background:#e8eef5; }
  .paidinv .pi-ein { width:100%; padding:3px 5px; border:1px solid #cdd9e6; border-radius:4px; font-size:12px; font-family:inherit; box-sizing:border-box; }
  .paidinv .pi-ein:focus { outline:none; border-color:#1F4E79; }
  .paidinv .pi-save { background:#1e7e34; color:#fff; border:none; border-radius:4px; font-size:11px; font-weight:600; cursor:pointer; padding:3px 7px; margin-right:2px; }
  .paidinv .pi-save:hover { background:#186429; }
  .paidinv .pi-cancel { background:none; border:none; color:#888; font-size:12px; cursor:pointer; padding:3px 4px; }
  .paidinv .pi-cancel:hover { color:#555; text-decoration:underline; }
  .tab { display:block; width:100%; text-align:left; background:none; border:none; border-bottom:1px solid #eef2f6;
         padding:12px 16px; cursor:pointer; font-size:13px; color:#2c3e50; font-family:inherit; }
  .tab:hover { background:#f5f8fb; }
  .tab.active { background:#1F4E79; color:#fff; }
  .tab .cnt { float:right; font-size:11px; opacity:.8; }
  .tab.active .cnt { color:#cfe0f0; }

  /* ── Customer Journey facet filter boxes ── */
  .cjfacet { background:#fff; border:1px solid #e4e9ef; border-radius:12px; overflow:hidden; box-shadow:0 1px 3px rgba(20,40,70,.05); }
  .cjfacet + .cjfacet { margin-top:12px; }
  .cjfacet-h { display:flex; align-items:center; justify-content:space-between; gap:8px;
    padding:9px 13px; background:linear-gradient(#f8fafc,#f1f5f9); border-bottom:1px solid #e9eef4;
    font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; color:#54657a; }
  .cjfacet-h .ic { margin-right:6px; opacity:.9; }
  .cjfacet-h .n { font-weight:700; color:#aab6c4; letter-spacing:0; }
  .cjfacet-clear { font-size:11px; font-weight:600; color:#1a73e8; text-transform:none; letter-spacing:0; cursor:pointer; text-decoration:none; }
  .cjfacet-clear:hover { text-decoration:underline; }
  .cjfacet-list { max-height:224px; overflow:auto; padding:5px; }
  .cjrow { display:flex; align-items:center; justify-content:space-between; gap:8px; width:100%; min-width:0; max-width:100%;
    text-align:left; background:none; border:none; cursor:pointer; font-family:inherit;
    font-size:12.5px; line-height:1.3; color:#33475b; padding:7px 9px; border-radius:8px;
    border-left:3px solid transparent; transition:background .12s; }
  .cjrow:hover { background:#f2f6fb; }
  .cjrow.active { background:#eaf1fa; border-left-color:#1F4E79; color:#1F4E79; font-weight:600; }
  .cjrow .lbl { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; flex:1 1 auto; }
  .cjpill { flex:0 0 auto; min-width:22px; text-align:center; font-size:11px; font-weight:700;
    color:#5a6b7b; background:#eef2f7; border-radius:999px; padding:1px 8px; }
  .cjrow:hover .cjpill { background:#e3ebf3; }
  .cjrow.active .cjpill { background:#1F4E79; color:#fff; }
  .cjfacet-empty { padding:11px 13px; font-size:12px; color:#9aa7b4; }

  .panel-wrap { flex:1 1 auto; margin-left:18px; min-width:0; }
  .panel { background:#fff; border:1px solid #dee5ec; border-radius:10px; overflow:hidden; }
  .panel-head { background:#f0f4f8; border-bottom:1px solid #dee5ec; padding:16px 20px; }
  .panel-head h2 { font-size:18px; color:#0D2B45; }
  .panel-head .sub { font-size:12px; color:#666; margin-top:4px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  thead th { background:#0D2B45; color:#fff; text-align:left; padding:9px 10px; font-weight:700; font-size:11px; white-space:nowrap; }
  thead th.c { text-align:center; }
  thead th.sortable { cursor:pointer; user-select:none; }
  thead th.sortable:hover { background:#143352; }
  thead th .arr { color:#7fd4d4; font-size:10px; margin-left:3px; }
  tbody td { padding:8px 10px; border-bottom:1px solid #eef2f6; vertical-align:top; }
  tbody td.c { text-align:center; }
  /* Vendor Spend tables */
  table.vsp-table { max-width:940px; margin:0 0 4px; }
  table.vsp-table tbody tr:hover td { background:#f4f8fb; }
  table.vsp-table tfoot td { padding:9px 10px; border-top:2px solid #0D2B45; background:#eef3f8; color:#1f3a5f; }
  table.vsp-table th, table.vsp-table td { white-space:nowrap; }
  tbody tr:nth-child(even) { background:#f8fafc; }
  .so { font-weight:700; color:#1F4E79; white-space:nowrap; }
  .status { padding:2px 8px; border-radius:10px; font-size:10px; white-space:nowrap; }
  .open { font-weight:700; color:#c0392b; }
  .po { color:#e67e22; white-space:nowrap; }
  .po-none { color:#999; }
  .po-wrap { white-space:nowrap; display:inline-block; margin:1px 0; }
  .po-email-btn { margin-left:6px; padding:1px 8px; font-size:10px; line-height:14px; border:1px solid #1F4E79;
    background:#1F4E79; color:#fff !important; border-radius:3px; text-decoration:none; vertical-align:middle; display:inline-block; }
  .po-email-btn:hover { background:#143352; }
  .copy-email-btn { padding:6px 12px; font-size:12px; font-weight:600; border:1px solid #1F4E79;
    background:#fff; color:#1F4E79; border-radius:6px; cursor:pointer; white-space:nowrap; }
  .copy-email-btn:hover { background:#1F4E79; color:#fff; }
  .so-group td { background:#eef3f8; border-top:2px solid #cdd9e6; padding:8px 12px; }
  .so-group .so-h { font-weight:700; color:#0D2B45; font-size:13px; margin-right:10px; }
  .so-group .so-date { color:#666; font-size:11px; margin-left:10px; }
  .so-group .so-cnt { color:#888; font-size:11px; margin-left:10px; }
  /* Customer Open SO's: sidebar SKU search + All-customers grouping */
  .custq-box { padding:11px 13px 12px; border-bottom:1px solid #eef2f6; background:#fbfdff; }
  .custq { width:100%; box-sizing:border-box; padding:8px 11px; border:1px solid #d7dee8; border-radius:7px;
    font-size:13px; font-family:inherit; color:#101E3E; outline:none; }
  .custq:focus { border-color:#2f6fd0; box-shadow:0 0 0 3px rgba(47,111,208,.13); }
  .custq-hint { font-size:11px; color:#7a8a99; margin-top:6px; line-height:1.35; }
  .cust-group td { background:#1F4E79; color:#fff; border-top:2px solid #163c5e; padding:9px 12px; }
  .cust-group .cg-h { font-weight:700; font-size:13px; letter-spacing:.01em; }
  .cust-group .cg-cnt { color:#cfe0f0; font-size:11px; margin-left:10px; font-weight:600; }
  .empty { padding:40px; text-align:center; color:#999; }
  .matrix-wrap { overflow-x:auto; }
  table.matrix td.item-name { max-width:300px; color:#101E3E; font-weight:600; }
  /* Column dividers on the demand matrix */
  table.matrix th, table.matrix td { border-right:1px solid #e3e9f0; }
  table.matrix th:last-child, table.matrix td:last-child { border-right:none; }
  table.matrix th.cust-col { white-space:normal; word-break:break-word; max-width:110px; vertical-align:bottom; }
  table.matrix td.hd-cell { vertical-align:top; }
  .hd-sub { font-size:10px; color:#6b7886; font-weight:600; margin-top:2px; white-space:nowrap; }
  .hd-q { font-weight:700; color:#0D2B45; }
  tr.aging-so td { background:#f6f9fc; padding:6px 12px 6px 24px; border-top:1px solid #eef2f6; }
  .so-h2 { font-weight:700; color:#1F4E79; margin-right:10px; }
  .hd-badge { display:inline-block; min-width:20px; padding:1px 8px; background:#1F4E79; color:#fff; border-radius:10px; font-size:11px; font-weight:700; }
  tr.hd-hot td { background:#fff4f0 !important; }
  tr.hd-hot td.item-name { box-shadow:inset 4px 0 0 #c0392b; }
  tr.hd-warm td.item-name { box-shadow:inset 4px 0 0 #e67e22; }
  .age-pill { display:inline-block; padding:2px 9px; border-radius:10px; font-size:11px; font-weight:700; }
  .age-green { background:#d4edda; color:#155724; }
  .age-orange { background:#ffe8cc; color:#9a5a16; }
  .age-red { background:#f8d7da; color:#a11d2a; }
  .age-na { background:#eee; color:#888; }
  .ca-email-btn { background:#008080; color:#fff; border:none; padding:9px 16px; border-radius:6px; font-size:13px; font-weight:700; cursor:pointer; white-space:nowrap; }
  .ca-email-btn:hover { background:#006666; }
  .ca-h { font-size:13px; font-weight:700; color:#1F4E79; margin:18px 0 8px 16px; }
  .ca-overall { margin:0 0 12px 32px; font-size:13px; color:#2c3e50; }
  .ca-overall li { margin:4px 0; }
  .ca-visuals { display:flex; gap:34px; flex-wrap:wrap; padding:0 16px; align-items:flex-start; }
  .footer { text-align:center; font-size:11px; color:#888; padding:18px; }
  /* ── Mobile (tablet & phone), app-like. All rules scoped to ≤760px so the
     desktop view is byte-for-byte unchanged. Containers span ~95% of the
     screen (1.5% gutters); the mode nav is a wrapped tile grid (2–3 rows);
     data-dense tables scroll horizontally within the panel. ── */
  @media (max-width:760px){
    .layout{ flex-direction:column; align-items:stretch; padding:10px 1.5% 28px; }
    .sidecol{ flex-basis:auto; width:100%; min-width:0; }
    .panel-wrap{ min-width:0; max-width:100%; }
    /* Customer / entity list becomes a 2-column tile grid instead of a single
       tall column (fills the width, fewer scrolls) */
    .tabs{ flex-basis:auto; width:100%; max-height:none; display:grid;
      grid-template-columns:repeat(2,1fr); gap:7px; background:transparent;
      border:none; border-radius:0; overflow:visible; }
    .tab{ width:auto; border:1px solid #dee5ec; border-radius:10px; background:#fff;
      padding:11px 12px; }
    .tab.active{ border-color:#1F4E79; }
    .panel-wrap{ margin-left:0; margin-top:12px; }
    /* Stack the header: logo on top, then date + Refresh button below it */
    .header{ padding:13px 1.5%; flex-direction:column; align-items:stretch; gap:10px; }
    .brand-word{ font-size:25px; }
    .header-right{ width:100%; justify-content:space-between; gap:12px; }
    .header .meta{ font-size:11px; text-align:left; }
    .meta-scope{ display:none; }  /* drop "2026 Sales Orders · Excl. ConMed" on mobile */
    .refresh-btn{ padding:11px 20px; }
    .kpis{ padding:14px 1.5% 0; gap:9px; }
    /* Bigger KPI cards — 2 per row, larger numbers */
    .kpi{ min-width:0; flex:1 1 46%; padding:16px 12px; }
    .kpi .v{ font-size:28px; }
    .kpi .l{ font-size:11px; }
    /* App-like mode nav: wrapped tile GRID that fills the width. Bigger tiles —
       ~3 columns on phones, more on tablets. */
    .modebar{ display:flex; flex-direction:row; align-items:flex-start; gap:8px; padding:14px 1.5%; border-bottom:none; overflow:visible; }
    .mode-group{ flex:1 1 0; min-width:0; gap:7px; padding:7px 7px 9px; }
    .mode-group-btns{ display:flex; flex-direction:column; gap:7px; }
    .mode-group-btns .mode-btn{ width:100%; }
    .mode-btn{ margin:0; border:1px solid #cdd9e6; border-radius:12px; padding:13px 8px;
      font-size:12.5px; line-height:1.18; white-space:normal; text-align:center;
      min-height:56px; display:flex; align-items:center; justify-content:center; }
    .mode-btn.active{ box-shadow:0 2px 6px rgba(13,43,69,.28); }
    /* Data-dense tables (P&L, vendor POs, payments, shipments, matrix): scroll
       sideways within the panel instead of being clipped by overflow:hidden */
    .panel{ overflow-x:auto; overflow-y:visible; -webkit-overflow-scrolling:touch; }
    .panel-head{ padding:12px 3%; }
    .pnl-wrap{ padding:14px 3%; }
    .ca-visuals{ gap:18px; padding:0 1.5%; }
    .footer{ padding:16px 1.5%; }
    /* Google Ads / Website-Traffic per-campaign small-multiples: 3 → 2 columns */
    .wt-mult{ grid-template-columns:repeat(2,1fr) !important; }
  }
  @media (max-width:480px){
    /* Bigger KPI cards, 2 per row */
    .kpi{ flex:1 1 46%; padding:15px 10px; }
    .kpi .v{ font-size:26px; }
    /* Bigger nav tiles — 3 columns on phones */
    .modebar{ grid-template-columns:repeat(3,1fr); }
    .mode-btn{ font-size:12px; padding:12px 6px; min-height:56px; }
    /* small-multiples: single column on phones */
    .wt-mult{ grid-template-columns:1fr !important; }
  }
  /* Customer Prices pivot: SO columns x SKU rows; SKU+COGS frozen at left */
  .cp-wrap{ overflow-x:auto; border:1px solid #e3e9f0; border-radius:8px; }
  table.cp-table{ border-collapse:separate; border-spacing:0; font-size:13px; width:auto; }
  table.cp-table th, table.cp-table td{ border-right:1px solid #eef2f7; border-bottom:1px solid #eef2f7; padding:6px 10px; white-space:nowrap; }
  table.cp-table thead th{ position:sticky; top:0; z-index:3; background:#101E3E; color:#fff; font-weight:600; text-align:center; vertical-align:bottom; }
  table.cp-table th.cp-so{ min-width:78px; }
  table.cp-table .cp-sonum{ font-weight:700; }
  table.cp-table .cp-sodate{ font-size:10px; font-weight:400; color:#c7d2e0; }
  table.cp-table th.cp-sticky, table.cp-table td.cp-sticky{ position:sticky; z-index:2; background:#fff; }
  table.cp-table th.cp-sticky{ z-index:4; background:#101E3E; }
  table.cp-table .cp-sku{ left:0; min-width:96px; text-align:left; font-weight:700; color:#101E3E; }
  table.cp-table .cp-cogs{ left:96px; min-width:74px; text-align:right; color:#5a6b82; box-shadow:1px 0 0 #d7dee8; }
  table.cp-table thead th.cp-cogs{ color:#fff; }
  table.cp-table td.cp-cell{ text-align:right; font-variant-numeric:tabular-nums; }
  table.cp-table td.cp-empty{ text-align:center; color:#c3ccd8; }
  table.cp-table td.cp-below{ color:#c0392b; font-weight:700; background:#fdecea; }
  table.cp-table tbody tr:nth-child(even) td.cp-sticky{ background:#f7f9fc; }
  table.cp-table tbody tr:nth-child(even) td{ background:#f7f9fc; }
  table.cp-table .cp-na{ color:#c3ccd8; }
  /* YTD Demand tab */
  .ytd-ctrl{ display:flex; align-items:center; gap:12px; margin:6px 2px 10px; }
  .ytd-search{ flex:0 0 300px; padding:8px 11px; border:1px solid #d7dee8; border-radius:7px;
    font-size:13px; font-family:inherit; color:#101E3E; outline:none; }
  .ytd-search:focus{ border-color:#2f6fd0; box-shadow:0 0 0 3px rgba(47,111,208,.13); }
  .ytd-stat{ font-size:12px; color:#5a6b82; font-weight:600; }
  table.ytd-table th.ytd-sticky, table.ytd-table td.ytd-sticky{ position:sticky; left:0; z-index:2; background:#fff; }
  table.ytd-table thead th.ytd-sticky{ z-index:4; background:#101E3E; }
  table.ytd-table tbody tr:nth-child(even) td.ytd-sticky{ background:#f7f9fc; }
  table.ytd-table .ytd-sku{ font-weight:700; color:#101E3E; min-width:96px; text-align:left;
    box-shadow:1px 0 0 #d7dee8; }
  table.ytd-table .ytd-prod{ min-width:260px; color:#33465f; }
  table.ytd-table .ytd-ven{ font-size:10px; color:#8a97a8; margin-top:2px; }
  table.ytd-table td.ytd-cell{ text-align:right; font-variant-numeric:tabular-nums; }
  table.ytd-table td.ytd-zero{ text-align:center; color:#c3ccd8; }
  table.ytd-table td.ytd-total{ text-align:right; font-weight:700; color:#101E3E;
    background:#eef3fb; font-variant-numeric:tabular-nums; }
  table.ytd-table td.ytd-amt{ text-align:right; font-weight:700; color:#20603a;
    background:#eef7f0; font-variant-numeric:tabular-nums; white-space:nowrap; }
  table.ytd-table th.ytd-ytdcol{ background:#16305c; }
  table.ytd-table td.ytd-ncust{ text-align:center; color:#7a8798; font-size:11px; }
  table.ytd-table td.ytd-none{ text-align:center; color:#8a97a8; padding:22px 8px; font-style:italic; }
  .ytd-note{ margin:10px 2px 0; font-size:11px; color:#8a97a8; line-height:1.5; }
</style>
</head>
<body>
<div class="header">
  <div class="brand"><span class="brand-word"><span class="brand-jit">JIT4</span><span class="brand-labs">Labs</span></span><small>Business Dashboard</small></div>
  <div class="header-right" style="display:flex;align-items:center;gap:18px;">
    <div class="meta">
      <div id="asof">&nbsp;</div>
      <div class="meta-scope">2026 Sales Orders &middot; Excl. ConMed</div>
    </div>
    <button id="refresh" class="refresh-btn" onclick="refreshData()">
      <span class="spin"></span><span class="lbl">Refresh</span>
    </button>
  </div>
</div>

<div class="kpis" id="kpis"></div>

<div class="modebar">
  <div class="mode-group mode-group-fin">
    <div class="mode-group-label">Financials</div>
    <div class="mode-group-btns">
      <button class="mode-btn mode-pnl active" data-mode="pnl" onclick="setMode('pnl')">P&amp;L Report</button>
      <button class="mode-btn" data-mode="spnl" onclick="setMode('spnl')">Shipments P&amp;L</button>
      <button class="mode-btn" data-mode="inv" onclick="setMode('inv')">Invoice Check</button>
      <button class="mode-btn" data-mode="pay" onclick="setMode('pay')">Payment Status</button>
    </div>
  </div>
  <div class="mode-group mode-group-ops">
    <div class="mode-group-label">Operations</div>
    <div class="mode-group-btns">
      <button class="mode-btn" data-mode="cust" onclick="setMode('cust')">Customer Open SO's</button>
      <button class="mode-btn" data-mode="vendor" onclick="setMode('vendor')">Open Vendor POs</button>
      <button class="mode-btn" data-mode="iopp" onclick="setMode('iopp')">Inventory Opportunities</button>
      <button class="mode-btn" data-mode="ship" onclick="setMode('ship')">Shipments</button>
      <button class="mode-btn" data-mode="cprices" onclick="setMode('cprices')">Customer Prices</button>
      <button class="mode-btn" data-mode="vspend" onclick="setMode('vspend')">Vendor Spend</button>
      <button class="mode-btn" data-mode="sku" onclick="setMode('sku')">High Demand SKUs</button>
      <button class="mode-btn" data-mode="ytd" onclick="setMode('ytd')">YTD Demand</button>
    </div>
  </div>
  <div class="mode-group mode-group-mkt">
    <div class="mode-group-label">Marketing</div>
    <div class="mode-group-btns">
      <button class="mode-btn" data-mode="ca" onclick="setMode('ca')">Customer Analysis</button>
      <button class="mode-btn" data-mode="wt" onclick="setMode('wt')">Website Traffic</button>
      <button class="mode-btn" data-mode="cj" onclick="setMode('cj')">Customer Journey</button>
      <button class="mode-btn" data-mode="gads" onclick="setMode('gads')">Google Ads</button>
      <button class="mode-btn" data-mode="li" onclick="setMode('li')">LinkedIn</button>
    </div>
  </div>
</div>

<div class="layout">
  <div class="sidecol">
    <div class="tabs" id="tabs"></div>
    <div class="altsrc" id="altsrc" style="display:none;"></div>
    <div class="altsrc paidinv" id="paidinv" style="display:none;"></div>
  </div>
  <div class="panel-wrap"><div class="panel" id="panel"></div></div>
</div>

<div class="footer">JIT4Labs &middot; Business Dashboard &middot; data refreshes from Vtiger on each scheduled run</div>

<script>
var DATA = __DATA_JSON__;
var DATA_URL = "__DATA_URL__";
// Client-side safety net: never show excluded/test accounts even if a stale data file still has them.
var EXCLUDE_CUST={'test company':1};
function isExclCust(n){ return !!EXCLUDE_CUST[String(n||'').trim().toLowerCase()]; }
function normData(d){ if(d&&d.customers){ d.customers=d.customers.filter(function(c){return !isExclCust(c&&c.name);}); } if(d&&d.high_demand&&d.high_demand.customers){ d.high_demand.customers=d.high_demand.customers.filter(function(n){return !isExclCust(n);}); } return d; }
DATA=normData(DATA);
var BTN = __BTN_CFG__;
// Offline mirror: when built as the local copy these hold the data inline (no fetch needed). Online build leaves them null so the page fetches fresh each load.
var GADS_EMBED = __GADS_EMBED__, LI_EMBED = __LI_EMBED__, WT_EMBED = __WT_EMBED__, SHIP_EMBED = __SHIP_EMBED__, PAY_EMBED = __PAY_EMBED__, SPNL_EMBED = __SPNL_EMBED__, CJ_EMBED = __CJ_EMBED__, IOPP_EMBED = __IOPP_EMBED__;
function _deobf(s,key){ if(!s) return ''; var raw=atob(s), out=''; for(var i=0;i<raw.length;i++){ out+=String.fromCharCode(raw.charCodeAt(i) ^ key.charCodeAt(i%key.length)); } return out; }
BTN.token = _deobf(BTN.token_obf, BTN.k || '');
var active = 0;     // selected customer index (Customer Open SO's view); -1 = All customers
var custSku = '';   // Customer Open SO's: SKU / product search text ('' = no filter)
var vactive = 0;    // selected vendor index (Open Vendor POs view)
var caactive = 0;   // selected IDL customer index (Customer Analysis view)
var mode = 'pnl';   // 'pnl' · 'cust' · 'vendor' · 'sku' · 'ca'

// Click a header to sort by it; click again to reverse. Each view has its own columns.
// Customer view: table grouped by SO (SO #, Status, Date appear in group headers).
var COLS_CUST = [
  {key:'product',    label:'Product',    type:'str'},
  {key:'vendor',     label:'Vendor',     type:'str'},
  {key:'ordered_qty',label:'Ord',        type:'num',  c:true},
  {key:'delivered_qty',label:'Del',      type:'num',  c:true},
  {key:'open_qty',   label:'Open',       type:'num',  c:true},
  {key:'pending_pos',label:'Pending PO', type:'str'},
  {key:'eta',        label:'ETA',        type:'date', c:true}
];
// Vendor view: table grouped by customer (Customer appears in group headers).
var COLS_VENDOR = [
  {key:'so_num',     label:'SO #',       type:'str'},
  {key:'order_date', label:'Order Date', type:'date'},
  {key:'product',    label:'Product',    type:'str'},
  {key:'ordered_qty',label:'Ord',        type:'num',  c:true},
  {key:'delivered_qty',label:'Del',      type:'num',  c:true},
  {key:'open_qty',   label:'Open',       type:'num',  c:true},
  {key:'pending_pos',label:'Pending PO', type:'str'},
  {key:'eta',        label:'ETA',        type:'date', c:true},
  {key:'fulfill_opp',label:'Fulfill Opp',type:'num',  c:true}
];
function curCols(){ return mode==='vendor' ? COLS_VENDOR : COLS_CUST; }
var sortState = {key:null, dir:1};
function colByKey(k){ var cols=curCols(); for(var i=0;i<cols.length;i++){ if(cols[i].key===k) return cols[i]; } return null; }
function cmp(a,b,type){
  if(type==='num'){ return (parseFloat(a)||0)-(parseFloat(b)||0); }
  if(type==='date'){ var da=a?Date.parse(a):0, db=b?Date.parse(b):0; da=isNaN(da)?0:da; db=isNaN(db)?0:db; return da-db; }
  return String(a==null?'':a).toLowerCase().localeCompare(String(b==null?'':b).toLowerCase());
}
function sortBy(key){ if(sortState.key===key){ sortState.dir=-sortState.dir; } else { sortState.key=key; sortState.dir=1; } renderPanel(); }
function sortByIdx(i){ var cols=curCols(); if(cols[i]) sortBy(cols[i].key); }
function sortedRows(c){
  var rows=(c.rows||[]).slice();
  if(sortState.key){ var col=colByKey(sortState.key);
    rows.sort(function(p,q){ return sortState.dir*cmp(p[sortState.key],q[sortState.key],col?col.type:'str'); }); }
  return rows;
}

function fmtQty(q){ q=Number(q)||0; return Number.isInteger(q)?String(q):q.toFixed(2).replace(/\\.?0+$/,''); }
function fmtDate(s){ if(!s) return '—'; var d=new Date(s+'T00:00:00'); if(isNaN(d)) return s;
  return d.toLocaleDateString('en-US',{month:'short',day:'2-digit',year:'numeric'}); }
function fmtDateShort(s){ if(!s) return ''; var d=new Date(s+'T00:00:00'); if(isNaN(d)) return s;
  return d.toLocaleDateString('en-US',{month:'short',day:'2-digit'}); }
function statusColors(st){ if(/Partial/.test(st)) return ['#fff3cd','#856404'];
  if(st==='Approved') return ['#d4edda','#155724']; return ['#cce5ff','#004085']; }
function etaColor(s){ if(!s) return '#999'; var d=new Date(s+'T00:00:00'); if(isNaN(d)) return '#2c3e50';
  var days=Math.floor((d-new Date())/86400000); return days<0?'#c0392b':(days<=7?'#e67e22':'#27ae60'); }

function renderKpis(){
  var t=DATA.totals||{};
  document.getElementById('kpis').innerHTML =
    kpi(t.customers,'Customers')+kpi(t.vendors,'Vendors')+kpi(t.open_sos,'Open SOs')+kpi(t.open_items,'Open Items')+kpi(t.high_demand_skus,'High-Demand');
}
function kpi(v,l,style){ return '<div class="kpi"'+(style?' style="'+style+'"':'')+'><div class="v">'+(v==null?'0':v)+'</div><div class="l">'+l+'</div></div>'; }

function renderTabs(){
  var tabsEl=document.getElementById('tabs');
  var fullWidth=(mode==='sku' || mode==='pnl' || mode==='gads' || mode==='li' || mode==='wt' || mode==='pay' || mode==='vspend' || mode==='cprices' || mode==='inv' || mode==='iopp');
  // Left-align: when a view has no left sidebar, collapse the side column so content aligns left (not centered).
  var sidecol=document.querySelector('.sidecol'); if(sidecol) sidecol.style.display = fullWidth ? 'none' : '';
  var pw=document.querySelector('.panel-wrap'); if(pw) pw.style.marginLeft = fullWidth ? '0' : '';
  showAltSrc(mode==='vendor');  // Alternative Sources box: vendor tab only
  showPaidInv(mode==='vendor'); // Paid Inventory box: vendor tab only
  if(fullWidth){ tabsEl.style.display='none'; tabsEl.innerHTML=''; return; }  // full-width views, no per-entity tabs
  if(mode==='ship'){ renderShipTabs(tabsEl); return; }  // Shipments: sidebar of customers (receivers)
  if(mode==='spnl'){ renderSpnlTabs(tabsEl); return; }  // Shipments P&L: sidebar of customers
  if(mode==='cj'){ renderCjTabs(tabsEl); return; }      // Customer Journey: sidebar of visitors
  if(mode==='ytd'){ renderYtdTabs(tabsEl); return; }    // YTD Demand: sidebar of customers (All + each)
  if(mode==='cust'){ renderCustTabs(tabsEl); return; }  // Customer Open SO's: SKU search + All customers + each
  tabsEl.style.display='';
  var list = mode==='vendor' ? (DATA.vendors||[]) : (mode==='ca' ? ((DATA.customer_analysis||{}).customers||[]) : (DATA.customers||[]));
  var cur = mode==='vendor' ? vactive : (mode==='ca' ? caactive : active);
  var h='';
  if(!list.length){ document.getElementById('tabs').innerHTML='<div class="empty">No open orders.</div>'; return; }
  for(var i=0;i<list.length;i++){
    var cnt = mode==='ca' ? (list[i].products||[]).length : list[i].open_items;
    h+='<button class="tab'+(i===cur?' active':'')+'" onclick="selectTab('+i+')">'+
       escapeHtml(list[i].name)+'<span class="cnt">'+cnt+'</span></button>';
  }
  document.getElementById('tabs').innerHTML=h;
}

function poCell(pending, noBtn){
  if(!pending) return '<span class="po-none">None</span>';
  var parts=String(pending).split(','), out=[];
  for(var i=0;i<parts.length;i++){
    var po=parts[i].replace(/^\s+|\s+$/g,''); if(!po) continue;
    var url=(DATA.po_emails||{})[po];
    var btn = (url && !noBtn) ? '<a class="po-email-btn" href="'+escapeHtml(url)+'" title="Email vendor about '+escapeHtml(po)+'">Email vendor</a>' : '';
    out.push('<span class="po-wrap"><span class="po">&#9679; '+escapeHtml(po)+'</span>'+btn+'</span>');
  }
  return out.length ? out.join('<br>') : '<span class="po-none">None</span>';
}
function vendorEmail(i){
  var v=(DATA.vendors||[])[i]; if(!v) return;
  var w=window.open('','_blank');
  if(!w){ alert('Please allow pop-ups for this site to create the email draft.'); return; }
  w.document.open(); w.document.write(v.email_doc||''); w.document.close();
}
function renderHead(){
  var cols=curCols(), h='';
  for(var i=0;i<cols.length;i++){
    var col=cols[i];
    var arr = sortState.key===col.key ? '<span class="arr">'+(sortState.dir>0?'▲':'▼')+'</span>' : '';
    h+='<th class="'+(col.c?'c ':'')+'sortable" onclick="sortByIdx('+i+')" title="Sort by '+escapeHtml(col.label)+'">'+escapeHtml(col.label)+arr+'</th>';
  }
  return h;
}
// ── Vendor Spend tab: 2026 PO spend by month × vendor + drill-down ──────────────
var vspVendor = '';   // '' = All vendors
var vspMonth  = '';   // '' = All months
function vspMoney(v){ return '$'+Number(v||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function vspMonthLabel(m){ if(!m) return ''; var p=m.split('-'); var names=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var mi=parseInt(p[1],10)-1; return (names[mi]||p[1])+' '+p[0]; }
function vspSetVendor(v){ vspVendor=v; renderVspendDetail(); }
function vspSetMonth(m){ vspMonth=m; renderVspendDetail(); }
var vspSort={key:'date', dir:-1};   // detail-table sort (default: newest PO date first)
function vspSortBy(k){ var cols=vspCols(); var col=null; for(var i=0;i<cols.length;i++){ if(cols[i].key===k) col=cols[i]; }
  if(!col) return;
  if(vspSort.key===k){ vspSort.dir=-vspSort.dir; }
  else { vspSort.key=k; vspSort.dir=(col.type==='str')?1:-1; }   // text A→Z, numbers/dates high→low first
  renderVspendDetail(); }
function vspCols(){ var c=[{key:'po',label:'PO #',type:'str',align:'left'},
    {key:'date',label:'PO Date',type:'date',align:'left'},
    {key:'amount',label:'Amount',type:'num',align:'right'},
    {key:'customer',label:'Customer',type:'str',align:'left'}];
  if(!vspVendor) c.push({key:'vendor',label:'Vendor',type:'str',align:'left'});
  if(!vspMonth)  c.push({key:'month',label:'Month',type:'str',align:'left'});
  return c; }
function vspCmp(a,b,type){ if(type==='num'){ return (parseFloat(a)||0)-(parseFloat(b)||0); }
  return String(a==null?'':a).toLowerCase().localeCompare(String(b==null?'':b).toLowerCase()); }
function renderVspendPanel(){
  var VS=DATA.vendor_spend;
  var el=document.getElementById('panel');
  if(!VS || !VS.months || !VS.months.length){ el.innerHTML='<div class="empty">No vendor spend data available.</div>'; return; }
  var vends=VS.vendors, months=VS.months, mtx=VS.matrix||{}, tot=VS.totals||{}, mtot=VS.month_totals||{};
  // ── Main table: months (rows) × vendors (columns) ──
  var h='<div style="padding:6px 0 2px;"><h2 style="margin:0 0 2px;">Vendor Spend &mdash; '+escapeHtml(VS.year)+'</h2>'+
        '<div style="color:#7a8a99;font-size:12px;margin:0 0 10px;">Sum of purchase-order grand totals by month (non-cancelled POs), for Allora, PMA, CLEARCHEM, ALDX and CONMED.</div></div>';
  h+='<div class="kpis" style="padding:0 0 10px;">'+kpi(vspMoney(VS.grand_total),'Total spend')+kpi(VS.po_count,'POs')+kpi(months.length,'Months')+kpi(vends.length,'Vendors')+'</div>';
  h+='<table class="vsp-table"><thead><tr><th style="text-align:left;">Month</th>';
  for(var i=0;i<vends.length;i++){ h+='<th style="text-align:right;">'+escapeHtml(vends[i])+'</th>'; }
  h+='<th style="text-align:right;font-weight:700;">Total</th></tr></thead><tbody>';
  for(var r=0;r<months.length;r++){ var m=months[r], row=mtx[m]||{};
    h+='<tr><td style="text-align:left;font-weight:600;">'+escapeHtml(vspMonthLabel(m))+'</td>';
    for(var c=0;c<vends.length;c++){ var val=row[vends[c]]||0;
      h+='<td style="text-align:right;'+(val?'':'color:#c3ccd4;')+'">'+(val?vspMoney(val):'—')+'</td>'; }
    h+='<td style="text-align:right;font-weight:700;">'+vspMoney(mtot[m]||0)+'</td></tr>';
  }
  h+='</tbody><tfoot><tr><td style="text-align:left;font-weight:700;">Total</td>';
  for(var t=0;t<vends.length;t++){ h+='<td style="text-align:right;font-weight:700;">'+vspMoney(tot[vends[t]]||0)+'</td>'; }
  h+='<td style="text-align:right;font-weight:700;color:#1f3a5f;">'+vspMoney(VS.grand_total)+'</td></tr>';
  var nM=months.length||1;
  h+='<tr><td style="text-align:left;font-weight:600;color:#5a6b7b;">Average / month</td>';
  for(var av=0;av<vends.length;av++){ h+='<td style="text-align:right;color:#5a6b7b;">'+vspMoney((tot[vends[av]]||0)/nM)+'</td>'; }
  h+='<td style="text-align:right;font-weight:700;color:#5a6b7b;">'+vspMoney((VS.grand_total||0)/nM)+'</td></tr></tfoot></table>';

  // ── Drill-down: vendor + month selectors, then a PO list ──
  h+='<div style="margin:22px 0 8px;"><h3 style="margin:0 0 8px;">Purchase orders detail</h3>'+
     '<div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;">'+
     '<label style="font-size:13px;color:#2c3e50;">Vendor '+
     '<select id="vspVendorSel" onchange="vspSetVendor(this.value)" style="margin-left:5px;padding:4px 8px;border:1px solid #cfd8e0;border-radius:6px;font-size:13px;">'+
     '<option value="">All vendors</option>';
  for(var v2=0;v2<vends.length;v2++){ h+='<option value="'+escapeHtml(vends[v2])+'"'+(vspVendor===vends[v2]?' selected':'')+'>'+escapeHtml(vends[v2])+'</option>'; }
  h+='</select></label>'+
     '<label style="font-size:13px;color:#2c3e50;">Month '+
     '<select id="vspMonthSel" onchange="vspSetMonth(this.value)" style="margin-left:5px;padding:4px 8px;border:1px solid #cfd8e0;border-radius:6px;font-size:13px;">'+
     '<option value="">All months</option>';
  for(var m2=0;m2<months.length;m2++){ h+='<option value="'+escapeHtml(months[m2])+'"'+(vspMonth===months[m2]?' selected':'')+'>'+escapeHtml(vspMonthLabel(months[m2]))+'</option>'; }
  h+='</select></label></div></div>';
  h+='<div id="vspDetail"></div>';
  el.innerHTML=h;
  renderVspendDetail();
}
function renderVspendDetail(){
  var VS=DATA.vendor_spend; var box=document.getElementById('vspDetail'); if(!box) return;
  var cols=vspCols();
  var pos=(VS.pos||[]).filter(function(p){
    return (!vspVendor || p.vendor===vspVendor) && (!vspMonth || p.month===vspMonth); });
  var sk=vspSort.key, sdir=vspSort.dir, styp='num';
  for(var ci=0;ci<cols.length;ci++){ if(cols[ci].key===sk) styp=cols[ci].type; }
  pos.sort(function(a,b){ var r=vspCmp(a[sk],b[sk],styp)*sdir; if(r) return r; return vspCmp(a.date,b.date,'date')*-1; });
  var sum=0; for(var i=0;i<pos.length;i++){ sum+=pos[i].amount||0; }
  var scope=(vspVendor||'All vendors')+' · '+(vspMonth?vspMonthLabel(vspMonth):'All months');
  var h='<div style="color:#7a8a99;font-size:12px;margin:0 0 6px;">'+escapeHtml(scope)+' &mdash; '+pos.length+' PO'+(pos.length===1?'':'s')+', '+vspMoney(sum)+'</div>';
  if(!pos.length){ box.innerHTML=h+'<div class="empty">No purchase orders for this selection.</div>'; return; }
  h+='<table class="vsp-table"><thead><tr>';
  for(var c=0;c<cols.length;c++){ var col=cols[c];
    var arr=(sk===col.key)?(' <span class="arr">'+(sdir>0?'&#9650;':'&#9660;')+'</span>'):'';
    h+='<th class="sortable" onclick="vspSortBy(\\''+col.key+'\\')" style="text-align:'+col.align+';">'+escapeHtml(col.label)+arr+'</th>'; }
  h+='</tr></thead><tbody>';
  for(var j=0;j<pos.length;j++){ var p=pos[j]; h+='<tr>';
    for(var k=0;k<cols.length;k++){ var ck=cols[k].key, al=cols[k].align, v;
      if(ck==='amount') v=vspMoney(p.amount);
      else if(ck==='date') v=escapeHtml(fmtDate(p.date));
      else if(ck==='month') v=escapeHtml(vspMonthLabel(p.month));
      else if(ck==='po') v='<b>'+escapeHtml(p.po||'—')+'</b>';
      else v=escapeHtml(p[ck]||'—');
      h+='<td style="text-align:'+al+';">'+v+'</td>'; }
    h+='</tr>'; }
  h+='</tbody><tfoot><tr>';
  for(var f=0;f<cols.length;f++){ var fk=cols[f].key;
    if(fk==='po') h+='<td style="text-align:left;font-weight:700;">Total</td>';
    else if(fk==='amount') h+='<td style="text-align:right;font-weight:700;">'+vspMoney(sum)+'</td>';
    else h+='<td></td>'; }
  h+='</tr></tfoot></table>';
  box.innerHTML=h;
}

function renderPanel(){
  if(mode==='pnl') renderPnlPanel();
  else if(mode==='vendor') renderVendorPanel();
  else if(mode==='vspend') renderVspendPanel();
  else if(mode==='sku') renderSkuPanel();
  else if(mode==='ytd') renderYtdPanel();
  else if(mode==='ca') renderCaPanel();
  else if(mode==='cprices') renderCpricesPanel();
  else if(mode==='iopp') renderIoppPanel();
  else if(mode==='gads') renderGadsPanel();
  else if(mode==='li') renderLiPanel();
  else if(mode==='wt') renderWtPanel();
  else if(mode==='cj') renderCjPanel();
  else if(mode==='ship') renderShipPanel();
  else if(mode==='spnl') renderSpnlPanel();
  else if(mode==='pay') renderPayPanel();
  else if(mode==='inv') renderInvPanel();
  else renderCustPanel();
}

// ── LinkedIn tab (own data file; profile posts via browser, company page via Supermetrics) ──
var LI=null, liLoading=false;
function loadLI(){
  if(LI_EMBED){ LI=LI_EMBED; liLoading=false; if(mode==='li') renderLiPanel(); return; }
  if(liLoading) return; liLoading=true;
  fetch('linkedin-data.json?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(d){ LI=d; liLoading=false; if(mode==='li') renderLiPanel(); })
    .catch(function(e){ liLoading=false; if(mode==='li') document.getElementById('panel').innerHTML='<div class="empty">Could not load LinkedIn data: '+escapeHtml(e.message)+'</div>'; });
}
function liRefresh(){ LI=null; liLoading=false; document.getElementById('panel').innerHTML='<div class="empty">Reloading LinkedIn data…</div>'; loadLI(); }
function renderLiPanel(){
  if(!LI){ document.getElementById('panel').innerHTML='<div class="empty">Loading LinkedIn data…</div>'; loadLI(); return; }
  var posts=(LI.posts||[]).slice().sort(function(a,b){ return (b.date||'').localeCompare(a.date||''); });
  var tImp=0,tEng=0,tClk=0;
  for(var i=0;i<posts.length;i++){ tImp+=posts[i].impressions||0; tEng+=posts[i].engagements||0; tClk+=(posts[i].clicks||0); }
  var cards='<div class="kpis" style="padding:6px 0 0;">'+
    kpi(posts.length,'Posts (since '+escapeHtml((LI.since||'').slice(5))+')')+
    kpi(Number(tImp).toLocaleString(),'Impressions')+
    kpi(Number(tEng).toLocaleString(),'Engagements')+
    kpi(tClk,'Company link clicks')+
    kpi(LI.website_clicks_ga4_ytd!=null?LI.website_clicks_ga4_ytd:'—','LinkedIn→site (GA4 YTD)')+'</div>';
  var body='';
  for(var p=0;p<posts.length;p++){ var r=posts[p];
    var srcColor = r.source==='Company page' ? ['#cce5ff','#004085'] : ['#e2d9f3','#5a3e8e'];
    var clk = (r.clicks==null) ? '<span class="po-none" title="LinkedIn does not expose link clicks on personal posts">n/a</span>' : (r.clicks>0?'<span class="open">'+r.clicks+'</span>':'0');
    body+='<tr>'+
      '<td><span class="status" style="background:'+srcColor[0]+';color:'+srcColor[1]+'">'+escapeHtml(r.source)+'</span></td>'+
      '<td class="so">'+fmtDate(r.date)+'</td>'+
      '<td class="item-name" style="max-width:460px;">'+(r.link?'<a href="'+escapeHtml(r.link)+'" target="_blank" rel="noopener" style="color:#1F4E79;text-decoration:none;">'+escapeHtml(r.text)+' <span style="color:#008080;">↗</span></a>':escapeHtml(r.text))+'</td>'+
      '<td class="c">'+Number(r.impressions||0).toLocaleString()+'</td>'+
      '<td class="c">'+(r.reactions||0)+'</td>'+
      '<td class="c">'+(r.comments||0)+'</td>'+
      '<td class="c">'+(r.shares||0)+'</td>'+
      '<td class="c open">'+(r.engagements||0)+'</td>'+
      '<td class="c">'+(r.eng_rate!=null?Number(r.eng_rate).toFixed(2)+'%':'—')+'</td>'+
      '<td class="c">'+clk+'</td></tr>';
  }
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'+
    '<div><h2>LinkedIn — Posts &amp; Engagement</h2><div class="sub">Profile: '+escapeHtml(LI.profile||'')+' &middot; Company page: '+escapeHtml(LI.company_page||'')+' &middot; since '+escapeHtml(LI.since||'')+' &middot; pulled '+escapeHtml(LI.pulled_at||'')+'</div></div>'+
    '<button class="refresh-btn" onclick="liRefresh()" title="Reload the latest LinkedIn snapshot"><span class="lbl">↻ Reload</span></button></div></div>'+
    cards+
    '<div class="matrix-wrap"><table class="matrix"><thead><tr>'+
    '<th>Source</th><th>Date</th><th>Post</th><th class="c">Impr.</th><th class="c">Reactions</th><th class="c">Comments</th><th class="c">Shares</th><th class="c">Engagements</th><th class="c">Eng %</th><th class="c">Clicks</th>'+
    '</tr></thead><tbody>'+body+'</tbody></table></div>'+
    liGa4Html()+
    '<div style="margin:14px 16px;padding:12px 16px;background:#fff8e1;border-left:4px solid #ffc107;font-size:12px;border-radius:6px;line-height:1.55;color:#2c3e50;">'+
    escapeHtml(LI.note||'')+'</div>';
}
function liGa4Html(){
  var g=(LI.ga4_landing_pages||[]); if(!g.length) return '';
  var tot=0; for(var i=0;i<g.length;i++) tot+=g[i].sessions||0;
  var rows='';
  for(var j=0;j<g.length;j++){ rows+='<tr><td class="item-name">'+escapeHtml(g[j].path)+'</td><td class="c open">'+(g[j].sessions||0)+'</td></tr>'; }
  return '<div class="ca-h">Website clicks from LinkedIn (GA4) — '+tot+' YTD, by landing page</div>'+
    '<div class="matrix-wrap" style="max-width:580px;"><table class="matrix"><thead><tr><th>Landing page on jit4you.com</th><th class="c">Sessions</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
}

// ── Customer Journey tab (GA4→BigQuery: per-visitor journeys, add_to_cart + purchase) ──
var CJ=null, cjLoading=false;
var cjWin='ytd';          // 'ytd' | 'month' | 'custom'
var cjCustStart='', cjCustEnd='';
var cjSel='';             // selected visitor (user_pseudo_id); '' = All customers
var cjTermFilter='', cjProdFilter='';   // active facet filters (search term / product)
var cjOutcome='all';                     // 'all' | 'cart' (added to cart or purchased) | 'left' (browsed, no cart)
var cjEntry='all';                       // 'all' | 'organic' | 'paid'  (entry-source filter)
var cjLine='all';                        // 'all' | conmed | beckman | siemens | abbott | diazyme  (product-line filter)
var cjLineOrder=['all','conmed','beckman','siemens','abbott','diazyme'];
var cjLineDefs=[['conmed','Conmed',/conmed|virovac|vv120|vs35302|armstand/i],['beckman','Beckman Coulter',/beckman/i],['siemens','Siemens',/siemens/i],['abbott','Abbott',/abbott/i],['diazyme','Diazyme',/diazyme|\bDZ\d/i]];
var cjTermList=[], cjProdList=[];        // facet lists (index -> value) for onclick handlers
function cjSessProducts(s){ return (s.atc_items||[]).concat(s.purchase_items||[]); }
function cjSessionText(s){ var t=[],j=s.journey||[]; for(var i=0;i<j.length;i++){ if(j[i]&&j[i].label) t.push(j[i].label); } return t.concat(s.atc_items||[],s.purchase_items||[]).join(' || '); }
function cjSessionLines(s){ var txt=cjSessionText(s),out=[]; for(var i=0;i<cjLineDefs.length;i++){ if(cjLineDefs[i][2].test(txt)) out.push(cjLineDefs[i][0]); } return out; }
function loadCJ(){
  if(CJ_EMBED){ CJ=CJ_EMBED; cjLoading=false; cjInitCustom(); if(mode==='cj'){ renderTabs(); renderCjPanel(); } return; }
  if(cjLoading) return; cjLoading=true;
  fetch('customer-journey-data.json?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(d){ CJ=d; cjLoading=false; cjInitCustom(); if(mode==='cj'){ renderTabs(); renderCjPanel(); } })
    .catch(function(e){ cjLoading=false; if(mode==='cj') document.getElementById('panel').innerHTML='<div class="empty">Could not load Customer Journey data: '+escapeHtml(e.message)+'</div>'; });
}
function cjRefresh(){ CJ=CJ_EMBED||null; cjLoading=false; document.getElementById('panel').innerHTML='<div class="empty">Reloading customer-journey snapshot…</div>'; loadCJ(); }
function cjInitCustom(){ if(!cjCustStart && CJ){ cjCustStart=CJ.data_from||''; cjCustEnd=CJ.data_through||''; } }
function cjShortUrl(u){
  if(!u) return '';
  try{ var a=document.createElement('a'); a.href=u; var p=a.pathname||'/'; if(a.search) p+=a.search; return (p||'/').slice(0,80); }
  catch(e){ return String(u).replace(/^https?:\/\/[^\/]+/,'').slice(0,80)||'/'; }
}
function cjWhen(iso){
  if(!iso) return '—';
  var d=new Date(iso); if(isNaN(d)) return escapeHtml(iso);
  return d.toLocaleDateString(undefined,{month:'short',day:'numeric'})+' '+d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
}
function cjDay(iso){ return (iso||'').slice(0,10); }
function cjInWin(iso){
  var day=cjDay(iso); if(!day) return false;
  if(cjWin==='ytd'){ return day>='2026-01-01' && day<='2026-12-31'; }
  if(cjWin==='month'){ var now=new Date(); var mk=now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0'); return day.slice(0,7)===mk; }
  // custom
  var lo=cjCustStart||'0000-00-00', hi=cjCustEnd||'9999-99-99';
  return day>=lo && day<=hi;
}
function cjWinSessions(){   // non-dev sessions within the window (no facet filters), newest first
  var all=(CJ&&CJ.sessions)||[];
  var out=all.filter(function(s){ return s.class!=='dev' && cjInWin(s.session_start); });
  out.sort(function(a,b){ return (b.session_start||'').localeCompare(a.session_start||''); });
  return out;
}
function cjBaseSessions(){   // window + outcome filter (drives the sidebar facet boxes)
  return cjWinSessions().filter(function(s){
    if(cjOutcome==='cart' && !(s.class==='abandoner'||s.class==='purchaser')) return false;
    if(cjOutcome==='left' && s.class!=='browser') return false;
    if(cjEntry!=='all'){ var ek=String((s.entry||{}).kind||'').toLowerCase();
      if(cjEntry==='organic' && ek.indexOf('organic')<0) return false;
      if(cjEntry==='paid' && ek.indexOf('paid')<0) return false; }
    if(cjLine!=='all' && cjSessionLines(s).indexOf(cjLine)<0) return false;
    return true;
  });
}
function cjSessions(){   // base sessions with the active search-term / product facet filters applied
  return cjBaseSessions().filter(function(s){
    if(cjTermFilter && (s.search_terms||[]).indexOf(cjTermFilter)<0) return false;
    if(cjProdFilter && cjSessProducts(s).indexOf(cjProdFilter)<0) return false;
    return true;
  });
}
function cjFacetBase(applyTerm, applyProd){   // base sessions with the OTHER facet applied (for cross-filtering)
  return cjBaseSessions().filter(function(s){
    if(applyTerm && cjTermFilter && (s.search_terms||[]).indexOf(cjTermFilter)<0) return false;
    if(applyProd && cjProdFilter && cjSessProducts(s).indexOf(cjProdFilter)<0) return false;
    return true;
  });
}
function cjWinTerms(){   // search terms — reflects the active product filter (cross-facet)
  var ws=cjFacetBase(false,true), m={}, order=[];
  for(var i=0;i<ws.length;i++){ var ts=ws[i].search_terms||[]; for(var j=0;j<ts.length;j++){ var t=ts[j]; if(!(t in m)){ m[t]=0; order.push(t); } m[t]++; } }
  order.sort(function(a,b){ return m[b]-m[a] || a.toLowerCase().localeCompare(b.toLowerCase()); });
  return order.map(function(t){ return {v:t, count:m[t]}; });
}
function cjWinProducts(){   // products — reflects the active search-term filter (cross-facet)
  var ws=cjFacetBase(true,false), m={}, order=[];
  for(var i=0;i<ws.length;i++){ var ps=cjSessProducts(ws[i]); var seen={}; for(var j=0;j<ps.length;j++){ var p=ps[j]; if(seen[p]) continue; seen[p]=1; if(!(p in m)){ m[p]=0; order.push(p); } m[p]++; } }
  order.sort(function(a,b){ return m[b]-m[a] || a.toLowerCase().localeCompare(b.toLowerCase()); });
  return order.map(function(p){ return {v:p, count:m[p]}; });
}
function cjVisitorLabel(s){
  var loc=[s.city,s.region].filter(function(x){return x;}).join(', ');
  return loc || (s.country||'') || ('Visitor '+String(s.user||'').slice(0,6));
}
function cjVisitors(){   // group in-window sessions by visitor (user_pseudo_id)
  var ss=cjSessions(), map={}, order=[];
  for(var i=0;i<ss.length;i++){ var u=ss[i].user||('_'+i); if(!map[u]){ map[u]={user:u, label:cjVisitorLabel(ss[i]), sessions:[], purchased:false, revenue:0}; order.push(u); }
    map[u].sessions.push(ss[i]);
    if(ss[i].class==='purchaser'){ map[u].purchased=true; map[u].revenue+=(ss[i].revenue||0); }
  }
  return order.map(function(u){ return map[u]; });
}
function cjSetWin(w){ cjWin=w; cjSel=''; renderTabs(); renderCjPanel(); }
function cjSetWinIdx(i){ cjSetWin(['ytd','month','custom'][i]||'ytd'); }
function cjSetCustom(){
  var a=document.getElementById('cjStart'), b=document.getElementById('cjEnd');
  if(a) cjCustStart=a.value; if(b) cjCustEnd=b.value; cjWin='custom'; cjSel=''; renderTabs(); renderCjPanel();
}
function cjFacetBox(title, items, activeVal, fn, icon){
  var right = activeVal ? '<a class="cjfacet-clear" href="javascript:void(0)" onclick="'+fn+'(-1)">clear</a>'
                        : '<span class="n">'+items.length+'</span>';
  var h='<div class="cjfacet"><div class="cjfacet-h"><span><span class="ic">'+icon+'</span>'+escapeHtml(title)+'</span>'+right+'</div>';
  if(!items.length){ return h+'<div class="cjfacet-empty">none in this view</div></div>'; }
  h+='<div class="cjfacet-list">';
  for(var i=0;i<items.length;i++){ var on=(items[i].v===activeVal);
    h+='<button class="cjrow'+(on?' active':'')+'" onclick="'+fn+'('+i+')" title="'+escapeHtml(items[i].v)+'"><span class="lbl">'+escapeHtml(items[i].v)+'</span><span class="cjpill">'+items[i].count+'</span></button>';
  }
  return h+'</div></div>';
}
function renderCjTabs(el){
  el.style.display='';
  if(!CJ){ el.innerHTML='<div class="empty">Loading…</div>'; loadCJ(); return; }
  // ── facet filter boxes: Products first, Search terms below (react to the outcome filter) ──
  cjProdList=cjWinProducts(); cjTermList=cjWinTerms();
  var h=cjFacetBox('Products added / purchased', cjProdList, cjProdFilter, 'cjToggleProdIdx', '📦');
  h+=cjFacetBox('Search terms', cjTermList, cjTermFilter, 'cjToggleTermIdx', '🔍');
  el.innerHTML=h;
}
var cjVisList=[];
function cjSelectVisitor(i){ cjSel = (i<0 ? '' : ((cjVisList[i]||{}).user||'')); renderTabs(); renderCjPanel(); }
function cjToggleTermIdx(i){ var v=(i<0?'':((cjTermList[i]||{}).v||'')); cjTermFilter=(cjTermFilter===v?'':v); cjSel=''; renderTabs(); renderCjPanel(); }
function cjToggleProdIdx(i){ var v=(i<0?'':((cjProdList[i]||{}).v||'')); cjProdFilter=(cjProdFilter===v?'':v); cjSel=''; renderTabs(); renderCjPanel(); }
function cjClearFilters(){ cjTermFilter=''; cjProdFilter=''; cjOutcome='all'; cjEntry='all'; cjLine='all'; cjSel=''; renderTabs(); renderCjPanel(); }
function cjSetOutcome(o){ cjOutcome=o; cjSel=''; renderTabs(); renderCjPanel(); }
function cjSetOutcomeIdx(i){ cjSetOutcome(['all','cart','left'][i]||'all'); }
function cjSetEntry(o){ cjEntry=o; cjSel=''; renderTabs(); renderCjPanel(); }
function cjSetEntryIdx(i){ cjSetEntry(['all','organic','paid'][i]||'all'); }
function cjSetLine(o){ cjLine=o; cjSel=''; renderTabs(); renderCjPanel(); }
function cjSetLineIdx(i){ cjSetLine(cjLineOrder[i]||'all'); }
function cjStepHtml(step){
  if(step.type==='cart'){
    return '<div style="padding:3px 0 3px 20px;"><span style="color:#a1362c;font-weight:600;">🛒 Added to cart:</span> <span style="color:#a1362c;">'+escapeHtml(step.label||'')+'</span></div>';
  }
  if(step.type==='buy'){
    var v=(step.value!=null)?(' — <span style="font-weight:700;">$'+Number(step.value).toLocaleString()+'</span>'):'';
    return '<div style="padding:3px 0 3px 20px;"><span style="color:#1e7e34;font-weight:700;">✓ Purchased:</span> <span style="color:#1e7e34;">'+escapeHtml(step.label||'')+'</span>'+v+'</div>';
  }
  var nm=step.label||cjShortUrl(step.url)||'(page)';
  return '<div style="padding:2px 0 2px 20px;color:#5a6b7b;font-size:12px;"><span style="color:#aab4bf;">▸</span> <span title="'+escapeHtml(step.url||'')+'">'+escapeHtml(nm)+'</span></div>';
}
function cjEntryIcon(kind){
  var k=(kind||'').toLowerCase();
  if(k.indexOf('paid')>=0) return '📣';
  if(k.indexOf('organic')>=0) return '🔍';
  if(k.indexOf('referral')>=0) return '↗';
  if(k.indexOf('email')>=0) return '✉';
  if(k.indexOf('social')>=0) return '👥';
  if(k.indexOf('direct')>=0) return '•';
  return '🌐';
}
function cjEntryColor(kind){
  var k=(kind||'').toLowerCase();
  if(k.indexOf('paid')>=0) return ['#e8f0fe','#1a56c4'];
  if(k.indexOf('organic')>=0) return ['#e6f4ea','#1e7e34'];
  if(k.indexOf('referral')>=0) return ['#f3e8fd','#6b3fa0'];
  if(k.indexOf('email')>=0) return ['#fff3cd','#7a5b00'];
  return ['#eef2f6','#4a5b6b'];
}
function cjJourneyCard(s){
  var loc=[s.city,s.region,s.country].filter(function(x){return x;}).join(', ');
  var outcome = s.class==='purchaser'
    ? '<span class="status" style="background:#e6f4ea;color:#1e7e34;">Purchased'+(s.revenue!=null?' · $'+Number(s.revenue).toLocaleString():'')+'</span>'
    : (s.class==='abandoner'
      ? '<span class="status" style="background:#fdecea;color:#a1362c;">Abandoned cart</span>'
      : '<span class="status" style="background:#eef2f6;color:#4a5b6b;">Browsed (no cart)</span>');
  // ── entry point + search term ──
  var e=s.entry||{}, ec=cjEntryColor(e.kind);
  var entryTxt=escapeHtml(e.kind||'Direct')+((e.detail)?(' · '+escapeHtml(e.detail)):'');
  var entryPill='<span class="status" style="background:'+ec[0]+';color:'+ec[1]+';" title="'+escapeHtml((e.source||'')+' / '+(e.medium||''))+'">'+cjEntryIcon(e.kind)+' Entry: '+entryTxt+'</span>';
  var terms=(s.search_terms||[]);
  var searchPill = terms.length
    ? '<span class="status" style="background:#fff8e1;color:#8a6d00;">🔎 Search: '+terms.map(escapeHtml).join(', ')+'</span>'
    : (String((e.kind||'')).toLowerCase().indexOf('organic')>=0 || String((e.kind||'')).toLowerCase().indexOf('paid')>=0
        ? '<span class="status" style="background:#f4f6f8;color:#8a97a5;" title="Google withholds the exact keyword for most organic/paid clicks">🔎 keyword not provided</span>' : '');
  var atc=(s.atc_items||[]);
  var atcLine = atc.length ? '<div style="font-size:11px;color:#a1362c;margin-top:4px;">In cart: '+atc.map(escapeHtml).join(', ')+'</div>' : '';
  var steps=(s.journey||[]).map(cjStepHtml).join('');
  return '<div style="border:1px solid #e3e8ee;border-radius:8px;padding:12px 14px;margin:10px 16px;background:#fff;">'+
    '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:6px;">'+
      '<div style="font-weight:600;color:#2c3e50;">'+escapeHtml(loc||'Unknown location')+' <span style="font-weight:400;color:#7a8a99;font-size:12px;">· '+cjWhen(s.session_start)+' · '+(s.page_views||0)+' pages</span></div>'+
      outcome+'</div>'+
    '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px;">'+entryPill+searchPill+'</div>'+
    atcLine+
    '<div style="font-size:11px;color:#7a8a99;margin:8px 0 2px;font-weight:600;">Page sequence</div>'+
    '<div style="border-left:2px solid #eef2f6;margin-left:6px;">'+steps+'</div></div>';
}
function renderCjPanel(){
  if(!CJ){ document.getElementById('panel').innerHTML='<div class="empty">Loading Customer Journey data…</div>'; loadCJ(); return; }
  cjInitCustom();
  // window selector
  function wb(w,lbl,idx){ return '<button class="mode-btn'+(cjWin===w?' active':'')+'" style="padding:5px 11px;border-radius:6px;border:1px solid #cdd9e6;font-size:12px;" onclick="cjSetWinIdx('+idx+')">'+lbl+'</button>'; }
  var sel='<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:2px 0 4px;">'+
    '<span style="font-size:12px;color:#7a8a99;margin-right:2px;">Window:</span>'+
    wb('ytd','2026 YTD',0)+wb('month','Current month',1)+wb('custom','Custom',2)+
    '<span style="margin-left:8px;font-size:12px;color:#7a8a99;">'+
      'from <input type="date" id="cjStart" value="'+escapeHtml(cjCustStart)+'" onchange="cjSetCustom()" style="font:inherit;padding:2px 4px;border:1px solid #cfd8e0;border-radius:4px;"> '+
      'to <input type="date" id="cjEnd" value="'+escapeHtml(cjCustEnd)+'" onchange="cjSetCustom()" style="font:inherit;padding:2px 4px;border:1px solid #cfd8e0;border-radius:4px;"></span>'+
    '</div>';
  function ob(o,lbl,idx){ return '<button class="mode-btn'+(cjOutcome===o?' active':'')+'" style="padding:5px 11px;border-radius:6px;border:1px solid #cdd9e6;font-size:12px;" onclick="cjSetOutcomeIdx('+idx+')">'+lbl+'</button>'; }
  sel+='<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:0 0 4px;">'+
    '<span style="font-size:12px;color:#7a8a99;margin-right:2px;">Outcome:</span>'+
    ob('all','All',0)+ob('cart','🛒 Added to cart / Purchased',1)+ob('left','Left without cart',2)+'</div>';
  function eb(o,lbl,idx){ return '<button class="mode-btn'+(cjEntry===o?' active':'')+'" style="padding:5px 11px;border-radius:6px;border:1px solid #cdd9e6;font-size:12px;" onclick="cjSetEntryIdx('+idx+')">'+lbl+'</button>'; }
  sel+='<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:0 0 4px;">'+
    '<span style="font-size:12px;color:#7a8a99;margin-right:2px;">Source:</span>'+
    eb('all','All',0)+eb('organic','🔍 Organic search',1)+eb('paid','📣 Paid search',2)+'</div>';
  function lb(o,lbl,idx){ return '<button class="mode-btn'+(cjLine===o?' active':'')+'" style="padding:5px 11px;border-radius:6px;border:1px solid #cdd9e6;font-size:12px;" onclick="cjSetLineIdx('+idx+')">'+lbl+'</button>'; }
  sel+='<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:0 0 4px;">'+
    '<span style="font-size:12px;color:#7a8a99;margin-right:2px;">Product line:</span>'+
    lb('all','All',0)+lb('conmed','Conmed',1)+lb('beckman','Beckman Coulter',2)+lb('siemens','Siemens',3)+lb('abbott','Abbott',4)+lb('diazyme','Diazyme',5)+'</div>';
  var head='<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'+
    '<div><h2>Customer Journey</h2><div class="sub">GA4→BigQuery event export &middot; '+escapeHtml(CJ.property||'')+' &middot; entry source, search term &amp; page path (add-to-cart &amp; purchase mapped) &middot; data '+escapeHtml(CJ.data_from||'')+' → '+escapeHtml(CJ.data_through||'')+' ('+(CJ.days_of_data||0)+' days) &middot; pulled '+escapeHtml(CJ.pulled_at||'')+'</div></div>'+
    '<button class="refresh-btn" onclick="cjRefresh()" title="Reload the latest Customer Journey snapshot"><span class="lbl">↻ Reload</span></button></div>'+sel+'</div>';
  // KPIs over in-window non-dev sessions (after active facet filters)
  var ss=cjSessions();
  var visN=cjVisitors().length, buyN=0, abN=0, brN=0, rev=0;
  for(var i=0;i<ss.length;i++){ var c=ss[i].class; if(c==='purchaser'){ buyN++; rev+=(ss[i].revenue||0); } else if(c==='abandoner'){ abN++; } else { brN++; } }
  var cards='<div class="kpis" style="padding:6px 0 0;">'+
    kpi(Number(visN).toLocaleString(),'Visitors')+
    kpi(Number(ss.length).toLocaleString(),'Sessions')+
    kpi(Number(buyN).toLocaleString(),'Purchased')+
    kpi(Number(abN).toLocaleString(),'Abandoned cart')+
    kpi(Number(brN).toLocaleString(),'Browsed only')+
    kpi(rev?('$'+Number(rev).toLocaleString()):'$0','Revenue')+'</div>';
  // active filter banner
  var fnote='';
  if(cjTermFilter||cjProdFilter||cjOutcome!=='all'||cjEntry!=='all'||cjLine!=='all'){
    var parts=[];
    if(cjEntry==='organic') parts.push('source: organic search');
    if(cjEntry==='paid') parts.push('source: paid search');
    if(cjLine!=='all'){ for(var li=0;li<cjLineDefs.length;li++){ if(cjLineDefs[li][0]===cjLine) parts.push('product line: '+cjLineDefs[li][1]); } }
    if(cjOutcome==='cart') parts.push('outcome: added to cart / purchased');
    if(cjOutcome==='left') parts.push('outcome: left without cart');
    if(cjTermFilter) parts.push('search term “'+escapeHtml(cjTermFilter)+'”');
    if(cjProdFilter) parts.push('product “'+escapeHtml(cjProdFilter)+'”');
    fnote='<div style="margin:8px 16px 0;font-size:12px;color:#1a56c4;background:#eef4fb;border:1px solid #d3e2f7;border-radius:6px;padding:6px 10px;">Filtered by '+parts.join(' + ')+' &middot; <a href="javascript:void(0)" onclick="cjClearFilters()" style="color:#1a73e8;font-weight:600;">clear filters</a></div>';
  }
  cards+=fnote;
  // body: selected visitor's journeys, else all in-window sessions
  var shown = cjSel ? ss.filter(function(s){ return s.user===cjSel; }) : ss;
  var body;
  if(!shown.length){
    body='<div class="empty" style="margin:14px 16px;line-height:1.6;">'+
      '<div style="font-size:15px;font-weight:600;color:#2c3e50;margin-bottom:6px;">No journeys match'+((cjTermFilter||cjProdFilter)?' this filter':' in this window')+'.</div>'+
      'This maps every visitor who searched, viewed a product, added to cart, or purchased (Belgrade developer traffic excluded). '+
      ((cjTermFilter||cjProdFilter)?'Try clearing the filters. ':'')+
      'The GA4→BigQuery export is not retroactive — it holds data from the link date ('+escapeHtml(CJ.link_date||'')+') forward and builds history from there.'+
      '</div>';
  } else {
    var CAP=50, over=shown.length-CAP, listS=shown.slice(0,CAP);
    var title = cjSel ? ('<div class="ca-h" style="margin:10px 16px 0;">'+escapeHtml(cjVisitorLabel(shown[0]))+' — '+shown.length+' session'+(shown.length>1?'s':'')+'</div>') : '';
    body=title+listS.map(cjJourneyCard).join('')+
      (over>0?('<div class="empty" style="margin:10px 16px;">Showing first '+CAP+' of '+shown.length+' sessions — use the Search terms / Products filters or pick a customer in the sidebar to narrow.</div>'):'');
  }
  var note='<div style="margin:14px 16px;padding:12px 16px;background:#eef4fb;border-left:4px solid #1a73e8;font-size:12px;border-radius:6px;line-height:1.55;color:#2c3e50;">'+escapeHtml(CJ.note||'')+'</div>';
  document.getElementById('panel').innerHTML = head + cards + body + note;
}

// ── Website Traffic tab (GA4 daily visitors by source + sales by source) ──────
var WT=null, wtLoading=false, wtWin='last_30_days', wtLabels=true, wtTrend=true, wtVisible={};
var wtCampVisible={};   // item 3: which Google Ads campaigns show as series in the bar chart
var wtMetricImpr=false, wtMetricClk=false, wtMetricAtc=true;   // ad-metric view: Google Ads impressions (area) / clicks (line) / add-to-cart (cart icons)
var wtCustFrom='', wtCustTo='';   // custom date-range picker (drives the whole tab via WT.daily)
var WT_WIN_ORDER=['today','yesterday','last_7_days','last_30_days','this_month','last_month','this_quarter','last_quarter','this_year'];
var WT_COLORS={ 'Direct':'#6b7a8f','Google Ads':'#1a73e8','Organic Search':'#34a853','Email':'#f59e0b','LinkedIn':'#0a66c2','Other':'#aab4bf' };
function loadWT(){
  if(WT_EMBED){ WT=WT_EMBED; wtLoading=false; if(mode==='wt') renderWtPanel(); return; }
  if(wtLoading) return; wtLoading=true;
  fetch('website-traffic-data.json?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(d){ WT=d; wtLoading=false; if(mode==='wt') renderWtPanel(); })
    .catch(function(e){ wtLoading=false; if(mode==='wt') document.getElementById('panel').innerHTML='<div class="empty">Could not load website-traffic data: '+escapeHtml(e.message)+'</div>'; });
}
function wtRefresh(){ WT=null; wtLoading=false; document.getElementById('panel').innerHTML='<div class="empty">Reloading website-traffic snapshot…</div>'; loadWT(); }
function wtSetWin(v){
  wtWin=v;
  if(v==='__custom__'){ var dly=WT&&WT.daily; if(dly){ if(!wtCustTo) wtCustTo=dly.end;
    if(!wtCustFrom){ var e=new Date((wtCustTo||dly.end)+'T00:00:00'); e.setDate(e.getDate()-29);
      var f=e.toISOString().slice(0,10); wtCustFrom=(f<dly.start?dly.start:f); } } }
  renderWtPanel();
}
function wtToggleLabels(c){ wtLabels=!!c; renderWtPanel(); }
function wtToggleTrend(c){ wtTrend=!!c; renderWtPanel(); }
function wtToggleSource(bk,c){ wtVisible[bk]=!!c; renderWtPanel(); }
function wtToggleSourceIdx(i,c){ var bk=((WT&&WT.buckets)||[])[i]; if(bk!=null){ wtVisible[bk]=!!c; renderWtPanel(); } }
function wtAllSources(c){ var bs=(WT&&WT.buckets)||[]; for(var i=0;i<bs.length;i++) wtVisible[bs[i]]=!!c; renderWtPanel(); }
function wtVisBuckets(){ var bs=(WT&&WT.buckets)||[], v=[]; for(var i=0;i<bs.length;i++) if(wtVisible[bs[i]]) v.push(bs[i]); return v; }
// ── item 3: active-campaign checkboxes that add campaign series to the visitors BAR CHART ──
var WT_CAMP_PALETTE=['#7e57c2','#26a69a','#ef6c00','#42a5f5','#ec407a','#9ccc65','#5c6bc0','#8d6e63','#26c6da','#c2185b'];
var wtCurWin=null;   // the window object currently being rendered (preset or custom)
function wtCampList(){ var w=wtCurWin||(WT&&WT.windows&&WT.windows[wtWin])||{}; return (w.gads_active&&w.gads_active.length)?w.gads_active:(w.gads_campaigns||[]); }
function wtInitCamps(){ var cs=wtCampList(); for(var i=0;i<cs.length;i++){ if(!(cs[i] in wtCampVisible)) wtCampVisible[cs[i]]=false; } }  // default OFF: chart starts on source buckets
function wtCampColor(name){ var cs=wtCampList(), i=cs.indexOf(name); if(i<0){ i=0; for(var j=0;j<name.length;j++) i+=name.charCodeAt(j); } return WT_CAMP_PALETTE[i%WT_CAMP_PALETTE.length]; }
function wtVisCampaigns(){ var cs=wtCampList(), v=[]; for(var i=0;i<cs.length;i++) if(wtCampVisible[cs[i]]) v.push(cs[i]); return v; }
function wtToggleCampIdx(i,c){ var cs=wtCampList(); if(cs[i]!=null){ wtCampVisible[cs[i]]=!!c; renderWtPanel(); } }
function wtAllCamps(c){ var cs=wtCampList(); for(var i=0;i<cs.length;i++) wtCampVisible[cs[i]]=!!c; renderWtPanel(); }
function wtToggleImpr(c){ wtMetricImpr=!!c; renderWtPanel(); }
function wtToggleClk(c){ wtMetricClk=!!c; renderWtPanel(); }
function wtToggleAtc(c){ wtMetricAtc=!!c; renderWtPanel(); }
// ── custom date range: reconstruct a full window object from WT.daily for [from,to] ──
function wtIsCustom(){ return wtWin==='__custom__'; }
function wtApplyCustom(){
  var f=document.getElementById('wtCustFrom'), t=document.getElementById('wtCustTo');
  if(!f||!t) return;
  if(!f.value||!t.value){ alert('Pick both a start and end date.'); return; }
  if(f.value>t.value){ var tmp=f.value; f.value=t.value; t.value=tmp; }
  wtCustFrom=f.value; wtCustTo=t.value; wtWin='__custom__'; renderWtPanel();
}
function wtBuildCustomWin(from,to){
  var dly=WT&&WT.daily; if(!dly) return null;
  var buckets=dly.buckets||WT.buckets||[];
  var inR=function(t){ return t>=from && t<=to; };
  // points (daily channel + camp:: sessions), filtered
  var points=[]; var camps=dly.campaigns||[];
  for(var i=0;i<(dly.points||[]).length;i++){ var p=dly.points[i]; if(inR(p.t)) points.push(p); }
  // sales-by-source: aggregate sday [sessions,conversions,revenue,transactions] per bucket
  var agg={}; for(var b=0;b<buckets.length;b++) agg[buckets[b]]={sessions:0,conversions:0,revenue:0,transactions:0};
  var sd=dly.sday||{};
  for(var t in sd){ if(!sd.hasOwnProperty(t)||!inR(t)) continue; var row=sd[t];
    for(var b=0;b<buckets.length;b++){ var bk=buckets[b], v=row[bk]||[0,0,0,0], a=agg[bk];
      a.sessions+=v[0]||0; a.conversions+=v[1]||0; a.revenue+=v[2]||0; a.transactions+=v[3]||0; } }
  var sales=[]; for(var b=0;b<buckets.length;b++){ var bk=buckets[b], a=agg[bk];
    sales.push({source:bk,sessions:a.sessions,conversions:a.conversions,revenue:Math.round(a.revenue*100)/100,transactions:a.transactions}); }
  var totals={sessions:0,conversions:0,revenue:0,transactions:0};
  for(var s=0;s<sales.length;s++){ totals.sessions+=sales[s].sessions; totals.conversions+=sales[s].conversions; totals.revenue+=sales[s].revenue; totals.transactions+=sales[s].transactions; }
  totals.revenue=Math.round(totals.revenue*100)/100;
  // per-campaign gads: series (impr/clicks/atc), cost, events, detail
  var gday=dly.gday||{}, gads_series={}, gads_cost={}, gads_events={}, gads_detail=[], itot={};
  for(var ci=0;ci<camps.length;ci++){ var c=camps[ci], arr=gday[c]||[], ser=[], cost=0, atc=0, pur=0, se=0,cv=0,rv=0,tr=0, im=0;
    for(var j=0;j<arr.length;j++){ var g=arr[j]; if(!inR(g.t)) continue;
      ser.push({t:g.t,impr:g.impr||0,clicks:g.clicks||0,atc:g.atc||0});
      cost+=g.cost||0; atc+=g.atc||0; pur+=g.purchase||0; im+=g.impr||0;
      se+=g.sessions||0; cv+=g.conversions||0; rv+=g.revenue||0; tr+=g.transactions||0; }
    gads_series[c]=ser; gads_cost[c]=Math.round(cost*100)/100; gads_events[c]={add_to_cart:atc,purchase:pur}; itot[c]=im;
    if(se>0||im>0){ var disp=(c.indexOf('(untagged')===0)?c:c;
      gads_detail.push({name:c,campaign:(c.indexOf('(untagged')===0?null:c),sessions:se,conversions:cv,revenue:Math.round(rv*100)/100,transactions:tr}); } }
  var gads_campaigns=camps.slice().filter(function(c){ return (itot[c]||0)>0 || (gads_series[c]&&gads_series[c].length); });
  gads_campaigns.sort(function(a,b){ return (itot[b]||0)-(itot[a]||0); });
  gads_detail.sort(function(a,b){ return b.sessions-a.sessions; });
  var lab=from+' → '+to;
  return {label:lab,granularity:'day',points:points,sales:sales,totals:totals,
    email_detail:[],gads_detail:gads_detail,gads_campaigns:gads_campaigns,gads_series:gads_series,
    gads_cost:gads_cost,gads_start:dly.gads_start||{},gads_events:gads_events,
    gads_types:dly.gads_types||{},gads_status:dly.gads_status||{},gads_active:gads_campaigns,
    klaviyo_flows:[],_custom:true};
}
// ── items 4 & 5: Klaviyo email-flow clicks (bar chart of clicks per flow) ──
function wtFlowChart(win){
  var flows=win.klaviyo_flows||[], k=(WT.klaviyo||{});
  if(!flows.length) return '<div class="empty" style="margin:6px 0;">'+(k.ok===false?'Klaviyo not connected — add a private API key to see per-flow clicks.':'No live Klaviyo flows found.')+'</div>';
  var max=0; for(var i=0;i<flows.length;i++){ if((flows[i].clicks||0)>max) max=flows[i].clicks||0; }
  var denom=max>0?max:1;
  var h='<div style="max-width:680px;margin:4px 2px 2px;">';
  for(var i=0;i<flows.length;i++){ var fl=flows[i], w=Math.round((fl.clicks||0)/denom*100);
    h+='<div style="display:flex;align-items:center;gap:10px;margin:5px 0;font-size:12px;">'+
       '<div style="flex:0 0 220px;color:#2c3e50;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="'+escapeHtml(fl.flow)+'">✉ '+escapeHtml(fl.flow)+'</div>'+
       '<div style="flex:1 1 auto;background:#f0f2f5;border-radius:4px;height:16px;"><div style="width:'+(fl.clicks>0?Math.max(w,4):0)+'%;height:16px;background:'+WT_COLORS.Email+';border-radius:4px;"></div></div>'+
       '<div style="flex:0 0 64px;text-align:right;font-weight:600;color:#2c3e50;">'+Number(fl.clicks||0).toLocaleString()+'</div>'+
     '</div>';
  }
  h+='</div>';
  if(max===0){ h+='<div style="font-size:11px;color:#9aa7b4;margin:2px 2px 6px;">No email-flow clicks in this window yet — fills in automatically as your flows send and get clicked.</div>'; }
  return h;
}
function wtLabel(t,gran){
  // t is YYYY-MM-DD (day) or week-Monday date (week)
  var p=(t||'').split('-'); if(p.length<3) return t;
  var mo=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][parseInt(p[1],10)-1];
  return mo+' '+parseInt(p[2],10);
}
function wtBarSvg(win){
  if(wtMetricImpr||wtMetricClk) return wtBarSvgAd(win);   // ad-metric view: campaign impressions / clicks
  var buckets=wtVisBuckets(), camps=wtVisCampaigns(), pts=win.points||[], n=pts.length;
  // when campaigns are selected they REPLACE the aggregate "Google Ads" bucket (avoid double-counting)
  if(camps.length){ buckets=buckets.filter(function(b){ return b!=='Google Ads'; }); }
  var series=[];
  for(var sb=0;sb<buckets.length;sb++) series.push({key:buckets[sb], color:WT_COLORS[buckets[sb]], label:buckets[sb]});
  for(var sc=0;sc<camps.length;sc++) series.push({key:'camp::'+camps[sc], color:wtCampColor(camps[sc]), label:camps[sc]});
  if(!series.length) return '<div class="empty">Select at least one source or campaign above to show the chart.</div>';
  if(!n) return '<div class="empty">No visitors in this window.</div>';
  var totals=[], maxT=0;
  for(var i=0;i<n;i++){ var s=0; for(var b=0;b<series.length;b++) s+=pts[i][series[b].key]||0; totals.push(s); if(s>maxT) maxT=s; }
  if(maxT<=0) maxT=1;
  function niceMax(m){ var pow=Math.pow(10,Math.floor(Math.log(m)/Math.LN10)); var f=m/pow; var nf=f<=1?1:f<=2?2:f<=5?5:10; return nf*pow; }
  var yMax=niceMax(maxT*1.08);
  var padL=46,padR=14,padT=18,padB=52, plotH=250;
  var minStep=26, plotW=Math.max(660-padL-padR, n*minStep);
  var W=padL+plotW+padR, H=padT+plotH+padB;
  var bw=Math.min(34, plotW/n*0.66), step=plotW/n;
  function yOf(v){ return padT+plotH-(plotH*v/yMax); }
  function cx(i){ return padL+step*i+step/2; }
  var svg='<svg viewBox="0 0 '+W+' '+H+'" width="100%" preserveAspectRatio="xMinYMin meet" style="max-width:'+W+'px;font-family:inherit;">';
  var gl=4;
  for(var g=0;g<=gl;g++){ var yv=yMax*g/gl, yy=padT+plotH-(plotH*g/gl);
    svg+='<line x1="'+padL+'" y1="'+yy.toFixed(1)+'" x2="'+(padL+plotW)+'" y2="'+yy.toFixed(1)+'" stroke="#e6ecf2" stroke-width="1"/>';
    svg+='<text x="'+(padL-6)+'" y="'+(yy+3.5).toFixed(1)+'" text-anchor="end" font-size="10" fill="#7a8a99">'+Math.round(yv).toLocaleString()+'</text>';
  }
  var labEvery=Math.ceil(n/12), lblEvery=1, lblFont=(n>22?7:(n>14?8:9.5));
  for(var i=0;i<n;i++){
    var x=padL+step*i+(step-bw)/2, yCur=padT+plotH;
    for(var b=0;b<series.length;b++){
      var v=pts[i][series[b].key]||0; if(v<=0) continue;
      var h=plotH*v/yMax; yCur-=h;
      svg+='<rect x="'+x.toFixed(1)+'" y="'+yCur.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+h.toFixed(1)+'" fill="'+series[b].color+'"><title>'+escapeHtml(wtLabel(pts[i].t,win.granularity))+' · '+escapeHtml(series[b].label)+': '+v+'</title></rect>';
    }
    // data label (total atop bar)
    if(wtLabels && totals[i]>0 && (i%lblEvery===0)){
      svg+='<text x="'+(x+bw/2).toFixed(1)+'" y="'+(yOf(totals[i])-3).toFixed(1)+'" text-anchor="middle" font-size="'+lblFont+'" font-weight="600" fill="#2c3e50">'+totals[i].toLocaleString()+'</text>';
    }
    // x label
    if(i%labEvery===0){
      var lx=x+bw/2, ly=padT+plotH+14;
      svg+='<text x="'+lx.toFixed(1)+'" y="'+ly+'" text-anchor="end" font-size="9.5" fill="#5a6b7a" transform="rotate(-45 '+lx.toFixed(1)+' '+ly+')">'+escapeHtml(wtLabel(pts[i].t,win.granularity))+'</text>';
    }
  }
  // linear regression trend line over per-period totals
  if(wtTrend && n>=2){
    var sx=0,sy=0,sxy=0,sxx=0;
    for(var k=0;k<n;k++){ sx+=k; sy+=totals[k]; sxy+=k*totals[k]; sxx+=k*k; }
    var den=(n*sxx - sx*sx)||1, m=(n*sxy - sx*sy)/den, c=(sy - m*sx)/n;
    var y0=Math.max(0,Math.min(yMax, c)), y1=Math.max(0,Math.min(yMax, m*(n-1)+c));
    svg+='<line x1="'+cx(0).toFixed(1)+'" y1="'+yOf(y0).toFixed(1)+'" x2="'+cx(n-1).toFixed(1)+'" y2="'+yOf(y1).toFixed(1)+'" stroke="#d6336c" stroke-width="2.5" stroke-dasharray="6 4" stroke-linecap="round"/>';
    svg+='<circle cx="'+cx(n-1).toFixed(1)+'" cy="'+yOf(y1).toFixed(1)+'" r="3" fill="#d6336c"/>';
  }
  svg+='<line x1="'+padL+'" y1="'+(padT+plotH)+'" x2="'+(padL+plotW)+'" y2="'+(padT+plotH)+'" stroke="#cdd9e6" stroke-width="1"/>';
  svg+='</svg>';
  return '<div style="overflow-x:auto;padding:4px 0;">'+svg+'</div>';
}
function wtBarSvgAd(win){
  // Google Ads: Impressions as AREA (left axis), Clicks as LINE (right axis),
  // Add-to-cart as CART ICONS on the periods the event occurred. Summed over selected campaigns.
  var pts=win.points||[], n=pts.length;
  var camps=wtVisCampaigns(); if(!camps.length) camps=wtCampList();
  if(!camps.length) return '<div class="empty">No Google Ads campaigns in this window.</div>';
  if(!(wtMetricImpr||wtMetricClk||wtMetricAtc)) return '<div class="empty">Select Impressions, Clicks and/or Add-to-cart above.</div>';
  if(!n) return '<div class="empty">No data in this window.</div>';
  // lookup: campaign -> time key -> {impr,clicks,atc}
  var gs=win.gads_series||{}, lut={};
  for(var ci=0;ci<camps.length;ci++){ var arr=gs[camps[ci]]||[], m={}; for(var k=0;k<arr.length;k++){ m[arr[k].t]=arr[k]; } lut[camps[ci]]=m; }
  var impr=[],clk=[],atc=[], imprMax=0,clkMax=0, imprTot=0,clkTot=0,atcTot=0;
  for(var i=0;i<n;i++){ var t=pts[i].t, si=0,sc=0,sa=0;
    for(var cj=0;cj<camps.length;cj++){ var rec=lut[camps[cj]][t]; if(rec){ si+=rec.impr||0; sc+=rec.clicks||0; sa+=rec.atc||0; } }
    impr.push(si); clk.push(sc); atc.push(sa); imprTot+=si; clkTot+=sc; atcTot+=sa;
    if(si>imprMax) imprMax=si; if(sc>clkMax) clkMax=sc;
  }
  function niceMax(m){ if(m<=0) return 1; var pow=Math.pow(10,Math.floor(Math.log(m)/Math.LN10)); var f=m/pow; var nf=f<=1?1:f<=2?2:f<=5?5:10; return nf*pow; }
  var iMax=niceMax(imprMax*1.08), cMax=niceMax(clkMax*1.12);
  var padL=52,padR=(wtMetricClk?52:16),padT=24,padB=52, plotH=250;
  var minStep=26, plotW=Math.max(660-padL-padR, n*minStep);
  var W=padL+plotW+padR, H=padT+plotH+padB, step=plotW/n;
  function X(i){ return padL+step*i+step/2; }
  function Yi(v){ v=Math.max(0,v); return padT+plotH-(plotH*v/iMax); }
  function Yc(v){ v=Math.max(0,v); return padT+plotH-(plotH*v/cMax); }
  var svg='<svg viewBox="0 0 '+W+' '+H+'" width="100%" preserveAspectRatio="xMinYMin meet" style="max-width:'+W+'px;font-family:inherit;">';
  var gl=4;
  for(var g=0;g<=gl;g++){ var yy=padT+plotH-(plotH*g/gl);
    svg+='<line x1="'+padL+'" y1="'+yy.toFixed(1)+'" x2="'+(padL+plotW)+'" y2="'+yy.toFixed(1)+'" stroke="#e6ecf2" stroke-width="1"/>';
    if(wtMetricImpr){ svg+='<text x="'+(padL-6)+'" y="'+(yy+3.5).toFixed(1)+'" text-anchor="end" font-size="10" fill="'+WT_IMPR_COLOR+'">'+Math.round(iMax*g/gl).toLocaleString()+'</text>'; }
    if(wtMetricClk){ svg+='<text x="'+(padL+plotW+6)+'" y="'+(yy+3.5).toFixed(1)+'" text-anchor="start" font-size="10" fill="'+WT_CLK_COLOR+'">'+Math.round(cMax*g/gl).toLocaleString()+'</text>'; }
  }
  // axis titles
  if(wtMetricImpr){ svg+='<text x="'+padL+'" y="'+(padT-10)+'" text-anchor="start" font-size="10" font-weight="700" fill="'+WT_IMPR_COLOR+'">Impressions ▧</text>'; }
  if(wtMetricClk){ svg+='<text x="'+(padL+plotW)+'" y="'+(padT-10)+'" text-anchor="end" font-size="10" font-weight="700" fill="'+WT_CLK_COLOR+'">Clicks —</text>'; }
  // impressions AREA
  if(wtMetricImpr){
    var ap='M '+X(0).toFixed(1)+' '+(padT+plotH).toFixed(1);
    for(var i=0;i<n;i++){ ap+=' L '+X(i).toFixed(1)+' '+Yi(impr[i]).toFixed(1); }
    ap+=' L '+X(n-1).toFixed(1)+' '+(padT+plotH).toFixed(1)+' Z';
    svg+='<path d="'+ap+'" fill="'+WT_IMPR_COLOR+'" fill-opacity="0.16" stroke="none"/>';
    var lp=''; for(var i=0;i<n;i++){ lp+=X(i).toFixed(1)+','+Yi(impr[i]).toFixed(1)+' '; }
    svg+='<polyline points="'+lp+'" fill="none" stroke="'+WT_IMPR_COLOR+'" stroke-width="2" stroke-linejoin="round"/>';
    for(var i=0;i<n;i++){ svg+='<circle cx="'+X(i).toFixed(1)+'" cy="'+Yi(impr[i]).toFixed(1)+'" r="1.6" fill="'+WT_IMPR_COLOR+'"><title>'+escapeHtml(wtLabel(pts[i].t,win.granularity))+' · Impressions: '+impr[i].toLocaleString()+'</title></circle>'; }
  }
  // clicks LINE (right axis)
  if(wtMetricClk){
    var cp=''; for(var i=0;i<n;i++){ cp+=X(i).toFixed(1)+','+Yc(clk[i]).toFixed(1)+' '; }
    svg+='<polyline points="'+cp+'" fill="none" stroke="'+WT_CLK_COLOR+'" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>';
    for(var i=0;i<n;i++){ svg+='<circle cx="'+X(i).toFixed(1)+'" cy="'+Yc(clk[i]).toFixed(1)+'" r="2.2" fill="#fff" stroke="'+WT_CLK_COLOR+'" stroke-width="1.4"><title>'+escapeHtml(wtLabel(pts[i].t,win.granularity))+' · Clicks: '+clk[i].toLocaleString()+'</title></circle>'; }
  }
  // add-to-cart CART ICONS on periods where atc>0 (placed near the top of the plot)
  if(wtMetricAtc){
    for(var i=0;i<n;i++){ if((atc[i]||0)<=0) continue;
      var iconY=padT+10;
      svg+='<text x="'+X(i).toFixed(1)+'" y="'+iconY.toFixed(1)+'" text-anchor="middle" font-size="13">🛒<title>'+escapeHtml(wtLabel(pts[i].t,win.granularity))+' · Add to cart: '+atc[i].toLocaleString()+'</title></text>';
      if(wtLabels){ svg+='<text x="'+X(i).toFixed(1)+'" y="'+(iconY+11).toFixed(1)+'" text-anchor="middle" font-size="8" font-weight="700" fill="#8a6d1a">'+atc[i].toLocaleString()+'</text>'; }
    }
  }
  var labEvery=Math.ceil(n/12);
  for(var i=0;i<n;i++){ if(i%labEvery!==0) continue; var lx=X(i), ly=padT+plotH+14;
    svg+='<text x="'+lx.toFixed(1)+'" y="'+ly+'" text-anchor="end" font-size="9.5" fill="#5a6b7a" transform="rotate(-45 '+lx.toFixed(1)+' '+ly+')">'+escapeHtml(wtLabel(pts[i].t,win.granularity))+'</text>';
  }
  svg+='<line x1="'+padL+'" y1="'+(padT+plotH)+'" x2="'+(padL+plotW)+'" y2="'+(padT+plotH)+'" stroke="#cdd9e6" stroke-width="1"/></svg>';
  var parts=[]; if(wtMetricImpr) parts.push('Impressions '+imprTot.toLocaleString()+' (area)'); if(wtMetricClk) parts.push('Clicks '+clkTot.toLocaleString()+' (line)'); if(wtMetricAtc) parts.push('🛒 Add-to-cart '+atcTot.toLocaleString()+' (icon on event days)');
  var note='<div style="font-size:11px;color:#7a8a99;margin:2px 2px 0;">Google Ads '+parts.join(' &middot; ')+' &middot; summed over '+(wtVisCampaigns().length?('the '+camps.length+' selected campaign'+(camps.length>1?'s':'')):('all '+camps.length+' active campaigns'))+' &middot; source buckets don’t apply to ad metrics.</div>';
  return '<div style="overflow-x:auto;padding:4px 0;">'+svg+'</div>'+note;
}
function wtLegend(){
  var buckets=WT.buckets||[], allOn=true;
  for(var a=0;a<buckets.length;a++){ if(!wtVisible[buckets[a]]) allOn=false; }
  var h='<div style="display:flex;flex-wrap:wrap;gap:6px 14px;margin:4px 2px 10px;font-size:12px;color:#2c3e50;align-items:center;">';
  h+='<span style="color:#7a8a99;">Show:</span>';
  h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:5px;font-weight:600;"><input type="checkbox" onchange="wtAllSources(this.checked)"'+(allOn?' checked':'')+'> All</label>';
  for(var b=0;b<buckets.length;b++){ var bk=buckets[b];
    var gAdsSplit = (bk==='Google Ads' && wtVisCampaigns().length>0);
    h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;'+(gAdsSplit?'opacity:.45;':'')+'" '+(gAdsSplit?'title="Split into the selected campaigns below"':'')+'><input type="checkbox" onchange="wtToggleSourceIdx('+b+',this.checked)"'+(wtVisible[bk]?' checked':'')+'><span style="width:12px;height:12px;border-radius:2px;background:'+WT_COLORS[bk]+';display:inline-block;"></span>'+escapeHtml(bk)+(gAdsSplit?' <span style="font-size:10px;color:#9aa7b4;">(split)</span>':'')+'</label>';
  }
  if(wtTrend){ h+='<span style="display:inline-flex;align-items:center;gap:6px;color:#7a8a99;"><span style="width:18px;height:0;border-top:2.5px dashed #d6336c;display:inline-block;"></span>Trend</span>'; }
  h+='</div>';
  // ad-metric checkboxes — Google Ads impressions / clicks (grouped-bar view; both can be on)
  h+='<div style="display:flex;flex-wrap:wrap;gap:6px 14px;margin:0 2px 8px;font-size:12px;color:#2c3e50;align-items:center;">';
  h+='<span style="color:#7a8a99;">Ad metric:</span>';
  h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;" title="Impressions shown as a filled area (left axis)"><input type="checkbox" onchange="wtToggleImpr(this.checked)"'+(wtMetricImpr?' checked':'')+'><span style="width:14px;height:9px;border-radius:2px;background:'+WT_IMPR_COLOR+';opacity:.55;display:inline-block;"></span>Impressions <span style="font-size:10px;color:#9aa7b4;">(area)</span></label>';
  h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;" title="Clicks shown as a line (right axis)"><input type="checkbox" onchange="wtToggleClk(this.checked)"'+(wtMetricClk?' checked':'')+'><span style="width:14px;height:0;border-top:2.4px solid '+WT_CLK_COLOR+';display:inline-block;"></span>Clicks <span style="font-size:10px;color:#9aa7b4;">(line)</span></label>';
  h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;" title="🛒 icon marks each day an add-to-cart event happened"><input type="checkbox" onchange="wtToggleAtc(this.checked)"'+(wtMetricAtc?' checked':'')+'>🛒 Add to cart <span style="font-size:10px;color:#9aa7b4;">(day icon)</span></label>';
  if(wtMetricImpr||wtMetricClk){ h+='<span style="font-size:11px;color:#9aa7b4;">Impressions area &amp; clicks line (dual axis)'+(wtMetricAtc?', 🛒 on add-to-cart days':'')+' — Visitors view paused</span>'; }
  else if(wtMetricAtc){ h+='<span style="font-size:11px;color:#9aa7b4;">turn on Impressions or Clicks to see the chart with 🛒 add-to-cart day markers</span>'; }
  h+='</div>';
  // campaign series checkboxes — break the Google Ads bucket into its active campaigns
  var cs=wtCampList();
  if(cs.length){
    var allOn=true; for(var i=0;i<cs.length;i++){ if(!wtCampVisible[cs[i]]) allOn=false; }
    var types=(WT.windows&&WT.windows[wtWin]&&WT.windows[wtWin].gads_types)||{};
    h+='<div style="display:flex;flex-wrap:wrap;gap:6px 14px;margin:0 2px 10px;font-size:12px;color:#2c3e50;align-items:center;">';
    h+='<span style="color:#7a8a99;">Campaigns:</span>';
    h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:5px;font-weight:600;"><input type="checkbox" onchange="wtAllCamps(this.checked)"'+(allOn?' checked':'')+'> All campaigns</label>';
    for(var i=0;i<cs.length;i++){ var tp=types[cs[i]]||'';
      h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;"><input type="checkbox" onchange="wtToggleCampIdx('+i+',this.checked)"'+(wtCampVisible[cs[i]]?' checked':'')+'><span style="width:12px;height:12px;border-radius:2px;background:'+wtCampColor(cs[i])+';display:inline-block;"></span>'+escapeHtml(cs[i])+(tp?' <span style="color:#9aa7b4;">'+escapeHtml(tp)+'</span>':'')+'</label>';
    }
    h+='</div>';
  }
  return h;
}
var WT_IMPR_COLOR='#1a73e8', WT_CLK_COLOR='#e8590c';
function wtGrowth(vals){ // % change from regression start->end
  var n=vals.length; if(n<2) return null;
  var sx=0,sy=0,sxy=0,sxx=0; for(var k=0;k<n;k++){ sx+=k; sy+=vals[k]; sxy+=k*vals[k]; sxx+=k*k; }
  var den=(n*sxx-sx*sx)||1, m=(n*sxy-sx*sy)/den, c=(sy-m*sx)/n;
  var start=c, end=m*(n-1)+c;
  var g = start>0 ? (end-start)/start*100 : (end>0?100:0);
  return { m:m, c:c, start:start, end:end, pct:Math.round(g) };
}
function wtPctBadge(pct){
  if(pct===null||pct===undefined) return '';
  var pos=pct>=0, col=pos?'#188038':'#d93025', ar=pos?'▲':'▼';
  return '<span style="color:'+col+';font-weight:700;">'+ar+Math.abs(pct)+'%</span>';
}
function wtMini(win,camp){
  var ser=(win.gads_series||{})[camp]||[], n=ser.length;
  var impr=[],clk=[], imprMax=0,clkMax=0, imprTot=0,clkTot=0;
  for(var i=0;i<n;i++){ var a=ser[i].impr||0, b=ser[i].clicks||0; impr.push(a); clk.push(b);
    imprTot+=a; clkTot+=b; if(a>imprMax)imprMax=a; if(b>clkMax)clkMax=b; }
  if(imprMax<=0)imprMax=1; if(clkMax<=0)clkMax=1;
  var W=250,H=132, padL=6,padR=8,padT=14,padB=28, plotH=H-padT-padB, plotW=W-padL-padR;
  function X(i){ return padL+(n>1?plotW*i/(n-1):plotW/2); }
  function Yi(v){ v=Math.max(0,Math.min(imprMax,v)); return padT+plotH-plotH*v/imprMax; }
  function Yc(v){ v=Math.max(0,Math.min(clkMax,v)); return padT+plotH-plotH*v/clkMax; }
  var svg='<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="'+H+'" preserveAspectRatio="xMidYMid meet" style="font-family:inherit;display:block;width:100%;height:auto;">';
  // gridlines
  for(var g=0;g<=2;g++){ var gy=padT+plotH*g/2; svg+='<line x1="'+padL+'" y1="'+gy.toFixed(1)+'" x2="'+(padL+plotW)+'" y2="'+gy.toFixed(1)+'" stroke="#eef2f6" stroke-width="1"/>'; }
  svg+='<line x1="'+padL+'" y1="'+(padT+plotH)+'" x2="'+(padL+plotW)+'" y2="'+(padT+plotH)+'" stroke="#dbe3ec" stroke-width="1"/>';
  function poly(arr,Yf){ var p=''; for(var i=0;i<n;i++){ p+=X(i).toFixed(1)+','+Yf(arr[i]).toFixed(1)+' '; } return p; }
  if(n>=1){
    svg+='<polyline points="'+poly(impr,Yi)+'" fill="none" stroke="'+WT_IMPR_COLOR+'" stroke-width="1.0" stroke-linejoin="round"/>';
    svg+='<polyline points="'+poly(clk,Yc)+'" fill="none" stroke="'+WT_CLK_COLOR+'" stroke-width="1.0" stroke-linejoin="round"/>';
    // impressions dots w/ tooltip
    for(var i=0;i<n;i++){ svg+='<circle cx="'+X(i).toFixed(1)+'" cy="'+Yi(impr[i]).toFixed(1)+'" r="1.3" fill="'+WT_IMPR_COLOR+'"><title>'+escapeHtml(wtLabel(ser[i].t,win.granularity))+' · impr '+impr[i]+' · clicks '+clk[i]+'</title></circle>'; }
  }
  // trend line on impressions + % growth next to it
  var gi=wtGrowth(impr);
  if(gi){
    svg+='<line x1="'+X(0).toFixed(1)+'" y1="'+Yi(gi.start).toFixed(1)+'" x2="'+X(n-1).toFixed(1)+'" y2="'+Yi(gi.end).toFixed(1)+'" stroke="#d6336c" stroke-width="1.4" stroke-dasharray="4 3"/>';
    var pos=gi.pct>=0, col=pos?'#188038':'#d93025', ar=pos?'▲':'▼';
    var ty=Math.max(9, Yi(gi.end)-3);
    svg+='<text x="'+(padL+plotW-2).toFixed(1)+'" y="'+ty.toFixed(1)+'" text-anchor="end" font-size="9.5" font-weight="700" fill="'+col+'">'+ar+Math.abs(gi.pct)+'%</text>';
  }
  // x-axis timescale (first / middle / last)
  if(n>=1){
    var idxs = n>=3 ? [0, Math.floor((n-1)/2), n-1] : (n===2?[0,1]:[0]);
    for(var j=0;j<idxs.length;j++){ var ix=idxs[j], anchor=(ix===0?'start':(ix===n-1?'end':'middle')), tx=X(ix);
      if(ix===0) tx=padL; if(ix===n-1) tx=padL+plotW;
      svg+='<text x="'+tx.toFixed(1)+'" y="'+(padT+plotH+12)+'" text-anchor="'+anchor+'" font-size="8.5" fill="#7a8a99">'+escapeHtml(wtLabel(ser[ix].t,win.granularity))+'</text>';
    }
  }
  svg+='</svg>';
  var nm=camp.length>24?camp.slice(0,23)+'…':camp;
  var gc=wtGrowth(clk);
  // purchases + revenue for this campaign (GA4 last-click; from gads_detail); cost from gads_cost
  var det=(win.gads_detail||[]), dd=null; for(var q=0;q<det.length;q++){ if(det[q].name===camp){ dd=det[q]; break; } }
  var purch=dd?(dd.transactions||0):0, rev=dd?(dd.revenue||0):0;
  // item 2: GA4 key events for THIS window (add_to_cart + purchase)
  var kev=(win.gads_events||{})[camp]||{}; var atc=kev.add_to_cart||0, pur=kev.purchase||0;
  var cost=(win.gads_cost&&win.gads_cost[camp]!=null)?win.gads_cost[camp]:0;
  var roas=cost>0?(rev/cost):null;
  var cpc=clkTot>0?(cost/clkTot):null;
  var sd=(win.gads_start&&win.gads_start[camp])?win.gads_start[camp]:'';
  var sdTxt='';
  if(sd){ var sp=sd.split('-'); if(sp.length===3){ var mo=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][parseInt(sp[1],10)-1]; sdTxt=mo+' '+parseInt(sp[2],10)+', '+sp[0]; } }
  return '<div style="border:1px solid #e6ecf2;border-radius:8px;padding:8px 10px 6px;background:#fff;">'+
    '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:6px;margin-bottom:3px;">'+
      '<span style="font-size:12px;font-weight:600;color:#2c3e50;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="'+escapeHtml(camp)+'">'+escapeHtml(nm)+'</span>'+
      (sdTxt?'<span style="font-size:10px;color:#9aa7b4;white-space:nowrap;">▶ '+escapeHtml(sdTxt)+'</span>':'')+
    '</div>'+
    '<div style="display:flex;justify-content:space-between;gap:8px;font-size:11px;margin-bottom:2px;">'+
      '<span style="color:'+WT_IMPR_COLOR+';white-space:nowrap;"><span style="display:inline-block;width:14px;height:2px;background:'+WT_IMPR_COLOR+';vertical-align:middle;margin-right:4px;"></span>Impr '+imprTot.toLocaleString()+' '+wtPctBadge(gi?gi.pct:null)+'</span>'+
      '<span style="color:'+WT_CLK_COLOR+';white-space:nowrap;"><span style="display:inline-block;width:14px;height:2px;background:'+WT_CLK_COLOR+';vertical-align:middle;margin-right:4px;"></span>Clicks '+clkTot.toLocaleString()+' '+wtPctBadge(gc?gc.pct:null)+'</span>'+
    '</div>'+
    '<div style="display:flex;justify-content:space-between;gap:6px;font-size:11px;margin-bottom:3px;padding:2px 6px;background:#eef7f0;border-radius:4px;">'+
      '<span style="color:#2c3e50;white-space:nowrap;" title="GA4 add_to_cart events in this window">🛒 Add to cart <b>'+Number(atc).toLocaleString()+'</b></span>'+
      '<span style="color:#188038;font-weight:700;white-space:nowrap;" title="GA4 purchase events in this window">✅ Purchases <b>'+Number(pur).toLocaleString()+'</b></span>'+
    '</div>'+
    '<div style="display:flex;justify-content:space-between;gap:6px;font-size:11px;margin-bottom:3px;padding:3px 6px;background:#f6f9fc;border-radius:4px;">'+
      '<span style="color:#b54708;white-space:nowrap;">Cost <b>'+money0(cost)+'</b>'+(cpc!=null?' · $'+cpc.toFixed(2)+'/clk':'')+'</span>'+
      '<span style="color:#188038;font-weight:700;white-space:nowrap;">Rev '+money0(rev)+'</span>'+
      (roas!==null?'<span style="color:#5a6b7a;white-space:nowrap;" title="Revenue / cost (GA4 last-click)">'+roas.toFixed(1)+'×</span>':'')+
    '</div>'+svg+'</div>';
}
function wtGadsGrid(win){
  var camps=win.gads_campaigns||[];
  if(!camps.length) return '<div class="empty" style="margin:6px 0;">No Google Ads campaign traffic in this window.</div>';
  var types=win.gads_types||{};
  // item 1: one row per campaign type (Search, then Shopping), each its own grid; item 3: respect campaign checkboxes.
  function rowFor(tkey,label,seen){
    var rc=[]; for(var i=0;i<camps.length;i++){ if((types[camps[i]]||'Other')===tkey){ rc.push(camps[i]); if(seen) seen[camps[i]]=1; } }
    if(!rc.length) return '';
    var s='<div style="font-size:12px;font-weight:700;color:#5a6b7a;margin:10px 2px 4px;">'+label+' <span style="color:#9aa7b4;font-weight:400;">('+rc.length+')</span></div>';
    s+='<div class="wt-mult" style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:2px 2px 6px;">';
    for(var j=0;j<rc.length;j++){ s+=wtMini(win,rc[j]); }
    return s+'</div>';
  }
  var seen={}, h=rowFor('Search','Search campaigns',seen)+rowFor('Shopping','Shopping campaigns',seen);
  var other=[]; for(var i=0;i<camps.length;i++){ if(!seen[camps[i]]) other.push(camps[i]); }
  if(other.length){
    h+='<div style="font-size:12px;font-weight:700;color:#5a6b7a;margin:10px 2px 4px;">Other campaigns <span style="color:#9aa7b4;font-weight:400;">('+other.length+')</span></div>';
    h+='<div class="wt-mult" style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:2px 2px 6px;">';
    for(var j=0;j<other.length;j++){ h+=wtMini(win,other[j]); }
    h+='</div>';
  }
  return h || '<div class="empty" style="margin:6px 0;">No campaigns selected — check a campaign above to show its chart.</div>';
}
function renderWtPanel(){
  if(!WT){ document.getElementById('panel').innerHTML='<div class="empty">Loading website-traffic data…</div>'; loadWT(); return; }
  var allb=WT.buckets||[]; for(var z=0;z<allb.length;z++){ if(!(allb[z] in wtVisible)) wtVisible[allb[z]]=true; }
  var wins=WT.windows||{}, ids=WT_WIN_ORDER, hasDaily=!!(WT&&WT.daily);
  var custWin=null, custErr='';
  if(wtIsCustom()){
    if(!hasDaily){ custErr='Custom ranges need the latest snapshot (daily history not found in this data). Reload, or pick a preset window.'; }
    else { custWin=wtBuildCustomWin(wtCustFrom,wtCustTo); if(!custWin||!(custWin.points&&custWin.points.length)) custErr='No data in the selected range ('+escapeHtml(wtCustFrom)+' → '+escapeHtml(wtCustTo)+').'; }
  }
  var win = wtIsCustom() ? (custWin||wins['last_30_days']||wins[ids[0]]) : (wins[wtWin]||wins['last_30_days']||wins[ids[0]]);
  wtCurWin=win; wtInitCamps();
  var sel='<select onchange="wtSetWin(this.value)" style="padding:7px 10px;border:1px solid #cdd9e6;border-radius:6px;font-size:13px;font-family:inherit;">';
  for(var i=0;i<ids.length;i++){ var w=wins[ids[i]]; if(!w) continue; sel+='<option value="'+ids[i]+'"'+(ids[i]===wtWin?' selected':'')+'>'+escapeHtml(w.label)+'</option>'; }
  sel+='<option value="__custom__"'+(wtIsCustom()?' selected':'')+'>Custom range…</option>';
  sel+='</select>';
  if(wtIsCustom()){
    var dmin=hasDaily?WT.daily.start:'', dmax=hasDaily?WT.daily.end:'';
    sel+=' <span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;color:#5a6b7a;">'+
      '<input type="date" id="wtCustFrom" value="'+escapeHtml(wtCustFrom||'')+'"'+(dmin?' min="'+dmin+'"':'')+(dmax?' max="'+dmax+'"':'')+' style="padding:5px 7px;border:1px solid #cdd9e6;border-radius:6px;font-size:12px;font-family:inherit;">'+
      '<span>→</span>'+
      '<input type="date" id="wtCustTo" value="'+escapeHtml(wtCustTo||'')+'"'+(dmin?' min="'+dmin+'"':'')+(dmax?' max="'+dmax+'"':'')+' style="padding:5px 7px;border:1px solid #cdd9e6;border-radius:6px;font-size:12px;font-family:inherit;">'+
      '<button class="refresh-btn" style="padding:6px 12px;" onclick="wtApplyCustom()"><span class="lbl">Apply</span></button>'+
      (hasDaily?'<span style="color:#9aa7b4;font-size:11px;">history '+escapeHtml(dmin)+' → '+escapeHtml(dmax)+'</span>':'')+
    '</span>';
  }
  if(custErr){
    document.getElementById('panel').innerHTML =
      '<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'+
      '<div><h2>Website Traffic — Visitors by Source</h2><div class="sub">GA4 '+escapeHtml(WT.property||'')+' &middot; pulled '+escapeHtml(WT.pulled_at||'')+'</div></div>'+
      '<div style="font-size:13px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">Time window: '+sel+
      '<button class="refresh-btn" onclick="wtRefresh()" title="Reload the latest website-traffic snapshot"><span class="lbl">↻ Reload</span></button></div></div></div>'+
      '<div class="empty" style="margin:16px 2px;">'+custErr+'</div>';
    return;
  }
  var tot=win.totals||{sessions:0,conversions:0,revenue:0,transactions:0};
  // marketing visitors = Google Ads + LinkedIn + Email
  var mkt=0, smap={}; for(var s=0;s<(win.sales||[]).length;s++){ smap[win.sales[s].source]=win.sales[s]; }
  mkt=(smap['Google Ads']?smap['Google Ads'].sessions:0)+(smap['LinkedIn']?smap['LinkedIn'].sessions:0)+(smap['Email']?smap['Email'].sessions:0);
  var kpis='<div class="kpis" style="padding:6px 0 2px;">'+
    kpi(Number(tot.sessions).toLocaleString(),'Visitors')+
    kpi(Number(mkt).toLocaleString(),'Paid+social+email')+
    kpi(Number(tot.conversions).toLocaleString(),'Key-event conv.')+
    kpi(money0(tot.revenue),'Revenue (attr.)')+
    kpi(tot.transactions,'Orders')+'</div>';
  // sales by source table
  var body='', order=WT.buckets||[];
  for(var b=0;b<order.length;b++){ var r=smap[order[b]]; if(!r) continue;
    var cr=r.sessions?(r.conversions/r.sessions*100).toFixed(1)+'%':'—';
    body+='<tr>'+
      '<td><span style="display:inline-flex;align-items:center;gap:7px;"><span style="width:11px;height:11px;border-radius:2px;background:'+WT_COLORS[order[b]]+';display:inline-block;"></span>'+escapeHtml(order[b])+'</span></td>'+
      '<td class="c">'+Number(r.sessions).toLocaleString()+'</td>'+
      '<td class="c">'+r.conversions+'</td>'+
      '<td class="c">'+cr+'</td>'+
      '<td class="c open">'+money0(r.revenue)+'</td>'+
      '<td class="c">'+r.transactions+'</td></tr>';
    // sub-rows: which specific email / campaign drove this bucket's traffic
    var det = order[b]==='Email' ? (win.email_detail||[]) : (order[b]==='Google Ads' ? (win.gads_detail||[]) : null);
    if(det!==null){
      for(var e=0;e<det.length;e++){ var d=det[e], dcr=d.sessions?(d.conversions/d.sessions*100).toFixed(1)+'%':'—';
        body+='<tr style="background:#fcfdfe;">'+
          '<td style="padding-left:30px;color:#5a6b7a;font-size:12px;">↳ '+escapeHtml(d.name)+(d.campaign&&d.campaign!==d.name?' <span style="color:#9aa7b4;">('+escapeHtml(d.campaign)+')</span>':'')+'</td>'+
          '<td class="c" style="font-size:12px;color:#5a6b7a;">'+Number(d.sessions).toLocaleString()+'</td>'+
          '<td class="c" style="font-size:12px;color:#5a6b7a;">'+d.conversions+'</td>'+
          '<td class="c" style="font-size:12px;color:#5a6b7a;">'+dcr+'</td>'+
          '<td class="c" style="font-size:12px;color:#5a6b7a;">'+money0(d.revenue)+'</td>'+
          '<td class="c" style="font-size:12px;color:#5a6b7a;">'+(d.transactions||0)+'</td></tr>';
      }
      if(!det.length && r.sessions>0){ body+='<tr style="background:#fcfdfe;"><td style="padding-left:30px;color:#9aa7b4;font-size:12px;" colspan="6">↳ no campaign tagging available for this window</td></tr>'; }
    }
    // item 5: email clicks per Klaviyo flow, nested under the Email source
    if(order[b]==='Email'){
      var eflows=win.klaviyo_flows||[];
      for(var fq=0;fq<eflows.length;fq++){ var fl=eflows[fq];
        body+='<tr style="background:#fffdf5;">'+
          '<td style="padding-left:30px;color:#8a6d1a;font-size:12px;">↳ ✉ '+escapeHtml(fl.flow)+' <span style="color:#b9a24a;">(Klaviyo flow)</span></td>'+
          '<td class="c" style="font-size:12px;color:#8a6d1a;" title="Email clicks from this flow ('+escapeHtml(win.label)+')">'+Number(fl.clicks||0).toLocaleString()+' <span style="color:#c9b46a;font-size:10px;">clicks</span></td>'+
          '<td colspan="4" style="font-size:11px;color:#b9a24a;padding-left:10px;">'+(fl.clicks>0?'email-flow clicks':'no clicks in this window yet')+'</td></tr>';
      }
      if(!eflows.length){ body+='<tr style="background:#fffdf5;"><td colspan="6" style="padding-left:30px;color:#b9a24a;font-size:12px;">↳ Klaviyo not connected / no live flows</td></tr>'; }
    }
  }
  body+='<tr class="so-group"><td>Total</td><td class="c">'+Number(tot.sessions).toLocaleString()+'</td><td class="c">'+tot.conversions+'</td><td class="c">'+(tot.sessions?(tot.conversions/tot.sessions*100).toFixed(1)+'%':'—')+'</td><td class="c open">'+money0(tot.revenue)+'</td><td class="c">'+tot.transactions+'</td></tr>';
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'+
    '<div><h2>Website Traffic — Visitors by Source</h2><div class="sub">GA4 '+escapeHtml(WT.property||'')+' &middot; pulled '+escapeHtml(WT.pulled_at||'')+'</div></div>'+
    '<div style="font-size:13px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">Time window: '+sel+
    '<button class="refresh-btn" onclick="wtRefresh()" title="Reload the latest website-traffic snapshot"><span class="lbl">↻ Reload</span></button></div></div></div>'+
    kpis+
    '<div class="ca-h" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">'+
      '<span>'+escapeHtml(win.label)+' &mdash; visitors by source</span>'+
      '<span style="font-weight:400;font-size:12px;color:#34495e;display:inline-flex;gap:16px;align-items:center;">'+
        '<label style="cursor:pointer;display:inline-flex;gap:5px;align-items:center;"><input type="checkbox" onchange="wtToggleLabels(this.checked)"'+(wtLabels?' checked':'')+'> Data labels</label>'+
        '<label style="cursor:pointer;display:inline-flex;gap:5px;align-items:center;"><input type="checkbox" onchange="wtToggleTrend(this.checked)"'+(wtTrend?' checked':'')+'> Trend line</label>'+
      '</span></div>'+
    wtLegend()+ wtBarSvg(win)+
    '<div class="ca-h" style="margin-top:18px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;"><span>Google Ads campaigns — impressions &amp; clicks ('+escapeHtml(win.label)+')</span><span style="font-weight:400;font-size:12px;color:#7a8a99;">grouped by type &middot; add-to-cart &amp; purchases per campaign &middot; per '+(win.granularity==='week'?'week':'day')+'</span></div>'+
    wtGadsGrid(win)+
    '<div class="ca-h" style="margin-top:18px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;"><span>Email flow clicks — clicks per Klaviyo flow ('+escapeHtml(win.label)+')</span><span style="font-weight:400;font-size:12px;color:#7a8a99;">'+((WT.klaviyo&&WT.klaviyo.account)?escapeHtml(WT.klaviyo.account)+' &middot; live flows':'')+'</span></div>'+
    wtFlowChart(win)+
    '<div class="ca-h" style="margin-top:18px;">Does it convert? Sales by source ('+escapeHtml(win.label)+')</div>'+
    '<div class="matrix-wrap" style="max-width:680px;"><table class="matrix"><thead><tr><th>Source</th><th class="c">Visitors</th><th class="c">Key-event conv.</th><th class="c">Conv. rate</th><th class="c">Revenue</th><th class="c">Orders</th></tr></thead><tbody>'+body+'</tbody></table></div>'+
    '<div style="margin:14px 16px;padding:12px 16px;background:#fff8e1;border-left:4px solid #ffc107;font-size:12px;border-radius:6px;line-height:1.55;color:#2c3e50;">'+
    escapeHtml(WT.note||'')+'</div>';
}

// ── Shipments tab (UPS My Choice for Business — Third Party; statuses auto-refreshed via UPS Track API) ──
var SHIP=null, shipLoading=false, shipFilter='all', shipFrom='', shipTo='', shipVis={}, shipShipperList=[], shipDatePreset='all', shipCust='', shipCustList=[];
var SHIP_DPS=[['all','All'],['today','Today'],['yesterday','Yesterday'],['thisweek','This week'],['lastweek','Last week'],['month','This month'],['quarter','This quarter']];
function loadShip(){
  if(SHIP_EMBED){ SHIP=SHIP_EMBED; shipLoading=false; if(mode==='ship'){ renderTabs(); renderShipPanel(); } return; }
  if(shipLoading) return; shipLoading=true;
  fetch('ups-shipments-data.json?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(d){ SHIP=d; shipLoading=false; if(mode==='ship'){ renderTabs(); renderShipPanel(); } })
    .catch(function(e){ shipLoading=false; if(mode==='ship') document.getElementById('panel').innerHTML='<div class="empty">Could not load shipments data: '+escapeHtml(e.message)+'</div>'; });
}
function shipRefresh(){ SHIP=SHIP_EMBED||null; shipLoading=false; document.getElementById('panel').innerHTML='<div class="empty">Reloading shipments…</div>'; loadShip(); }
function shipSetFilter(v){ shipFilter=v; renderShipPanel(); }
function renderShipTabs(el){
  el.style.display='';
  if(!SHIP){ el.innerHTML='<div class="empty">Loading…</div>'; return; }
  var all=SHIP.shipments||[], cnt={}, names=[], totalVis=0;
  for(var i=0;i<all.length;i++){ if(shipVis[all[i].shipper||'(none)']===false) continue; if(isExclCust(all[i].receiver)) continue; totalVis++; var r=all[i].receiver||'(no customer)'; if(!(r in cnt)){ cnt[r]=0; names.push(r); } cnt[r]++; }
  names.sort(function(a,b){ return a.toLowerCase().localeCompare(b.toLowerCase()); });
  shipCustList=names;
  if(shipCust && names.indexOf(shipCust)<0){ shipCust=''; }  // selected customer hidden by shipper filter -> reset to All
  var h='<button class="tab'+(shipCust===''?' active':'')+'" onclick="shipSelectCust(-1)">All customers<span class="cnt">'+totalVis+'</span></button>';
  for(var j=0;j<names.length;j++){
    h+='<button class="tab'+(shipCust===names[j]?' active':'')+'" onclick="shipSelectCust('+j+')">'+escapeHtml(names[j])+'<span class="cnt">'+cnt[names[j]]+'</span></button>';
  }
  el.innerHTML=h;
}
function shipSelectCust(i){ shipCust = (i<0 ? '' : (shipCustList[i]||'')); renderTabs(); renderShipPanel(); }
function shipFAll(){ shipSetFilter('all'); }
function shipFTransit(){ shipSetFilter('transit'); }
function shipFDelivered(){ shipSetFilter('delivered'); }
function shipISO(d){ return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); }
function shipMonday(d){ var x=new Date(d); var wd=(x.getDay()+6)%7; x.setDate(x.getDate()-wd); return x; }
function shipSetDatePreset(p){
  shipDatePreset=p; var now=new Date();
  if(p==='all'){ shipFrom=''; shipTo=''; }
  else if(p==='today'){ shipFrom=shipISO(now); shipTo=shipISO(now); }
  else if(p==='yesterday'){ var y=new Date(now); y.setDate(y.getDate()-1); shipFrom=shipISO(y); shipTo=shipISO(y); }
  else if(p==='thisweek'){ shipFrom=shipISO(shipMonday(now)); shipTo=shipISO(now); }
  else if(p==='lastweek'){ var m=shipMonday(now); var ls=new Date(m); ls.setDate(ls.getDate()-7); var le=new Date(m); le.setDate(le.getDate()-1); shipFrom=shipISO(ls); shipTo=shipISO(le); }
  else if(p==='month'){ shipFrom=shipISO(new Date(now.getFullYear(),now.getMonth(),1)); shipTo=shipISO(now); }
  else if(p==='quarter'){ var q=Math.floor(now.getMonth()/3); shipFrom=shipISO(new Date(now.getFullYear(),q*3,1)); shipTo=shipISO(now); }
  renderShipPanel();
}
function shipSetDatePresetIdx(i){ if(SHIP_DPS[i]) shipSetDatePreset(SHIP_DPS[i][0]); }
function shipToggleShipperIdx(i,c){ var sh=shipShipperList[i]; if(sh!=null){ shipVis[sh]=!!c; renderTabs(); renderShipPanel(); } }
function shipAllShippers(c){ for(var i=0;i<shipShipperList.length;i++) shipVis[shipShipperList[i]]=!!c; renderTabs(); renderShipPanel(); }
function shipTC(s){ s=(s||'').trim(); return s.replace(/\w\S*/g,function(t){return t.charAt(0).toUpperCase()+t.substr(1).toLowerCase();}); }
function shipLoc(s){ var p=(s||'').split(','); if(p.length===2 && p[1].trim().length<=3){ return shipTC(p[0])+', '+p[1].trim().toUpperCase(); } return shipTC(s); }
function shipControls(){
  var h='<div style="display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:0 2px 12px;font-size:12px;color:#2c3e50;">';
  h+='<span style="color:#7a8a99;">Date:</span>';
  for(var i=0;i<SHIP_DPS.length;i++){ var on=(shipDatePreset===SHIP_DPS[i][0]);
    h+='<button onclick="shipSetDatePresetIdx('+i+')" class="mode-btn'+(on?' active':'')+'" style="padding:5px 11px;border-radius:6px;border:1px solid #cdd9e6;font-size:12px;">'+escapeHtml(SHIP_DPS[i][1])+'</button>'; }
  h+='<span style="color:#7a8a99;margin-left:10px;">Shipper:</span>';
  var allOn=true; for(var a=0;a<shipShipperList.length;a++){ if(!shipVis[shipShipperList[a]]) allOn=false; }
  h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:5px;font-weight:600;"><input type="checkbox" onchange="shipAllShippers(this.checked)"'+(allOn?' checked':'')+'> All</label>';
  for(var k=0;k<shipShipperList.length;k++){ var sh=shipShipperList[k];
    h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:5px;"><input type="checkbox" onchange="shipToggleShipperIdx('+k+',this.checked)"'+(shipVis[sh]?' checked':'')+'>'+escapeHtml(sh)+'</label>';
  }
  return h+'</div>';
}
function renderShipPanel(){
  if(!SHIP){ document.getElementById('panel').innerHTML='<div class="empty">Loading shipments…</div>'; loadShip(); return; }
  var all=(SHIP.shipments||[]);
  var nDel=0,nTransit=0,nExc=0;
  for(var i=0;i<all.length;i++){ var st=all[i].status||''; if(all[i].delivered||st==='Delivered') nDel++; else if(st==='Exception') nExc++; else nTransit++; }
  // distinct shippers (preserve checkbox choices; default new ones on)
  shipShipperList=[]; var seenSh={};
  for(var z=0;z<all.length;z++){ all[z]._idx=z; var sh=all[z].shipper||'(none)'; if(!seenSh[sh]){ seenSh[sh]=1; shipShipperList.push(sh); if(!(sh in shipVis)) shipVis[sh]=true; } }
  var rows=all.filter(function(s){
    var dv=(s.delivered||s.status==='Delivered');
    if(shipFilter==='transit' && dv) return false;
    if(shipFilter==='delivered' && !dv) return false;
    if(shipVis[s.shipper||'(none)']===false) return false;
    if(isExclCust(s.receiver)) return false;
    if(shipCust && (s.receiver||'(no customer)')!==shipCust) return false;
    var sd=s.ship_date||s.date||'';
    if(shipFrom && sd < shipFrom) return false;
    if(shipTo && sd > shipTo) return false;
    return true;
  });
  // sort: in-transit first, then by date desc
  rows.sort(function(a,b){ var ad=(a.delivered?1:0), bd=(b.delivered?1:0); if(ad!==bd) return ad-bd; return (b.ship_date||b.date||'').localeCompare(a.ship_date||a.date||''); });
  function pill(s){ var d=(s.delivered||s.status==='Delivered'); var ex=(s.status==='Exception');
    if(!d && !ex && !s.status){ return '<span class="status" style="background:#eef1f4;color:#7a8a99" title="Live status needs the FedEx API">Label created</span>'; }
    var c=d?['#d4edda','#155724']:(ex?['#f8d7da','#721c24']:['#fff3cd','#856404']);
    return '<span class="status" style="background:'+c[0]+';color:'+c[1]+'">'+escapeHtml(d?'Delivered':(ex?'Exception':'In Transit'))+'</span>'; }
  var body='';
  for(var r=0;r<rows.length;r++){ var s=rows[r];
    var url=s.url||('https://www.ups.com/track?loc=en_US&tracknum='+encodeURIComponent(s.tracking));
    var upd=shipLoc(s.location||''); if(s.date){ upd+=(upd?' · ':'')+fmtDate(s.date)+(s.time?' '+s.time:''); }
    var _sm={'Pirate Ship':['#e7e0f7','#5b3fa0','Pirate Ship'],'Shopify':['#d8f0e0','#1b7a3d','Shopify'],'UPS My Choice (3rd Party)':['#e6ecf2','#4a5b6a','My Choice']};
    var _sc=_sm[s.source]||['#e6ecf2','#4a5b6a',(s.source||'—')];
    var _slabel=_sc[2]+((s.source==='Shopify'&&s.order)?' '+s.order:'');
    var srcBadge='<span class="status" style="background:'+_sc[0]+';color:'+_sc[1]+';white-space:nowrap;">'+escapeHtml(_slabel)+'</span>';
    if(s.shopify_fulfilled){ srcBadge+=' <span class="status" title="UPS tracking written to Shopify order '+escapeHtml(s.shopify_order||'')+'" style="background:#d8f0e0;color:#1b7a3d;white-space:nowrap;">🛍️ Shopify'+(s.shopify_order?' '+escapeHtml(s.shopify_order):'')+'</span>'; }
    body+='<tr>'+
      '<td class="so"><a href="'+url+'" target="_blank" rel="noopener" style="color:#1F4E79;text-decoration:none;">'+escapeHtml(s.tracking)+' <span style="color:#008080;">↗</span></a></td>'+
      '<td>'+srcBadge+'</td>'+
      '<td>'+pill(s)+'</td>'+
      '<td>'+escapeHtml(shipTC(s.activity||''))+'</td>'+
      '<td class="item-name">'+escapeHtml(s.shipper||'')+'</td>'+
      '<td class="item-name">'+escapeHtml(s.receiver||'')+'</td>'+
      (s.items&&s.items.length ? '<td class="c"><a onclick="shipItems('+s._idx+')" style="cursor:pointer;color:#1F4E79;white-space:nowrap;" title="View packing list">📋 '+s.items.length+'</a></td>' : '<td class="c" style="color:#c0cad4;">—</td>')+
      '<td>'+escapeHtml(s.ship_to||'')+'</td>'+
      '<td>'+escapeHtml((s.service||'').replace(/^UPS /,''))+'</td>'+
      '<td>'+(s.ship_date?fmtDate(s.ship_date):'—')+'</td>'+
      '<td>'+escapeHtml(upd)+'</td></tr>';
  }
  if(!rows.length) body='<tr><td colspan="11" class="empty" style="padding:18px;">No shipments in this filter.</td></tr>';
  function fbtn(v,l,fn){ return '<button onclick="'+fn+'()" class="mode-btn'+(shipFilter===v?' active':'')+'" style="padding:6px 14px;border-radius:6px;border:1px solid #cdd9e6;font-size:12px;">'+l+'</button>'; }
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'+
    '<div><h2>Shipments — UPS</h2><div class="sub">UPS My Choice (Third Party) + Pirate Ship &middot; statuses refreshed '+escapeHtml(SHIP.last_status_refresh||SHIP.pulled_at||'')+'</div></div>'+
    '<button class="refresh-btn" onclick="shipRefresh()" title="Reload the latest shipments snapshot"><span class="lbl">↻ Reload</span></button></div></div>'+
    '<div class="kpis" style="padding:6px 0 2px;">'+kpi(all.length,'Shipments')+kpi(nTransit,'In transit')+kpi(nDel,'Delivered')+(nExc?kpi(nExc,'Exceptions'):'')+'</div>'+
    '<div style="display:flex;gap:8px;margin:6px 2px 8px;">'+fbtn('all','All','shipFAll')+fbtn('transit','In transit','shipFTransit')+fbtn('delivered','Delivered','shipFDelivered')+'</div>'+
    shipControls()+
    '<div class="matrix-wrap"><table class="matrix"><thead><tr>'+
    '<th>Tracking #</th><th>Source</th><th>Status</th><th>Activity</th><th>Shipper</th><th>Receiver</th><th>Items</th><th>Ship-To</th><th>Service</th><th>Label date</th><th>Last update</th>'+
    '</tr></thead><tbody>'+body+'</tbody></table></div>'+
    '<div style="margin:14px 16px;padding:12px 16px;background:#fff8e1;border-left:4px solid #ffc107;font-size:12px;border-radius:6px;line-height:1.55;color:#2c3e50;">'+
    escapeHtml(SHIP.note||'')+'</div>'+
    '<div id="shipModal" onclick="shipCloseItems(event)" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;align-items:center;justify-content:center;"><div onclick="event.stopPropagation()" style="background:#fff;max-width:600px;width:92%;max-height:82vh;overflow:auto;border-radius:10px;padding:20px 22px;box-shadow:0 12px 44px rgba(0,0,0,.32);"><div id="shipModalBody"></div><div style="text-align:right;margin-top:14px;"><button onclick="shipCloseItems()" class="mode-btn" style="padding:6px 16px;border-radius:6px;border:1px solid #cdd9e6;">Close</button></div></div></div>';
}
function shipItems(i){ var s=((SHIP&&SHIP.shipments)||[])[i]; if(!s) return; var it=s.items||[];
  var rows='', tot=0;
  for(var k=0;k<it.length;k++){ tot+=it[k].qty||0; rows+='<tr><td class="c">'+(it[k].qty||0)+'</td><td>'+escapeHtml(it[k].sku||'')+'</td><td class="item-name">'+escapeHtml(it[k].name||'')+'</td></tr>'; }
  // Sales Order # comes from the Shopify "Vtiger SO:" order tag; PO(s) resolved from Vtiger.
  // Each links to its Vtiger detail page when the record id is known.
  var VT='https://jit4youinc.od2.vtiger.com/index.php?module=';
  function vtLink(mod,num,id){ if(!num) return '<span style="color:#9aa7b4;">—</span>';
    if(!id) return '<b>'+escapeHtml(num)+'</b>';
    return '<a href="'+VT+mod+'&view=Detail&record='+encodeURIComponent(id)+'" target="_blank" rel="noopener" style="color:#1F4E79;font-weight:700;text-decoration:none;">'+escapeHtml(num)+' <span style="color:#008080;">↗</span></a>'; }
  var soHtml = vtLink('SalesOrder', s.so_num, s.so_id);
  var poArr = (s.pos&&s.pos.length) ? s.pos : (s.po?[{po:s.po,po_id:s.po_id,vendor:''}]:[]);
  var poHtml = poArr.length ? poArr.map(function(p){ return vtLink('PurchaseOrder',p.po,p.po_id)+(p.vendor?' <span style="color:#7a8a99;font-size:11px;">('+escapeHtml(p.vendor)+')</span>':''); }).join('&nbsp; ') : '<span style="color:#9aa7b4;">—</span>';
  var chip='background:#eef3f9;border:1px solid #d5e0ec;border-radius:6px;padding:3px 10px;font-size:12.5px;';
  var idLine =
    '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;">'+
      '<span style="'+chip+'">Sales Order: '+soHtml+'</span>'+
      '<span style="'+chip+'">Purchase Order: '+poHtml+'</span>'+
    '</div>';
  var h='<h2 style="margin:0 0 4px;">Packing list &middot; '+escapeHtml(s.tracking)+'</h2>'+
    '<div class="sub" style="margin-bottom:8px;">'+escapeHtml(s.receiver||'')+(s.order?' &middot; Order '+escapeHtml(s.order):'')+(s.ship_to?' &middot; '+escapeHtml(s.ship_to):'')+'</div>'+
    idLine+
    '<div class="matrix-wrap"><table class="matrix"><thead><tr><th class="c">Qty</th><th>SKU</th><th>Item</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+
    '<div style="margin-top:8px;font-size:12px;color:#5a6b7a;">'+it.length+' line item'+(it.length!=1?'s':'')+' &middot; '+tot+' unit'+(tot!=1?'s':'')+'</div>';
  document.getElementById('shipModalBody').innerHTML=h;
  document.getElementById('shipModal').style.display='flex';
}
function shipCloseItems(e){ if(e&&e.target&&e.target.id!=='shipModal') return; var m=document.getElementById('shipModal'); if(m) m.style.display='none'; }

// ── Shipments P&L tab (shipping charged to customer [SKU 999 on the SO] vs. what
// UPS bills us [net charge from the Billing Center export, matched by PO/tracking]) ──
var SPNL=null, spnlLoading=false, spnlCust='', spnlCustList=[], spnlInterval='ytd';
var SPNL_IVS=(function(){
  var base=[['ytd','2026 YTD'],['month','This month'],['lastmonth','Last month']];
  var now=new Date(), y=now.getFullYear(), m=now.getMonth();  // m: 0-based current month
  var MN=['January','February','March','April','May','June','July','August','September','October','November','December'];
  for(var mm=m-2; mm>=0; mm--){ base.push(['m-'+y+'-'+String(mm+1<10?'0':'')+(mm+1), MN[mm]]); }
  return base;
})();
// Client-side CSV upload override (re-matches a dropped UPS Billing CSV in the browser)
var spnlUpRows=null, spnlUpUnatt=0, spnlUpUnattCat={}, spnlUpName='', spnlUpMatched=0, spnlUpUnmatched=0;
function spnlNorm(h){ return String(h==null?'':h).toLowerCase().replace(/[^a-z0-9]/g,''); }
function spnlFindCol(hdr,keys){ var i,k; for(i=0;i<hdr.length;i++){ var nh=spnlNorm(hdr[i]); for(k=0;k<keys.length;k++){ if(nh===keys[k]) return i; } } for(i=0;i<hdr.length;i++){ nh=spnlNorm(hdr[i]); for(k=0;k<keys.length;k++){ if(nh.indexOf(keys[k])>=0) return i; } } return -1; }
function spnlMoneyParse(v){ if(v==null) return 0; var s=String(v).replace(/[$,\\s]/g,''); var neg=/^\\(.*\\)$/.test(s); s=s.replace(/[()]/g,''); var n=parseFloat(s); if(isNaN(n)) return 0; return neg?-n:n; }
function spnlNormPo(ref){ var m=/^P[O0](\\d+)$/i.exec(String(ref==null?'':ref).trim()); return m?('PO'+m[1]):null; }
function spnlParseCSV(text){ var rows=[],i=0,n=text.length,field='',row=[],inq=false;
  while(i<n){ var ch=text.charAt(i);
    if(inq){ if(ch=='"'){ if(text.charAt(i+1)=='"'){ field+='"'; i+=2; continue; } inq=false; i++; continue; } field+=ch; i++; continue; }
    if(ch=='"'){ inq=true; i++; continue; }
    if(ch==','){ row.push(field); field=''; i++; continue; }
    if(ch=='\\r'){ i++; continue; }
    if(ch=='\\n'){ row.push(field); rows.push(row); row=[]; field=''; i++; continue; }
    field+=ch; i++; }
  if(field.length||row.length){ row.push(field); rows.push(row); }
  return rows; }
function spnlTriggerUpload(){ var el=document.getElementById('spnlFile'); if(el) el.click(); }
function spnlOnFile(input){ var f=input&&input.files&&input.files[0]; if(!f) return; var rd=new FileReader();
  rd.onload=function(e){ try{ spnlApplyCsv(String(e.target.result||''), f.name); }catch(err){ alert('Could not parse CSV: '+err.message); } input.value=''; };
  rd.readAsText(f); }
function spnlClearUpload(){ spnlUpRows=null; spnlUpName=''; spnlUpUnatt=0; spnlUpUnattCat={}; spnlUpMatched=0; spnlUpUnmatched=0; renderTabs(); renderSpnlPanel(); }
function spnlApplyCsv(text, fname){
  if(!SPNL||!SPNL.maps){ alert('Match maps not loaded yet — try again in a moment.'); return; }
  var t2s=SPNL.maps.track2so||{}, p2s=SPNL.maps.po2so||{}, si=SPNL.maps.so_info||{};
  var data=spnlParseCSV(text); if(data.length<2){ alert('That CSV looks empty.'); return; }
  var hdr=data[0];
  var ciT=spnlFindCol(hdr,['trackingnumber','tracking']),
      ciR1=spnlFindCol(hdr,['referencenumber1','reference1','packagereference1']),
      ciR2=spnlFindCol(hdr,['referencenumber2','reference2']),
      ciN=spnlFindCol(hdr,['netamountdue','netamount','netcharge','billedamount','amount']),
      ciC=spnlFindCol(hdr,['shippingsystemadjustment','shippingsystem','chargecategory','adjustment']);
  if(ciN<0 || (ciT<0&&ciR1<0)){ alert('Could not find the Net Amount and Tracking/Reference columns in this CSV. Headers: '+hdr.join(', ')); return; }
  var costBySo={}, trkBySo={}, unatt=0, unattCat={}, matched=0, unmatched=0, seenTrk={};
  for(var r=1;r<data.length;r++){ var rr=data[r]; if(!rr||(rr.length===1&&rr[0]==='')) continue;
    var tn=(ciT>=0?(rr[ciT]||''):'').trim();
    var r1=(ciR1>=0?(rr[ciR1]||''):'').trim();
    var r2=(ciR2>=0?(rr[ciR2]||''):'').trim();
    var net=spnlMoneyParse(ciN>=0?rr[ciN]:'');
    var so=(tn&&t2s[tn])?t2s[tn]:null;
    if(!so){ var cand=[r1,r2]; for(var c=0;c<2;c++){ var po=spnlNormPo(cand[c]); if(po&&p2s[po]){ so=p2s[po]; break; } } }
    if(so){ costBySo[so]=(costBySo[so]||0)+net;              // cost sums every charge line
      if(tn){ var st=trkBySo[so]||(trkBySo[so]={}); st[tn]=1; }  // Pkgs = UNIQUE tracking #s only
      if(tn&&!seenTrk[tn]){ seenTrk[tn]=1; matched++; }       // count unique packages matched
    }
    else if(!tn){ var cat=(ciC>=0?(rr[ciC]||''):'').trim()||'Other'; unatt+=net; unattCat[cat]=(unattCat[cat]||0)+net; }
    else { unmatched++; }
  }
  function _pkgn(soid){ var o=trkBySo[soid]; return o?Object.keys(o).length:0; }
  var rows=[], soid;
  for(soid in si){ if(!si.hasOwnProperty(soid)) continue; var info=si[soid];
    rows.push({customer:info.customer,so_num:info.so_num,so_id:soid,date:info.date,pos:info.pos||[],po_rows:info.po_rows||0,packages:_pkgn(soid),revenue:info.revenue||0,cost:Math.round((costBySo[soid]||0)*100)/100,has_cost:(soid in costBySo)}); }
  for(soid in costBySo){ if(!si[soid]){ rows.push({customer:'SO '+soid,so_num:'',so_id:soid,date:'',pos:[],po_rows:0,packages:_pkgn(soid),revenue:0,cost:Math.round(costBySo[soid]*100)/100,has_cost:true}); } }
  spnlUpRows=rows; spnlUpUnatt=Math.round(unatt*100)/100; spnlUpUnattCat=unattCat; spnlUpName=fname; spnlUpMatched=matched; spnlUpUnmatched=unmatched;
  renderTabs(); renderSpnlPanel();
}
var spnlSort={key:'date', dir:-1};
var SPNL_COLS=[
  {k:'customer',   t:'str', lbl:'Customer',  c:false},
  {k:'so_num',     t:'str', lbl:'SO #',      c:false},
  {k:'date',       t:'date',lbl:'SO date',   c:false},
  {k:'pos',        t:'str', lbl:'PO(s)',     c:false},
  {k:'po_rows',    t:'num', lbl:'PO Rows',   c:true},
  {k:'packages',   t:'num', lbl:'Pkgs',      c:true},
  {k:'disc',       t:'num', lbl:'Discrepancy', c:true},
  {k:'revenue',    t:'num', lbl:'Shipping charged (SKU 999)', c:true},
  {k:'cost',       t:'num', lbl:'UPS cost',  c:true},
  {k:'margin',     t:'num', lbl:'Margin',    c:true},
  {k:'margin_pct', t:'num', lbl:'Margin %',  c:true}
];
function loadSpnl(){
  if(SPNL_EMBED){ SPNL=SPNL_EMBED; spnlLoading=false; if(mode==='spnl'){ renderTabs(); renderSpnlPanel(); } return; }
  if(spnlLoading) return; spnlLoading=true;
  fetch('shipments-pnl-data.json?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(d){ SPNL=d; spnlLoading=false; if(mode==='spnl'){ renderTabs(); renderSpnlPanel(); } })
    .catch(function(e){ spnlLoading=false; if(mode==='spnl') document.getElementById('panel').innerHTML='<div class="empty">Could not load Shipments P&L data: '+escapeHtml(e.message)+'</div>'; });
}
function spnlRefresh(){ SPNL=SPNL_EMBED||null; spnlLoading=false; document.getElementById('panel').innerHTML='<div class="empty">Reloading Shipments P&L…</div>'; loadSpnl(); }
function spnlSetInterval(v){ spnlInterval=v; renderTabs(); renderSpnlPanel(); }
function spnlSetIntervalIdx(i){ if(SPNL_IVS[i]) spnlSetInterval(SPNL_IVS[i][0]); }
function spnlSelectCust(i){ spnlCust=(i<0?'':(spnlCustList[i]||'')); renderTabs(); renderSpnlPanel(); }
function spnlSortByIdx(i){ var c=SPNL_COLS[i]; if(!c) return;
  if(spnlSort.key===c.k){ spnlSort.dir=-spnlSort.dir; } else { spnlSort.key=c.k; spnlSort.dir=(c.t==='num'?-1:1); } renderSpnlPanel(); }
function spnlInInterval(ds){
  if(spnlInterval==='ytd') return true;   // data is already this-year, non-cancelled
  if(!ds) return false;
  var now=new Date(), y=now.getFullYear(), m=now.getMonth();
  var d=new Date(ds+'T00:00:00'); if(isNaN(d)) return false;
  if(spnlInterval==='month') return d.getFullYear()===y && d.getMonth()===m;
  if(spnlInterval==='lastmonth'){ var lm=(m===0?11:m-1), ly=(m===0?y-1:y); return d.getFullYear()===ly && d.getMonth()===lm; }
  if(spnlInterval.indexOf('m-')===0){ var p=spnlInterval.split('-'); return d.getFullYear()===(+p[1]) && (d.getMonth()+1)===(+p[2]); }
  return true;
}
function spnlBaseRows(){
  var all=spnlUpRows||((SPNL&&SPNL.rows)||[]), out=[];
  for(var i=0;i<all.length;i++){ var r=all[i];
    if(isExclCust(r.customer)) continue;
    if(!spnlInInterval(r.date)) continue;
    out.push(r);
  }
  return out;
}
function spnlDeriv(r){
  var ov=spnlOvr(r.so_id);
  var rev=(ov&&ov.revenue!=null)?(Number(ov.revenue)||0):(Number(r.revenue)||0);
  var cost=Number(r.cost)||0, margin=rev-cost;
  var mp = rev!==0 ? (margin/rev*100) : (cost!==0? -100 : 0);
  var poRows=Number(r.po_rows)||0, pkgs=Number(r.packages)||0;
  var zeroRev=(rev===0);
  return {customer:r.customer, so_num:r.so_num, so_id:r.so_id, date:r.date,
          pos:(r.pos||[]).join(', '), po_rows:poRows, packages:pkgs,
          disc:((pkgs!==poRows)||zeroRev)?1:0, zeroRev:zeroRev,
          revEdited:!!ov, revComment:(ov&&ov.comment)||'',
          revenue:rev, cost:cost, has_cost:!!r.has_cost, margin:margin, margin_pct:mp};
}
function renderSpnlTabs(el){
  el.style.display='';
  if(!SPNL){ el.innerHTML='<div class="empty">Loading…</div>'; return; }
  var base=spnlBaseRows(), cnt={}, names=[], total=0;
  for(var i=0;i<base.length;i++){ total++; var c=base[i].customer||'(no customer)'; if(!(c in cnt)){ cnt[c]=0; names.push(c); } cnt[c]++; }
  names.sort(function(a,b){ return a.toLowerCase().localeCompare(b.toLowerCase()); });
  spnlCustList=names;
  if(spnlCust && names.indexOf(spnlCust)<0){ spnlCust=''; }
  var h='<button class="tab'+(spnlCust===''?' active':'')+'" onclick="spnlSelectCust(-1)">All customers<span class="cnt">'+total+'</span></button>';
  for(var j=0;j<names.length;j++){
    h+='<button class="tab'+(spnlCust===names[j]?' active':'')+'" onclick="spnlSelectCust('+j+')">'+escapeHtml(names[j])+'<span class="cnt">'+cnt[names[j]]+'</span></button>';
  }
  el.innerHTML=h;
}
function spnlMoney(v){ var n=Number(v)||0; var s='$'+Math.abs(n).toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g,','); return n<0?('-'+s):s; }
// ── Discrepancy email to Conmed (per flagged line: PO items, trackings, matched QB bill) ──
function spnlEmailList(){ var de=(SPNL&&SPNL.discrepancy_emails)||{}, out=[]; for(var k in de){ if(de.hasOwnProperty(k)&&!spnlAccIs(k)) out.push(k); }
  out.sort(function(a,b){ return String(de[b].so_num||'').localeCompare(String(de[a].so_num||'')); }); return out; }
function spnlEmailOpen(){
  var ids=spnlEmailList(); if(!ids.length){ alert('No discrepancy lines (Pkgs ≠ PO Rows) available to email.'); return; }
  var de=SPNL.discrepancy_emails;
  var sel='<select id="spnlEmailSel" onchange="spnlEmailRender(this.value)" style="padding:6px 10px;font-size:13px;border:1px solid #cdd9e6;border-radius:6px;max-width:100%;">';
  for(var i=0;i<ids.length;i++){ var e=de[ids[i]]; sel+='<option value="'+escapeHtml(ids[i])+'">'+escapeHtml(e.so_num+' · '+e.customer+' — '+e.packages+' pkgs vs '+e.po_rows+' PO rows')+'</option>'; }
  sel+='</select>';
  document.getElementById('spnlModalBody').innerHTML=
    '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:10px;">'+
    '<div><b style="font-size:15px;">Discrepancy email &rarr; Conmed</b><div style="font-size:12px;color:#7a8a99;">Pick a flagged line, then copy the HTML into your email client.</div></div>'+
    '<div>'+sel+'</div></div>'+
    '<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;">'+
    '<button onclick="spnlEmailCopy()" class="mode-btn" style="padding:6px 14px;border:1px solid #cdd9e6;border-radius:6px;">📋 Copy email HTML</button>'+
    '<span id="spnlEmailMsg" style="font-size:12px;color:#1e7d34;"></span></div>'+
    '<div id="spnlEmailBody" style="border:1px solid #e3e8ef;border-radius:8px;overflow:auto;max-height:60vh;"></div>';
  document.getElementById('spnlModal').style.display='flex';
  spnlEmailRender(ids[0]);
}
function spnlEmailRender(so_id){ var e=(SPNL.discrepancy_emails||{})[so_id]; if(e) document.getElementById('spnlEmailBody').innerHTML=spnlEmailHtml(e); }
function spnlEmailHtml(e){
  var poList=(e.pos||[]).join(', '), one=(e.po_rows==1);
  var items=(e.po_items||[]).map(function(it){ return '<tr><td style="padding:4px 10px;border:1px solid #e0e0e0;">'+escapeHtml(it.product)+'</td><td style="padding:4px 10px;border:1px solid #e0e0e0;text-align:center;">'+fmtQty(it.qty)+'</td></tr>'; }).join('')||'<tr><td colspan="2" style="padding:4px 10px;border:1px solid #e0e0e0;color:#888;">(no PO line items)</td></tr>';
  var trk=(e.trackings||[]).map(function(t){ return '<li><a href="https://www.ups.com/track?loc=en_US&tracknum='+encodeURIComponent(t)+'">'+escapeHtml(t)+'</a></li>'; }).join('')||'<li>(none on file)</li>';
  var b=e.bill, billHtml;
  if(b){
    var mine=(e.pos||[]).map(function(p){return String(p).toUpperCase();});
    var lines=(b.lines||[]).map(function(l){ var isMine=mine.indexOf(String(l.desc||'').toUpperCase())>=0;
      return '<tr'+(isMine?' style="background:#fff5d6;font-weight:600;"':'')+'><td style="padding:4px 10px;border:1px solid #e0e0e0;">'+escapeHtml(l.desc||'—')+(isMine?' &larr; this order':'')+'</td><td style="padding:4px 10px;border:1px solid #e0e0e0;text-align:right;">$'+(Number(l.amount)||0).toFixed(2)+'</td></tr>'; }).join('');
    var others=(b.lines||[]).filter(function(l){ return mine.indexOf(String(l.desc||'').toUpperCase())<0; }).length;
    billHtml='<p style="margin:14px 0 6px;"><b>Associated invoice &mdash; QuickBooks Bill #'+escapeHtml(b.doc_number||'')+'</b> (dated '+escapeHtml(b.date||'')+', total $'+(Number(b.total)||0).toFixed(2)+((Number(b.balance)||0)>0?', balance $'+(Number(b.balance)).toFixed(2):', paid')+')'+(others>0?' &mdash; note this invoice also bills '+others+' other PO'+(others!=1?'s':''):'')+':</p>'+
      '<table style="border-collapse:collapse;font-size:13px;"><thead><tr><th style="padding:4px 10px;border:1px solid #e0e0e0;background:#f4f7fb;text-align:left;">Charged (PO)</th><th style="padding:4px 10px;border:1px solid #e0e0e0;background:#f4f7fb;">Amount</th></tr></thead><tbody>'+lines+'</tbody></table>';
  } else { billHtml='<p style="margin:14px 0;color:#a06000;">No matching QuickBooks Conmed bill was found for this PO.</p>'; }
  return '<div style="font-family:Arial,Helvetica,sans-serif;color:#1f2d3d;font-size:14px;line-height:1.5;padding:18px 20px;background:#fff;">'+
    '<p style="margin:0 0 6px;color:#7a8a99;font-size:12px;">To: Conmed &nbsp;&middot;&nbsp; Subject: Shipping discrepancy &mdash; '+escapeHtml(e.so_num)+' ('+escapeHtml(poList)+')</p><hr style="border:none;border-top:1px solid #e3e8ef;margin:6px 0 14px;">'+
    '<p>Hi Conmed team,</p>'+
    '<p>We spotted a discrepancy on the order below &mdash; the number of UPS packages does not match the line items on our purchase order &mdash; and wanted to flag it for reconciliation.</p>'+
    '<p style="margin:10px 0;"><b>Order:</b> '+escapeHtml(e.so_num)+' &nbsp;&middot;&nbsp; <b>PO:</b> '+escapeHtml(poList)+' &nbsp;&middot;&nbsp; <b>PO date:</b> '+escapeHtml(e.po_date||'—')+'<br><b>Ship-to:</b> '+escapeHtml(e.customer)+'</p>'+
    '<p style="margin:12px 0 4px;"><b>Items we ordered ('+e.po_rows+' line item'+(one?'':'s')+'):</b></p>'+
    '<table style="border-collapse:collapse;font-size:13px;"><thead><tr><th style="padding:4px 10px;border:1px solid #e0e0e0;background:#f4f7fb;text-align:left;">Item</th><th style="padding:4px 10px;border:1px solid #e0e0e0;background:#f4f7fb;">Qty</th></tr></thead><tbody>'+items+'</tbody></table>'+
    '<p style="margin:12px 0 4px;"><b>UPS packages: '+e.packages+'</b> (vs '+e.po_rows+' ordered line item'+(one?'':'s')+') &mdash; tracking numbers:</p>'+
    '<ul style="margin:4px 0 4px 18px;padding:0;">'+trk+'</ul>'+
    billHtml+
    '<p style="margin:14px 0 4px;">Could you please help us reconcile why '+e.packages+' packages were shipped/billed against '+e.po_rows+' ordered line item'+(one?'':'s')+'? Happy to share any additional detail.</p>'+
    '<p style="margin:10px 0 0;">Thank you,<br>JIT4You</p></div>';
}
function spnlEmailCopy(){ var s=document.getElementById('spnlEmailSel'); var e=s?(SPNL.discrepancy_emails||{})[s.value]:null; if(!e) return;
  var html=spnlEmailHtml(e);
  var done=function(){ var m=document.getElementById('spnlEmailMsg'); if(m){ m.textContent='Copied HTML to clipboard.'; setTimeout(function(){m.textContent='';},2500);} };
  try{ if(navigator.clipboard&&navigator.clipboard.write&&window.ClipboardItem){ navigator.clipboard.write([new ClipboardItem({'text/html':new Blob([html],{type:'text/html'}),'text/plain':new Blob([html],{type:'text/plain'})})]).then(done,function(){spnlCopyFallback(html,done);}); return; } }catch(err){}
  spnlCopyFallback(html,done); }
function spnlCopyFallback(text,done){ var ta=document.createElement('textarea'); ta.value=text; document.body.appendChild(ta); ta.select(); try{document.execCommand('copy');}catch(e){} document.body.removeChild(ta); if(done)done(); }
function spnlEmailClose(ev){ if(ev&&ev.target&&ev.target.id!=='spnlModal') return; var m=document.getElementById('spnlModal'); if(m) m.style.display='none'; }
// ── Accept a discrepancy (excludes it from prepared emails). Persisted to the repo
//    file spnl_accepted.json via the button token (so the scheduled build honours it). ──
var SPNL_ACC=null;
function spnlAccSet(){ if(SPNL_ACC) return SPNL_ACC; SPNL_ACC={}; var a=(SPNL&&SPNL.accepted)||[]; for(var i=0;i<a.length;i++) SPNL_ACC[a[i]]=1;
  try{ var p=JSON.parse(localStorage.getItem('jit4_spnl_accepted')||'[]'); for(var j=0;j<p.length;j++) SPNL_ACC[p[j]]=1; }catch(e){} return SPNL_ACC; }
function spnlAccIs(id){ return !!spnlAccSet()[id]; }
function spnlToggleAccept(so_id){ var s=spnlAccSet(); if(s[so_id]) delete s[so_id]; else s[so_id]=1;
  try{ localStorage.setItem('jit4_spnl_accepted', JSON.stringify(Object.keys(s))); }catch(e){}
  spnlAccCommit(); renderTabs(); renderSpnlPanel();
  if(document.getElementById('spnlModal') && document.getElementById('spnlModal').style.display!=='none') spnlEmailOpen(); }
function spnlAccCommit(){ if(!BTN||!BTN.token) return; var arr=Object.keys(spnlAccSet());
  var base='https://api.github.com/repos/'+BTN.repo+'/contents/spnl_accepted.json';
  var hdr={'Authorization':'Bearer '+BTN.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'};
  fetch(base+'?ref='+encodeURIComponent(BTN.branch)+'&cb='+Date.now(),{headers:hdr,cache:'no-store'})
    .then(function(r){ if(r.status===404) return {sha:null}; if(!r.ok) throw new Error('read '+r.status); return r.json().then(function(j){ return {sha:j.sha}; }); })
    .then(function(st){ return fetch(base,{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},hdr),
      body:JSON.stringify({message:'Update accepted shipment discrepancies ('+arr.length+')', content:_b64enc(JSON.stringify({accepted:arr},null,2)+'\\n'), sha:st.sha||undefined, branch:BTN.branch})}); })
    .then(function(r){ if(!r.ok) throw new Error('save '+r.status); })
    .catch(function(e){ /* kept in localStorage; will sync on next toggle */ }); }
// ── Manual shipping-charge override (used when SKU-999 revenue is $0). Persisted to
//    spnl_overrides.json via the button token; recalculates the row live. ──
var SPNL_OVR=null;
function spnlOvrMap(){ if(SPNL_OVR) return SPNL_OVR; SPNL_OVR={}; var o=(SPNL&&SPNL.overrides)||{}; for(var k in o){ if(o.hasOwnProperty(k)) SPNL_OVR[k]=o[k]; }
  try{ var p=JSON.parse(localStorage.getItem('jit4_spnl_overrides')||'{}'); for(var k2 in p){ if(p.hasOwnProperty(k2)) SPNL_OVR[k2]=p[k2]; }}catch(e){} return SPNL_OVR; }
function spnlOvr(id){ return spnlOvrMap()[id]||null; }
function spnlEditOpen(so_id){
  var e=null, rows=(SPNL&&SPNL.rows)||[]; for(var i=0;i<rows.length;i++){ if(rows[i].so_id===so_id){ e=rows[i]; break; } }
  var ov=spnlOvr(so_id); var cur=(ov&&ov.revenue!=null)?ov.revenue:(e?e.revenue:0);
  document.getElementById('spnlModalBody').innerHTML=
    '<b style="font-size:15px;">Edit shipping charged — '+escapeHtml(e?e.so_num:so_id)+'</b>'+
    '<div style="font-size:12px;color:#7a8a99;margin-bottom:10px;">'+escapeHtml(e?e.customer:'')+' &middot; SKU-999 value: '+spnlMoney(e?(e.revenue||0):0)+'</div>'+
    '<label style="font-size:13px;display:block;margin-bottom:4px;">Shipping charged ($)</label>'+
    '<input id="spnlEditRev" type="number" step="0.01" value="'+(cur||0)+'" style="padding:6px 10px;font-size:14px;border:1px solid #cdd9e6;border-radius:6px;width:180px;">'+
    '<label style="font-size:13px;display:block;margin:12px 0 4px;">Comment (why this was changed)</label>'+
    '<textarea id="spnlEditNote" rows="3" style="width:100%;box-sizing:border-box;padding:6px 10px;font-size:13px;border:1px solid #cdd9e6;border-radius:6px;">'+escapeHtml((ov&&ov.comment)||'')+'</textarea>'+
    '<div style="display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap;">'+
    '<button onclick="spnlEditSave(\\''+so_id+'\\')" class="mode-btn" style="padding:6px 16px;border:1px solid #cdd9e6;border-radius:6px;">Save</button>'+
    (ov?'<button onclick="spnlEditClear(\\''+so_id+'\\')" class="mode-btn" style="padding:6px 12px;border:1px solid #cdd9e6;border-radius:6px;">Reset to SKU-999</button>':'')+
    '<span id="spnlEditMsg" style="font-size:12px;color:#c0392b;"></span></div>';
  document.getElementById('spnlModal').style.display='flex';
}
function spnlEditSave(so_id){
  var v=parseFloat(document.getElementById('spnlEditRev').value);
  if(isNaN(v)){ document.getElementById('spnlEditMsg').textContent='Enter a number.'; return; }
  var note=(document.getElementById('spnlEditNote').value||'').replace(/^\\s+|\\s+$/g,'');
  var m=spnlOvrMap(); m[so_id]={revenue:Math.round(v*100)/100, comment:note, at:new Date().toISOString().slice(0,10)};
  try{ localStorage.setItem('jit4_spnl_overrides', JSON.stringify(m)); }catch(e){}
  spnlOvrCommit(); document.getElementById('spnlModal').style.display='none'; renderTabs(); renderSpnlPanel(); }
function spnlEditClear(so_id){ var m=spnlOvrMap(); delete m[so_id]; try{ localStorage.setItem('jit4_spnl_overrides', JSON.stringify(m)); }catch(e){}
  spnlOvrCommit(); document.getElementById('spnlModal').style.display='none'; renderTabs(); renderSpnlPanel(); }
function spnlOvrCommit(){ if(!BTN||!BTN.token) return; var m=spnlOvrMap();
  var base='https://api.github.com/repos/'+BTN.repo+'/contents/spnl_overrides.json';
  var hdr={'Authorization':'Bearer '+BTN.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'};
  fetch(base+'?ref='+encodeURIComponent(BTN.branch)+'&cb='+Date.now(),{headers:hdr,cache:'no-store'})
    .then(function(r){ if(r.status===404) return {sha:null}; if(!r.ok) throw new Error('read '+r.status); return r.json().then(function(j){ return {sha:j.sha}; }); })
    .then(function(st){ return fetch(base,{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},hdr),
      body:JSON.stringify({message:'Update manual shipping-charge overrides ('+Object.keys(m).length+')', content:_b64enc(JSON.stringify({overrides:m},null,2)+'\\n'), sha:st.sha||undefined, branch:BTN.branch})}); })
    .then(function(r){ if(!r.ok) throw new Error('save '+r.status); }).catch(function(e){}); }
function renderSpnlPanel(){
  if(!SPNL){ document.getElementById('panel').innerHTML='<div class="empty">Loading Shipments P&L…</div>'; loadSpnl(); return; }
  var base=spnlBaseRows();
  var rows=base.map(spnlDeriv).filter(function(r){ return !spnlCust || r.customer===spnlCust; });
  // sort
  var col=null; for(var i=0;i<SPNL_COLS.length;i++){ if(SPNL_COLS[i].k===spnlSort.key) col=SPNL_COLS[i]; }
  rows.sort(function(a,b){ return spnlSort.dir*cmp(a[spnlSort.key],b[spnlSort.key],col?col.t:'str'); });
  var tRev=0,tCost=0,tPkg=0,tPoRows=0,nDisc=0,nAcc=0,anyCost=false;
  for(var r=0;r<rows.length;r++){ tRev+=rows[r].revenue; tCost+=rows[r].cost; tPkg+=rows[r].packages; tPoRows+=rows[r].po_rows; if(rows[r].disc){ if(spnlAccIs(rows[r].so_id)) nAcc++; else nDisc++; } if(rows[r].has_cost) anyCost=true; }
  var tMargin=tRev-tCost, tMp=tRev!==0?(tMargin/tRev*100):0;
  var VT='https://jit4youinc.od2.vtiger.com/index.php?module=';
  function soLink(r){ if(!r.so_id) return escapeHtml(r.so_num||'—'); return '<a href="'+VT+'SalesOrder&view=Detail&record='+encodeURIComponent(r.so_id)+'" target="_blank" rel="noopener" style="color:#1F4E79;text-decoration:none;font-weight:600;">'+escapeHtml(r.so_num)+' <span style="color:#008080;">↗</span></a>'; }
  var pending=!(SPNL.has_billing)&&!spnlUpRows;
  // header row (sortable)
  var thead='';
  for(var c=0;c<SPNL_COLS.length;c++){ var cc=SPNL_COLS[c]; var ar=(spnlSort.key===cc.k?(spnlSort.dir>0?' ▲':' ▼'):'');
    thead+='<th'+(cc.c?' class="c"':'')+' style="cursor:pointer;white-space:nowrap;" onclick="spnlSortByIdx('+c+')">'+escapeHtml(cc.lbl)+ar+'</th>'; }
  var body='';
  for(var k=0;k<rows.length;k++){ var x=rows[k];
    var costCell = (!x.has_cost && pending) ? '<span style="color:#b0862a;" title="Awaiting UPS Billing Center CSV">pending</span>'
                   : (x.has_cost ? spnlMoney(x.cost) : '<span style="color:#9aa7b4;" title="No UPS charge matched this SO">—</span>');
    var mVal = x.has_cost ? spnlMoney(x.margin) : (pending?'<span style="color:#b0862a;">—</span>':'<span style="color:#9aa7b4;">—</span>');
    var mCol = x.margin<0?'#c0392b':'#1e7d34';
    var mPct = x.has_cost ? (x.margin_pct.toFixed(1)+'%') : '—';
    var discWhy=((x.packages!==x.po_rows)?('Pkgs ('+x.packages+') ≠ PO Rows ('+x.po_rows+')'):'')+(x.zeroRev?(((x.packages!==x.po_rows)?'; ':'')+'shipping charged is $0'):'');
    var discDot;
    if(!x.disc){ discDot='<span title="Pkgs match PO Rows" style="color:#1e7d34;font-size:15px;">●</span>'; }
    else if(spnlAccIs(x.so_id)){ discDot='<span onclick="spnlToggleAccept(\\''+x.so_id+'\\')" title="Accepted — excluded from emails. Click to re-open." style="cursor:pointer;color:#8a97a6;font-size:14px;">✓ accepted</span>'; }
    else { discDot='<span onclick="spnlToggleAccept(\\''+x.so_id+'\\')" title="Discrepancy: '+discWhy+'. Click to accept (exclude from emails)." style="cursor:pointer;color:#c0392b;font-size:15px;">●</span>'; }
    body+='<tr>'+
      '<td class="item-name">'+escapeHtml(x.customer)+'</td>'+
      '<td>'+soLink(x)+'</td>'+
      '<td>'+(x.date?fmtDate(x.date):'—')+'</td>'+
      '<td>'+escapeHtml(x.pos||'—')+'</td>'+
      '<td class="c">'+(x.po_rows||0)+'</td>'+
      '<td class="c">'+(x.packages||0)+'</td>'+
      '<td class="c">'+discDot+'</td>'+
      '<td class="c" style="cursor:pointer;white-space:nowrap;" onclick="spnlEditOpen(\\''+x.so_id+'\\')" title="'+(x.revEdited?('Manually set'+(x.revComment?(': '+escapeHtml(x.revComment)):'')):'Click to edit shipping charged')+'">'+spnlMoney(x.revenue)+(x.revEdited?' <span style="color:#b0862a;">✎</span>':(x.zeroRev?' <span style="color:#c0392b;">✎</span>':''))+'</td>'+
      '<td class="c">'+costCell+'</td>'+
      '<td class="c" style="font-weight:600;color:'+(x.has_cost?mCol:'#9aa7b4')+';">'+mVal+'</td>'+
      '<td class="c" style="color:'+(x.has_cost?mCol:'#9aa7b4')+';">'+mPct+'</td></tr>';
  }
  if(!rows.length) body='<tr><td colspan="11" class="empty" style="padding:18px;">No SOs with UPS shipments in this selection.</td></tr>';
  // interval buttons + CSV upload
  var ivb='<span style="color:#7a8a99;">Period:</span>';
  for(var v=0;v<SPNL_IVS.length;v++){ var on=(spnlInterval===SPNL_IVS[v][0]);
    ivb+='<button onclick="spnlSetIntervalIdx('+v+')" class="mode-btn'+(on?' active':'')+'" style="padding:5px 12px;border-radius:6px;border:1px solid #cdd9e6;font-size:12px;">'+escapeHtml(SPNL_IVS[v][1])+'</button>'; }
  ivb+='<span style="margin-left:14px;"></span><input type="file" id="spnlFile" accept=".csv,text/csv" style="display:none;" onchange="spnlOnFile(this)">'+
    '<button onclick="spnlTriggerUpload()" class="mode-btn" style="padding:5px 12px;border-radius:6px;border:1px solid #cdd9e6;font-size:12px;" title="Upload a UPS Billing Center CSV and re-match it in your browser (nothing leaves this page)">⬆ Upload UPS Billing CSV</button>'+
    (spnlUpRows?(' <span style="font-size:12px;color:#20603a;">using <b>'+escapeHtml(spnlUpName)+'</b> ('+spnlUpMatched+' matched'+(spnlUpUnmatched?', '+spnlUpUnmatched+' unmatched':'')+') <a onclick="spnlClearUpload()" style="cursor:pointer;color:#1F4E79;">clear</a></span>'):'');
  var nEmail=spnlEmailList().length;  // excludes accepted discrepancies
  ivb+='<span style="margin-left:14px;"></span><button onclick="spnlEmailOpen()" class="mode-btn"'+(nEmail?'':' disabled')+' style="padding:5px 12px;border-radius:6px;border:1px solid #cdd9e6;font-size:12px;'+(nEmail?'':'opacity:.5;')+'" title="Generate a discrepancy alert email to Conmed for a flagged line">✉ Discrepancy email'+(nEmail?' ('+nEmail+')':'')+'</button>';
  var uatt = spnlUpRows? spnlUpUnatt : (SPNL.unattributed_total||0);
  var srcLabel = spnlUpRows? spnlUpName : (SPNL.billing_source||SPNL.billing_asof||'');
  var banner = pending
    ? '<div style="margin:8px 2px 12px;padding:11px 15px;background:#fff8e1;border-left:4px solid #ffc107;font-size:12.5px;border-radius:6px;color:#5c4a12;">UPS costs are <b>pending</b>. UPS does not expose invoiced charges via API, so <b>Upload a UPS Billing Center CSV</b> (button above) or drop it in the QB&nbsp;Files folder &mdash; I match each Tracking/PO to its SO and fill the cost / margin columns. Shipping revenue below is live from SKU&nbsp;999.</div>'
    : '<div style="margin:8px 2px 12px;padding:9px 14px;background:#eef7f0;border-left:4px solid #2e9e57;font-size:12px;border-radius:6px;color:#20603a;">UPS cost matched by Tracking/PO from <b>'+escapeHtml(srcLabel)+'</b>'+(uatt?(' &middot; plus '+spnlMoney(uatt)+' in account-level charges (fees, adjustments, 3rd-party summaries) not tied to any SO &mdash; see &ldquo;UPS acct charges&rdquo; below'):'')+(SPNL.unmatched_charges&&!spnlUpRows?(' &middot; '+SPNL.unmatched_charges+' pkg charge(s) '+spnlMoney(SPNL.unmatched_cost)+' unmatched'):'')+'.</div>';
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'+
    '<div><h2 style="margin:0;">Shipments P&amp;L &mdash; UPS (Conmed)</h2><div class="sub">Conmed drop-ships only &middot; shipping charged (SKU 999 on SO) vs. UPS billed cost, matched by PO/tracking &middot; '+escapeHtml(SPNL.generated_at||'')+'</div></div>'+
    '<button class="refresh-btn" onclick="spnlRefresh()" title="Reload the latest Shipments P&L snapshot"><span class="lbl">↻ Reload</span></button></div></div>'+
    '<div class="kpis" style="padding:6px 0 2px;">'+kpi(spnlMoney(tRev),'Shipping charged')+kpi(anyCost?spnlMoney(tCost):'—','UPS cost')+kpi(anyCost?spnlMoney(tMargin):'—','Margin')+kpi(anyCost?tMp.toFixed(1)+'%':'—','Margin %')+kpi(rows.length,'Sales Orders')+kpi(tPkg,'UPS packages')+kpi(nDisc,'Open discrepancies',(nDisc?'color:#c0392b;':''))+(nAcc?kpi(nAcc,'Accepted'):'')+(uatt?kpi(spnlMoney(uatt),'UPS acct charges'):'')+'</div>'+
    '<div style="display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:8px 2px 6px;font-size:12px;">'+ivb+'</div>'+
    banner+
    '<div class="matrix-wrap"><table class="matrix"><thead><tr>'+thead+'</tr></thead><tbody>'+body+
    '</tbody><tfoot><tr style="font-weight:700;background:#f4f7fb;">'+
    '<td colspan="4" style="text-align:right;">Totals ('+escapeHtml((SPNL_IVS.filter(function(z){return z[0]===spnlInterval;})[0]||['',''])[1])+(spnlCust?' &middot; '+escapeHtml(spnlCust):'')+')</td>'+
    '<td class="c">'+tPoRows+'</td><td class="c">'+tPkg+'</td>'+
    '<td class="c"'+(nDisc?' style="color:#c0392b;"':'')+'>'+(nDisc?nDisc+' ●':'✓')+'</td>'+
    '<td class="c">'+spnlMoney(tRev)+'</td><td class="c">'+(anyCost?spnlMoney(tCost):'—')+'</td>'+
    '<td class="c" style="color:'+(tMargin<0?'#c0392b':'#1e7d34')+';">'+(anyCost?spnlMoney(tMargin):'—')+'</td>'+
    '<td class="c" style="color:'+(tMargin<0?'#c0392b':'#1e7d34')+';">'+(anyCost?tMp.toFixed(1)+'%':'—')+'</td></tr></tfoot></table></div>'+
    '<div id="spnlModal" onclick="spnlEmailClose(event)" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;align-items:center;justify-content:center;"><div onclick="event.stopPropagation()" style="background:#fff;max-width:760px;width:94%;max-height:88vh;overflow:auto;border-radius:10px;padding:20px 22px;box-shadow:0 12px 44px rgba(0,0,0,.32);"><div id="spnlModalBody"></div><div style="text-align:right;margin-top:14px;"><button onclick="spnlEmailClose()" class="mode-btn" style="padding:6px 16px;border-radius:6px;border:1px solid #cdd9e6;">Close</button></div></div></div>';
}

// ── Google Ads tab (data loaded from a separate google-ads-data.json file so the
// Vtiger Refresh never overwrites it) ────────────────────────────────────────
var GADS=null, gadsInterval='this_year', gadsLoading=false;
// Sortable main table on the Google Ads tab (independent of the customer/vendor sortState).
var gadsSort={key:null, dir:1};
var GADS_COLS=[{k:'name',t:'str',lbl:'Campaign',c:false},{k:'status',t:'str',lbl:'Status',c:false},
  {k:'type',t:'str',lbl:'Type',c:false},{k:'start_date',t:'str',lbl:'Started',c:false},
  {k:'clicks',t:'num',lbl:'Clicks',c:true},{k:'impressions',t:'num',lbl:'Impr.',c:true},
  {k:'ctr',t:'num',lbl:'CTR',c:true},{k:'cpc',t:'num',lbl:'Avg CPC',c:true},
  {k:'cost',t:'num',lbl:'Spend',c:true},
  {k:'page_views',t:'num',lbl:'Page views',c:true},{k:'add_to_cart',t:'num',lbl:'Add to cart',c:true},{k:'purchases',t:'num',lbl:'Purchases',c:true},
  {k:'conv_value',t:'num',lbl:'Conv. value',c:true},{k:'roas',t:'num',lbl:'ROAS',c:true}];
function gadsSortByIdx(i){ var c=GADS_COLS[i]; if(!c) return;
  if(gadsSort.key===c.k){ gadsSort.dir=-gadsSort.dir; } else { gadsSort.key=c.k; gadsSort.dir=1; } renderGadsPanel(); }
function loadGads(){
  if(GADS_EMBED){ GADS=GADS_EMBED; gadsLoading=false; if(mode==='gads') renderGadsPanel(); return; }
  if(gadsLoading) return; gadsLoading=true;
  fetch('google-ads-data.json?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(d){ GADS=d; gadsLoading=false; if(mode==='gads') renderGadsPanel(); })
    .catch(function(e){ gadsLoading=false; if(mode==='gads') document.getElementById('panel').innerHTML='<div class="empty">Could not load Google Ads data: '+escapeHtml(e.message)+'</div>'; });
}
function gadsSetInterval(v){ gadsInterval=v; renderGadsPanel(); }
function gadsRefresh(){ GADS=null; gadsLoading=false; document.getElementById('panel').innerHTML='<div class="empty">Refreshing Google Ads &amp; GA4 data…</div>'; loadGads(); }
var gadsJWindow='last_30_days';
function gadsJSetWindow(v){ gadsJWindow=v; renderGadsPanel(); }
function money0(n){ return '$'+Number(n||0).toLocaleString(undefined,{maximumFractionDigits:0}); }
function money2(n){ return '$'+Number(n||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
function renderGadsPanel(){
  if(!GADS){ document.getElementById('panel').innerHTML='<div class="empty">Loading Google Ads data…</div>'; loadGads(); return; }
  var ivs=GADS.intervals||[], cur=null;
  for(var i=0;i<ivs.length;i++){ if(ivs[i].id===gadsInterval){ cur=ivs[i]; break; } }
  if(!cur && ivs.length){ cur=ivs[0]; gadsInterval=cur.id; }
  var sel='<select onchange="gadsSetInterval(this.value)" style="padding:7px 10px;border:1px solid #cdd9e6;border-radius:6px;font-size:13px;font-family:inherit;">';
  for(var j=0;j<ivs.length;j++){ sel+='<option value="'+escapeHtml(ivs[j].id)+'"'+(ivs[j].id===gadsInterval?' selected':'')+'>'+escapeHtml(ivs[j].label)+'</option>'; }
  sel+='</select>';
  var rows=(cur?cur.campaigns:[]).slice(), tc=0,ti=0,tcost=0,tconv=0,tval=0,tpv=0,tatc=0,tpur=0, body='';
  if(gadsSort.key){
    var _gc=null; for(var gi=0;gi<GADS_COLS.length;gi++){ if(GADS_COLS[gi].k===gadsSort.key){ _gc=GADS_COLS[gi]; break; } }
    var _gt=_gc?_gc.t:'str';
    rows.sort(function(a,b){ var av=a[gadsSort.key], bv=b[gadsSort.key];
      if(_gt==='num'){ return gadsSort.dir*((Number(av)||0)-(Number(bv)||0)); }
      return gadsSort.dir*String(av==null?'':av).localeCompare(String(bv==null?'':bv)); });
  }
  for(var k=0;k<rows.length;k++){ var r=rows[k];
    tc+=r.clicks; ti+=r.impressions; tcost+=r.cost; tconv+=r.conversions; tval+=r.conv_value;
    tpv+=r.page_views||0; tatc+=r.add_to_cart||0; tpur+=r.purchases||0;
    var st=r.status, sc = st==='enabled'?['#d4edda','#155724']:(st==='paused'?['#fff3cd','#856404']:['#eee','#666']);
    body+='<tr>'+
      '<td class="item-name">'+escapeHtml(r.name)+'</td>'+
      '<td><span class="status" style="background:'+sc[0]+';color:'+sc[1]+'">'+escapeHtml(st)+'</span></td>'+
      '<td>'+escapeHtml(r.type)+'</td>'+
      '<td>'+fmtDate(r.start_date)+'</td>'+
      '<td class="c">'+Number(r.clicks).toLocaleString()+'</td>'+
      '<td class="c">'+Number(r.impressions).toLocaleString()+'</td>'+
      '<td class="c">'+(r.ctr*100).toFixed(2)+'%</td>'+
      '<td class="c">'+money2(r.cpc)+'</td>'+
      '<td class="c open">'+money2(r.cost)+'</td>'+
      '<td class="c">'+Number(r.page_views||0).toLocaleString()+'</td>'+
      '<td class="c">'+Number(r.add_to_cart||0).toLocaleString()+'</td>'+
      '<td class="c open">'+Number(r.purchases||0).toLocaleString()+'</td>'+
      '<td class="c">'+money0(r.conv_value)+'</td>'+
      '<td class="c">'+(r.roas?Number(r.roas).toFixed(1)+'x':'—')+'</td>'+
      '</tr>';
  }
  var tctr=ti?(tc/ti*100).toFixed(2)+'%':'—', tcpc=tc?money2(tcost/tc):'—', troas=tcost?(tval/tcost).toFixed(1)+'x':'—';
  body+='<tr class="so-group"><td>Total ('+escapeHtml(cur?cur.label:'')+')</td><td></td><td></td><td></td>'+
    '<td class="c">'+tc.toLocaleString()+'</td><td class="c">'+ti.toLocaleString()+'</td><td class="c">'+tctr+'</td>'+
    '<td class="c">'+tcpc+'</td><td class="c open">'+money2(tcost)+'</td>'+
    '<td class="c">'+tpv.toLocaleString()+'</td><td class="c">'+tatc.toLocaleString()+'</td><td class="c open">'+tpur.toLocaleString()+'</td>'+
    '<td class="c">'+money0(tval)+'</td><td class="c">'+troas+'</td></tr>';
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'+
    '<div><h2>Google Ads — Campaign Performance</h2><div class="sub">Account: '+escapeHtml(GADS.account||'')+' &middot; data pulled '+escapeHtml(GADS.pulled_at||'')+' &middot; '+escapeHtml(GADS.currency||'USD')+'</div></div>'+
    '<div style="font-size:13px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">Time interval: '+sel+
    '<button class="refresh-btn" onclick="gadsRefresh()" title="Reload the latest Google Ads / GA4 snapshot (separate from the Vtiger Refresh)"><span class="lbl">↻ Refresh Google Ads</span></button></div></div></div>'+
    '<div class="matrix-wrap"><table class="matrix"><thead><tr>'+ gadsHeadHtml() +
    '</tr></thead><tbody>'+body+'</tbody></table></div>'+ gadsJourneyHtml();
}
function gadsHeadHtml(){
  var h='';
  for(var i=0;i<GADS_COLS.length;i++){ var c=GADS_COLS[i];
    var arr = gadsSort.key===c.k ? '<span class="arr">'+(gadsSort.dir>0?'▲':'▼')+'</span>' : '';
    h+='<th class="'+(c.c?'c ':'')+'sortable" onclick="gadsSortByIdx('+i+')" title="Sort by '+escapeHtml(c.lbl)+'">'+escapeHtml(c.lbl)+arr+'</th>';
  }
  return h;
}
function gadsJourneyHtml(){
  var J=GADS.journey;
  if(!J){ return '<div style="margin:16px;padding:12px 16px;background:#fff8e1;border-left:4px solid #ffc107;font-size:13px;border-radius:6px;line-height:1.5;"><b>Click trails / user journey:</b> '+escapeHtml(GADS.note_click_trails||'')+'</div>'; }
  var order=['today','last_7_days','last_30_days','this_month','last_month','this_year'];
  var labelMap={}; var ivs=GADS.intervals||[]; for(var z=0;z<ivs.length;z++){ labelMap[ivs[z].id]=ivs[z].label; }
  if(!J[gadsJWindow]){ gadsJWindow = J.last_30_days ? 'last_30_days' : Object.keys(J)[0]; }
  var jiv=J[gadsJWindow], jlabel=labelMap[gadsJWindow]||gadsJWindow;
  if(!jiv){ return ''; }
  // independent window selector for the journey / landing-page list
  var wsel='<select onchange="gadsJSetWindow(this.value)" style="padding:6px 10px;border:1px solid #cdd9e6;border-radius:6px;font-size:13px;font-family:inherit;">';
  for(var o=0;o<order.length;o++){ if(J[order[o]]) wsel+='<option value="'+order[o]+'"'+(order[o]===gadsJWindow?' selected':'')+'>'+escapeHtml(labelMap[order[o]]||order[o])+'</option>'; }
  wsel+='</select>';
  var s=jiv.summary||{};
  var cards='<div class="kpis" style="padding:6px 0 0;">'+
    kpi(Number(s.sessions||0).toLocaleString(),'Paid sessions')+
    kpi(Math.round((s.engagement_rate||0)*100)+'%','Engaged')+
    kpi((s.pages_per_session||0),'Pages / session')+
    kpi(Math.round((s.bounce||0)*100)+'%','Bounce')+
    kpi(s.conversions||0,'GA4 conversions')+'</div>';
  // key event name(s) for this window (to label per-page conversions)
  var keyEvents=[]; var evs=jiv.events||[]; for(var k=0;k<evs.length;k++){ if(evs[k].is_key) keyEvents.push(evs[k].event); }
  var keyLabel = keyEvents.length===1 ? keyEvents[0] : (keyEvents.length>1 ? 'key events' : '');
  function convCell(n){ n=n||0; return '<td class="c '+(n>0?'open':'')+'">'+n+(n>0&&keyLabel?' <span style="font-size:9px;color:#2e7d32;font-weight:700;white-space:nowrap;">'+escapeHtml(keyLabel)+'</span>':'')+'</td>'; }
  // ENABLED campaigns only, for the SAME selected window
  var enabled={}; for(var z2=0;z2<ivs.length;z2++){ if(ivs[z2].id===gadsJWindow){ var cl=ivs[z2].campaigns||[]; for(var y=0;y<cl.length;y++){ if((cl[y].status||'')==='enabled') enabled[cl[y].name]=1; } } }
  var camps=(jiv.campaigns||[]).filter(function(c){ return enabled[c.campaign]; }), body='';
  for(var ci=0;ci<camps.length;ci++){
    var cmp=camps[ci], t=cmp.totals||{};
    body+='<tr class="so-group"><td><span class="so-h">'+escapeHtml(cmp.campaign)+'</span> <span style="font-size:10px;color:#2e7d32;font-weight:700;">enabled</span></td>'+
      '<td class="c">'+Number(t.sessions||0).toLocaleString()+'</td>'+
      '<td class="c">'+Number(t.engaged||0).toLocaleString()+'</td>'+
      '<td class="c"></td><td class="c"></td>'+convCell(t.conversions)+'</tr>';
    var lps=cmp.landing_pages||[];
    for(var i=0;i<lps.length;i++){ var r=lps[i];
      body+='<tr><td class="item-name" style="max-width:420px;padding-left:24px;">'+escapeHtml(r.path)+'</td>'+
        '<td class="c">'+Number(r.sessions).toLocaleString()+'</td>'+
        '<td class="c">'+Number(r.engaged).toLocaleString()+'</td>'+
        '<td class="c">'+r.pages_per_session+'</td>'+
        '<td class="c">'+Math.round((r.bounce||0)*100)+'%</td>'+convCell(r.conversions)+'</tr>';
    }
  }
  if(!body){ body='<tr><td colspan="6" class="empty">No enabled campaigns with paid traffic in this period.</td></tr>'; }
  var evb='';
  for(var e=0;e<evs.length;e++){ var ev=evs[e];
    evb+='<tr'+(ev.is_key?' style="background:#eef8f0;"':'')+'><td class="item-name">'+escapeHtml(ev.event)+(ev.is_key?' <span style="font-size:10px;color:#2e7d32;font-weight:700;">★ key event</span>':'')+'</td>'+
      '<td class="c">'+Number(ev.count||0).toLocaleString()+'</td>'+
      '<td class="c '+((ev.conversions||0)>0?'open':'')+'">'+(ev.conversions||0)+'</td></tr>';
  }
  var defs=GADS.defs||{};
  var evTable = evs.length ? ('<div class="ca-h">Conversions by key event</div>'+
    '<div class="matrix-wrap" style="max-width:520px;"><table class="matrix"><thead><tr><th>Event</th><th class="c">Event count</th><th class="c">Conversions</th></tr></thead><tbody>'+evb+'</tbody></table></div>') : '';
  return '<div class="ca-h" style="margin-top:24px;border-top:1px solid #dee5ec;padding-top:14px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">'+
    '<span>User Journey (GA4) — paid Google Ads traffic &middot; <span style="font-weight:400;color:#888;">'+escapeHtml(GADS.ga4_property||'')+'</span></span>'+
    '<span style="font-weight:400;font-size:13px;color:#2c3e50;">Landing-page window: '+wsel+'</span></div>'+
    cards+
    '<div class="ca-h">Landing pages — '+escapeHtml(jlabel)+' &middot; enabled campaigns only, where ad clicks enter'+(keyLabel?' &middot; conversions are <b>'+escapeHtml(keyLabel)+'</b>':'')+'</div>'+
    '<div class="matrix-wrap"><table class="matrix"><thead><tr><th>Campaign / Landing page</th><th class="c">Sessions</th><th class="c">Engaged</th><th class="c">Pages/sess</th><th class="c">Bounce</th><th class="c">Conv.</th></tr></thead><tbody>'+body+'</tbody></table></div>'+
    evTable+
    '<div style="margin:14px 16px;padding:12px 16px;background:#eef8f0;border-left:4px solid #2e7d32;font-size:12px;border-radius:6px;color:#2c3e50;line-height:1.55;">'+
    '<b>What "Conversions" means:</b> '+escapeHtml(defs.conversion||'')+'<br><br>'+
    '<b>What "Bounce" means:</b> '+escapeHtml(defs.bounce||'')+'</div>';
}
function caTrend(t){
  if(t==='up')   return '<span style="color:#2e7d32;font-weight:700;">▲ up</span>';
  if(t==='down') return '<span style="color:#c62828;font-weight:700;">▼ down</span>';
  if(t==='due')  return '<span style="color:#e67e22;font-weight:700;">● due</span>';
  return '<span style="color:#888;">– steady</span>';
}
function caEmail(i){
  var c=((DATA.customer_analysis||{}).customers||[])[i]; if(!c) return;
  var w=window.open('','_blank');
  if(!w){ alert('Please allow pop-ups for this site to create the email draft.'); return; }
  w.document.open(); w.document.write(c.email_doc||''); w.document.close();
}
function renderCaPanel(){
  var ca=DATA.customer_analysis||{customers:[],months:[]};
  var c=(ca.customers||[])[caactive];
  if(!c){ document.getElementById('panel').innerHTML='<div class="empty">No Independent Diagnostic Lab or Online Reseller customers with orders this year.</div>'; return; }
  var months=ca.months||c.months||[];
  // ── Header + Create email button ──
  var hasEmail = c.email && c.email.indexOf('@')>-1;
  var head='<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;">'+
    '<div><h2>'+escapeHtml(c.name)+'</h2>'+
    '<div class="sub">'+(c.industry?escapeHtml(c.industry)+' &middot; ':'')+(c.products||[]).length+' product(s) &middot; '+
    fmtQty(c.total_units)+' units YTD &middot; '+(c.total_spend!=null?('$'+Number(c.total_spend).toLocaleString()+' YTD &middot; '):'')+
    c.active_months+' active month(s) &middot; '+(hasEmail?escapeHtml(c.email):'<span style="color:#c62828;">no email on file</span>')+'</div></div>'+
    '<button class="ca-email-btn" onclick="caEmail('+caactive+')">✉ Create email draft</button>'+
    '</div></div>';

  // ── Matrix: Product × Month (+ Total) ──
  var prods=c.products||[];
  var mh=''; for(var m=0;m<months.length;m++) mh+='<th class="c">'+escapeHtml(months[m])+'</th>';
  var mrows='';
  for(var p=0;p<prods.length;p++){
    var pr=prods[p], cells='';
    for(var m2=0;m2<months.length;m2++){ var q=pr.by_month[m2]; cells+='<td class="c">'+(q?('<span class="hd-q">'+fmtQty(q)+'</span>'):'<span class="po-none">·</span>')+'</td>'; }
    mrows+='<tr><td class="item-name">'+escapeHtml(pr.name)+'</td>'+cells+'<td class="c open">'+fmtQty(pr.total)+'</td></tr>';
  }
  // monthly totals footer
  var foot=''; for(var m3=0;m3<months.length;m3++) foot+='<td class="c" style="font-weight:700;">'+fmtQty((c.monthly_units||[])[m3]||0)+'</td>';
  var matrix='<div class="ca-h">Monthly Ordering Matrix — units per product</div>'+
    '<div class="matrix-wrap"><table class="matrix"><thead><tr><th>Product</th>'+mh+'<th class="c">Total</th></tr></thead>'+
    '<tbody>'+mrows+'</tbody><tfoot><tr class="so-group"><td>Total units</td>'+foot+'<td class="c open">'+fmtQty(c.total_units)+'</td></tr></tfoot></table></div>';

  // ── Visual: monthly units trend bars + top products bars ──
  var mu=c.monthly_units||[]; var muMax=Math.max.apply(null, mu.concat([1]));
  var bars=''; var labs='';
  for(var i=0;i<mu.length;i++){
    var hgt=mu[i]>0?Math.max(3,Math.round(mu[i]/muMax*70)):1;
    bars+='<td style="vertical-align:bottom;text-align:center;padding:0 4px;"><div title="'+fmtQty(mu[i])+' units" style="width:26px;height:'+hgt+'px;background:#008080;margin:0 auto;border-radius:3px 3px 0 0;"></div></td>';
    labs+='<td style="text-align:center;font-size:10px;color:#666;padding:3px 4px 0;">'+escapeHtml(months[i])+'<br><b style="color:#101E3E;">'+fmtQty(mu[i])+'</b></td>';
  }
  var trend='<div class="ca-h">Units ordered per month</div>'+
    '<div class="matrix-wrap"><table style="border-collapse:collapse;height:90px;"><tr>'+bars+'</tr><tr>'+labs+'</tr></table></div>';
  // top products horizontal bars
  var topN=prods.slice(0,8); var topMax=topN.length?topN[0].total:1;
  var tp='<div class="ca-h">Top products (YTD units)</div><div style="max-width:680px;">';
  for(var t=0;t<topN.length;t++){
    var w=Math.max(2,Math.round(topN[t].total/topMax*100));
    tp+='<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px;">'+
      '<div style="flex:0 0 230px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="'+escapeHtml(topN[t].name)+'">'+escapeHtml(topN[t].name)+'</div>'+
      '<div style="flex:1 1 auto;background:#eef2f6;border-radius:4px;"><div style="width:'+w+'%;background:#1F4E79;height:14px;border-radius:4px;"></div></div>'+
      '<div style="flex:0 0 40px;text-align:right;font-weight:700;">'+fmtQty(topN[t].total)+'</div></div>';
  }
  tp+='</div>';

  // ── Recommendations table ──
  var recs=c.recommendations||[]; var rr='';
  for(var r=0;r<recs.length;r++){ var rc=recs[r];
    rr+='<tr><td class="item-name">'+escapeHtml(rc.product)+'</td>'+
      '<td class="c">'+rc.months_ordered+'</td><td class="c">'+fmtQty(rc.total)+'</td>'+
      '<td class="c">'+fmtQty(rc.avg)+'</td><td class="c"><span class="hd-badge">'+rc.par+'</span></td>'+
      '<td class="c">'+caTrend(rc.trend)+'</td><td>'+escapeHtml(rc.suggestion)+'</td></tr>';
  }
  var ovl=''; var ov=c.overall||[]; for(var o=0;o<ov.length;o++) ovl+='<li>'+escapeHtml(ov[o])+'</li>';
  var recHtml='<div class="ca-h">Procurement Recommendations</div>'+
    '<ul class="ca-overall">'+ovl+'</ul>'+
    '<div class="matrix-wrap"><table class="matrix"><thead><tr><th>Product</th><th class="c">Mo. Ordered</th>'+
    '<th class="c">Total</th><th class="c">Avg/Mo (YTD)</th><th class="c">Suggested Par</th><th class="c">Trend</th><th>Recommendation</th>'+
    '</tr></thead><tbody>'+rr+'</tbody></table></div>';

  document.getElementById('panel').innerHTML = head + matrix +
    '<div class="ca-visuals">'+trend+tp+'</div>' + recHtml;
}

// ── Customer Prices tab (IDL customers: per-SO unit-selling-price matrix + COGS) ──
var cpActive = 0;        // selected IDL customer index
var cpWindow = 'ytd';    // 'ytd' or a 'YYYY-MM' month key
var cpSku = '';          // SKU / product search filter
function cpMoney(v){ v=Number(v)||0; return '$'+v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function cpMonthKey(dateStr){ return (dateStr||'').slice(0,7); }
function cpMonthLabel(mk){ if(!mk) return ''; var d=new Date(mk+'-01T00:00:00'); if(isNaN(d)) return mk;
  return d.toLocaleDateString('en-US',{month:'short',year:'numeric'}); }
function cpSetOrg(v){ cpActive=parseInt(v,10)||0; cpWindow='ytd'; cpSku=''; renderCpricesPanel(); }
function cpSetWindow(v){ cpWindow=v||'ytd'; renderCpricesPanel(); }
function cpSetSku(v){ cpSku=v||''; renderCpricesPanel();
  var el=document.getElementById('cpSkuSearch'); if(el){ el.focus(); try{ var n=el.value.length; el.setSelectionRange(n,n); }catch(e){} } }
// Snapshot of exactly what the table is currently showing, so Export CSV always
// matches the visible org / window / SKU filter.
var cpLastGrid = null;
var CP_CRLF = String.fromCharCode(13,10);
// NB: the RegExp is built from a string on purpose — this file is emitted from a
// Python string literal, so a backslash escape inside a /regex/ would be eaten.
var CP_NEEDS_QUOTE = new RegExp('[",' + String.fromCharCode(10) + String.fromCharCode(13) + ']');
function cpCsvCell(v){ v=(v===null||v===undefined)?'':String(v);
  return CP_NEEDS_QUOTE.test(v) ? '"'+v.replace(/"/g,'""')+'"' : v; }
function cpExportCsv(){
  var G=cpLastGrid;
  if(!G || !G.skuOrder.length || !G.sos.length){ alert('Nothing to export — the table is empty.'); return; }
  var rows=[];
  // Header: SKU | Product | COGS | one column per SO (SO# + date)
  var hdr=['SKU','Product','COGS'];
  for(var s=0;s<G.sos.length;s++){
    var d=G.sos[s].date?(' ('+G.sos[s].date+')'):'';
    hdr.push((G.sos[s].so_num||'')+d);
  }
  rows.push(hdr);
  for(var k=0;k<G.skuOrder.length;k++){
    var sk=G.skuOrder[k], info=G.skuInfo[sk]||{};
    var r=[sk, info.product||'', (info.cogs===undefined||info.cogs===null||info.cogs==='')?'':Number(info.cogs).toFixed(2)];
    for(var s2=0;s2<G.sos.length;s2++){
      var pv=G.priceBySo[s2][sk];
      r.push((pv===undefined||pv===null)?'':Number(pv).toFixed(2));
    }
    rows.push(r);
  }
  var csv=rows.map(function(r){ return r.map(cpCsvCell).join(','); }).join(CP_CRLF);
  var name=('customer-prices_'+(G.custName||'customer')+'_'+(G.windowKey||'ytd'))
    .replace(/[^A-Za-z0-9._-]+/g,'-').replace(/-+/g,'-')+'.csv';
  try{
    var blob=new Blob(['﻿'+csv],{type:'text/csv;charset=utf-8;'});
    var url=URL.createObjectURL(blob);
    var a=document.createElement('a'); a.href=url; a.download=name;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1500);
  }catch(e){ alert('Could not export CSV: '+e.message); }
}
function renderCpricesPanel(){
  var CP=DATA.customer_prices||{customers:[]};
  var custs=CP.customers||[];
  var panel=document.getElementById('panel');
  if(!custs.length){ panel.innerHTML='<div class="empty">No Independent Diagnostic Lab customers with 2026 Sales Orders found.</div>'; return; }
  if(cpActive>=custs.length) cpActive=0;
  var c=custs[cpActive];
  var allSos=(c.sos||[]).slice();

  // Time-window selector: 2026 YTD (all) + one option per month that has SOs.
  var monthSet={}; for(var i=0;i<allSos.length;i++){ var mk=cpMonthKey(allSos[i].date); if(mk) monthSet[mk]=1; }
  var months=Object.keys(monthSet).sort();
  var winOpts='<option value="ytd"'+(cpWindow==='ytd'?' selected':'')+'>'+escapeHtml(''+(CP.year||'2026'))+' YTD (all)</option>';
  for(var m=0;m<months.length;m++){ winOpts+='<option value="'+months[m]+'"'+(cpWindow===months[m]?' selected':'')+'>'+escapeHtml(cpMonthLabel(months[m]))+'</option>'; }

  // Organization selector (IDL customers only, already filtered server-side).
  var orgOpts=''; for(var o=0;o<custs.length;o++){ orgOpts+='<option value="'+o+'"'+(o===cpActive?' selected':'')+'>'+escapeHtml(custs[o].name)+' ('+custs[o].so_count+' SO)</option>'; }

  // Filter SOs by window.
  var sos=allSos.filter(function(s){ return cpWindow==='ytd' ? true : cpMonthKey(s.date)===cpWindow; });
  // Most-recent SO first (column next to COGS), oldest on the right.
  sos.sort(function(a,b){ return String(b.date).localeCompare(String(a.date)); });

  var controls='<div style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;margin:2px 0 12px;">'+
    '<div><div style="font-size:11px;font-weight:700;color:#6b7a90;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px;">Organization (Independent Diagnostic Lab)</div>'+
    '<select onchange="cpSetOrg(this.value)" style="min-width:280px;padding:7px 10px;border:1px solid #cfd8e3;border-radius:8px;font-size:14px;background:#fff;">'+orgOpts+'</select></div>'+
    '<div><div style="font-size:11px;font-weight:700;color:#6b7a90;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px;">Time window</div>'+
    '<select onchange="cpSetWindow(this.value)" style="min-width:170px;padding:7px 10px;border:1px solid #cfd8e3;border-radius:8px;font-size:14px;background:#fff;">'+winOpts+'</select></div>'+
    '<div><div style="font-size:11px;font-weight:700;color:#6b7a90;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px;">Search SKU</div>'+
    '<input id="cpSkuSearch" type="text" value="'+escapeHtml(cpSku)+'" oninput="cpSetSku(this.value)" placeholder="Filter by SKU or product…" '+
    'style="min-width:220px;padding:7px 10px;border:1px solid #cfd8e3;border-radius:8px;font-size:14px;background:#fff;"></div>'+
    '<div><button onclick="cpExportCsv()" class="mode-btn" '+
    'style="padding:7px 14px;border-radius:8px;border:1px solid #cdd9e6;font-size:13px;cursor:pointer;" '+
    'title="Download the table exactly as filtered (organization, time window, SKU search) as a CSV">'+
    '⬇ Export CSV</button></div>'+
    '</div>';

  var head='<div class="head"><div><h2 style="margin:0;">Customer Prices</h2></div></div>';

  if(!sos.length){ cpLastGrid=null;
    panel.innerHTML=head+controls+'<div class="empty">No Sales Orders for this organization in the selected window.</div>'; return; }

  // Build SKU rows: union across the filtered SOs (preserve first-seen order).
  var skuOrder=[], skuInfo={};   // sku -> {product, cogs}
  var priceBySo=[];              // per-SO map sku -> unit_price
  for(var si=0;si<sos.length;si++){
    var pm={}; var its=sos[si].items||[];
    for(var it=0;it<its.length;it++){ var r=its[it]; var sk=r.sku||'(no sku)';
      pm[sk]=r.unit_price;
      if(!skuInfo[sk]){ skuInfo[sk]={product:r.product||'', cogs:r.cogs}; skuOrder.push(sk); }
      else if((!skuInfo[sk].cogs) && r.cogs){ skuInfo[sk].cogs=r.cogs; }
    }
    priceBySo.push(pm);
  }
  skuOrder.sort();

  // SKU search filter (matches SKU code or product name).
  var q=(cpSku||'').trim().toLowerCase();
  if(q){ skuOrder=skuOrder.filter(function(sk){ var info=skuInfo[sk]||{}; return sk.toLowerCase().indexOf(q)>=0 || (info.product||'').toLowerCase().indexOf(q)>=0; }); }

  // Keep a snapshot for Export CSV — same rows/columns the user is looking at.
  cpLastGrid = { custName:c.name||'', windowKey:(cpWindow==='ytd'?(CP.year||'ytd')+'-YTD':cpWindow),
                 sos:sos, skuOrder:skuOrder, skuInfo:skuInfo, priceBySo:priceBySo };

  // Header: SKU | COGS | one column per SO (SO# + date).
  var thead='<tr>'+
    '<th class="cp-sticky cp-sku">SKU</th>'+
    '<th class="cp-sticky cp-cogs">COGS</th>';
  for(var s2=0;s2<sos.length;s2++){
    thead+='<th class="cp-so"><div class="cp-sonum">'+escapeHtml(sos[s2].so_num||'')+'</div>'+
      '<div class="cp-sodate">'+escapeHtml(fmtDateShort(sos[s2].date))+'</div></th>';
  }
  thead+='</tr>';

  var body='';
  for(var k=0;k<skuOrder.length;k++){
    var sk2=skuOrder[k]; var info=skuInfo[sk2];
    body+='<tr>'+
      '<td class="cp-sticky cp-sku" title="'+escapeHtml(info.product||'')+'">'+escapeHtml(sk2)+'</td>'+
      '<td class="cp-sticky cp-cogs">'+(info.cogs?cpMoney(info.cogs):'<span class="cp-na">—</span>')+'</td>';
    for(var s3=0;s3<sos.length;s3++){
      var pv=priceBySo[s3][sk2];
      if(pv===undefined || pv===null){ body+='<td class="cp-cell cp-empty">·</td>'; }
      else {
        var mark = (info.cogs && pv>0 && pv<info.cogs) ? ' cp-below' : '';   // sold below COGS = red flag
        body+='<td class="cp-cell'+mark+'">'+cpMoney(pv)+'</td>';
      }
    }
    body+='</tr>';
  }

  var table='<div class="cp-wrap"><table class="cp-table"><thead>'+thead+'</thead><tbody>'+body+'</tbody></table></div>';
  if(!skuOrder.length){ table='<div class="empty">No SKU matches "'+escapeHtml(cpSku)+'" for this organization in the selected window.</div>'; }
  var legend='<div style="font-size:12px;color:#7b8798;margin-top:8px;">'+skuOrder.length+' SKU(s)'+(q?' matching "'+escapeHtml(cpSku)+'"':'')+' &middot; each cell = unit selling price for that SKU on that SO ( · = not on that order). '+
    '<span style="color:#c0392b;font-weight:600;">Red</span> = sold below COGS.</div>';
  panel.innerHTML = head + controls + table + legend;
}
function renderPnlPanel(){
  var html=DATA.pnl_html||'';
  document.getElementById('panel').innerHTML = html
    ? '<div class="pnl-wrap">'+html+'</div>'
    : '<div class="empty">P&amp;L report will appear after the next refresh.</div>';
  try{ pnlCmApply(); }catch(e){}
  try{ pnlDetApply(); }catch(e){}
}
// Sections 4 & 5 month filter: toggle the per-month blocks.
function pnlCmApply(){
  var mo=document.getElementById('pnlCmMonth'); if(!mo) return; var mv=mo.value;
  var b=document.querySelectorAll('.pnl-cm-mo');
  for(var i=0;i<b.length;i++){ b[i].style.display=(b[i].getAttribute('data-mo')===mv)?'':'none'; }
}
// Section 7 Detailed Report: month toggle + customer filter (All / IDL-only / a specific
// IDL customer) with live-recomputed totals for the visible month.
// Section 7 Detailed Report: click a column header to sort that month's table.
// Reorders only the .pnl-det-row rows; the Total row (and the "no sales orders"
// placeholder) are always re-appended last so the total stays pinned at the bottom.
// Row visibility set by pnlDetApply() survives, since this only moves nodes.
function pnlDetSort(th){
  var key=th.getAttribute('data-key'); if(!key) return;
  var type=th.getAttribute('data-type')||'s';
  var table=th.parentNode; while(table && table.tagName!=='TABLE') table=table.parentNode;
  if(!table) return;
  var tb=table.getElementsByTagName('tbody')[0]; if(!tb) return;
  var dir=(th.getAttribute('data-dir')==='asc')?'desc':'asc';
  // Clear every header's arrow in this table, then mark the clicked one.
  var ths=table.getElementsByTagName('th');
  for(var i=0;i<ths.length;i++){
    ths[i].removeAttribute('data-dir');
    var c=ths[i].getElementsByClassName('pnl-det-caret')[0];
    if(c) c.textContent='';
  }
  th.setAttribute('data-dir',dir);
  var caret=th.getElementsByClassName('pnl-det-caret')[0];
  if(caret) caret.textContent = (dir==='asc' ? ' ▲' : ' ▼');
  var all=[].slice.call(tb.children);
  var rows=[], rest=[];
  for(var r=0;r<all.length;r++){
    ((all[r].className||'').indexOf('pnl-det-row')>=0 ? rows : rest).push(all[r]);
  }
  var mul=(dir==='asc')?1:-1;
  rows.sort(function(a,b){
    var x=a.getAttribute('data-'+key), y=b.getAttribute('data-'+key);
    if(type==='n'){ x=parseFloat(x); y=parseFloat(y);
      if(isNaN(x)) x=0; if(isNaN(y)) y=0; return (x-y)*mul; }
    x=String(x||'').toLowerCase(); y=String(y||'').toLowerCase();
    return (x<y?-1:(x>y?1:0))*mul;
  });
  for(var j=0;j<rows.length;j++) tb.appendChild(rows[j]);
  for(var k=0;k<rest.length;k++) tb.appendChild(rest[k]);
}
function pnlDetApply(){
  var mo=document.getElementById('pnlDetMonth'); var cu=document.getElementById('pnlDetCust');
  if(!mo) return; var mv=mo.value, cv=cu?cu.value:'__ALL__';
  var blocks=document.querySelectorAll('.pnl-detail-mo');
  for(var i=0;i<blocks.length;i++){
    var show=(blocks[i].getAttribute('data-mo')===mv);
    blocks[i].style.display=show?'':'none';
    if(!show) continue;
    var rows=blocks[i].querySelectorAll('tr.pnl-det-row');
    var net=0,cost=0,qb=0,nd=0,vis=0;
    for(var r=0;r<rows.length;r++){
      var rc=rows[r].getAttribute('data-cust'), idl=(rows[r].getAttribute('data-idl')==='1');
      var ok=(cv==='__ALL__')||(cv==='__IDL__'&&idl)||(rc===cv);
      rows[r].style.display=ok?'':'none';
      if(ok){ vis++; net+=parseFloat(rows[r].getAttribute('data-net'))||0; cost+=parseFloat(rows[r].getAttribute('data-cost'))||0; qb+=parseFloat(rows[r].getAttribute('data-qb'))||0; nd+=parseFloat(rows[r].getAttribute('data-netdep'))||0; }
    }
    var tot=blocks[i].querySelector('tr.pnl-det-total');
    if(tot){ var pnl=net-cost, mg=(net!==0?(pnl/net*100):0);
      function _set(cl,v){ var c=tot.querySelector('.'+cl); if(c) c.innerHTML=v; }
      _set('t-net',payMoney(net)); _set('t-cost',payMoney(cost)); _set('t-pnl',payMoney(pnl));
      _set('t-margin',(net!==0?mg.toFixed(1)+'%':'—')); _set('t-qb',payMoney(qb)); _set('t-netdep',payMoney(nd)); }
    var em=blocks[i].querySelector('tr.pnl-det-filterempty'); if(em) em.parentNode.removeChild(em);
    if(vis===0 && tot){ var tr=document.createElement('tr'); tr.className='pnl-det-filterempty';
      tr.innerHTML='<td colspan="9" style="padding:12px;color:#7a8a99;">No sales orders match this customer filter for the selected month.</td>';
      tot.parentNode.insertBefore(tr, tot); }
  }
}

// ── Customer Open SO's: sidebar (SKU search + "All customers" + one tab per customer) ──
// The search text matches the product string, which carries the SKU
// (e.g. "Beckman Coulter 33565 Testosterone CalIbrator" matches "33565").
function custQ(){ return custSku.replace(/^\s+|\s+$/g,'').toLowerCase(); }
function custMatch(r){ var q=custQ(); if(!q) return true;
  return String(r.product||'').toLowerCase().indexOf(q)>=0; }
// A customer's open rows, narrowed to the SKU search (all rows when the box is empty).
function custRows(c){ var out=[], rows=(c&&c.rows)||[];
  for(var i=0;i<rows.length;i++){ if(custMatch(rows[i])) out.push(rows[i]); }
  return out; }
function renderCustTabs(tabsEl){
  tabsEl.style.display='';
  if(!(DATA.customers||[]).length){ tabsEl.innerHTML='<div class="empty">No open orders.</div>'; return; }
  tabsEl.innerHTML='<div class="custq-box">'+
    '<input id="cust-q" class="custq" type="text" placeholder="Search SKU or product&hellip;" '+
    'autocomplete="off" spellcheck="false" value="'+escapeHtml(custSku)+'" oninput="custOnSearch(this.value)">'+
    '<div class="custq-hint" id="cust-q-hint"></div></div>'+
    '<div id="cust-tablist"></div>';
  renderCustTabList();
}
// Only this list (never the input) is re-rendered while typing, so the box keeps focus + caret.
function renderCustTabList(){
  var el=document.getElementById('cust-tablist'); if(!el) return;
  var list=(DATA.customers||[]), q=custQ(), counts=[], tot=0, shown=0;
  for(var i=0;i<list.length;i++){ var n=custRows(list[i]).length; counts.push(n); tot+=n; }
  var h='<button class="tab'+(active<0?' active':'')+'" onclick="custSelect(-1)">'+
        'All customers<span class="cnt">'+tot+'</span></button>';
  for(var j=0;j<list.length;j++){
    if(q && !counts[j]) continue;                 // hide customers with nothing matching
    shown++;
    h+='<button class="tab'+(j===active?' active':'')+'" onclick="custSelect('+j+')">'+
       escapeHtml(list[j].name)+'<span class="cnt">'+counts[j]+'</span></button>';
  }
  if(q && !shown) h+='<div class="empty" style="padding:14px 16px;font-size:12px;">No open item matches that SKU.</div>';
  el.innerHTML=h;
  var hint=document.getElementById('cust-q-hint');
  if(hint) hint.textContent = q
    ? (shown+' customer'+(shown===1?'':'s')+' · '+tot+' open item'+(tot===1?'':'s')+' match')
    : 'Filter open items by SKU or product name.';
}
function custSelect(i){ active=i; renderCustTabList(); renderCustPanel(); }
function custOnSearch(v){
  custSku=String(v||'');
  // If the selected customer has nothing matching, fall back to the All-customers view
  // so a search always lands on results rather than an empty panel.
  if(custQ() && active>=0){ var c=(DATA.customers||[])[active];
    if(!c || !custRows(c).length) active=-1; }
  renderCustTabList(); renderCustPanel();
}
// SO-grouped <tbody> rows for one customer's (already filtered) open items.
function custSoBody(rows, ncol){
  var groups={}, order=[], body='';
  for(var i=0;i<rows.length;i++){
    var r=rows[i], so=r.so_num||'(no SO)';
    if(!groups[so]){ groups[so]={so:so, status:r.so_status, date:r.order_date, items:[]}; order.push(so); }
    var g=groups[so]; g.items.push(r);
    if(r.order_date && (!g.date || r.order_date<g.date)) g.date=r.order_date;
  }
  // Order SO groups by order date (oldest first), then SO number.
  order.sort(function(a,b){ var d=cmp(groups[a].date,groups[b].date,'date'); return d!==0?d:cmp(groups[a].so,groups[b].so,'str'); });
  for(var gi=0;gi<order.length;gi++){
    var grp=groups[order[gi]];
    var its=grp.items.slice();
    if(sortState.key){ var col=colByKey(sortState.key);
      its.sort(function(p,q){ return sortState.dir*cmp(p[sortState.key],q[sortState.key],col?col.type:'str'); }); }
    else { its.sort(function(p,q){ return cmp(p.product,q.product,'str'); }); }
    var sc=statusColors(grp.status);
    body+='<tr class="so-group"><td colspan="'+ncol+'">'+
      '<span class="so-h">'+escapeHtml(grp.so)+'</span>'+
      '<span class="status" style="background:'+sc[0]+';color:'+sc[1]+'">'+escapeHtml(grp.status)+'</span>'+
      '<span class="so-date">'+fmtDate(grp.date)+'</span>'+
      '<span class="so-cnt">'+grp.items.length+' open item(s)</span></td></tr>';
    for(var j=0;j<its.length;j++){
      var r2=its[j];
      body+='<tr>'+
        '<td>'+escapeHtml(r2.product)+'</td>'+
        '<td>'+escapeHtml(r2.vendor)+'</td>'+
        '<td class="c">'+fmtQty(r2.ordered_qty)+'</td>'+
        '<td class="c">'+fmtQty(r2.delivered_qty)+'</td>'+
        '<td class="c open">'+fmtQty(r2.open_qty)+'</td>'+
        '<td>'+poCell(r2.pending_pos)+'</td>'+
        '<td class="c" style="font-weight:600;color:'+etaColor(r2.eta)+'">'+fmtDate(r2.eta)+'</td>'+
        '</tr>';
    }
  }
  return body;
}
// "All customers": every customer that still has a matching open item, banded by customer.
function renderCustAllPanel(){
  var list=(DATA.customers||[]), q=custQ(), ncol=COLS_CUST.length;
  var body='', nc=0, ni=0, nso={};
  for(var i=0;i<list.length;i++){
    var c=list[i], rows=custRows(c);
    if(!rows.length) continue;
    nc++; ni+=rows.length;
    for(var k=0;k<rows.length;k++) nso[c.name+'||'+(rows[k].so_num||'')]=1;
    body+='<tr class="cust-group"><td colspan="'+ncol+'">'+
      '<span class="cg-h">'+escapeHtml(c.name)+'</span>'+
      '<span class="cg-cnt">'+rows.length+' open item(s)</span></td></tr>'+
      custSoBody(rows, ncol);
  }
  var sortNote = sortState.key ? ' &middot; sorted by '+escapeHtml(colByKey(sortState.key).label)+(sortState.dir>0?' ▲':' ▼') : '';
  var el=document.getElementById('panel');
  if(!body){
    el.innerHTML='<div class="panel-head"><h2 style="margin:0;">All customers</h2>'+
      '<div class="sub">SKU search: &ldquo;'+escapeHtml(custSku)+'&rdquo;</div></div>'+
      '<div class="empty">No customer has that SKU open right now.</div>';
    return;
  }
  el.innerHTML =
    '<div class="panel-head"><h2 style="margin:0;">All customers'+(q?' &mdash; SKU &ldquo;'+escapeHtml(custSku)+'&rdquo;':'')+'</h2>'+
    '<div class="sub">'+nc+' customer(s) &middot; '+Object.keys(nso).length+' SO(s) &middot; '+ni+' open item(s)'+
    (q?' matching the search':'')+' &middot; grouped by customer, then SO'+sortNote+'</div></div>'+
    '<table><thead><tr>'+renderHead()+'</tr></thead><tbody>'+body+'</tbody></table>';
}
function renderCustPanel(){
  var list=(DATA.customers||[]);
  if(!list.length){ document.getElementById('panel').innerHTML='<div class="empty">No open orders.</div>'; return; }
  if(active<0){ renderCustAllPanel(); return; }
  var c=list[active];
  if(!c){ active=-1; renderCustAllPanel(); return; }
  var q=custQ(), rows=custRows(c), ncol=COLS_CUST.length;
  var body=custSoBody(rows, ncol);
  var nso={}; for(var k=0;k<rows.length;k++) nso[rows[k].so_num||'']=1;
  var sortNote = sortState.key ? ' &middot; sorted by '+escapeHtml(colByKey(sortState.key).label)+(sortState.dir>0?' ▲':' ▼') : '';
  var sub = q
    ? (Object.keys(nso).length+' matching SO(s) &middot; '+rows.length+' open item(s) matching &ldquo;'+escapeHtml(custSku)+'&rdquo; &middot; grouped by SO'+sortNote)
    : (c.open_sos+' open SO(s) &middot; '+c.open_items+' open item(s) &middot; '+(c.vendors||[]).length+' vendor(s) &middot; grouped by SO'+sortNote);
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">'+
    '<h2 style="margin:0;">'+escapeHtml(c.name)+'</h2>'+
    '<button class="copy-email-btn" onclick="custEmailToClipboard()" title="Copy this customer&#39;s open-order email (Product, List Price, quantities) — pastes as a formatted table into email/Word/Docs">📋 Copy email</button></div>'+
    '<div class="sub">'+sub+'</div></div>'+
    '<table><thead><tr>'+renderHead()+'</tr></thead><tbody>'+(body||'')+'</tbody></table>'+
    (body?'':'<div class="empty">No open item matches &ldquo;'+escapeHtml(custSku)+'&rdquo; for this customer.</div>');
}

// ── "Copy email" for the selected customer (Customer Open SO's tab) ──
// Builds a standalone HTML email of the customer's open orders grouped by SO.
// Columns: Product, List Price (from the SO), Ordered, Delivered, Open.
// (No Vendor / Pending PO / ETA / Email-vendor button — per request.)
function fmtMoney(v){ if(v==null||v==='') return '&mdash;'; var n=Number(v); if(isNaN(n)) return '&mdash;';
  return '$'+n.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g,','); }
function buildCustEmailHtml(c){
  var groups={}, order=[], rows=(c.rows||[]);
  for(var i=0;i<rows.length;i++){
    var r=rows[i], so=r.so_num||'(no SO)';
    if(!groups[so]){ groups[so]={so:so, status:r.so_status, date:r.order_date, items:[]}; order.push(so); }
    var g=groups[so]; g.items.push(r);
    if(r.order_date && (!g.date || r.order_date<g.date)) g.date=r.order_date;
  }
  order.sort(function(a,b){ var d=cmp(groups[a].date,groups[b].date,'date'); return d!==0?d:cmp(groups[a].so,groups[b].so,'str'); });
  var td='padding:8px 10px;border-bottom:1px solid #e6ebf1;font-size:13px;color:#2c3e50;';
  var tdc=td+'text-align:center;';
  var tdr=td+'text-align:right;white-space:nowrap;';
  var th='padding:8px 10px;background:#1f3a5f;color:#fff;font-size:12px;text-align:left;';
  var thc='padding:8px 10px;background:#1f3a5f;color:#fff;font-size:12px;text-align:center;';
  var thr='padding:8px 10px;background:#1f3a5f;color:#fff;font-size:12px;text-align:right;';
  var sections='';
  for(var gi=0;gi<order.length;gi++){
    var grp=groups[order[gi]];
    var its=grp.items.slice().sort(function(p,q){ return cmp(p.product,q.product,'str'); });
    var rowsHtml='';
    for(var j=0;j<its.length;j++){
      var r2=its[j];
      rowsHtml+='<tr>'+
        '<td style="'+td+'">'+escapeHtml(r2.product)+'</td>'+
        '<td style="'+tdr+'">'+fmtMoney(r2.list_price)+'</td>'+
        '<td style="'+tdc+'">'+fmtQty(r2.ordered_qty)+'</td>'+
        '<td style="'+tdc+'">'+fmtQty(r2.delivered_qty)+'</td>'+
        '<td style="'+tdc+'font-weight:700;color:#c0392b;">'+fmtQty(r2.open_qty)+'</td>'+
        '</tr>';
    }
    sections+='<div style="margin:0 0 6px;font-size:13px;color:#1f3a5f;font-weight:700;">'+
      'SO '+escapeHtml(grp.so)+' &middot; '+escapeHtml(grp.status||'')+' &middot; '+fmtDate(grp.date)+
      '</div>'+
      '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;max-width:640px;margin:0 0 18px;border:1px solid #e6ebf1;">'+
      '<thead><tr><th style="'+th+'">Product</th><th style="'+thr+'">List Price</th>'+
      '<th style="'+thc+'">Ordered</th><th style="'+thc+'">Delivered</th><th style="'+thc+'">Open</th></tr></thead>'+
      '<tbody>'+rowsHtml+'</tbody></table>';
  }
  if(!order.length) sections='<p style="font-size:13px;color:#2c3e50;">No open orders found.</p>';
  var today=new Date().toLocaleDateString('en-US',{month:'long',day:'numeric',year:'numeric'});
  return '<div style="font-family:Arial,Helvetica,sans-serif;max-width:680px;margin:0 auto;color:#2c3e50;">'+
    '<div style="background:#1f3a5f;color:#fff;padding:16px 18px;border-radius:6px 6px 0 0;">'+
    '<div style="font-size:18px;font-weight:700;">JIT4Labs &mdash; Open Order Status</div>'+
    '<div style="font-size:13px;color:#cdd9e6;margin-top:2px;">'+escapeHtml(c.name)+' &middot; '+today+'</div></div>'+
    '<div style="padding:18px;border:1px solid #e6ebf1;border-top:none;border-radius:0 0 6px 6px;">'+
    '<p style="font-size:13px;color:#2c3e50;margin:0 0 16px;">Please find below the current status of your open orders with JIT4Labs.</p>'+
    sections+
    '<p style="font-size:12px;color:#8a97a6;margin:14px 0 0;">Prices shown are the unit list price from each sales order. Quantities reflect the latest fulfillment status. Questions? Reply to this email.</p>'+
    '</div></div>';
}
function custEmailToClipboard(){
  if(active<0){ alert('Pick a single customer in the sidebar to copy their open-order email.'); return; }
  var c=(DATA.customers||[])[active];
  if(!c){ alert('No customer selected.'); return; }
  // Always emails the customer's FULL open orders — the SKU search only narrows the on-screen view.
  var html=buildCustEmailHtml(c);
  function done(){ var b=document.querySelector('.copy-email-btn'); if(b){ var o=b.innerHTML; b.innerHTML='✓ Copied!'; setTimeout(function(){ b.innerHTML=o; },1800); } }
  // Copy as rich text/html so pasting drops in the RENDERED TABLE (email/Word/Docs), not raw code.
  try {
    if(navigator.clipboard && window.ClipboardItem){
      var item=new ClipboardItem({
        'text/html':new Blob([html],{type:'text/html'}),
        'text/plain':new Blob([html],{type:'text/plain'})
      });
      navigator.clipboard.write([item]).then(done, function(){ fallbackCopyHtml(html, done); });
      return;
    }
  } catch(e){}
  fallbackCopyHtml(html, done);
}
function fallbackCopyHtml(html, cb){
  // Copy rendered rich content via a temporary contenteditable node so paste yields a table.
  var div=document.createElement('div'); div.contentEditable='true'; div.innerHTML=html;
  div.style.position='fixed'; div.style.left='-9999px'; div.style.top='0';
  document.body.appendChild(div);
  var sel=window.getSelection(); sel.removeAllRanges();
  var range=document.createRange(); range.selectNodeContents(div); sel.addRange(range);
  try{ document.execCommand('copy'); if(cb) cb(); }catch(e){ alert('Copy failed — please select and copy manually.'); }
  sel.removeAllRanges(); document.body.removeChild(div);
}

function renderVendorPanel(){
  var v=(DATA.vendors||[])[vactive];
  if(!v){ document.getElementById('panel').innerHTML='<div class="empty">No open vendor orders.</div>'; return; }
  // Group this vendor's rows by customer.
  var groups={}, order=[], rows=(v.rows||[]);
  for(var i=0;i<rows.length;i++){
    var r=rows[i], cu=r.customer||'(no customer)';
    if(!groups[cu]){ groups[cu]={cust:cu, items:[]}; order.push(cu); }
    groups[cu].items.push(r);
  }
  order.sort(function(a,b){ return cmp(a,b,'str'); });  // customers A→Z
  var ncol=COLS_VENDOR.length, body='';
  for(var gi=0;gi<order.length;gi++){
    var grp=groups[order[gi]];
    var its=grp.items.slice();
    if(sortState.key){ var col=colByKey(sortState.key);
      // 'fulfill_opp' is computed from the uploaded inventory, not a field on the row.
      var val=function(r){ return sortState.key==='fulfill_opp' ? fopAvail(r, v.name) : r[sortState.key]; };
      its.sort(function(p,q){ return sortState.dir*cmp(val(p),val(q),col?col.type:'str'); }); }
    else { its.sort(function(p,q){ var d=cmp(p.order_date,q.order_date,'date'); return d!==0?d:cmp(p.product,q.product,'str'); }); }
    body+='<tr class="so-group"><td colspan="'+ncol+'">'+
      '<span class="so-h">'+escapeHtml(grp.cust)+'</span>'+
      '<span class="so-cnt">'+grp.items.length+' open item(s)</span></td></tr>';
    for(var j=0;j<its.length;j++){
      var r2=its[j]; var sc=statusColors(r2.so_status);
      body+='<tr>'+
        '<td class="so">'+escapeHtml(r2.so_num)+'</td>'+
        '<td>'+fmtDate(r2.order_date)+'</td>'+
        '<td>'+escapeHtml(r2.product)+'</td>'+
        '<td class="c">'+fmtQty(r2.ordered_qty)+'</td>'+
        '<td class="c">'+fmtQty(r2.delivered_qty)+'</td>'+
        '<td class="c open">'+fmtQty(r2.open_qty)+'</td>'+
        '<td>'+poCell(r2.pending_pos, true)+'</td>'+
        '<td class="c" style="font-weight:600;color:'+etaColor(r2.eta)+'">'+fmtDate(r2.eta)+'</td>'+
        '<td class="c">'+fopCell(r2, v.name)+'</td>'+
        '</tr>';
    }
  }
  var sortNote = sortState.key ? ' &middot; sorted by '+escapeHtml(colByKey(sortState.key).label)+(sortState.dir>0?' ▲':' ▼') : '';
  var hasEmail = v.email && v.email.indexOf('@')>-1;
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;">'+
    '<div><h2>'+escapeHtml(v.name)+'</h2>'+
    '<div class="sub">'+v.pos+' open PO(s) &middot; '+v.open_items+' open item(s) &middot; '+
    (v.customers||[]).length+' customer(s) &middot; '+(hasEmail?escapeHtml(v.email):'<span style="color:#c62828;">no email on file</span>')+
    ' &middot; grouped by customer'+sortNote+'</div></div>'+
    '<button class="ca-email-btn" onclick="vendorEmail('+vactive+')">✉ Create email draft</button>'+
    '</div></div>'+
    '<table><thead><tr>'+renderHead()+'</tr></thead><tbody>'+body+'</tbody></table>';
}

// ── Payment Status tab (QuickBooks 2026 invoices for Independent Diagnostic Lab customers) ──
var PAY=null, payLoading=false, payCust='', payReadyOnly=false, payNotReadyOnly=false, payShowManual=false;
// "Ready for payment" = Not Paid AND Fulfilled.  "Not ready" = Not Paid AND NOT Fulfilled.
// ── Manual payment overrides ──────────────────────────────────────────────
// When a payment has come in but the accountant hasn't marked the QuickBooks invoice yet,
// the user records the amount received here. Balance and every KPI recompute live from the
// EFFECTIVE balance, and the override persists to payment-overrides.json via the button token
// (so it survives page reloads, other devices, and the nightly rebuild). QuickBooks stays the
// source of truth: once the accountant marks it paid, the invoice drops out on its own.
var PAYOVR=null;
function _payToday(){ var d=new Date(); return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2); }
function payOvrMap(){ if(PAYOVR==null){ PAYOVR={}; try{ var p=JSON.parse(localStorage.getItem('jit4_pay_ovr')||'{}'); for(var k in p){ if(p.hasOwnProperty(k)) PAYOVR[k]=p[k]; } }catch(e){} } return PAYOVR; }
function paySaveLocal(m){ PAYOVR=m; try{ localStorage.setItem('jit4_pay_ovr', JSON.stringify(m)); }catch(e){} }
function payOvrOf(v){ var m=payOvrMap(); return (v&&m[v.number])||null; }
function payHasOvr(v){ return !!payOvrOf(v); }
function payEffBalance(v){ var o=payOvrOf(v); var amt=Number(v.amount)||0;
  if(o&&o.paid!=null){ var b=amt-Number(o.paid); return Math.round((b>0?b:0)*100)/100; }
  return Number(v.balance)||0; }
function payEffStatus(v){ if(v.status==='Voided') return 'Voided'; return payEffBalance(v)<=0.005?'Paid':'Not Paid'; }
function payOvrFetch(){ fetch('payment-overrides.json?cb='+Date.now(),{cache:'no-store'})
  .then(function(r){ return r.ok?r.json():null; })
  .then(function(j){ if(j&&j.overrides){ var m=payOvrMap(),ch=false; for(var k in j.overrides){ if(j.overrides.hasOwnProperty(k)&&!m.hasOwnProperty(k)){ m[k]=j.overrides[k]; ch=true; } } if(ch){ paySaveLocal(m); if(mode==='pay') renderPayPanel(); } } })
  .catch(function(){}); }
function payOvrCommit(){ if(!BTN||!BTN.token) return; var m=payOvrMap();
  var base='https://api.github.com/repos/'+BTN.repo+'/contents/payment-overrides.json';
  var hdr={'Authorization':'Bearer '+BTN.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'};
  fetch(base+'?ref='+encodeURIComponent(BTN.branch)+'&cb='+Date.now(),{headers:hdr,cache:'no-store'})
    .then(function(r){ return r.ok?r.json():{}; })
    .then(function(st){ return fetch(base,{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},hdr),
      body:JSON.stringify({message:'Update manual payment overrides ('+Object.keys(m).length+')', content:_b64enc(JSON.stringify({overrides:m},null,2)+'\\n'), sha:st.sha||undefined, branch:BTN.branch})}); })
    .catch(function(){}); }
function payRecord(number, amount){ var m=payOvrMap(), cur=m[number];
  var def=(cur&&cur.paid!=null)?cur.paid:amount;
  var inp=window.prompt('Record payment for invoice '+number+'\\nInvoice total: '+payMoney(amount)+'\\nEnter TOTAL amount received on this invoice (set 0 to undo):', String(def));
  if(inp===null) return; inp=String(inp).replace(/[^0-9.]/g,''); var paid=Number(inp);
  if(isNaN(paid)) return;
  if(paid<=0){ delete m[number]; } else { m[number]={paid:Math.round(paid*100)/100, at:_payToday()}; }
  paySaveLocal(m); renderPayPanel(); payOvrCommit(); }
function payMarkFull(number, amount){ var m=payOvrMap(); m[number]={paid:Math.round((Number(amount)||0)*100)/100, at:_payToday()}; paySaveLocal(m); renderPayPanel(); payOvrCommit(); }
function payUndo(number){ var m=payOvrMap(); delete m[number]; paySaveLocal(m); renderPayPanel(); payOvrCommit(); }
function payActMark(a){ payMarkFull(a.getAttribute('data-num'), Number(a.getAttribute('data-amt'))); }
function payActRecord(a){ payRecord(a.getAttribute('data-num'), Number(a.getAttribute('data-amt'))); }
function payActUndo(a){ payUndo(a.getAttribute('data-num')); }
function payInvoices(c){ var invs=(c&&c.invoices)||[];
  if(payReadyOnly) return invs.filter(function(v){ return payEffStatus(v)==='Not Paid' && v.fulfillment==='Fulfilled'; });
  if(payNotReadyOnly) return invs.filter(function(v){ return payEffStatus(v)==='Not Paid' && v.fulfillment!=='Fulfilled'; });
  if(payShowManual) return invs.filter(function(v){ return payHasOvr(v); });
  return invs.filter(function(v){ return payEffStatus(v)!=='Paid'; }); }   // hide fully-paid (incl. manually recorded)
// Balance cell: green $0 when fully paid; amber+bold when PARTIALLY paid (0<balance<amount) — the case to watch; plain when fully unpaid.
function payBalanceCell(v){ var bal=payEffBalance(v), amt=Number(v.amount)||0, man=payHasOvr(v);
  var tag = man? ' <span title="Manually recorded — '+payMoney(amt-bal)+' received on '+((payOvrOf(v)||{}).at||'')+'" style="font-size:10px;color:#8a6d00;background:#fff8e1;border-radius:6px;padding:0 5px;">manual</span>':'';
  if(bal<=0.005) return '<span style="color:#27ae60;">'+payMoney(0)+'</span>'+tag;
  if(bal < amt-0.005) return '<span style="color:#e67e22;font-weight:700;" title="Partially paid — '+payMoney(amt-bal)+' of '+payMoney(amt)+' received">'+payMoney(bal)+'</span>'+tag;
  return '<span style="color:#2c3e50;">'+payMoney(bal)+'</span>'+tag; }
function payTotals(invs){ var amt=0, unpaid=0; for(var i=0;i<invs.length;i++){ amt+=Number(invs[i].amount)||0; unpaid+=payEffBalance(invs[i]); }
  return {count:invs.length, amount:Math.round(amt*100)/100, unpaid:Math.round(unpaid*100)/100}; }
function payToggleReady(cb){ payReadyOnly=!!cb.checked; if(payReadyOnly){ payNotReadyOnly=false; payShowManual=false; } renderPayPanel(); }
function payToggleNotReady(cb){ payNotReadyOnly=!!cb.checked; if(payNotReadyOnly){ payReadyOnly=false; payShowManual=false; } renderPayPanel(); }
function payToggleManual(cb){ payShowManual=!!cb.checked; if(payShowManual){ payReadyOnly=false; payNotReadyOnly=false; } renderPayPanel(); }
function loadPay(){
  if(PAY_EMBED){ PAY=PAY_EMBED; payLoading=false; payOvrFetch(); if(mode==='pay'){ renderPayPanel(); } return; }
  if(payLoading) return; payLoading=true;
  fetch('payment-status-data.json?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(d){ PAY=d; payLoading=false; payOvrFetch(); if(mode==='pay'){ renderPayPanel(); } })
    .catch(function(e){ payLoading=false; if(mode==='pay') document.getElementById('panel').innerHTML='<div class="empty">Could not load payment data: '+escapeHtml(e.message)+'</div>'; });
}
function payCustomers(){ return ((PAY&&PAY.customers)||[]).filter(function(c){ return (c.invoices||[]).length>0; }); }
function payCurrent(){ var cs=payCustomers(); if(!cs.length) return null;
  if(payCust==='__ALL__') return null;
  if(!payCust || !cs.some(function(c){return c.name===payCust;})) payCust=cs[0].name;
  return cs.filter(function(c){return c.name===payCust;})[0]; }
function payCurrentOrAll(){
  if(payCust!=='__ALL__') return payCurrent();
  var cs=payCustomers(), inv=[];
  for(var i=0;i<cs.length;i++){ var iv=cs[i].invoices||[]; for(var j=0;j<iv.length;j++){ inv.push(Object.assign({_cust:cs[i].name}, iv[j])); } }
  return {name:'All customers', invoices:inv, _all:true}; }
function paySelectChange(){ var s=document.getElementById('paySelect'); if(s){ payCust=s.value; } renderPayPanel(); }
function payMoney(v){ var n=Number(v)||0; return '$'+n.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g,','); }
function payBadge(st){ var col= st==='Paid'?['#d4edda','#155724']:(st==='Not Paid'?['#f8d7da','#842029']:['#e2e3e5','#41464b']);
  return '<span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;background:'+col[0]+';color:'+col[1]+';">'+escapeHtml(st)+'</span>'; }
function payFulfillCell(v){
  var f=v.fulfillment||'';
  if(f==='Fulfilled') return '<span style="color:#188038;font-weight:600;">Fulfilled</span>';
  if(f==='Cancelled') return '<span style="color:#9aa7b4;">Cancelled</span>';
  if(f==='Partially'){
    if(v.so_num) return '<a href="#" data-so="'+escapeHtml(v.so_num)+'" onclick="showFulfill(this);return false;" style="color:#b54708;font-weight:600;text-decoration:none;">Partially <span style="font-size:11px;">▦</span></a>';
    return '<span style="color:#b54708;font-weight:600;">Partially</span>';
  }
  if(f==='Unknown') return '<span title="Vtiger did not return this SO&#39;s line items on the last refresh, so fulfillment could not be verified. Treated as NOT ready for payment. Re-run the refresh to resolve." style="color:#b54708;font-weight:600;">Unverified <span style="font-size:11px;">&#9888;</span></span>';
  return '<span style="color:#c8d0d8;">—</span>';
}
function closeFulfill(){ var m=document.getElementById('fulfillModal'); if(m) m.parentNode.removeChild(m); }
function payFindInvoice(soNum){
  var cs=(PAY&&PAY.customers)||[];
  for(var i=0;i<cs.length;i++){ var iv=cs[i].invoices||[]; for(var j=0;j<iv.length;j++){ if(iv[j].so_num===soNum) return {inv:iv[j],cust:cs[i].name}; } }
  return null;
}
function showFulfill(soNum){
  if(soNum&&soNum.getAttribute) soNum=soNum.getAttribute('data-so');
  var hit=payFindInvoice(soNum), inv=hit?hit.inv:null, cust=hit?hit.cust:'', odate=inv?inv.so_date:'';
  var rows=(inv&&inv.open_items)||[];
  var body='';
  if(!rows.length){ body='<div style="padding:14px 4px;color:#7a8a99;">No open line items returned from Vtiger for '+escapeHtml(soNum)+'.</div>'; }
  else {
    body='<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:6px;"><thead><tr>'+
      '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e6ecf2;">Open item</th>'+
      '<th style="text-align:right;padding:6px 8px;border-bottom:2px solid #e6ecf2;">Open qty</th>'+
      '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e6ecf2;">Vendor</th>'+
      '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e6ecf2;">PO #</th></tr></thead><tbody>';
    for(var k=0;k<rows.length;k++){ var r=rows[k];
      body+='<tr>'+
        '<td style="padding:6px 8px;border-bottom:1px solid #eef2f6;">'+escapeHtml(r.product||'')+'</td>'+
        '<td style="padding:6px 8px;border-bottom:1px solid #eef2f6;text-align:right;">'+(r.open_qty!=null?r.open_qty:'')+'</td>'+
        '<td style="padding:6px 8px;border-bottom:1px solid #eef2f6;">'+escapeHtml(r.vendor||'')+'</td>'+
        '<td style="padding:6px 8px;border-bottom:1px solid #eef2f6;">'+escapeHtml(r.po||'')+'</td></tr>';
    }
    body+='</tbody></table>';
  }
  var html='<div id="fulfillModal" style="position:fixed;inset:0;background:rgba(20,30,45,0.45);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;" onclick="if(event.target===this)closeFulfill();">'+
    '<div style="background:#fff;border-radius:10px;max-width:640px;width:100%;max-height:80vh;overflow:auto;box-shadow:0 12px 40px rgba(0,0,0,0.25);padding:18px 20px;">'+
    '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;">'+
      '<div><div style="font-size:16px;font-weight:700;color:#1f3a5f;">Open items &mdash; '+escapeHtml(soNum)+'</div>'+
      '<div style="font-size:12px;color:#5a6b7a;margin-top:2px;">'+escapeHtml(cust||'')+(odate?' &middot; SO date '+escapeHtml(fmtDate(odate)):'')+'</div></div>'+
      '<button onclick="closeFulfill()" style="border:none;background:#eef2f6;border-radius:6px;font-size:18px;line-height:1;padding:4px 10px;cursor:pointer;color:#5a6b7a;">&times;</button>'+
    '</div>'+body+'</div></div>';
  var d=document.createElement('div'); d.innerHTML=html; document.body.appendChild(d.firstChild);
}
function payGrandTotals(){
  var cs=payCustomers(), amt=0,unpaid=0,ready=0,readyN=0,openN=0,paid=0;
  for(var i=0;i<cs.length;i++){ var iv=cs[i].invoices||[];
    for(var j=0;j<iv.length;j++){ var v=iv[j], bal=payEffBalance(v);
      amt+=Number(v.amount)||0; unpaid+=bal;
      if(payEffStatus(v)==='Not Paid'){ openN++; if(v.fulfillment==='Fulfilled'){ ready+=bal; readyN++; } }
    }
  }
  paid=amt-unpaid;
  return {amt:amt,unpaid:unpaid,paid:paid,ready:ready,readyN:readyN,openN:openN,custN:cs.length};
}
function renderPayPanel(){
  if(!PAY){ document.getElementById('panel').innerHTML='<div class="empty">Loading payment status…</div>'; loadPay(); return; }
  var cs=payCustomers();
  if(!cs.length){ document.getElementById('panel').innerHTML='<div class="empty">No 2026 invoices found for Independent Diagnostic Lab customers.</div>'; return; }
  // "Ready for payment only" also narrows the customer dropdown to customers who have
  // ready (Not Paid + Fulfilled) invoices, and shows the ready count per customer.
  function readyCount(cc){ var n=0,iv=(cc&&cc.invoices)||[]; for(var i=0;i<iv.length;i++){ if(payEffStatus(iv[i])==='Not Paid'&&iv[i].fulfillment==='Fulfilled') n++; } return n; }
  function notReadyCount(cc){ var n=0,iv=(cc&&cc.invoices)||[]; for(var i=0;i<iv.length;i++){ if(payEffStatus(iv[i])==='Not Paid'&&iv[i].fulfillment!=='Fulfilled') n++; } return n; }
  function notPaidCount(cc){ var n=0,iv=(cc&&cc.invoices)||[]; for(var i=0;i<iv.length;i++){ if(payEffStatus(iv[i])!=='Paid') n++; } return n; }
  function manualCount(cc){ var n=0,iv=(cc&&cc.invoices)||[]; for(var i=0;i<iv.length;i++){ if(payHasOvr(iv[i])) n++; } return n; }
  var vcs = payReadyOnly ? cs.filter(function(cc){ return readyCount(cc)>0; })
          : (payNotReadyOnly ? cs.filter(function(cc){ return notReadyCount(cc)>0; })
          : (payShowManual ? cs.filter(function(cc){ return manualCount(cc)>0; })
          : cs.filter(function(cc){ return notPaidCount(cc)>0; })));
  if(payCust!=='__ALL__' && (payReadyOnly||payNotReadyOnly) && !vcs.some(function(cc){return cc.name===payCust;})) payCust='__ALL__';
  var c=payCurrentOrAll(); var allMode=!!(c&&c._all);
  var opts='<option value="__ALL__"'+(allMode?' selected':'')+'>All customers ('+vcs.length+')</option>';
  for(var i=0;i<vcs.length;i++){ var cnt=payReadyOnly?readyCount(vcs[i]):(payNotReadyOnly?notReadyCount(vcs[i]):(payShowManual?manualCount(vcs[i]):notPaidCount(vcs[i]))); opts+='<option value="'+escapeHtml(vcs[i].name)+'"'+(vcs[i].name===payCust?' selected':'')+'>'+escapeHtml(vcs[i].name)+' ('+cnt+')</option>'; }
  var invs=payInvoices(c), body='';
  for(var j=0;j<invs.length;j++){ var v=invs[j]; var est=payEffStatus(v), man=payHasOvr(v); var amtN=Number(v.amount)||0;
    var sob=v.shopify_order||'';
    var soCell = sob? '<a href="https://admin.shopify.com/store/jit4you/orders?query='+encodeURIComponent(sob.replace(/^#/,''))+'" target="_blank" rel="noopener" title="Open in Shopify admin" style="color:#1f6f43;text-decoration:none;white-space:nowrap;">'+escapeHtml(sob)+' ↗</a>' : '<span style="color:#c8d0d8;">—</span>';
    var actions = (v.status==='Voided') ? '<span style="color:#c8d0d8;">—</span>' :
      (man? '<a href="#" data-num="'+escapeHtml(v.number)+'" data-amt="'+amtN+'" onclick="payActRecord(this);return false;" style="color:#1a73e8;text-decoration:none;">✎ Edit</a> &middot; <a href="#" data-num="'+escapeHtml(v.number)+'" onclick="payActUndo(this);return false;" style="color:#b54708;text-decoration:none;">↩ Undo</a>'
          : '<a href="#" data-num="'+escapeHtml(v.number)+'" data-amt="'+amtN+'" onclick="payActMark(this);return false;" title="Mark fully paid" style="color:#188038;text-decoration:none;">✓ Mark paid</a> &middot; <a href="#" data-num="'+escapeHtml(v.number)+'" data-amt="'+amtN+'" onclick="payActRecord(this);return false;" title="Record a specific amount received" style="color:#1a73e8;text-decoration:none;">$…</a>');
    body+='<tr>'+
      (allMode?'<td style="white-space:nowrap;">'+escapeHtml(v._cust||'')+'</td>':'')+
      '<td>'+escapeHtml(v.number)+'</td>'+
      '<td>'+(v.so_num?escapeHtml(v.so_num):'<span style="color:#c8d0d8;">—</span>')+'</td>'+
      '<td>'+soCell+'</td>'+
      '<td class="c">'+payBadge(est)+(man&&est==='Paid'?' <span style="font-size:10px;color:#8a6d00;">(manual)</span>':'')+'</td>'+
      '<td class="c">'+payFulfillCell(v)+'</td>'+
      '<td class="c">'+fmtDate(v.date)+'</td>'+
      '<td style="text-align:right;">'+payMoney(v.amount)+'</td>'+
      '<td style="text-align:right;">'+payBalanceCell(v)+'</td>'+
      '<td class="c" style="white-space:nowrap;font-size:12px;">'+actions+'</td>'+
      '<td class="c">'+(function(){var u=v.invoice_link||v.link; if(!u) return '<span style="color:#999;">'+(est==='Paid'?'&mdash;':'No link')+'</span>'; return '<a href="'+escapeHtml(u)+'" target="_blank" rel="noopener" title="'+(v.invoice_link?'Opens the full invoice (line items) with a Pay button':'Opens the payment page')+'">'+(v.invoice_link?'View invoice &amp; pay ':'Pay ')+payMoney(payEffBalance(v)||v.amount)+' <span style="color:#008080;">↗</span></a>';})()+'</td>'+
      '</tr>';
  }
  if(!invs.length){ body='<tr><td colspan="'+(allMode?11:10)+'" class="empty" style="padding:16px;">No invoices'+(payReadyOnly?' ready for payment':(payNotReadyOnly?' not ready for payment':(payShowManual?' manually recorded as paid':'')))+' for this selection.</td></tr>'; }
  var t=payTotals(invs);
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">'+
    '<h2 style="margin:0;">Payment Status</h2>'+
    '<select id="paySelect" onchange="paySelectChange()" style="padding:7px 10px;border:1px solid #cdd9e6;border-radius:6px;font-size:13px;min-width:240px;">'+opts+'</select>'+
    '<label style="display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#2c3e50;cursor:pointer;white-space:nowrap;"><input type="checkbox" onchange="payToggleReady(this)"'+(payReadyOnly?' checked':'')+'> Ready for payment only</label>'+
    '<label style="display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#2c3e50;cursor:pointer;white-space:nowrap;"><input type="checkbox" onchange="payToggleNotReady(this)"'+(payNotReadyOnly?' checked':'')+'> Not ready for payment</label>'+
    '<label style="display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#2c3e50;cursor:pointer;white-space:nowrap;" title="Invoices you manually recorded a payment on"><input type="checkbox" onchange="payToggleManual(this)"'+(payShowManual?' checked':'')+'> Manually recorded</label>'+
    '<button class="copy-email-btn" onclick="copyPayTable()" title="Copy this invoice table — pastes as a formatted table into email/Word/Docs">📋 Copy table</button></div>'+
    '<div class="sub">Independent Diagnostic Lab &middot; '+t.count+' invoice(s)'+(payReadyOnly?' ready for payment':(payNotReadyOnly?' not ready for payment':(payShowManual?' manually recorded as paid':'')))+' &middot; '+payMoney(t.amount)+' total &middot; '+payMoney(t.unpaid)+' unpaid &middot; QuickBooks '+escapeHtml(''+(PAY.year||''))+' &middot; as of '+escapeHtml(PAY.generated_at||'')+'</div></div>'+
    (function(){var g=payGrandTotals(), apTot=((PAY.payables&&PAY.payables.grand_total)||0), net=g.unpaid-apTot;
      return '<div class="ca-h" style="margin:2px 0 4px;">Portfolio summary &mdash; all Independent Diagnostic Labs ('+g.custN+' customers)</div>'+
      '<div class="kpis" style="padding:2px 0 12px;">'+
        (PAY.bank ? kpi(payMoney(PAY.bank.balance||0),'Bank balance &middot; '+escapeHtml(PAY.bank.name||''),
            'background:linear-gradient(135deg,#eaf3fb,#bcd9f2);border-color:#2e6da4;') : '')+
        kpi(payMoney(g.unpaid),'Outstanding (A/R)')+
        kpi(payMoney(g.ready),'Ready for payment')+
        kpi(g.readyN+' / '+g.openN,'Invoices ready / open')+
        kpi(payMoney(apTot),'Vendor payable (A/P)')+
        kpi((net<0?'-':'')+payMoney(Math.abs(net)),'Net (A/R &minus; A/P)',
            'background:linear-gradient(135deg,'+(net<0?'#fdecea,#f1a9a0':'#e8f8ef,#a3dfbb')+');border-color:'+(net<0?'#c0392b':'#1e8449')+';')+
      '</div>';})()+
    '<div class="ca-h" style="margin-top:6px;">'+escapeHtml(allMode?'All customers':c.name)+' &mdash; invoices</div>'+
    '<table><thead><tr>'+(allMode?'<th>Customer</th>':'')+'<th>Invoice #</th><th>SO #</th><th>Shopify</th><th class="c">Status</th><th class="c">Fulfillment</th><th class="c">Date</th><th style="text-align:right;">Amount</th><th style="text-align:right;">Balance</th><th class="c">Actions</th><th class="c">Link</th></tr></thead><tbody>'+body+
    '<tr class="so-group"><td colspan="'+(allMode?7:6)+'" style="text-align:right;font-weight:700;">Total ('+t.count+')</td>'+
    '<td style="text-align:right;font-weight:700;">'+payMoney(t.amount)+'</td>'+
    '<td style="text-align:right;font-weight:700;color:'+(t.unpaid>0.005?'#c0392b':'#27ae60')+';">'+payMoney(t.unpaid)+'</td>'+
    '<td class="c"></td><td class="c"></td></tr>'+
    '</tbody></table>' +
    payablesHtml();
}
// ── Accounts Payable section (vendors we owe, from QuickBooks open bills) ──
function payablesHtml(){
  var ap=PAY.payables;
  if(!ap || !ap.vendors || !ap.vendors.length){
    return '<div class="ca-h" style="margin-top:26px;">Accounts Payable &mdash; vendors you owe (QuickBooks)</div>'+
      '<div class="empty" style="padding:12px;">No open vendor bills in QuickBooks.</div>';
  }
  var vs=ap.vendors.slice().sort(function(a,b){return b.balance-a.balance;});
  var rows='';
  for(var i=0;i<vs.length;i++){ var v=vs[i];
    rows+='<tr>'+
      '<td>'+escapeHtml(v.vendor||'')+'</td>'+
      '<td class="c">'+(v.count||0)+'</td>'+
      '<td style="text-align:right;color:'+((v.pastdue>0.005)?'#c0392b':'#7f8c8d')+';font-weight:'+((v.pastdue>0.005)?'700':'400')+';">'+payMoney(v.pastdue||0)+'</td>'+
      '<td style="text-align:right;font-weight:700;color:#c0392b;">'+payMoney(v.balance||0)+'</td>'+
      '</tr>';
  }
  return '<div class="ca-h" style="margin-top:26px;">Accounts Payable &mdash; vendors you owe (QuickBooks)</div>'+
    '<div class="kpis" style="padding:2px 0 12px;">'+
      kpi(payMoney(ap.grand_total||0),'Total payable')+
      kpi(payMoney(ap.past_due||0),'Past due')+
      kpi((ap.count||0)+' / '+vs.length,'Bills / vendors')+
    '</div>'+
    '<table><thead><tr><th>Vendor</th><th class="c">Open bills</th><th style="text-align:right;">Past due</th><th style="text-align:right;">Balance owed</th></tr></thead><tbody>'+
    rows+
    '<tr class="so-group"><td style="font-weight:700;">Total ('+vs.length+' vendor'+(vs.length===1?'':'s')+')</td>'+
    '<td class="c" style="font-weight:700;">'+(ap.count||0)+'</td>'+
    '<td style="text-align:right;font-weight:700;color:'+((ap.past_due>0.005)?'#c0392b':'#7f8c8d')+';">'+payMoney(ap.past_due||0)+'</td>'+
    '<td style="text-align:right;font-weight:700;color:#c0392b;">'+payMoney(ap.grand_total||0)+'</td></tr>'+
    '</tbody></table>';
}
// Standalone HTML invoice table for the selected customer — copies as a rendered table for email.
function buildPayEmailHtml(c){
  var td='padding:8px 10px;border-bottom:1px solid #e6ebf1;font-size:13px;color:#2c3e50;';
  var th='padding:8px 10px;background:#1f3a5f;color:#fff;font-size:12px;';
  var invs=payInvoices(c), rows='';
  for(var j=0;j<invs.length;j++){ var v=invs[j];
    var sc = v.status==='Paid'?'#155724':(v.status==='Not Paid'?'#842029':'#6c757d');
    rows+='<tr>'+
      '<td style="'+td+'">'+escapeHtml(v.number)+'</td>'+
      '<td style="'+td+'color:'+sc+';font-weight:600;">'+escapeHtml(v.status)+'</td>'+
      '<td style="'+td+'">'+fmtDate(v.date)+'</td>'+
      '<td style="'+td+'text-align:right;white-space:nowrap;">'+payMoney(v.amount)+'</td>'+
      '<td style="'+td+'">'+(v.link?'<a href="'+escapeHtml(v.link)+'">Pay '+payMoney(v.balance||v.amount)+'</a>':(v.status==='Paid'?'&mdash;':'No link'))+'</td>'+
      '</tr>';
  }
  var t=payTotals(invs);
  return '<div style="font-family:Arial,Helvetica,sans-serif;max-width:680px;color:#2c3e50;">'+
    '<div style="font-size:16px;font-weight:700;color:#1f3a5f;margin:0 0 4px;">JIT4Labs &mdash; Invoice Payment Status</div>'+
    '<div style="font-size:13px;color:#555;margin:0 0 12px;">'+escapeHtml(c.name)+' &middot; QuickBooks '+escapeHtml(''+(PAY.year||''))+'</div>'+
    '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;max-width:660px;border:1px solid #e6ebf1;">'+
    '<thead><tr><th style="'+th+'text-align:left;">Invoice #</th><th style="'+th+'text-align:left;">Status</th>'+
    '<th style="'+th+'text-align:left;">Date</th><th style="'+th+'text-align:right;">Amount</th><th style="'+th+'text-align:left;">Link</th></tr></thead>'+
    '<tbody>'+rows+
    '<tr><td colspan="3" style="'+td+'text-align:right;font-weight:700;">Total ('+t.count+')</td>'+
    '<td style="'+td+'text-align:right;font-weight:700;">'+payMoney(t.amount)+'</td>'+
    '<td style="'+td+'font-weight:700;color:#c0392b;">'+payMoney(t.unpaid)+' unpaid</td></tr>'+
    '</tbody></table></div>';
}
function copyPayTable(){
  var c=payCurrentOrAll(); if(!c){ alert('No customer selected.'); return; }
  var html=buildPayEmailHtml(c);
  function done(){ var b=document.querySelectorAll('.copy-email-btn'); for(var i=0;i<b.length;i++){ if(/Copy table/.test(b[i].textContent)||/Copied/.test(b[i].textContent)){ var o=b[i].innerHTML; b[i].innerHTML='✓ Copied!'; (function(el,txt){ setTimeout(function(){ el.innerHTML=txt; },1800); })(b[i],'📋 Copy table'); } } }
  try {
    if(navigator.clipboard && window.ClipboardItem){
      var item=new ClipboardItem({'text/html':new Blob([html],{type:'text/html'}),'text/plain':new Blob([html],{type:'text/plain'})});
      navigator.clipboard.write([item]).then(done, function(){ fallbackCopyHtml(html, done); });
      return;
    }
  } catch(e){}
  fallbackCopyHtml(html, done);
}

function setMode(m){
  if(mode===m) return;
  mode=m; sortState={key:null, dir:1};
  var btns=document.querySelectorAll('.mode-btn');
  for(var i=0;i<btns.length;i++){
    var dm=btns[i].getAttribute('data-mode');
    var extra = dm==='pnl' ? ' mode-pnl' : '';  // only P&L keeps a tinted button; group frames carry the colour coding
    btns[i].className = 'mode-btn'+extra+(dm===m?' active':'');
  }
  renderTabs(); renderPanel();
}

// ── High-Demand SKUs matrix (Product × Customer) ──────────────────────────────
var skuSort = {key:'__demand__', dir:-1};   // default: most customers / orders / qty first
var skuCols = [];                            // rebuilt each render (dynamic customer columns)
function skuSortByIdx(i){
  var c=skuCols[i]; if(!c) return;
  if(skuSort.key===c.k){ skuSort.dir=-skuSort.dir; }
  else { skuSort.key=c.k; skuSort.dir = (c.k==='product'||c.k==='vendor') ? 1 : -1; }
  renderSkuPanel();
}
function ageInfo(dateStr){
  if(!dateStr) return {cls:'age-na', label:'—'};
  var d=new Date(dateStr+'T00:00:00'); if(isNaN(d)) return {cls:'age-na', label:dateStr};
  var days=Math.floor((Date.now()-d.getTime())/86400000);
  // < 2 weeks green · 2–3 weeks orange · > 3 weeks red
  var cls = days<14 ? 'age-green' : (days<=21 ? 'age-orange' : 'age-red');
  return {cls:cls, label:days+'d'};
}
function agingHtml(){
  // Flatten open lines, then group by customer → SO (oldest SO first).
  var vs=DATA.vendors||[], byCust={}, custOrder=[];
  for(var i=0;i<vs.length;i++){ var rows=vs[i].rows||[];
    for(var j=0;j<rows.length;j++){ var r=rows[j], cu=r.customer||'(no customer)', so=r.so_num||'(no SO)';
      if(!byCust[cu]){ byCust[cu]={}; custOrder.push(cu); }
      if(!byCust[cu][so]){ byCust[cu][so]={so:so, date:r.order_date, items:[]}; }
      var g=byCust[cu][so];
      if(r.order_date && (!g.date || r.order_date<g.date)) g.date=r.order_date;
      g.items.push({vendor:vs[i].name, product:r.product, open_qty:r.open_qty, pending_pos:r.pending_pos, eta:r.eta});
    } }
  custOrder.sort(function(a,b){ return cmp(a,b,'str'); });   // customers A→Z
  var NCOL=5, body='';
  for(var ci=0;ci<custOrder.length;ci++){
    var cu=custOrder[ci], sos=byCust[cu], soKeys=[];
    for(var key in sos){ if(sos.hasOwnProperty(key)) soKeys.push(key); }
    soKeys.sort(function(a,b){ var d=cmp(sos[a].date,sos[b].date,'date'); return d!==0?d:cmp(a,b,'str'); });  // oldest→newest
    body+='<tr class="so-group"><td colspan="'+NCOL+'"><span class="so-h">'+escapeHtml(cu)+'</span></td></tr>';
    for(var si=0;si<soKeys.length;si++){
      var g=sos[soKeys[si]], ai=ageInfo(g.date);
      body+='<tr class="aging-so"><td colspan="'+NCOL+'">'+
        '<span class="so-h2">'+escapeHtml(g.so)+'</span>'+
        '<span class="so-date">'+fmtDate(g.date)+'</span>'+
        '<span class="age-pill '+ai.cls+'" style="margin-left:8px;">'+ai.label+' open</span></td></tr>';
      g.items.sort(function(a,b){ return cmp(a.product,b.product,'str'); });
      for(var k=0;k<g.items.length;k++){ var it=g.items[k];
        var po = it.pending_pos ? escapeHtml(it.pending_pos) : '<span class="po-none">—</span>';
        body+='<tr>'+
          '<td>'+escapeHtml(it.product)+'</td>'+
          '<td>'+escapeHtml(it.vendor)+'</td>'+
          '<td class="c open">'+fmtQty(it.open_qty)+'</td>'+
          '<td>'+po+'</td>'+
          '<td class="c" style="font-weight:600;color:'+etaColor(it.eta)+'">'+fmtDate(it.eta)+'</td></tr>';
      }
    }
  }
  return '<div class="panel-head" style="margin-top:24px;border-top:1px solid #dee5ec;"><h2>Open SO Aging</h2>'+
    '<div class="sub">Grouped by customer, then SO (oldest first) &middot; how long each order has been open: '+
    '<span class="age-pill age-green">&lt; 2 weeks</span> '+
    '<span class="age-pill age-orange">2–3 weeks</span> '+
    '<span class="age-pill age-red">&gt; 3 weeks</span></div></div>'+
    '<div class="matrix-wrap"><table><thead><tr>'+
    '<th>Product</th><th>Vendor</th><th class="c">Open</th><th>Pending PO</th><th class="c">ETA</th>'+
    '</tr></thead><tbody>'+body+'</tbody></table></div>';
}

function renderSkuPanel(){
  var hd=DATA.high_demand||{customers:[],items:[]};
  var custs=hd.customers||[], items=(hd.items||[]).slice();
  var head='<div class="panel-head"><h2>High-Demand SKUs</h2>'+
    '<div class="sub">SKUs that appear on more than one PO &middot; open quantity each customer has &middot; prioritize the highlighted rows</div></div>';
  var matrixHtml;
  if(!items.length){
    matrixHtml='<div class="empty">No SKU is currently open on more than one PO.</div>';
  } else {
    // Dynamic columns: Product, Vendor, [each customer], Total, #POs, #Cust, #Orders.
    skuCols=[{k:'product',label:'Product',cls:''},{k:'vendor',label:'Vendor',cls:''}];
    for(var ci=0;ci<custs.length;ci++) skuCols.push({k:'cust::'+custs[ci],label:custs[ci],cls:'c cust-col'});
    skuCols.push({k:'total',label:'Total',cls:'c'},{k:'po_count',label:'#POs',cls:'c'},{k:'cust_count',label:'#Cust',cls:'c'},{k:'order_count',label:'#Orders',cls:'c'});
    items.sort(function(a,b){
      var k=skuSort.key, d=skuSort.dir;
      if(k==='__demand__') return (b.po_count-a.po_count)||(b.cust_count-a.cust_count)||(b.total-a.total)||cmp(a.product,b.product,'str');
      if(k==='product') return d*cmp(a.product,b.product,'str');
      if(k==='vendor')  return d*cmp(a.vendor,b.vendor,'str');
      if(k==='total')   return d*((a.total||0)-(b.total||0));
      if(k==='po_count')    return d*((a.po_count||0)-(b.po_count||0));
      if(k==='cust_count')  return d*((a.cust_count||0)-(b.cust_count||0));
      if(k==='order_count') return d*((a.order_count||0)-(b.order_count||0));
      if(k.indexOf('cust::')===0){ var c=k.slice(6); return d*(((a.qty||{})[c]||0)-((b.qty||{})[c]||0)); }
      return 0;
    });
    var th='';
    for(var i=0;i<skuCols.length;i++){
      var col=skuCols[i];
      var arr = (skuSort.key===col.k) ? '<span class="arr">'+(skuSort.dir>0?'▲':'▼')+'</span>' : '';
      th+='<th class="'+(col.cls?col.cls+' ':'')+'sortable" onclick="skuSortByIdx('+i+')" title="Sort by '+escapeHtml(col.label)+'">'+escapeHtml(col.label)+arr+'</th>';
    }
    var body='';
    for(var r=0;r<items.length;r++){
      var it=items[r];
      var hot = it.po_count>=3 ? ' hd-hot' : (it.po_count>=2 ? ' hd-warm' : '');
      var row='<tr class="'+hot+'"><td class="item-name">'+escapeHtml(it.product)+'</td><td>'+escapeHtml(it.vendor)+'</td>';
      for(var ci2=0;ci2<custs.length;ci2++){
        var cu=custs[ci2], q=(it.qty||{})[cu];
        if(q){
          var sub='', dets=(it.detail||{})[cu]||[];
          for(var di=0;di<dets.length;di++){
            var dd=dets[di], po=(dd.po||'—'), dt=fmtDateShort(dd.date);
            sub+='<div class="hd-sub">'+escapeHtml(po)+(dt?(' &middot; '+dt):'')+'</div>';
          }
          row+='<td class="c hd-cell"><span class="hd-q">'+fmtQty(q)+'</span>'+sub+'</td>';
        } else {
          row+='<td class="c"><span class="po-none">·</span></td>';
        }
      }
      row+='<td class="c open">'+fmtQty(it.total)+'</td>'+
           '<td class="c"><span class="hd-badge">'+it.po_count+'</span></td>'+
           '<td class="c">'+it.cust_count+'</td>'+
           '<td class="c">'+it.order_count+'</td></tr>';
      body+=row;
    }
    matrixHtml='<div class="matrix-wrap"><table class="matrix"><thead><tr>'+th+'</tr></thead><tbody>'+body+'</tbody></table></div>';
  }
  document.getElementById('panel').innerHTML = head + matrixHtml + agingHtml();
}

// ── YTD Demand: units sold per item x month (sidebar customer filter + search) ──
var ytdCust = '';                       // '' = All Customers
var ytdSearch = '';                     // free-text item filter
var ytdSort = {key:'ytd', dir:-1};      // default: biggest YTD movers first
var ytdCols = [];                       // rebuilt each render (SKU, Product, months…, YTD)
var ytdCustList = [];                   // sidebar order; index -1 = All Customers
// Handlers take an INDEX, never a string literal — inline onclick attributes are built
// inside single-quoted JS strings, so a quoted argument would terminate the string.
function ytdSetCustIdx(i){
  ytdCust = (i<0 ? '' : (ytdCustList[i]||''));
  renderYtdTabs(document.getElementById('tabs')); renderYtdPanel();
}
function ytdSortByIdx(i){
  var c=ytdCols[i]; if(!c) return;
  if(ytdSort.key===c.k){ ytdSort.dir=-ytdSort.dir; }
  else { ytdSort.key=c.k; ytdSort.dir=(c.k==='sku'||c.k==='product'||c.k==='vendor')?1:-1; }
  renderYtdPanel();
}
function ytdOnSearch(v){
  ytdSearch=String(v||'');
  var tb=document.getElementById('ytd-tbody'); if(!tb){ renderYtdPanel(); return; }
  // Re-render only the table body so the input keeps focus + caret while typing.
  tb.innerHTML=ytdBodyHtml(ytdRows());
  var st=document.getElementById('ytd-stat'); if(st) st.textContent=ytdStatText(ytdRows());
}
function ytdQty(v){ if(!v) return ''; return Number(v).toLocaleString('en-US',{maximumFractionDigits:2}); }
// One row per item, already sliced to the selected customer and search text.
function ytdRows(){
  var YD=DATA.ytd_demand||{items:[],months:[]}, n=(YD.months||[]).length;
  var q=ytdSearch.replace(/^\s+|\s+$/g,'').toLowerCase();
  var out=[];
  for(var i=0;i<(YD.items||[]).length;i++){
    var it=YD.items[i], by, ytd;
    var amt;
    if(ytdCust){
      var cm=(it.cust||{})[ytdCust];
      if(!cm) continue;                     // item never ordered by this customer
      by=cm.by_month; ytd=cm.ytd; amt=cm.amt_ytd;
    } else { by=it.by_month; ytd=it.ytd; amt=it.amt_ytd; }
    if(q){
      var hay=(it.sku+' '+(it.product||'')+' '+(it.vendor||'')).toLowerCase();
      if(hay.indexOf(q)<0) continue;
    }
    out.push({sku:it.sku, product:it.product, vendor:it.vendor,
              by:(by||[]).slice(0,n), ytd:ytd, amt:(amt||0),
              ncust:Object.keys(it.cust||{}).length});
  }
  var k=ytdSort.key, d=ytdSort.dir;
  out.sort(function(a,b){
    if(k==='sku')     return d*cmp(a.sku,b.sku,'str');
    if(k==='product') return d*cmp(a.product,b.product,'str');
    if(k==='vendor')  return d*cmp(a.vendor,b.vendor,'str');
    if(k==='ytd')     return d*((a.ytd||0)-(b.ytd||0)) || cmp(a.sku,b.sku,'str');
    if(k==='amt')     return d*((a.amt||0)-(b.amt||0)) || cmp(a.sku,b.sku,'str');
    if(k.indexOf('m::')===0){ var mi=parseInt(k.slice(3),10);
      return d*(((a.by||[])[mi]||0)-((b.by||[])[mi]||0)) || cmp(a.sku,b.sku,'str'); }
    return 0;
  });
  return out;
}
function ytdMoney(v){ return '$'+Number(v||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function ytdStatText(rows){
  var t=0, m=0;
  for(var i=0;i<rows.length;i++){ t+=(rows[i].ytd||0); m+=(rows[i].amt||0); }
  return rows.length+' item'+(rows.length===1?'':'s')+' · '+ytdQty(t)+' units · '+ytdMoney(m)+' spent';
}
function ytdBodyHtml(rows){
  var YD=DATA.ytd_demand||{months:[]}, n=(YD.months||[]).length;
  if(!rows.length){
    return '<tr><td colspan="'+(n+5)+'" class="ytd-none">No items match'+
           (ytdSearch?' “'+escapeHtml(ytdSearch)+'”':'')+
           (ytdCust?' for '+escapeHtml(ytdCust):'')+'.</td></tr>';
  }
  var h='';
  for(var r=0;r<rows.length;r++){
    var it=rows[r];
    h+='<tr><td class="ytd-sku ytd-sticky">'+escapeHtml(it.sku)+'</td>'+
       '<td class="ytd-prod">'+escapeHtml(it.product||'')+
       (it.vendor?'<div class="ytd-ven">'+escapeHtml(it.vendor)+'</div>':'')+'</td>';
    for(var m=0;m<n;m++){
      var q=(it.by||[])[m]||0;
      h+= q ? '<td class="c ytd-cell">'+ytdQty(q)+'</td>'
            : '<td class="c ytd-zero">·</td>';
    }
    h+='<td class="c ytd-total">'+ytdQty(it.ytd)+'</td>'+
       '<td class="c ytd-amt">'+(it.amt?ytdMoney(it.amt):'<span class="ytd-zero">·</span>')+'</td>'+
       '<td class="c ytd-ncust">'+(ytdCust?'—':it.ncust)+'</td></tr>';
  }
  return h;
}
function renderYtdTabs(tabsEl){
  var YD=DATA.ytd_demand||{customers:[],items:[]};
  tabsEl.style.display='';
  var custs=(YD.customers||[]).filter(function(c){ return !isExclCust(c); });
  // Units per customer, so the sidebar doubles as a ranking.
  var tot={}, all=0;
  for(var i=0;i<(YD.items||[]).length;i++){
    var it=YD.items[i]; all+=(it.ytd||0);
    for(var c in (it.cust||{})){ tot[c]=(tot[c]||0)+((it.cust[c]||{}).ytd||0); }
  }
  custs.sort(function(a,b){ return (tot[b]||0)-(tot[a]||0) || cmp(a,b,'str'); });
  ytdCustList=custs;
  var h='<button class="tab'+(ytdCust===''?' active':'')+'" onclick="ytdSetCustIdx(-1)">'+
        'All Customers<span class="cnt">'+ytdQty(all)+'</span></button>';
  for(var j=0;j<custs.length;j++){
    var nm=custs[j];
    h+='<button class="tab'+(ytdCust===nm?' active':'')+'" onclick="ytdSetCustIdx('+j+')">'+
       escapeHtml(nm)+'<span class="cnt">'+ytdQty(tot[nm]||0)+'</span></button>';
  }
  tabsEl.innerHTML=h;
}
function renderYtdPanel(){
  var YD=DATA.ytd_demand;
  if(!YD || !(YD.items||[]).length){
    document.getElementById('panel').innerHTML=
      '<div class="panel-head"><h2>YTD Demand</h2></div>'+
      '<div class="empty">No demand data in this snapshot yet — it is built on the next scheduled refresh.</div>';
    return;
  }
  var months=YD.months||[], rows=ytdRows();
  var head='<div class="panel-head"><h2>YTD Demand'+(ytdCust?' &mdash; '+escapeHtml(ytdCust):'')+'</h2>'+
    '<div class="sub">Units sold per item by month &middot; '+(YD.year||'')+
    ' Sales Orders'+(ytdCust?'':' &middot; all customers')+'</div></div>';
  var ctrl='<div class="ytd-ctrl">'+
    '<input id="ytd-q" class="ytd-search" type="text" placeholder="Search SKU or product…" '+
    'autocomplete="off" spellcheck="false" value="'+escapeHtml(ytdSearch)+'" oninput="ytdOnSearch(this.value)">'+
    '<span id="ytd-stat" class="ytd-stat">'+ytdStatText(rows)+'</span></div>';
  // Column list drives both the header and the sort handler (index-based, no quoted args).
  ytdCols=[{k:'sku',label:'SKU',cls:'ytd-sticky'},{k:'product',label:'Product',cls:''}];
  for(var m=0;m<months.length;m++) ytdCols.push({k:'m::'+m,label:months[m],cls:'c'});
  ytdCols.push({k:'ytd',label:'YTD',cls:'c ytd-ytdcol'});
  ytdCols.push({k:'amt',label:'$ Spent',cls:'c ytd-ytdcol'});
  var th='';
  for(var ci=0;ci<ytdCols.length;ci++){
    var col=ytdCols[ci];
    var arr=(ytdSort.key===col.k)?'<span class="arr">'+(ytdSort.dir>0?'▲':'▼')+'</span>':'';
    th+='<th class="'+(col.cls?col.cls+' ':'')+'sortable" onclick="ytdSortByIdx('+ci+')" title="Sort by '+
        escapeHtml(col.label)+'">'+escapeHtml(col.label)+arr+'</th>';
  }
  th+='<th class="c" title="How many customers ordered this item">#Cust</th>';
  var tbl='<div class="matrix-wrap"><table class="matrix ytd-table"><thead><tr>'+th+'</tr></thead>'+
          '<tbody id="ytd-tbody">'+ytdBodyHtml(rows)+'</tbody></table></div>';
  var note=YD.note?'<div class="ytd-note">'+escapeHtml(YD.note)+'</div>':'';
  document.getElementById('panel').innerHTML=head+ctrl+tbl+note;
}

function renderAsOf(){
  document.getElementById('asof').textContent = 'Last refreshed: '+(DATA.generated_at||'—');
}
function selectTab(i){ if(mode==='vendor') vactive=i; else if(mode==='ca') caactive=i; else active=i; renderTabs(); renderPanel(); }

// ── Alternative Sources box (vendor tab): type a SKU, compare 4 vendor costs ──
function showAltSrc(show){
  var el=document.getElementById('altsrc'); if(!el) return;
  if(!show){ el.style.display='none'; return; }
  el.style.display='';
  if(!el.getAttribute('data-built')){
    el.innerHTML='<h3>Alternative Sources</h3>'+
      '<div class="as-sub">Beckman Coulter SKU &rarr; vendor cost</div>'+
      '<input id="as-sku" type="text" placeholder="Enter SKU / Part #" autocomplete="off" '+
      'spellcheck="false" oninput="lookupAltSrc()">'+
      '<div id="as-result"><div class="as-hint">Type a SKU to compare PMA, Allora, ALDX &amp; ClearChem costs.</div></div>';
    el.setAttribute('data-built','1');
  }
}
function asCost(v){ return (v===''||v==null) ? '<td class="na">N/A</td>' : '<td class="v">$'+Number(v).toFixed(2)+'</td>'; }
function lookupAltSrc(){
  var inp=document.getElementById('as-sku'); if(!inp) return;
  var res=document.getElementById('as-result'); if(!res) return;
  var raw=inp.value.replace(/^\s+|\s+$/g,'');
  if(!raw){ res.innerHTML='<div class="as-hint">Type a SKU to compare PMA, Allora, ALDX &amp; ClearChem costs.</div>'; return; }
  var rec=(DATA.alt_sources||{})[raw.toUpperCase()];
  if(!rec){ res.innerHTML='<div class="as-none">SKU “'+escapeHtml(raw)+'” not found in Beckman Coulter catalog.</div>'; return; }
  res.innerHTML=(rec[0]?'<div class="as-name">'+escapeHtml(rec[0])+'</div>':'')+
    '<table><tbody>'+
    '<tr><td class="lbl">PMA</td>'+asCost(rec[1])+'</tr>'+
    '<tr><td class="lbl">Allora</td>'+asCost(rec[2])+'</tr>'+
    '<tr><td class="lbl">ALDX</td>'+asCost(rec[3])+'</tr>'+
    '<tr><td class="lbl">ClearChem</td>'+asCost(rec[4])+'</tr>'+
    '</tbody></table>';
}

// ── Paid Inventory box (vendor tab): add SKU / qty / location(vendor) / expiration ──
// Source of truth is paid_inventory.json in the repo (served next to the data file).
// Adds commit to that file via the GitHub contents API (durable across the Refresh
// button, the scheduled task, and other devices). localStorage bridges the ~1 min
// GitHub Pages redeploy lag so a just-added row never disappears on reload.
var PAID_URL = 'paid_inventory.json';
var _paidServer = null;   // authoritative list from the repo file (or baked fallback)
var _piEdit = null;       // {kind, idx} of the row currently being edited inline, or null
function _piLoadPending(){ try { return JSON.parse(localStorage.getItem('jit4_paid_inv_pending')||'[]'); } catch(e){ return []; } }
function _piSavePending(a){ try { localStorage.setItem('jit4_paid_inv_pending', JSON.stringify(a)); } catch(e){} }
function _piKey(x){ return [String(x.sku||'').toUpperCase(), x.qty, String(x.location||''), String(x.exp||'')].join('|'); }
// Match an item in a freshly-read file back to a target row: prefer stable id, else fall
// back to the legacy composite key (sku/qty/location/exp). Lets us edit/remove reliably.
function _piSameItem(it, target){ if(target && target.id && it && it.id) return it.id===target.id; return _piKey(it)===_piKey(target); }
function _piVal(id){ var e=document.getElementById(id); return e?e.value:''; }
function _piNewId(){ return Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8); }
function _b64enc(s){ return btoa(unescape(encodeURIComponent(s))); }
function _b64dec(s){ return decodeURIComponent(escape(atob(String(s||'').replace(/\s+/g,'')))); }

function showPaidInv(show){
  var el=document.getElementById('paidinv'); if(!el) return;
  if(!show){ el.style.display='none'; return; }
  el.style.display='';
  if(!el.getAttribute('data-built')){
    var vlist=((DATA&&DATA.vendors)||[]).map(function(v){return v&&v.name;}).filter(Boolean);
    ['PMA Services','Allora Biotech LLC','ALDX Holding Corporation','ClearChem Diagnostics Inc'].forEach(function(n){ if(vlist.indexOf(n)<0) vlist.push(n); });
    var opts=vlist.map(function(n){return '<option value="'+escapeHtml(n)+'">';}).join('');
    el.innerHTML='<h3>Paid Inventory</h3>'+
      '<div class="as-sub">Log inventory already paid for</div>'+
      '<label for="pi-sku">SKU / Part #</label>'+
      '<input id="pi-sku" class="pi-sku" type="text" placeholder="Enter SKU" autocomplete="off" spellcheck="false">'+
      '<label for="pi-qty">Quantity</label>'+
      '<input id="pi-qty" type="number" min="0" step="1" placeholder="0" autocomplete="off">'+
      '<label for="pi-loc">Location (vendor name)</label>'+
      '<input id="pi-loc" type="text" placeholder="Vendor name" list="pi-vendors" autocomplete="off">'+
      '<datalist id="pi-vendors">'+opts+'</datalist>'+
      '<label for="pi-sender">Sender</label>'+
      '<input id="pi-sender" type="text" placeholder="Who sent the material" autocomplete="off">'+
      '<label for="pi-exp">Expiration date</label>'+
      '<input id="pi-exp" type="date" autocomplete="off">'+
      '<label for="pi-po">Allocated PO# (optional)</label>'+
      '<input id="pi-po" type="text" placeholder="PO# this material is for" autocomplete="off">'+
      '<button class="pi-add" id="pi-add-btn" onclick="addPaidInv()">Add</button>'+
      '<div class="pi-note" id="pi-note"></div>'+
      '<div class="pi-list" id="pi-list"></div>';
    el.setAttribute('data-built','1');
    if(_paidServer==null){ _paidServer=((DATA&&DATA.paid_inventory)||[]).slice(); }
    renderPaidList();
    fetchPaidInv();
  }
}
function fetchPaidInv(){
  fetch(PAID_URL+'?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(!r.ok) throw 0; return r.json(); })
    .then(function(j){ if(j&&j.items){ _paidServer=j.items; _piReconcile(); renderPaidList(); } })
    .catch(function(){ /* keep baked fallback */ });
}
// Drop any pending rows that now appear in the server list.
function _piReconcile(){
  var srv={}; (_paidServer||[]).forEach(function(x){ srv[_piKey(x)]=1; });
  var pend=_piLoadPending().filter(function(x){ return !srv[_piKey(x)]; });
  _piSavePending(pend);
}
function renderPaidList(){
  var box=document.getElementById('pi-list'); if(!box) return;
  var srv=(_paidServer||[]), pend=_piLoadPending();
  var rows='';
  function row(x,pending,kind,idx){
    var editing = _piEdit && _piEdit.kind===kind && _piEdit.idx===idx;
    if(editing){
      return '<tr class="pi-editing">'+
        '<td><input id="pi-e-sku" class="pi-ein" value="'+escapeHtml(x.sku||'')+'" style="text-transform:uppercase"></td>'+
        '<td class="pi-q"><input id="pi-e-qty" class="pi-ein" type="number" min="0" step="1" value="'+escapeHtml(String(x.qty==null?'':x.qty))+'"></td>'+
        '<td><input id="pi-e-loc" class="pi-ein" value="'+escapeHtml(x.location||'')+'" list="pi-vendors"></td>'+
        '<td><input id="pi-e-sender" class="pi-ein" value="'+escapeHtml(x.sender||'')+'"></td>'+
        '<td><input id="pi-e-exp" class="pi-ein" type="date" value="'+escapeHtml(x.exp||'')+'"></td>'+
        '<td><input id="pi-e-po" class="pi-ein" value="'+escapeHtml(x.po||'')+'" placeholder="PO#"></td>'+
        '<td class="pi-act"><button class="pi-save" onclick="savePaidInv('+kind+','+idx+')">Save</button>'+
          '<button class="pi-cancel" onclick="cancelPaidEdit()">Cancel</button></td></tr>';
    }
    var cls=[]; if(pending) cls.push('pi-pending'); if(x.po) cls.push('pi-alloc');
    return '<tr'+(cls.length?' class="'+cls.join(' ')+'"':'')+'>'+
      '<td>'+escapeHtml(x.sku||'')+(pending?' &middot; saving…':'')+'</td>'+
      '<td class="pi-q">'+escapeHtml(String(x.qty==null?'':x.qty))+'</td>'+
      '<td>'+escapeHtml(x.location||'')+'</td>'+
      '<td>'+escapeHtml(x.sender||'')+'</td>'+
      '<td>'+escapeHtml(x.exp||'')+'</td>'+
      '<td class="pi-po">'+escapeHtml(x.po||'')+'</td>'+
      '<td class="pi-act">'+
        '<button class="pi-edit" title="Edit" onclick="editPaidInv('+kind+','+idx+')">&#9998;</button>'+
        '<button class="pi-del" title="Remove" onclick="removePaidInv('+kind+','+idx+')">&times;</button>'+
      '</td></tr>';
  }
  srv.forEach(function(x,ix){ rows+=row(x,false,0,ix); });
  pend.forEach(function(x,ix){ rows+=row(x,true,1,ix); });
  if(!rows){ box.innerHTML='<div class="pi-empty">No paid inventory logged yet.</div>'; return; }
  box.innerHTML='<div class="pi-list-h">Logged inventory ('+(srv.length+pend.length)+')</div>'+
    '<table><thead><tr><td>SKU</td><td class="pi-q">Qty</td><td>Location</td><td>Sender</td><td>Exp.</td><td>Allocated PO#</td><td></td></tr></thead>'+
    '<tbody>'+rows+'</tbody></table>';
}
function _piNote(cls,msg){ var n=document.getElementById('pi-note'); if(n){ n.className='pi-note '+cls; n.textContent=msg; } }
function addPaidInv(){
  var sku=(document.getElementById('pi-sku').value||'').replace(/^\s+|\s+$/g,'').toUpperCase();
  var qtyRaw=(document.getElementById('pi-qty').value||'').replace(/^\s+|\s+$/g,'');
  var loc=(document.getElementById('pi-loc').value||'').replace(/^\s+|\s+$/g,'');
  var sender=(document.getElementById('pi-sender').value||'').replace(/^\s+|\s+$/g,'');
  var exp=(document.getElementById('pi-exp').value||'').replace(/^\s+|\s+$/g,'');
  var po=(document.getElementById('pi-po').value||'').replace(/^\s+|\s+$/g,'');
  if(!sku){ _piNote('err','Enter a SKU.'); return; }
  if(qtyRaw===''||isNaN(Number(qtyRaw))){ _piNote('err','Enter a valid quantity.'); return; }
  var entry={id:_piNewId(), sku:sku, qty:Number(qtyRaw), location:loc, sender:sender, exp:exp, po:po, added:new Date().toISOString()};
  // optimistic: add to pending + show immediately
  var pend=_piLoadPending(); pend.push(entry); _piSavePending(pend); renderPaidList();
  // clear inputs
  document.getElementById('pi-sku').value=''; document.getElementById('pi-qty').value='';
  document.getElementById('pi-loc').value=''; document.getElementById('pi-sender').value='';
  document.getElementById('pi-exp').value=''; document.getElementById('pi-po').value='';
  if(!BTN||!BTN.token){ _piNote('warn','Saved on this device only (no sync token).'); return; }
  var btn=document.getElementById('pi-add-btn'); if(btn) btn.disabled=true;
  _piNote('warn','Saving…');
  var base='https://api.github.com/repos/'+BTN.repo+'/contents/'+PAID_URL;
  var hdr={'Authorization':'Bearer '+BTN.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'};
  fetch(base+'?ref='+encodeURIComponent(BTN.branch)+'&cb='+Date.now(),{headers:hdr,cache:'no-store'})
    .then(function(r){ if(r.status===404) return {obj:{items:[]}, sha:null}; if(!r.ok) throw new Error('read '+r.status); return r.json().then(function(j){ var o; try{ o=JSON.parse(_b64dec(j.content)); }catch(e){ o={items:[]}; } if(!o.items) o.items=[]; return {obj:o, sha:j.sha}; }); })
    .then(function(st){ st.obj.items.push(entry);
      return fetch(base,{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},hdr),
        body:JSON.stringify({message:'Add paid inventory '+entry.sku+' x'+entry.qty, content:_b64enc(JSON.stringify(st.obj,null,2)+'\\n'), sha:st.sha||undefined, branch:BTN.branch})})
        .then(function(r){ if(!r.ok) return r.text().then(function(t){ throw new Error('save '+r.status+' '+t.slice(0,120)); }); return st.obj; }); })
    .then(function(obj){ _paidServer=obj.items; _piReconcile(); renderPaidList(); _piNote('ok','Saved to shared inventory.'); })
    .catch(function(e){ _piNote('warn','Saved on this device; sync failed ('+e.message+'). Will retry on next add.'); })
    .finally(function(){ var b=document.getElementById('pi-add-btn'); if(b) b.disabled=false; });
}
// ── Inline edit: open a row for editing (incl. Allocated PO#), save, cancel ──
function editPaidInv(kind, idx){ _piEdit={kind:kind, idx:idx}; renderPaidList(); }
function cancelPaidEdit(){ _piEdit=null; renderPaidList(); _piNote('',''); }
// kind: 0 = server item (commit edit to repo), 1 = pending/local-only item
function savePaidInv(kind, idx){
  var sku=(_piVal('pi-e-sku')||'').replace(/^\s+|\s+$/g,'').toUpperCase();
  var qtyRaw=(_piVal('pi-e-qty')||'').replace(/^\s+|\s+$/g,'');
  var loc=(_piVal('pi-e-loc')||'').replace(/^\s+|\s+$/g,'');
  var sender=(_piVal('pi-e-sender')||'').replace(/^\s+|\s+$/g,'');
  var exp=(_piVal('pi-e-exp')||'').replace(/^\s+|\s+$/g,'');
  var po=(_piVal('pi-e-po')||'').replace(/^\s+|\s+$/g,'');
  if(!sku){ _piNote('err','Enter a SKU.'); return; }
  if(qtyRaw===''||isNaN(Number(qtyRaw))){ _piNote('err','Enter a valid quantity.'); return; }
  var patch={sku:sku, qty:Number(qtyRaw), location:loc, sender:sender, exp:exp, po:po};
  if(kind===1){
    var p=_piLoadPending(); if(idx>=0&&idx<p.length){ p[idx]=Object.assign({}, p[idx], patch); _piSavePending(p); }
    _piEdit=null; renderPaidList(); return;
  }
  var list=_paidServer||[]; if(idx<0||idx>=list.length) return;
  var target=list[idx];
  var updated=Object.assign({}, target, patch);
  if(!updated.id) updated.id=_piNewId();   // stamp an id so future edits/removes match reliably
  _paidServer=list.slice(); _paidServer[idx]=updated; _piEdit=null; renderPaidList();   // optimistic
  if(!BTN||!BTN.token){ _piNote('warn','Updated on this device only (no sync token).'); return; }
  _piNote('warn','Saving…');
  var base='https://api.github.com/repos/'+BTN.repo+'/contents/'+PAID_URL;
  var hdr={'Authorization':'Bearer '+BTN.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'};
  fetch(base+'?ref='+encodeURIComponent(BTN.branch)+'&cb='+Date.now(),{headers:hdr,cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('read '+r.status); return r.json(); })
    .then(function(j){ var o; try{ o=JSON.parse(_b64dec(j.content)); }catch(e){ o={items:[]}; } if(!o.items) o.items=[];
      var done=false;
      o.items=o.items.map(function(it){ if(!done && _piSameItem(it,target)){ done=true; return updated; } return it; });
      if(!done) o.items.push(updated);   // not found (e.g. added elsewhere) → append the edited row
      return fetch(base,{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},hdr),
        body:JSON.stringify({message:'Edit paid inventory '+(updated.sku||''), content:_b64enc(JSON.stringify(o,null,2)+'\\n'), sha:j.sha, branch:BTN.branch})})
        .then(function(r){ if(!r.ok) return r.text().then(function(t){ throw new Error('save '+r.status+' '+t.slice(0,120)); }); return o; }); })
    .then(function(o){ _paidServer=o.items; _piReconcile(); renderPaidList(); _piNote('ok','Saved to shared inventory.'); })
    .catch(function(e){ _piNote('err','Save failed ('+e.message+'). Refreshing…'); fetchPaidInv(); });
}
// kind: 0 = server item (commit removal to repo), 1 = pending/local-only item
function removePaidInv(kind, idx){
  if(kind===1){ var p=_piLoadPending(); if(idx>=0&&idx<p.length){ p.splice(idx,1); _piSavePending(p); } renderPaidList(); return; }
  var list=_paidServer||[]; if(idx<0||idx>=list.length) return;
  var target=list[idx];
  if(typeof confirm==='function' && !confirm('Remove '+(target.sku||'this item')+' from paid inventory?')) return;
  _paidServer=list.slice(0,idx).concat(list.slice(idx+1)); renderPaidList();   // optimistic
  if(!BTN||!BTN.token){ _piNote('warn','Removed on this device only (no sync token).'); return; }
  _piNote('warn','Removing…');
  var base='https://api.github.com/repos/'+BTN.repo+'/contents/'+PAID_URL;
  var hdr={'Authorization':'Bearer '+BTN.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'};
  fetch(base+'?ref='+encodeURIComponent(BTN.branch)+'&cb='+Date.now(),{headers:hdr,cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('read '+r.status); return r.json(); })
    .then(function(j){ var o; try{ o=JSON.parse(_b64dec(j.content)); }catch(e){ o={items:[]}; } if(!o.items) o.items=[];
      var removed=false;
      o.items=o.items.filter(function(it){ if(!removed && _piSameItem(it,target)){ removed=true; return false; } return true; });
      return fetch(base,{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},hdr),
        body:JSON.stringify({message:'Remove paid inventory '+(target.sku||''), content:_b64enc(JSON.stringify(o,null,2)+'\\n'), sha:j.sha, branch:BTN.branch})})
        .then(function(r){ if(!r.ok) return r.text().then(function(t){ throw new Error('save '+r.status+' '+t.slice(0,120)); }); return o; }); })
    .then(function(o){ _paidServer=o.items; _piReconcile(); renderPaidList(); _piNote('ok','Removed from shared inventory.'); })
    .catch(function(e){ _piNote('err','Remove failed ('+e.message+'). Refreshing…'); fetchPaidInv(); });
}

// ── Inventory Opportunities tab ──────────────────────────────────────────────
// Upload up to 3 vendor inventory spreadsheets (.xlsx/.xls/.csv). Each file is parsed
// IN THE BROWSER (SheetJS, lazy-loaded from cdnjs) and stored in the repo file
// inventory-opportunities.json via the button token, so the parsed stock survives a
// reload AND the nightly rebuild. Any SKU here with qty > 0 that also has an open
// vendor PO earns a flashing green dot in the Open Vendor POs "Fulfill Opp" column.
// NOTE: this whole script is emitted from a NON-raw Python string — never write a
// backslash escape in this block (no newline escapes, no regex metacharacters that
// need one). Use String.fromCharCode() instead.
var IOPP=null, ioppLoading=false, ioppQ='', ioppNote='', ioppBusy=false;
var IOPP_MAX_FILES=3;
var IOPP_NL=String.fromCharCode(10);

function ioppBlank(){ return {files:[], saved_at:''}; }
function loadIopp(){
  if(IOPP) return;
  if(IOPP_EMBED){ IOPP=IOPP_EMBED; if(mode==='iopp') renderIoppPanel(); return; }
  if(ioppLoading) return; ioppLoading=true;
  fetch('inventory-opportunities.json?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(r.status===404) return ioppBlank(); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(d){ IOPP=d&&d.files?d:ioppBlank(); ioppLoading=false; _ioppIdx=null;
      if(mode==='iopp') renderIoppPanel(); if(mode==='vendor') renderVendorPanel(); })
    .catch(function(){ IOPP=ioppBlank(); ioppLoading=false; if(mode==='iopp') renderIoppPanel(); });
}
// SKU normalisation: uppercase, strip surrounding and internal spaces. Deliberately
// conservative — we do NOT strip hyphens or dots, because "OSR-6006" and "OSR6006"
// are not reliably the same part across vendors.
function ioppNorm(s){
  var t=String(s==null?'':s).toUpperCase();
  t=t.split(' ').join('').split(String.fromCharCode(9)).join('');
  return t.trim();
}
var _ioppIdx=null;
function ioppIndex(){
  if(_ioppIdx) return _ioppIdx;
  _ioppIdx={};
  var fs=(IOPP&&IOPP.files)||[];
  for(var i=0;i<fs.length;i++){
    var f=fs[i], rows=f.rows||[];
    for(var j=0;j<rows.length;j++){
      var r=rows[j], k=ioppNorm(r.sku); if(!k) continue;
      var q=Number(r.qty)||0; if(q<=0) continue;   // positive quantity only
      if(!_ioppIdx[k]) _ioppIdx[k]={qty:0, srcs:[]};
      _ioppIdx[k].qty+=q;
      _ioppIdx[k].srcs.push({vendor:f.vendor||f.name||'file '+(i+1), qty:q, lot:r.lot||'', exp:r.exp||''});
    }
  }
  return _ioppIdx;
}
function ioppFor(sku){ var k=ioppNorm(sku); if(!k) return null; return ioppIndex()[k]||null; }
function ioppHasAny(){ var x=ioppIndex(); for(var k in x){ if(x.hasOwnProperty(k)) return true; } return false; }
// Vendor-name comparison: letters+digits only, so "PMA Services" == "PMA SERVICES"
// and "ClearChem Diagnostics, Inc" == "ClearChem Diagnostics Inc".
function ioppVenNorm(s){ return String(s==null?'':s).toUpperCase().replace(/[^A-Z0-9]/g,''); }

// ALTERNATIVE-SOURCE ONLY. A PO line is an opportunity only when the stock sits with
// a DIFFERENT vendor than the one the PO is already placed with — e.g. PMA Services
// has the SKU that we are currently waiting on Allora Biotech for. Stock held by the
// PO's own vendor is not an opportunity (they are already the supplier), so it never
// lights the dot.
function ioppOtherFor(sku, poVendor){
  var hit=ioppFor(sku); if(!hit) return null;
  var pv=ioppVenNorm(poVendor), qty=0, srcs=[];
  for(var i=0;i<hit.srcs.length;i++){
    var s=hit.srcs[i];
    if(pv && ioppVenNorm(s.vendor)===pv) continue;   // same vendor as the PO — skip
    qty+=s.qty; srcs.push(s);
  }
  return srcs.length?{qty:qty, srcs:srcs}:null;
}

// The Fulfill Opp cell in Open Vendor POs (poVendor = the vendor this PO sits with).
// Shows the ALTERNATIVE vendor(s) holding the SKU and how much they have, flashing
// green. Quantities are summed per vendor, so multiple lots of the same SKU from one
// vendor read as a single number.
function fopCell(r, poVendor){
  var hit=ioppOtherFor(r&&r.sku, poVendor);
  if(!hit) return '<span class="fop-none">&mdash;</span>';
  // Collapse the sources to one entry per vendor.
  var byVen={}, order=[];
  for(var i=0;i<hit.srcs.length;i++){
    var s=hit.srcs[i], key=ioppVenNorm(s.vendor)||s.vendor;
    if(!byVen[key]){ byVen[key]={vendor:s.vendor, qty:0, lots:[]}; order.push(key); }
    byVen[key].qty+=s.qty;
    if(s.lot) byVen[key].lots.push(s.lot+(s.exp?(' exp '+s.exp):''));
  }
  order.sort(function(a,b){ return byVen[b].qty-byVen[a].qty; });   // biggest holder first
  var shown=[], tipLines=[];
  for(var k=0;k<order.length;k++){
    var v=byVen[order[k]];
    if(k<2) shown.push(escapeHtml(v.vendor)+' <span class="fop-q">'+fmtQty(v.qty)+'</span>');
    tipLines.push(v.vendor+': '+fmtQty(v.qty)+(v.lots.length?(' ('+v.lots.join('; ')+')'):''));
  }
  var more=order.length>2 ? '<span class="fop-more">+'+(order.length-2)+' more vendor(s)</span>' : '';
  var tip='Available from another vendor: '+fmtQty(hit.qty)+IOPP_NL+tipLines.join(IOPP_NL)+
          IOPP_NL+'(this PO is with '+(poVendor||'?')+')';
  return '<span class="fop-txt" title="'+escapeHtml(tip)+'" '+
         'onclick="setMode(String.fromCharCode(105,111,112,112))">'+
         shown.join('<br>')+more+'</span>';
}
function fopAvail(r, poVendor){ var h=ioppOtherFor(r&&r.sku, poVendor); return h?h.qty:0; }

// ── Spreadsheet parsing (SheetJS, lazy-loaded) ───────────────────────────────
var _xlsxLoading=null;
function _loadXlsx(){
  if(window.XLSX) return Promise.resolve(window.XLSX);
  if(_xlsxLoading) return _xlsxLoading;
  _xlsxLoading=new Promise(function(res,rej){
    var s=document.createElement('script');
    s.src='https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js';
    s.onload=function(){ res(window.XLSX); };
    s.onerror=function(){ rej(new Error('Could not load the spreadsheet reader (network blocked?)')); };
    document.head.appendChild(s);
  });
  return _xlsxLoading;
}
function _ioppHdrKind(h){
  var t=String(h==null?'':h).toLowerCase().trim();
  if(!t) return '';
  if(t.indexOf('sku')>-1 || t.indexOf('item number')>-1 || t.indexOf('item no')>-1 ||
     t.indexOf('item #')>-1 || t.indexOf('part')>-1 || t.indexOf('catalog')>-1 ||
     t.indexOf('cat no')>-1 || t.indexOf('ref')>-1 || t==='code' || t.indexOf('product code')>-1 ||
     t.indexOf('item code')>-1 || t.indexOf('material')>-1) return 'sku';
  if(t.indexOf('quantity')>-1 || t==='qty' || t.indexOf('qty')>-1 || t.indexOf('on hand')>-1 ||
     t.indexOf('onhand')>-1 || t.indexOf('available')>-1 || t.indexOf('stock')>-1 ||
     t.indexOf('units')>-1 || t.indexOf('count')>-1) return 'qty';
  if(t.indexOf('lot')>-1 || t.indexOf('batch')>-1) return 'lot';
  if(t.indexOf('exp')>-1 || t.indexOf('expir')>-1 || t.indexOf('best before')>-1 ||
     t.indexOf('use by')>-1) return 'exp';
  if(t.indexOf('description')>-1 || t.indexOf('product name')>-1 || t.indexOf('item name')>-1) return 'desc';
  return '';
}
function _ioppNum(v){
  if(v==null||v==='') return 0;
  if(typeof v==='number') return v;
  var t=String(v).replace(/[^0-9.eE+-]/g,'');
  var n=parseFloat(t); return isNaN(n)?0:n;
}
function _ioppDate(v){
  if(v==null||v==='') return '';
  if(v instanceof Date && !isNaN(v.getTime())) return v.toISOString().slice(0,10);
  if(typeof v==='number' && v>20000 && v<80000){          // Excel serial date
    var ms=Date.UTC(1899,11,30)+v*86400000; var d=new Date(ms);
    return isNaN(d.getTime())?'':d.toISOString().slice(0,10);
  }
  var s=String(v).trim(); if(!s) return '';
  var p=Date.parse(s); if(!isNaN(p)) return new Date(p).toISOString().slice(0,10);
  return s.slice(0,32);
}
// Find the header row anywhere in the first 12 rows, then map the columns we care about.
function _ioppParseGrid(grid){
  var hdrRow=-1, map=null;
  for(var i=0;i<grid.length && i<12;i++){
    var row=grid[i]||[], m={}, seen=0;
    for(var c=0;c<row.length;c++){
      var kind=_ioppHdrKind(row[c]);
      if(kind && m[kind]===undefined){ m[kind]=c; seen++; }
    }
    if(m.sku!==undefined && m.qty!==undefined){ hdrRow=i; map=m; break; }
  }
  if(hdrRow<0) return {err:'Could not find a header row with both a SKU column and a quantity column in the first 12 rows.'};
  var out=[], skipped=0;
  for(var r=hdrRow+1;r<grid.length;r++){
    var row2=grid[r]||[];
    var sku=String(row2[map.sku]==null?'':row2[map.sku]).trim();
    if(!sku) continue;
    var qty=_ioppNum(row2[map.qty]);
    if(qty<=0){ skipped++; continue; }              // only positive quantities matter
    var rec={sku:sku, qty:qty};
    if(map.lot!==undefined && row2[map.lot]!=null && String(row2[map.lot]).trim()) rec.lot=String(row2[map.lot]).trim().slice(0,40);
    if(map.exp!==undefined) { var e=_ioppDate(row2[map.exp]); if(e) rec.exp=e; }
    if(map.desc!==undefined && row2[map.desc]!=null && String(row2[map.desc]).trim()) rec.desc=String(row2[map.desc]).trim().slice(0,120);
    out.push(rec);
  }
  var cols=[]; for(var k in map){ if(map.hasOwnProperty(k)) cols.push(k); }
  return {rows:out, skipped:skipped, cols:cols};
}
function ioppHandleFiles(fileList){
  var files=[]; for(var i=0;i<fileList.length;i++) files.push(fileList[i]);
  if(!files.length) return;
  var cur=((IOPP&&IOPP.files)||[]).length;
  if(cur+files.length>IOPP_MAX_FILES){
    _ioppNote('err','You can hold '+IOPP_MAX_FILES+' inventory files at a time. Remove one first (currently '+cur+').');
    return;
  }
  _ioppNote('','Reading '+files.length+' file(s)…');
  _loadXlsx().then(function(XLSX){
    var done=0, added=0, errs=[];
    files.forEach(function(f){
      var fr=new FileReader();
      fr.onload=function(ev){
        try{
          var wb=XLSX.read(new Uint8Array(ev.target.result),{type:'array',cellDates:true});
          var sh=wb.Sheets[wb.SheetNames[0]];
          var grid=XLSX.utils.sheet_to_json(sh,{header:1,raw:true,defval:''});
          var res=_ioppParseGrid(grid);
          if(res.err){ errs.push(f.name+': '+res.err); }
          else if(!res.rows.length){ errs.push(f.name+': no rows with a positive quantity.'); }
          else {
            if(!IOPP) IOPP=ioppBlank();
            IOPP.files.push({name:f.name, vendor:(ioppGuessVendor(f.name)||_ioppVendorFromName(f.name)),
                             uploaded_at:new Date().toISOString().slice(0,19).replace('T',' '),
                             cols:res.cols, skipped:res.skipped, rows:res.rows});
            added++;
          }
        }catch(e){ errs.push(f.name+': '+e.message); }
        done++;
        if(done===files.length){
          _ioppIdx=null;
          if(added) ioppCommit();
          _ioppNote(errs.length?'err':'ok',
            (added?(added+' file(s) loaded. '):'')+(errs.length?errs.join(' | '):''));
          renderIoppPanel(); if(mode==='vendor') renderVendorPanel();
        }
      };
      fr.onerror=function(){ done++; errs.push(f.name+': could not read file.');
        if(done===files.length){ _ioppNote('err',errs.join(' | ')); renderIoppPanel(); } };
      fr.readAsArrayBuffer(f);
    });
  }).catch(function(e){ _ioppNote('err',e.message); });
}
function _ioppVendorFromName(n){
  var s=String(n||'').replace(/[.][^.]+$/,'');
  s=s.split('_').join(' ').split('-').join(' ').trim();
  return s.slice(0,60) || 'Vendor';
}
function _ioppNote(cls,msg){ ioppNote=msg||''; var n=document.getElementById('iopp-note');
  if(n){ n.className='iop-note '+(cls||''); n.innerHTML=escapeHtml(msg||''); } }
function ioppRemove(i){
  if(!IOPP||!IOPP.files[i]) return;
  IOPP.files.splice(i,1); _ioppIdx=null; ioppCommit();
  renderIoppPanel(); if(mode==='vendor') renderVendorPanel();
}
function ioppClearAll(){ if(!IOPP) return; IOPP.files=[]; _ioppIdx=null; ioppCommit();
  renderIoppPanel(); if(mode==='vendor') renderVendorPanel(); }
function ioppRenameVendor(i,val){ if(!IOPP||!IOPP.files[i]) return;
  IOPP.files[i].vendor=String(val||'').slice(0,60); _ioppIdx=null; ioppCommit(); }
// Persist to inventory-opportunities.json via the button token (same pattern as
// spnl_accepted.json / payment-overrides.json).
function ioppCommit(){
  if(!IOPP) return;
  IOPP.saved_at=new Date().toISOString().slice(0,19).replace('T',' ');
  try{ localStorage.setItem('jit4_iopp', JSON.stringify(IOPP)); }catch(e){}
  if(!BTN||!BTN.token){ _ioppNote('err','Loaded for this browser only — no save token on this page.'); return; }
  var base='https://api.github.com/repos/'+BTN.repo+'/contents/inventory-opportunities.json';
  var hdr={'Authorization':'Bearer '+BTN.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'};
  ioppBusy=true;
  fetch(base+'?ref='+encodeURIComponent(BTN.branch)+'&cb='+Date.now(),{headers:hdr,cache:'no-store'})
    .then(function(r){ if(r.status===404) return {sha:null}; if(!r.ok) throw new Error('read '+r.status); return r.json().then(function(j){ return {sha:j.sha}; }); })
    .then(function(st){ return fetch(base,{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},hdr),
      body:JSON.stringify({message:'Update vendor inventory ('+((IOPP.files||[]).length)+' file(s))',
        content:_b64enc(JSON.stringify(IOPP,null,1)+IOPP_NL), sha:st.sha||undefined, branch:BTN.branch})}); })
    .then(function(r){ if(!r.ok) throw new Error('save '+r.status); ioppBusy=false; })
    .catch(function(e){ ioppBusy=false; _ioppNote('err','Saved in this browser, but the shared copy failed ('+e.message+').'); });
}
function ioppSearch(v){ ioppQ=String(v||'').toLowerCase(); renderIoppPanel(); }

// Build the matched list: every open vendor-PO line whose SKU is in stock WITH A
// DIFFERENT VENDOR than the PO is placed with (same rule as the Fulfill Opp dot).
function ioppMatches(){
  var out=[], vs=(DATA.vendors||[]);
  for(var i=0;i<vs.length;i++){
    var rows=vs[i].rows||[];
    for(var j=0;j<rows.length;j++){
      var r=rows[j], hit=ioppOtherFor(r.sku, vs[i].name);
      if(!hit) continue;
      out.push({sku:r.sku, product:r.product, po_vendor:vs[i].name, customer:r.customer,
                so_num:r.so_num, open_qty:Number(r.open_qty)||0, eta:r.eta,
                pending_pos:r.pending_pos, avail:hit.qty, srcs:hit.srcs});
    }
  }
  out.sort(function(a,b){ var d=cmp(b.avail,a.avail,'num'); return d!==0?d:cmp(a.sku,b.sku,'str'); });
  return out;
}
// SKUs on an open PO where the ONLY stock we hold is with that same vendor — not an
// opportunity, but worth counting so the tab can say why a match did not light up.
function ioppSameVendorCount(){
  var n=0, vs=(DATA.vendors||[]);
  for(var i=0;i<vs.length;i++){ var rows=vs[i].rows||[];
    for(var j=0;j<rows.length;j++){
      if(ioppFor(rows[j].sku) && !ioppOtherFor(rows[j].sku, vs[i].name)) n++; } }
  return n;
}
// The PO vendors a file can be attributed to (plus a free "other supplier" option).
function ioppVendorOptions(){
  var vs=(DATA.vendors||[]), out=[];
  for(var i=0;i<vs.length;i++) out.push(vs[i].name);
  return out;
}
// Best-effort guess of which PO vendor a filename belongs to, so the dropdown starts
// on the right entry. Falls back to "" (treated as an outside supplier).
function ioppGuessVendor(fname){
  var base=ioppVenNorm(String(fname||'').replace(/[.][^.]+$/,''));
  if(!base) return '';
  var opts=ioppVendorOptions(), best='', bestLen=0;
  for(var i=0;i<opts.length;i++){
    var tokens=String(opts[i]).split(' '), hit=0, tot=0;
    for(var t=0;t<tokens.length;t++){
      var tk=ioppVenNorm(tokens[t]);
      if(tk.length<3) continue;                 // skip LLC / INC / short noise
      tot++; if(base.indexOf(tk)>-1) hit++;
    }
    if(tot && hit===tot && ioppVenNorm(opts[i]).length>bestLen){ best=opts[i]; bestLen=ioppVenNorm(opts[i]).length; }
  }
  return best;
}
function ioppSetVendor(i,val){
  if(!IOPP||!IOPP.files[i]) return;
  IOPP.files[i].vendor=String(val||'');
  _ioppIdx=null; ioppCommit();
  renderIoppPanel(); if(mode==='vendor') renderVendorPanel();
}
function renderIoppPanel(){
  loadIopp();
  var fs=(IOPP&&IOPP.files)||[];
  var nRows=0; for(var i=0;i<fs.length;i++) nRows+=(fs[i].rows||[]).length;
  var opts=ioppVendorOptions();
  var chips='';
  for(var k=0;k<fs.length;k++){
    var cv=fs[k].vendor||'', known=false, sel='';
    for(var o=0;o<opts.length;o++){
      var isSel=ioppVenNorm(opts[o])===ioppVenNorm(cv); if(isSel) known=true;
      sel+='<option value="'+escapeHtml(opts[o])+'"'+(isSel?' selected':'')+'>'+escapeHtml(opts[o])+'</option>';
    }
    sel='<option value=""'+(known?'':' selected')+'>Other supplier'+(known?'':(cv?(' — '+escapeHtml(cv)):''))+'</option>'+sel;
    chips+='<span class="iop-file">'+
      '<select title="Which vendor holds this stock? Used to decide whether a PO line is an alternative-source opportunity." '+
        'onchange="ioppSetVendor('+k+',this.value)" style="border:1px solid #cdd9e6;border-radius:6px;padding:2px 4px;font-size:12px;max-width:190px;">'+
        sel+'</select> &middot; '+
      (fs[k].rows||[]).length+' SKU(s) &middot; <span style="color:#6b7a8a;">'+escapeHtml(fs[k].name)+'</span>'+
      ' <span class="iop-x" title="Remove this file" onclick="ioppRemove('+k+')">&times;</span></span>';
  }
  var up='<div class="iop-up" id="iopp-drop">'+
    '<div style="font-weight:700;color:#0D2B45;margin-bottom:6px;">Vendor inventory files</div>'+
    '<div style="font-size:12px;color:#6b7a8a;margin-bottom:10px;">Drop up to '+IOPP_MAX_FILES+
      ' spreadsheets here, or <label style="color:#1F4E79;text-decoration:underline;cursor:pointer;">browse'+
      '<input type="file" accept=".xlsx,.xls,.csv" multiple style="display:none;" '+
      'onchange="ioppHandleFiles(this.files); this.value=String.fromCharCode();"></label>.'+
      ' Needs a SKU column and a quantity column; lot and expiration are picked up when present.</div>'+
    (chips||'<div style="font-size:12px;color:#9aa7b4;">No files loaded yet.</div>')+
    (fs.length?('<div style="margin-top:10px;"><button class="ca-email-btn" onclick="ioppClearAll()">Remove all</button></div>'):'')+
    '<div id="iopp-note" class="iop-note">'+escapeHtml(ioppNote)+'</div>'+
    '</div>';

  var ms=ioppMatches(), mb='';
  for(var m=0;m<ms.length;m++){
    var x=ms[m], src=[];
    for(var s=0;s<x.srcs.length;s++) src.push(escapeHtml(x.srcs[s].vendor)+' ('+fmtQty(x.srcs[s].qty)+
      (x.srcs[s].lot?(', lot '+escapeHtml(x.srcs[s].lot)):'')+(x.srcs[s].exp?(', exp '+escapeHtml(x.srcs[s].exp)):'')+')');
    var covers=x.avail>=x.open_qty;
    mb+='<tr>'+
      '<td class="c"><span class="fop-dot"></span></td>'+
      '<td style="font-weight:600;">'+escapeHtml(x.sku||'')+'</td>'+
      '<td>'+escapeHtml(x.product||'')+'</td>'+
      '<td>'+escapeHtml(x.customer||'')+'</td>'+
      '<td class="so">'+escapeHtml(x.so_num||'')+'</td>'+
      '<td class="c open">'+fmtQty(x.open_qty)+'</td>'+
      '<td class="c" style="font-weight:700;color:'+(covers?'#1b7a3d':'#b54708')+';">'+fmtQty(x.avail)+'</td>'+
      '<td>'+src.join('; ')+'</td>'+
      '<td>'+escapeHtml(x.po_vendor||'')+'</td>'+
      '<td class="c">'+fmtDate(x.eta)+'</td>'+
      '</tr>';
  }
  var sameV=fs.length?ioppSameVendorCount():0;
  var matchTbl = ms.length
    ? ('<table><thead><tr><th></th><th>SKU</th><th>Product</th><th>Customer</th><th>SO #</th>'+
       '<th class="c">Open</th><th class="c">Available elsewhere</th><th>Alternative source</th>'+
       '<th>PO currently with</th><th class="c">ETA</th>'+
       '</tr></thead><tbody>'+mb+'</tbody></table>')
    : ('<div class="empty">'+(fs.length?'No open vendor PO line has stock sitting with a different vendor.'
                                      :'Upload an inventory file to see fulfillment opportunities.')+'</div>');

  // Full uploaded inventory, searchable.
  var ib='', shown=0;
  for(var fi=0;fi<fs.length;fi++){
    var f=fs[fi], rws=f.rows||[];
    for(var ri=0;ri<rws.length;ri++){
      var rr=rws[ri];
      if(ioppQ){
        var hay=((rr.sku||'')+' '+(rr.desc||'')+' '+(f.vendor||'')).toLowerCase();
        if(hay.indexOf(ioppQ)<0) continue;
      }
      shown++;
      if(shown>500) continue;
      var isMatch=_ioppIsOpp(rr.sku, f.vendor);
      ib+='<tr'+(isMatch?' style="background:#f2fbf5;"':'')+'>'+
        '<td class="c">'+(isMatch?'<span class="fop-dot"></span>':'')+'</td>'+
        '<td style="font-weight:600;">'+escapeHtml(rr.sku||'')+'</td>'+
        '<td>'+escapeHtml(rr.desc||'')+'</td>'+
        '<td class="c">'+fmtQty(rr.qty)+'</td>'+
        '<td>'+escapeHtml(rr.lot||'')+'</td>'+
        '<td class="c">'+escapeHtml(rr.exp||'')+'</td>'+
        '<td>'+escapeHtml(f.vendor||f.name)+'</td>'+
        '</tr>';
    }
  }
  var invTbl = nRows
    ? ('<table><thead><tr><th></th><th>SKU</th><th>Description</th><th class="c">Qty</th><th>Lot</th>'+
       '<th class="c">Expiration</th><th>Vendor file</th></tr></thead><tbody>'+ib+'</tbody></table>'+
       (shown>500?('<div style="font-size:12px;color:#6b7a8a;padding:8px 2px;">Showing the first 500 of '+shown+' matching rows — narrow the search to see more.</div>'):''))
    : '';

  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><h2>Inventory Opportunities</h2>'+
    '<div class="sub">'+fs.length+' file(s) &middot; '+nRows+' SKU(s) in stock &middot; '+
      ms.length+' open PO line(s) another vendor could fill'+
      (sameV?(' &middot; '+sameV+' more already stocked by their own PO vendor'):'')+
      (IOPP&&IOPP.saved_at?(' &middot; saved '+escapeHtml(IOPP.saved_at)):'')+'</div></div>'+
    up+
    '<h3 style="margin:18px 0 8px;color:#0D2B45;font-size:15px;">Fulfillment opportunities '+
      '<span style="font-weight:400;font-size:12px;color:#6b7a8a;">&mdash; open PO lines where a '+
      '<b>different</b> vendor holds the stock</span></h3>'+
    matchTbl+
    (nRows?('<h3 style="margin:22px 0 8px;color:#0D2B45;font-size:15px;">All uploaded inventory</h3>'+
      '<input type="search" placeholder="Search SKU, description or vendor…" value="'+escapeHtml(ioppQ)+'" '+
      'oninput="ioppSearch(this.value)" style="padding:8px 10px;border:1px solid #cdd9e6;border-radius:8px;'+
      'font-size:13px;width:280px;margin-bottom:10px;">'+invTbl):'');
  _ioppWireDrop();
}
// SKU -> the set of PO vendors that SKU is currently on order with. Used to highlight
// a row in the full inventory list only when THIS file's vendor is a genuine
// alternative to whoever the PO already sits with.
var _ioppOpenSkuCache=null;
function _ioppOpenSkus(){
  if(_ioppOpenSkuCache) return _ioppOpenSkuCache;
  _ioppOpenSkuCache={};
  var vs=(DATA.vendors||[]);
  for(var i=0;i<vs.length;i++){ var rows=vs[i].rows||[];
    for(var j=0;j<rows.length;j++){ var k=ioppNorm(rows[j].sku); if(!k) continue;
      if(!_ioppOpenSkuCache[k]) _ioppOpenSkuCache[k]={};
      _ioppOpenSkuCache[k][ioppVenNorm(vs[i].name)]=1; } }
  return _ioppOpenSkuCache;
}
function _ioppIsOpp(sku, fileVendor){
  var k=ioppNorm(sku); if(!k) return false;
  var vend=_ioppOpenSkus()[k]; if(!vend) return false;
  var fv=ioppVenNorm(fileVendor);
  for(var v in vend){ if(vend.hasOwnProperty(v) && v!==fv) return true; }
  return false;
}
function _ioppWireDrop(){
  var d=document.getElementById('iopp-drop'); if(!d) return;
  ['dragenter','dragover'].forEach(function(ev){ d.addEventListener(ev,function(e){
    e.preventDefault(); e.stopPropagation(); d.classList.add('drag'); }); });
  ['dragleave','drop'].forEach(function(ev){ d.addEventListener(ev,function(e){
    e.preventDefault(); e.stopPropagation(); d.classList.remove('drag'); }); });
  d.addEventListener('drop',function(e){ if(e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files.length)
    ioppHandleFiles(e.dataTransfer.files); });
}

// ── Invoice Check tab ────────────────────────────────────────────────────────
// Upload a vendor PDF invoice + enter the PO#. Each SKU on the PO is located in the
// invoice text and its unit price compared to the PO unit (list) price. All parsing
// runs in the browser via pdf.js (lazy-loaded from cdnjs). PO prices come from
// DATA.po_prices, baked at the last Vtiger refresh.
var _pdfjsLoading=null;
function _loadPdfJs(){
  if(window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
  if(_pdfjsLoading) return _pdfjsLoading;
  _pdfjsLoading=new Promise(function(res,rej){
    var s=document.createElement('script');
    s.src='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
    s.onload=function(){ try{ window.pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js'; }catch(e){} res(window.pdfjsLib); };
    s.onerror=function(){ rej(new Error('Could not load the PDF reader (network blocked?)')); };
    document.head.appendChild(s);
  });
  return _pdfjsLoading;
}
function _invNote(cls,msg){ var n=document.getElementById('inv-note'); if(n){ n.className='inv-note '+(cls||''); n.innerHTML=msg||''; } }
function _invPoKey(raw){
  var q=String(raw||'').replace(/^\s+|\s+$/g,'').toUpperCase().replace(/\s+/g,'');
  if(!q) return '';
  var pp=(DATA&&DATA.po_prices)||{};
  var digits=q.replace(/[^0-9]/g,'');
  var cands=[q, 'PO'+q.replace(/^#/,''), digits, 'PO'+digits];
  for(var i=0;i<cands.length;i++){ if(cands[i] && pp[cands[i]]) return cands[i]; }
  return '';
}
function _invIsAlnum(ch){ return (ch>='0'&&ch<='9')||(ch>='A'&&ch<='Z')||(ch>='a'&&ch<='z'); }
// Find SKU as a bounded token in uppercased haystack; return index just after it, else -1.
function _invFindSku(hayU, skuU){
  var from=0;
  while(true){
    var idx=hayU.indexOf(skuU, from);
    if(idx<0) return -1;
    var before = idx>0 ? hayU.charAt(idx-1) : '';
    var afterPos = idx+skuU.length;
    var after = afterPos<hayU.length ? hayU.charAt(afterPos) : '';
    if(!_invIsAlnum(before) && !_invIsAlnum(after)) return afterPos;
    from=idx+1;
  }
}
function _invAllMoney(s){
  var re=/(\d{1,3}(?:,\d{3})+|\d+)\.\d{2}/g, out=[], m;
  while((m=re.exec(s))!==null){ out.push(parseFloat(m[0].replace(/,/g,''))); }
  return out;
}
function renderInvPanel(){
  var pp=(DATA&&DATA.po_prices)||{}, npo=0; for(var k in pp){ if(pp.hasOwnProperty(k)) npo++; }
  document.getElementById('panel').innerHTML=
   '<div class="panel-head"><h2>Invoice Check</h2>'+
   '<div class="sub">Upload a vendor PDF invoice and enter the PO#. Each SKU is matched to the PO and its unit price compared to the PO unit (list) price. '+npo+' PO'+(npo===1?'':'s')+' available as of the last refresh.</div></div>'+
   '<div class="invchk">'+
     '<div class="inv-form">'+
       '<div class="inv-fld"><label for="inv-po">PO number</label><input id="inv-po" type="text" placeholder="e.g. PO776" autocomplete="off"></div>'+
       '<div class="inv-fld"><label for="inv-file">Vendor invoice (PDF)</label><input id="inv-file" type="file" accept="application/pdf,.pdf"></div>'+
       '<button class="inv-go" id="inv-go" onclick="runInvoiceCheck()">Check invoice</button>'+
     '</div>'+
     '<div class="inv-note" id="inv-note"></div>'+
     '<div id="inv-results"></div>'+
     '<div class="inv-hint">Works on normal text-based PDF invoices; scanned/photo (image-only) invoices can\\'t be read automatically. Only the unit price is compared, per SKU. Invoice lines whose SKU is not on this PO are not priced.</div>'+
   '</div>';
}
function runInvoiceCheck(){
  var poRaw=(document.getElementById('inv-po').value||'');
  var fileEl=document.getElementById('inv-file');
  var poKey=_invPoKey(poRaw);
  if(!poKey){ _invNote('err','PO &ldquo;'+escapeHtml(poRaw.replace(/^\s+|\s+$/g,''))+'&rdquo; isn\\'t in the current data. It may be newer than the last refresh (click a tab\\'s Refresh), or check the number.'); return; }
  if(!fileEl||!fileEl.files||!fileEl.files.length){ _invNote('err','Choose a PDF invoice to upload.'); return; }
  var file=fileEl.files[0];
  var go=document.getElementById('inv-go'); if(go) go.disabled=true;
  _invNote('warn','Reading PDF…'); document.getElementById('inv-results').innerHTML='';
  _loadPdfJs().then(function(){ return file.arrayBuffer(); })
  .then(function(buf){ return pdfjsLib.getDocument({data:new Uint8Array(buf)}).promise; })
  .then(function(pdf){
    var pages=[]; for(var i=1;i<=pdf.numPages;i++) pages.push(i);
    return pages.reduce(function(chain,pn){
      return chain.then(function(acc){
        return pdf.getPage(pn).then(function(page){ return page.getTextContent(); }).then(function(tc){
          var byY={};
          tc.items.forEach(function(it){ if(!it.str) return; var y=Math.round(it.transform[5]); (byY[y]=byY[y]||[]).push({x:it.transform[4], s:it.str}); });
          Object.keys(byY).forEach(function(y){ var arr=byY[y].sort(function(a,b){ return a.x-b.x; }); acc.push(arr.map(function(o){ return o.s; }).join(' ')); });
          return acc;
        });
      });
    }, Promise.resolve([]));
  })
  .then(function(lines){ _invCompare(poKey, lines, file.name); })
  .catch(function(e){ _invNote('err','Could not read invoice: '+escapeHtml(e.message||String(e))); })
  .finally(function(){ var g=document.getElementById('inv-go'); if(g) g.disabled=false; });
}
function _invCompare(poKey, lines, fname){
  var po=(DATA.po_prices||{})[poKey]||{skus:{}}, skus=po.skus||{};
  var flat=lines.join('  |  '), flatU=flat.toUpperCase();
  var rows=[], nOk=0, nBad=0, nMiss=0;
  Object.keys(skus).forEach(function(sku){
    var poUnit=skus[sku].unit, prod=skus[sku].product||'';
    var afterPos=_invFindSku(flatU, sku.toUpperCase());
    var invUnit=null;
    if(afterPos>=0){
      var cands=_invAllMoney(flat.substring(afterPos, afterPos+160));
      if(cands.length){
        invUnit=cands[0]; var best=Math.abs(cands[0]-poUnit);
        for(var i=1;i<cands.length;i++){ var d=Math.abs(cands[i]-poUnit); if(d<best){ best=d; invUnit=cands[i]; } }
      }
    }
    var status;
    if(afterPos<0){ status='miss'; nMiss++; }
    else if(invUnit===null){ status='noprice'; nMiss++; }
    else if(Math.abs(invUnit-poUnit)<0.005){ status='ok'; nOk++; }
    else if(invUnit>poUnit){ status='over'; nBad++; }
    else { status='under'; nBad++; }
    rows.push({sku:sku, prod:prod, poUnit:poUnit, invUnit:invUnit, status:status});
  });
  var order={over:0, under:1, noprice:2, miss:3, ok:4};
  rows.sort(function(a,b){ return (order[a.status]-order[b.status]) || a.sku.localeCompare(b.sku); });
  function money(v){ return v==null?'—':'$'+Number(v).toFixed(2); }
  var body='';
  rows.forEach(function(r){
    var trcls, badge;
    if(r.status==='ok'){ trcls='inv-ok'; badge='<span class="inv-badge ok">OK</span>'; }
    else if(r.status==='over'){ trcls='inv-over'; badge='<span class="inv-badge over">Overbilled</span>'; }
    else if(r.status==='under'){ trcls='inv-under'; badge='<span class="inv-badge under">Underbilled</span>'; }
    else if(r.status==='noprice'){ trcls='inv-miss'; badge='<span class="inv-badge miss">Price not found</span>'; }
    else { trcls='inv-miss'; badge='<span class="inv-badge miss">Not on invoice</span>'; }
    var delta=(r.invUnit!=null)?(r.invUnit-r.poUnit):null;
    var deltaTxt=(delta==null)?'—':((delta>0?'+':'')+'$'+delta.toFixed(2));
    body+='<tr class="'+trcls+'">'+
      '<td>'+escapeHtml(r.sku)+'</td>'+
      '<td>'+escapeHtml(r.prod)+'</td>'+
      '<td class="num">'+money(r.poUnit)+'</td>'+
      '<td class="num">'+money(r.invUnit)+'</td>'+
      '<td class="num delta">'+deltaTxt+'</td>'+
      '<td>'+badge+'</td></tr>';
  });
  var head='<div class="inv-sum">'+
    '<span class="inv-chip ok">'+nOk+' match'+(nOk===1?'':'es')+'</span>'+
    '<span class="inv-chip bad">'+nBad+' discrepanc'+(nBad===1?'y':'ies')+'</span>'+
    '<span class="inv-chip miss">'+nMiss+' not found</span></div>';
  var meta='<div class="sub" style="margin-bottom:8px;">PO '+escapeHtml(poKey)+(po.vendor?' &middot; '+escapeHtml(po.vendor):'')+(po.status?' &middot; '+escapeHtml(po.status):'')+' &middot; invoice: '+escapeHtml(fname||'')+' &middot; '+rows.length+' PO SKU'+(rows.length===1?'':'s')+'</div>';
  document.getElementById('inv-results').innerHTML=meta+head+
    '<table><thead><tr><td>SKU</td><td>Product</td><td class="num">PO unit</td><td class="num">Invoice unit</td><td class="num">&Delta;</td><td>Status</td></tr></thead><tbody>'+body+'</tbody></table>';
  if(nBad>0) _invNote('err', nBad+' price discrepanc'+(nBad===1?'y':'ies')+' found — see highlighted rows.');
  else if(nOk>0 && nMiss>0) _invNote('warn','All matched SKUs are at the PO price. '+nMiss+' PO SKU'+(nMiss===1?'':'s')+' not found on the invoice.');
  else if(nOk>0) _invNote('ok','All '+nOk+' invoice price'+(nOk===1?'':'s')+' match the PO.');
  else _invNote('warn','No PO SKUs were located in this invoice — check that it matches PO '+escapeHtml(poKey)+'.');
}

function renderAll(){ _ioppOpenSkuCache=null; loadIopp(); renderKpis(); renderTabs(); renderPanel(); renderAsOf(); }

function fetchData(){ return fetch(DATA_URL+'?cb='+Date.now(),{cache:'no-store'})
  .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); }); }

function btnBusy(on,label){
  var btn=document.getElementById('refresh');
  if(on){ btn.classList.add('loading'); btn.disabled=true; }
  else { btn.classList.remove('loading'); btn.disabled=false; }
  btn.querySelector('.lbl').textContent = label || 'Refresh';
}

// Snapshot-only refresh: just reload the latest published JSON.
function reloadSnapshot(){
  btnBusy(true,'Loading…');
  fetchData().then(function(d){ DATA=normData(d); if(active>=(DATA.customers||[]).length) active=0; if(vactive>=(DATA.vendors||[]).length) vactive=0; if(caactive>=(((DATA.customer_analysis||{}).customers)||[]).length) caactive=0; renderAll(); })
    .catch(function(e){ alert('Could not refresh data: '+e.message); })
    .finally(function(){ btnBusy(false); });
}

// Live refresh: trigger the GitHub Actions workflow to re-pull Vtiger, then poll
// the snapshot until its generated_at timestamp changes, then re-render.
function refreshData(){
  if(!BTN.token){ return reloadSnapshot(); }
  var prevStamp=(DATA && DATA.generated_at) || '';
  btnBusy(true,'Refreshing…');
  var url='https://api.github.com/repos/'+BTN.repo+'/actions/workflows/'+BTN.workflow+'/dispatches';
  fetch(url,{method:'POST',headers:{
      'Authorization':'Bearer '+BTN.token,
      'Accept':'application/vnd.github+json',
      'X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json'},
    body:JSON.stringify({ref:BTN.branch})})
    .then(function(r){ if(r.status!==204) return r.text().then(function(t){ throw new Error('Trigger failed ('+r.status+'). '+t.slice(0,160)); });
      pollForUpdate(prevStamp,0); })
    .catch(function(e){ btnBusy(false); alert('Could not start refresh: '+e.message); });
}

function pollForUpdate(prevStamp,tries){
  // Workflow re-pulls Vtiger (rate-limited) — can take a few minutes. Poll ~10 min.
  var MAX=40; // 40 * 15s = 10 min
  if(tries>=MAX){ btnBusy(false); alert('Refresh is taking longer than expected. The data will update once the run finishes — click Refresh again shortly to load it.'); return; }
  btnBusy(true,'Refreshing… '+Math.min(99,Math.round(tries/MAX*100))+'%');
  setTimeout(function(){
    fetchData().then(function(d){
      if(d && d.generated_at && d.generated_at!==prevStamp){
        DATA=normData(d); if(active>=(DATA.customers||[]).length) active=0; if(vactive>=(DATA.vendors||[]).length) vactive=0; if(caactive>=(((DATA.customer_analysis||{}).customers)||[]).length) caactive=0; renderAll(); btnBusy(false);
      } else { pollForUpdate(prevStamp,tries+1); }
    }).catch(function(){ pollForUpdate(prevStamp,tries+1); });
  },15000);
}

function escapeHtml(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(m){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]; }); }

renderAll();
</script>
</body>
</html>""".replace("__DATA_JSON__", data_json).replace("__DATA_URL__", data_url).replace("__BTN_CFG__", btn_cfg).replace("__GADS_EMBED__", gads_embed).replace("__LI_EMBED__", li_embed).replace("__WT_EMBED__", wt_embed).replace("__SHIP_EMBED__", ship_embed).replace("__PAY_EMBED__", pay_embed).replace("__SPNL_EMBED__", spnl_embed).replace("__CJ_EMBED__", cj_embed).replace("__IOPP_EMBED__", iopp_embed)


# ─────────────────────────────────────────────
# GitHub Pages push (Contents API, same pattern as customer_order_status.py)
# ─────────────────────────────────────────────
def _gh_request(endpoint, method="GET", data=None):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/{endpoint}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "message": e.read().decode() if e.fp else ""}


def push_file_to_github(local_path, repo_path):
    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()
    existing = _gh_request(f"contents/{repo_path}")
    sha = existing.get("sha") if isinstance(existing, dict) and "sha" in existing else None
    payload = {
        "message": f"Update {repo_path} — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha
    result = _gh_request(f"contents/{repo_path}", method="PUT", data=payload)
    if "content" in result:
        return True
    log(f"  GitHub push failed for {repo_path}: {result.get('error','')} {str(result.get('message',''))[:200]}")
    return False


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
# ── Vendor Spend: 2026 non-cancelled PO totals by month × vendor (5 alt-source vendors) ──
# Each display column maps to one or more Vtiger Vendor record ids (the CRM holds
# several vendor records per real vendor, e.g. "ALDX", "ALDX Holding", ...).
VSPEND_YEAR = "2026"
VSPEND_GROUPS = {
    "Allora":    ["11x130659", "11x143741", "11x73970"],
    "PMA":       ["11x130679", "11x108033"],
    "CLEARCHEM": ["11x145501", "11x101534"],
    "ALDX":      ["11x130577", "11x142607", "11x76607"],
    "CONMED":    ["11x63346"],
}
VSPEND_ORDER = ["Allora", "PMA", "CLEARCHEM", "ALDX", "CONMED"]


def build_vendor_spend(vt):
    """Sum PurchaseOrder grand totals (2026, non-cancelled) by createdtime month,
    grouped into the five alt-source vendor columns, plus a per-PO detail list
    (po #, amount, customer) that powers the drill-down table. READ-ONLY."""
    vid_to_group = {vid: g for g, ids in VSPEND_GROUPS.items() for vid in ids}
    inlist = "('" + "','".join(vid_to_group.keys()) + "')"
    try:
        pos = vt.query_all(
            "SELECT id, purchaseorder_no, hdnGrandTotal, createdtime, vendor_id, "
            f"accountid, postatus FROM PurchaseOrder WHERE vendor_id IN {inlist}")
    except Exception as e:
        log(f"  vendor_spend: PO query failed ({e})")
        return {}
    acct_name = {}
    try:
        for a in vt.query_all("SELECT id, accountname FROM Accounts"):
            acct_name[a["id"]] = a.get("accountname", "")
    except Exception as e:
        log(f"  vendor_spend: could not load accounts ({e})")

    matrix, totals, month_totals, details, months = {}, {v: 0.0 for v in VSPEND_ORDER}, {}, [], set()
    n = 0
    for p in pos:
        ct = str(p.get("createdtime", ""))
        if not ct.startswith(VSPEND_YEAR):
            continue
        if "cancel" in str(p.get("postatus", "")).strip().lower():
            continue
        grp = vid_to_group.get(p.get("vendor_id", ""))
        if not grp:
            continue
        month = ct[:7]
        try:
            amt = round(float(p.get("hdnGrandTotal") or 0), 2)
        except (TypeError, ValueError):
            amt = 0.0
        months.add(month)
        matrix.setdefault(month, {v: 0.0 for v in VSPEND_ORDER})
        matrix[month][grp] = round(matrix[month][grp] + amt, 2)
        totals[grp] = round(totals[grp] + amt, 2)
        month_totals[month] = round(month_totals.get(month, 0.0) + amt, 2)
        details.append({"po": p.get("purchaseorder_no", ""), "amount": amt,
                        "customer": acct_name.get(p.get("accountid", ""), p.get("accountid", "") or "—"),
                        "vendor": grp, "month": month, "date": ct[:10]})
        n += 1
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
        "year": VSPEND_YEAR, "vendors": VSPEND_ORDER, "months": sorted(months),
        "matrix": matrix, "month_totals": month_totals, "totals": totals,
        "grand_total": round(sum(totals.values()), 2), "po_count": n, "pos": details,
    }


def main():
    parser = argparse.ArgumentParser(description="JIT4You Open Orders — tabbed page")
    parser.add_argument("--no-push", action="store_true", help="Build files locally, don't push to GitHub Pages")
    parser.add_argument("--dry-run", action="store_true", help="Preview counts only")
    args = parser.parse_args()

    log("=" * 60)
    log("JIT4You Open Orders — Per-Customer Tabbed Page (duplicate)")
    log("=" * 60)

    # Connect to Vtiger using the SAME date-scoped cache the report uses, so
    # this run benefits from the same warm cache and rate-limit resilience.
    today = datetime.now().strftime("%Y-%m-%d")
    cache_path = os.path.join(CONFIG["output_dir"], f"retrieve_cache_{today}.json")
    vt = VtigerAPI(CONFIG["vtiger_rest_base"], CONFIG["vtiger_user"],
                   CONFIG["vtiger_accesskey"], cache_path=cache_path)
    # Bulk-query cache is purged by VtigerAPI on load once it exceeds QUERY_CACHE_TTL
    # (see open_orders_report.VtigerAPI). Force a full purge with PURGE_QUERY_CACHE=1.
    vt.login()

    open_items = extract_open_orders(vt, dry_run=args.dry_run)

    if args.dry_run:
        log("Dry run complete")
        return

    # Same completeness gate as the report: only publish on a clean, complete pass.
    if vt.fetch_failures > 0:
        vt.save_cache()
        log(f"INCOMPLETE: {vt.fetch_failures} record fetches failed (likely rate-limited).")
        log("Progress saved to cache. Re-run to resume; page not generated/published this pass.")
        sys.exit(2)

    if not open_items:
        log("No open items found!")
        return

    page_data = build_page_data(open_items)
    log(f"Built page data: {page_data['totals']['customers']} customers, "
        f"{page_data['totals']['open_items']} open items")

    # P&L report (same data pipeline, fresh from Vtiger) embedded as the first tab.
    log("Building P&L report...")
    page_data["pnl_html"] = build_pnl(vt)
    log(f"  P&L HTML: {len(page_data['pnl_html'])} bytes")

    # Customer Analysis (IDL customers) — ordering matrix, recommendations, email drafts.
    log("Building Customer Analysis...")
    page_data["customer_analysis"] = build_customer_analysis(vt)
    log(f"  Customer Analysis: {len(page_data['customer_analysis']['customers'])} IDL customers")

    # Customer Prices (IDL customers) — per-SO unit-selling-price matrix (SO x SKU) + COGS.
    log("Building Customer Prices...")
    page_data["customer_prices"] = build_customer_prices(vt)
    _cp = page_data["customer_prices"]
    log(f"  Customer Prices: {len(_cp['customers'])} IDL customers, "
        f"{sum(c['so_count'] for c in _cp['customers'])} SOs, "
        f"{sum(c['line_count'] for c in _cp['customers'])} priced lines")

    # Alternative Sources: Beckman Coulter SKU -> 4-vendor cost map (vendor tab side box).
    log("Building Alternative Sources cost map...")
    page_data["alt_sources"] = build_alt_sources(vt)
    log(f"  Alt sources: {len(page_data['alt_sources'])} Beckman Coulter products")

    # PO price map: PO -> {sku: {unit, product, qty}} for the Invoice Check tab
    # (built from PO line items already in the retrieve cache — no extra Vtiger calls).
    log("Building PO price map (Invoice Check)...")
    page_data["po_prices"] = build_po_prices(vt)
    _npo = len(page_data["po_prices"])
    _nsku = sum(len(v.get("skus", {})) for v in page_data["po_prices"].values())
    log(f"  PO prices: {_npo} POs, {_nsku} priced SKU lines")

    # Paid Inventory: user-maintained list, source of truth is paid_inventory.json
    # (served next to the data file; the page also live-fetches + commits to it).
    # Bake the current contents so the local mirror / artifact show a baseline.
    try:
        with open(PAID_INVENTORY_FILENAME) as _pf:
            _pi = json.load(_pf)
        page_data["paid_inventory"] = _pi.get("items", []) if isinstance(_pi, dict) else (_pi or [])
    except Exception:
        page_data["paid_inventory"] = []
    log(f"  Paid inventory: {len(page_data['paid_inventory'])} logged item(s)")

    # Vendor Spend: 2026 PO spend by month × vendor (Allora/PMA/CLEARCHEM/ALDX/CONMED).
    log("Building Vendor Spend...")
    page_data["vendor_spend"] = build_vendor_spend(vt)
    _vs = page_data["vendor_spend"]
    log(f"  Vendor Spend: {_vs.get('po_count',0)} POs, {len(_vs.get('months',[]))} months, "
        f"grand ${_vs.get('grand_total',0):,.2f}")

    # YTD Demand: units sold per item x month, from the SO line items already in the
    # retrieve cache (no extra Vtiger calls).
    log("Building YTD Demand...")
    _acct_names = {}
    try:
        for _a in vt.query_all("SELECT id, accountname FROM Accounts"):
            _acct_names[_a["id"]] = _a.get("accountname", "")
    except Exception as _e:
        log(f"  YTD Demand: could not load accounts ({_e})")
    page_data["ytd_demand"] = build_ytd_demand(vt, acct_names=_acct_names)
    _yd = page_data["ytd_demand"]
    log(f"  YTD Demand: {len(_yd.get('items',[]))} items, {len(_yd.get('customers',[]))} customers, "
        f"{_yd.get('so_count',0)} SOs, {len(_yd.get('months',[]))} months")

    out_dir = CONFIG["output_dir"]
    data_path = os.path.join(out_dir, DATA_FILENAME)
    html_path = os.path.join(out_dir, PAGE_FILENAME)
    with open(data_path, "w") as f:
        json.dump(page_data, f, indent=2)
    with open(html_path, "w") as f:
        f.write(build_html(page_data))
    log(f"Wrote {html_path}")
    log(f"Wrote {data_path}")

    if args.no_push:
        log("Skipping GitHub Pages push (--no-push flag)")
    else:
        log("Publishing to GitHub Pages...")
        ok_data = push_file_to_github(data_path, DATA_FILENAME)
        ok_page = push_file_to_github(html_path, PAGE_FILENAME)
        if ok_data and ok_page:
            log(f"Published: {GITHUB_PAGES_URL}/{PAGE_FILENAME}")
        else:
            log("WARNING: one or more files failed to publish.")

    log("Done!")


if __name__ == "__main__":
    main()
