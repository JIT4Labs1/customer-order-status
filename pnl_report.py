#!/usr/bin/env python3
"""
JIT4You Daily Sales & P&L Report — server-side reimplementation.

Reproduces the EXACT same 7 sections / tables / layout as the agent-built
"daily-vtiger-sales-pnl-report" email, but as a Python function that returns an
HTML fragment (inline styles, email-identical) for embedding as the P&L tab of
the Open Orders page. Pulls fresh from Vtiger via the same REST API the open
orders report uses (build_pnl(vt) receives a VtigerAPI instance).

Section order (fixed): 1 Monthly Summary · 2 New IDL Customers · 3 Note
(Draft PO / Shipping Income / CC Fees) · 4 Industry · 5 Pareto · 6 IDL Stats
(sparklines) · 7 Detailed Report.
"""

import calendar
import json
from datetime import datetime
from collections import defaultdict

# Shipping product IDs — their LineItem amounts are the "shipping" on an SO.
SHIPPING_PRODUCT_IDS = ["6x56546", "25x16189", "25x28867"]
ALLOWED_SO_STATUSES = {"created", "approved", "delivered", "partially delivered",
                       "fully delivered", "sent", "delivery initiated"}
EXCLUDED_PO_STATUSES = {"draft", "cancelled", "new"}
PO394_ADJUSTMENT = 5211.00
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# Payment-processing fee rate used for "Net deposit" (QB payment received minus fees).
# QuickBooks does not expose per-invoice deposit/fee via API, so net deposit is computed as
# amount received x (1 - NET_FEE_RATE). (Interim rate until actual QB fees are wired in.)
NET_FEE_RATE = 0.033


def _pac_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        from datetime import timezone, timedelta
        return datetime.now(timezone.utc) - timedelta(hours=8)


def _f(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _money(x):
    neg = x < 0
    s = "${:,.2f}".format(abs(x))
    return ("-" + s) if neg else s


def _pct(num, den):
    if not den:
        return "—"
    return "{:.1f}%".format(num / den * 100.0)


def _month_of(created):
    try:
        return int(str(created)[5:7])
    except Exception:
        return 0


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ── Sparkline (nested HTML table of colored div bars — email-safe, per spec) ──
def _spark_table(arr, current_month_val, avg, row_max):
    n = len(arr); H = 28; W = 22
    t = '<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
    # Row 1: spend data label on TOP of each bar
    t += '<tr>'
    for i in range(n):
        val = arr[i]
        label = "0" if val == 0 else "{:.1f}k".format(val / 1000.0)
        color = "#bbb" if val == 0 else "#2c3e50"
        if i == n - 1 and val != 0:
            if current_month_val >= avg:
                color = "#2e7d32"
            elif current_month_val > 0 and current_month_val < avg:
                color = "#c62828"
        t += ('<td align="center" valign="bottom" style="width:%dpx;font-size:9px;color:%s;padding:0 0 2px 0;font-family:Arial,sans-serif;white-space:nowrap;">%s</td>'
              % (W, color, label))
    t += '</tr>'
    # Row 2: bars
    t += '<tr>'
    for i in range(n):
        val = arr[i]
        h = max(2, round((val / row_max) * H)) if (val > 0 and row_max > 0) else 1
        color = "#d0d0d0" if val == 0 else "#2c3e50"
        if i == n - 1:
            if val == 0 and current_month_val == 0:
                color = "#d0d0d0"
            elif current_month_val >= avg:
                color = "#2e7d32"
            elif current_month_val > 0 and current_month_val < avg:
                color = "#c62828"
        t += ('<td valign="bottom" align="center" style="width:%dpx;height:%dpx;padding:0;vertical-align:bottom;">'
              '<div style="width:10px;height:%dpx;background:%s;margin:0 auto;line-height:1px;font-size:1px;">&nbsp;</div></td>'
              % (W, H, h, color))
    t += '</tr>'
    # Row 3: month label under each bar
    t += '<tr>'
    for i in range(n):
        m = MONTHS[i] if i < len(MONTHS) else ""
        t += ('<td align="center" style="width:%dpx;font-size:9px;color:#888;padding:2px 0 0 0;font-family:Arial,sans-serif;">%s</td>'
              % (W, m))
    t += '</tr>'
    t += '</table>'
    return t


def _load_qb_payments():
    """Load the QuickBooks payment cache (SO# -> {paid,total,customer,date}) written by the
    dashboard refresh (QuickBooks MCP). Missing/invalid file -> empty maps (QB column shows —)."""
    import os as _os
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "qb_payments.json")
    try:
        d = json.load(open(p))
        d.setdefault("by_cust_date", {})
        # Per-customer date index for the near-date fallback (SO created date can drift a
        # few days from the QuickBooks invoice date).
        idx = {}
        for k, rec in d["by_cust_date"].items():
            if "|" in k:
                c, dt = k.rsplit("|", 1)
                idx.setdefault(c, []).append((dt, rec))
        d["_by_cust"] = idx
        return d
    except Exception:
        return {"by_cust_date": {}, "_by_cust": {}}


def _load_ship_costs():
    """Total UPS shipping COST per receiver, from ups-shipments-data.json ('cost' field on each
    shipment). Used for the Section 7 'Shipping cost' column (medical-spa customers only).
    Receiver in the shipments tab == the P&L customer name."""
    import os as _os
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "ups-shipments-data.json")
    costs = {}
    try:
        d = json.load(open(p))
        for x in d.get("shipments", []):
            c = x.get("cost")
            if c in (None, ""):
                continue
            r = str(x.get("receiver", "")).strip().lower()
            if not r:
                continue
            costs[r] = round(costs.get(r, 0.0) + float(c), 2)
    except Exception:
        pass
    return costs


def build_pnl(vt):
    """Fetch fresh data and return the P&L report HTML fragment."""
    now = _pac_now()
    Y = now.year
    cur_month = now.month
    today_day = now.day
    day_of_year = now.timetuple().tm_yday

    # ── Fetch ────────────────────────────────────────────────────────────────
    year_sos = vt.query_all(
        "SELECT salesorder_no, sostatus, createdtime, hdnGrandTotal, account_id, id, potential_id, "
        "cf_salesorder_leadsourcedealoriginated FROM SalesOrder "
        "WHERE createdtime >= '%d-01-01' AND createdtime < '%d-01-01'" % (Y, Y + 1))
    # Lead source actually lives on the linked Opportunity (Potentials.leadsource);
    # the SO custom field cf_salesorder_leadsourcedealoriginated is only filled in
    # sporadically. Build an opportunity -> leadsource map so we can fall back to it.
    opp_lead = {p["id"]: (p.get("leadsource", "") or "")
                for p in vt.query_all("SELECT id, leadsource FROM Potentials")}
    year_pos = vt.query_all(
        "SELECT purchaseorder_no, postatus, hdnGrandTotal, salesorder_id, id FROM PurchaseOrder "
        "WHERE createdtime >= '%d-01-01' AND createdtime < '%d-01-01'" % (Y, Y + 1))
    accounts = vt.query_all("SELECT id, accountname, industry FROM Accounts")
    acct = {a["id"]: {"name": a.get("accountname", ""), "industry": (a.get("industry", "") or "")}
            for a in accounts}
    # Exclude test/dummy accounts (e.g. "Test company") from the P&L entirely.
    EXCLUDED_ACCOUNTS_PNL = {"test company"}
    _excl_ids = {aid for aid, a in acct.items() if a.get("name", "").strip().lower() in EXCLUDED_ACCOUNTS_PNL}
    if _excl_ids:
        year_sos = [s for s in year_sos if s.get("account_id", "") not in _excl_ids]

    # Shipping map: SO id -> shipping $ (sum qty*listprice of shipping line items)
    ship_map = defaultdict(float)
    for pid in SHIPPING_PRODUCT_IDS:
        for li in vt.query_all("SELECT parent_id, quantity, listprice FROM LineItem WHERE productid = '%s'" % pid):
            ship_map[li.get("parent_id", "")] += _f(li.get("quantity")) * _f(li.get("listprice"))

    # All-time earliest SO per account (for new-customer detection)
    earliest = {}
    for s in vt.query_all("SELECT account_id, createdtime FROM SalesOrder"):
        aid = s.get("account_id", ""); ct = s.get("createdtime", "")
        if aid and ct and (aid not in earliest or ct < earliest[aid]):
            earliest[aid] = ct

    # ── PO totals per SO (+PO394 adjustment) and all-PO map (for draft detection) ──
    pos_by_so = defaultdict(list)
    for po in year_pos:
        pos_by_so[po.get("salesorder_id", "")].append({
            "no": po.get("purchaseorder_no", ""), "status": (po.get("postatus", "") or ""),
            "gross": _f(po.get("hdnGrandTotal"))})
    po_total_by_so = defaultdict(float)
    for so_id, pos in pos_by_so.items():
        tot = 0.0
        for p in pos:
            if p["status"].strip().lower() in EXCLUDED_PO_STATUSES:
                continue
            amt = p["gross"]
            if str(p["no"]).strip().upper() == "PO394":
                amt -= PO394_ADJUSTMENT
            tot += amt
        po_total_by_so[so_id] = tot
    single_draft_ids = {so_id for so_id, pos in pos_by_so.items()
                        if len(pos) == 1 and pos[0]["status"].strip().lower() == "draft"}

    # ── Normalize SOs ────────────────────────────────────────────────────────
    def mk(so):
        sid = so.get("id", "")
        gross = _f(so.get("hdnGrandTotal"))
        ship = ship_map.get(sid, 0.0)
        net = max(0.0, gross - ship)
        aid = so.get("account_id", "")
        return {
            "no": so.get("salesorder_no", ""), "status": (so.get("sostatus", "") or ""),
            "created": so.get("createdtime", ""), "month": _month_of(so.get("createdtime", "")),
            "gross": gross, "ship": ship, "net": net, "id": sid, "acct_id": aid,
            "customer": acct.get(aid, {}).get("name", "Unknown"),
            "industry": acct.get(aid, {}).get("industry", ""),
            "lead": ((so.get("cf_salesorder_leadsourcedealoriginated", "") or "").strip()
                     or opp_lead.get(so.get("potential_id", ""), "")),
            "po_total": po_total_by_so.get(sid, 0.0),
        }
    allowed = [mk(s) for s in year_sos if (s.get("sostatus", "") or "").strip().lower() in ALLOWED_SO_STATUSES]
    main_sos = [s for s in allowed if s["id"] not in single_draft_ids]
    draft_sos = [s for s in allowed if s["id"] in single_draft_ids]
    cur_sos = [s for s in main_sos if s["month"] == cur_month]

    H = []  # html parts
    H.append('<div style="font-family:Arial,Helvetica,sans-serif;color:#2c3e50;font-size:13px;line-height:1.45;">')
    H.append('<h2 style="color:#2c3e50;margin:0 0 4px 0;">JIT4You Daily Sales &amp; P&amp;L Report</h2>'
             '<div style="color:#888;font-size:12px;margin-bottom:18px;">%s</div>' % now.strftime("%-m/%-d/%Y"))

    th = 'background:#2c3e50;color:#fff;padding:8px 10px;text-align:right;font-size:12px;'
    thl = 'background:#2c3e50;color:#fff;padding:8px 10px;text-align:left;font-size:12px;'
    td = 'padding:7px 10px;border-bottom:1px solid #eee;text-align:right;'
    tdl = 'padding:7px 10px;border-bottom:1px solid #eee;text-align:left;'
    tbl = 'border-collapse:collapse;width:100%;max-width:920px;margin:0 0 10px 0;font-size:12px;'

    # ── SECTION 1: Year-to-Date Monthly Summary ──────────────────────────────
    H.append('<h3 style="color:#2c3e50;">1. Year-to-Date Monthly Summary</h3>')
    H.append('<table style="%s"><thead><tr>'
             '<th style="%s">Month</th><th style="%s">Orders</th><th style="%s">SO Revenue</th>'
             '<th style="%s">PO Cost</th><th style="%s">P&amp;L</th><th style="%s">Margin</th>'
             '<th style="%s">Avg Rev/Day</th></tr></thead><tbody>'
             % (tbl, thl, th, th, th, th, th, th))
    t_orders = t_rev = t_cost = 0.0
    for m in range(1, cur_month + 1):
        msos = [s for s in main_sos if s["month"] == m]
        orders = len(msos)
        rev = sum(s["net"] for s in msos)
        cost = sum(s["po_total"] for s in msos)
        pnl = rev - cost
        days = today_day if m == cur_month else calendar.monthrange(Y, m)[1]
        avg = rev / days if days else 0
        t_orders += orders; t_rev += rev; t_cost += cost
        H.append('<tr><td style="%s">%s %d</td><td style="%s">%d</td><td style="%s">%s</td>'
                 '<td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td></tr>'
                 % (tdl, MONTHS[m - 1], Y, td, orders, td, _money(rev), td, _money(cost),
                    td, _money(pnl), td, _pct(pnl, rev), td, _money(avg)))
    t_pnl = t_rev - t_cost
    tot_avg = t_rev / day_of_year if day_of_year else 0
    bt = 'padding:8px 10px;border-top:2px solid #2c3e50;background:#f5f5f5;font-weight:bold;text-align:right;'
    btl = bt + 'text-align:left;'
    H.append('<tr><td style="%s">Total</td><td style="%s">%d</td><td style="%s">%s</td>'
             '<td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td></tr>'
             % (btl, bt, int(t_orders), bt, _money(t_rev), bt, _money(t_cost), bt, _money(t_pnl),
                bt, _pct(t_pnl, t_rev), bt, _money(tot_avg)))
    H.append('</tbody></table>')

    # ── SECTION 2: New IDL Customers ─────────────────────────────────────────
    H.append('<h3 style="color:#2c3e50;">2. New IDL Customers — First Order in %d</h3>' % Y)
    ytd_net_by_acct = defaultdict(float)
    for s in main_sos:
        ytd_net_by_acct[s["acct_id"]] += s["net"]
    new_idl = []  # (month, acct_id, name, first_date, cumulative)
    for aid, ej in earliest.items():
        info = acct.get(aid, {})
        if (info.get("industry", "") or "").strip().lower() != "independent diagnostic lab":
            continue
        if not str(ej).startswith(str(Y)):
            continue
        new_idl.append((_month_of(ej), aid, info.get("name", "Unknown"), ej, ytd_net_by_acct.get(aid, 0.0)))
    H.append('<table style="%s"><thead><tr><th style="%s">Month</th><th style="%s">New IDL Customers</th>'
             '<th style="%s">Customer</th><th style="%s">First Order</th><th style="%s">Cumulative YTD (net)</th>'
             '</tr></thead><tbody>' % (tbl, thl, th, thl, th, th))
    if not new_idl:
        H.append('<tr><td colspan="5" style="%sfont-style:italic;color:#888;text-align:center;">'
                 'No new Independent Diagnostic Lab customers yet this year.</td></tr>' % tdl)
    else:
        by_month = defaultdict(list)
        for r in new_idl:
            by_month[r[0]].append(r)
        gt_count = 0; gt_cum = 0.0
        for m in sorted(by_month.keys()):
            rows = sorted(by_month[m], key=lambda x: x[2].lower())
            for i, r in enumerate(rows):
                cells = ''
                if i == 0:
                    cells = ('<td style="%sbackground:#fafafa;" rowspan="%d">%s %d</td>'
                             '<td style="%sbackground:#fafafa;text-align:right;" rowspan="%d">%d</td>'
                             % (tdl, len(rows), MONTHS[m - 1], Y, td, len(rows), len(rows)))
                first = datetime.strptime(str(r[3])[:10], "%Y-%m-%d").strftime("%-m/%-d/%Y")
                H.append('<tr>%s<td style="%sfont-weight:bold;">%s</td><td style="%s">%s</td>'
                         '<td style="%s">%s</td></tr>'
                         % (cells, tdl, _esc(r[2]), td, first, td, _money(r[4])))
                gt_count += 1; gt_cum += r[4]
        H.append('<tr><td style="%s">Total New IDL Customers %d</td>'
                 '<td style="%scolor:#2e7d32;">%d</td>'
                 '<td colspan="2" style="%s">Cumulative Order Amount YTD (net)</td>'
                 '<td style="%scolor:#2e7d32;">%s</td></tr>'
                 % (btl, Y, bt, gt_count, btl, bt, _money(gt_cum)))
    H.append('</tbody></table>')
    H.append('<div style="font-size:11px;color:#888;margin-bottom:16px;">A "new IDL customer" is an Account with '
             'industry = "Independent Diagnostic Lab" whose earliest Sales Order across all-time history falls in the '
             'current calendar year. <b>Cumulative YTD</b> is the sum of that customer\'s %d Sales Order amounts '
             '(net of shipping).</div>' % Y)

    # ── SECTION 3: Note (Draft PO + Shipping Income + CC Fees) ────────────────
    H.append('<div style="background:#fff8e1;border-left:4px solid #ffc107;padding:14px 16px;margin:0 0 18px 0;max-width:920px;">')
    # Part A — Draft PO table
    H.append('<div style="font-weight:bold;margin-bottom:6px;">SOs excluded due to single Draft PO (gross amounts):</div>')
    if not draft_sos:
        H.append('<div style="font-style:italic;color:#888;margin-bottom:8px;">None.</div>')
    else:
        H.append('<table style="%sbackground:#fff;"><thead><tr><th style="%s">Customer</th><th style="%s">SO #</th>'
                 '<th style="%s">SO Amount</th><th style="%s">PO Total</th><th style="%s">P&amp;L</th>'
                 '<th style="%s">Margin</th></tr></thead><tbody>' % (tbl, thl, thl, th, th, th, th))
        dg = dp = 0.0
        for s in sorted(draft_sos, key=lambda x: x["customer"].lower()):
            pnl = s["gross"] - s["po_total"]
            dg += s["gross"]; dp += s["po_total"]
            H.append('<tr><td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td>'
                     '<td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td></tr>'
                     % (tdl, _esc(s["customer"]), tdl, _esc(s["no"]), td, _money(s["gross"]),
                        td, _money(s["po_total"]), td, _money(pnl), td, _pct(pnl, s["gross"])))
        H.append('<tr><td colspan="2" style="%s">Total</td><td style="%s">%s</td><td style="%s">%s</td>'
                 '<td style="%s">%s</td><td style="%s">%s</td></tr>'
                 % (btl, bt, _money(dg), bt, _money(dp), bt, _money(dg - dp), bt, _pct(dg - dp, dg)))
        H.append('</tbody></table>')
    # Part B — Shipping income (current month only)
    cur_ship = sum(s["ship"] for s in cur_sos)
    H.append('<div style="background:#e8f5e9;border-left:4px solid #4caf50;padding:10px 14px;margin:10px 0;">'
             '<b>Shipping Income (current month):</b> %s &nbsp;·&nbsp; '
             'Estimated additional income (30%%): <b style="color:#2e7d32;">%s</b></div>'
             % (_money(cur_ship), _money(cur_ship * 0.30)))
    # Part C — Expected CC fees (gross basis, exclude Online Reseller)
    cc_base = sum(s["gross"] for s in cur_sos if s["industry"].strip().lower() != "online reseller")
    H.append('<div style="background:#fce4ec;border-left:4px solid #e91e63;padding:10px 14px;margin:10px 0 0 0;">'
             '<b style="color:#880e4f;">Expected CC Fees (gross basis, includes shipping):</b> '
             'Base %s &nbsp;·&nbsp; Fee (3%%): <b style="color:#c62828;">%s</b></div>'
             % (_money(cc_base), _money(cc_base * 0.03)))
    H.append('</div>')

    # ── SECTION 4: Industry Breakdown (current month, NET) ────────────────────
    H.append('<h3 style="color:#2c3e50;">4. Industry Breakdown (current month)</h3>')
    ind = defaultdict(lambda: {"orders": 0, "rev": 0.0, "cost": 0.0})
    for s in cur_sos:
        k = s["industry"] or "(Unspecified)"
        ind[k]["orders"] += 1; ind[k]["rev"] += s["net"]; ind[k]["cost"] += s["po_total"]
    H.append('<table style="%s"><thead><tr><th style="%s">Industry</th><th style="%s">Orders</th>'
             '<th style="%s">SO Amount</th><th style="%s">PO Total</th><th style="%s">P&amp;L</th>'
             '<th style="%s">Margin</th></tr></thead><tbody>' % (tbl, thl, th, th, th, th, th))
    io = ir = ic = 0
    for k in sorted(ind.keys(), key=lambda x: -ind[x]["rev"]):
        v = ind[k]; pnl = v["rev"] - v["cost"]
        io += v["orders"]; ir += v["rev"]; ic += v["cost"]
        H.append('<tr><td style="%s">%s</td><td style="%s">%d</td><td style="%s">%s</td><td style="%s">%s</td>'
                 '<td style="%s">%s</td><td style="%s">%s</td></tr>'
                 % (tdl, _esc(k), td, v["orders"], td, _money(v["rev"]), td, _money(v["cost"]),
                    td, _money(pnl), td, _pct(pnl, v["rev"])))
    H.append('<tr><td style="%s">Total</td><td style="%s">%d</td><td style="%s">%s</td><td style="%s">%s</td>'
             '<td style="%s">%s</td><td style="%s">%s</td></tr></tbody></table>'
             % (btl, bt, io, bt, _money(ir), bt, _money(ic), bt, _money(ir - ic), bt, _pct(ir - ic, ir)))

    # ── SECTION 5: 90% Pareto (current month, NET) ───────────────────────────
    H.append('<h3 style="color:#2c3e50;">5. Key Customers — 90%% Pareto (current month)</h3>')
    par = defaultdict(lambda: {"orders": 0, "rev": 0.0, "cost": 0.0})
    for s in cur_sos:
        lead = s["lead"].strip().lower()
        key = "Inmode*" if lead == "inmode" else ("GoogleAds**" if lead == "googleads" else s["customer"])
        par[key]["orders"] += 1; par[key]["rev"] += s["net"]; par[key]["cost"] += s["po_total"]
    par_total = sum(v["rev"] for v in par.values())
    ranked = sorted(par.items(), key=lambda kv: -kv[1]["rev"])
    H.append('<table style="%s"><thead><tr><th style="%s">#</th><th style="%s">Customer</th><th style="%s">Orders</th>'
             '<th style="%s">SO Amount</th><th style="%s">PO Total</th><th style="%s">P&amp;L</th>'
             '<th style="%s">Margin</th><th style="%s">Cumul. %%</th></tr></thead><tbody>'
             % (tbl, th, thl, th, th, th, th, th, th))
    cum = 0.0; rank = 0
    for name, v in ranked:
        rank += 1; cum += v["rev"]; pnl = v["rev"] - v["cost"]
        cumpct = (cum / par_total * 100.0) if par_total else 0
        H.append('<tr><td style="%s">%d</td><td style="%s">%s</td><td style="%s">%d</td><td style="%s">%s</td>'
                 '<td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td><td style="%s">%.1f%%</td></tr>'
                 % (td, rank, tdl, _esc(name), td, v["orders"], td, _money(v["rev"]), td, _money(v["cost"]),
                    td, _money(pnl), td, _pct(pnl, v["rev"]), td, cumpct))
        if cumpct >= 90.0:
            break
    H.append('</tbody></table>')
    H.append('<div style="font-size:11px;color:#888;margin-bottom:16px;">* Inmode = SOs with Lead Source "InMode" '
             '(aggregated). ** GoogleAds = SOs with Lead Source "GoogleAds" (aggregated). SO Amount is net of shipping.</div>')

    # ── SECTION 6: IDL Customer Statistics (YTD, NET, sparklines) ─────────────
    H.append('<h3 style="color:#2c3e50;">6. Independent Diagnostic Lab Customer Statistics (YTD)</h3>')
    idl = defaultdict(lambda: {"name": "", "months": [0.0] * cur_month})
    for s in main_sos:
        if s["industry"].strip().lower() != "independent diagnostic lab":
            continue
        if 1 <= s["month"] <= cur_month:
            idl[s["acct_id"]]["months"][s["month"] - 1] += s["net"]
            idl[s["acct_id"]]["name"] = s["customer"]
    rows6 = []
    for aid, d in idl.items():
        ytd = sum(d["months"])
        first_m = next((i + 1 for i, v in enumerate(d["months"]) if v > 0), cur_month)
        avg = ytd / (cur_month - first_m + 1) if (cur_month - first_m + 1) > 0 else 0
        cm = d["months"][cur_month - 1]
        rows6.append((d["name"], ytd, avg, d["months"], cm))
    rows6.sort(key=lambda x: -x[1])
    H.append('<table style="%s"><thead><tr><th style="%s">Customer</th><th style="%s">SO Amount YTD</th>'
             '<th style="%s">Monthly Avg</th><th style="%s">Monthly Trend (Jan–%s)</th>'
             '<th style="%s">Current Month</th><th style="%s">%% of Avg</th></tr></thead><tbody>'
             % (tbl, thl, th, th, thl, MONTHS[cur_month - 1], th, th))
    for name, ytd, avg, months, cm in rows6:
        row_max = max(months) if months else 0
        spark = _spark_table(months, cm, avg, row_max)
        pa = (cm / avg * 100.0) if avg else 0
        pcolor = "#2e7d32" if pa >= 100 else ("#c62828" if (cm == 0 or pa < 50) else "#2c3e50")
        H.append('<tr><td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td>'
                 '<td style="%spadding:6px 10px;">%s</td><td style="%s">%s</td>'
                 '<td style="%scolor:%s;font-weight:bold;">%s</td></tr>'
                 % (tdl, _esc(name), td, _money(ytd), td, _money(avg), tdl, spark, td, _money(cm),
                    td, pcolor, ("%.0f%%" % pa) if avg else "—"))
    H.append('</tbody></table>')
    H.append('<div style="font-size:11px;color:#888;margin-bottom:16px;">Monthly Average = YTD SO Amount / (months '
             'since customer\'s first %d order). Monthly Trend = bar chart of SO amounts (Jan–%s %d), scaled per row; '
             'last bar green if current-month ≥ avg, red if &lt; avg. %% of Avg = Current Month / Monthly Avg. '
             'Only %d data used. SO Amount is net of shipping.</div>' % (Y, MONTHS[cur_month - 1], Y, Y))

    # ── SECTION 7: Detailed Report (current month, NET) ──────────────────────
    H.append('<h3 style="color:#2c3e50;">7. Detailed Report</h3>')
    qbp = _load_qb_payments()

    from datetime import date as _date

    def _pd(x):
        try:
            p = str(x)[:10].split("-"); return _date(int(p[0]), int(p[1]), int(p[2]))
        except Exception:
            return None

    def _qb_for(s):
        # Match QB invoices by Customer + created date (Vtiger SO# != QuickBooks invoice #).
        cust = str(s.get("customer", "")).strip().lower()
        dt = str(s.get("created", ""))[:10]
        rec = qbp.get("by_cust_date", {}).get(cust + "|" + dt)
        if rec is not None:
            return rec
        # Near-date fallback: exactly one QB invoice for this customer within +/-7 days.
        d0 = _pd(dt); cand = qbp.get("_by_cust", {}).get(cust, [])
        if d0 is not None and cand:
            near = [r for od, r in cand if _pd(od) and abs((d0 - _pd(od)).days) <= 7]
            if len(near) == 1:
                return near[0]
        return None

    # Month selector (default = current month). Inline onchange toggles the per-month blocks.
    sel = ('<div style="margin:4px 0 10px;font-size:13px;color:#2c3e50;">Month: '
           '<select onchange="var v=this.value;var b=document.querySelectorAll(&quot;.pnl-detail-mo&quot;);'
           'for(var i=0;i&lt;b.length;i++){b[i].style.display=(b[i].getAttribute(&quot;data-mo&quot;)===v)?&quot;&quot;:&quot;none&quot;;}" '
           'style="padding:4px 9px;font-size:13px;border:1px solid #cdd9e6;border-radius:6px;">')
    for m in range(cur_month, 0, -1):
        sel += '<option value="%d"%s>%s %d</option>' % (m, (" selected" if m == cur_month else ""), MONTHS[m - 1], Y)
    sel += '</select></div>'
    H.append(sel)

    # Green column group styles (QuickBooks columns).
    thg = th + "background:#e8f5e9;color:#1b7a3d;"
    tdg = td + "background:#f4fbf5;"
    btg = bt + "background:#d8f0e0;"

    qb_note = False
    for m in range(1, cur_month + 1):
        msos = sorted([s for s in main_sos if s["month"] == m], key=lambda x: x["created"], reverse=True)
        disp = "" if m == cur_month else "none"
        H.append('<div class="pnl-detail-mo" data-mo="%d" style="display:%s;">' % (m, disp))
        H.append('<table style="%s"><thead><tr><th style="%s">Customer</th><th style="%s">SO #</th><th style="%s">Created</th>'
                 '<th style="%s">SO Amount</th><th style="%s">PO Total</th><th style="%s">P&amp;L</th><th style="%s">Margin</th>'
                 '<th style="%s">QB Payment</th><th style="%s">Net deposit</th>'
                 '</tr></thead><tbody>' % (tbl, thl, thl, thl, th, th, th, th, thg, thg))
        dr = dc = dqb = dnet = 0.0
        for s in msos:
            pnl = s["net"] - s["po_total"]; dr += s["net"]; dc += s["po_total"]
            created = datetime.strptime(str(s["created"])[:10], "%Y-%m-%d").strftime("%-m/%-d/%Y") if s["created"] else ""
            rec = _qb_for(s)
            if rec is not None:
                paid = float(rec.get("paid", 0) or 0); dqb += paid
                qbcell = _money(paid) if paid > 0 else '<span style="color:#c62828;">%s</span>' % _money(0)
                netdep = round(paid * (1.0 - NET_FEE_RATE), 2); dnet += netdep
                netcell = _money(netdep) if paid > 0 else '<span style="color:#c62828;">%s</span>' % _money(0)
            else:
                qbcell = '<span style="color:#c0cad4;">&mdash;</span>'; qb_note = True
                netcell = '<span style="color:#c0cad4;">&mdash;</span>'
            H.append('<tr><td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td>'
                     '<td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td>'
                     '<td style="%s">%s</td><td style="%s">%s</td></tr>'
                     % (tdl, _esc(s["customer"]), tdl, _esc(s["no"]), tdl, created,
                        td, _money(s["net"]), td, _money(s["po_total"]), td, _money(pnl), td, _pct(pnl, s["net"]),
                        tdg, qbcell, tdg, netcell))
        if not msos:
            H.append('<tr><td colspan="9" style="%spadding:14px;color:#7a8a99;">No sales orders in %s %d.</td></tr>' % (tdl, MONTHS[m - 1], Y))
        H.append('<tr><td colspan="3" style="%s">Total</td>'
                 '<td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td><td style="%s">%s</td>'
                 '<td style="%s">%s</td><td style="%s">%s</td></tr></tbody></table>'
                 % (btl, bt, _money(dr), bt, _money(dc), bt, _money(dr - dc), bt, _pct(dr - dc, dr),
                    btg, _money(dqb), btg, _money(dnet)))
        H.append('</div>')
    H.append('<div style="font-size:11px;color:#888;margin-bottom:8px;">Green columns are from QuickBooks (matched by '
             'Customer + created date). QB Payment = amount received on the matching QuickBooks invoice. '
             'Net deposit = QB Payment minus processing fees (%.1f%%). '
             '&mdash; = no matching QB invoice%s. SO Amount is net of shipping.</div>'
             % (NET_FEE_RATE * 100, " (cache may need a refresh)" if qb_note else ""))

    H.append('</div>')
    return "".join(H)
