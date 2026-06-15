"""
One-time URL-change blast for JIT4Labs customer-order-status migration.

Behavior:
  1. Check sentinel file `url_change_done.json` on GitHub. If present, exit silently.
  2. Read tracking file `sent_welcome_emails.json` to get the customer list.
  3. Look up each customer's email in Vtiger (with override map for known gaps).
  4. Skip excluded accounts and customers without email.
  5. For each customer without an existing report on the new repo, generate a
     branded "No open orders at this time" placeholder and push it.
  6. Send a branded "URL changed" notice via Zapier webhook (one per customer).
  7. Write the sentinel so future runs are no-ops.

All secrets are loaded from environment variables. Set before running:
  VTIGER_USER, VTIGER_ACCESS_KEY, GITHUB_TOKEN, ZAPIER_WEBHOOK_URL
"""
import os, sys, re, json, time, base64, urllib.request, urllib.parse, urllib.error
from datetime import datetime

VTIGER_URL = "https://jit4youinc.od2.vtiger.com"
VTIGER_USER = os.environ.get("VTIGER_USER", "")
VTIGER_ACCESS_KEY = os.environ.get("VTIGER_ACCESS_KEY", "")

GITHUB_REPO = "JIT4Labs1/customer-order-status"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_PAGES_URL = "https://JIT4Labs1.github.io/customer-order-status"
OLD_PAGES_URL = "https://JIT4Labs.github.io/customer-order-status"
LOGO_URL = "https://jit4labs1.github.io/customer-order-status/JIT4LABS-Logo.jpg"

ZAPIER_WEBHOOK_URL = os.environ.get(
    "ZAPIER_WEBHOOK_URL", "https://hooks.zapier.com/hooks/catch/2373110/u7vlb95/"
)

TRACKING_FILE = "sent_welcome_emails.json"
SENTINEL_FILE = "url_change_done.json"

EXCLUDED_ACCOUNTS = {"pmahealthcare.com", "labx diagnostics"}

# Per-customer email overrides for accounts whose Vtiger email1 is empty/stale
EMAIL_OVERRIDES = {
    "doctors general laboratory": "mahendra@dglinc.com",
}

THROTTLE_SEC = 0.35
_last = [0.0]


def safe_filename(name):
    s = re.sub(r"[^\w\s-]", "", name).strip().lower()
    s = re.sub(r"\s+", "-", s)
    return s


def gh_api(endpoint, method="GET", data=None):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/{endpoint}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "message": e.read().decode()[:300] if e.fp else ""}


def gh_put_file(repo_path, content_bytes, message):
    content_b64 = base64.b64encode(content_bytes).decode()
    existing = gh_api(f"contents/{repo_path}")
    sha = existing.get("sha") if "sha" in existing else None
    payload = {"message": message, "content": content_b64}
    if sha:
        payload["sha"] = sha
    return gh_api(f"contents/{repo_path}", method="PUT", data=payload)


def vt_query(q, max_retries=8):
    encoded = urllib.parse.quote(q + ";")
    url = f"{VTIGER_URL}/restapi/v1/vtiger/default/query?query={encoded}"
    auth = base64.b64encode(f"{VTIGER_USER}:{VTIGER_ACCESS_KEY}".encode()).decode()
    for attempt in range(max_retries):
        elapsed = time.time() - _last[0]
        if elapsed < THROTTLE_SEC:
            time.sleep(THROTTLE_SEC - elapsed)
        _last[0] = time.time()
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Basic {auth}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
                return data.get("result", []) if data.get("success") else []
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(2 ** attempt, 30))
                continue
            return []
    return []


def vt_query_all(q):
    out, off = [], 0
    while True:
        batch = vt_query(f"{q} LIMIT {off}, 100")
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        off += 100
    return out


def placeholder_html(customer_name, customer_address):
    address_block = ""
    if customer_address:
        address_block = (
            f'<p style="margin: 2px 0 0 0; font-size: 11px; '
            f'color: rgba(16, 30, 62, 0.4);">{customer_address}</p>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Open Order Report — {customer_name}</title>
<style>
  body {{ margin: 0; padding: 0; background: #f0f2f5; font-family: 'Open Sans', 'Segoe UI', Arial, sans-serif; color: rgba(16, 30, 62, 0.75); }}
  .wrap {{ max-width: 760px; margin: 32px auto; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
  .header {{ background: #fff; padding: 24px 40px; border-bottom: 3px solid #008080; display: flex; justify-content: space-between; align-items: center; }}
  .header img {{ height: 48px; }}
  .header .title {{ text-align: right; }}
  .header .title h1 {{ margin: 0; font-size: 20px; color: #101E3E; font-weight: 700; }}
  .body {{ padding: 56px 40px; text-align: center; }}
  .body h2 {{ font-size: 22px; color: #101E3E; margin: 0 0 12px 0; }}
  .body p {{ font-size: 15px; line-height: 1.7; margin: 0 0 12px 0; }}
  .footer {{ background: #101E3E; color: rgba(255,255,255,0.7); font-size: 12px; padding: 18px 40px; text-align: center; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <img src="{LOGO_URL}" alt="JIT4Labs">
    <div class="title">
      <h1>Open Order Report</h1>
      <p style="margin: 4px 0 0 0; font-size: 13px; color: rgba(16, 30, 62, 0.55);">{customer_name}</p>
      {address_block}
    </div>
  </div>
  <div class="body">
    <h2>No open orders at this time</h2>
    <p>All of your previous orders have been fully delivered.</p>
    <p>This page refreshes daily. When new orders are placed and processed, they will appear here automatically.</p>
    <p style="margin-top: 28px; font-size: 13px; color: rgba(16, 30, 62, 0.55);">
      Questions? Email <a href="mailto:customersupport@jit4you.com" style="color: #008080; text-decoration: none;">customersupport@jit4you.com</a>
    </p>
  </div>
  <div class="footer">© {datetime.now().year} JIT4Labs — Powered by JIT4You Inc.</div>
</div>
</body>
</html>"""


def url_change_email_html(customer_name, new_url, old_url):
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Your JIT4Labs Open Order portal has a new link</title></head>
<body style="margin: 0; padding: 0; background-color: #f0f2f5; font-family: 'Open Sans', 'Segoe UI', Arial, sans-serif; color: rgba(16, 30, 62, 0.75);">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #f0f2f5;">
<tr><td align="center" style="padding: 32px 16px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; width: 100%; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06);">
  <tr>
    <td style="background-color: #ffffff; padding: 24px 40px; border-bottom: 3px solid #008080;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="vertical-align: middle;">
          <img src="{LOGO_URL}" alt="JIT4Labs" width="140" style="display: block; height: auto;">
        </td>
        <td style="text-align: right; vertical-align: middle;">
          <p style="margin: 0; font-size: 20px; font-weight: 700; color: #101E3E; letter-spacing: -0.3px;">Open Order Portal</p>
          <p style="margin: 4px 0 0 0; font-size: 13px; color: rgba(16, 30, 62, 0.55);">{customer_name}</p>
        </td>
      </tr></table>
    </td>
  </tr>
  <tr>
    <td style="padding: 36px 40px;">
      <p style="margin: 0 0 18px 0; font-size: 15px; line-height: 1.7;">
        Dear <strong style="color: #101E3E;">{customer_name}</strong>,
      </p>
      <p style="margin: 0 0 18px 0; font-size: 15px; line-height: 1.7;">
        The URL for your <strong style="color: #101E3E;">Open Order Report</strong> has moved to a new home. Please update your bookmark using the link below — the old URL will no longer work.
      </p>
      <p style="margin: 24px 0; text-align: center;">
        <a href="{new_url}" style="display: inline-block; background: #008080; color: #ffffff; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px;">View Your Open Order Report</a>
      </p>
      <p style="margin: 0 0 12px 0; font-size: 13px; color: rgba(16, 30, 62, 0.55); text-align: center;">
        Or copy this URL:<br><span style="color: #008080;">{new_url}</span>
      </p>
      <p style="margin: 24px 0 0 0; font-size: 15px; line-height: 1.7;">
        Everything else stays the same — same data, same daily refresh, same support team. If you have any questions, reach us at <a href="mailto:customersupport@jit4you.com" style="color: #008080;">customersupport@jit4you.com</a>.
      </p>
      <p style="margin: 18px 0 0 0; font-size: 15px; line-height: 1.7;">
        Thank you,<br><strong style="color: #101E3E;">The JIT4Labs Team</strong>
      </p>
    </td>
  </tr>
  <tr>
    <td style="background-color: #101E3E; padding: 18px 40px; text-align: center; color: rgba(255,255,255,0.7); font-size: 12px;">
      © {datetime.now().year} JIT4Labs — Powered by JIT4You Inc.
    </td>
  </tr>
</table>
</td></tr></table>
</body>
</html>"""


def send_via_zapier(to_name, to_email, subject, html_body):
    payload = {
        "to_email": to_email,
        "to_name": to_name,
        "subject": subject,
        "html_body": html_body,
        "customer_name": to_name,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(ZAPIER_WEBHOOK_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True
    except Exception as e:
        print(f"    ! Zapier error for {to_name}: {e}")
        return False


def main():
    print("=" * 60)
    print("JIT4Labs One-Time URL-Change Blast")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Idempotency: bail if sentinel already exists
    sentinel = gh_api(f"contents/{SENTINEL_FILE}")
    if "content" in sentinel:
        print("Sentinel found — URL-change blast already completed. Exiting.")
        return 0

    # Required secrets
    missing = [k for k, v in {
        "VTIGER_USER": VTIGER_USER, "VTIGER_ACCESS_KEY": VTIGER_ACCESS_KEY,
        "GITHUB_TOKEN": GITHUB_TOKEN, "ZAPIER_WEBHOOK_URL": ZAPIER_WEBHOOK_URL,
    }.items() if not v]
    if missing:
        print(f"Missing env vars: {missing}. Exiting.")
        return 1

    # Load tracking
    tr = gh_api(f"contents/{TRACKING_FILE}")
    if "content" not in tr:
        print("Tracking file not found. Exiting.")
        return 1
    tracking = json.loads(base64.b64decode(tr["content"]).decode())
    customers = tracking.get("sent", [])
    print(f"\nTracking-file customers: {len(customers)}")

    # Fetch Vtiger accounts once
    print("Fetching Vtiger accounts…")
    accts = vt_query_all(
        "SELECT id, accountname, email1, bill_street, bill_city, bill_state, bill_code FROM Accounts"
    )
    acct_by_lower = {a["accountname"].strip().lower(): a for a in accts}
    print(f"  {len(accts)} accounts")

    # Existing reports on repo
    root_listing = gh_api("contents/")
    existing_reports = set()
    if isinstance(root_listing, list):
        existing_reports = {
            f["name"] for f in root_listing
            if f["name"].endswith(".html") and f["name"] != "index.html"
        }
    print(f"  Existing reports on new repo: {len(existing_reports)}")

    # Build per-customer plan
    placeholders_pushed = 0
    emails_sent = 0
    skipped = []

    for c in customers:
        lk = c.strip().lower()
        if lk in EXCLUDED_ACCOUNTS:
            skipped.append((c, "EXCLUDED_ACCOUNTS"))
            continue

        acct = acct_by_lower.get(lk, {})
        email = EMAIL_OVERRIDES.get(lk, acct.get("email1", "") or "")
        if not email:
            skipped.append((c, "no email"))
            continue

        fname = safe_filename(c) + ".html"
        new_url = f"{GITHUB_PAGES_URL}/{fname}"
        old_url = f"{OLD_PAGES_URL}/{fname}"

        # Generate placeholder if no report exists
        if fname not in existing_reports:
            addr_parts = [acct.get(k, "") for k in ("bill_street", "bill_city", "bill_state", "bill_code")]
            address = ", ".join(p for p in addr_parts if p)
            html = placeholder_html(c, address)
            result = gh_put_file(
                fname, html.encode("utf-8"),
                f"Add placeholder report for {c} (URL-change migration)"
            )
            if "content" in result:
                placeholders_pushed += 1
                print(f"  + placeholder: {fname}")
            else:
                print(f"  ! placeholder push failed for {fname}: {result.get('error')} {result.get('message','')[:120]}")
                skipped.append((c, "placeholder push failed"))
                continue

        # Send URL-change email
        subject = "Your JIT4Labs Open Order portal has a new link"
        body = url_change_email_html(c, new_url, old_url)
        if send_via_zapier(c, email, subject, body):
            emails_sent += 1
            print(f"  ✓ email -> {c} <{email}>")
        else:
            skipped.append((c, "email send failed"))

    # Write sentinel
    sentinel_doc = {
        "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "emails_sent": emails_sent,
        "placeholders_pushed": placeholders_pushed,
        "skipped": [{"customer": c, "reason": r} for c, r in skipped],
    }
    result = gh_put_file(
        SENTINEL_FILE,
        json.dumps(sentinel_doc, indent=2).encode("utf-8"),
        "URL-change blast completed",
    )
    sentinel_ok = "content" in result

    print("\n" + "=" * 60)
    print("URL-CHANGE BLAST COMPLETE")
    print("=" * 60)
    print(f"  Placeholders pushed: {placeholders_pushed}")
    print(f"  Emails sent:         {emails_sent}")
    print(f"  Skipped:             {len(skipped)}")
    for c, r in skipped:
        print(f"    - {c}: {r}")
    print(f"  Sentinel written:    {'yes' if sentinel_ok else 'NO — will re-run!'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
