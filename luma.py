#!/usr/bin/env python3
"""Discover -- which Luma calendars serve NYC, and can we subscribe to them?

Luma is the one aggregator here covering a category the index has none of:
tech meetups, run clubs, craft circles, reading groups. None of it appears in
NYC Open Data, none of it sells through Ticketmaster, and almost none of it has
a website with a feed. It is also, unusually, readable without any of the
compromises the other aggregators demand.

The route in is the Subscribe button. Every Luma calendar exposes

    https://api.lu.ma/ics/get?entity=calendar&id=<cal-id>

as real `text/calendar`. That is a published, user-facing feed being used as a
feed -- not an undocumented internal endpoint, and not a scrape. It is the same
distinction that ruled Eventbrite out: there, the only paths left were an
ownership-scoped API we have no standing in and an internal search endpoint the
terms forbid. Here the front door is open.

What is genuinely limited is DISCOVERY. There is no city-wide feed --
`entity=discover-place&id=nyc` returns 400 -- and no sitemap. The public
`/nyc` page embeds exactly 20 events in `__NEXT_DATA__`, one calendar each, and
the category variants (`/nyc/tech`, `?k=tech`) are filtered client-side and
return the same 20. So one page load buys 20 calendars and no more.

The answer is to accumulate rather than paginate. The 20 are scored by
popularity and rotate, so the registry is MERGED on every run and grows over
time. A calendar that has stopped publishing is kept but marked, because
disappearing from a discovery page is not evidence a calendar is dead.

    python3 luma.py                 # discover, merge into the registry
    python3 luma.py --verify        # re-check every known calendar's feed
    python3 luma.py --report        # re-print, no network
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http, ics  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
ICS_URL = "https://api.lu.ma/ics/get?entity=calendar&id={}"
DISCOVERY = "https://luma.com/{}"


def scrape_calendars(place, delay):
    """Pull the calendars behind the events on a Luma discovery page."""
    r = http.get_full(DISCOVERY.format(place), delay=delay, max_bytes=4_000_000)
    if not r["body"]:
        print(f"  discovery page unreachable: {r['status']} {r['error']}")
        return {}
    m = NEXT_DATA.search(r["body"].decode("utf-8", "replace"))
    if not m:
        print("  no __NEXT_DATA__ on the discovery page -- layout changed")
        return {}
    try:
        data = json.loads(m.group(1))["props"]["pageProps"]["initialData"]["data"]
    except (KeyError, ValueError) as e:
        print(f"  unexpected discovery payload: {e}")
        return {}

    found = {}
    for item in data.get("events") or []:
        cal = item.get("calendar") or {}
        cid = cal.get("api_id")
        if cid:
            found[cid] = cal.get("name") or cid
    return found


def check_feed(cal_id, delay, horizon_days=30):
    """Fetch a calendar's ICS and report what is actually in it."""
    r = http.get_full(ICS_URL.format(cal_id), delay=delay,
                      accept="text/calendar,*/*;q=0.8", check_robots=False,
                      max_bytes=8_000_000)
    if not r["body"]:
        return {"ok": False, "status": r["status"], "total": 0, "upcoming": 0}
    text = r["body"].decode("utf-8", "replace")
    if "BEGIN:VCALENDAR" not in text[:2000]:
        return {"ok": False, "status": r["status"], "total": 0, "upcoming": 0}
    items = ics.events(text)
    return {"ok": True, "status": r["status"], "total": len(items),
            "upcoming": len(ics.upcoming(items, horizon_days=horizon_days))}


# ---------------------------------------------------------------- io

def registry_path(city):
    return os.path.join(ROOT, "cities", city, "luma.json")


def load(city):
    try:
        with open(registry_path(city)) as f:
            return json.load(f)
    except Exception:
        return {"city": city, "calendars": []}


def save(city, payload):
    os.makedirs(os.path.dirname(registry_path(city)), exist_ok=True)
    with open(registry_path(city), "w") as f:
        json.dump(payload, f, indent=1)


def report(payload):
    cals = payload.get("calendars") or []
    live = [c for c in cals if c.get("upcoming")]
    total = sum(c.get("upcoming") or 0 for c in cals)
    print(f"\n{len(cals)} calendars known, {len(live)} with anything upcoming")
    print(f"upcoming events reachable: {total}\n")
    for c in sorted(cals, key=lambda c: -(c.get("upcoming") or 0))[:20]:
        print(f"  {str(c.get('upcoming') or 0):>4} upcoming  "
              f"{(c.get('name') or '')[:38]:<38} {c['id']}")
    print()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--place", default="nyc", help="Luma discovery slug")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--verify", action="store_true",
                    help="re-check every known calendar, not just new ones")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        report(load(args.city))
        return 0

    payload = load(args.city)
    known = {c["id"]: c for c in payload.get("calendars") or []}

    found = scrape_calendars(args.place, args.delay)
    new = [cid for cid in found if cid not in known]
    print(f"discovery page: {len(found)} calendars, {len(new)} new "
          f"({len(known)} already known)")

    # Only new calendars are fetched by default. The discovery page rotates, so
    # a daily run re-surfaces the same popular calendars, and re-fetching all of
    # them every time would spend the whole request budget re-learning what the
    # registry already says.
    targets = list(found) if args.verify else new
    for i, cid in enumerate(targets, 1):
        info = check_feed(cid, args.delay, args.horizon)
        rec = known.get(cid, {"id": cid})
        rec["name"] = found.get(cid) or rec.get("name") or cid
        rec.update({"ok": info["ok"], "total": info["total"],
                    "upcoming": info["upcoming"],
                    "checked_at": datetime.now(timezone.utc).isoformat()})
        known[cid] = rec
        mark = f"{info['upcoming']:>4} upcoming" if info["ok"] else "no feed"
        print(f"  [{i}/{len(targets)}] {mark:<16} {rec['name'][:44]}")

    payload = {"city": args.city, "place": args.place,
               "generated_at": datetime.now(timezone.utc).isoformat(),
               "calendars": sorted(known.values(),
                                   key=lambda c: -(c.get("upcoming") or 0))}
    save(args.city, payload)
    print(f"\nwrote {registry_path(args.city)}")
    report(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
