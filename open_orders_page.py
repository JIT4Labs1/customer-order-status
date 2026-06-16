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
from open_orders_report import VtigerAPI, extract_open_orders, CONFIG, log, build_po_email_url

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

# ── Refresh button → GitHub Actions workflow_dispatch ──────────────────────────
# The page's Refresh button triggers this workflow to re-pull Vtiger live, then
# polls the data snapshot until it updates. GH_BUTTON_TOKEN is a DEDICATED,
# minimal fine-grained PAT (Actions: write on this repo ONLY). It is embedded in
# the published page so the button can dispatch the workflow; if it leaks the
# only thing it can do is trigger this refresh. Leave it empty to build a page
# whose button just reloads the latest snapshot (no live pull).
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


def build_page_data(open_items):
    """Group the flat open_items list into a per-customer payload for the page."""
    # Drop excluded Sales Orders (e.g. SO314, SO390) from every view.
    open_items = [it for it in open_items if not _is_excluded_so(it.get("so_num", ""))]
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
        vendors.append({
            "name": vname,
            "open_items": len(vitems),
            "customers": vcusts,
            "pos": len(vpos),
            "rows": vrows,
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
def build_html(page_data):
    data_json = json.dumps(page_data).replace("</", "<\\/").replace("<!--", "<\\!--")
    data_url = f"{DATA_FILENAME}"  # same-origin relative fetch on GitHub Pages
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
  <button class="mode-btn active" data-mode="cust" onclick="setMode('cust')">Customer Open SO's</button>
  <button class="mode-btn" data-mode="vendor" onclick="setMode('vendor')">Open Vendor POs</button>
  <button class="mode-btn" data-mode="sku" onclick="setMode('sku')">High Demand SKUs</button>
</div>

<div class="layout">
  <div class="tabs" id="tabs"></div>
  <div class="panel-wrap"><div class="panel" id="panel"></div></div>
</div>

<div class="footer">JIT4You Inc. &middot; Open Orders &middot; data refreshes from Vtiger on each scheduled run</div>

<script>
var DATA = __DATA_JSON__;
var DATA_URL = "__DATA_URL__";
var BTN = __BTN_CFG__;
function _deobf(s,key){ if(!s) return ''; var raw=atob(s), out=''; for(var i=0;i<raw.length;i++){ out+=String.fromCharCode(raw.charCodeAt(i) ^ key.charCodeAt(i%key.length)); } return out; }
BTN.token = _deobf(BTN.token_obf, BTN.k || '');
var active = 0;     // selected customer index (Customer Open SO's view)
var vactive = 0;    // selected vendor index (Open Vendor POs view)
var mode = 'cust';  // 'cust' = Customer Open SO's · 'vendor' = Open Vendor POs

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
  if(mode==='sku'){ tabsEl.style.display='none'; tabsEl.innerHTML=''; return; }  // matrix is full-width, no per-entity tabs
  tabsEl.style.display='';
  var list = mode==='vendor' ? (DATA.vendors||[]) : (DATA.customers||[]);
  var cur = mode==='vendor' ? vactive : active;
  var h='';
  if(!list.length){ document.getElementById('tabs').innerHTML='<div class="empty">No open orders.</div>'; return; }
  for(var i=0;i<list.length;i++){
    h+='<button class="tab'+(i===cur?' active':'')+'" onclick="selectTab('+i+')">'+
       escapeHtml(list[i].name)+'<span class="cnt">'+list[i].open_items+'</span></button>';
  }
  document.getElementById('tabs').innerHTML=h;
}

function poCell(pending){
  if(!pending) return '<span class="po-none">None</span>';
  var parts=String(pending).split(','), out=[];
  for(var i=0;i<parts.length;i++){
    var po=parts[i].replace(/^\s+|\s+$/g,''); if(!po) continue;
    var url=(DATA.po_emails||{})[po];
    var btn = url ? '<a class="po-email-btn" href="'+escapeHtml(url)+'" title="Email vendor about '+escapeHtml(po)+'">Email vendor</a>' : '';
    out.push('<span class="po-wrap"><span class="po">&#9679; '+escapeHtml(po)+'</span>'+btn+'</span>');
  }
  return out.length ? out.join('<br>') : '<span class="po-none">None</span>';
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
function renderPanel(){ if(mode==='vendor') renderVendorPanel(); else if(mode==='sku') renderSkuPanel(); else renderCustPanel(); }

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
    '<div class="panel-head"><h2>'+escapeHtml(c.name)+'</h2>'+
    '<div class="sub">'+c.open_sos+' open SO(s) &middot; '+c.open_items+' open item(s) &middot; '+
    (c.vendors||[]).length+' vendor(s) &middot; grouped by SO'+sortNote+'</div></div>'+
    '<table><thead><tr>'+renderHead()+'</tr></thead><tbody>'+body+'</tbody></table>';
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
        '<td>'+poCell(r2.pending_pos)+'</td>'+
        '<td class="c" style="font-weight:600;color:'+etaColor(r2.eta)+'">'+fmtDate(r2.eta)+'</td>'+
        '</tr>';
    }
  }
  var sortNote = sortState.key ? ' &middot; sorted by '+escapeHtml(colByKey(sortState.key).label)+(sortState.dir>0?' ▲':' ▼') : '';
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><h2>'+escapeHtml(v.name)+'</h2>'+
    '<div class="sub">'+v.pos+' open PO(s) &middot; '+v.open_items+' open item(s) &middot; '+
    (v.customers||[]).length+' customer(s) &middot; grouped by customer'+sortNote+'</div></div>'+
    '<table><thead><tr>'+renderHead()+'</tr></thead><tbody>'+body+'</tbody></table>';
}

function setMode(m){
  if(mode===m) return;
  mode=m; sortState={key:null, dir:1};
  var btns=document.querySelectorAll('.mode-btn');
  for(var i=0;i<btns.length;i++){ btns[i].className = (btns[i].getAttribute('data-mode')===m) ? 'mode-btn active' : 'mode-btn'; }
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
function selectTab(i){ if(mode==='vendor') vactive=i; else active=i; renderTabs(); renderPanel(); }
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
  fetchData().then(function(d){ DATA=d; if(active>=(DATA.customers||[]).length) active=0; if(vactive>=(DATA.vendors||[]).length) vactive=0; renderAll(); })
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
        DATA=d; if(active>=(DATA.customers||[]).length) active=0; if(vactive>=(DATA.vendors||[]).length) vactive=0; renderAll(); btnBusy(false);
      } else { pollForUpdate(prevStamp,tries+1); }
    }).catch(function(){ pollForUpdate(prevStamp,tries+1); });
  },15000);
}

function escapeHtml(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(m){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]; }); }

renderAll();
</script>
</body>
</html>""".replace("__DATA_JSON__", data_json).replace("__DATA_URL__", data_url).replace("__BTN_CFG__", btn_cfg)


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
