#!/usr/bin/env python3
"""
YTD Demand — units sold per item, by month, for the current year.

Built entirely from the Sales Order records already sitting in vt.retrieve_cache
(the main open-orders pass retrieves every non-cancelled 2026 SO), so this adds
ZERO extra Vtiger calls. Powers the "YTD Demand" tab under Operations:

    rows    = items (SKU + product name)
    columns = Jan..current month + YTD total
    sidebar = customer filter (All Customers + one entry per customer)
    search  = free-text filter over SKU / product name

Quantity = sum of SalesOrder line-item quantity. Cancelled SOs are excluded;
shipping/freight/fee lines are dropped (same SKIP_ITEMS rule as Customer Prices).
"""
import datetime

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:
    _PT = None

SKIP_ITEMS = ("shipping", "freight", "handling", "restricted", "discount",
              "credit", "surcharge", "fuel", "adjustment")

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _pac_now():
    return datetime.datetime.now(_PT) if _PT else datetime.datetime.now()


def _f(v):
    try:
        return float(str(v).strip())
    except Exception:
        return 0.0


def _is_skip(name):
    n = (name or "").lower().strip()
    if not n:
        return True
    if any(k in n for k in SKIP_ITEMS):
        return True
    if not any(ch.isalnum() for ch in n):
        return True
    return False


def _sku_from_name(name):
    for tok in (name or "").replace(",", " ").split():
        t = tok.strip()
        if 3 <= len(t) <= 16 and any(c.isdigit() for c in t):
            return t.upper()
    return (name or "").strip()[:16].upper()


def _month_of(rec):
    """Month index 0-11 from the SO date, or None if it isn't this year."""
    for key in ("createdtime", "duedate", "start_date"):
        raw = (rec.get(key) or "").strip()
        if not raw:
            continue
        d = raw[:10]
        try:
            y, m = int(d[0:4]), int(d[5:7])
        except Exception:
            continue
        return y, m - 1
    return None


def build_ytd_demand(vt, year=None, acct_names=None):
    """Return {year, months, generated_at, customers[], items[]}.

    items[] = [{sku, product, vendor, by_month[12], ytd,
                cust: {customer: {m: qty,... , 'ytd': qty}}}]
    Customer-level numbers let the page re-slice client-side with no refetch.
    """
    now = _pac_now()
    Y = year or now.year
    cur_month = now.month - 1 if now.year == Y else 11

    # productid -> (sku, name). Reuse the EXACT query string build_customer_prices /
    # build_po_prices run so it hits the warm query cache — no extra Vtiger scan.
    prod = {}
    try:
        for p in vt.query_all("SELECT id, productcode, purchase_cost, productname FROM Products"):
            pid = p.get("id")
            if pid:
                prod[pid] = ((p.get("productcode") or "").strip(),
                             (p.get("productname") or "").strip())
    except Exception as e:
        try:
            from open_orders_report import log
            log(f"  YTD Demand: Products query failed: {e}")
        except Exception:
            pass

    # account_id -> name (supplied by the caller so we match the main pass's naming)
    acct_names = acct_names or {}

    items = {}          # key -> row
    customers = set()
    so_seen = set()

    for _rid, rec in (getattr(vt, "retrieve_cache", {}) or {}).items():
        if not isinstance(rec, dict):
            continue
        sono = rec.get("salesorder_no")
        li = rec.get("LineItems", rec.get("lineItems"))
        if not sono or not isinstance(li, list):
            continue
        if (rec.get("sostatus") or "").strip().lower() == "cancelled":
            continue
        ym = _month_of(rec)
        if not ym or ym[0] != Y:
            continue
        mi = ym[1]
        if mi < 0 or mi > 11:
            continue
        if sono in so_seen:
            continue
        so_seen.add(sono)

        cust = (acct_names.get(rec.get("account_id", ""))
                or (rec.get("account_id_display") or "").strip()
                or "(no customer)")
        customers.add(cust)

        for it in li:
            if not isinstance(it, dict):
                continue
            name = (it.get("product_name") or it.get("productid_display") or "").strip()
            if _is_skip(name):
                continue
            pid = it.get("productid", "")
            psku, pname = prod.get(pid, ("", ""))
            sku = (psku or _sku_from_name(name)).upper()
            if not sku:
                continue
            qty = _f(it.get("quantity", it.get("qty", 0)))
            if qty <= 0:
                continue

            row = items.get(sku)
            if row is None:
                row = items[sku] = {
                    "sku": sku,
                    "product": pname or name,
                    "vendor": "",
                    "by_month": [0.0] * 12,
                    "ytd": 0.0,
                    "cust": {},
                    "so_count": 0,
                }
            row["by_month"][mi] += qty
            row["ytd"] += qty
            row["so_count"] += 1
            cm = row["cust"].setdefault(cust, {"by_month": [0.0] * 12, "ytd": 0.0})
            cm["by_month"][mi] += qty
            cm["ytd"] += qty

    def _r(x):
        return round(x, 2) if x % 1 else int(x)

    out_items = []
    for row in items.values():
        row["by_month"] = [_r(v) for v in row["by_month"]]
        row["ytd"] = _r(row["ytd"])
        row["cust"] = {c: {"by_month": [_r(v) for v in d["by_month"]], "ytd": _r(d["ytd"])}
                       for c, d in row["cust"].items()}
        out_items.append(row)
    out_items.sort(key=lambda r: (-float(r["ytd"] or 0), r["sku"]))

    return {
        "year": Y,
        "generated_at": now.strftime("%Y-%m-%d %I:%M:%S %p %Z").strip(),
        "months": MONTH_NAMES[:cur_month + 1],
        "month_count": cur_month + 1,
        "customers": sorted(customers),
        "items": out_items,
        "so_count": len(so_seen),
        "note": ("Units = sum of Sales Order line-item quantity for %d. "
                 "Cancelled SOs and shipping/fee lines excluded. Scope matches the "
                 "dashboard: 2026 Sales Orders, ConMed accounts excluded." % Y),
    }
