#!/usr/bin/env python3
"""Discover -- which Squarespace venues actually have an events collection?

The census found 106 venues where `?format=json` returns JSON (15% of readable,
larger than ICS, JSON-LD Event and Tribe combined). That is the biggest
structured surface on the venue side, and it is the reason this adapter exists.

But `squarespace_json: true` was measured against whatever page the census
happened to be holding -- usually the homepage. It means "this site serves JSON",
not "this site has events". Treating the two as the same is the exact mistake
that made ICS look like a 4% source when it was a 3% one, so discovery is a
measured step here rather than an assumption, and it writes down its own yield.

Two things make finding the events page harder than it looks:

  * The slug is arbitrary. Fraunces Tavern publishes at `/events-calendar`,
    which the census's `EVENT_PATH` regex misses because it anchors on a `/` or
    `?` straight after "events". Real slugs seen here include `events-calendar`,
    `upcoming-events` and `calendar-1`. So segments are matched on substring,
    and the sitemap -- which every Squarespace site serves -- is what enumerates
    them.

  * The events are not in `items`. For a collection whose `typeName` is
    `events`, `items` comes back empty and the records live in `upcoming` and
    `past`. A parser written against `items` finds nothing and reports it as a
    site with no events, which is indistinguishable from the truth unless you
    look. Only `upcoming` is kept -- `past` is a backfill question, not a
    listings one.

What comes back per event is the richest shape in the project so far: title,
start and end as epoch milliseconds, a relative `fullUrl`, an HTML `excerpt`,
an image `assetUrl`, and a `location` block carrying `mapLat`/`mapLng` plus a
street address. Coordinates and a ticket URL are exactly the two fields the
shipped index is thinnest on.

Discovery is separated from fetching on purpose. Slugs change on the scale of a
site redesign, so the registry it writes is committed and read by `fetch.py` on
every run, the same bargain as `neighborhoods.geojson`.

    python3 squarespace.py                  # discover, write the registry
    python3 squarespace.py --limit 20       # a taste first
    python3 squarespace.py --report         # re-print, no network
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402
from census import is_blocked, load_existing  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
JSON_ACCEPT = "application/json,text/javascript;q=0.9,*/*;q=0.8"

# Substring, not a bounded path segment. `events-calendar` and `upcoming-events`
# are both real slugs here and both fail a regex that demands a delimiter after
# the keyword -- which is how the census came to record these sites as having no
# event listing at all.
EVENTISH = re.compile(
    r"event|calendar|program|exhibit|whats-?on|show|performance|screening"
    r"|workshop|class|lecture|talk|concert|tour|festival|film", re.I)

# Tried when a site has no sitemap, or one with nothing event-shaped in it.
FALLBACK_SLUGS = ("events", "calendar", "events-calendar", "upcoming-events",
                  "programs", "whats-on")

MAX_CANDIDATES = 3
LOC = re.compile(r"<loc>([^<]+)</loc>", re.I)


def origin_of(rec):
    base = rec.get("final_url") or rec["url"]
    p = urllib.parse.urlsplit(base)
    return f"{p.scheme}://{p.netloc}"


def sitemap_segments(origin, delay):
    """Top-level path segments from /sitemap.xml, event-ish ones first.

    Ranked by how many URLs sit under them: a collection with 364 entries is a
    calendar, a one-page section that merely mentions "tour" is not.
    """
    r = http.get_full(f"{origin}/sitemap.xml", delay=delay,
                      accept="application/xml,text/xml,*/*;q=0.8")
    if not r["body"]:
        return []
    groups = {}
    for u in LOC.findall(r["body"].decode("utf-8", "replace")):
        segs = urllib.parse.urlsplit(u).path.strip("/").split("/")
        if segs and segs[0]:
            groups.setdefault(segs[0], set()).add(u)
    ranked = sorted(((len(v), k) for k, v in groups.items() if EVENTISH.search(k)),
                    reverse=True)
    return [seg for _, seg in ranked]


def read_collection(url, delay):
    """Fetch a page as JSON and report it only if it is an events collection."""
    r = http.get_full(url, delay=delay, accept=JSON_ACCEPT)
    if not r["body"] or "json" not in r["ctype"]:
        return None
    try:
        d = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception:
        return None
    coll = d.get("collection") or {}
    if coll.get("typeName") != "events":
        return None
    return {"url": r["url"], "title": coll.get("title"),
            "collection_id": coll.get("id"),
            "upcoming": len(d.get("upcoming") or []),
            "past": len(d.get("past") or [])}


def discover(rec, delay):
    """Up to five requests: one sitemap, then the best few candidate slugs."""
    origin = origin_of(rec)
    out = {"host": rec["host"], "name": rec["name"], "origin": origin,
           "collections": [], "tried": 0}

    # The census's own guess first when it has one -- it is free to try and is
    # right whenever the site uses a conventional slug.
    cands = []
    listing = rec.get("listing_url")
    if listing:
        cands.append(listing)
    for seg in sitemap_segments(origin, delay)[:MAX_CANDIDATES]:
        cands.append(f"{origin}/{seg}")
    out["tried"] += 1  # the sitemap

    if len(cands) <= 1:
        cands += [f"{origin}/{s}" for s in FALLBACK_SLUGS]

    seen = set()
    for url in cands:
        u = url.split("?")[0].rstrip("/")
        if u in seen:
            continue
        seen.add(u)
        out["tried"] += 1
        coll = read_collection(f"{u}?format=json", delay)
        if coll and not any(c["url"].split("?")[0] == coll["url"].split("?")[0]
                            for c in out["collections"]):
            out["collections"].append(coll)
        if len(seen) >= MAX_CANDIDATES + 2:
            break
    return out


# ---------------------------------------------------------------- io

def registry_path(city):
    return os.path.join(ROOT, "cities", city, "squarespace.json")


def save(city, payload):
    os.makedirs(os.path.dirname(registry_path(city)), exist_ok=True)
    with open(registry_path(city), "w") as f:
        json.dump(payload, f, indent=1)


def load(city):
    try:
        with open(registry_path(city)) as f:
            return json.load(f)
    except Exception:
        return None


def report(payload):
    venues = payload.get("venues") or []
    probed = payload.get("probed", len(venues))
    with_coll = [v for v in venues if v.get("collections")]
    live = [v for v in with_coll
            if sum(c["upcoming"] for c in v["collections"]) > 0]
    upcoming = sum(c["upcoming"] for v in venues for c in v.get("collections") or [])

    print(f"\n{probed} Squarespace venues probed, {payload.get('requests', 0)} requests\n")
    print(f"  has an events collection      {len(with_coll):5d}  "
          f"{100.0 * len(with_coll) / max(1, probed):4.0f}%")
    print(f"  ...with anything upcoming     {len(live):5d}  "
          f"{100.0 * len(live) / max(1, probed):4.0f}%")
    print(f"  upcoming events discoverable  {upcoming:5d}")

    if live:
        ranked = sorted(live, key=lambda v: -sum(c["upcoming"] for c in v["collections"]))
        print("\nBIGGEST CALENDARS")
        for v in ranked[:20]:
            n = sum(c["upcoming"] for c in v["collections"])
            print(f"  {n:4d} upcoming  {v['name'][:34]:<34} "
                  f"{v['collections'][0]['url'][:58]}")
    print()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        payload = load(args.city)
        if not payload:
            print("no registry -- run discovery first")
            return 1
        report(payload)
        return 0

    sites = load_existing(args.city)
    if not sites:
        print("no census.json -- run census.py first")
        return 1

    def sig(r, k):
        return (r.get("signals") or {}).get(k)

    todo = [r for r in sites
            if r.get("signals") and not is_blocked(r)
            and (sig(r, "squarespace_json") or "squarespace" in (sig(r, "cms") or []))]
    if args.limit:
        todo = todo[:args.limit]

    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "city": args.city, "probed": len(todo), "requests": 0, "venues": []}
    print(f"{len(todo)} Squarespace venues to probe\n")

    for i, rec in enumerate(todo, 1):
        try:
            v = discover(rec, args.delay)
        except KeyboardInterrupt:
            print("\ninterrupted -- saving what we have")
            break
        except Exception as e:
            v = {"host": rec["host"], "name": rec["name"], "origin": origin_of(rec),
                 "collections": [], "tried": 0, "error": f"{type(e).__name__}: {e}"}
        payload["venues"].append(v)
        payload["requests"] += v.get("tried", 0)

        n = sum(c["upcoming"] for c in v.get("collections") or [])
        mark = (f"{n:>4} upcoming" if n else
                "collection, empty" if v.get("collections") else "-")
        print(f"  [{i}/{len(todo)}] {mark:<18} {v['host'][:44]}")
        if i % 20 == 0:
            save(args.city, payload)

    payload["probed"] = len(payload["venues"])
    save(args.city, payload)
    print(f"\nwrote {registry_path(args.city)}")
    report(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
