#!/usr/bin/env python3
"""
PART 1 refresh via the GA4 Data API (replaces the Supermetrics AW + GAWA pulls).

Google Ads is linked to GA4, so campaign SPEND / CLICKS / IMPRESSIONS come straight from
GA4's advertiser* metrics (identical to Google Ads). Conversions / revenue / ROAS here reflect
GA4 last-click attribution (see note in the file), which is why ad revenue looks lower than the
Google-Ads console credits — checkout completes on Shopify and re-attributes to Direct/Organic.

Rebuilds google-ads-data.json "intervals" + "journey"; preserves account/currency/ga4_property/
note_click_trails/defs. Campaign status/type/start_date are carried over from the previous file by
name (GA4 doesn't expose them). Then publishes.
"""
import os, json, datetime, subprocess
from ga4_client import run_report, _token, named_range

HERE = os.path.dirname(os.path.abspath(__file__))
PID = "433514600"
F = os.path.join(HERE, "google-ads-data.json")
WINDOWS = ["today", "last_7_days", "last_30_days", "this_month", "last_month", "this_year"]

def num(x):
    try:
        f = float(x); return int(f) if f == int(f) else f
    except Exception:
        return 0
def r2(x): return round(x, 2)
def r3(x): return round(x, 3)
def r4(x): return round(x, 4)

def month_label(mo): return datetime.date(2026, mo, 1).strftime("%B")

def guess_type(name):
    n = (name or "").lower()
    if "search" in n: return "Search"
    if "shopping" in n: return "Shopping"
    return ""

def main():
    today = datetime.date.today()
    g = json.load(open(F))
    # preserve prior campaign metadata (status/type/start_date) keyed by name
    prior = {}
    for iv in g.get("intervals", []):
        for c in iv.get("campaigns", []):
            prior.setdefault(c["name"], {}) .update(
                {k: c.get(k) for k in ("status", "type", "start_date") if c.get(k) not in (None, "")})

    labels = {
        "today": "Today", "last_7_days": "Last 7 days", "last_30_days": "Last 30 days",
        "this_month": f"This month ({month_label(today.month)})",
        "last_month": f"Last month ({month_label((today.replace(day=1)-datetime.timedelta(days=1)).month)})",
        "this_year": f"{today.year} YTD",
    }
    tok = _token()
    KEEP_EVENTS = ["view_item", "add_to_cart", "begin_checkout", "purchase", "generate_lead"]

    intervals = []
    journey = {}
    for w in WINDOWS:
        dr = ("named", w)
        # ---- campaign KPIs (ads-scoped: spend/clicks/impr from Google Ads via GA4) ----
        try:
            rows = run_report(PID, ["sessionGoogleAdsCampaignName"],
                ["advertiserAdCost", "advertiserAdClicks", "advertiserAdImpressions",
                 "conversions", "totalRevenue", "returnOnAdSpend"], dr, token=tok, limit=100)
        except Exception as e:
            rows = []; print(f"  [warn] campaigns {w}: {e}")
        # per-campaign event counts (page_view / add_to_cart / purchase) to split the Conv. column.
        # SESSION-scoped campaign dim so page_view/add_to_cart/purchase (which don't carry the
        # event-scoped googleAdsCampaignName) inherit the session's Google Ads campaign.
        try:
            evc = run_report(PID, ["sessionGoogleAdsCampaignName", "eventName"], ["eventCount"],
                             dr, token=tok, limit=1000)
        except Exception as e:
            evc = []; print(f"  [warn] campaign events {w}: {e}")
        ev_by_camp = {}
        for r in evc:
            nm = r.get("sessionGoogleAdsCampaignName") or ""
            if nm in ("", "(not set)"): continue
            ev_by_camp.setdefault(nm, {})[r.get("eventName") or ""] = num(r.get("eventCount"))
        camps = []
        for r in rows:
            name = r.get("sessionGoogleAdsCampaignName") or ""
            if name in ("", "(not set)"): continue
            clicks = num(r.get("advertiserAdClicks")); impr = num(r.get("advertiserAdImpressions"))
            cost = num(r.get("advertiserAdCost")); conv = num(r.get("conversions"))
            cval = num(r.get("totalRevenue")); roas = num(r.get("returnOnAdSpend"))
            if not (impr > 0 or cost > 0 or clicks > 0): continue
            meta = prior.get(name, {})
            camps.append({
                "name": name, "status": meta.get("status", ""),
                "type": meta.get("type") or guess_type(name),
                "clicks": clicks, "impressions": impr,
                "ctr": r4(clicks / impr) if impr else 0,
                "cpc": r4(cost / clicks) if clicks else 0,
                "cost": r2(cost), "conversions": conv, "conv_value": r2(cval),
                "page_views": ev_by_camp.get(name, {}).get("page_view", 0),
                "add_to_cart": ev_by_camp.get(name, {}).get("add_to_cart", 0),
                "purchases": ev_by_camp.get(name, {}).get("purchase", 0),
                "roas": r4(roas), "start_date": meta.get("start_date", ""),
            })
        camps.sort(key=lambda c: c["cost"], reverse=True)
        intervals.append({"id": w, "label": labels[w], "campaigns": camps})

        # ---- journey: landing pages by campaign ----
        try:
            lp = run_report(PID, ["sessionGoogleAdsCampaignName", "landingPage"],
                ["sessions", "engagedSessions", "screenPageViewsPerSession", "bounceRate", "conversions"],
                dr, token=tok, dimension_filter_contains=("sessionSourceMedium", "cpc"), limit=200)
        except Exception as e:
            lp = []; print(f"  [warn] landing {w}: {e}")
        by_camp = {}
        for r in lp:
            camp = r.get("sessionGoogleAdsCampaignName") or "(not set)"
            path = r.get("landingPage") or "(not set)"
            by_camp.setdefault(camp, []).append({
                "path": path, "sessions": num(r.get("sessions")),
                "engaged": num(r.get("engagedSessions")),
                "pages_per_session": r2(num(r.get("screenPageViewsPerSession"))),
                "bounce": r3(num(r.get("bounceRate"))), "conversions": num(r.get("conversions"))})
        campaigns = []; tot_s = tot_e = tot_c = 0; tot_bw = tot_pw = 0.0
        for camp, lps in by_camp.items():
            lps_sorted = sorted(lps, key=lambda x: x["sessions"], reverse=True)
            cs = sum(l["sessions"] for l in lps); ce = sum(l["engaged"] for l in lps)
            cc = sum(l["conversions"] for l in lps)
            campaigns.append({"campaign": camp, "landing_pages": lps_sorted[:8],
                "totals": {"sessions": cs, "engaged": ce, "conversions": cc}})
            tot_s += cs; tot_e += ce; tot_c += cc
            for l in lps:
                tot_bw += l["bounce"] * l["sessions"]; tot_pw += l["pages_per_session"] * l["sessions"]
        campaigns.sort(key=lambda c: c["totals"]["sessions"], reverse=True)
        summary = {"sessions": tot_s, "engaged": tot_e, "conversions": tot_c,
            "bounce": r3(tot_bw / tot_s) if tot_s else 0,
            "pages_per_session": r2(tot_pw / tot_s) if tot_s else 0,
            "engagement_rate": r3(tot_e / tot_s) if tot_s else 0}
        # ---- events ----
        try:
            ev = run_report(PID, ["eventName"], ["eventCount", "conversions"], dr, token=tok,
                dimension_filter_contains=("sessionSourceMedium", "cpc"), limit=50)
        except Exception as e:
            ev = []; print(f"  [warn] events {w}: {e}")
        evd = {r.get("eventName"): (num(r.get("eventCount")), num(r.get("conversions"))) for r in ev}
        events = []
        for e in KEEP_EVENTS:
            if e in evd:
                cnt, conv = evd[e]
                events.append({"event": e, "count": cnt, "conversions": conv, "is_key": conv > 0})
        journey[w] = {"summary": summary, "campaigns": campaigns, "events": events}

    g["intervals"] = intervals
    g["journey"] = journey
    g["pulled_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    g["source"] = "GA4 Data API (Google Ads linked); spend/clicks/impressions = Google Ads; conversions/revenue/ROAS = GA4 last-click"
    json.dump(g, open(F, "w"), indent=2)

    ytd = intervals[-1]["campaigns"]
    print("YTD clicks:", sum(c["clicks"] for c in ytd),
          "| spend:", round(sum(c["cost"] for c in ytd), 2),
          "| impressions:", sum(c["impressions"] for c in ytd),
          "| conv:", sum(c["conversions"] for c in ytd))
    print("intervals:", [(i["id"], len(i["campaigns"])) for i in intervals])
    if os.environ.get("SKIP_PUBLISH"):
        print("SKIP_PUBLISH set — wrote google-ads-data.json, deferring push to batched publish")
    else:
        rc = subprocess.run(["python3", os.path.join(HERE, "publish_google_ads_data.py"), "google-ads-data.json"], cwd=HERE)
        print("publish exit:", rc.returncode)

if __name__ == "__main__":
    main()
