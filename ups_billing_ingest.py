#!/usr/bin/env python3
"""
UPS Billing Center CSV -> ups-billing-data.json

Parses a UPS Billing Center charges export and produces a compact per-package
charge feed for the Shipments P&L tab:
  charges[] = {tracking, refs:[ref1, ref2], net, invoice_date, desc}

Column detection is tolerant of the common UPS Billing Center header variants
(Tracking Number / Package Reference; Reference Number 1/2; Net Amount / Net
Charge). Multiple charge lines for the same tracking are summed. Run after you
download the CSV:  python3 ups_billing_ingest.py [path-to-csv]
If no path is given, the newest *.csv whose name looks like a UPS billing export
in the QB Files folder is used.
"""
import os, sys, csv, json, glob, datetime, re

QB = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(QB, "ups-billing-data.json")

# candidate header names (lowercased, non-alnum stripped) -> canonical field
TRACK_KEYS = ["trackingnumber", "tracking", "packagereference1number", "leadshipmentnumber",
              "packagetrackingnumber", "shipmenttrackingnumber"]
REF1_KEYS  = ["referencenumber1", "referenceno1", "reference1", "packagereference1",
              "shipmentreferencenumber1", "referencenumber"]
REF2_KEYS  = ["referencenumber2", "referenceno2", "reference2", "packagereference2",
              "shipmentreferencenumber2"]
NET_KEYS   = ["netamount", "netcharge", "net", "billedamount", "amount", "totalnetamount",
              "incentiveamount"]  # incentive handled specially below
DATE_KEYS  = ["invoicedate", "transactiondate", "pickupdate", "shipmentdate"]
DESC_KEYS  = ["chargedescription", "chargecategorydetail", "detailchargedescription",
              "chargeclassificationdetail", "chargetypedetail"]
SYS_KEYS   = ["shippingsystemadjustment", "shippingsystem", "chargecategory",
              "adjustment"]


def _norm_po(ref):
    """UPS reference fields sometimes carry 'P0664' (zero) for 'PO664'. Return the
    canonical PO number when the ref looks like a PO, else None."""
    m = re.match(r"^P[O0](\d+)$", (ref or "").strip().upper())
    return "PO" + m.group(1) if m else None


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _pick(headers, keys):
    hm = {_norm(h): h for h in headers}
    for k in keys:
        if k in hm:
            return hm[k]
    # loose contains-match fallback
    for k in keys:
        for nh, orig in hm.items():
            if k in nh:
                return orig
    return None


def _money(v):
    if v is None:
        return 0.0
    s = str(v).strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        n = float(s)
    except Exception:
        return 0.0
    return -n if neg else n


def parse_csv(path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except Exception:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        headers = reader.fieldnames or []
        c_track = _pick(headers, TRACK_KEYS)
        c_ref1 = _pick(headers, REF1_KEYS)
        c_ref2 = _pick(headers, REF2_KEYS)
        c_net = _pick(headers, NET_KEYS)
        c_date = _pick(headers, DATE_KEYS)
        c_desc = _pick(headers, DESC_KEYS)
        c_sys = _pick(headers, SYS_KEYS)
        detected = {"tracking": c_track, "ref1": c_ref1, "ref2": c_ref2,
                    "net": c_net, "date": c_date, "desc": c_desc, "category": c_sys}
        if not c_net or not (c_track or c_ref1):
            raise SystemExit(
                "Could not detect required columns in the CSV.\n"
                f"  Headers seen: {headers}\n"
                f"  Detected: {detected}\n"
                "Tell me the exact column names for Tracking, Reference 1/2 and Net Amount "
                "and I'll map them.")
        agg = {}     # key (tracking or PO ref) -> {tracking, refs, net, invoice_date}
        unattributed = {}   # category -> net  (fees/adjustments/3rd-party summaries: no tracking, no PO)
        unatt_total = 0.0
        rows = 0
        for row in reader:
            rows += 1
            tn = (row.get(c_track, "") or "").strip() if c_track else ""
            r1 = (row.get(c_ref1, "") or "").strip() if c_ref1 else ""
            r2 = (row.get(c_ref2, "") or "").strip() if c_ref2 else ""
            net = _money(row.get(c_net)) if c_net else 0.0
            # normalized refs: keep PO-looking refs (canonicalized) plus originals
            refs = []
            for x in (r1, r2):
                if not x:
                    continue
                po = _norm_po(x)
                refs.append(po or x)
            po_refs = [x for x in refs if _norm_po(x)]
            if tn:
                key = "T:" + tn
            elif po_refs:
                key = "P:" + po_refs[0]
            else:
                # account-level charge (fee / adjustment / third-party summary): not a package
                cat = ((row.get(c_sys, "") or "").strip() if c_sys else "") or "Other"
                unattributed[cat] = round(unattributed.get(cat, 0.0) + net, 2)
                unatt_total = round(unatt_total + net, 2)
                continue
            rec = agg.setdefault(key, {"tracking": tn, "refs": list(refs), "net": 0.0,
                                       "invoice_date": (row.get(c_date, "") or "").strip() if c_date else ""})
            rec["net"] = round(rec["net"] + net, 2)
            for x in refs:
                if x not in rec["refs"]:
                    rec["refs"].append(x)
        return list(agg.values()), detected, rows, headers, unattributed, unatt_total


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        cands = [p for p in glob.glob(os.path.join(QB, "*.csv"))
                 if re.search(r"(ups|billing|invoice|charge)", os.path.basename(p), re.I)]
        if not cands:
            cands = glob.glob(os.path.join(QB, "*.csv"))
        if not cands:
            raise SystemExit("No CSV found. Drop the UPS Billing Center export in QB Files "
                             "or pass its path: python3 ups_billing_ingest.py <file.csv>")
        path = max(cands, key=os.path.getmtime)
    charges, detected, rows, headers, unattributed, unatt_total = parse_csv(path)
    total = round(sum(c["net"] for c in charges), 2)
    now = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
    out = {"generated_at": now, "source_file": os.path.basename(path),
           "detected_columns": detected, "row_count": rows,
           "package_count": len(charges), "total_net": total,
           "unattributed_total": unatt_total, "unattributed": unattributed,
           "charges": charges}
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT}")
    print(f"  source: {os.path.basename(path)} | rows: {rows} | package/PO charges: {len(charges)} | package net: ${total:,.2f}")
    print(f"  unattributed account charges (fees/adjustments/3rd-party summaries): ${unatt_total:,.2f}")
    for k, v in sorted(unattributed.items(), key=lambda x: -x[1]):
        print(f"      {k}: ${v:,.2f}")
    print(f"  detected columns: {detected}")


if __name__ == "__main__":
    main()
