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
    _m = re.findall(r"PO[\s\-#]*([0-9]+)", raw)   # "SO1047--PO822" -> 822 (not 1047822)
    digits = _m[-1] if _m else re.sub(r"[^0-9]", "", raw)
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

import re as _re, os as _os
def make_pdf(r, outdir=None):
    """Generate a readable PDF copy of an invoice from its extracted data. Returns repo-relative path."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
    here = _os.path.dirname(_os.path.abspath(__file__))
    outdir = outdir or _os.path.join(here, "invoices")
    _os.makedirs(outdir, exist_ok=True)
    base = _re.sub(r"[^A-Za-z0-9._-]+", "_", _os.path.splitext(r.get("filename") or "invoice")[0]).strip("_") or "invoice"
    tag = _re.sub(r"[^A-Za-z0-9]", "", (r.get("msg_id") or ""))[-6:]
    rel = f"invoices/{base}_{tag}.pdf" if tag else f"invoices/{base}.pdf"
    st = getSampleStyleSheet()
    doc = SimpleDocTemplate(_os.path.join(here, rel), pagesize=letter, title=base, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    el = [Paragraph("Invoice copy", st["Title"]),
          Paragraph("<i>Generated from the data extracted from the vendor email &mdash; not the vendor's original PDF.</i>", st["Italic"]), Spacer(1, 10)]
    info = [["Vendor", r.get("vendor", "")], ["Invoice file", r.get("filename", "")], ["Source", "QuickBooks" if r.get("source") == "quickbooks" else "Zoho Books"],
            ["Email subject", (r.get("subject") or "")[:90]], ["Received (UTC)", str(r.get("received", ""))[:16].replace("T", " ")],
            ["PO # on invoice", r.get("po_raw", "")], ["Matched PO", f"{r.get('po_key','')} ({r.get('po_status','')})" if r.get("po_key") else "not found"]]
    t = Table(info, colWidths=[110, 400]); t.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    el += [t, Spacer(1, 12)]
    rows = [["SKU", "Description", "Qty", "Rate", "Amount", "PO unit", "Check"]]
    total = 0.0
    lab = {"ok": "OK", "over": "Overbilled", "under": "Underbilled", "notpo": "Not on PO"}
    for l in r.get("lines", []):
        q = l.get("qty") or 0; rt = l.get("rate") or 0; amt = q * rt; total += amt
        pu = l.get("po_unit")
        rows.append([str(l.get("sku", "")), str(l.get("desc", ""))[:34], f"{q:g}", f"${rt:,.2f}", f"${amt:,.2f}", "" if pu is None else f"${pu:,.2f}", lab.get(l.get("status"), str(l.get("status", "")))])
    rows.append(["", "", "", "Total", f"${total:,.2f}", "", ""])
    lt = Table(rows, repeatRows=1, colWidths=[78, 150, 34, 56, 62, 56, 74])
    lt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTNAME", (3, -1), (4, -1), "Helvetica-Bold"),
                            ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -2), 0.25, colors.grey), ("ALIGN", (2, 0), (5, -1), "RIGHT")]))
    el.append(lt)
    miss = r.get("po_missing") or []
    if miss:
        el += [Spacer(1, 10), Paragraph("On the PO but not on this invoice: " + ", ".join(map(str, miss)), st["Normal"])]
    doc.build(el)
    return rel

def inv_number(r):
    """Invoice number as it would be keyed as a QuickBooks Bill DocNumber."""
    fn = (r.get("filename") or "")
    m = _re.search(r"(INV-\d+)", fn, _re.I)
    if m: return m.group(1).upper()
    m = _re.search(r"[Ii]nvoice[_ ]+(\d+)", fn) or _re.search(r"invoice (\d+)", r.get("subject") or "", _re.I)
    return m.group(1) if m else ""

def _norm_doc(x):
    x = _re.sub(r"[^A-Z0-9]", "", str(x or "").upper())
    return x.lstrip("0") if x.isdigit() else x

def qb_check(results):
    """Mark each result with r['qb'] = {entered, number, bill_total, balance, date, id, total_match}.
    A result counts as entered when a QuickBooks Bill exists from that vendor with the same document number."""
    try:
        access = B.refresh_token()
        since = min([(r.get("received") or "9999")[:10] for r in results] + ["9999"])
        if since == "9999": return
        since = (datetime.date.fromisoformat(since) - datetime.timedelta(days=45)).isoformat()
        bills, start = [], 1
        while True:
            q = B.qb_query(access, f"SELECT * FROM Bill WHERE TxnDate >= '{since}' ORDERBY TxnDate DESC STARTPOSITION {start} MAXRESULTS 200")
            c = (q.get("QueryResponse", {}) or {}).get("Bill", []) or []
            bills += c
            if len(c) < 200: break
            start += 200
        idx = {}
        for b in bills:
            vn = ((b.get("VendorRef") or {}).get("name") or "").lower()
            idx[(("allora" if "allora" in vn else "pma" if "pma" in vn else vn), _norm_doc(b.get("DocNumber")))] = b
        now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        for r in results:
            sup = "allora" if (r.get("source") == "quickbooks" or "allora" in (r.get("vendor") or "").lower()) else "pma"
            b = idx.get((sup, _norm_doc(inv_number(r))))
            if b:
                tot = sum((float(l.get("qty") or 0) * float(l.get("rate") or 0)) for l in (r.get("lines") or []))
                bt = float(b.get("TotalAmt") or 0)
                r["qb"] = {"entered": True, "id": b.get("Id"), "number": b.get("DocNumber"), "bill_total": bt,
                           "balance": float(b.get("Balance") or 0), "date": b.get("TxnDate"),
                           "total_match": abs(tot - bt) < 0.02 if tot else None, "checked": now}
            else:
                r["qb"] = {"entered": False, "checked": now}
    except Exception as e:
        print("qb_check failed:", e)

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
        try: r["file"] = make_pdf(r)
        except Exception as e: print("pdf failed", key, e)
        base.setdefault("results", []).append(r); have.add(key); added += 1
    qb_check(base.get("results", []))
    base["last_scan"] = {"id": sid, "at": now, "window": meta.get("window", ""), "since": meta.get("since", ""),
                         "scanned": meta.get("scanned", len(inc)), "added": added}
    json.dump(base, open(OUT, "w"), indent=2)
    print(f"merged: +{added} new, {len(base['results'])} total -> {OUT}")

if __name__ == "__main__":
    main()
