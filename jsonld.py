#!/usr/bin/env python3
"""Discover -- which venues publish their whole calendar as schema.org markup?

The census found that 54% of readable sites carry JSON-LD and 7% carry a JSON-LD
*Event*, and that gap is the whole reason this script exists rather than a rule
that says "site has JSON-LD, read it". Most of that markup is `Organization`,
`WebSite` and `BreadcrumbList` -- SEO boilerplate a theme emits, describing the
site rather than what is on at it.

What this looks for is narrower and worth more: a LISTING page whose markup
already contains several dated events. Where that is true the whole calendar
costs ONE request, which is a different economic proposition from DICE's 1,539
and from crawling a detail page per event. Where it is not true -- one event per
detail page, which is the more common shape -- the venue is recorded with what
it has and left alone, because harvesting it means link discovery plus a fetch
per event and that bargain deserves its own decision rather than being smuggled
in here.

Two things this does NOT do, both deliberate:

  * **No guessing at URLs.** It reads `listing_url` as the census recorded it.
    A site that renamed its calendar is a census problem and gets fixed there.
  * **No trust that a venue's page is about that venue.** The distinct
    `location.name` values are recorded per site so the roster can be read
    before it is used. newyorkcitytheatre.com-style aggregators publish other
    rooms' shows under their own domain, and a site listing the Palace and the
    Beacon is not a venue calendar however good its markup is. Those get a
    hand-set `skip` with a reason, the same as in wordpress.json.

    python3 jsonld.py                # probe every candidate, ~50 requests
    python3 jsonld.py --report       # re-print from the registry, no network
"""
import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))

# Two dated events is the floor. One is indistinguishable from a detail page
# that happens to be the listing, and this whole script exists to find the
# pages where a calendar arrives whole.
MIN_EVENTS = 2


def registry_path(city):
    return os.path.join(ROOT, "cities", city, "jsonld.json")


def census(city):
    with open(os.path.join(ROOT, "data", city, "census.json")) as f:
        return json.load(f)["sites"]


def place_index(city):
    """osm_id -> the venue's own coordinates, from the committed places file.

    `raw/` is gitignored so the OSM extract may not be on disk; places.json is
    committed and carries the same points. It writes the id as `node/123` where
    the census keeps a bare integer.
    """
    try:
        with open(os.path.join(ROOT, "data", city, "places.json")) as f:
            blob = json.load(f)
    except Exception:
        return {}
    rows = blob.get("places") if isinstance(blob, dict) else blob
    return {str(r.get("osm_id", "")).split("/")[-1]: r
            for r in rows or [] if r.get("lat") is not None}


def candidates(sites):
    """Readable sites the census saw an Event on, that state a listing URL."""
    out = []
    for s in sites:
        sig = s.get("signals") or {}
        if not sig.get("jsonld_event"):
            continue
        if s.get("status") in (401, 403) or s.get("error") == "robots-denied":
            continue
        if not s.get("listing_url"):
            continue
        out.append(s)
    return out


def detail_prefix(rec):
    """The path shape this site's event pages live under, from the census.

    `/event/best-of-the-apollo` -> `/event`. Read off a detail URL the census
    already followed, rather than guessed, which is what lets a venue whose
    listing is /whats-on and whose events are /shows/ work with no rule: the
    two are simply different questions and the census answered the second.
    """
    det = rec.get("detail_url") or ""
    seg = urllib.parse.urlsplit(det).path.strip("/").split("/")[0]
    return "/" + seg if seg else None


def probe(rec, delay):
    """Read one listing page and report the events its markup already carries.

    Two outcomes, and the split is what the whole registry turns on. A listing
    whose markup already holds the calendar is `mode: listing` and costs one
    request forever. Anything else is `mode: detail` -- the events exist, one
    per page, and reaching them means following links -- so this measures how
    many candidate links the listing offers and lets the fetch decide whether
    that is affordable.
    """
    # Imported here rather than at module scope: fetch.py imports this module's
    # siblings, and the extractor is the one piece that must not drift between
    # discovery and the fetch that follows it.
    from fetch import _ld_events, _ld_links, _one, _event_type

    url = rec["listing_url"]
    out = {"host": rec["host"], "name": rec["name"], "url": url,
           "events": 0, "locations": [], "types": [], "status": None,
           "error": None, "mode": None, "prefix": None, "links": 0}
    try:
        r = http.get_full(url, delay=delay, accept=http.HTML_ACCEPT,
                          max_bytes=2_000_000)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["status"] = r["status"]
    if r["error"]:
        out["error"] = r["error"]
    if not r["body"]:
        return out

    body = r["body"].decode("utf-8", "replace")
    evs = _ld_events(body)
    out["events"] = len(evs)
    out["locations"] = sorted({str(_one(e.get("location")).get("name"))
                               for e in evs if _one(e.get("location")).get("name")})
    out["types"] = sorted({_event_type(e) for e in evs if _event_type(e)})

    if out["events"] >= MIN_EVENTS:
        out["mode"] = "listing"
        return out

    prefix = detail_prefix(rec)
    out["prefix"] = prefix
    if prefix:
        out["links"] = len(_ld_links(body, r["url"] or url, prefix))
    # A site with Event markup on its detail page and no reachable links to it
    # is not readable by this adapter. Usually the listing is rendered by
    # JavaScript, so the anchors exist for a browser and not for us. Recorded
    # as `detail` with zero links rather than dropped, because the finding is
    # that it was checked.
    out["mode"] = "detail"
    return out


def report(reg):
    vs = reg["venues"]
    live = [v for v in vs if v["events"] >= MIN_EVENTS and not v.get("skip")]
    skipped = [v for v in vs if v.get("skip")]
    print(f"\n{len(vs)} candidates probed · {len(live)} carry a calendar "
          f"({sum(v['events'] for v in live)} events) · {len(skipped)} skipped\n")
    for v in sorted(live, key=lambda x: -x["events"]):
        rooms = f"  [{len(v['locations'])} rooms]" if len(v["locations"]) > 1 else ""
        print(f"  {v['events']:>4}  {v['name'][:38]:40} {','.join(v['types'])[:22]:24}{rooms}")
    for v in skipped:
        print(f"  skip  {v['name'][:38]:40} {v.get('why', '')[:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--report", action="store_true",
                    help="re-print from the registry on disk, no network")
    args = ap.parse_args()

    path = registry_path(args.city)

    if args.report:
        try:
            with open(path) as f:
                report(json.load(f))
        except FileNotFoundError:
            print(f"no {path} -- run `python3 jsonld.py` first")
            return 1
        return 0

    # Hand-set decisions survive a re-probe. Everything else is measured again,
    # because an events count is a fact about today and a skip is a judgement
    # about the site.
    prior = {}
    try:
        with open(path) as f:
            prior = {v["host"]: v for v in json.load(f)["venues"]}
    except Exception:
        pass

    sites = census(args.city)
    places = place_index(args.city)
    todo = candidates(sites)
    print(f"{len(todo)} sites carry a JSON-LD Event and state a listing URL\n")

    venues = []
    for i, rec in enumerate(todo, 1):
        p = probe(rec, args.delay)
        # The building's own point, for the single-room case only. Recorded for
        # every venue because it is a fact about the host either way; whether
        # it may be USED is the adapter's decision, and it turns on how many
        # rooms the calendar names.
        pl = places.get(str(rec.get("osm_id")))
        p["lat"] = pl.get("lat") if pl else None
        p["lon"] = pl.get("lon") if pl else None
        p["borough"] = pl.get("borough") if pl else None
        old = prior.get(p["host"]) or {}
        for k in ("skip", "why", "venue_name"):
            if old.get(k) is not None:
                p[k] = old[k]
        venues.append(p)
        mark = f"{p['events']:>4} events" if p["events"] else f"  -- {p['error'] or 'none'}"
        print(f"  [{i}/{len(todo)}] {mark:<22} {p['host'][:44]}")

    venues.sort(key=lambda v: (-v["events"], v["host"]))
    reg = {
        "_comment": [
            "Venues whose LISTING page carries their calendar as schema.org markup,",
            "so the whole roster costs one request. Discovered by jsonld.py from the",
            "census's `jsonld_event` signal, which is narrower than `jsonld_detail`:",
            "54% of sites carry JSON-LD and 7% carry an Event, and the difference is",
            "Organization/WebSite boilerplate that describes the site rather than",
            "anything on at it.",
            "REPLACED each run except for hand-set `skip`, `why` and `venue_name`.",
            "An events count is a fact about today; a skip is a judgement about the",
            "site and must not be undone by a re-probe.",
            "`locations` is every distinct location.name seen, and it is the field to",
            "read before trusting a site: a venue calendar names its own rooms, while",
            "an aggregator names other people's. Events carry a PostalAddress and no",
            "coordinates, so each location is geocoded from its street address rather",
            "than inheriting one point per host -- which is what lets a club with",
            "three rooms place all three correctly.",
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "city": args.city,
        "source": "jsonld.py",
        "min_events": MIN_EVENTS,
        "venues": venues,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(reg, f, indent=1)
    print(f"\nwrote {path}")
    report(reg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
