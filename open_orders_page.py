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
from open_orders_report import VtigerAPI, extract_open_orders, CONFIG, log

# ─────────────────────────────────────────────
# GitHub Pages publishing (same host/repo as the customer-order-status reports)
# ─────────────────────────────────────────────
GITHUB_REPO = os.environ.get("GH_PAGES_REPO", "JIT4Labs1/customer-order-status")
GITHUB_TOKEN = os.environ.get("GH_PAT_TOKEN", "")
GITHUB_PAGES_URL = os.environ.get("GH_PAGES_URL", "https://jit4labs1.github.io/customer-order-status")

PAGE_FILENAME = "open-orders.html"
DATA_FILENAME = "open-orders-data.json"

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


# ─────────────────────────────────────────────
# Shape the extracted open_items into a per-customer structure for the page
# ─────────────────────────────────────────────
def build_page_data(open_items):
    """Group the flat open_items list into a per-customer payload for the page."""
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

    totals = {
        "customers": len(customers),
        "open_sos": len(set((i["customer"], i["so_num"]) for i in open_items)),
        "open_items": len(open_items),
    }
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": totals,
        "customers": customers,
    }


# ─────────────────────────────────────────────
# HTML page (self-contained; tabs + Refresh button; renders from embedded JSON
# and re-fetches the JSON snapshot on Refresh)
# ─────────────────────────────────────────────
def build_html(page_data):
    data_json = json.dumps(page_data).replace("</", "<\\/").replace("<!--", "<\\!--")
    data_url = f"{DATA_FILENAME}"  # same-origin relative fetch on GitHub Pages
    btn_cfg = json.dumps({
        "token": GH_BUTTON_TOKEN,
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
  tbody td { padding:8px 10px; border-bottom:1px solid #eef2f6; vertical-align:top; }
  tbody td.c { text-align:center; }
  tbody tr:nth-child(even) { background:#f8fafc; }
  .so { font-weight:700; color:#1F4E79; white-space:nowrap; }
  .status { padding:2px 8px; border-radius:10px; font-size:10px; white-space:nowrap; }
  .open { font-weight:700; color:#c0392b; }
  .po { color:#e67e22; white-space:nowrap; }
  .po-none { color:#999; }
  .empty { padding:40px; text-align:center; color:#999; }
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

<div class="layout">
  <div class="tabs" id="tabs"></div>
  <div class="panel-wrap"><div class="panel" id="panel"></div></div>
</div>

<div class="footer">JIT4You Inc. &middot; Open Orders &middot; data refreshes from Vtiger on each scheduled run</div>

<script>
var DATA = __DATA_JSON__;
var DATA_URL = "__DATA_URL__";
var BTN = __BTN_CFG__;
var active = 0;

function fmtQty(q){ q=Number(q)||0; return Number.isInteger(q)?String(q):q.toFixed(2).replace(/\\.?0+$/,''); }
function fmtDate(s){ if(!s) return '—'; var d=new Date(s+'T00:00:00'); if(isNaN(d)) return s;
  return d.toLocaleDateString('en-US',{month:'short',day:'2-digit',year:'numeric'}); }
function statusColors(st){ if(/Partial/.test(st)) return ['#fff3cd','#856404'];
  if(st==='Approved') return ['#d4edda','#155724']; return ['#cce5ff','#004085']; }
function etaColor(s){ if(!s) return '#999'; var d=new Date(s+'T00:00:00'); if(isNaN(d)) return '#2c3e50';
  var days=Math.floor((d-new Date())/86400000); return days<0?'#c0392b':(days<=7?'#e67e22':'#27ae60'); }

function renderKpis(){
  var t=DATA.totals||{};
  document.getElementById('kpis').innerHTML =
    kpi(t.customers,'Customers')+kpi(t.open_sos,'Open SOs')+kpi(t.open_items,'Open Items');
}
function kpi(v,l){ return '<div class="kpi"><div class="v">'+(v==null?'0':v)+'</div><div class="l">'+l+'</div></div>'; }

function renderTabs(){
  var c=DATA.customers||[]; var h='';
  if(!c.length){ document.getElementById('tabs').innerHTML='<div class="empty">No open orders.</div>'; return; }
  for(var i=0;i<c.length;i++){
    h+='<button class="tab'+(i===active?' active':'')+'" onclick="selectTab('+i+')">'+
       escapeHtml(c[i].name)+'<span class="cnt">'+c[i].open_items+'</span></button>';
  }
  document.getElementById('tabs').innerHTML=h;
}

function renderPanel(){
  var c=(DATA.customers||[])[active];
  if(!c){ document.getElementById('panel').innerHTML='<div class="empty">No open orders.</div>'; return; }
  var rows='';
  for(var i=0;i<c.rows.length;i++){
    var r=c.rows[i]; var sc=statusColors(r.so_status);
    var po = r.pending_pos ? '<span class="po">&#9679; '+escapeHtml(r.pending_pos)+'</span>' : '<span class="po-none">None</span>';
    rows+='<tr>'+
      '<td class="so">'+escapeHtml(r.so_num)+'</td>'+
      '<td><span class="status" style="background:'+sc[0]+';color:'+sc[1]+'">'+escapeHtml(r.so_status)+'</span></td>'+
      '<td>'+fmtDate(r.order_date)+'</td>'+
      '<td>'+escapeHtml(r.product)+'</td>'+
      '<td>'+escapeHtml(r.vendor)+'</td>'+
      '<td class="c">'+fmtQty(r.ordered_qty)+'</td>'+
      '<td class="c">'+fmtQty(r.delivered_qty)+'</td>'+
      '<td class="c open">'+fmtQty(r.open_qty)+'</td>'+
      '<td>'+po+'</td>'+
      '<td class="c" style="font-weight:600;color:'+etaColor(r.eta)+'">'+fmtDate(r.eta)+'</td>'+
      '</tr>';
  }
  document.getElementById('panel').innerHTML =
    '<div class="panel-head"><h2>'+escapeHtml(c.name)+'</h2>'+
    '<div class="sub">'+c.open_sos+' open SO(s) &middot; '+c.open_items+' open item(s) &middot; '+
    (c.vendors||[]).length+' vendor(s)</div></div>'+
    '<table><thead><tr>'+
    '<th>SO #</th><th>Status</th><th>Order Date</th><th>Product</th><th>Vendor</th>'+
    '<th class="c">Ord</th><th class="c">Del</th><th class="c">Open</th><th>Pending PO</th><th class="c">ETA</th>'+
    '</tr></thead><tbody>'+rows+'</tbody></table>';
}

function renderAsOf(){
  document.getElementById('asof').textContent = 'As of '+(DATA.generated_at||'—');
}
function selectTab(i){ active=i; renderTabs(); renderPanel(); }
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
  fetchData().then(function(d){ DATA=d; if(active>=(DATA.customers||[]).length) active=0; renderAll(); })
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
        DATA=d; if(active>=(DATA.customers||[]).length) active=0; renderAll(); btnBusy(false);
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
