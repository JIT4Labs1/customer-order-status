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
import os, json, datetime

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:
    _PT = None

QB = os.path.dirname(os.path.abspath(__file__))
SHIPS_FILE = os.path.join(QB, "ups-shipments-data.json")
BILLING_FILE = os.path.join(QB, "ups-billing-data.json")
OUT_FILE = os.path.join(QB, "shipments-pnl-data.json")

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

    def _po_rows(po_numbers):
        """Total number of line items across the given Conmed PO(s)."""
        total = 0
        for pn in po_numbers:
            pc = po_crmid.get(pn)
            if not pc:
                continue
            det = vt.retrieve_with_retry(pc, label="PO-PNL")
            if det:
                total += len(det.get("LineItems", det.get("lineItems", [])) or [])
        return total
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
        n_pkgs = len(so_billed_trk.get(cid) or ship_trk.get(cid) or set())
        po_rows = _po_rows(pos)
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
