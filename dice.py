#!/usr/bin/env python3
"""Discover -- which DICE events are in this city, and when did each last change?

DICE is the ticketer for the tier of room the index has been missing entirely:
too small for Ticketmaster, too professional for a Squarespace calendar. Every
other route to that tier in this project ended at an ownership-scoped API or a
terms-of-service wall. This one does not, and the reason is worth stating
plainly, because it is the whole basis for the adapter existing:

    User-agent: *
    Content-Signal: search=yes,ai-train=no,use=reference
    Allow: /
    Sitemap: https://dice.fm/sitemaps/sitemap.xml

A published sitemap and schema.org markup on every event page are not an
oversight to be exploited. They are the mechanism a site uses to say *index
me*, and reading them is the use they were put there for. The content signal
sets the boundary the same way: reference is permitted, training is not, so
this stores a listing and links back to DICE rather than mirroring the page.

DISCOVERY IS CHEAP HERE, which inverts the bargain the other registries make.
`squarespace.py` commits its registry because re-deriving a venue's event slug
costs four requests per venue and the answer only changes when a site is
redesigned. DICE costs FOUR REQUESTS FOR THE WHOLE CITY -- one sitemap index,
three member sitemaps -- and the answer changes daily, because events are born
and die constantly. So this registry is REPLACED on every run rather than
merged, and it is meant to be re-run with the fetch rather than once a season.
Merging would be actively wrong: an event that has left the sitemap has been
cancelled or has passed, and keeping it would resurrect it.

Two things make the city filter subtler than it looks:

  * **The city lives in the slug, and its length varies.** A DICE event slug
    ends `-<venue>-<city>-tickets`, so the city can be matched without fetching
    anything. But `new-york-city` is three tokens, `new-york` is two and
    `brooklyn` is one, and taking a fixed number of them mis-slices all but
    one. Matching the whole `-<city>-tickets` suffix is what works.

  * **A one-token match is not safe.** `-york-tickets` looks like a reasonable
    shortcut and is not: 53 of those are a venue in York, England. The tokens
    are configured per city in `sources.json` for the multi-city rule, and
    because choosing them is a judgement about a place, not about DICE.

The filter is a PREFILTER, not the arbiter. It is a string match on a URL, and
it is allowed to be slightly wrong in the generous direction -- `normalize.py`
reads the coordinates out of each event's own markup and drops anything that
does not land inside a real neighbourhood polygon, exactly as it already does
for the Ticketmaster DMA. Costing a handful of wasted requests to avoid missing
a venue is the right way round.

`lastmod` is the other reason to do discovery separately. Every event URL
carries one, so `fetch.py` can re-read only what has actually changed instead
of pulling 1,500 unchanged pages a day. That is the same move as reading
`X-WP-Total` instead of paging to find a collection's size: the server has
already answered the question, so asking again is noise.

    python3 dice.py                 # scan the sitemaps, write the registry
    python3 dice.py --report        # re-print, no network
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))

SITEMAP_INDEX = "https://dice.fm/sitemaps/sitemap.xml"
XML_ACCEPT = "application/xml,text/xml,*/*;q=0.8"

# The sitemaps are ~10,000 URLs each and arrive uncompressed, so the default
# 800 KB cap would truncate them mid-document and silently lose the tail.
SITEMAP_BYTES = 8_000_000

LOC = re.compile(r"<loc>([^<]+)</loc>", re.I)
# <lastmod> is optional per the sitemap schema and a handful of entries omit it.
# Pairing on the enclosing <url> block keeps a missing one from shifting every
# subsequent timestamp onto the wrong event.
URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.I | re.S)
LASTMOD = re.compile(r"<lastmod>([^<]+)</lastmod>", re.I)

EVENT_PATH = "/event/"
SUFFIX = "-tickets"


def city_slugs(city):
    """The `-<city>-tickets` tokens for this city, from its source registry.

    Config, not code: which slugs mean "this city" is a fact about the city,
    and the multi-city rule says those live in `cities/<slug>/`.
    """
    path = os.path.join(ROOT, "cities", city, "sources.json")
    with open(path) as f:
        sources = json.load(f)["sources"]
    for s in sources:
        if s.get("adapter") == "dice":
            slugs = s.get("city_slugs") or []
            if not slugs:
                raise RuntimeError(f"the dice source in {path} has no city_slugs")
            return list(slugs)
    raise RuntimeError(f"no dice source in {path}")


def sitemap_urls(delay):
    """The member sitemaps listed in the index."""
    r = http.get_full(SITEMAP_INDEX, delay=delay, accept=XML_ACCEPT,
                      max_bytes=SITEMAP_BYTES)
    if r["status"] != 200 or not r["body"]:
        raise RuntimeError(f"sitemap index returned {r['status']}")
    return LOC.findall(r["body"].decode("utf-8", "replace"))


def scan(url, delay):
    """Every event URL in one sitemap, with its lastmod -> [(url, lastmod)].

    Raises rather than returning empty. A member sitemap that fails would
    otherwise cost a third of the city with nothing to show for it, and a
    registry that is quietly a third short is worse than no registry: the
    freshness gate in `validate.py` checks that a source produced *something*,
    so a partial run would sail through it.
    """
    r = http.get_full(url, delay=delay, accept=XML_ACCEPT, max_bytes=SITEMAP_BYTES)
    if r["status"] != 200 or not r["body"]:
        raise RuntimeError(f"{url} returned {r['status']} ({r['error'] or 'no body'})")
    out = []
    for block in URL_BLOCK.findall(r["body"].decode("utf-8", "replace")):
        loc = LOC.search(block)
        if not loc or EVENT_PATH not in loc.group(1):
            continue
        mod = LASTMOD.search(block)
        out.append((loc.group(1), mod.group(1) if mod else None))
    return out


def match_city(url, slugs):
    """Which configured city slug this event URL ends with, or None.

    Anchored on the whole `-<slug>-tickets` suffix rather than on the slug
    appearing anywhere: a band called Brooklyn playing in Bristol should not
    become a Brooklyn listing.
    """
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if not slug.endswith(SUFFIX):
        return None
    for s in slugs:
        if slug.endswith(f"-{s}{SUFFIX}"):
            return s
    return None


# ---------------------------------------------------------------- io

def registry_path(city):
    return os.path.join(ROOT, "cities", city, "dice.json")


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
    events = payload.get("events") or []
    scanned = payload.get("scanned", 0)
    by_slug = {}
    for e in events:
        by_slug[e.get("city_slug")] = by_slug.get(e.get("city_slug"), 0) + 1

    print(f"\n{scanned} DICE event URLs scanned, {len(events)} in this city "
          f"({100.0 * len(events) / max(1, scanned):.1f}%)\n")
    for slug, n in sorted(by_slug.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  -{slug}-tickets")
    undated = sum(1 for e in events if not e.get("lastmod"))
    if undated:
        print(f"\n  {undated} without a lastmod -- these are re-read every run")
    print()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        payload = load(args.city)
        if not payload:
            print("no registry -- run discovery first")
            return 1
        report(payload)
        return 0

    slugs = city_slugs(args.city)
    print(f"dice: {args.city}, matching {', '.join('-' + s + '-tickets' for s in slugs)}")

    try:
        members = sitemap_urls(args.delay)
        print(f"  {len(members)} member sitemaps")

        scanned, events, seen = 0, [], set()
        for m in members:
            rows = scan(m, args.delay)
            scanned += len(rows)
            for url, mod in rows:
                slug = match_city(url, slugs)
                if not slug or url in seen:
                    continue
                seen.add(url)
                events.append({"url": url, "city_slug": slug, "lastmod": mod})
            print(f"    {m.rsplit('/', 1)[-1]}: {len(rows)} events, "
                  f"{len(events)} kept so far")
    except RuntimeError as e:
        # The previous registry is left in place deliberately. Yesterday's list
        # is stale but usable; a truncated one is a silent outage.
        print(f"  FAILED: {e}\n  keeping the existing registry")
        return 1

    # Not a gate -- DICE reorganising its sitemaps is their business, not an
    # error. But a registry that halves overnight is worth seeing before the
    # fetch spends an hour acting on it.
    prev = load(args.city) or {}
    was = len(prev.get("events") or [])
    if was and len(events) < was * 0.5:
        print(f"  WARNING: {was} events last run, {len(events)} now -- "
              f"check the city_slugs still match DICE's slugs")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "city": args.city,
        "city_slugs": slugs,
        "sitemaps": len(members),
        "scanned": scanned,
        "events": sorted(events, key=lambda e: e["url"]),
    }
    save(args.city, payload)
    print(f"\nwrote {registry_path(args.city)}")
    report(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
