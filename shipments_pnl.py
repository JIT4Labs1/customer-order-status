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
    """Return (lines, asof). lines = [{tracking, refs:[...], net}]."""
    d = _load_json(BILLING_FILE, None)
    if not d:
        return [], ""
    if isinstance(d, dict):
        return d.get("charges", d.get("lines", [])) or [], d.get("generated_at", d.get("asof", ""))
    return d, ""


def build_shipments_pnl(vt):
    now = _pac_now()
    Y = now.year

    # 1) UPS shipments -> per-SO metadata (SO id, POs, tracking numbers).
    ships = (_load_json(SHIPS_FILE, {}) or {}).get("shipments", [])
    ups = [s for s in ships if (s.get("carrier") == "UPS")]
    so_meta = {}     # so_num -> {so_id, pos:set, trackings:set, receiver}
    track2so = {}    # tracking -> so_num
    po2so = {}       # PO# -> so_num
    for s in ups:
        so = (s.get("so_num") or "").strip()
        if not so:
            continue
        m = so_meta.setdefault(so, {"so_id": s.get("so_id"), "pos": set(),
                                    "trackings": set(), "receiver": s.get("receiver", "")})
        if not m["so_id"] and s.get("so_id"):
            m["so_id"] = s.get("so_id")
        tn = (s.get("tracking") or "").strip()
        if tn:
            m["trackings"].add(tn)
            track2so.setdefault(tn, so)
        pos = s.get("pos") or ([{"po": s.get("po")}] if s.get("po") else [])
        for p in pos:
            po = (p.get("po") or "").strip()
            if po:
                m["pos"].add(po)
                po2so.setdefault(po, so)

    # 2) UPS billing charges -> cost per SO (match by tracking, then by PO in refs).
    billing, billing_asof = _load_billing()
    so_cost = {}
    matched_sos = set()
    unmatched_n = 0
    unmatched_cost = 0.0
    for bl in billing:
        net = _f(bl.get("net", bl.get("net_charge", bl.get("amount", 0))))
        tn = (bl.get("tracking") or "").strip()
        refs = bl.get("refs") or [bl.get("ref1", ""), bl.get("ref2", "")]
        so = track2so.get(tn)
        if not so:
            for r in refs:
                r = (r or "").strip()
                if r and r in po2so:
                    so = po2so[r]
                    break
        if so:
            so_cost[so] = so_cost.get(so, 0.0) + net
            matched_sos.add(so)
        else:
            unmatched_n += 1
            unmatched_cost += net

    # 3) Vtiger: account names + SKU-999 shipping revenue per SO.
    accts = {a["id"]: (a.get("accountname", "") or "")
             for a in vt.query_all("SELECT id, accountname FROM Accounts")}
    # productid -> productcode, to identify the shipping line
    pcode = {}
    for p in vt.query_all("SELECT id, productcode FROM Products"):
        pcode[p["id"]] = (p.get("productcode") or "").strip()

    def _crmid(soid):
        # Vtiger retrieve needs the module-prefixed crmid (SalesOrder module = 15),
        # e.g. "156771" -> "15x156771". Shipments store the bare numeric id.
        soid = str(soid or "")
        return soid if ("x" in soid or not soid) else ("15x" + soid)

    rows = []
    for so_num, m in so_meta.items():
        detail = vt.retrieve_with_retry(_crmid(m["so_id"]), label="SO-PNL") if m.get("so_id") else None
        revenue = 0.0
        acct = ""
        date = ""
        status = ""
        if detail:
            acct = accts.get(detail.get("account_id", ""), "")
            date = str(detail.get("createdtime", ""))[:10]
            status = (detail.get("sostatus", "") or "").strip()
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
        if (status or "").lower() == "cancelled":
            continue
        customer = acct or m.get("receiver") or "(no customer)"
        cost = round(so_cost.get(so_num, 0.0), 2)
        rows.append({
            "customer": customer,
            "so_num": so_num,
            "so_id": m.get("so_id") or "",
            "date": date,
            "pos": sorted(m["pos"]),
            "packages": len(m["trackings"]),
            "revenue": round(revenue, 2),
            "cost": cost,
            "has_cost": so_num in matched_sos,
        })

    rows.sort(key=lambda r: (r["date"] or ""), reverse=True)
    gen = now.strftime("%Y-%m-%d %I:%M %p PT")
    return {
        "generated_at": gen,
        "year": Y,
        "has_billing": bool(billing),
        "billing_asof": billing_asof,
        "unmatched_charges": unmatched_n,
        "unmatched_cost": round(unmatched_cost, 2),
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
