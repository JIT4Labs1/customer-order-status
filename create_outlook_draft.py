#!/usr/bin/env python3
"""Create an UNSENT Outlook draft (customersupport mailbox) with a customer's open-order status email.
Same layout as the dashboard's Customer Open SO's email (logo header, Created/Today/Open-days per SO,
no price column). Auth: Microsoft Graph client-credentials (Mail.ReadWrite application permission).
Env: MS_TENANT_ID MS_CLIENT_ID MS_CLIENT_SECRET, CUSTOMER (exact name), optional MS_MAILBOX, DRAFT_TO."""
import os, sys, json, html, datetime, urllib.request, urllib.parse

def esc(s): return html.escape(str(s if s is not None else ''), quote=True)
def fq(q):
    q = float(q or 0); return str(int(q)) if q == int(q) else ('%.2f' % q).rstrip('0').rstrip('.')
def fdate(s):
    try: return datetime.date.fromisoformat(str(s)[:10]).strftime('%b %d, %Y')
    except Exception: return '—' if not s else str(s)

def build(c, eta, today):
    groups, order = {}, []
    for r in c.get('rows', []):
        so = r.get('so_num') or '(no SO)'
        if so not in groups:
            groups[so] = dict(so=so, status=r.get('so_status'), date=r.get('order_date'), items=[], total=0); order.append(so)
        g = groups[so]; g['items'].append(r); g['total'] = max(g['total'], r.get('so_total_lines') or 0)
        if r.get('order_date') and (not g['date'] or r['order_date'] < g['date']): g['date'] = r['order_date']
    order.sort(key=lambda k: (groups[k]['date'] or '', k))
    td = 'padding:8px 10px;border-bottom:1px solid #e3e7ee;'; tdc = td + 'white-space:nowrap;'
    thb = 'background:#101E3E;color:#ffffff;padding:8px 10px;font-size:11px;letter-spacing:.4px;text-transform:uppercase;white-space:nowrap;'
    F = 'font:13px/1.5 Arial, Helvetica, sans-serif;'
    sections = ''
    for k in order:
        g = groups[k]; its = sorted(g['items'], key=lambda r: str(r.get('product')))
        try: od = (today - datetime.date.fromisoformat(str(g['date'])[:10])).days; od = max(od, 0)
        except Exception: od = '—'
        rows = ''
        for r in its:
            e = (eta.get('%s|%s' % (r.get('so_num') or '', r.get('sku') or r.get('product') or '')) or {}).get('d') or r.get('eta') or ''
            rows += ('<tr><td align="left" valign="top" style="%s">%s</td>'
                     '<td align="center" valign="top" style="%s">%s</td><td align="center" valign="top" style="%s">%s</td>'
                     '<td align="center" valign="top" style="%sfont-weight:700;color:#008080;">%s</td>'
                     '<td align="left" valign="top" style="%s">%s</td></tr>') % (
                td, esc(r.get('product')), tdc, fq(r.get('ordered_qty')), tdc, fq(r.get('delivered_qty')),
                tdc, fq(r.get('open_qty')), tdc, fdate(e) if e else '&mdash;')
        n = len(g['items'])
        summ = ('Lines on original order: <b>%d</b> &nbsp;&middot;&nbsp; Fulfilled: <b>%d</b> &nbsp;&middot;&nbsp; Still to fulfill: <b>%d</b>' % (g['total'], max(0, g['total'] - n), n)) if g['total'] > 0 else 'Lines still to fulfill: <b>%d</b>' % n
        sections += ('<div style="margin:0 0 16px;"><p style="margin:0 0 4px;font-weight:700;color:#101E3E;">SO %s &middot; %s</p>'
            '<p style="margin:0 0 4px;font-size:12.5px;color:#101E3E;">Created: <b>%s</b> &nbsp;&middot;&nbsp; Today: <b>%s</b> &nbsp;&middot;&nbsp; Open: <b style="color:#008080;">%s days</b></p>'
            '<p style="margin:0 0 6px;font-size:12.5px;color:#6b7280;">%s</p>'
            '<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%%;%scolor:#101E3E;">'
            '<tr><th align="left" style="%s">Product</th><th align="center" style="%s">Ordered</th><th align="center" style="%s">Delivered</th>'
            '<th align="center" style="%s">Open</th><th align="left" style="%s">ETA</th></tr>%s</table></div>') % (
            esc(g['so']), esc(g['status'] or ''), fdate(g['date']), today.strftime('%b %d, %Y'), od, summ, F, thb, thb, thb, thb, thb, rows)
    if not order: sections = '<p style="margin:0 0 12px">No open orders found.</p>'
    return ('<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f4f6f9"><tr><td align="center" style="padding:18px 10px">'
      '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:760px;width:100%;background:#fff;border:1px solid #e3e7ee">'
      '<tr><td style="padding:18px 24px;border-bottom:3px solid #008080"><a href="https://jit4labs.com" target="_blank" style="text-decoration:none;color:#101E3E">'
      '<img src="https://jit4labs.com/cdn/shop/files/JIT4LABS-Logo.png" alt="JIT4LABS" width="175" height="30" border="0" style="display:block;width:175px;max-width:175px;height:30px;border:0;outline:none;text-decoration:none;font:bold 22px Arial, Helvetica, sans-serif;color:#101E3E;line-height:30px"></a></td></tr>'
      '<tr><td style="padding:24px;font:14px/1.6 Arial, Helvetica, sans-serif;color:#101E3E"><p style="margin:0 0 12px">Hello,</p>'
      '<p style="margin:0 0 16px">Please find below the current status of your open orders with JIT4Labs as of __DATE__. Quantities reflect the latest fulfillment status.</p>__SECTIONS__'
      '<p style="margin:0 0 16px;font-size:12px;color:#6b7280">If anything looks different from your records, reply and we will sort it out.</p>'
      '<p style="margin:18px 0 0">Thank you,<br><b>JIT4Labs team</b></p></td></tr>'
      '<tr><td style="padding:14px 24px;border-top:3px solid #008080;font:12px/1.6 Arial, Helvetica, sans-serif;color:#6b7280">JIT4Labs &middot; <a href="mailto:CustomerSupport@jit4you.com" style="color:#008080;text-decoration:none">CustomerSupport@jit4you.com</a> &middot; <a href="tel:+18882422301" style="color:#008080;text-decoration:none">(888) 242-2301</a> &middot; <a href="https://jit4labs.com" style="color:#008080;text-decoration:none">jit4labs.com</a></td></tr>'
      '</table></td></tr></table>').replace('__DATE__', today.strftime('%B %d, %Y')).replace('__SECTIONS__', sections)

def post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read().decode() or '{}')

def main():
    cust = os.environ.get('CUSTOMER', '').strip()
    if not cust: sys.exit('CUSTOMER not set')
    d = json.load(open('open-orders-data.json'))
    c = next((x for x in d.get('customers', []) if x.get('name', '').strip().lower() == cust.lower()), None)
    if not c: sys.exit('Customer not found: %s' % cust)
    try: eta = json.load(open('so-eta.json'))
    except Exception: eta = {}
    body = build(c, eta, datetime.date.today())
    tenant, cid, sec = (os.environ.get(k) for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET'))
    if not (tenant and cid and sec): sys.exit('Missing MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET repo secrets')
    tok = post('https://login.microsoftonline.com/%s/oauth2/v2.0/token' % tenant, urllib.parse.urlencode(
        dict(client_id=cid, client_secret=sec, scope='https://graph.microsoft.com/.default', grant_type='client_credentials')).encode(),
        {'Content-Type': 'application/x-www-form-urlencoded'})['access_token']
    box = os.environ.get('MS_MAILBOX', 'CustomerSupport@jit4you.com')
    msg = dict(subject='JIT4Labs - Open order status (%s)' % cust, body=dict(contentType='HTML', content=body))
    to = os.environ.get('DRAFT_TO', '').strip()
    if to: msg['toRecipients'] = [dict(emailAddress=dict(address=a.strip())) for a in to.split(',') if a.strip()]
    r = post('https://graph.microsoft.com/v1.0/users/%s/messages' % urllib.parse.quote(box), json.dumps(msg).encode(),
             {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    print('Draft created in %s: %s (id %s...)' % (box, r.get('subject'), str(r.get('id'))[:12]))
main()
