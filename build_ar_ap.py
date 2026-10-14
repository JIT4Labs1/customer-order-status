#!/usr/bin/env python3
"""A/R + A/P datasets for the dashboard's "A/R & A/P" tab (replaces the old Payment Status view).

Called from build_payment_status.main(); output is stored under payment-status-data.json keys
"ar" and "ap".  QuickBooks = source of truth for balances, Vtiger = invoice->SO / PO->SO links,
Shopify = order + fulfillment IDs (order via the 'Vtiger SO: SOxxx' tag map _ship_so_tags.json;
fulfillment IDs via _shopify_fulfillments.json {order_name: [{id, tracking, status}]} which the
scheduled refresh fills from the Shopify MCP).
"""
import json, os, re, datetime
import build_payment_status as B

QB = B.QB
SHOPIFY_FUL = os.path.join(QB, "_shopify_fulfillments.json")
SHOPIFY_ORD = os.path.join(QB, "_shopify_orders.json")   # {order: {status,total,outstanding}} from Shopify MCP


def _load(p, d):
    try:
        return json.load(open(p))
    except Exception:
        return d


def _all(access, entity, where, page=500):
    out, start = [], 1
    while True:
        r = B.qb_query(access, f"SELECT * FROM {entity} WHERE {where} ORDERBY TxnDate DESC "
                               f"STARTPOSITION {start} MAXRESULTS {page}")
        c = (r.get("QueryResponse", {}) or {}).get(entity, []) or []
        out += c
        if len(c) < page:
            break
        start += page
    return out


def open_invoices(access):
    return _all(access, "Invoice", "Balance > '0'")


def build_ar(access, invoices=None):
    invs = invoices if invoices is not None else open_invoices(access)
    somap = B.vtiger_so_map([i.get("DocNumber", "") for i in invs])
    so2ship = {}
    for order_name, sonum in (_load(os.path.join(QB, "_ship_so_tags.json"), {}) or {}).items():
        if sonum and sonum not in so2ship:
            so2ship[sonum] = order_name
    ful = _load(SHOPIFY_FUL, {})
    sord = _load(SHOPIFY_ORD, {})
    custs = {}
    for i in invs:
        bal = B._fnum(i.get("Balance"))
        if bal <= 0.005:
            continue
        num = i.get("DocNumber", "") or ("INV-" + i.get("Id", ""))
        cname = (i.get("CustomerRef") or {}).get("name", "(unknown)")
        so = somap.get(num, {})
        # Primary source: the invoice's internal note, e.g. "Shopify order #1234, shipment #1234-F1
        # (order id 7227..., fulfilment id 6547...)". Fallback: Vtiger SO -> Shopify order tag map.
        note = i.get("PrivateNote") or ""
        mo = re.search(r"Shopify order\s*(#\d+)", note)
        mf = re.search(r"fulfil+ment id\s*(\d+)", note, re.I)
        ms = re.search(r"shipment\s*(#[\w-]+)", note)
        order = mo.group(1) if mo else (so2ship.get(so.get("so_num", ""), "") if so.get("so_num") else "")
        fl = [{"id": mf.group(1), "status": ms.group(1) if ms else "", "tracking": i.get("TrackingNum") or ""}] if mf else (ful.get(order, []) if order else [])
        row = {"number": num, "date": i.get("TxnDate", ""), "due": i.get("DueDate", "") or i.get("TxnDate", ""),
               "amount": round(B._fnum(i.get("TotalAmt")), 2), "balance": round(bal, 2),
               "so_num": so.get("so_num", ""), "shopify_order": order,
               "shopify_fulfillments": fl}
        so_ = sord.get(order) if order else None
        pis = "paid in shopify" in note.lower()
        if so_:
            row["shopify_status"] = so_.get("status", "")
            row["shopify_total"] = so_.get("total")
            row["shopify_outstanding"] = so_.get("outstanding")
        elif pis:
            row["shopify_status"] = "PAID"
        # Shopify says the order is paid but QuickBooks still shows the invoice open -> needs reconcile.
        row["reconcile"] = (row.get("shopify_status") == "PAID")
        c = custs.setdefault(cname, {"name": cname, "balance": 0.0, "invoices": []})
        c["balance"] += bal
        c["invoices"].append(row)
    out = sorted(custs.values(), key=lambda c: -c["balance"])
    for c in out:
        c["balance"] = round(c["balance"], 2)
        c["invoices"].sort(key=lambda r: r["due"])
    return {"generated_at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z"),
            "as_of": datetime.date.today().isoformat(),
            "total": round(sum(c["balance"] for c in out), 2),
            "count": sum(len(c["invoices"]) for c in out), "customers": out}


PO_RE = re.compile(r"\bPO\s*-?\s*(\d{2,5})\b", re.I)


def build_ap(access, bills=None):
    bills = bills if bills is not None else _all(access, "Bill", "Balance > '0'", 100)
    # Collect PO numbers (bill line descriptions, memo, doc number) and QB line customers.
    prepped = []
    all_pos = set()
    for b in bills:
        pos, custs = [], []
        texts = [b.get("PrivateNote", "") or ""] + [l.get("Description", "") or "" for l in b.get("Line", [])]
        for t in texts:
            for m in PO_RE.findall(t):
                p = "PO" + m
                if p not in pos:
                    pos.append(p)
        for l in b.get("Line", []):
            d = l.get("AccountBasedExpenseLineDetail") or {}
            cn = (d.get("CustomerRef") or {}).get("name", "")
            if cn and cn not in custs:
                custs.append(cn)
        prepped.append((b, pos, custs))
        all_pos.update(pos)
    # Vtiger: PO -> SO -> customer (fallback when the bill line has no customer)
    po_info = {}
    pl = sorted(all_pos)
    for k in range(0, len(pl), 40):
        inl = "','".join(pl[k:k + 40])
        rows = B.vtiger_query("SELECT id, purchaseorder_no, salesorder_id, postatus FROM PurchaseOrder "
                              f"WHERE purchaseorder_no IN ('{inl}') LIMIT 0,200;")
        for r in rows:
            po_info[r.get("purchaseorder_no", "")] = r
    so_ids = sorted(set(r.get("salesorder_id", "") for r in po_info.values() if r.get("salesorder_id")))
    so_info = {}
    for k in range(0, len(so_ids), 40):
        inl = "','".join(so_ids[k:k + 40])
        for r in B.vtiger_query("SELECT id, salesorder_no, account_id FROM SalesOrder "
                                f"WHERE id IN ('{inl}') LIMIT 0,200;"):
            so_info[r.get("id", "")] = r
    acc_ids = sorted(set(r.get("account_id", "") for r in so_info.values() if r.get("account_id")))
    acc_name = {}
    for k in range(0, len(acc_ids), 40):
        inl = "','".join(acc_ids[k:k + 40])
        for r in B.vtiger_query(f"SELECT id, accountname FROM Accounts WHERE id IN ('{inl}') LIMIT 0,200;"):
            acc_name[r.get("id", "")] = r.get("accountname", "")
    today = datetime.date.today()
    vend = {}
    for b, pos, custs in prepped:
        bal = B._fnum(b.get("Balance"))
        vname = (b.get("VendorRef") or {}).get("name", "(unknown)")
        due = b.get("DueDate", "") or ""
        pd = 0
        try:
            pd = (today - datetime.date.fromisoformat(due)).days if due else 0
        except Exception:
            pass
        po_rows = []
        for p in pos:
            pi = po_info.get(p, {})
            so = so_info.get(pi.get("salesorder_id", ""), {})
            po_rows.append({"po": p, "so": so.get("salesorder_no", ""),
                            "vtiger_customer": acc_name.get(so.get("account_id", ""), "")})
        cust = custs[:] if custs else [r["vtiger_customer"] for r in po_rows if r["vtiger_customer"]]
        seen, cl = set(), []
        for c in cust:
            if c and c not in seen:
                seen.add(c); cl.append(c)
        row = {"number": b.get("DocNumber", "") or ("BILL-" + b.get("Id", "")), "date": b.get("TxnDate", ""),
               "due": due, "amount": round(B._fnum(b.get("TotalAmt")), 2), "balance": round(bal, 2),
               "days_past_due": pd, "pos": po_rows, "customers": cl}
        v = vend.setdefault(vname, {"vendor": vname, "balance": 0.0, "pastdue": 0.0, "bills": []})
        v["balance"] += bal
        if pd > 0:
            v["pastdue"] += bal
        v["bills"].append(row)
    vs = sorted(vend.values(), key=lambda v: -v["balance"])
    for v in vs:
        v["balance"] = round(v["balance"], 2)
        v["pastdue"] = round(v["pastdue"], 2)
        v["bills"].sort(key=lambda r: r["due"])
    return {"generated_at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z"),
            "total": round(sum(v["balance"] for v in vs), 2),
            "past_due": round(sum(v["pastdue"] for v in vs), 2),
            "count": sum(len(v["bills"]) for v in vs), "vendors": vs}


if __name__ == "__main__":
    a = B.refresh_token()
    ar = build_ar(a)
    ap = build_ap(a)
    need = sorted({r["shopify_order"] for c in ar["customers"] for r in c["invoices"] if r["shopify_order"]})
    print("AR", ar["count"], ar["total"], "| orders needing fulfillments:", need)
    print("AP", ap["count"], ap["total"])
    for v in ap["vendors"]:
        print(v["vendor"], v["balance"], [(b["number"], [p["po"] for p in b["pos"]], b["customers"]) for b in v["bills"][:3]])
