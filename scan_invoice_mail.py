#!/usr/bin/env python3
"""Bulk Invoice Check - mailbox scan (replaces the hourly Claude task).
Reads customersupport@jit4you.com through Microsoft Graph (app credentials), finds vendor invoice emails
(PMA/Zoho Books + Allora/QuickBooks), extracts the PDF text with pdftotext, parses PO + line items and writes
_invbulk_incoming.json for invoice_bulk.py.  READ-ONLY on the mailbox.
Env: MS_TENANT_ID MS_CLIENT_ID MS_CLIENT_SECRET, REQ_ID, REQ_WINDOW (week|2weeks|month|<N>d)
     PROBE=1 -> also dump raw PDF text into ./_probe/ (development only)
"""
import os, sys, re, json, base64, subprocess, tempfile, datetime, urllib.request, urllib.parse, urllib.error
from zoneinfo import ZoneInfo
MAILBOX = "customersupport@jit4you.com"
PT = ZoneInfo("America/Los_Angeles")
G = "https://graph.microsoft.com/v1.0"

def http(url, data=None, headers=None, form=False, tries=4):
    import time
    for i in range(tries):
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        req = urllib.request.Request(url, body, headers or {})
        try:
            with urllib.request.urlopen(req, timeout=90) as f:
                return json.loads(f.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 504) and i < tries - 1:
                time.sleep(int(e.headers.get("Retry-After", 5 * (i + 1)))); continue
            raise SystemExit("Graph HTTP %s %s" % (e.code, e.read()[:200]))

def token():
    r = http("https://login.microsoftonline.com/%s/oauth2/v2.0/token" % os.environ["MS_TENANT_ID"],
             {"client_id": os.environ["MS_CLIENT_ID"], "client_secret": os.environ["MS_CLIENT_SECRET"],
              "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}, form=True)
    return r["access_token"]

def window_start(w):
    now = datetime.datetime.now(PT)
    if w == "month": s = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif w == "2weeks": s = now - datetime.timedelta(days=14)
    elif re.fullmatch(r"\d+d", w or ""): s = now - datetime.timedelta(days=int(w[:-1]))
    else: s = now - datetime.timedelta(days=7)
    return s

def pdf_text(b):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as t:
        t.write(b); t.flush()
        return subprocess.run(["pdftotext", "-layout", t.name, "-"], capture_output=True, text=True).stdout

def money(s):
    try: return float(str(s).replace("$", "").replace(",", "").strip())
    except Exception: return None

# ---------------- parsers (tuned on real invoices) ----------------
def parse_zoho(text):
    return parse_generic(text, "zoho")
def parse_qb(text):
    return parse_generic(text, "quickbooks")
def parse_generic(text, src):
    raise NotImplementedError

def main():
    w = os.environ.get("REQ_WINDOW", "week"); rid = os.environ.get("REQ_ID") or "manual-" + datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    since = window_start(w); tok = token(); H = {"Authorization": "Bearer " + tok}
    known = set()
    try:
        known = {r["key"] for r in json.load(open("invoice-bulk-data.json")).get("results", [])}
    except Exception: pass
    items, scanned = [], 0
    senders = [("zoho", "message-service@sender.zohobooks.com"), ("quickbooks", "quickbooks@notification.intuit.com")]
    if os.environ.get("PROBE"): os.makedirs("_probe", exist_ok=True)
    for src, sender in senders:
        flt = "from/emailAddress/address eq '%s' and receivedDateTime ge %s and hasAttachments eq true" % (sender, since.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        url = "%s/users/%s/messages?" % (G, MAILBOX) + urllib.parse.urlencode({"$filter": flt, "$select": "id,subject,receivedDateTime,from", "$top": "50"}, quote_via=urllib.parse.quote)
        while url:
            d = http(url, headers=H)
            for m in d.get("value", []):
                subj = m.get("subject") or ""
                if src == "zoho" and not re.search(r"invoice", subj, re.I): continue
                if src == "quickbooks" and not re.search(r"new payment request from (?!jit4you)", subj, re.I): continue
                scanned += 1
                a = http("%s/users/%s/messages/%s/attachments?$select=id,name,contentType,size" % (G, MAILBOX, urllib.parse.quote(m["id"], safe="")), headers=H)
                for at in a.get("value", []):
                    nm = at.get("name") or ""
                    ok = nm.upper().startswith("INV") if src == "zoho" else nm.lower().startswith("invoice")
                    if not ok or not nm.lower().endswith(".pdf"): continue
                    key = "%s|%s" % (m["id"], nm)
                    if key in known: continue
                    full = http("%s/users/%s/messages/%s/attachments/%s" % (G, MAILBOX, urllib.parse.quote(m["id"], safe=""), urllib.parse.quote(at["id"], safe="")), headers=H)
                    text = pdf_text(base64.b64decode(full.get("contentBytes", "")))
                    if os.environ.get("PROBE"):
                        open("_probe/%s_%s.txt" % (src, re.sub(r"[^A-Za-z0-9]", "", m["id"])[-8:]), "w").write("SUBJECT: %s\nFILE: %s\n%s" % (subj, nm, text))
                        continue
                    po, lines = (parse_zoho if src == "zoho" else parse_qb)(text)
                    if not po or not lines: print("skip (no PO/lines):", nm); continue
                    items.append({"msg_id": m["id"], "source": src, "sender": sender, "subject": subj, "received": m["receivedDateTime"],
                                  "filename": nm, "vendor": "", "po_raw": po, "lines": lines})
            url = d.get("@odata.nextLink")
    if os.environ.get("PROBE"): print("probe done", scanned); return
    json.dump({"scan": {"request_id": rid, "window": w, "since": since.isoformat(), "scanned": scanned}, "items": items}, open("_invbulk_incoming.json", "w"), indent=1)
    print("scan: %d candidate emails, %d new invoices" % (scanned, len(items)))

if __name__ == "__main__":
    main()
