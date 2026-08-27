#!/usr/bin/env python3
"""
Build a SELF-CONTAINED local mirror of the dashboard, so it still works if the
GitHub Pages site / account becomes unavailable.

Output folder (next to this script):  JIT4You-Dashboard-Local/
  - JIT4You-Dashboard.html   (open it by double-clicking — all data baked in, no internet needed)
  - open-orders-data.json, google-ads-data.json, linkedin-data.json, website-traffic-data.json  (raw backups)

Data sources:
  - The 3 marketing files are read from the local QB Files copies (written by the refresh run).
  - open-orders-data.json (Vtiger customers/vendors/P&L/analysis) is taken from a local cache if
    present; otherwise fetched once from GitHub Pages and cached locally. If GitHub is down and no
    cache exists yet, the mirror is built without that tab's data (a note is shown).

Run:  python3 build_local_mirror.py
Intended to run at the END of each refresh so the offline copy stays current.
"""
import os, json, shutil, urllib.request, importlib.util
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MIRROR = os.path.join(HERE, "JIT4You-Dashboard-Local")
DATA_FILES = ["open-orders-data.json", "google-ads-data.json", "linkedin-data.json", "website-traffic-data.json", "ups-shipments-data.json", "payment-status-data.json", "shipments-pnl-data.json", "customer-journey-data.json"]


def _load_oop():
    spec = importlib.util.spec_from_file_location("oop", os.path.join(HERE, "open_orders_page.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _read_local_json(name):
    p = os.path.join(HERE, name)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception as e:
            print(f"  WARN: could not parse local {name}: {e}")
    return None


def _get_page_data(oop):
    # If this refresh run pulled Vtiger open-orders locally (open_orders_page.py
    # --no-push in the task's Vtiger step), the local open-orders-data.json is the
    # freshest copy — prefer it and do NOT clobber it with GitHub's (older) copy or
    # wait on Pages CDN deploy lag. Signalled by MIRROR_USE_LOCAL_OO=1.
    if os.environ.get("MIRROR_USE_LOCAL_OO") == "1":
        local = _read_local_json("open-orders-data.json")
        if local is not None:
            print(f"  open-orders-data.json: using fresh LOCAL copy (generated_at "
                  f"{local.get('generated_at','?')}) — MIRROR_USE_LOCAL_OO=1")
            return local
        print("  MIRROR_USE_LOCAL_OO=1 but no local open-orders-data.json found; falling back to GitHub")
    # Fetch the freshest copy from GitHub when reachable (and refresh the local cache);
    # fall back to the last local cache if GitHub is unavailable.
    cache = os.path.join(HERE, "open-orders-data.json")
    try:
        url = f"{oop.GITHUB_PAGES_URL}/{oop.DATA_FILENAME}?cb=mirror"
        pd = json.loads(urllib.request.urlopen(url, timeout=30).read().decode())
        json.dump(pd, open(cache, "w"))  # refresh local cache for offline use
        print("  open-orders-data.json: fetched fresh from GitHub (cache updated)")
        return pd
    except Exception as e:
        print(f"  open-orders-data.json: GitHub unreachable ({e}); using local cache")
        local = _read_local_json("open-orders-data.json")
        if local is not None:
            return local
        return {"generated_at": "(unavailable offline)", "totals": {}, "customers": [], "vendors": [],
                "pnl_html": "<div class='empty'>Vtiger data not cached locally yet — open the online "
                            "dashboard once while GitHub is reachable to cache it.</div>",
                "customer_analysis": {"customers": []}}


def main():
    oop = _load_oop()
    os.makedirs(MIRROR, exist_ok=True)
    page_data = _get_page_data(oop)
    embeds = {
        "gads": _read_local_json("google-ads-data.json"),
        "li":   _read_local_json("linkedin-data.json"),
        "wt":   _read_local_json("website-traffic-data.json"),
        "ship": _read_local_json("ups-shipments-data.json"),
        "pay":  _read_local_json("payment-status-data.json"),
        "spnl": _read_local_json("shipments-pnl-data.json"),
        "cj":   _read_local_json("customer-journey-data.json"),
        "iopp": _read_local_json("inventory-opportunities.json"),
    }
    html = oop.build_html(page_data, embeds=embeds)
    stamp = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    banner = (f'<div style="background:#fff3cd;border-bottom:1px solid #ffe08a;color:#7a5b00;'
              f'font:13px -apple-system,Segoe UI,Roboto,sans-serif;padding:7px 16px;text-align:center;">'
              f'\U0001F4BE Local offline copy &middot; built {stamp} &middot; data is a snapshot from the last refresh</div>')
    html = html.replace("<body>", "<body>\n" + banner, 1)
    out = os.path.join(MIRROR, "JIT4You-Dashboard.html")
    open(out, "w").write(html)
    # raw json backups alongside
    for f in DATA_FILES:
        src = os.path.join(HERE, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(MIRROR, f))
    print(f"Local mirror written: {out}")


if __name__ == "__main__":
    main()
