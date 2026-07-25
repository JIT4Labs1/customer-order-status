#!/usr/bin/env python3
"""
Customer Prices — for each Independent Diagnostic Lab customer, a per-SO pricing
matrix: every 2026 Sales Order's line items with SKU, unit selling price, and the
product's COGS (Vtiger Products field 'purchase_cost'). Powers the "Customer Prices"
dashboard tab (SO columns x SKU rows; each cell = unit selling price in that SO).

Reuses vt.retrieve_with_retry (cached), so when this runs after Customer Analysis
in the same process the SO details are already warm — no extra Vtiger load.
"""
import datetime

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:
    _PT = None

TARGET_INDUSTRY = "independent diagnostic lab"

# Non-product / freight / fee lines to drop from the pricing matrix.
SKIP_ITEMS = ("shipping", "freight", "handling", "restricted", "discount",
              "credit", "surcharge", "fuel", "adjustment")


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
    # pure punctuation / empty
    if not any(ch.isalnum() for ch in n):
        return True
    return False


def _sku_from_name(name):
    """Fallback SKU: first token in the product name that looks like a part number."""
    for tok in (name or "").replace(",", " ").split():
        t = tok.strip()
        # a code token has digits and is short-ish (e.g. A16793, 33880, OSR6134, VV120-DO)
        if 3 <= len(t) <= 16 and any(c.isdigit() for c in t) and any(c.isalpha() or c.isdigit() for c in t):
            return t.upper()
    return (name or "").strip()[:16].upper()


def build_customer_prices(vt):
    now = _pac_now()
    Y = now.year

    # 1) IDL accounts
    accts = vt.query_all("SELECT id, accountname, industry, email1 FROM Accounts")
    idl = {a["id"]: {"name": a.get("accountname", ""), "email": (a.get("email1", "") or "")}
           for a in accts
           if (a.get("industry", "") or "").strip().lower() == TARGET_INDUSTRY}

    # 2) Products map: productid -> {sku, cogs, name}
    pmap = {}
    for p in vt.query_all("SELECT id, productcode, purchase_cost, productname FROM Products"):
        pmap[p["id"]] = {
            "sku": (p.get("productcode") or "").strip(),
            "cogs": _f(p.get("purchase_cost")),
            "name": (p.get("productname") or "").strip(),
        }

    # 3) IDL Sales Orders this year (non-cancelled)
    sos = vt.query_all(
        "SELECT id, salesorder_no, account_id, createdtime, sostatus FROM SalesOrder "
        "WHERE createdtime >= '%d-01-01' AND createdtime < '%d-01-01'" % (Y, Y + 1))
    idl_sos = [s for s in sos if s.get("account_id") in idl
               and (s.get("sostatus", "") or "").strip().lower() != "cancelled"]
    # oldest first so SO columns read left->right chronologically
    idl_sos.sort(key=lambda s: str(s.get("createdtime", "")))

    by_acct = {}
    for s in idl_sos:
        aid = s["account_id"]
        detail = vt.retrieve_with_retry(s["id"], label="SO-CP")
        if not detail:
            continue
        items = []
        seen = {}  # sku -> index (merge duplicate SKU lines within one SO)
        for li in detail.get("LineItems", detail.get("lineItems", [])) or []:
            name = li.get("product_name", "") or li.get("productid_display", "")
            if _is_skip(name):
                continue
            pid = li.get("productid", "")
            pinfo = pmap.get(pid, {})
            sku = pinfo.get("sku") or _sku_from_name(name)
            cogs = pinfo.get("cogs", 0.0)
            unit = _f(li.get("discounted_unit_selling_price"))
            if unit == 0:
                unit = _f(li.get("listprice", li.get("netprice", 0)))
            qty = _f(li.get("quantity", li.get("qty", 0)))
            if sku in seen:
                # keep the latest non-zero unit price; sum qty
                row = items[seen[sku]]
                if unit:
                    row["unit_price"] = round(unit, 2)
                row["qty"] = round(row["qty"] + qty, 2)
            else:
                seen[sku] = len(items)
                items.append({
                    "sku": sku,
                    "product": name,
                    "unit_price": round(unit, 2),
                    "cogs": round(cogs, 2),
                    "qty": round(qty, 2),
                })
        if not items:
            continue
        date = str(s.get("createdtime", ""))[:10]
        by_acct.setdefault(aid, []).append({
            "so_num": s.get("salesorder_no", ""),
            "date": date,
            "status": (s.get("sostatus", "") or "").strip(),
            "items": items,
        })

    customers = []
    for aid, info in idl.items():
        so_list = by_acct.get(aid)
        if not so_list:
            continue
        n_items = sum(len(so["items"]) for so in so_list)
        customers.append({
            "account_id": aid,
            "name": info["name"],
            "email": info["email"],
            "so_count": len(so_list),
            "line_count": n_items,
            "sos": so_list,
        })
    customers.sort(key=lambda c: (-c["so_count"], c["name"].lower()))

    gen = now.strftime("%Y-%m-%d %I:%M %p PT")
    return {"year": Y, "generated_at": gen, "customers": customers}
