#!/usr/bin/env python3
"""
Customer Analysis — for each Independent Diagnostic Lab customer with orders this
year, build a Product x Month ordering matrix, procurement recommendations (table
+ data for a visual), and a ready-to-send customer-facing HTML email draft.

build_customer_analysis(vt) returns a dict embedded into the page; the page
renders the matrix / recommendations / visual and a "Create email" button that
opens the precomputed draft (it only creates the draft — the user sends it).
"""

import math
from datetime import datetime
from collections import defaultdict

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SKIP_ITEMS = ("shipping", "freight", "sales tax", "tax", "handling", "delivery fee")
LOGO = "https://jit4labs.github.io/customer-order-status/assets/JIT4LABS-Logo.png"


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


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _qstr(q):
    q = round(_f(q), 2)
    return str(int(q)) if q == int(q) else ("%g" % q)


def _money(x):
    return "${:,.0f}".format(round(_f(x)))


def _is_skip(name):
    n = (name or "").lower()
    if any(k in n for k in SKIP_ITEMS):
        return True
    if n.strip().replace(".", "").replace("%", "").isdigit():
        return True
    return False


def build_customer_analysis(vt):
    now = _pac_now()
    Y = now.year
    cm = now.month
    months = MONTHS[:cm]

    accts = vt.query_all("SELECT id, accountname, industry, email1 FROM Accounts")
    TARGET_INDUSTRIES = {"independent diagnostic lab", "online reseller"}
    idl = {a["id"]: {"name": a.get("accountname", ""), "email": (a.get("email1", "") or ""),
                     "industry": (a.get("industry", "") or "")}
           for a in accts if (a.get("industry", "") or "").strip().lower() in TARGET_INDUSTRIES}

    sos = vt.query_all(
        "SELECT id, salesorder_no, account_id, createdtime, sostatus FROM SalesOrder "
        "WHERE createdtime >= '%d-01-01' AND createdtime < '%d-01-01'" % (Y, Y + 1))
    idl_sos = [s for s in sos if s.get("account_id") in idl
               and (s.get("sostatus", "") or "").strip().lower() != "cancelled"]

    # per account -> product -> [qty per month]; and per account -> [orders per month]
    acct_prod = defaultdict(lambda: defaultdict(lambda: [0.0] * cm))
    acct_orders = defaultdict(lambda: [0] * cm)
    acct_spend = defaultdict(lambda: defaultdict(float))  # YTD spend per product (qty*listprice)
    for s in idl_sos:
        aid = s["account_id"]
        try:
            mo = int(str(s.get("createdtime", ""))[5:7])
        except Exception:
            continue
        if not (1 <= mo <= cm):
            continue
        acct_orders[aid][mo - 1] += 1
        detail = vt.retrieve_with_retry(s["id"], label="SO-CA")
        if not detail:
            continue
        for li in detail.get("LineItems", detail.get("lineItems", [])) or []:
            name = li.get("product_name", "") or li.get("productid_display", "")
            if not name or _is_skip(name):
                continue
            qty = _f(li.get("quantity", li.get("qty", 0)))
            acct_prod[aid][name][mo - 1] += qty
            acct_spend[aid][name] += qty * _f(li.get("listprice", li.get("price", 0)))

    customers = []
    for aid, info in idl.items():
        prods = acct_prod.get(aid)
        if not prods:
            continue
        active_months = sum(1 for x in acct_orders[aid] if x > 0)
        monthly_units = [0.0] * cm
        products = []
        for name, by_month in prods.items():
            total = sum(by_month)
            if total <= 0:
                continue
            for i in range(cm):
                monthly_units[i] += by_month[i]
            products.append({"name": name, "by_month": [round(x, 2) for x in by_month],
                             "total": round(total, 2), "spend": round(acct_spend[aid].get(name, 0.0), 2)})
        if not products:
            continue
        products.sort(key=lambda p: -p["total"])
        monthly_orders = acct_orders[aid]

        # Recommendations (procurement-focused)
        recs = []
        for p in products:
            mo_ordered = sum(1 for x in p["by_month"] if x > 0)
            # Avg/Mo is a 2026 YTD average: total units / months elapsed this year (Jan–current).
            avg = p["total"] / cm if cm else 0
            # Suggested Par = YTD monthly average + 50% safety buffer, rounded up (min 1).
            par = max(1, int(math.ceil(avg * 1.5))) if p["total"] > 0 else 0
            last = p["by_month"][-1]
            if avg and last > avg * 1.2:
                trend = "up"
            elif avg and 0 < last < avg * 0.8:
                trend = "down"
            elif last == 0 and p["total"] > 0:
                trend = "due"
            else:
                trend = "steady"
            regular = mo_ordered >= max(2, math.ceil(active_months / 2.0))
            if regular:
                sug = "Regular item — set a standing monthly order of ~%d units to avoid stockouts." % max(par, 1)
            elif trend == "due":
                sug = "Not ordered this month — consider a reorder (~%d units) to maintain stock." % max(par, 1)
            else:
                sug = "Occasional item — keep ~%d units on hand." % max(par, 1)
            recs.append({"product": p["name"], "months_ordered": mo_ordered,
                         "total": p["total"], "avg": round(avg, 1), "par": par, "trend": trend, "suggestion": sug})

        total_units = round(sum(monthly_units), 2)
        total_spend = round(sum(p.get("spend", 0.0) for p in products), 2)
        top = products[0]["name"] if products else ""
        overall = [
            "Across %d month(s) you've ordered %d distinct product(s), ~%s units total."
            % (active_months, len(products), _qstr(total_units)),
            "Your most-ordered item is %s — a standing order would guarantee availability and speed fulfillment." % top,
            "Consolidating into fewer, larger monthly orders can reduce shipping cost and handling.",
        ]

        email_subject = "Your %d Procurement Analysis & Recommendations — JIT4Labs" % Y
        email_html = _build_email_html(info["name"], Y, recs, overall, products, total_spend)
        email_doc = _build_email_doc(info["name"], info["email"], email_subject, email_html)

        customers.append({
            "name": info["name"], "email": info["email"], "account_id": aid,
            "industry": info.get("industry", ""), "total_spend": total_spend,
            "months": months,
            "products": products,
            "monthly_units": [round(x, 2) for x in monthly_units],
            "monthly_orders": monthly_orders,
            "recommendations": recs,
            "overall": overall,
            "total_units": total_units,
            "active_months": active_months,
            "email_subject": email_subject,
            "email_doc": email_doc,
        })

    customers.sort(key=lambda c: -c["total_units"])
    return {"year": Y, "months": months, "customers": customers}


def _wordmark(size=26):
    # Text-based logo (no image) so it always renders — email clients never block it.
    return ('<div style="font-family:\'Open Sans\',Arial,sans-serif;font-size:%dpx;font-weight:800;letter-spacing:.4px;">'
            '<span style="color:#101E3E;">JIT4</span><span style="color:#008080;">Labs</span></div>' % size)


def _spend_visual(products, total_spend, Y):
    """Email-safe spend breakdown: a 100%% horizontal stacked bar built from colored
    table cells (no images / SVG, nothing to download) + a color-matched legend."""
    palette = ['#1F4E79', '#008080', '#e67e22', '#2e7d32', '#8e44ad', '#c0392b']
    other_color = '#95a5a6'
    ps = sorted([p for p in products if _f(p.get('spend', 0)) > 0], key=lambda x: -x['spend'])
    if not ps:
        return ''
    segs = []
    for i, p in enumerate(ps[:6]):
        segs.append((p['name'], p['spend'], palette[i % len(palette)]))
    other = sum(p['spend'] for p in ps[6:])
    if other > 0:
        segs.append(('Other products', other, other_color))
    total = total_spend if total_spend > 0 else (sum(s[1] for s in segs) or 1)
    bar = ''
    for name, sp, col in segs:
        w = max(1, int(round(sp / total * 100)))
        bar += ('<td bgcolor="%s" style="background:%s;width:%d%%;font-size:1px;line-height:1px;">&nbsp;</td>'
                % (col, col, w))
    legend = ''
    for name, sp, col in segs:
        pct = (sp / total * 100) if total else 0
        # Swatch is a real <td bgcolor> cell (Outlook keeps cell bgcolor; it drops
        # background on empty <span>s, which made the colored bullets disappear on paste).
        legend += ('<tr>'
                   '<td width="16" bgcolor="%s" style="background:%s;width:16px;font-size:1px;line-height:1px;border-radius:2px;">&nbsp;</td>'
                   '<td style="padding:4px 4px 4px 8px;font-size:12px;color:#101E3E;">%s</td>'
                   '<td style="padding:4px 8px;font-size:12px;text-align:right;font-weight:700;color:#101E3E;">%s</td>'
                   '<td style="padding:4px 8px;font-size:12px;text-align:right;color:#666;">%.0f%%</td>'
                   '</tr>' % (col, col, _esc(name), _money(sp), pct))
    return (
        '<p style="margin:6px 0 4px 0;font-weight:700;color:#101E3E;">Your %d spend with JIT4Labs &amp; how it breaks down</p>'
        '<p style="margin:0 0 10px 0;font-size:22px;font-weight:800;color:#008080;">%s</p>'
        '<table width="100%%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;height:26px;margin-bottom:8px;"><tr>%s</tr></table>'
        '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-bottom:4px;">%s</table>'
        % (Y, _money(total_spend), bar, legend)
    )


def _build_email_html(name, Y, recs, overall, products, total_spend):
    """Customer-facing procurement recommendations email (branded JIT4Labs, no images)."""
    top = sorted(recs, key=lambda r: -r["total"])[:8]
    rows = ""
    for r in top:
        rows += ('<tr>'
                 '<td style="padding:8px 10px;border-bottom:1px solid #eee;">' + _esc(r["product"]) + '</td>'
                 '<td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:center;">' + str(r["months_ordered"]) + '</td>'
                 '<td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:center;">' + _qstr(r["total"]) + '</td>'
                 '<td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:center;">' + _qstr(r["avg"]) + '</td>'
                 '<td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:center;font-weight:700;color:#008080;">' + str(r["par"]) + '</td>'
                 '</tr>')
    bullets = "".join("<li style='margin:4px 0;'>" + _esc(b) + "</li>" for b in overall)
    footnote = ('<div style="font-size:11px;color:#888;margin-top:6px;">Months Ordered = number of ' + str(Y) +
                ' months with at least one order. Avg/Mo (YTD) = total ' + str(Y) + ' units &divide; months elapsed '
                'this year. Suggested Par = Avg/Mo (YTD) &times; 1.5 (safety buffer), rounded up.</div>')
    return (
        '<div style="font-family:\'Open Sans\',Arial,sans-serif;color:#101E3E;max-width:720px;margin:0 auto;background:#fff;">'
        '<div style="background:#fff;padding:20px 28px;border-bottom:3px solid #008080;">' + _wordmark(26) + '</div>'
        '<div style="padding:26px 28px;font-size:14px;line-height:1.6;">'
        '<p>Hi ' + _esc(name) + ' team,</p>'
        '<p>As part of our commitment to keeping you fully stocked, we reviewed your ' + str(Y) + ' ordering history '
        'with JIT4Labs and put together a short spend analysis plus a few procurement recommendations to make '
        'reordering easier and avoid stockouts.</p>'
        + _spend_visual(products, total_spend, Y) +
        '<p style="margin:18px 0 8px 0;font-weight:700;color:#101E3E;">Recommended monthly stocking levels</p>'
        '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
        '<thead><tr style="background:#101E3E;color:#fff;">'
        '<th style="padding:8px 10px;text-align:left;">Product</th>'
        '<th style="padding:8px 10px;">Months Ordered</th>'
        '<th style="padding:8px 10px;">YTD Units</th>'
        '<th style="padding:8px 10px;">Avg/Mo (YTD)</th>'
        '<th style="padding:8px 10px;">Suggested Par</th></tr></thead>'
        '<tbody>' + rows + '</tbody></table>'
        + footnote +
        '<p style="margin:18px 0 8px 0;font-weight:700;color:#101E3E;">Our recommendations</p>'
        '<ul style="padding-left:20px;margin:0;">' + bullets + '</ul>'
        '<p style="margin-top:20px;">We\'d be glad to set up a standing monthly order or a custom par-level plan so '
        'your key items are always on hand. Just reply to this email and we\'ll take care of it.</p>'
        '<p style="margin-top:18px;">Best regards,<br>The JIT4Labs Team</p>'
        '<p style="font-size:12px;color:#008080;margin-top:4px;">'
        '<a href="mailto:CustomerSupport@jit4you.com" style="color:#008080;text-decoration:none;">CustomerSupport@jit4you.com</a> '
        '&nbsp;&middot;&nbsp; (949) 396-9194</p>'
        '</div>'
        '<div style="background:#101E3E;color:rgba(255,255,255,.65);text-align:center;padding:16px;font-size:11px;">'
        '&copy; ' + str(Y) + ' JIT4You Inc. — All rights reserved.</div>'
        '</div>'
    )


def _build_email_doc(name, email, subject, email_html):
    """A self-contained draft-review document opened by the 'Create email' button.
    It only displays the draft (To/Subject + body) with a Copy button — no sending."""
    to = email or "(no email on file)"
    mailto = "mailto:%s?subject=%s" % (email, subject.replace(" ", "%20").replace("&", "%26"))
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Email draft — %s</title></head>'
        '<body style="margin:0;background:#eef1f5;font-family:Arial,sans-serif;">'
        '<div style="background:#0D2B45;color:#fff;padding:14px 20px;">'
        '<div style="font-size:13px;margin-bottom:3px;"><b>To:</b> %s</div>'
        '<div style="font-size:13px;margin-bottom:10px;"><b>Subject:</b> %s</div>'
        '<button id="cp" style="padding:7px 14px;border:none;border-radius:5px;background:#008080;color:#fff;font-weight:700;cursor:pointer;">Copy formatted email</button>'
        '<a href="%s" style="color:#7fd4d4;margin-left:14px;font-size:13px;">Open blank email to customer</a>'
        '<div style="margin-top:8px;font-size:12px;opacity:.9;">Click <b>Copy formatted email</b>, then in Outlook paste with <b>Ctrl/Cmd+V</b> (it pastes the rendered email, not code). This only creates the draft — you send it.</div>'
        '</div>'
        '<div style="padding:20px;"><div id="emailbody" style="max-width:760px;margin:0 auto;background:#fff;">%s</div></div>'
        '<script>(function(){var b=document.getElementById("cp"),body=document.getElementById("emailbody");'
        'function ok(){b.textContent="Copied \\u2713 \\u2014 paste into Outlook";}'
        'function fail(){b.textContent="Select the email and press Ctrl/Cmd+C";}'
        'function selCopy(){var r=document.createRange();r.selectNode(body);var s=window.getSelection();'
        's.removeAllRanges();s.addRange(r);try{var done=document.execCommand("copy");s.removeAllRanges();done?ok():fail();}catch(e){fail();}}'
        'b.addEventListener("click",function(){var html=body.innerHTML;'
        'if(window.ClipboardItem&&navigator.clipboard&&navigator.clipboard.write){try{'
        'var item=new ClipboardItem({"text/html":new Blob([html],{type:"text/html"}),"text/plain":new Blob([body.innerText],{type:"text/plain"})});'
        'navigator.clipboard.write([item]).then(ok,selCopy);}catch(e){selCopy();}}else{selCopy();}});})();'
        '</script></body></html>'
        % (_esc(name), _esc(to), _esc(subject), mailto, email_html)
    )
