#!/usr/bin/env python3
"""Bulk Invoice Check backend.
Usage:  python3 invoice_bulk.py [incoming.json]   (default _invbulk_incoming.json)

incoming.json = list of {msg_id, source ('zoho'|'quickbooks'), sender, subject, received, filename,
                         vendor, po_raw, lines:[{sku, desc, qty, rate}]}  (+ optional top-level meta in
                         {"scan":{"window":..,"since":..,"scanned":N,"request_id":..},"items":[...]})
Merges into invoice-bulk-data.json (NEVER drops existing results — only the page's Clear button does),
matches each PO in Vtiger (PO949 / 949 / PO 949 / PO-949 variants) and compares invoice unit rate to the
PO line-item list price per SKU. Fetches the current repo copy first so a Clear done on the page is honoured.
"""
import json, sys, re, os, base64, datetime, time, urllib.request
import build_payment_status as B

OUT = "invoice-bulk-data.json"
REPO_BASE = "https://jit4labs1.github.io/customer-order-status/"

def po_variants(raw):
    raw = str(raw or "").strip().upper()
    digits = re.sub(r"[^0-9]", "", raw)
    v = []
    for c in [raw, raw.replace(" ", ""), ("PO" + digits) if digits else "", digits,
              ("PO " + digits) if digits else "", ("PO-" + digits) if digits else ""]:
        if c and c not in v:
            v.append(c)
    return v

_prod_cache = {}
def sku_of(pid):
    if pid in _prod_cache:
        return _prod_cache[pid]
    r = B.vtiger_query(f"SELECT id, productcode FROM Products WHERE id = '{pid}';")
    code = (r[0].get("productcode") or "").strip().upper() if r else ""
    _prod_cache[pid] = code
    return code

def lookup_po(raw):
    vs = po_variants(raw)
    if not vs:
        return None
    inlist = ",".join("'" + x.replace("'", "") + "'" for x in vs)
    rows = []
    for attempt in range(3):   # vtiger_query swallows throttling errors and returns [] -> retry before declaring "not found"
        rows = B.vtiger_query(f"SELECT id, purchaseorder_no, postatus, vendor_id FROM PurchaseOrder WHERE purchaseorder_no IN ({inlist});")
        if rows: break
        time.sleep(6)
    rows = [r for r in rows if (r.get("postatus") or "").lower() != "cancelled"] or rows
    if not rows:
        return None
    r = rows[0]
    rec = B.vtiger_retrieve(r["id"])
    if rec is B.RETRIEVE_FAILED:
        return {"po_key": r.get("purchaseorder_no"), "status": r.get("postatus") or "", "vendor": "", "skus": None}
    skus = {}
    for it in rec.get("LineItems", []) or []:
        code = sku_of(it.get("productid", ""))
        if not code:
            continue
        try: unit = float(it.get("listprice") or it.get("netprice") or 0)
        except Exception: unit = 0.0
        if code not in skus or (skus[code]["unit"] == 0 and unit):
            skus[code] = {"unit": round(unit, 2), "product": it.get("product_name") or ""}
    vendor = ""
    if rec.get("vendor_id"):
        vr = B.vtiger_query(f"SELECT vendorname FROM Vendors WHERE id = '{rec['vendor_id']}';")
        vendor = (vr[0].get("vendorname") if vr else "") or ""
    return {"po_key": r.get("purchaseorder_no"), "status": r.get("postatus") or "", "vendor": vendor, "skus": skus}

def compare(item):
    po = lookup_po(item.get("po_raw"))
    out = dict(item)
    lines = []
    if not po or po["skus"] is None:
        out["verdict"] = "no_po" if not po else "unreadable"
        out["po_key"] = (po or {}).get("po_key") or ""
        out["lines"] = [{"sku": l.get("sku"), "desc": l.get("desc"), "qty": l.get("qty"), "rate": l.get("rate"),
                         "po_unit": None, "delta": None, "status": "notpo"} for l in item.get("lines", [])]
        out["summary"] = {"ok": 0, "bad": 0, "miss": len(out["lines"])}
        return out
    out["po_key"] = po["po_key"]; out["po_status"] = po["status"]
    if po["vendor"] and not out.get("vendor"): out["vendor"] = po["vendor"]
    ok = bad = miss = 0; seen = set()
    for l in item.get("lines", []):
        sku = str(l.get("sku") or "").strip().upper(); seen.add(sku)
        pu = po["skus"].get(sku)
        psku = sku
        if pu is None:   # vendor invoices often add a suffix (OSR6137-BK vs PO OSR6137)
            for cand in (sku.split("-")[0], re.sub(r"-[A-Z]{1,3}$", "", sku)):
                if cand in po["skus"]:
                    pu, psku = po["skus"][cand], cand; break
        rate = l.get("rate")
        if pu is None or rate is None:
            st, pun, d = "notpo", (pu or {}).get("unit"), None; miss += 1
        else:
            pun = pu["unit"]; d = round(float(rate) - pun, 2)
            if abs(d) < 0.005: st = "ok"; ok += 1
            elif d > 0: st = "over"; bad += 1
            else: st = "under"; bad += 1
        seen.add(psku)
        lines.append({"sku": sku, "desc": l.get("desc"), "qty": l.get("qty"), "rate": rate, "po_unit": pun, "delta": d, "status": st})
    out["lines"] = lines
    out["po_missing"] = [k for k in po["skus"] if k not in seen]
    out["summary"] = {"ok": ok, "bad": bad, "miss": miss}
    out["verdict"] = "discrepancy" if bad else (("ok" if not miss else "partial") if ok else ("partial" if miss else "unreadable"))
    return out

def load_base():
    try:
        with urllib.request.urlopen(REPO_BASE + OUT + "?cb=" + str(datetime.datetime.utcnow().timestamp()), timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception:
        if os.path.exists(OUT):
            return json.load(open(OUT))
        return {"version": 1, "results": []}

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "_invbulk_incoming.json"
    inc = json.load(open(src))
    meta = {}
    if isinstance(inc, dict):
        meta = inc.get("scan", {}); inc = inc.get("items", [])
    base = load_base()
    have = {r["key"] for r in base.get("results", [])}
    now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    sid = meta.get("request_id") or ("manual-" + now)
    added = 0
    for i, r in enumerate(base.get("results", [])):   # re-check earlier lookups that failed / found no PO
        if r.get("verdict") in ("no_po", "unreadable") and r.get("lines") is not None:
            src = dict(r); src["lines"] = [{"sku": l.get("sku"), "desc": l.get("desc"), "qty": l.get("qty"), "rate": l.get("rate")} for l in r["lines"]]
            for k in ("po_missing", "summary", "verdict", "po_key", "po_status"): src.pop(k, None)
            nr = compare(src)
            if nr["verdict"] not in ("no_po", "unreadable"):
                nr["key"], nr["found_at"], nr["scan_id"] = r["key"], r.get("found_at"), r.get("scan_id")
                base["results"][i] = nr
    for it in inc:
        key = f"{it['msg_id']}|{it.get('filename','')}"
        if key in have:
            continue
        r = compare(it); r["key"] = key; r["found_at"] = now; r["scan_id"] = sid
        base.setdefault("results", []).append(r); have.add(key); added += 1
    base["last_scan"] = {"id": sid, "at": now, "window": meta.get("window", ""), "since": meta.get("since", ""),
                         "scanned": meta.get("scanned", len(inc)), "added": added}
    json.dump(base, open(OUT, "w"), indent=2)
    print(f"merged: +{added} new, {len(base['results'])} total -> {OUT}")

if __name__ == "__main__":
    main()
