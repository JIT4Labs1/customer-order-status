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
SHOPIFY_PAY = os.path.join(QB, "_shopify_payments.json")   # paid/part-paid Shopify orders (Shopify MCP)
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


PDF_CACHE = os.path.join(QB, "_qb_invoice_pdf_cache.json")


def _pdf_fields(access, inv, cache):
    """The QB REST API does not return invoice custom fields, but the printed invoice does
    ("Shopify Fulfillment ID: #1094-F2", "Customer PO: ..."). Pull the invoice PDF, extract those two
    fields, cache by invoice Id + SyncToken (re-fetched only when the invoice is edited)."""
    key = str(inv.get("Id"))
    ent = cache.get(key)
    if ent and ent.get("st") == str(inv.get("SyncToken")) and "link" in ent:
        return ent
    import urllib.request, subprocess, tempfile
    def _link():
        try:
            rq = urllib.request.Request(f"{B.BASE}/company/{B.REALM}/invoice/{inv['Id']}?minorversion=65&include=invoiceLink",
                                        headers={"Authorization": "Bearer " + access, "Accept": "application/json"})
            return (json.load(urllib.request.urlopen(rq, timeout=60)).get("Invoice") or {}).get("InvoiceLink", "") or ""
        except Exception as e:
            print("[WARN] invoice link fetch failed for", inv.get("DocNumber"), e)
            return ""
    if ent and ent.get("st") == str(inv.get("SyncToken")):
        ent["link"] = _link()          # PDF fields already cached; only the customer payment link is missing
        cache[key] = ent
        try:
            json.dump(cache, open(PDF_CACHE, "w"))
        except Exception:
            pass
        return ent
    ent = {"st": str(inv.get("SyncToken")), "ful": "", "po": "", "ok": False, "link": _link()}
    try:
        req = urllib.request.Request(f"{B.BASE}/company/{B.REALM}/invoice/{inv['Id']}/pdf?minorversion=65",
                                     headers={"Authorization": "Bearer " + access, "Accept": "application/pdf"})
        data = urllib.request.urlopen(req, timeout=60).read()
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tf:
            tf.write(data); tf.flush()
            txt = subprocess.run(["pdftotext", "-layout", tf.name, "-"], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"Shopify Fulfillment ID:[ \t]*([^\n]*)", txt, re.I)
        ent["ful"] = re.sub(r"\s{2,}.*$", "", m.group(1).strip()) if m else ""
        m = re.search(r"Customer PO:[ \t]*([^\n]*)", txt, re.I)
        ent["po"] = re.sub(r"\s{2,}.*$", "", m.group(1).strip()) if m else ""
        ent["ok"] = True
    except Exception as e:
        print("[WARN] invoice PDF read failed for", inv.get("DocNumber"), e)
        if ent.get("ok") is False and cache.get(key):
            return cache[key]
    cache[key] = ent
    try:
        json.dump(cache, open(PDF_CACHE, "w"))
    except Exception:
        pass
    return ent


def build_ar(access, invoices=None):
    invs = invoices if invoices is not None else open_invoices(access)
    somap = B.vtiger_so_map([i.get("DocNumber", "") for i in invs])
    so2ship = {}
    for order_name, sonum in (_load(os.path.join(QB, "_ship_so_tags.json"), {}) or {}).items():
        if sonum and sonum not in so2ship:
            so2ship[sonum] = order_name
    ful = _load(SHOPIFY_FUL, {})
    sord = _load(SHOPIFY_ORD, {})
    spay = {p["name"]: p for p in _load(SHOPIFY_PAY, [])}
    all_trk = {}   # any tracking number -> Shopify fulfillment (label), across every order
    for _o, _fl in ful.items():
        for f_ in _fl:
            for t_ in (f_.get("trackings") or [f_.get("tracking")]):
                if t_:
                    all_trk.setdefault(t_, f_)
    def _norm(x):
        return re.sub(r"[^a-z0-9]", "", (x or "").lower())
    def _find_by_amount(cname, amt):
        n = _norm(cname)
        for p in spay.values():
            if any(abs(a - amt) < 0.015 for a in ([p.get("received"), p.get("total")] + list(p.get("amounts") or [])) if a is not None):
                for k in (_norm(p.get("company")), _norm(p.get("contact"))):
                    if k and n and (k == n or k in n or n in k):
                        return p["name"]
        return ""
    # Amount QuickBooks already shows as paid per Shopify order (all invoices whose note names the order).
    qb_paid, qb_noted = {}, {}
    try:
        for x in _all(access, "Invoice", "TxnDate >= '2026-06-01'"):
            m_ = re.search(r"Shopify order\s*(#\d+)", x.get("PrivateNote") or "")
            if m_:
                qb_paid[m_.group(1)] = qb_paid.get(m_.group(1), 0.0) + B._fnum(x.get("TotalAmt")) - B._fnum(x.get("Balance"))
                qb_noted[m_.group(1)] = qb_noted.get(m_.group(1), 0.0) + B._fnum(x.get("TotalAmt"))
    except Exception as e:
        print("[WARN] could not total QB-paid per Shopify order:", e)
    # Shopify cash not yet matched by a paid QB invoice, per order (allocated oldest invoice first).
    avail = {n: (p.get("received") or 0.0) - qb_paid.get(n, 0.0) for n, p in spay.items()}
    # tracking number -> Shopify order (from the shipments dataset), used when an invoice has no Shopify note
    trk2order = {}
    for sh_ in (_load(os.path.join(QB, "ups-shipments-data.json"), {}) or {}).get("shipments", []):
        o_ = sh_.get("shopify_order") or sh_.get("order") or ""
        if sh_.get("tracking") and str(o_).startswith("#"):
            trk2order[str(sh_["tracking"]).strip()] = o_
    pdfc = _load(PDF_CACHE, {})
    custs = {}
    for i in sorted(invs, key=lambda x: (x.get("TxnDate") or "", x.get("DocNumber") or "")):
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
        # QuickBooks custom field "Shopify Fulfillment ID" (only visible on the printed invoice) holds the shipment label.
        pf = _pdf_fields(access, i, pdfc) if bal > 0.005 else {}
        pdf_labels = re.findall(r"#\d+-F\d+", pf.get("ful", "") or "")
        pdf_ids = [] if pdf_labels else re.findall(r"\b\d{8,}\b", pf.get("ful", "") or "")
        pdf_order = pdf_labels[0].split("-F")[0] if pdf_labels else ""
        if not pdf_order and pdf_ids:
            for o_, fl_ in ful.items():
                if any(f_.get("id") in pdf_ids for f_ in fl_):
                    pdf_order = o_; break
        order = mo.group(1) if mo else (pdf_order or (so2ship.get(so.get("so_num", ""), "") if so.get("so_num") else ""))
        matched_by = "note" if mo else ("qb_field" if pdf_order else ("vtiger" if order else ""))
        trk_hits = []
        if not order:
            toks = [str(i.get("TrackingNum") or "")] + [l.get("Description") or "" for l in i.get("Line", [])]
            for t_ in toks:
                for w in re.findall(r"[0-9A-Za-z]{10,}", t_):
                    if w in trk2order and w not in trk_hits:
                        trk_hits.append(w)
            if trk_hits:
                order = trk2order[trk_hits[0]]
                matched_by = "tracking"
        if not order:
            order = _find_by_amount(cname, bal) or _find_by_amount(cname, B._fnum(i.get("TotalAmt")))
            if order:
                matched_by = "amount"
        if not order:
            # Same customer + this invoice's total plus the other QB invoices that already name the order
            # add up to the Shopify order total (e.g. a big invoice with no note + a small one that cites the order).
            tot_ = B._fnum(i.get("TotalAmt"))
            for p_ in spay.values():
                if abs(tot_ + qb_noted.get(p_["name"], 0.0) - (p_.get("total") or 0.0)) < 0.02 and tot_ > 0.005:
                    n_ = _norm(cname)
                    if any(k and n_ and (k == n_ or k in n_ or n_ in k) for k in (_norm(p_.get("company")), _norm(p_.get("contact")))):
                        order = p_["name"]; matched_by = "sum"; break
        fl_pdf = [{"id": "", "shipment": l_, "tracking": ""} for l_ in pdf_labels] or [{"id": x_, "shipment": "", "tracking": ""} for x_ in pdf_ids]
        fl = fl_pdf if (fl_pdf and not mf) else [{"id": mf.group(1), "shipment": ms.group(1) if ms else "", "tracking": i.get("TrackingNum") or ""}] if mf else ([{"id": "", "shipment": "", "tracking": t_} for t_ in trk_hits] if trk_hits else (ful.get(order, []) if order else []))
        row = {"number": num, "date": i.get("TxnDate", ""), "due": i.get("DueDate", "") or i.get("TxnDate", ""),
               "amount": round(B._fnum(i.get("TotalAmt")), 2), "balance": round(bal, 2),
               "so_num": so.get("so_num", ""), "shopify_order": order,
               "shopify_fulfillments": fl, "pay_link": pf.get("link", "")}
        # Resolve Shopify shipment labels (#order-Fn) from the Shopify fulfillment map: by fulfillment id, then tracking.
        if order and ful.get(order):
            by_id = {f_["id"]: f_ for f_ in ful[order]}
            by_trk = {}
            for f_ in ful[order]:
                for t_ in (f_.get("trackings") or [f_.get("tracking")]):
                    if t_:
                        by_trk.setdefault(t_, f_)
            for e_ in fl:
                hit = by_id.get(e_.get("id")) or by_trk.get(e_.get("tracking")) or all_trk.get(e_.get("tracking"))
                if hit:
                    e_["shipment"] = hit.get("shipment", "")
                    e_["id"] = e_.get("id") or hit.get("id", "")
            if not fl and not trk_hits:
                fl = [dict(f_) for f_ in ful[order]] if False else fl
        so_ = sord.get(order) if order else None
        sp_ = spay.get(order) if order else None
        pis = "paid in shopify" in note.lower()
        st = (sp_ or so_ or {}).get("status") or ("PAID" if pis else "")
        row["shopify_status"] = st
        if sp_ or so_:
            row["shopify_total"] = (sp_ or so_).get("total")
            row["shopify_received"] = (sp_ or {}).get("received")
            row["shopify_outstanding"] = (so_ or {}).get("outstanding")
        if matched_by:
            row["shopify_match"] = matched_by
        # Fully paid in Shopify, or Shopify received >= this invoice's balance (e.g. order shows PARTIALLY_PAID
        # only because of a small later adjustment) while QuickBooks still shows it open -> reconcile.
        recvd = [a for a in (list((sp_ or {}).get("amounts") or []) + [(sp_ or {}).get("received")]) if a is not None]
        covers = False
        if st == "PARTIALLY_PAID" and sp_ and bal > 0:
            # Only trust the cash allocation when the QB invoices that name this order account for most of it
            # (otherwise earlier shipments were invoiced without a note and already consumed the cash).
            known = qb_noted.get(order, 0.0) >= 0.7 * (sp_.get("total") or 0.0)
            if known and avail.get(order, 0.0) + 0.015 >= bal:     # Shopify cash received still covers this open invoice
                covers = True
                avail[order] -= bal
        row["reconcile"] = (st == "PAID") or covers
        c = custs.setdefault(cname, {"name": cname, "balance": 0.0, "qb_balance": 0.0, "invoices": []})
        c["qb_balance"] += bal
        c["balance"] += 0.0 if row.get("reconcile") else bal   # A/R view: paid-in-Shopify invoices count as 0
        c["invoices"].append(row)
    try:
        json.dump(pdfc, open(PDF_CACHE, "w"))
    except Exception:
        pass
    out = sorted(custs.values(), key=lambda c: -c["balance"])
    for c in out:
        c["balance"] = round(c["balance"], 2)
        c["qb_balance"] = round(c["qb_balance"], 2)
        c["invoices"].sort(key=lambda r: r["due"])
    return {"generated_at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z"),
            "as_of": datetime.date.today().isoformat(),
            "total": round(sum(c["balance"] for c in out), 2),
            "qb_total": round(sum(c["qb_balance"] for c in out), 2),
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
