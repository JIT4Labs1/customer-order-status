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

    by_cust = {cid: [] for cid in IDL}
    for inv in idl_invs:
        cref = (inv.get("CustomerRef") or {}).get("value")
        iid = inv.get("Id", "")
        num = inv.get("DocNumber", "") or ("INV-" + iid)
        total = float(inv.get("TotalAmt", 0) or 0)
        bal = float(inv.get("Balance", 0) or 0)
        voided = (total == 0 and "void" in (inv.get("PrivateNote", "") or "").lower())
        status = "Voided" if voided else ("Paid" if bal <= 0.005 else "Not Paid")
        # Only unpaid invoices get a pay link (paid/voided have nothing to collect).
        link = paylinks.get(num, "") if status == "Not Paid" else ""
        by_cust[cref].append({
            "number": num,
            "status": status,
            "amount": round(total, 2),
            "balance": round(bal, 2),
            "date": inv.get("TxnDate", ""),
            "link": link,
        })

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

    out = {
        "generated_at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z"),
        "year": 2026,
        "customers": customers,
    }
    json.dump(out, open(OUT, "w"), indent=2)
    tot_inv = sum(c["totals"]["count"] for c in customers)
    print(f"payment-status-data.json: {len(customers)} IDL customers, {tot_inv} invoices "
          f"(from {len(invoices)} total 2026 invoices)")
    for c in customers:
        if c["totals"]["count"]:
            print(f"  {c['name']}: {c['totals']['count']} inv, "
                  f"${c['totals']['amount']:.2f} (${c['totals']['unpaid']:.2f} unpaid)")


if __name__ == "__main__":
    main()
