#!/usr/bin/env python3
"""
Shipments P&L — shipping charged to the customer (SKU 999 line on the Vtiger SO)
vs. what UPS actually bills us (net charge from the UPS Billing Center export,
matched to the SO through the PO in Reference 1/2, or through the tracking #).

Data feeding the "Shipments P&L" dashboard tab (shipments-pnl-data.json):
  rows[] = one row per Sales Order that has at least one UPS shipment, each with
  customer, SO#, date, PO(s), # UPS packages, shipping revenue (SKU 999),
  UPS cost (matched net charges), and whether cost was matched.

Revenue: Vtiger — SKU 999 ("Shipping") line total on each 2026 non-cancelled SO
         that has a UPS shipment in ups-shipments-data.json.
Cost:    ups-billing-data.json (produced by ups_billing_ingest.py from the UPS
         Billing Center CSV). Empty/absent => costs are "pending" (revenue only).
UPS only (per requirement); FedEx / Pirate Ship excluded.
READ-ONLY on Vtiger.
"""
import os, json, datetime, re, time, urllib.request

PAGES_URL = os.environ.get("GH_PAGES_URL", "https://jit4labs1.github.io/customer-order-status")
INVOICE_DIR = "invoices"  # re-hosted bill PDFs (public, for the vendor email link)

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:
    _PT = None

QB = os.path.dirname(os.path.abspath(__file__))
SHIPS_FILE = os.path.join(QB, "ups-shipments-data.json")
BILLING_FILE = os.path.join(QB, "ups-billing-data.json")
OUT_FILE = os.path.join(QB, "shipments-pnl-data.json")
ACCEPTED_FILE = os.path.join(QB, "spnl_accepted.json")  # {accepted:[so_id,...]} user-accepted discrepancies

SHIP_PID = "6x56546"      # Vtiger product id for SKU 999 "Shipping"
SHIP_CODE = "999"


def _f(v):
    try:
        return float(str(v).strip())
    except Exception:
        return 0.0


def _pac_now():
    return datetime.datetime.now(_PT) if _PT else datetime.datetime.now()


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _load_billing():
    """Return (lines, asof, unatt_total, unatt_breakdown, source_file)."""
    d = _load_json(BILLING_FILE, None)
    if not d:
        return [], "", 0.0, {}, ""
    if isinstance(d, dict):
        return (d.get("charges", d.get("lines", [])) or [],
                d.get("generated_at", d.get("asof", "")),
                _f(d.get("unattributed_total", 0)), d.get("unattributed", {}) or {},
                d.get("source_file", ""))
    return d, "", 0.0, {}, ""


def _crmid(soid):
    # Vtiger retrieve needs the module-prefixed crmid (SalesOrder module = 15),
    # e.g. "156771" -> "15x156771". Shipments store the bare numeric id.
    soid = str(soid or "")
    return soid if ("x" in soid or not soid) else ("15x" + soid)


def _bare(crmid):
    # crmid "15x156771" -> numeric "156771" (used for the Vtiger detail-view link).
    s = str(crmid or "")
    return s.split("x", 1)[1] if "x" in s else s


def build_shipments_pnl(vt):
    now = _pac_now()
    Y = now.year

    # 1) UPS shipments -> tracking/PO -> SO crmid (90-day window), + which SOs shipped UPS.
    ships = (_load_json(SHIPS_FILE, {}) or {}).get("shipments", [])
    ups = [s for s in ships if (s.get("carrier") == "UPS")]
    track2crmid = {}          # tracking -> SO crmid
    ship_crmids = set()       # SO crmids that have a UPS shipment on file
    ship_receiver = {}        # SO crmid -> receiver (fallback customer name)
    ship_trk = {}             # SO crmid -> set of trackings
    for s in ups:
        cid = _crmid(s.get("so_id"))
        if not cid:
            continue
        ship_crmids.add(cid)
        ship_receiver.setdefault(cid, s.get("receiver", ""))
        tn = (s.get("tracking") or "").strip()
        if tn:
            track2crmid.setdefault(tn, cid)
            ship_trk.setdefault(cid, set()).add(tn)

    # 2) Vtiger master maps (bulk, cached): PO# -> SO crmid; SO crmid -> meta; accounts; products.
    # Vendor id -> name, to keep ONLY Conmed POs on this tab.
    vend_name = {v["id"]: (v.get("vendorname", "") or "")
                 for v in vt.query_all("SELECT id, vendorname FROM Vendors")}
    def _is_conmed(vid):
        return "conmed" in (vend_name.get(vid, "") or "").lower()

    po2crmid = {}
    crmid_pos = {}          # SO crmid -> set of its Conmed PO#s (tab is Conmed-only)
    conmed_crmids = set()   # SO crmids that have at least one Conmed PO
    po_crmid = {}           # Conmed PO# -> PO crmid (to retrieve its line items)
    for po in vt.query_all("SELECT id, purchaseorder_no, salesorder_id, vendor_id FROM PurchaseOrder"):
        pn = (po.get("purchaseorder_no") or "").strip().upper()
        sid = (po.get("salesorder_id") or "").strip()
        if not (pn and sid):
            continue
        po2crmid[pn] = sid
        if _is_conmed(po.get("vendor_id", "")):
            crmid_pos.setdefault(sid, set()).add(pn)
            conmed_crmids.add(sid)
            po_crmid[pn] = po.get("id")

    def _po_info(po_numbers):
        """(row_count, items[{product,qty}], order_date) across the given Conmed PO(s)."""
        total = 0
        items = []
        odate = ""
        for pn in po_numbers:
            pc = po_crmid.get(pn)
            if not pc:
                continue
            det = vt.retrieve_with_retry(pc, label="PO-PNL")
            if not det:
                continue
            lis = det.get("LineItems", det.get("lineItems", [])) or []
            total += len(lis)
            if not odate:
                odate = str(det.get("orderdate") or det.get("createdtime", ""))[:10]
            for li in lis:
                items.append({"product": (li.get("product_name") or li.get("productname") or "").strip(),
                              "qty": _f(li.get("quantity", 1))})
        return total, items, odate

    _today = _pac_now().strftime("%Y-%m-%d")

    def _qb_conmed_bills():
        """PO# -> matched QuickBooks Conmed vendor bill (the invoice for that PO).
        Conmed bills store the Vtiger PO number in each Line's Description.
        Cached per-day so repeated resumable passes don't re-query QB."""
        cache = os.path.join(QB, f"_qb_bills_{_today}.json")
        cached = _load_json(cache, None)
        if cached is not None:
            return cached
        try:
            import build_payment_status as _qb
            acc = _qb.refresh_token()
            realm = getattr(_qb, "REALM", "")
            r = _qb.qb_query(acc, "SELECT * FROM Bill WHERE VendorRef='450' MAXRESULTS 1000")
            bills = r.get("QueryResponse", {}).get("Bill", []) or []
        except Exception as e:
            print("  QB Conmed bills unavailable:", e)
            return {}
        po_map = {}
        for b in bills:
            lines = []
            for ln in b.get("Line", []) or []:
                lines.append({"desc": (ln.get("Description") or "").strip(),
                              "amount": round(_f(ln.get("Amount")), 2)})
            bid = b.get("Id", "")
            info = {"doc_number": b.get("DocNumber", ""), "date": b.get("TxnDate", ""),
                    "total": round(_f(b.get("TotalAmt")), 2), "balance": round(_f(b.get("Balance")), 2),
                    "id": bid, "lines": lines,
                    "view_url": ("https://qbo.intuit.com/app/login?pagereq=bill%3FtxnId%3D"
                                 + str(bid) + "&deeplinkcompanyid=" + str(realm))}
            for ln in lines:
                m = re.match(r"(PO\d+)", (ln["desc"] or "").upper())
                if m:
                    po_map[m.group(1)] = info
        try:
            json.dump(po_map, open(cache, "w"))
        except Exception:
            pass
        return po_map
    so_master = {}   # crmid -> {no, account_id, date, status}
    for s in vt.query_all(
            "SELECT id, salesorder_no, account_id, createdtime, sostatus FROM SalesOrder "
            "WHERE createdtime >= '%d-01-01' AND createdtime < '%d-01-01'" % (Y, Y + 1)):
        so_master[s["id"]] = {
            "no": (s.get("salesorder_no") or "").strip(),
            "account_id": s.get("account_id", ""),
            "date": str(s.get("createdtime", ""))[:10],
            "status": (s.get("sostatus", "") or "").strip(),
        }
    accts = {a["id"]: (a.get("accountname", "") or "")
             for a in vt.query_all("SELECT id, accountname FROM Accounts")}
    pcode = {}
    for p in vt.query_all("SELECT id, productcode FROM Products"):
        pcode[p["id"]] = (p.get("productcode") or "").strip()

    # 3) UPS billing charges -> cost per SO crmid. Match by tracking first, then by
    #    PO (Reference 1/2) -> SO across the FULL year (not just the 90-day shipments file).
    billing, billing_asof, unatt_total, unatt_breakdown, billing_src = _load_billing()
    so_cost = {}
    so_billed_pos = {}     # crmid -> set of PO#s that produced a matched charge
    so_billed_trk = {}     # crmid -> set of billed trackings
    matched_sos = set()
    unmatched_n = 0
    unmatched_cost = 0.0
    for bl in billing:
        net = _f(bl.get("net", bl.get("net_charge", bl.get("amount", 0))))
        tn = (bl.get("tracking") or "").strip()
        refs = [(r or "").strip() for r in (bl.get("refs") or [bl.get("ref1", ""), bl.get("ref2", "")])]
        cid = track2crmid.get(tn)
        matched_po = None
        if not cid:
            for r in refs:
                ru = r.upper()
                if ru in po2crmid:
                    cid = po2crmid[ru]
                    matched_po = ru
                    break
        if cid:
            so_cost[cid] = so_cost.get(cid, 0.0) + net
            matched_sos.add(cid)
            if matched_po:
                so_billed_pos.setdefault(cid, set()).add(matched_po)
            if tn:
                so_billed_trk.setdefault(cid, set()).add(tn)
        else:
            unmatched_n += 1
            unmatched_cost += net

    # 4) Universe = SOs with a UPS shipment OR a matched UPS charge, RESTRICTED to SOs
    #    whose PO vendor is Conmed (this tab is Conmed drop-ships only). Revenue from SKU 999.
    universe = (set(ship_crmids) | set(so_cost.keys())) & conmed_crmids
    rows = []
    disc_src = {}   # so_id -> {po_items, po_date, trackings} for discrepancy-email building
    for cid in universe:
        meta = so_master.get(cid, {})
        status = meta.get("status", "")
        if (status or "").lower() == "cancelled":
            continue
        detail = vt.retrieve_with_retry(cid, label="SO-PNL") if cid else None
        revenue = 0.0
        acct = ""
        if detail:
            acct = accts.get(detail.get("account_id", ""), "")
            for li in detail.get("LineItems", detail.get("lineItems", [])) or []:
                pid = str(li.get("productid", "") or "")
                name = (li.get("product_name") or li.get("productname")
                        or li.get("productid_display") or "")
                code = pcode.get(pid, "")
                is_ship = (pid == SHIP_PID or code == SHIP_CODE
                           or "shipping" in name.lower() or "freight" in name.lower())
                if not is_ship:
                    continue
                qty = _f(li.get("quantity", li.get("qty", 1))) or 1
                unit = _f(li.get("discounted_unit_selling_price"))
                if unit == 0:
                    unit = _f(li.get("listprice", li.get("netprice", 0)))
                revenue += unit * qty
        if not acct:
            acct = accts.get(meta.get("account_id", ""), "") or ship_receiver.get(cid, "") or "(no customer)"
        so_num = meta.get("no", "") or (detail.get("salesorder_no", "") if detail else "")
        date = meta.get("date", "") or (str(detail.get("createdtime", ""))[:10] if detail else "")
        # Show every PO linked to this SO in Vtiger (not only PO-matched charges), so
        # tracking-matched rows still display their PO. Prefer the actually-billed PO(s).
        pos = sorted(crmid_pos.get(cid, set()) or so_billed_pos.get(cid, set()))
        trk_set = so_billed_trk.get(cid) or ship_trk.get(cid) or set()
        n_pkgs = len(trk_set)
        po_rows, po_items, po_date = _po_info(pos)
        disc_src[_bare(cid)] = {"po_items": po_items, "po_date": po_date,
                                "trackings": sorted(trk_set)}
        rows.append({
            "customer": acct,
            "so_num": so_num,
            "so_id": _bare(cid),
            "date": date,
            "pos": pos,
            "po_rows": po_rows,
            "packages": n_pkgs,
            "revenue": round(revenue, 2),
            "cost": round(so_cost.get(cid, 0.0), 2),
            "has_cost": cid in matched_sos,
        })

    rows.sort(key=lambda r: (r["date"] or ""), reverse=True)

    # Discrepancy emails: for each row where Pkgs != PO Rows, assemble a vendor
    # (Conmed) alert package — PO items + date, UPS packages/trackings, and the
    # matching QuickBooks Conmed bill (the invoice for that PO, incl. other POs
    # billed on the same invoice).
    def _qb_bill_pdfs(bill_ids):
        """{bill_id -> public Pages URL of the invoice PDF}. Downloads each bill's
        attached PDF from QuickBooks and saves it under invoices/ so it can be
        published and opened by the vendor without a QuickBooks login."""
        out = {}
        if not bill_ids:
            return out
        try:
            import build_payment_status as _qb
            acc = _qb.refresh_token()
            base = f"{_qb.BASE}/company/{_qb.REALM}"
        except Exception as e:
            print("  QB invoice PDFs unavailable:", e)
            return out

        def _get(url, as_json=True, auth=True):
            last = None
            for attempt in range(6):
                try:
                    req = urllib.request.Request(url)
                    if auth:
                        req.add_header("Authorization", "Bearer " + acc)
                        req.add_header("Accept", "application/json")
                    data = urllib.request.urlopen(req, timeout=40).read()
                    return json.loads(data.decode()) if as_json else data
                except Exception as e:
                    last = e
                    time.sleep(2 * (attempt + 1))
            raise last

        # bill_id -> attachable id. Cached (attachment ids are stable); only scan
        # if some needed bill isn't already mapped. Saved incrementally so a
        # timed-out scan resumes on the next pass.
        att_cache = os.path.join(QB, "_qb_att_map.json")
        scan_file = os.path.join(QB, "_qb_att_scan.json")
        att = _load_json(att_cache, {}) or {}
        scan = _load_json(scan_file, {"pos": 1, "done": False})
        if not scan.get("done") and any(b not in att for b in bill_ids):
            try:
                pos = scan.get("pos", 1)
                while True:
                    r = _qb.qb_query(acc, f"SELECT * FROM Attachable STARTPOSITION {pos} MAXRESULTS 100")
                    ats = r.get("QueryResponse", {}).get("Attachable", []) or []
                    if not ats:
                        scan["done"] = True
                        break
                    for a in ats:
                        if a.get("ContentType") != "application/pdf":
                            continue
                        for rf in (a.get("AttachableRef", []) or []):
                            bid = str((rf.get("EntityRef") or {}).get("value"))
                            att.setdefault(bid, a.get("Id"))
                    pos += 100
                    scan["pos"] = pos
                    if len(ats) < 100:
                        scan["done"] = True
                    try:
                        json.dump(att, open(att_cache, "w"))
                        json.dump(scan, open(scan_file, "w"))
                    except Exception:
                        pass
                    if scan["done"] or all(b in att for b in bill_ids):
                        break
            except Exception as e:
                print("  QB attachable scan (partial, will resume):", e)

        os.makedirs(os.path.join(QB, INVOICE_DIR), exist_ok=True)
        for bid, doc in bill_ids.items():
            safe = re.sub(r"[^A-Za-z0-9._-]", "", "CONMED-" + str(doc) + ".pdf")
            dest = os.path.join(QB, INVOICE_DIR, safe)
            if os.path.exists(dest) and os.path.getsize(dest) > 100:
                out[bid] = f"{PAGES_URL}/{INVOICE_DIR}/{safe}"
                continue
            aid = att.get(bid)
            if not aid:
                continue
            try:
                a = _get(f"{base}/attachable/{aid}?minorversion=65").get("Attachable", {})
                tu = a.get("TempDownloadUri")
                if not tu:
                    continue
                pdf = _get(tu, as_json=False, auth=False)
                if pdf[:4] != b"%PDF":
                    continue
                with open(dest, "wb") as f:
                    f.write(pdf)
                out[bid] = f"{PAGES_URL}/{INVOICE_DIR}/{safe}"
            except Exception as e:
                print(f"  PDF for bill {bid} failed:", e)
        return out

    _acc = _load_json(ACCEPTED_FILE, {})
    accepted = set(_acc.get("accepted", []) if isinstance(_acc, dict) else (_acc or []))
    disc_emails = {}
    # Build emails for ALL discrepancies; the tab filters accepted ones (so accept/un-accept
    # takes effect immediately without a server rebuild).
    disc_rows = [r for r in rows if r["packages"] != r["po_rows"]]
    if disc_rows:
        po_bill = _qb_conmed_bills()
        for r in disc_rows:
            src = disc_src.get(r["so_id"], {})
            bill = None
            bill_po = None
            for pn in r["pos"]:
                if pn.upper() in po_bill:
                    bill = po_bill[pn.upper()]
                    bill_po = pn.upper()
                    break
            disc_emails[r["so_id"]] = {
                "so_num": r["so_num"], "customer": r["customer"], "pos": r["pos"],
                "po_date": src.get("po_date", ""), "po_items": src.get("po_items", []),
                "po_rows": r["po_rows"], "packages": r["packages"],
                "trackings": src.get("trackings", []),
                "bill_po": bill_po, "bill": bill,
            }
        print(f"  Discrepancy emails: {len(disc_emails)} (with QB bill matched: "
              f"{sum(1 for v in disc_emails.values() if v['bill'])})")

    # Maps so the tab can re-match a newly uploaded UPS Billing CSV in the browser
    # (tracking# / PO -> SO id) and look up each SO's shipping revenue + customer.
    maps = {
        "po2so": {po: _bare(cid) for cid, pos in crmid_pos.items() for po in pos},
        "track2so": {tn: _bare(cid) for tn, cid in track2crmid.items() if cid in conmed_crmids},
        "so_info": {r["so_id"]: {"customer": r["customer"], "so_num": r["so_num"],
                                 "date": r["date"], "revenue": r["revenue"], "pos": r["pos"],
                                 "po_rows": r["po_rows"]} for r in rows},
    }
    gen = now.strftime("%Y-%m-%d %I:%M %p PT")
    return {
        "maps": maps,
        "generated_at": gen,
        "year": Y,
        "has_billing": bool(billing),
        "billing_asof": billing_asof,
        "unmatched_charges": unmatched_n,
        "unmatched_cost": round(unmatched_cost, 2),
        "unattributed_total": round(unatt_total, 2),
        "unattributed": unatt_breakdown,
        "billing_source": billing_src,
        "matched_cost": round(sum(r["cost"] for r in rows), 2),
        "discrepancy_emails": disc_emails,
        "accepted": sorted(accepted),
        "rows": rows,
    }


def main():
    import sys
    sys.path.insert(0, QB)
    from open_orders_report import VtigerAPI, CONFIG
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    cache_path = os.path.join(CONFIG["output_dir"], f"retrieve_cache_{today}.json")
    vt = VtigerAPI(CONFIG["vtiger_rest_base"], CONFIG["vtiger_user"],
                   CONFIG["vtiger_accesskey"], cache_path=cache_path)
    vt.login()
    data = build_shipments_pnl(vt)
    with open(OUT_FILE, "w") as f:
        json.dump(data, f, indent=2)
    tot_rev = sum(r["revenue"] for r in data["rows"])
    tot_cost = sum(r["cost"] for r in data["rows"])
    print(f"Wrote {OUT_FILE}: {len(data['rows'])} SO rows, "
          f"revenue ${tot_rev:,.2f}, cost ${tot_cost:,.2f}, "
          f"billing={'yes' if data['has_billing'] else 'PENDING (no CSV)'}, "
          f"unmatched charges={data['unmatched_charges']}")


if __name__ == "__main__":
    main()
