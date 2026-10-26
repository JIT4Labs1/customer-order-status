#!/usr/bin/env python3
"""Send (or save as draft) an email from customersupport@jit4you.com via Microsoft Graph.
Payload comes from outbox/<id>.json committed by the dashboard button. Auth: sha256(MS_SEND_PIN + ':' + id)."""
import os, sys, json, hashlib, re, urllib.request, urllib.parse, urllib.error
SENDER = "customersupport@jit4you.com"          # fixed - the page cannot change the sender
RID = os.environ.get("OUTBOX_ID", "")
if not re.fullmatch(r"[a-z0-9]{6,40}", RID): sys.exit("bad id")
p = json.load(open("outbox/%s.json" % RID, encoding="utf-8"))
pin = os.environ.get("MS_SEND_PIN", "")
if not pin or hashlib.sha256((pin + ":" + RID).encode()).hexdigest() != p.get("auth"):
    sys.exit("AUTH FAILED - request ignored")
mode = p.get("mode")
if mode not in ("send", "draft"): sys.exit("bad mode")
EM = re.compile(r"^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$")
def addrs(s):
    out = [a.strip() for a in re.split(r"[,;]+", s or "") if a.strip()]
    for a in out:
        if not EM.match(a): sys.exit("bad address: " + a)
    return out
to, cc = addrs(p.get("to")), addrs(p.get("cc"))
if mode == "send" and not to: sys.exit("no recipient")
if len(to) + len(cc) > 10: sys.exit("too many recipients")
html = p.get("html", "")
if not html or len(html) > 600000: sys.exit("bad body size")
subject = (p.get("subject") or "JIT4Labs")[:250]
def post(url, data, headers, form=False):
    body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    r = urllib.request.Request(url, body, headers)
    try:
        with urllib.request.urlopen(r, timeout=60) as f: return f.status, f.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
s, b = post("https://login.microsoftonline.com/%s/oauth2/v2.0/token" % os.environ["MS_TENANT_ID"],
            {"client_id": os.environ["MS_CLIENT_ID"], "client_secret": os.environ["MS_CLIENT_SECRET"],
             "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}, {}, True)
if s != 200: sys.exit("token failed %s" % s)
tok = json.loads(b)["access_token"]
msg = {"subject": subject, "body": {"contentType": "HTML", "content": html},
       "toRecipients": [{"emailAddress": {"address": a}} for a in to],
       "ccRecipients": [{"emailAddress": {"address": a}} for a in cc]}
H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
base = "https://graph.microsoft.com/v1.0/users/%s" % SENDER
if mode == "send":
    s, b = post(base + "/sendMail", {"message": msg, "saveToSentItems": True}, H)
    ok = s == 202
else:
    s, b = post(base + "/messages", msg, H)
    ok = s == 201
print(("OK " if ok else "FAILED ") + mode, "HTTP", s, "to=%s cc=%s subject=%r label=%r" % (to, cc, subject, p.get("label")))
if not ok:
    print(b[:300]); sys.exit(1)
