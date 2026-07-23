#!/usr/bin/env python3
"""Build payment-status-data.json for the dashboard "Payment Status" tab.

Pulls 2026 invoices from QuickBooks Online (direct REST API, reusing the OAuth
credentials/refresh flow from vtiger_qb_sync.py), keeps only the Independent
Diagnostic Lab customers (Vtiger industry == "Independent Diagnostic Lab", whose
QB customer IDs are resolved below), and writes per-customer invoice lists:
  { number, status (Paid/Not Paid/Voided), amount, balance, date, link }

Re-run any time to refresh. Publishes with publish_google_ads_data.py.
"""
import json, os, base64, urllib.parse, urllib.request, urllib.error, datetime
from concurrent.futures import ThreadPoolExecutor

QB = os.path.dirname(os.path.abspath(__file__))
TOKENS = os.path.join(QB, "qb_tokens.json")
OUT = os.path.join(QB, "payment-status-data.json")
LINK_CACHE = os.path.join(QB, "qb_invoice_links.json")  # (legacy) {invoice_id: multi-invoice share link}
PAYLINKS = os.path.join(QB, "qb_payment_links.json")    # {invoice DocNumber: single-amount pay link}

CLIENT_ID = "ABSHyaBUQLLXKGHjY5kxqdscgZYQkmxR3sI5HNbDqfw0BsN5mG"
CLIENT_SECRET = "TEENd2mp2tRqwWKksnUBIpkk2wB3p18Z4o5XSHRz"
REALM = "9341452706936433"
BASE = "https://quickbooks.api.intuit.com/v3"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

# Vtiger — maps each QuickBooks invoice (DocNumber) to its Vtiger Sales Order + fulfillment.
# The Vtiger SO custom field cf_salesorder_invoiceid holds the QB invoice number (DocNumber).
VTIGER_URL = "https://jit4youinc.od2.vtiger.com"
VTIGER_USER = "customersupport@jit4you.com"
VTIGER_ACCESS_KEY = "fIPkOulq0BaA5y2s"


def vtiger_query(q):
    auth = base64.b64encode(f"{VTIGER_USER}:{VTIGER_ACCESS_KEY}".encode()).decode()
    url = VTIGER_URL + "/restapi/v1/vtiger/default/query?query=" + urllib.parse.quote(q)
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + auth)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode()).get("result", []) or []
    except Exception:
        return []


def so_fulfillment(sostatus):
    s = (sostatus or "").strip().lower()
    if "cancel" in s:
        return "Cancelled"
    if "fully delivered" in s or s == "delivered":
        return "Fulfilled"
    return "Partially"   # partially delivered / sent / approved / created -> still has open items


def vtiger_retrieve(eid):
    auth = base64.b64encode(f"{VTIGER_USER}:{VTIGER_ACCESS_KEY}".encode()).decode()
    req = urllib.request.Request(VTIGER_URL + "/restapi/v1/vtiger/default/retrieve?id=" + eid)
    req.add_header("Authorization", "Basic " + auth)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode()).get("result", {}) or {}
    except Exception:
        return {}


def vtiger_so_map(docnumbers):
    """DocNumber -> {so_num, so_id, sostatus, fulfillment}. The ONLY valid link between a QB invoice
    and a Vtiger Sales Order is the SO custom field cf_salesorder_invoiceid — which may hold MULTIPLE
    comma-separated invoice numbers (e.g. "SO509, SO490") when several invoices bill one SO. So we
    fetch every SO whose Invoice ID field is populated and tokenize it on commas, matching each token
    to a QB DocNumber. We NEVER match by salesorder_no. Empty field -> no match. Fulfillment computed
    later from line items."""
    docs = set(d for d in docnumbers if d)
    rows, seen = [], set()
    for pat in ("%SO%", "%INV%"):   # invoice numbers look like SO### / INV###
        start = 0
        while True:
            b = vtiger_query("SELECT id, salesorder_no, cf_salesorder_invoiceid, sostatus, createdtime "
                             f"FROM SalesOrder WHERE cf_salesorder_invoiceid LIKE '{pat}' LIMIT {start},100;")
            if not isinstance(b, list):
                b = []
            for r in b:
                if r.get("id") not in seen:
                    seen.add(r.get("id")); rows.append(r)
            if len(b) < 100:
                break
            start += 100
    out = {}
    for r in rows:
        for tok in (r.get("cf_salesorder_invoiceid") or "").split(","):
            tok = tok.strip()
            if tok in docs and tok not in out:
                out[tok] = {"so_num": r.get("salesorder_no", ""), "so_id": r.get("id", ""),
                            "so_date": (r.get("createdtime", "") or "")[:10],
                            "sostatus": r.get("sostatus", ""), "fulfillment": "", "open_items": []}
    return out


SKIP_LINE = {"restricted", "shipping", "discount", "freight", "handling", ""}


def _fnum(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0


def enrich_open_items(somap):
    """For every mapped SO, compute fulfillment from actual line items (delivered vs ordered,
    skipping non-product lines) — sostatus is unreliable. For SOs with genuinely open product
    lines, attach open_items:[{product,open_qty,po,vendor}] resolved via linked POs."""
    mapped = [v for v in somap.values() if v.get("so_id")]
    if not mapped:
        return
    # Pass 1: retrieve each SO (parallel), compute real open lines (ignore fully-delivered + junk).
    with ThreadPoolExecutor(max_workers=10) as ex:
        li_map = dict(zip([v["so_id"] for v in mapped],
                          ex.map(lambda sid: vtiger_retrieve(sid).get("LineItems", []) or [],
                                 [v["so_id"] for v in mapped])))
    for v in mapped:
        opens = []
        for it in li_map.get(v["so_id"], []):
            pid = (it.get("productid") or "").strip()
            name = (it.get("product_name") or "").strip()
            if not pid or name.lower() in SKIP_LINE:
                continue
            oq = _fnum(it.get("quantity")) - _fnum(it.get("delivered_qty"))
            if oq > 0.001:
                opens.append({"product": name, "open_qty": round(oq, 2), "pid": pid, "po": "", "vendor": ""})
        if "cancel" in (v.get("sostatus") or "").lower():
            v["fulfillment"], v["open_items"] = "Cancelled", []
        elif opens:
            v["fulfillment"], v["open_items"] = "Partially", opens
        else:
            v["fulfillment"], v["open_items"] = "Fulfilled", []
    # Pass 2: only for SOs with open lines, resolve PO # + vendor by product.
    part = [v for v in mapped if v.get("open_items")]
    if not part:
        return
    inlist = "','".join(v["so_id"] for v in part)
    po_by_so, vendor_ids = {}, set()
    for po in vtiger_query("SELECT purchaseorder_no, vendor_id, salesorder_id, id FROM "
                           f"PurchaseOrder WHERE salesorder_id IN ('{inlist}') AND postatus != 'Cancelled';"):
        po_by_so.setdefault(po.get("salesorder_id", ""), []).append(
            (po.get("purchaseorder_no", ""), po.get("vendor_id", ""), po.get("id", "")))
        if po.get("vendor_id"):
            vendor_ids.add(po["vendor_id"])
    vname = {}
    if vendor_ids:
        for v in vtiger_query("SELECT id, vendorname FROM Vendors WHERE id IN ('%s');" % "','".join(vendor_ids)):
            vname[v.get("id", "")] = v.get("vendorname", "")
    # Per-PO line items only needed for SOs with MULTIPLE POs (to match each item to its PO).
    multi_po_ids = [po_id for v in part if len(po_by_so.get(v["so_id"], [])) > 1
                    for (_n, _vi, po_id) in po_by_so.get(v["so_id"], []) if po_id]
    po_li = {}
    if multi_po_ids:
        with ThreadPoolExecutor(max_workers=6) as ex:
            po_li = dict(zip(multi_po_ids,
                             ex.map(lambda pid: vtiger_retrieve(pid).get("LineItems", []) or [], multi_po_ids)))
    for v in part:
        pos = po_by_so.get(v["so_id"], [])
        v["pos"] = [{"po": pn, "vendor": vname.get(vi, "")} for (pn, vi, _pid) in pos]
        if len(pos) == 1:
            pn, vi, _pid = pos[0]
            for oi in v["open_items"]:
                oi.pop("pid", None); oi["po"], oi["vendor"] = pn, vname.get(vi, "")
        elif len(pos) > 1:
            prod_po = {}
            for pn, vi, po_id in pos:
                for it in po_li.get(po_id, []):
                    pid = (it.get("productid") or "").strip()
                    if pid and pid not in prod_po:
                        prod_po[pid] = (pn, vname.get(vi, ""))
            for oi in v["open_items"]:
                oi["po"], oi["vendor"] = prod_po.get(oi.pop("pid", ""), ("", ""))
        else:
            for oi in v["open_items"]:
                oi.pop("pid", None); oi["po"], oi["vendor"] = "", ""

# Independent Diagnostic Lab customers -> QB customer id (resolved via QB customer search)
IDL = {
    "1288": "Mercedes Scientific", "1269": "LabX Diagnostic Systems",
    "453": "Doctors General Laboratory", "1430": "Radiance Diagnostics",
    "1408": "Chicago Lab", "439": "Stark Family Health Center",
    "1386": "3 Alpha Labs, LLC", "1418": "OM Diagnostic Laboratories",
    "1338": "Lucid Labs", "823": "Allora Biotech", "1486": "MEDICAL HEALTH LAB",
    "1278": "My clinical lab INC", "628": "ALDX Holding", "1316": "Clearchem",
    "796": "TRYCOM INC.", "1369": "Epic Laboratory Services Inc",
    "1460": "Clinical Laboratory, Inc.", "1404": "ESIC Corp",
    "1378": "Inpatient Research Clinic", "1440": "Molecular Depot LLC",
    "1493": "Well Health Labs", "1483": "L&M LAB CORP",
}


def refresh_token():
    toks = json.load(open(TOKENS))
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    body = ("grant_type=refresh_token&refresh_token="
            + urllib.parse.quote(toks["refresh_token"])).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Authorization", "Basic " + auth)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
    toks["access_token"] = resp["access_token"]
    toks["refresh_token"] = resp.get("refresh_token", toks["refresh_token"])
    toks["updated_at"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(toks, open(TOKENS, "w"), indent=2)
    return toks["access_token"]


def qb_query(access, q):
    url = f"{BASE}/company/{REALM}/query?query={urllib.parse.quote(q)}&minorversion=65"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + access)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def owner_link(inv_id):
    # Fallback only — opens the invoice inside QuickBooks (requires QBO login).
    return (f"https://qbo.intuit.com/app/login?pagereq=invoice%3FtxnId%3D{inv_id}"
            f"&deeplinkcompanyid={REALM}")


def fetch_share_link(access, inv_id):
    """Customer-facing shareable view + pay link (no QBO login) via include=invoiceLink."""
    url = f"{BASE}/company/{REALM}/invoice/{inv_id}?include=invoiceLink&minorversion=65"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + access)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            inv = (json.loads(r.read().decode()) or {}).get("Invoice", {}) or {}
        return inv.get("InvoiceLink") or ""
    except Exception:
        return ""


BANK_OVERRIDE = os.path.join(QB, "bank_balance.json")


def build_bank(access):
    """Bank balance card for Chase CHK (9219).

    QuickBooks' API only exposes the *In-QuickBooks* book balance (Account.CurrentBalance);
    it does NOT expose the *Bank balance* from the Chase feed shown on the QBO home screen.
    So the card headline uses a manual bank-feed value from bank_balance.json (updated by the
    user), and we also carry the live QB book balance for reference. If the override file is
    missing, we fall back to the QB book balance."""
    name, book = "Chase CHK (9219)", None
    try:
        r = qb_query(access, "SELECT * FROM Account WHERE AccountType = 'Bank'")
        for a in r.get("QueryResponse", {}).get("Account", []) or []:
            if "9219" in (a.get("Name") or ""):
                name = a.get("Name", name)
                book = _fnum(a.get("CurrentBalance"))
                break
    except Exception:
        pass
    feed, feed_asof = None, None
    try:
        with open(BANK_OVERRIDE) as f:
            ov = json.load(f)
        feed = _fnum(ov.get("balance"))
        feed_asof = ov.get("as_of")
    except Exception:
        pass
    headline = feed if feed is not None else book
    if headline is None:
        return None
    return {
        "name": name,
        "balance": round(headline, 2),
        "source": "Bank balance" if feed is not None else "In QuickBooks",
        "book_balance": (round(book, 2) if book is not None else None),
        "bank_asof": feed_asof,
        "as_of": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z"),
    }


def build_payables(access):
    """Accounts Payable: open vendor bills from QuickBooks (Balance > 0), aggregated
    per vendor. Returns {generated_at, grand_total, past_due, count, vendors:[
      {vendor, balance, count, pastdue, bills:[{num,date,due,amount,balance}]}]}."""
    bills, start = [], 1
    while True:
        try:
            r = qb_query(access, "SELECT * FROM Bill WHERE Balance > '0' "
                                 f"ORDERBY TxnDate DESC STARTPOSITION {start} MAXRESULTS 100")
        except Exception:
            break
        chunk = r.get("QueryResponse", {}).get("Bill", []) or []
        bills += chunk
        if len(chunk) < 100:
            break
        start += 100
    today = datetime.date.today()
    agg = {}
    for b in bills:
        v = (b.get("VendorRef") or {}).get("name", "(unknown)")
        bal = _fnum(b.get("Balance"))
        due = b.get("DueDate") or ""
        pd = 0.0
        if due:
            try:
                if datetime.date.fromisoformat(due) < today:
                    pd = bal
            except Exception:
                pass
        e = agg.setdefault(v, {"vendor": v, "balance": 0.0, "count": 0, "pastdue": 0.0, "bills": []})
        e["balance"] += bal
        e["count"] += 1
        e["pastdue"] += pd
        e["bills"].append({"num": b.get("DocNumber", ""), "date": (b.get("TxnDate") or "")[:10],
                           "due": due, "amount": _fnum(b.get("TotalAmt")), "balance": bal})
    vendors = sorted(agg.values(), key=lambda x: -x["balance"])
    for v in vendors:
        v["balance"] = round(v["balance"], 2)
        v["pastdue"] = round(v["pastdue"], 2)
        v["bills"].sort(key=lambda x: x["date"], reverse=True)
        for bl in v["bills"]:
            bl["amount"] = round(bl["amount"], 2)
            bl["balance"] = round(bl["balance"], 2)
    return {
        "generated_at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z"),
        "grand_total": round(sum(v["balance"] for v in vendors), 2),
        "past_due": round(sum(v["pastdue"] for v in vendors), 2),
        "count": sum(v["count"] for v in vendors),
        "vendors": vendors,
    }


def main():
    access = refresh_token()
    invoices = []
    start = 1
    while True:
        q = (f"SELECT * FROM Invoice WHERE TxnDate >= '2026-01-01' AND TxnDate <= "
             f"'2026-12-31' ORDERBY TxnDate DESC STARTPOSITION {start} MAXRESULTS 1000")
        resp = qb_query(access, q)
        batch = (resp.get("QueryResponse", {}) or {}).get("Invoice", []) or []
        invoices.extend(batch)
        if len(batch) < 1000:
            break
        start += 1000

    # Keep only IDL-customer invoices.
    idl_invs = [inv for inv in invoices if (inv.get("CustomerRef") or {}).get("value") in IDL]

    # Single-amount, per-invoice customer pay links (created once via QB payment links,
    # keyed by invoice DocNumber in qb_payment_links.json). Used ONLY for unpaid invoices —
    # each opens a QuickBooks page showing just that invoice's amount (no combined total).
    paylinks = {}
    if os.path.exists(PAYLINKS):
        try:
            paylinks = json.load(open(PAYLINKS))
        except Exception:
            paylinks = {}

    # Customer-facing INVOICE-VIEW links (show line items + Pay button, no QBO login).
    # Keyed by invoice Id in qb_invoice_links.json; fetched on demand for any not cached.
    invlinks = {}
    if os.path.exists(LINK_CACHE):
        try:
            invlinks = json.load(open(LINK_CACHE))
        except Exception:
            invlinks = {}

    # First pass: build rows + list unpaid invoices still missing an invoice-view link.
    staged = []  # (cref, row, iid, status)
    need = []
    for inv in idl_invs:
        cref = (inv.get("CustomerRef") or {}).get("value")
        iid = inv.get("Id", "")
        num = inv.get("DocNumber", "") or ("INV-" + iid)
        total = float(inv.get("TotalAmt", 0) or 0)
        bal = float(inv.get("Balance", 0) or 0)
        voided = (total == 0 and "void" in (inv.get("PrivateNote", "") or "").lower())
        status = "Voided" if voided else ("Paid" if bal <= 0.005 else "Not Paid")
        # Only unpaid invoices need a customer link (paid/voided have nothing to collect).
        link = paylinks.get(num, "") if status == "Not Paid" else ""
        row = {"number": num, "status": status, "amount": round(total, 2),
               "balance": round(bal, 2), "date": inv.get("TxnDate", ""), "link": link}
        staged.append((cref, row, iid, status))
        if status == "Not Paid" and iid and not invlinks.get(iid):
            need.append(iid)

    # Fetch any missing invoice-view links in parallel, then refresh the cache.
    if need:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for iid, il in zip(need, ex.map(lambda x: fetch_share_link(access, x), need)):
                if il:
                    invlinks[iid] = il
        try:
            json.dump(invlinks, open(LINK_CACHE, "w"), indent=1)
        except Exception:
            pass

    # A QuickBooks invoice-view link opens a customer PORTAL that lists ALL of that customer's
    # unpaid invoices (each viewable). QB only returns a direct link for some invoices, so use
    # any one link per customer as the shared "portal" link for that customer's other invoices —
    # every invoice then has a working view icon that reaches it inside the portal.
    portal = {}
    for cref, row, iid, status in staged:
        il = invlinks.get(iid, "")
        if il and cref not in portal:
            portal[cref] = il

    # Map each invoice DocNumber -> Vtiger Sales Order (number + fulfillment status),
    # then enrich Partially-fulfilled SOs with their open line items + PO/vendor (from Vtiger).
    somap = vtiger_so_map([row["number"] for cref, row, iid, status in staged])
    enrich_open_items(somap)

    by_cust = {cid: [] for cid in IDL}
    for cref, row, iid, status in staged:
        # invoice_link = customer-facing invoice portal (line items + Pay); own link, else the
        # customer's portal link (still shows this invoice). Only unpaid invoices need it.
        if status == "Not Paid":
            row["invoice_link"] = invlinks.get(iid, "") or portal.get(cref, "")
        else:
            row["invoice_link"] = ""
        # Vtiger SO match (field is new -> often empty). so_num "" when unlinked.
        so = somap.get(row["number"], {})
        row["so_num"] = so.get("so_num", "")
        row["fulfillment"] = so.get("fulfillment", "")
        row["so_date"] = so.get("so_date", "")
        row["open_items"] = so.get("open_items", []) if so.get("fulfillment") == "Partially" else []
        by_cust[cref].append(row)

    customers = []
    for cid, name in sorted(IDL.items(), key=lambda kv: kv[1].lower()):
        invs = sorted(by_cust[cid], key=lambda x: x["date"], reverse=True)
        amt = round(sum(i["amount"] for i in invs), 2)
        unpaid = round(sum(i["balance"] for i in invs), 2)
        customers.append({
            "name": name, "qb_id": cid, "invoices": invs,
            "totals": {"count": len(invs), "amount": amt,
                       "paid": round(amt - unpaid, 2), "unpaid": unpaid},
        })

    payables = build_payables(access)
    bank = build_bank(access)

    out = {
        "generated_at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z"),
        "year": 2026,
        "customers": customers,
        "payables": payables,
        "bank": bank,
    }
    json.dump(out, open(OUT, "w"), indent=2)
    tot_inv = sum(c["totals"]["count"] for c in customers)
    print(f"payment-status-data.json: {len(customers)} IDL customers, {tot_inv} invoices "
          f"(from {len(invoices)} total 2026 invoices)")
    for c in customers:
        if c["totals"]["count"]:
            print(f"  {c['name']}: {c['totals']['count']} inv, "
                  f"${c['totals']['amount']:.2f} (${c['totals']['unpaid']:.2f} unpaid)")
    if bank:
        print(f"Bank: {bank['name']} balance ${bank['balance']:.2f}")
    print(f"Accounts Payable: {payables['count']} open bills, ${payables['grand_total']:.2f} owed "
          f"(${payables['past_due']:.2f} past due) across {len(payables['vendors'])} vendor(s)")
    for v in payables["vendors"]:
        print(f"  {v['vendor']}: ${v['balance']:.2f} ({v['count']} bills, ${v['pastdue']:.2f} past due)")


if __name__ == "__main__":
    main()
