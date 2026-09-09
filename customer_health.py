#!/usr/bin/env python3
"""
JIT4Labs — Customer Health (credit-risk early warning)

Builds `customer-health-data.json` for the "Customer Health" tab in the
Financials cluster of open-orders.html.

Scope: Vtiger Accounts with industry == "Independent Diagnostic Lab".
By default only *buying* accounts (>=1 Sales Order or Invoice) are included --
set BUYERS_ONLY = False to cover every tagged account.

AUTOMATED checks (run unattended, every state, no human needed):
  1. CLIA certificate     CMS Provider-of-Services CLIA file (data.cms.gov)
                          -> termination code, cert expiry, accreditation body.
                          A terminated CLIA means the lab legally cannot bill
                          for testing: the single strongest "in trouble" signal.
  2. NPI registry         NPPES public API -> deactivation, last update.
  3. OIG exclusions       LEIE monthly file -> entity/owner excluded from
                          federal healthcare programs.
  4. Website liveness     HTTP status of the account's website.

ASSISTED checks (state registry, civil courts, bankruptcy) cannot be automated:
every state portal differs and most are CAPTCHA- or login-gated. Those live in
`customer-health-manual.json`, refreshed during the weekly assisted run, and are
merged in here. Anything whose manual record is older than STALE_DAYS is
surfaced as "check due" so the queue stays honest about what is unverified.

Usage:
    python3 customer_health.py              # build + publish
    python3 customer_health.py --no-push    # build locally only
    python3 customer_health.py --all-accounts
"""

import os
import re
import csv
import io
import sys
import json
import time
import base64
import argparse
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Vtiger ────────────────────────────────────────────────────────────────────
VTIGER_URL = os.environ.get("VTIGER_URL", "https://jit4youinc.od2.vtiger.com")
VTIGER_USER = os.environ.get("VTIGER_USER", "customersupport@jit4you.com")
VTIGER_ACCESS_KEY = os.environ.get("VTIGER_ACCESS_KEY", "")

INDUSTRY = "independent diagnostic lab"
BUYERS_ONLY = True

# ── GitHub Pages ──────────────────────────────────────────────────────────────
GITHUB_REPO = os.environ.get("GH_PAGES_REPO", "JIT4Labs1/customer-order-status")
GITHUB_TOKEN = os.environ.get("GH_PAT_TOKEN", "")
DATA_FILENAME = "customer-health-data.json"
MANUAL_FILENAME = "customer-health-manual.json"

# ── Data sources ──────────────────────────────────────────────────────────────
CLIA_API = "https://data.cms.gov/data-api/v1/dataset/d3eb38ac-d8e9-40d3-b7b7-6205d3d1dc16/data"
NPPES_API = "https://npiregistry.cms.hhs.gov/api/"
LEIE_CSV = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"

STALE_DAYS = 45          # manual court/registry check older than this = "check due"
UA = {"User-Agent": "jit4labs-customer-health/1.0", "Accept": "application/json"}

# CMS Provider-of-Services termination codes. "00" is the only healthy value.
TRMNTN = {
    "00": ("active", "Active provider"),
    "01": ("closed", "Voluntary — merger or closure"),
    "02": ("closed", "Voluntary — dissatisfied with reimbursement"),
    "03": ("closed", "Voluntary — closed to avoid involuntary termination"),
    "04": ("closed", "Voluntary — other reason"),
    "05": ("revoked", "Involuntary — failed health/safety requirements"),
    "06": ("revoked", "Involuntary — failed to meet agreement"),
    "07": ("changed", "Other — provider status change"),
    "08": ("revoked", "Nonpayment of CLIA fees"),
    "09": ("revoked", "Revoked — unsuccessful proficiency testing"),
    "10": ("revoked", "Revoked — other reason"),
    "11": ("lapsed", "Incomplete CLIA application"),
    "12": ("closed", "No longer performing testing"),
    "13": ("pending", "Awaiting state approval"),
    "14": ("lapsed", "Unable to locate facility"),
    "15": ("lapsed", "Failure to renew certificate"),
    "16": ("closed", "Withdrawn from CLIA"),
    "17": ("dup", "Duplicate number"),
}
ACCRED_COLS = [("COLA_ACRDTD_CD", "COLA"), ("CAP_ACRDTD_CD", "CAP"),
               ("JCAHO_ACRDTD_CD", "Joint Commission"), ("AABB_ACRDTD_CD", "AABB"),
               ("A2LA_ACRDTD_CD", "A2LA"), ("ASHI_ACRDTD_CD", "ASHI"),
               ("AOA_ACRDTD_CD", "AOA")]


def log(m):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def http_json(url, timeout=90, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


# ═══════════════════════════════════════════════════════════════════
# Vtiger
# ═══════════════════════════════════════════════════════════════════
_AUTH = base64.b64encode(f"{VTIGER_USER}:{VTIGER_ACCESS_KEY}".encode()).decode()
_last_call = [0.0]


def vt_query(sql):
    gap = time.time() - _last_call[0]
    if gap < 0.35:
        time.sleep(0.35 - gap)
    _last_call[0] = time.time()
    url = f"{VTIGER_URL}/restapi/v1/vtiger/default/query?query=" + urllib.parse.quote(sql)
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + _AUTH})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    if not d.get("success"):
        raise RuntimeError(d)
    return d["result"]


def vt_all(select, table):
    rows, off = [], 0
    while True:
        page = vt_query(f"SELECT {select} FROM {table} LIMIT {off},100;")
        rows += page
        if len(page) < 100:
            return rows
        off += 100


def load_accounts():
    cols = ("id,accountname,industry,bill_city,bill_state,bill_code,bill_country,"
            "phone,website,email1,createdtime")
    accts = [a for a in vt_all(cols, "Accounts")
             if (a.get("industry") or "").strip().lower() == INDUSTRY]
    log(f"Vtiger: {len(accts)} accounts tagged '{INDUSTRY}'")

    so_n, inv_n, last_activity = defaultdict(int), defaultdict(int), {}
    for s in vt_all("id,account_id,sostatus,createdtime", "SalesOrder"):
        aid = s.get("account_id")
        if aid:
            so_n[aid] += 1
            d = (s.get("createdtime") or "")[:10]
            if d and d > last_activity.get(aid, ""):
                last_activity[aid] = d
    for iv in vt_all("id,account_id,invoicestatus,invoicedate", "Invoice"):
        aid = iv.get("account_id")
        if aid:
            inv_n[aid] += 1
            d = (iv.get("invoicedate") or "")[:10]
            if d and d > last_activity.get(aid, ""):
                last_activity[aid] = d

    out = []
    for a in accts:
        aid = a["id"]
        rec = {
            "vtiger_id": aid,
            "name": (a.get("accountname") or "").strip(),
            "city": (a.get("bill_city") or "").strip(),
            "state": (a.get("bill_state") or "").strip(),
            "zip": (a.get("bill_code") or "").strip(),
            "country": (a.get("bill_country") or "").strip(),
            "phone": (a.get("phone") or "").strip(),
            "website": (a.get("website") or "").strip(),
            "email": (a.get("email1") or "").strip(),
            "so_count": so_n.get(aid, 0),
            "inv_count": inv_n.get(aid, 0),
            "last_order": last_activity.get(aid, ""),
        }
        if BUYERS_ONLY and rec["so_count"] == 0 and rec["inv_count"] == 0:
            continue
        out.append(rec)
    out.sort(key=lambda r: (-(r["so_count"] + r["inv_count"]), r["name"].lower()))
    log(f"In scope: {len(out)} account(s)" + (" (buyers only)" if BUYERS_ONLY else ""))
    return out


# ═══════════════════════════════════════════════════════════════════
# Name matching
# ═══════════════════════════════════════════════════════════════════
_STOP = {"inc", "llc", "lp", "llp", "corp", "corporation", "co", "company", "the",
         "of", "and", "pc", "pa", "pllc", "ltd", "services", "service"}

# Words that describe *every* lab in the file. Two names sharing only these is
# not a match -- that is how "Medlake Laboratory" once matched "Laboratory
# Corporation of America". Identity has to come from a distinctive token.
_GENERIC = {"lab", "labs", "laboratory", "laboratories", "clinical", "diagnostic",
            "diagnostics", "medical", "health", "healthcare", "center", "centre",
            "group", "associates", "care", "sciences", "science", "systems", "system"}


def norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(w for w in s.split() if w and w not in _STOP)


def core(s):
    """Distinctive tokens only -- generic lab words stripped out."""
    return set(norm(s).split()) - _GENERIC


def score_name(a, b):
    """Token overlap 0..1 between two normalized names."""
    A, B = set(norm(a).split()), set(norm(b).split())
    if not A or not B:
        return 0.0
    return len(A & B) / max(len(A), len(B))


def same_entity(a, b):
    """
    True only when the two names share distinctive (non-generic) identity.
    Requires every distinctive token of the shorter name to appear in the other,
    so "Lab District" == "Lab District, Inc" but != "Ultimate Labs".
    """
    A, B = core(a), core(b)
    if not A or not B:
        # Both names are entirely generic -- fall back to a strict full compare.
        return norm(a) == norm(b) and bool(norm(a))
    small, big = (A, B) if len(A) <= len(B) else (B, A)
    return small.issubset(big)


US_STATE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
    "virgin islands": "VI",
}


def st_code(s):
    s = (s or "").strip()
    if len(s) == 2:
        return s.upper()
    return US_STATE.get(s.lower(), "")


# ═══════════════════════════════════════════════════════════════════
# CLIA (CMS Provider of Services)
# ═══════════════════════════════════════════════════════════════════
CLIA_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".clia-cache")
CLIA_CACHE_DAYS = 20      # source file is refreshed quarterly


def clia_for_states(states):
    """
    Pull every CLIA lab row for the states we care about, once.
    Cached on disk: the CMS file only changes quarterly, and re-downloading
    ~400k rows on every weekly run is pointless.
    """
    os.makedirs(CLIA_CACHE_DIR, exist_ok=True)
    by_state = {}
    for st in sorted(s for s in states if s):
        cpath = os.path.join(CLIA_CACHE_DIR, f"clia_{st}.json")
        if os.path.exists(cpath):
            age = (time.time() - os.path.getmtime(cpath)) / 86400
            if age < CLIA_CACHE_DAYS:
                try:
                    with open(cpath) as f:
                        by_state[st] = json.load(f)
                    log(f"  CLIA {st}: {len(by_state[st])} labs (cached, {age:.0f}d old)")
                    continue
                except Exception:
                    pass
        rows, off, size = [], 0, 5000
        while True:
            url = (f"{CLIA_API}?size={size}&offset={off}&"
                   + urllib.parse.urlencode({"filter[STATE_CD]": st}))
            try:
                page = http_json(url)
            except Exception as e:
                log(f"  CLIA {st}: fetch failed ({e})")
                break
            rows += page
            if len(page) < size:
                break
            off += size
        by_state[st] = rows
        if rows:
            try:
                with open(cpath, "w") as f:
                    json.dump(rows, f)
            except Exception as e:
                log(f"  CLIA {st}: cache write failed ({e})")
        log(f"  CLIA {st}: {len(rows)} labs on file")
    return by_state


def match_clia(rec, rows):
    """
    Find this account's CLIA record(s).

    A lab often has several rows on file -- an old terminated certificate plus a
    current one. Only the *absence of any active certificate* means trouble, so
    collect every row belonging to the entity and prefer an active one; report a
    termination only when nothing active exists.
    """
    city = norm(rec["city"])
    cands = []
    for r in rows:
        if not same_entity(rec["name"], r.get("FAC_NAME")):
            continue
        s = score_name(rec["name"], r.get("FAC_NAME"))
        if city and norm(r.get("CITY_NAME")) == city:
            s += 0.5                       # same city = much stronger evidence
        cands.append((s, r))
    if not cands:
        return None

    # If any candidate is in the account's own city, trust only those.
    in_city = [c for c in cands if city and norm(c[1].get("CITY_NAME")) == city]
    pool = in_city or cands
    if not in_city and city:
        # Name-only match in a different city: require an exact normalized name.
        pool = [c for c in pool if norm(c[1].get("FAC_NAME")) == norm(rec["name"])]
        if not pool:
            return None

    def cert_key(r):
        return (r.get("CRTFCTN_DT") or "")

    active = [c for c in pool if (c[1].get("PGM_TRMNTN_CD") or "").strip() == "00"]
    chosen = max(active or pool, key=lambda c: (c[0], cert_key(c[1])))
    best, best_s = chosen[1], chosen[0]
    code = (best.get("PGM_TRMNTN_CD") or "").strip()
    state_, desc = TRMNTN.get(code, ("unknown", f"Code {code}"))
    accs = [lbl for col, lbl in ACCRED_COLS if (best.get(col) or "").strip().upper() == "X"]

    def d(v):
        v = (v or "").strip()
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 and v.isdigit() else ""

    return {
        "number": (best.get("PRVDR_NUM") or "").strip(),
        "facility": (best.get("FAC_NAME") or "").strip(),
        "city": (best.get("CITY_NAME") or "").strip(),
        "state": (best.get("STATE_CD") or "").strip(),
        "code": code,
        "state_label": state_,
        "desc": desc,
        "cert_date": d(best.get("CRTFCTN_DT")),
        "expires": d(best.get("TRMNTN_EXPRTN_DT")),
        "accreditation": accs,
        "match_score": round(best_s, 2),
        "records_on_file": len(pool),
        "active_records": len(active),
    }


# ═══════════════════════════════════════════════════════════════════
# NPPES
# ═══════════════════════════════════════════════════════════════════
def match_npi(rec):
    st = st_code(rec["state"])
    tokens = norm(rec["name"]).split()
    if not tokens:
        return None
    for probe in (" ".join(tokens[:3]), tokens[0]):
        qs = {"version": "2.1", "organization_name": probe + "*", "limit": "20"}
        if st:
            qs["state"] = st
        try:
            d = http_json(NPPES_API + "?" + urllib.parse.urlencode(qs), timeout=45)
        except Exception:
            continue
        best, best_s = None, 0.0
        for r in d.get("results", []):
            b = r.get("basic", {})
            if not same_entity(rec["name"], b.get("organization_name")):
                continue
            s = score_name(rec["name"], b.get("organization_name"))
            if s > best_s:
                best, best_s = r, s
        if best and best_s >= 0.5:
            b = best.get("basic", {})
            return {
                "number": str(best.get("number") or ""),
                "name": b.get("organization_name", ""),
                "status": b.get("status", ""),          # A = active
                "deactivated": b.get("deactivation_date") or "",
                "reactivated": b.get("reactivation_date") or "",
                "last_updated": b.get("last_updated", ""),
                "official": " ".join(x for x in [b.get("authorized_official_first_name"),
                                                 b.get("authorized_official_last_name")] if x).title(),
                "official_title": b.get("authorized_official_title_or_position", ""),
                "match_score": round(best_s, 2),
            }
    return None


# ═══════════════════════════════════════════════════════════════════
# OIG LEIE exclusions
# ═══════════════════════════════════════════════════════════════════
def load_leie():
    try:
        req = urllib.request.Request(LEIE_CSV, headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:
        log(f"  LEIE: unavailable ({e}) — exclusion check skipped this run")
        return None
    idx = defaultdict(list)
    for row in csv.DictReader(io.StringIO(raw)):
        busn = (row.get("BUSNAME") or "").strip()
        if not busn:
            continue
        for tok in set(norm(busn).split()):
            idx[tok].append(row)
    log(f"  LEIE: index built ({len(idx)} tokens)")
    return idx


def match_leie(rec, idx):
    if idx is None:
        return {"checked": False, "hit": False, "detail": "source unavailable"}
    toks = norm(rec["name"]).split()
    if not toks:
        return {"checked": True, "hit": False, "detail": ""}
    cands, seen = [], set()
    for t in toks:
        for row in idx.get(t, []):
            k = id(row)
            if k in seen:
                continue
            seen.add(k)
            cands.append(row)
    st = st_code(rec["state"])
    for row in cands:
        if same_entity(rec["name"], row.get("BUSNAME")) and \
                score_name(rec["name"], row.get("BUSNAME")) >= 0.8 and \
                (not st or (row.get("STATE") or "").strip().upper() == st):
            return {"checked": True, "hit": True,
                    "detail": f"{row.get('BUSNAME','').strip()} — excl. {row.get('EXCLDATE','')} "
                              f"({row.get('EXCLTYPE','')})"}
    return {"checked": True, "hit": False, "detail": ""}


# ═══════════════════════════════════════════════════════════════════
# Website liveness
# ═══════════════════════════════════════════════════════════════════
def check_site(url):
    u = (url or "").strip()
    if not u:
        return {"checked": False, "ok": None, "code": 0, "url": ""}
    if not u.startswith("http"):
        u = "https://" + u
    host = urllib.parse.urlparse(u).netloc.lower()
    if host.endswith("google.com") or host == "google.com":
        return {"checked": False, "ok": None, "code": 0, "url": u, "note": "placeholder URL"}
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (compatible; jit4labs-health/1.0)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return {"checked": True, "ok": r.status < 400, "code": r.status, "url": u}
    except urllib.error.HTTPError as e:
        # 403/406 are usually bot-blocking, not a dead site.
        return {"checked": True, "ok": e.code in (401, 403, 405, 406, 429), "code": e.code, "url": u}
    except Exception as e:
        return {"checked": True, "ok": False, "code": 0, "url": u, "error": type(e).__name__}


# ═══════════════════════════════════════════════════════════════════
# Scoring
# ═══════════════════════════════════════════════════════════════════
def score(rec):
    """alert = stop / secure the account. watch = look before extending terms."""
    flags, status = [], "ok"

    c = rec.get("clia")
    if not c:
        flags.append(("watch", "No CLIA certificate matched on the CMS file"))
    elif c["state_label"] in ("revoked",):
        flags.append(("alert", f"CLIA {c['desc'].lower()}"))
    elif c["state_label"] in ("closed", "lapsed"):
        flags.append(("alert", f"CLIA terminated — {c['desc'].lower()}"))
    elif c["state_label"] in ("pending", "dup", "changed", "unknown"):
        flags.append(("watch", f"CLIA status: {c['desc'].lower()}"))
    else:
        exp = c.get("expires")
        if exp:
            try:
                days = (datetime.strptime(exp, "%Y-%m-%d").date() - datetime.now().date()).days
                if days < 0:
                    flags.append(("alert", f"CLIA certificate expired {exp}"))
                elif days < 90:
                    flags.append(("watch", f"CLIA certificate expires {exp} ({days}d)"))
            except ValueError:
                pass

    n = rec.get("npi")
    if n:
        if n.get("deactivated") and not n.get("reactivated"):
            flags.append(("alert", f"NPI deactivated {n['deactivated']}"))
        elif n.get("status") and n["status"].upper() != "A":
            flags.append(("watch", f"NPI status '{n['status']}'"))

    if rec.get("leie", {}).get("hit"):
        flags.append(("alert", "OIG excluded — " + rec["leie"]["detail"]))

    s = rec.get("site", {})
    if s.get("checked") and s.get("ok") is False:
        flags.append(("watch", f"Website not reachable ({s.get('code') or s.get('error','no response')})"))

    m = rec.get("manual") or {}
    for key, label in (("bankruptcy", "Bankruptcy"), ("registry", "State registry"),
                       ("courts", "Court records"), ("collections", "Collections")):
        v = m.get(key)
        if isinstance(v, dict):
            sev, note = v.get("severity"), v.get("note", "")
            if sev in ("alert", "watch"):
                flags.append((sev, f"{label}: {note}"))

    if any(f[0] == "alert" for f in flags):
        status = "alert"
    elif any(f[0] == "watch" for f in flags):
        status = "watch"

    checked_on = m.get("checked_on", "")
    due = True
    if checked_on:
        try:
            age = (datetime.now().date() - datetime.strptime(checked_on, "%Y-%m-%d").date()).days
            due = age > STALE_DAYS
        except ValueError:
            due = True
    rec["manual_due"] = due
    rec["flags"] = [{"sev": s_, "text": t} for s_, t in flags]
    rec["status"] = status
    return rec


# ═══════════════════════════════════════════════════════════════════
# GitHub push
# ═══════════════════════════════════════════════════════════════════
def gh_push(path, remote_name):
    if not GITHUB_TOKEN:
        log("  GH_PAT_TOKEN not set — skipping push")
        return False
    api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{remote_name}"
    hdr = {"Authorization": f"Bearer {GITHUB_TOKEN}",
           "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": UA["User-Agent"]}
    sha = None
    try:
        req = urllib.request.Request(api, headers=hdr)
        with urllib.request.urlopen(req, timeout=45) as r:
            sha = json.loads(r.read().decode()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log(f"  GitHub GET {remote_name}: HTTP {e.code}")
    with open(path, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    body = {"message": f"customer health: {remote_name} {datetime.now():%Y-%m-%d %H:%M}",
            "content": content}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(api, method="PUT",
                                 headers={**hdr, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=60):
            log(f"  pushed {remote_name}")
            return True
    except urllib.error.HTTPError as e:
        log(f"  push {remote_name} failed: HTTP {e.code} {e.read()[:200]}")
        return False


def load_manual(local_dir):
    p = os.path.join(local_dir, MANUAL_FILENAME)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception as e:
            log(f"  manual layer unreadable ({e})")
    return {}


# ═══════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--all-accounts", action="store_true",
                    help="include tagged accounts that have never ordered")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()

    global BUYERS_ONLY
    if args.all_accounts:
        BUYERS_ONLY = False

    log("Loading Vtiger accounts...")
    recs = load_accounts()

    states = {st_code(r["state"]) for r in recs}
    log(f"Loading CLIA file for {len([s for s in states if s])} state(s)...")
    clia = clia_for_states(states)

    log("Loading OIG LEIE exclusions...")
    leie = load_leie()

    manual = load_manual(args.out)
    manual_by_id = manual.get("labs", {}) if isinstance(manual, dict) else {}

    log("Checking each account...")
    for i, r in enumerate(recs, 1):
        st = st_code(r["state"])
        r["clia"] = match_clia(r, clia.get(st, [])) if st else None
        r["npi"] = match_npi(r)
        r["leie"] = match_leie(r, leie)
        r["site"] = check_site(r["website"])
        r["manual"] = manual_by_id.get(r["vtiger_id"]) or manual_by_id.get(r["name"]) or {}
        score(r)
        log(f"  [{i}/{len(recs)}] {r['name'][:38]:40} {r['status']:5} "
            f"clia={(r['clia'] or {}).get('state_label','—'):8} "
            f"npi={(r['npi'] or {}).get('status','—')}")

    order = {"alert": 0, "watch": 1, "ok": 2}
    recs.sort(key=lambda r: (order.get(r["status"], 3),
                             not r.get("manual_due"),
                             -(r["so_count"] + r["inv_count"])))

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": ("Vtiger industry = Independent Diagnostic Lab"
                  + (", accounts with at least one sales order or invoice" if BUYERS_ONLY else "")),
        "stale_days": STALE_DAYS,
        "counts": {
            "total": len(recs),
            "alert": sum(1 for r in recs if r["status"] == "alert"),
            "watch": sum(1 for r in recs if r["status"] == "watch"),
            "ok": sum(1 for r in recs if r["status"] == "ok"),
            "manual_due": sum(1 for r in recs if r.get("manual_due")),
        },
        "sources": {
            "clia": "CMS Provider of Services — CLIA (quarterly)",
            "npi": "NPPES registry (live)",
            "leie": "OIG LEIE exclusions (monthly)" if leie is not None else "unavailable this run",
            "manual": "state registry / civil courts / bankruptcy — assisted weekly run",
        },
        "labs": recs,
    }

    path = os.path.join(args.out, DATA_FILENAME)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    log(f"Wrote {path}")
    c = out["counts"]
    log(f"Summary: {c['alert']} alert · {c['watch']} watch · {c['ok']} ok · {c['manual_due']} manual check due")

    if not args.no_push:
        log("Publishing...")
        gh_push(path, DATA_FILENAME)
    log("Done!")


if __name__ == "__main__":
    main()
