#!/usr/bin/env python3
"""
JIT4You Customer Open Order Status Report Generator

Connects to Vtiger CRM, fetches open sales orders for specified customers,
computes open (undelivered) quantities, retrieves ETA from purchase orders,
and generates per-customer HTML pages for GitHub Pages hosting.

Usage:
    python3 customer_order_status.py

Outputs:
    - Per-customer HTML files in ./customer-order-status/ directory
    - Pushes to GitHub Pages (JIT4Labs1.github.io/customer-order-status/)
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import base64
import subprocess
from datetime import datetime, date, timedelta
from collections import defaultdict

# ─── Vtiger CRM Configuration ─────────────────────────────────────────────────
# Secrets are loaded from environment variables. Set them before running:
#   export VTIGER_USER=customersupport@jit4you.com
#   export VTIGER_ACCESS_KEY=<access key from Vtiger My Preferences>
#   export GITHUB_TOKEN=<GitHub PAT with repo scope>
VTIGER_URL = "https://jit4youinc.od2.vtiger.com"
VTIGER_USER = os.environ.get("VTIGER_USER", "")
VTIGER_ACCESS_KEY = os.environ.get("VTIGER_ACCESS_KEY", "")

# ─── Target Vendors (PO suppliers) ────────────────────────────────────────────
# Reports are auto-generated for any customer with SOs linked to POs from these vendors
TARGET_VENDORS = ["pma services", "clearchem diagnostics, inc", "allora biotech llc", "aldx"]

# ─── Customer Name Merges ─────────────────────────────────────────────────────
# Map alternate account names to a single canonical name
CUSTOMER_NAME_MERGE = {}

# ─── Excluded Accounts ────────────────────────────────────────────────────────
# These accounts should not get reports (internal/vendor accounts, old duplicates)
EXCLUDED_ACCOUNTS = ["pmahealthcare.com", "labx diagnostics"]

# ─── GitHub Pages Config ──────────────────────────────────────────────────────
GITHUB_REPO = "JIT4Labs1/customer-order-status"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_PAGES_URL = "https://JIT4Labs1.github.io/customer-order-status"

# ─── Zapier Webhook for Welcome Emails (via Outlook) ─────────────────────────
ZAPIER_WEBHOOK_URL = os.environ.get("ZAPIER_WEBHOOK_URL", "https://hooks.zapier.com/hooks/catch/2373110/u7vlb95/")

# ─── Welcome Email Tracking ──────────────────────────────────────────────────
# JSON file stored on GitHub to track which customers already received welcome emails
WELCOME_TRACKING_FILE = "sent_welcome_emails.json"

# ─── Output Directory ─────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "customer-order-status")


# ═══════════════════════════════════════════════════════════════════════════════
# Vtiger REST API Helper
# ═══════════════════════════════════════════════════════════════════════════════

# Proactive throttle — space out requests to stay under Vtiger's rate limit.
# Vtiger tends to throttle around ~3-5 req/sec; 0.35s ≈ 2.8 req/sec, safe.
REQUEST_THROTTLE_SEC = 0.35
_last_request_time = [0.0]

def _throttle():
    """Enforce a minimum gap between Vtiger API requests."""
    now = time.time()
    elapsed = now - _last_request_time[0]
    if elapsed < REQUEST_THROTTLE_SEC:
        time.sleep(REQUEST_THROTTLE_SEC - elapsed)
    _last_request_time[0] = time.time()


def vtiger_query(query_str, max_retries=8):
    """Execute a Vtiger REST API query with proactive throttle + 429 retry."""
    encoded_query = urllib.parse.quote(query_str + ";")
    url = f"{VTIGER_URL}/restapi/v1/vtiger/default/query?query={encoded_query}"

    auth_string = base64.b64encode(f"{VTIGER_USER}:{VTIGER_ACCESS_KEY}".encode()).decode()

    for attempt in range(max_retries):
        _throttle()
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Basic {auth_string}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success"):
                    return data.get("result", [])
                else:
                    print(f"  API error: {data.get('error', {}).get('message', 'Unknown')}")
                    return []
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(2 ** attempt, 30)
                print(f"  Rate limited (429). Waiting {wait}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait)
                continue
            print(f"  HTTP error {e.code}: {e.reason}")
            return []
        except Exception as e:
            print(f"  Request error: {e}")
            return []
    print(f"  FAILED after {max_retries} retries — query was: {query_str[:120]}")
    return []


class VtigerFetchError(Exception):
    """Raised when a Vtiger fetch ultimately fails after retries."""
    pass


def vtiger_query_all(query_str):
    """Fetch all records using pagination (Vtiger limits to 100 per query)."""
    all_results = []
    offset = 0
    while True:
        paginated = f"{query_str} LIMIT {offset}, 100"
        batch = vtiger_query(paginated)
        if not batch:
            break
        all_results.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return all_results


# ═══════════════════════════════════════════════════════════════════════════════
# Data Fetching
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_account_map():
    """Fetch all accounts and return {id: {name, address, email}} map."""
    print("  Fetching accounts...")
    accounts = vtiger_query_all(
        "SELECT id, accountname, email1, bill_street, bill_city, bill_state, bill_code FROM Accounts"
    )
    result = {}
    for a in accounts:
        parts = [p for p in [
            a.get("bill_street", ""),
            a.get("bill_city", ""),
            a.get("bill_state", ""),
            a.get("bill_code", ""),
        ] if p]
        result[a["id"]] = {
            "name": a["accountname"],
            "address": ", ".join(parts) if parts else "",
            "email": a.get("email1", "") or "",
        }
    return result


def fetch_product_map():
    """Fetch all products and return {id: name} map."""
    print("  Fetching products...")
    products = vtiger_query_all("SELECT id, productname FROM Products")
    return {p["id"]: p["productname"] for p in products}


def fetch_vendor_map():
    """Fetch all vendors and return {id: vendorname} map."""
    print("  Fetching vendors...")
    vendors = vtiger_query_all("SELECT id, vendorname FROM Vendors")
    vmap = {v["id"]: v["vendorname"] for v in vendors}
    print(f"    Found {len(vmap)} vendors")
    return vmap


def discover_customers_by_vendor(vendor_map, account_map):
    """
    Start from target vendor POs, find linked SOs, and return qualifying SOs + POs.
    This approach is vendor-first: no SO status or date filter — if a PO from a target
    vendor links to an SO that still has open items, it qualifies.
    """
    print("  Discovering SOs via target vendor Purchase Orders...")

    # Build set of vendor IDs matching TARGET_VENDORS
    target_vendor_ids = set()
    for vid, vname in vendor_map.items():
        if vname.strip().lower() in TARGET_VENDORS:
            target_vendor_ids.add(vid)
            print(f"    Target vendor: {vname} ({vid})")

    if not target_vendor_ids:
        print("  WARNING: No matching vendors found!")
        return [], []

    # Fetch POs from target vendors (non-cancelled, 2026+)
    all_qualifying_pos = []
    so_ids_from_pos = set()
    for tvid in target_vendor_ids:
        vname = vendor_map.get(tvid, "?")
        pos = vtiger_query_all(
            f"SELECT * FROM PurchaseOrder WHERE vendor_id = '{tvid}' "
            f"AND postatus != 'Cancelled'"
        )
        for po in pos:
            so_ref = po.get("salesorder_id", "")
            if so_ref:
                all_qualifying_pos.append(po)
                so_ids_from_pos.add(so_ref)
        print(f"    {vname}: {len(pos)} POs found")

    print(f"    {len(so_ids_from_pos)} unique SOs linked to target vendor POs")

    if not so_ids_from_pos:
        return [], []

    # Step A: Find which CUSTOMERS have at least one qualifying SO
    qualifying_account_ids = set()
    for so_id in so_ids_from_pos:
        sos = vtiger_query_all(
            f"SELECT * FROM SalesOrder WHERE id = '{so_id}' "
            f"AND sostatus != 'Cancelled' AND sostatus != 'Delivered' "
            f"AND sostatus != 'Fully delivered'"
        )
        for so in sos:
            qualifying_account_ids.add(so.get("account_id", ""))

    print(f"    {len(qualifying_account_ids)} customers qualify")

    # Step B: Fetch ALL open SOs for those customers (not just vendor-linked ones)
    qualifying_sos = []
    all_pos = []
    for acct_id in qualifying_account_ids:
        acct_info = account_map.get(acct_id, {"name": "Unknown"})
        acct_name = acct_info["name"] if isinstance(acct_info, dict) else acct_info
        sos = vtiger_query_all(
            f"SELECT * FROM SalesOrder WHERE account_id = '{acct_id}' "
            f"AND sostatus != 'Cancelled' AND sostatus != 'Delivered' "
            f"AND sostatus != 'Fully delivered'"
        )
        print(f"    {acct_name}: {len(sos)} open SOs")
        qualifying_sos.extend(sos)

    # Fetch POs for ALL qualifying SOs (for ETA data)
    so_ids_all = [so["id"] for so in qualifying_sos]
    for so_id in so_ids_all:
        pos = vtiger_query_all(
            f"SELECT * FROM PurchaseOrder WHERE salesorder_id = '{so_id}' "
            f"AND postatus != 'Cancelled'"
        )
        all_pos.extend(pos)

    print(f"    {len(qualifying_sos)} total open SOs, {len(all_pos)} POs for ETA data")
    return qualifying_sos, all_pos


def fetch_delivery_notes_for_sos(so_ids):
    """Fetch Delivery Notes linked to specific Sales Orders."""
    print("  Fetching Delivery Notes for target SOs...")
    all_dns = []
    for so_id in so_ids:
        dns = vtiger_query_all(
            f"SELECT * FROM DeliveryNotes WHERE related_to = '{so_id}'"
        )
        all_dns.extend(dns)
    print(f"    Found {len(all_dns)} Delivery Notes for target SOs")
    return all_dns


def fetch_purchase_orders_for_sos(so_ids):
    """Fetch Purchase Orders linked to specific Sales Orders."""
    print("  Fetching Purchase Orders for ETA data...")
    all_pos = []
    # Vtiger doesn't support IN clause well, so we batch
    for so_id in so_ids:
        pos = vtiger_query_all(
            f"SELECT * FROM PurchaseOrder WHERE salesorder_id = '{so_id}' "
            f"AND postatus != 'Cancelled'"
        )
        all_pos.extend(pos)
    print(f"    Found {len(all_pos)} Purchase Orders")
    return all_pos


def fetch_line_items(module_id, max_retries=10):
    """Fetch line items for a specific record. Raises VtigerFetchError on
    ultimate failure — critical data, silent [] would cause data loss."""
    url = f"{VTIGER_URL}/restapi/v1/vtiger/default/retrieve?id={module_id}"
    auth_string = base64.b64encode(f"{VTIGER_USER}:{VTIGER_ACCESS_KEY}".encode()).decode()

    for attempt in range(max_retries):
        _throttle()
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Basic {auth_string}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success"):
                    result = data.get("result", {})
                    return result.get("LineItems", [])
                return []
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(2 ** attempt, 30)
                print(f"  [line_items {module_id}] 429 — waiting {wait}s (retry {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue
            raise VtigerFetchError(f"HTTP {e.code} fetching line items for {module_id}: {e.reason}")
        except Exception as e:
            raise VtigerFetchError(f"Error fetching line items for {module_id}: {e}")
    raise VtigerFetchError(f"Rate-limited out after {max_retries} retries for {module_id}")


# ═══════════════════════════════════════════════════════════════════════════════
# Data Processing
# ═══════════════════════════════════════════════════════════════════════════════

def compute_open_items(sales_orders, purchase_orders, product_map, account_map):
    """
    Compute open (undelivered) items per customer using Vtiger's built-in
    outstanding_qty field on SO line items (no delivery note cross-referencing needed).

    Returns: {customer_name: {
        "address": str,
        "items": [{so_number, so_date, item_name, ordered_qty, delivered_qty, open_qty, eta, po_number, web_order_id}]
    }}
    """
    print("\nStep 3: Computing open quantities (using SO line item outstanding_qty)...")

    # Build PO ETA map: {so_id: {product_id: {eta, po_number}}}
    po_eta_map = defaultdict(lambda: defaultdict(dict))
    for po in purchase_orders:
        so_id = po.get("salesorder_id", "")
        po_number = po.get("purchaseorder_no", "")
        po_id = po.get("id", "")
        try:
            line_items = fetch_line_items(po_id)
        except VtigerFetchError as e:
            print(f"      WARN: PO ETA unavailable for {po_number} ({e})")
            line_items = []
        for li in line_items:
            prod_id = li.get("productid", "")
            eta = li.get("cf_purchaseorder_eta", "")
            po_eta_map[so_id][prod_id] = {
                "eta": eta,
                "po_number": po_number,
            }

    # Build open items per customer
    customer_data = defaultdict(lambda: {"address": "", "items": []})
    for so in sales_orders:
        so_id = so.get("id", "")
        so_number = so.get("salesorder_no", "")
        so_date = so.get("createdtime", "")[:10]  # YYYY-MM-DD
        acct_id = so.get("account_id", "")
        acct_info = account_map.get(acct_id, {"name": "Unknown", "address": ""})
        customer_name = acct_info["name"] if isinstance(acct_info, dict) else acct_info
        customer_address = acct_info.get("address", "") if isinstance(acct_info, dict) else ""

        # Skip excluded accounts
        if customer_name.strip().lower() in [e.lower() for e in EXCLUDED_ACCOUNTS]:
            continue

        # Merge alternate names to canonical name
        merged = CUSTOMER_NAME_MERGE.get(customer_name.strip().lower(), "")
        if merged:
            customer_name = merged

        so_status = so.get("sostatus", "")
        # Website order ID from "Purchase Order" field on the SO
        web_order_id = so.get("vtiger_purchaseorder", "") or ""

        # Get SO line items
        try:
            so_line_items = fetch_line_items(so_id)
        except VtigerFetchError as e:
            print(f"      ERROR fetching line items for {so_number}: {e}")
            print(f"      This SO will be SKIPPED entirely. Re-run the script to retry.")
            continue

        # Collect open items for this SO first
        so_open_items = []
        for li in so_line_items:
            prod_id = li.get("productid", "")
            prod_name = li.get("product_name", "") or product_map.get(prod_id, "Unknown")
            ordered_qty = float(li.get("quantity", 0))

            # Skip non-product line items (shipping, taxes, fees, etc.)
            SKIP_KEYWORDS = ("shipping", "freight", "sales tax", "tax", "handling", "delivery fee")
            if any(kw in prod_name.lower() for kw in SKIP_KEYWORDS):
                continue

            # Use Vtiger's built-in outstanding_qty field directly
            outstanding = li.get("outstanding_qty", "")
            delivered = li.get("delivered_qty", "")

            if outstanding not in ("", None, "0", "0.000", "0.00"):
                open_qty = float(outstanding)
                delivered_qty = ordered_qty - open_qty
            elif delivered not in ("", None):
                delivered_qty = float(delivered)
                open_qty = ordered_qty - delivered_qty
            else:
                # Fallback: assume nothing delivered
                delivered_qty = 0
                open_qty = ordered_qty

            # Skip fully delivered items
            if open_qty <= 0:
                continue

            # Get ETA from PO line item
            po_info = po_eta_map.get(so_id, {}).get(prod_id, {})
            eta_raw = po_info.get("eta", "")

            # Default ETA: if no ETA on PO line item, use SO date + 10 days
            if not eta_raw:
                try:
                    so_created = datetime.strptime(so_date, "%Y-%m-%d")
                    default_eta = so_created + timedelta(days=10)
                    eta_raw = default_eta.strftime("%Y-%m-%d")
                except ValueError:
                    eta_raw = ""

            so_open_items.append({
                "so_number": so_number,
                "so_date": so_date,
                "item_name": prod_name,
                "ordered_qty": ordered_qty,
                "delivered_qty": delivered_qty,
                "open_qty": open_qty,
                "eta": eta_raw,
                "so_status": so_status,
                "web_order_id": web_order_id,
            })

        # Skip SO entirely if all items are fully delivered
        if not so_open_items:
            print(f"      {so_number}: fully delivered — skipping")
            continue

        customer_data[customer_name]["address"] = customer_address
        customer_data[customer_name]["items"].extend(so_open_items)

    return dict(customer_data)


# ═══════════════════════════════════════════════════════════════════════════════
# HTML Report Generation
# ═══════════════════════════════════════════════════════════════════════════════

def make_safe_filename(name):
    """Convert customer name to a URL/file-safe string."""
    return name.replace(",", "").replace(" ", "-").lower()


def generate_customer_html(customer_name, items, customer_address=""):
    """Generate a professional HTML page for a single customer."""
    report_date = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    safe_name = make_safe_filename(customer_name)

    # Group items by SO
    so_groups = defaultdict(list)
    for item in items:
        so_groups[item["so_number"]].append(item)

    # Sort SOs by date (oldest first), items alphabetically within each SO
    sorted_sos = sorted(so_groups.items(),
                        key=lambda x: x[1][0]["so_date"] if x[1] else "")
    for so_number, so_items in sorted_sos:
        so_items.sort(key=lambda x: x["item_name"].lower())

    # Build table rows
    table_rows = ""
    for so_number, so_items in sorted_sos:
        for idx, item in enumerate(so_items):
            so_display = so_number if idx == 0 else ""
            date_display = item["so_date"] if idx == 0 else ""
            web_order_display = item.get("web_order_id", "") if idx == 0 else ""
            row_class = "first-in-group" if idx == 0 else ""

            # Format ETA with color and "passed" comment
            eta = item["eta"]
            eta_comment = ""
            if eta:
                try:
                    eta_date = datetime.strptime(eta, "%Y-%m-%d")
                    eta_display = eta_date.strftime("%b %d, %Y")
                    days_until = (eta_date.date() - date.today()).days
                    if days_until < 0:
                        # ETA is in the past
                        eta_class = "eta-delayed"
                        eta_comment = '<span class="eta-passed">ETA passed &ndash; follow-up in progress.</span>'
                    elif days_until <= 10:
                        eta_class = "eta-ontrack"
                    else:
                        eta_class = "eta-ontrack"
                except ValueError:
                    eta_display = eta
                    eta_class = "eta-ontrack"
            else:
                eta_display = "TBD"
                eta_class = "eta-ontrack"

            # Format date
            if date_display:
                try:
                    d = datetime.strptime(date_display, "%Y-%m-%d")
                    date_display = d.strftime("%b %d, %Y")
                except ValueError:
                    pass

            table_rows += f"""
            <tr class="{row_class}">
                <td class="so-number">{so_display}</td>
                <td class="web-order">{web_order_display}</td>
                <td class="so-date">{date_display}</td>
                <td class="item-name">{item["item_name"]}</td>
                <td class="qty">{int(item["open_qty"])}</td>
                <td class="{eta_class}">{eta_display}{eta_comment}</td>
            </tr>"""

    total_items = len(items)
    total_sos = len(so_groups)

    # Escape address for HTML
    address_html = f'<div class="customer-address">{customer_address}</div>' if customer_address else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Open Order Report — {customer_name} | JIT4Labs</title>
<link rel="icon" href="https://jit4you.myshopify.com/cdn/shop/files/JIT4LABS_Favicon.png" type="image/png">
<link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Open Sans', sans-serif;
    background: #f0f2f5;
    color: rgba(16, 30, 62, 0.75);
    min-height: 100vh;
  }}

  /* Header — white bg, logo left, title right */
  .header {{
    background: #ffffff;
    color: #101E3E;
    padding: 24px 40px;
    border-bottom: 3px solid #008080;
  }}
  .header-content {{
    max-width: 1200px;
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .header .logo-img {{
    height: 52px;
    width: auto;
  }}
  .header-right {{
    text-align: right;
  }}
  .header h1 {{
    font-size: 24px;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: #101E3E;
  }}
  .header .customer-name {{
    font-size: 15px;
    color: rgba(16, 30, 62, 0.7);
    margin-top: 4px;
    font-weight: 400;
  }}
  .header .customer-address {{
    font-size: 12px;
    color: rgba(16, 30, 62, 0.45);
    margin-top: 2px;
    font-weight: 400;
  }}

  /* Container */
  .container {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 28px 24px;
  }}

  /* Summary Cards */
  .summary-row {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 28px;
  }}
  .summary-card {{
    background: white;
    border-radius: 12px;
    padding: 22px 24px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
    text-align: center;
    border-top: 3px solid #008080;
  }}
  .summary-card .value {{
    font-size: 32px;
    font-weight: 800;
    color: #101E3E;
    line-height: 1;
  }}
  .summary-card .label {{
    font-size: 11px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-top: 8px;
    font-weight: 600;
  }}

  /* Report info bar */
  .info-bar {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    flex-wrap: wrap;
    gap: 8px;
  }}
  .report-date {{
    font-size: 13px;
    color: #888;
  }}
  .legend {{
    display: flex;
    gap: 16px;
    font-size: 12px;
    color: #666;
  }}
  .legend span {{
    display: flex;
    align-items: center;
    gap: 5px;
  }}
  .legend .dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
  }}
  .dot-ontrack {{ background: #008080; }}
  .dot-delayed {{ background: #101E3E; }}

  /* Table */
  .table-card {{
    background: white;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }}
  thead th {{
    background: #101E3E;
    padding: 14px 16px;
    text-align: left;
    font-weight: 700;
    color: #ffffff;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.8px;
    border-bottom: 2px solid #008080;
  }}
  tbody td {{
    padding: 13px 16px;
    border-bottom: 1px solid #f0f0f0;
    vertical-align: top;
  }}
  tbody tr:hover {{
    background: #f5fafa;
  }}
  tr.first-in-group td {{
    border-top: 2px solid #e0e0e0;
  }}
  tbody tr:first-child td {{
    border-top: none;
  }}

  .so-number {{
    font-weight: 700;
    color: #008080;
    white-space: nowrap;
  }}
  .web-order {{
    color: #555;
    white-space: nowrap;
    font-size: 13px;
  }}
  .so-date {{
    color: #777;
    white-space: nowrap;
    font-size: 13px;
  }}
  .item-name {{
    max-width: 340px;
    color: #101E3E;
  }}
  .qty {{
    text-align: center;
    font-weight: 700;
    color: #101E3E;
    font-size: 15px;
  }}

  /* ETA colors */
  .eta-ontrack {{
    color: #008080;
    font-weight: 400;
  }}
  .eta-delayed {{
    color: #101E3E;
    font-weight: 400;
  }}
  .eta-passed {{
    display: block;
    font-size: 11px;
    color: #101E3E;
    font-weight: 400;
    font-style: italic;
    margin-top: 2px;
  }}

  /* Shop button */
  .shop-btn-wrapper {{
    text-align: left;
    margin-top: 28px;
  }}
  .shop-btn {{
    display: inline-block;
    background: #008080;
    color: #ffffff;
    padding: 14px 36px;
    border-radius: 8px;
    font-size: 15px;
    font-weight: 700;
    text-decoration: none;
    transition: background 0.2s;
    letter-spacing: 0.3px;
  }}
  .shop-btn:hover {{
    background: #006666;
  }}

  /* Message section */
  .message-section {{
    margin-top: 28px;
    padding: 24px 28px;
    background: #ffffff;
    border-radius: 12px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
    font-size: 14px;
    color: rgba(16, 30, 62, 0.75);
    line-height: 1.7;
  }}
  .message-section .contact-line {{
    margin-top: 12px;
    font-size: 13px;
    color: #101E3E;
  }}
  .message-section a {{
    color: #008080;
    text-decoration: none;
    font-weight: 600;
  }}

  /* Footer — JIT4You navy */
  .footer {{
    background: #101E3E;
    text-align: center;
    padding: 24px 20px;
    color: rgba(255,255,255,0.6);
    font-size: 12px;
    margin-top: 32px;
  }}
  .footer a {{
    color: #008080;
    text-decoration: none;
  }}

  /* Empty state */
  .empty-state {{
    text-align: center;
    padding: 60px 20px;
    color: #999;
  }}
  .empty-state h2 {{
    font-size: 20px;
    color: #101E3E;
    margin-bottom: 8px;
  }}

  /* Responsive */
  @media (max-width: 768px) {{
    .header {{ padding: 20px; }}
    .header h1 {{ font-size: 20px; }}
    .header .logo-img {{ height: 36px; }}
    .container {{ padding: 16px; }}
    table {{ font-size: 13px; }}
    thead th, tbody td {{ padding: 10px 8px; }}
    .item-name {{ max-width: 180px; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div class="header-content">
    <img src="https://jit4labs1.github.io/customer-order-status/JIT4LABS-Logo.jpg" alt="JIT4Labs" class="logo-img">
    <div class="header-right">
      <h1>Open Order Report</h1>
      <div class="customer-name">{customer_name}</div>
      {address_html}
    </div>
  </div>
</div>

<div class="container">

  <div class="summary-row">
    <div class="summary-card">
      <div class="value">{total_sos}</div>
      <div class="label">Open Sales Orders</div>
    </div>
    <div class="summary-card">
      <div class="value">{total_items}</div>
      <div class="label">Open Line Items</div>
    </div>
    <div class="summary-card">
      <div class="value">{sum(1 for i in items if i.get('eta'))}</div>
      <div class="label">Items with ETA</div>
    </div>
  </div>

  <div class="info-bar">
    <div class="report-date">Report generated: {report_date}</div>
    <div class="legend">
      <span><span class="dot dot-ontrack"></span> On track (5-10 days)</span>
      <span><span class="dot dot-delayed"></span> Delayed (10+ days)</span>
    </div>
  </div>

  <div class="table-card">
    {"" if items else '<div class="empty-state"><h2>All Clear!</h2><p>All items have been delivered. No open orders at this time.</p></div>'}
    {"<table><thead><tr><th>Sales Order</th><th>Order ID</th><th>Date</th><th>Item</th><th>Open Qty</th><th>ETA</th></tr></thead><tbody>" + table_rows + "</tbody></table>" if items else ""}
  </div>

  <!-- shop button disabled -->


  <div class="message-section">
    <p>We are working diligently to provide you with full transparency and visibility into the shipping and delivery process, so you can better plan your operations.</p>
    <p>For any questions, don't hesitate to contact us:</p>
    <p class="contact-line"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#008080" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align: middle; margin-right: 6px;"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg><a href="mailto:CustomerSupport@jit4you.com">CustomerSupport@jit4you.com</a></p>
    <p class="contact-line"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#008080" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align: middle; margin-right: 6px;"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg><a href="tel:+19493969194">(949) 396-9194</a></p>
  </div>

</div>

<div class="footer">
  <p>&copy; {datetime.now().year} JIT4You Inc. &mdash; All rights reserved.</p>
</div>

</body>
</html>"""
    return html


def generate_index_html(customer_names):
    """Generate an index page (not publicly linked) for internal use."""
    report_date = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    customer_links = ""
    for cust in sorted(customer_names):
        safe_name = make_safe_filename(cust)
        customer_links += f'<li><a href="{safe_name}.html">{cust}</a></li>\n'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Customer Order Status — JIT4Labs</title>
<link rel="icon" href="https://jit4you.myshopify.com/cdn/shop/files/JIT4LABS_Favicon.png" type="image/png">
<link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  body {{ font-family: 'Open Sans', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: rgba(16,30,62,0.75); }}
  .index-header {{ background: #101E3E; padding: 24px 30px; border-radius: 12px; margin-bottom: 24px; display: flex; align-items: center; justify-content: space-between; }}
  .index-header h1 {{ color: #fff; font-size: 22px; font-weight: 700; }}
  .index-header img {{ height: 42px; }}
  ul {{ list-style: none; padding: 0; margin-top: 20px; }}
  li {{ margin: 12px 0; }}
  li a {{ display: block; padding: 16px 20px; background: #fff; border-radius: 10px; color: #101E3E; text-decoration: none; font-size: 17px; font-weight: 600; box-shadow: 0 1px 4px rgba(0,0,0,0.06); border-left: 4px solid #008080; transition: background 0.15s; }}
  li a:hover {{ background: #f5fafa; }}
  .date {{ color: #999; font-size: 13px; margin-top: 8px; }}
</style>
</head>
<body>
  <div class="index-header">
    <h1>Customer Order Status</h1>
    <img src="https://jit4labs1.github.io/customer-order-status/JIT4LABS-Logo.jpg" alt="JIT4Labs">
  </div>
  <p class="date">Last updated: {report_date}</p>
  <ul>
    {customer_links}
  </ul>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# GitHub Pages Push
# ═══════════════════════════════════════════════════════════════════════════════

def github_api_request(endpoint, method="GET", data=None):
    """Make an authenticated GitHub API request."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/{endpoint}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"error": e.code, "message": error_body}


def push_file_to_github(filepath, repo_path):
    """Push a single file to GitHub using the Contents API."""
    with open(filepath, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    # Check if file already exists (need its SHA to update)
    existing = github_api_request(f"contents/{repo_path}")
    sha = existing.get("sha", None) if "sha" in existing else None

    payload = {
        "message": f"Update {repo_path} — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    result = github_api_request(f"contents/{repo_path}", method="PUT", data=payload)
    if "content" in result:
        return True
    else:
        print(f"    API error for {repo_path}: {result.get('error', '')} {result.get('message', '')[:200]}")
        return False


def push_to_github():
    """Push generated HTML files to GitHub Pages via API."""
    print("\nStep 5: Pushing to GitHub Pages...")

    success_count = 0
    for fname in os.listdir(OUTPUT_DIR):
        if fname.endswith(".html"):
            filepath = os.path.join(OUTPUT_DIR, fname)
            ok = push_file_to_github(filepath, fname)
            status = "OK" if ok else "FAILED"
            print(f"  {fname}: {status}")
            if ok:
                success_count += 1

    if success_count > 0:
        print(f"  Pushed {success_count} file(s) to {GITHUB_REPO}")
        return True
    else:
        print("  No files were pushed successfully.")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Welcome Email — Tracking, Composition & Sending via Zapier
# ═══════════════════════════════════════════════════════════════════════════════

def load_welcome_tracking():
    """Load the list of customers who already received welcome emails from GitHub."""
    existing = github_api_request(f"contents/{WELCOME_TRACKING_FILE}")
    if "content" in existing:
        try:
            raw = base64.b64decode(existing["content"]).decode()
            data = json.loads(raw)
            return data, existing.get("sha", "")
        except Exception as e:
            print(f"  Warning: Could not parse tracking file: {e}")
    return {"sent": []}, ""


def save_welcome_tracking(data, sha=""):
    """Save the welcome email tracking file to GitHub."""
    content_b64 = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    payload = {
        "message": f"Update welcome email tracking — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha
    result = github_api_request(f"contents/{WELCOME_TRACKING_FILE}", method="PUT", data=payload)
    return "content" in result


def compose_welcome_email_html(customer_name, report_url, customer_address=""):
    """Generate personalized welcome email HTML matching the report header design."""
    address_row = ""
    if customer_address:
        address_row = f'<p style="margin: 2px 0 0 0; font-size: 11px; color: rgba(16, 30, 62, 0.4);">{customer_address}</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Welcome to Your Open Order Report — JIT4Labs</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f0f2f5; font-family: 'Open Sans', 'Segoe UI', Arial, sans-serif; color: rgba(16, 30, 62, 0.75);">

<!-- Wrapper -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #f0f2f5;">
<tr><td align="center" style="padding: 32px 16px;">

<!-- Email Container -->
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; width: 100%; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06);">

  <!-- Header — matches report: white bg, logo left, title right, teal border -->
  <tr>
    <td style="background-color: #ffffff; padding: 24px 40px; border-bottom: 3px solid #008080;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="vertical-align: middle;">
            <img src="https://jit4labs1.github.io/customer-order-status/JIT4LABS-Logo.jpg" alt="JIT4Labs" width="140" style="display: block; height: auto;">
          </td>
          <td style="text-align: right; vertical-align: middle;">
            <p style="margin: 0; font-size: 20px; font-weight: 700; color: #101E3E; letter-spacing: -0.3px;">Open Order Report</p>
            <p style="margin: 4px 0 0 0; font-size: 13px; color: rgba(16, 30, 62, 0.55);">{customer_name}</p>
            {address_row}
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Body Content -->
  <tr>
    <td style="padding: 36px 40px;">
      <p style="margin: 0 0 18px 0; font-size: 15px; line-height: 1.7; color: rgba(16, 30, 62, 0.75);">
        Dear <strong style="color: #101E3E;">{customer_name}</strong>,
      </p>
      <p style="margin: 0 0 18px 0; font-size: 15px; line-height: 1.7; color: rgba(16, 30, 62, 0.75);">
        We are excited to introduce your personalized <strong style="color: #101E3E;">Open Order Report</strong> — an online dashboard where you can view the status of all your open orders with JIT4Labs at any time.
      </p>

      <!-- Feature Cards -->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin: 24px 0;">
        <tr>
          <td style="padding: 14px 18px; background: #f5fafa; border-left: 3px solid #008080; border-radius: 6px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding-right: 12px; vertical-align: top; font-size: 20px; color: #008080;">&#128340;</td>
                <td style="font-size: 14px; color: rgba(16, 30, 62, 0.75); line-height: 1.5;">
                  <strong style="color: #101E3E;">Updated Daily</strong> — Your report refreshes every morning at 6:30 AM with the latest order information.
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr><td style="height: 10px;"></td></tr>
        <tr>
          <td style="padding: 14px 18px; background: #f5fafa; border-left: 3px solid #008080; border-radius: 6px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding-right: 12px; vertical-align: top; font-size: 20px; color: #008080;">&#128278;</td>
                <td style="font-size: 14px; color: rgba(16, 30, 62, 0.75); line-height: 1.5;">
                  <strong style="color: #101E3E;">Bookmark It</strong> — Save the link to your browser bookmarks and check your order status anytime, from any device.
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr><td style="height: 10px;"></td></tr>
        <tr>
          <td style="padding: 14px 18px; background: #f5fafa; border-left: 3px solid #008080; border-radius: 6px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding-right: 12px; vertical-align: top; font-size: 20px; color: #008080;">&#128203;</td>
                <td style="font-size: 14px; color: rgba(16, 30, 62, 0.75); line-height: 1.5;">
                  <strong style="color: #101E3E;">Full Transparency</strong> — See open items, quantities, and estimated delivery dates for every order.
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>

      <p style="margin: 24px 0 28px 0; font-size: 15px; line-height: 1.7; color: rgba(16, 30, 62, 0.75);">
        Click the button below to view your report now:
      </p>

      <!-- CTA Button -->
      <table role="presentation" cellpadding="0" cellspacing="0">
        <tr>
          <td style="background-color: #008080; border-radius: 8px; text-align: center;">
            <a href="{report_url}" style="display: inline-block; padding: 16px 40px; font-size: 16px; font-weight: 700; color: #ffffff; text-decoration: none; letter-spacing: 0.3px;">View My Open Order Report</a>
          </td>
        </tr>
      </table>

      <p style="margin: 24px 0 0 0; font-size: 13px; color: rgba(16, 30, 62, 0.5); line-height: 1.6;">
        You can also copy and paste this link into your browser:<br>
        <a href="{report_url}" style="color: #008080; text-decoration: none; word-break: break-all;">{report_url}</a>
      </p>
    </td>
  </tr>

  <!-- Divider -->
  <tr>
    <td style="padding: 0 40px;">
      <div style="border-top: 1px solid #e8e8e8;"></div>
    </td>
  </tr>

  <!-- Message Section -->
  <tr>
    <td style="padding: 28px 40px;">
      <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.7; color: rgba(16, 30, 62, 0.75);">
        We are working diligently to provide you with full transparency and visibility into the shipping and delivery process, so you can better plan your operations.
      </p>
      <p style="margin: 0 0 12px 0; font-size: 14px; color: rgba(16, 30, 62, 0.75);">For any questions, don't hesitate to contact us:</p>
      <p style="margin: 8px 0 4px 0; font-size: 14px;">
        &#9993; <a href="mailto:CustomerSupport@jit4you.com" style="color: #008080; text-decoration: none; font-weight: 600;">CustomerSupport@jit4you.com</a>
      </p>
      <p style="margin: 4px 0 0 0; font-size: 14px;">
        &#9742; <a href="tel:+19493969194" style="color: #008080; text-decoration: none; font-weight: 600;">(949) 396-9194</a>
      </p>
    </td>
  </tr>

  <!-- Footer — matches report: navy bg -->
  <tr>
    <td style="background-color: #101E3E; padding: 24px 40px; text-align: center;">
      <p style="margin: 0; font-size: 12px; color: rgba(255,255,255,0.6);">&copy; {datetime.now().year} JIT4You Inc. &mdash; All rights reserved.</p>
    </td>
  </tr>

</table>
<!-- End Email Container -->

</td></tr>
</table>
<!-- End Wrapper -->

</body>
</html>"""


def send_welcome_email_via_zapier(customer_name, customer_email, report_url, html_body):
    """Send a welcome email via Zapier catch hook (triggers Outlook send)."""
    payload = {
        "to_email": customer_email,
        "customer_name": customer_name,
        "report_url": report_url,
        "subject": f"Your Open Order Report is Ready — {customer_name}",
        "html_body": html_body,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(ZAPIER_WEBHOOK_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = resp.read().decode()
            print(f"    Zapier response: {result[:200]}")
            return True
    except Exception as e:
        print(f"    Zapier webhook error: {e}")
        return False


def send_welcome_emails_to_new_customers(customer_data, account_map):
    """
    Check which customers are new (not in tracking file) and send welcome emails.
    Only sends once per customer — tracked via sent_welcome_emails.json on GitHub.
    """
    print("\nStep 6: Checking for new customers to send welcome emails...")

    # Load tracking data from GitHub
    tracking, tracking_sha = load_welcome_tracking()
    already_sent = set(name.strip().lower() for name in tracking.get("sent", []))

    # Build reverse map: account_name -> email (from account_map)
    email_lookup = {}
    for acct_id, info in account_map.items():
        if isinstance(info, dict) and info.get("email"):
            email_lookup[info["name"].strip().lower()] = info["email"]

    new_customers = []
    for customer_name in customer_data:
        if customer_name.strip().lower() not in already_sent:
            new_customers.append(customer_name)

    if not new_customers:
        print("  No new customers — all have already received welcome emails.")
        return

    print(f"  Found {len(new_customers)} new customer(s) to email:")
    emails_sent = []

    for customer_name in new_customers:
        safe_name = make_safe_filename(customer_name)
        report_url = f"{GITHUB_PAGES_URL}/{safe_name}.html"
        customer_email = email_lookup.get(customer_name.strip().lower(), "")

        if not customer_email:
            print(f"    {customer_name}: NO EMAIL FOUND in Vtiger — skipping")
            continue

        print(f"    {customer_name} ({customer_email})...")

        # Compose personalized email HTML (include address from customer_data)
        customer_address = customer_data.get(customer_name, {}).get("address", "")
        html_body = compose_welcome_email_html(customer_name, report_url, customer_address)

        # Send via Zapier webhook
        success = send_welcome_email_via_zapier(
            customer_name, customer_email, report_url, html_body
        )

        if success:
            emails_sent.append(customer_name)
            print(f"    ✓ Welcome email sent to {customer_name}")
        else:
            print(f"    ✗ Failed to send to {customer_name}")

    # Update tracking file with newly sent emails
    if emails_sent:
        tracking["sent"].extend(emails_sent)
        tracking["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if save_welcome_tracking(tracking, tracking_sha):
            print(f"  Tracking file updated with {len(emails_sent)} new customer(s)")
        else:
            print("  WARNING: Failed to update tracking file on GitHub!")
    else:
        print("  No emails were sent this run.")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("JIT4You Customer Open Order Status Report")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Step 1: Fetch reference data
    print("\nStep 1: Fetching reference data from Vtiger...")
    account_map = fetch_account_map()
    product_map = fetch_product_map()
    vendor_map = fetch_vendor_map()

    # Step 2: Start from target vendor POs, discover linked SOs and customers
    print("\nStep 2: Discovering customers with qualifying vendor POs...")
    sales_orders, purchase_orders = discover_customers_by_vendor(vendor_map, account_map)

    if not sales_orders:
        print("No open Sales Orders linked to target vendor POs found.")
        return

    # Step 3: Compute open items (uses SO line item outstanding_qty directly)
    customer_data = compute_open_items(
        sales_orders, purchase_orders, product_map, account_map
    )

    # Step 4: Generate HTML reports
    print("\nStep 4: Generating HTML reports...")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for customer_name, cust_data in customer_data.items():
        items = cust_data["items"]
        address = cust_data.get("address", "")
        safe_name = make_safe_filename(customer_name)
        filepath = os.path.join(OUTPUT_DIR, f"{safe_name}.html")
        html_content = generate_customer_html(customer_name, items, address)
        with open(filepath, "w") as f:
            f.write(html_content)
        print(f"  Generated: {safe_name}.html ({len(items)} open items)")

    # Generate index (for internal navigation)
    index_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(index_path, "w") as f:
        f.write(generate_index_html(list(customer_data.keys())))
    print("  Generated: index.html")

    # Step 5: Push to GitHub Pages
    push_to_github()

    # Step 6: Send welcome emails to new customers via Zapier → Outlook
    send_welcome_emails_to_new_customers(customer_data, account_map)

    # Summary
    print("\n" + "=" * 60)
    print("REPORT COMPLETE")
    print("=" * 60)
    for customer_name in customer_data:
        safe_name = make_safe_filename(customer_name)
        url = f"{GITHUB_PAGES_URL}/{safe_name}.html"
        print(f"  {customer_name}: {url}")
    print()


if __name__ == "__main__":
    main()
