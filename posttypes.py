#!/usr/bin/env python3
"""Stop guessing which plugin a venue runs -- ask its CMS to list them.

Every probe before this one guessed. `census.py` looked for an ICS feed the
site advertised in `<link rel=alternate>`, which most themes never emit, and
counted The Events Calendar by sniffing markup. `ical.py` guessed three archive
slugs. Both were asking "is it the plugin I have in mind?" and recording a
negative when the answer was "no, a different one".

Two endpoints answer the question directly, with no key and no advertisement:

  WordPress   /wp-json/wp/v2/types    every registered post type + its rest_base
  Drupal      /jsonapi                every node type as a JSON:API resource

That is a registry, not a guess. It names the endpoint holding the events
instead of making us invent its slug -- which matters, because the slugs are
nothing like guessable. Real ones found on NYC institutions: `walkingtours`,
`performancearchive`, `show-programs`, `lpr_events`, `ch_events`, `mec-events`,
`current-event`. No amount of slug-guessing reaches those.

The second half is the part that keeps the number honest. A registered post
type is NOT a readable feed: `/wp-json/wp/v2/types` lists types whether or not
they answer, so registry hits overstate. Every hit is therefore re-asked at
`/wp-json/wp/v2/{rest_base}?per_page=3` and only counts if it returns items --
the same trap `ical.py` documented, where an installed plugin served an empty
calendar and still looked like a source.

Even then there are two grades of hit, and conflating them would be the third
version of the same mistake:

  items only   title, link, body. `date` and `date_gmt` are the WordPress POST
               dates -- when the page was published, not when the event runs.
               Useless as an event feed on its own.
  items+meta   a plugin that registered its date meta in REST, so the real
               start time is there: `_EventStartDate`, `WooCommerceEventsDate`,
               `_eventorganiser_schedule_start_start`, `mec_start_date`.

Only the second is a feed. The first needs the detail page or the plugin's own
route -- e.g. The Events Calendar hides `_EventStartDate` from `wp/v2` but
serves the whole event at `/wp-json/tribe/events/v1/events`, which `--tribe`
re-tests on exactly the sites that turn out to run it.

    python3 posttypes.py                 # registry sweep, then verify
    python3 posttypes.py --tribe         # re-test tribe REST + ICS on tribe sites
    python3 posttypes.py --report        # re-print the tables, no network
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402
from census import load_existing  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))

# Post types worth a second look. Wide on purpose -- a false positive costs one
# request, a false negative costs a venue.
EVENTISH = re.compile(
    r"event|screening|program|performance|show|calendar|class|exhibit|concert|"
    r"workshop|tour|product|ticket", re.I)

# Registered by plugins that are not calendars. `jp_*` is Jetpack's internal
# bookkeeping, `tec_calendar_embed` is a shortcode holder, `tribe_*_tickets`
# are ticket products attached to an event rather than the event itself.
NOISE = {
    "jp_pay_product", "jp_act_log_event", "jp_product_search",
    "tec_calendar_embed", "tribe_rsvp_tickets", "tribe_tpp_tickets",
    "single-class-cancell", "dt_slideshow", "block-showcase",
}

# Meta keys that carry a real event start, by plugin. Presence of any one of
# these is what separates a feed from a list of web pages.
DATE_META = re.compile(r"date|start|time|when", re.I)
POST_DATE = {"date", "date_gmt", "modified", "modified_gmt"}


def registry(host, delay):
    """Ask one site what post/node types it has. Never raises."""
    origin = "https://" + host
    out = {"host": host, "wp_types": None, "drupal_types": None, "error": None}

    r = http.get_full(origin + "/wp-json/wp/v2/types", delay=delay,
                      accept="application/json", max_bytes=2_000_000)
    if r.get("status") == 200 and "json" in (r.get("ctype") or ""):
        try:
            d = json.loads(r["body"])
            if isinstance(d, dict):
                out["wp_types"] = {
                    k: v.get("rest_base") for k, v in d.items()
                    if isinstance(v, dict) and v.get("rest_base")
                    and EVENTISH.search(k) and k not in NOISE}
        except ValueError:
            out["error"] = "wp: not json"

    if not out["wp_types"]:
        r = http.get_full(origin + "/jsonapi", delay=delay,
                          accept="application/vnd.api+json,application/json",
                          max_bytes=2_000_000)
        if r.get("status") == 200 and "json" in (r.get("ctype") or ""):
            try:
                links = json.loads(r["body"]).get("links", {})
                out["drupal_types"] = sorted(
                    k for k in links
                    if k.startswith("node--") and EVENTISH.search(k))
            except ValueError:
                out["error"] = "drupal: not json"
    return out


def verify(host, types, delay):
    """Re-ask each registered type for actual rows. Registry hits overstate."""
    origin = "https://" + host
    open_ = []
    for slug, base in sorted(types.items()):
        r = http.get_full(f"{origin}/wp-json/wp/v2/{base}?per_page=3",
                          delay=delay, accept="application/json",
                          max_bytes=1_500_000)
        if r.get("status") != 200:
            continue
        try:
            rows = json.loads(r["body"])
        except ValueError:
            continue
        if not isinstance(rows, list) or not rows:
            continue
        sample = rows[0]
        meta = sample.get("meta") or {}
        # A real event date can only come from plugin meta. The top-level
        # `date` is when the post was published.
        event_dates = sorted(k for k in meta
                             if DATE_META.search(k) and k not in POST_DATE)
        # Header casing is the server's choice; HTTP names are case-insensitive.
        total = next((v for k, v in (r.get("headers") or {}).items()
                      if k.lower() == "x-wp-total"), None)
        open_.append({
            "slug": slug, "base": base,
            "total": total,
            "returned": len(rows),
            "event_date_meta": event_dates,
            "sample_title": (sample.get("title") or {}).get("rendered", "")[:80],
            "sample_link": sample.get("link", ""),
        })
    return open_


def tribe(host, delay):
    """The Events Calendar keeps its dates off wp/v2 but serves its own route."""
    origin = "https://" + host
    out = {"host": host, "rest": None, "ics": None}

    r = http.get_full(f"{origin}/wp-json/tribe/events/v1/events?per_page=2",
                      delay=delay, accept="application/json")
    if r.get("status") == 200 and "json" in (r.get("ctype") or ""):
        try:
            d = json.loads(r["body"])
            out["rest"] = d.get("total", len(d.get("events", [])))
        except ValueError:
            pass

    for path in ("/events/?ical=1", "/?post_type=tribe_events&ical=1"):
        r = http.get_full(origin + path, delay=delay, accept="text/calendar")
        body = r.get("body") or b""
        if r.get("status") == 200 and body.lstrip()[:15] == b"BEGIN:VCALENDAR":
            out["ics"] = {"url": origin + path,
                          "vevents": body.count(b"BEGIN:VEVENT")}
            break
    return out


def out_path(city):
    return os.path.join(ROOT, "data", city, "posttypes.json")


def load(city):
    with open(out_path(city)) as f:
        return json.load(f)


def save(city, payload):
    os.makedirs(os.path.dirname(out_path(city)), exist_ok=True)
    with open(out_path(city), "w") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")


def report(payload):
    sites = payload["sites"]
    reg = [s for s in sites if s.get("wp_types") or s.get("drupal_types")]
    live = [s for s in sites if s.get("open")]
    feeds = [s for s in live
             if any(o["event_date_meta"] for o in s["open"])]

    print(f"\nprobed              {len(sites)}")
    print(f"registry hit        {len(reg)}   (a type exists)")
    print(f"endpoint returns    {len(live)}   (and it answers with rows)")
    print(f"carries event date  {len(feeds)}   (and the date is in the payload)")
    print("\nThe last number is the only one that is a feed.\n")

    for s in sorted(feeds, key=lambda s: s["name"]):
        for o in s["open"]:
            if o["event_date_meta"]:
                print(f"  {s['name'][:38]:40} {o['base']:22} "
                      f"n={str(o['total'] or o['returned']):>5}  "
                      f"{o['event_date_meta'][:2]}")

    t = payload.get("tribe") or []
    if t:
        rest = [x for x in t if isinstance(x["rest"], int) and x["rest"] > 0]
        ics = [x for x in t if x["ics"] and x["ics"]["vevents"] > 0]
        print(f"\nThe Events Calendar, on the {len(t)} sites that actually run it:")
        print(f"  tribe REST answering   {len(rest)}/{len(t)}")
        print(f"  ICS with >=1 VEVENT    {len(ics)}/{len(t)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--delay", type=float, default=1.0, help="per-host seconds")
    ap.add_argument("--limit", type=int, help="probe only the first N")
    ap.add_argument("--tribe", action="store_true",
                    help="also re-test tribe REST + ICS where tribe_events exists")
    ap.add_argument("--report", action="store_true",
                    help="re-print the tables, no network")
    args = ap.parse_args()

    if args.report:
        report(load(args.city))
        return

    sites = load_existing(args.city)
    live = [s for s in sites
            if s.get("status") and 200 <= s["status"] < 400
            and http.allowed("https://" + s["host"])]
    if args.limit:
        live = live[:args.limit]
    print(f"probing {len(live)} readable sites", file=sys.stderr)

    rows = []
    for i, site in enumerate(live, 1):
        rec = registry(site["host"], args.delay)
        rec["name"] = site["name"]
        rec["open"] = (verify(site["host"], rec["wp_types"], args.delay)
                       if rec["wp_types"] else [])
        rows.append(rec)
        if i % 25 == 0:
            print(f"  {i}/{len(live)}", file=sys.stderr)

    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "probed": len(rows), "sites": rows, "tribe": []}

    if args.tribe:
        hosts = [r for r in rows
                 if r["wp_types"] and "tribe_events" in r["wp_types"]]
        print(f"re-testing tribe on {len(hosts)} sites", file=sys.stderr)
        for r in hosts:
            t = tribe(r["host"], args.delay)
            t["name"] = r["name"]
            payload["tribe"].append(t)

    save(args.city, payload)
    report(payload)


if __name__ == "__main__":
    main()
