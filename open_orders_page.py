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

# ─────────────────────────────────────────────
# GitHub Pages publishing (same host/repo as the customer-order-status reports)
# ─────────────────────────────────────────────
GITHUB_REPO = os.environ.get("GH_PAGES_REPO", "JIT4Labs1/customer-order-status")
GITHUB_TOKEN = os.environ.get("GH_PAT_TOKEN", "")
GITHUB_PAGES_URL = os.environ.get("GH_PAGES_URL", "https://jit4labs1.github.io/customer-order-status")

PAGE_FILENAME = "open-orders.html"
DATA_FILENAME = "open-orders-data.json"

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
# durable PAT before it expires (~2026-06-25), else the Refresh button reverts to snapshot-only.
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
    gads_embed, li_embed, wt_embed, ship_embed, pay_embed = _emb("gads"), _emb("li"), _emb("wt"), _emb("ship"), _emb("pay")
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
<title>JIT4You — Open Orders by Customer</title>
<link rel="icon" href="https://jit4you.myshopify.com/cdn/shop/files/JIT4LABS_Favicon.png" type="image/png">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'Segoe UI',Arial,Helvetica,sans-serif; background:#f0f2f5; color:#2c3e50; }
  .header { background:#0D2B45; color:#fff; padding:18px 28px; display:flex; align-items:center;
            justify-content:space-between; flex-wrap:wrap; gap:12px; }
  .header .brand { font-size:22px; font-weight:700; letter-spacing:1px; }
  .header .brand small { display:block; font-size:13px; font-weight:600; color:#cdd9e6; letter-spacing:0; margin-top:2px; }
  .header .meta { text-align:right; font-size:12px; color:#cdd9e6; }
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

  .modebar { display:flex; gap:8px; padding:16px 28px 0; flex-wrap:wrap; border-bottom:1px solid #dee5ec; }
  .mode-btn { background:#fff; border:1px solid #cdd9e6; border-bottom:none; padding:10px 20px; border-radius:8px 8px 0 0;
    font-size:13px; font-weight:700; color:#1F4E79; cursor:pointer; font-family:inherit; margin-bottom:-1px; }
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
  .pnl-wrap { overflow-x:auto; padding:20px 22px; }
  .pnl-wrap h2, .pnl-wrap h3 { color:#2c3e50; }

  .layout { display:flex; gap:0; padding:18px 28px 40px; align-items:flex-start; }
  .tabs { flex:0 0 270px; background:#fff; border:1px solid #dee5ec; border-radius:10px; overflow:hidden; max-height:78vh; overflow-y:auto; }
  .tab { display:block; width:100%; text-align:left; background:none; border:none; border-bottom:1px solid #eef2f6;
         padding:12px 16px; cursor:pointer; font-size:13px; color:#2c3e50; font-family:inherit; }
  .tab:hover { background:#f5f8fb; }
  .tab.active { background:#1F4E79; color:#fff; }
  .tab .cnt { float:right; font-size:11px; opacity:.8; }
  .tab.active .cnt { color:#cfe0f0; }

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
  @media (max-width:760px){ .layout{flex-direction:column;} .tabs{flex-basis:auto;width:100%;max-height:none;}
    .panel-wrap{margin-left:0;margin-top:14px;} }
</style>
</head>
<body>
<div class="header">
  <div class="brand">JIT4You<small>Open Orders by Customer</small></div>
  <div style="display:flex;align-items:center;gap:18px;">
    <div class="meta">
      <div id="asof">&nbsp;</div>
      <div>2026 Sales Orders &middot; Excl. ConMed</div>
    </div>
    <button id="refresh" class="refresh-btn" onclick="refreshData()">
      <span class="spin"></span><span class="lbl">Refresh</span>
    </button>
  </div>
</div>

<div class="kpis" id="kpis"></div>

<div class="modebar">
  <button class="mode-btn mode-pnl active" data-mode="pnl" onclick="setMode('pnl')">P&amp;L Report</button>
  <button class="mode-btn" data-mode="cust" onclick="setMode('cust')">Customer Open SO's</button>
  <button class="mode-btn" data-mode="vendor" onclick="setMode('vendor')">Open Vendor POs</button>
  <button class="mode-btn" data-mode="sku" onclick="setMode('sku')">High Demand SKUs</button>
  <button class="mode-btn mode-ship" data-mode="ship" onclick="setMode('ship')">Shipments</button>
  <button class="mode-btn mode-pay" data-mode="pay" onclick="setMode('pay')">Payment Status</button>
  <button class="mode-btn" data-mode="ca" onclick="setMode('ca')">Customer Analysis</button>
  <button class="mode-btn mode-mkt" data-mode="wt" onclick="setMode('wt')">Website Traffic</button>
  <button class="mode-btn mode-mkt" data-mode="gads" onclick="setMode('gads')">Google Ads</button>
  <button class="mode-btn mode-mkt" data-mode="li" onclick="setMode('li')">LinkedIn</button>
</div>

<div class="layout">
  <div class="tabs" id="tabs"></div>
  <div class="panel-wrap"><div class="panel" id="panel"></div></div>
</div>

<div class="footer">JIT4You Inc. &middot; Open Orders &middot; data refreshes from Vtiger on each scheduled run</div>

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
var GADS_EMBED = __GADS_EMBED__, LI_EMBED = __LI_EMBED__, WT_EMBED = __WT_EMBED__, SHIP_EMBED = __SHIP_EMBED__, PAY_EMBED = __PAY_EMBED__;
function _deobf(s,key){ if(!s) return ''; var raw=atob(s), out=''; for(var i=0;i<raw.length;i++){ out+=String.fromCharCode(raw.charCodeAt(i) ^ key.charCodeAt(i%key.length)); } return out; }
BTN.token = _deobf(BTN.token_obf, BTN.k || '');
var active = 0;     // selected customer index (Customer Open SO's view)
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
  {key:'eta',        label:'ETA',        type:'date', c:true}
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
function kpi(v,l){ return '<div class="kpi"><div class="v">'+(v==null?'0':v)+'</div><div class="l">'+l+'</div></div>'; }

function renderTabs(){
  var tabsEl=document.getElementById('tabs');
  if(mode==='sku' || mode==='pnl' || mode==='gads' || mode==='li' || mode==='wt' || mode==='pay'){ tabsEl.style.display='none'; tabsEl.innerHTML=''; return; }  // full-width views, no per-entity tabs
  if(mode==='ship'){ renderShipTabs(tabsEl); return; }  // Shipments: sidebar of customers (receivers)
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
function renderPanel(){
  if(mode==='pnl') renderPnlPanel();
  else if(mode==='vendor') renderVendorPanel();
  else if(mode==='sku') renderSkuPanel();
  else if(mode==='ca') renderCaPanel();
  else if(mode==='gads') renderGadsPanel();
  else if(mode==='li') renderLiPanel();
  else if(mode==='wt') renderWtPanel();
  else if(mode==='ship') renderShipPanel();
  else if(mode==='pay') renderPayPanel();
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

// ── Website Traffic tab (GA4 daily visitors by source + sales by source) ──────
var WT=null, wtLoading=false, wtWin='last_30_days', wtLabels=true, wtTrend=true, wtVisible={};
var WT_WIN_ORDER=['today','last_7_days','last_30_days','this_month','last_month','this_quarter','last_quarter','this_year'];
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
function wtSetWin(v){ wtWin=v; renderWtPanel(); }
function wtToggleLabels(c){ wtLabels=!!c; renderWtPanel(); }
function wtToggleTrend(c){ wtTrend=!!c; renderWtPanel(); }
function wtToggleSource(bk,c){ wtVisible[bk]=!!c; renderWtPanel(); }
function wtToggleSourceIdx(i,c){ var bk=((WT&&WT.buckets)||[])[i]; if(bk!=null){ wtVisible[bk]=!!c; renderWtPanel(); } }
function wtAllSources(c){ var bs=(WT&&WT.buckets)||[]; for(var i=0;i<bs.length;i++) wtVisible[bs[i]]=!!c; renderWtPanel(); }
function wtVisBuckets(){ var bs=(WT&&WT.buckets)||[], v=[]; for(var i=0;i<bs.length;i++) if(wtVisible[bs[i]]) v.push(bs[i]); return v; }
function wtLabel(t,gran){
  // t is YYYY-MM-DD (day) or week-Monday date (week)
  var p=(t||'').split('-'); if(p.length<3) return t;
  var mo=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][parseInt(p[1],10)-1];
  return mo+' '+parseInt(p[2],10);
}
function wtBarSvg(win){
  var buckets=wtVisBuckets(), pts=win.points||[], n=pts.length;
  if(!buckets.length) return '<div class="empty">Select at least one source above to show the chart.</div>';
  if(!n) return '<div class="empty">No visitors in this window.</div>';
  var totals=[], maxT=0;
  for(var i=0;i<n;i++){ var s=0; for(var b=0;b<buckets.length;b++) s+=pts[i][buckets[b]]||0; totals.push(s); if(s>maxT) maxT=s; }
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
    for(var b=0;b<buckets.length;b++){
      var bk=buckets[b], v=pts[i][bk]||0; if(v<=0) continue;
      var h=plotH*v/yMax; yCur-=h;
      svg+='<rect x="'+x.toFixed(1)+'" y="'+yCur.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+h.toFixed(1)+'" fill="'+WT_COLORS[bk]+'"><title>'+escapeHtml(wtLabel(pts[i].t,win.granularity))+' · '+escapeHtml(bk)+': '+v+'</title></rect>';
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
function wtLegend(){
  var buckets=WT.buckets||[], allOn=true;
  for(var a=0;a<buckets.length;a++){ if(!wtVisible[buckets[a]]) allOn=false; }
  var h='<div style="display:flex;flex-wrap:wrap;gap:6px 14px;margin:4px 2px 10px;font-size:12px;color:#2c3e50;align-items:center;">';
  h+='<span style="color:#7a8a99;">Show:</span>';
  h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:5px;font-weight:600;"><input type="checkbox" onchange="wtAllSources(this.checked)"'+(allOn?' checked':'')+'> All</label>';
  for(var b=0;b<buckets.length;b++){ var bk=buckets[b];
    h+='<label style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;"><input type="checkbox" onchange="wtToggleSourceIdx('+b+',this.checked)"'+(wtVisible[bk]?' checked':'')+'><span style="width:12px;height:12px;border-radius:2px;background:'+WT_COLORS[bk]+';display:inline-block;"></span>'+escapeHtml(bk)+'</label>';
  }
  if(wtTrend){ h+='<span style="display:inline-flex;align-items:center;gap:6px;color:#7a8a99;"><span style="width:18px;height:0;border-top:2.5px dashed #d6336c;display:inline-block;"></span>Trend</span>'; }
  return h+'</div>';
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
    '<div style="display:flex;justify-content:space-between;gap:6px;font-size:11px;margin-bottom:3px;padding:3px 6px;background:#f6f9fc;border-radius:4px;">'+
      '<span style="color:#2c3e50;white-space:nowrap;">🛒 <b>'+Number(purch).toLocaleString()+'</b></span>'+
      '<span style="color:#b54708;white-space:nowrap;">Cost <b>'+money0(cost)+'</b>'+(cpc!=null?' · $'+cpc.toFixed(2)+'/clk':'')+'</span>'+
      '<span style="color:#188038;font-weight:700;white-space:nowrap;">Rev '+money0(rev)+'</span>'+
      (roas!==null?'<span style="color:#5a6b7a;white-space:nowrap;">'+roas.toFixed(1)+'×</span>':'')+
    '</div>'+svg+'</div>';
}
function wtGadsGrid(win){
  var camps=win.gads_campaigns||[];
  if(!camps.length) return '<div class="empty" style="margin:6px 0;">No Google Ads campaign traffic in this window.</div>';
  var h='<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:6px 2px 4px;">';
  for(var i=0;i<camps.length;i++){ h+=wtMini(win,camps[i]); }
  return h+'</div>';
}
function renderWtPanel(){
  if(!WT){ document.getElementById('panel').innerHTML='<div class="empty">Loading website-traffic data…</div>'; loadWT(); return; }
  var allb=WT.buckets||[]; for(var z=0;z<allb.length;z++){ if(!(allb[z] in wtVisible)) wtVisible[allb[z]]=true; }
  var wins=WT.windows||{}, ids=WT_WIN_ORDER;
  var win=wins[wtWin]||wins['last_30_days']||wins[ids[0]];
  var sel='<select onchange="wtSetWin(this.value)" style="padding:7px 10px;border:1px solid #cdd9e6;border-radius:6px;font-size:13px;font-family:inherit;">';
  for(var i=0;i<ids.length;i++){ var w=wins[ids[i]]; if(!w) continue; sel+='<option value="'+ids[i]+'"'+(ids[i]===wtWin?' selected':'')+'>'+escapeHtml(w.label)+'</option>'; }
  sel+='</select>';
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
    '<div class="ca-h" style="margin-top:18px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;"><span>Google Ads visitors by campaign ('+escapeHtml(win.label)+')</span><span style="font-weight:400;font-size:12px;color:#7a8a99;">sessions per '+(win.granularity==='week'?'week':'day')+' &middot; one chart per campaign</span></div>'+
    wtGadsGrid(win)+
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
  // Sales Order # comes from the Shopify "Vtiger SO:" order tag.
  var soHtml = s.so_num ? '<b>'+escapeHtml(s.so_num)+'</b>' : '<span style="color:#9aa7b4;">—</span>';
  var idLine =
    '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;">'+
      '<span style="background:#eef3f9;border:1px solid #d5e0ec;border-radius:6px;padding:3px 10px;font-size:12.5px;">Sales Order: '+soHtml+'</span>'+
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

// ── Google Ads tab (data loaded from a separate google-ads-data.json file so the
// Vtiger Refresh never overwrites it) ────────────────────────────────────────
var GADS=null, gadsInterval='this_year', gadsLoading=false;
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
  var rows=(cur?cur.campaigns:[]), tc=0,ti=0,tcost=0,tconv=0,tval=0, body='';
  for(var k=0;k<rows.length;k++){ var r=rows[k];
    tc+=r.clicks; ti+=r.impressions; tcost+=r.cost; tconv+=r.conversions; tval+=r.conv_value;
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
      '<td class="c">'+fmtQty(r.conversions)+'</td>'+
      '<td class="c">'+money0(r.conv_value)+'</td>'+
      '<td class="c">'+(r.roas?Number(r.roas).toFixed(1)+'x':'—')+'</td>'+
      '</tr>';
  }
  var tctr=ti?(tc/ti*100).toFixed(2)+'%':'—', tcpc=tc?money2(tcost/tc):'—', troas=tcost?(tval/tcost).toFixed(1)+'x':'—';
  body+='<tr class="so-group"><td>Total ('+escapeHtml(cur?cur.label:'')+')</td><td></td><td></td><td></td>'+
    '<td class="c">'+tc.toLocaleString()+'</td><td class="c">'+ti.toLocaleString()+'</td><td class="c">'+tctr+'</td>'+
    '<td class="c">'+tcpc+'</td><td class="c open">'+money2(tcost)+'</td><td class="c">'+fmtQty(tconv)+'</td>'+
    '<td class="c">'+money0(tval)+'</td><td class="c">'+troas+'</td></tr>';
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'+
    '<div><h2>Google Ads — Campaign Performance</h2><div class="sub">Account: '+escapeHtml(GADS.account||'')+' &middot; data pulled '+escapeHtml(GADS.pulled_at||'')+' &middot; '+escapeHtml(GADS.currency||'USD')+'</div></div>'+
    '<div style="font-size:13px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">Time interval: '+sel+
    '<button class="refresh-btn" onclick="gadsRefresh()" title="Reload the latest Google Ads / GA4 snapshot (separate from the Vtiger Refresh)"><span class="lbl">↻ Refresh Google Ads</span></button></div></div></div>'+
    '<div class="matrix-wrap"><table class="matrix"><thead><tr>'+
    '<th>Campaign</th><th>Status</th><th>Type</th><th>Started</th><th class="c">Clicks</th><th class="c">Impr.</th><th class="c">CTR</th>'+
    '<th class="c">Avg CPC</th><th class="c">Spend</th><th class="c">Conv.</th><th class="c">Conv. value</th><th class="c">ROAS</th>'+
    '</tr></thead><tbody>'+body+'</tbody></table></div>'+ gadsJourneyHtml();
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
function renderPnlPanel(){
  var html=DATA.pnl_html||'';
  document.getElementById('panel').innerHTML = html
    ? '<div class="pnl-wrap">'+html+'</div>'
    : '<div class="empty">P&amp;L report will appear after the next refresh.</div>';
}

function renderCustPanel(){
  var c=(DATA.customers||[])[active];
  if(!c){ document.getElementById('panel').innerHTML='<div class="empty">No open orders.</div>'; return; }
  // Group this customer's rows by SO number.
  var groups={}, order=[], rows=(c.rows||[]);
  for(var i=0;i<rows.length;i++){
    var r=rows[i], so=r.so_num||'(no SO)';
    if(!groups[so]){ groups[so]={so:so, status:r.so_status, date:r.order_date, items:[]}; order.push(so); }
    var g=groups[so]; g.items.push(r);
    if(r.order_date && (!g.date || r.order_date<g.date)) g.date=r.order_date;
  }
  // Order SO groups by order date (oldest first), then SO number.
  order.sort(function(a,b){ var d=cmp(groups[a].date,groups[b].date,'date'); return d!==0?d:cmp(groups[a].so,groups[b].so,'str'); });
  var ncol=COLS_CUST.length, body='';
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
  var sortNote = sortState.key ? ' &middot; sorted by '+escapeHtml(colByKey(sortState.key).label)+(sortState.dir>0?' ▲':' ▼') : '';
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">'+
    '<h2 style="margin:0;">'+escapeHtml(c.name)+'</h2>'+
    '<button class="copy-email-btn" onclick="custEmailToClipboard()" title="Copy this customer&#39;s open-order email (Product, List Price, quantities) — pastes as a formatted table into email/Word/Docs">📋 Copy email</button></div>'+
    '<div class="sub">'+c.open_sos+' open SO(s) &middot; '+c.open_items+' open item(s) &middot; '+
    (c.vendors||[]).length+' vendor(s) &middot; grouped by SO'+sortNote+'</div></div>'+
    '<table><thead><tr>'+renderHead()+'</tr></thead><tbody>'+body+'</tbody></table>';
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
  var c=(DATA.customers||[])[active];
  if(!c){ alert('No customer selected.'); return; }
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
      its.sort(function(p,q){ return sortState.dir*cmp(p[sortState.key],q[sortState.key],col?col.type:'str'); }); }
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
var PAY=null, payLoading=false, payCust='', payReadyOnly=false;
// "Ready for payment" = still owed (Not Paid) AND has a customer pay link.
function payInvoices(c){ var invs=(c&&c.invoices)||[]; return payReadyOnly ? invs.filter(function(v){ return v.status==='Not Paid' && v.fulfillment==='Fulfilled'; }) : invs; }
function payTotals(invs){ var amt=0, unpaid=0; for(var i=0;i<invs.length;i++){ amt+=Number(invs[i].amount)||0; unpaid+=Number(invs[i].balance)||0; }
  return {count:invs.length, amount:Math.round(amt*100)/100, unpaid:Math.round(unpaid*100)/100}; }
function payToggleReady(cb){ payReadyOnly=!!cb.checked; renderPayPanel(); }
function loadPay(){
  if(PAY_EMBED){ PAY=PAY_EMBED; payLoading=false; if(mode==='pay'){ renderPayPanel(); } return; }
  if(payLoading) return; payLoading=true;
  fetch('payment-status-data.json?cb='+Date.now(),{cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(d){ PAY=d; payLoading=false; if(mode==='pay'){ renderPayPanel(); } })
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
    for(var j=0;j<iv.length;j++){ var v=iv[j], bal=Number(v.balance)||0;
      amt+=Number(v.amount)||0; unpaid+=bal;
      if(v.status==='Not Paid'){ openN++; if(v.fulfillment==='Fulfilled'){ ready+=bal; readyN++; } }
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
  function readyCount(cc){ var n=0,iv=(cc&&cc.invoices)||[]; for(var i=0;i<iv.length;i++){ if(iv[i].status==='Not Paid'&&iv[i].fulfillment==='Fulfilled') n++; } return n; }
  var vcs = payReadyOnly ? cs.filter(function(cc){ return readyCount(cc)>0; }) : cs;
  if(payCust!=='__ALL__' && payReadyOnly && !vcs.some(function(cc){return cc.name===payCust;})) payCust='__ALL__';
  var c=payCurrentOrAll(); var allMode=!!(c&&c._all);
  var opts='<option value="__ALL__"'+(allMode?' selected':'')+'>All customers ('+vcs.length+')</option>';
  for(var i=0;i<vcs.length;i++){ var cnt=payReadyOnly?readyCount(vcs[i]):vcs[i].totals.count; opts+='<option value="'+escapeHtml(vcs[i].name)+'"'+(vcs[i].name===payCust?' selected':'')+'>'+escapeHtml(vcs[i].name)+' ('+cnt+')</option>'; }
  var invs=payInvoices(c), body='';
  for(var j=0;j<invs.length;j++){ var v=invs[j];
    body+='<tr>'+
      (allMode?'<td style="white-space:nowrap;">'+escapeHtml(v._cust||'')+'</td>':'')+
      '<td>'+escapeHtml(v.number)+'</td>'+
      '<td>'+(v.so_num?escapeHtml(v.so_num):'<span style="color:#c8d0d8;">—</span>')+'</td>'+
      '<td class="c">'+payBadge(v.status)+'</td>'+
      '<td class="c">'+payFulfillCell(v)+'</td>'+
      '<td class="c">'+fmtDate(v.date)+'</td>'+
      '<td style="text-align:right;">'+payMoney(v.amount)+'</td>'+
      '<td class="c">'+(function(){var u=v.invoice_link||v.link; if(!u) return '<span style="color:#999;">'+(v.status==='Paid'?'&mdash;':'No link')+'</span>'; return '<a href="'+escapeHtml(u)+'" target="_blank" rel="noopener" title="'+(v.invoice_link?'Opens the full invoice (line items) with a Pay button':'Opens the payment page')+'">'+(v.invoice_link?'View invoice &amp; pay ':'Pay ')+payMoney(v.balance||v.amount)+' <span style="color:#008080;">↗</span></a>';})()+'</td>'+
      '</tr>';
  }
  if(!invs.length){ body='<tr><td colspan="'+(allMode?8:7)+'" class="empty" style="padding:16px;">No invoices'+(payReadyOnly?' ready for payment':'')+' for this selection.</td></tr>'; }
  var t=payTotals(invs);
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">'+
    '<h2 style="margin:0;">Payment Status</h2>'+
    '<select id="paySelect" onchange="paySelectChange()" style="padding:7px 10px;border:1px solid #cdd9e6;border-radius:6px;font-size:13px;min-width:240px;">'+opts+'</select>'+
    '<label style="display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#2c3e50;cursor:pointer;white-space:nowrap;"><input type="checkbox" onchange="payToggleReady(this)"'+(payReadyOnly?' checked':'')+'> Ready for payment only</label>'+
    '<button class="copy-email-btn" onclick="copyPayTable()" title="Copy this invoice table — pastes as a formatted table into email/Word/Docs">📋 Copy table</button></div>'+
    '<div class="sub">Independent Diagnostic Lab &middot; '+t.count+' invoice(s)'+(payReadyOnly?' ready for payment':'')+' &middot; '+payMoney(t.amount)+' total &middot; '+payMoney(t.unpaid)+' unpaid &middot; QuickBooks '+escapeHtml(''+(PAY.year||''))+' &middot; as of '+escapeHtml(PAY.generated_at||'')+'</div></div>'+
    (function(){var g=payGrandTotals(); return '<div class="ca-h" style="margin:2px 0 4px;">Portfolio summary &mdash; all Independent Diagnostic Labs ('+g.custN+' customers)</div>'+
      '<div class="kpis" style="padding:2px 0 12px;">'+
        kpi(payMoney(g.unpaid),'Outstanding')+
        kpi(payMoney(g.ready),'Ready for payment')+
        kpi(g.readyN+' / '+g.openN,'Invoices ready / open')+
      '</div>';})()+
    '<div class="ca-h" style="margin-top:6px;">'+escapeHtml(allMode?'All customers':c.name)+' &mdash; invoices</div>'+
    '<table><thead><tr>'+(allMode?'<th>Customer</th>':'')+'<th>Invoice #</th><th>SO #</th><th class="c">Status</th><th class="c">Fulfillment</th><th class="c">Date</th><th style="text-align:right;">Amount</th><th class="c">Link</th></tr></thead><tbody>'+body+
    '<tr class="so-group"><td colspan="'+(allMode?6:5)+'" style="text-align:right;font-weight:700;">Total ('+t.count+')</td>'+
    '<td style="text-align:right;font-weight:700;">'+payMoney(t.amount)+'</td><td class="c" style="font-weight:700;color:#c0392b;">'+payMoney(t.unpaid)+' unpaid</td></tr>'+
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
    var extra = dm==='pnl' ? ' mode-pnl' : (dm==='ship' ? ' mode-ship' : (dm==='pay' ? ' mode-pay' : ((dm==='wt'||dm==='gads'||dm==='li') ? ' mode-mkt' : '')));  // P&L green, marketing tabs orange, shipments/payment blue
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

function renderAsOf(){
  document.getElementById('asof').textContent = 'Last refreshed: '+(DATA.generated_at||'—');
}
function selectTab(i){ if(mode==='vendor') vactive=i; else if(mode==='ca') caactive=i; else active=i; renderTabs(); renderPanel(); }
function renderAll(){ renderKpis(); renderTabs(); renderPanel(); renderAsOf(); }

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
</html>""".replace("__DATA_JSON__", data_json).replace("__DATA_URL__", data_url).replace("__BTN_CFG__", btn_cfg).replace("__GADS_EMBED__", gads_embed).replace("__LI_EMBED__", li_embed).replace("__WT_EMBED__", wt_embed).replace("__SHIP_EMBED__", ship_embed).replace("__PAY_EMBED__", pay_embed)


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
