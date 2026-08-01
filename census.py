#!/usr/bin/env python3
"""Probe -- what can each venue's own site actually give us?

The coverage thesis rests on a stack of untested assumptions: that ICS is
widespread among institutions, that JSON-LD `Event` blocks are on detail pages,
that some useful share of arts venues run WordPress. Every one of those is
currently a guess, and writing adapters against guesses is how you end up with
six parsers that each cover four venues.

This answers them by measurement instead. `raw/<city>/osm-places.json` already
holds ~1,600 venues, ~950 of them with a website tag. For each host it asks a
small fixed set of questions -- is there an ICS feed, a WordPress REST API, an
RSS feed, JSON-LD on a real event detail page, a link out to a ticketing
platform -- and writes the answers to `data/<city>/census.json`.

The output is a table of hit counts. Adapters then get written in descending
order of that table, which is the whole point: the expensive decision here is
WHICH adapters to build, and it should be made from evidence that costs one
overnight run rather than from a plausible-sounding tier list.

Two findings this is specifically designed to surface, because both contradict
something currently assumed:

  * Detail pages, not homepages. An earlier probe looked for `Event` JSON-LD on
    homepages, found none, and nearly wrote the format off. Homepages carry
    `Organization` and `WebSite`; events live one or two clicks in. So this
    follows landing -> listing -> detail and reports the types found at each.

  * A venue can be unreadable rather than unstructured. Several roster sites
    return no event links at all to a plain HTTP client because they render
    client-side. That is invisible to a stdlib-only fetcher, and it is a
    different problem from "has no feed" -- one is solved by a parser, the other
    is not solved at all without a browser. They are counted separately.

Politeness: pacing is per-host and lives in lib/http, so a sequential run does
not stall on the clock between different hosts. Budget is ~4 requests per venue.
robots.txt is honoured, and a disallow is recorded as a result rather than
skipped silently -- "we are not allowed in" is a finding too.

    python3 census.py                      # every venue with a website
    python3 census.py --limit 40           # a taste, to sanity-check first
    python3 census.py --resume             # continue an interrupted run
    python3 census.py --report             # re-print the table, no network
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

ROOT = os.path.dirname(os.path.abspath(__file__))
FLUSH_EVERY = 25

# Paths that tend to hold listings. Used to walk landing -> listing -> detail.
EVENT_PATH = re.compile(
    r"/(events?|shows?|calendar|performances?|whats-?on|programs?|programming"
    r"|screenings?|listings?|lineup|agenda|exhibitions?|classes|workshops?)(/|$|\?)",
    re.I)

# schema.org Event and its subtypes. Presence of any of these on a detail page
# is the single most valuable thing this census can find -- it means one generic
# adapter reads the venue, with no bespoke parsing at all.
EVENT_TYPES = {
    "Event", "MusicEvent", "TheaterEvent", "ScreeningEvent", "ComedyEvent",
    "ExhibitionEvent", "EducationEvent", "SocialEvent", "Festival", "DanceEvent",
    "LiteraryEvent", "BusinessEvent", "SportsEvent", "FoodEvent", "ChildrensEvent",
    "VisualArtsEvent", "PublicationEvent", "CourseInstance", "EventSeries",
}

# Where small venues actually sell. A link out to one of these means the venue's
# own site is a storefront and the events are somewhere else -- which is good
# news, because one platform adapter then covers every venue that uses it.
PLATFORMS = {
    "eventbrite": r"eventbrite\.(com|co\.uk)",
    "dice": r"dice\.fm",
    "resident-advisor": r"\bra\.co|residentadvisor\.net",
    "ticketweb": r"ticketweb\.com",
    "ticketmaster": r"ticketmaster\.com|livenation\.com",
    "axs": r"axs\.com",
    "seetickets": r"seetickets\.us",
    "tixr": r"tixr\.com",
    "shotgun": r"shotgun\.live",
    "seated": r"seated\.com",
    "etix": r"etix\.com",
    "showclix": r"showclix\.com",
    "prekindle": r"prekindle\.com",
    "eventzilla": r"eventzilla\.net",
    "universe": r"universe\.com",
    "withfriends": r"withfriends\.co",
    "squadup": r"squadup\.com",
    "opendate": r"opendate\.io|venuepilot\.co",
    "bandsintown": r"bandsintown\.com",
    "songkick": r"songkick\.com",
    "luma": r"lu\.ma",
    "posh": r"posh\.vip",
}

# Site platforms. Some of these have their own documented event API (localist,
# trumba, 25live, libcal); some embed the page's data as JSON in the HTML
# (next, nuxt, squarespace), which is a source in its own right.
CMS = {
    "localist": r"localist\.com|/api/2/events",
    "trumba": r"trumba\.com",
    "25live": r"25live|collegenet",
    "libcal": r"libcal\.com|springshare",
    "communico": r"communico\.co",
    "squarespace": r"Static\.SQUARESPACE_CONTEXT|squarespace\.com",
    "wix": r"wix\.com|_wixCIDX|wixstatic",
    "next": r"__NEXT_DATA__",
    "nuxt": r"__NUXT__",
    "cargo": r"cargocollective|cargo\.site",
    "drupal": r"/sites/default/files|Drupal\.settings",
}

SOCIAL_ONLY = re.compile(r"^https?://(www\.)?(facebook|instagram|twitter|x|linktr)\.", re.I)

LD_BLOCK = re.compile(
    rb"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.I | re.S)
ANCHOR = re.compile(rb"""<a\b[^>]*\bhref=["']([^"']+)["']""", re.I)
LINK_TAG = re.compile(rb"<link\b[^>]*>", re.I)
SCRIPT_BLOCK = re.compile(rb"<script\b.*?</script>", re.I | re.S)


# ---------------------------------------------------------------- detection

def _walk_types(node, out):
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.add(t)
        elif isinstance(t, list):
            out.update(x for x in t if isinstance(x, str))
        for v in node.values():
            _walk_types(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_types(v, out)


def jsonld_types(body):
    """Every @type mentioned anywhere in the page's JSON-LD blocks.

    Tolerant on purpose. Some CMSes emit blocks with trailing commas or raw
    newlines in strings, and a census is counting capability rather than
    extracting data -- knowing an `Event` block is present is the finding, and
    a strict parser that drops the whole block would hide it.
    """
    types = set()
    for m in LD_BLOCK.finditer(body):
        raw = m.group(1).decode("utf-8", "replace").strip()
        try:
            _walk_types(json.loads(raw), types)
        except Exception:
            types.update(re.findall(r'"@type"\s*:\s*"([A-Za-z]+)"', raw))
    return types


def feed_links(body, base):
    """ICS and RSS advertised in <link rel=alternate>, plus bare .ics hrefs."""
    ics, rss = [], []
    for m in LINK_TAG.finditer(body):
        tag = m.group(0).decode("utf-8", "replace")
        href = re.search(r"""href=["']([^"']+)["']""", tag, re.I)
        if not href:
            continue
        url = urllib.parse.urljoin(base, href.group(1))
        low = tag.lower()
        if "text/calendar" in low or url.lower().endswith(".ics"):
            ics.append(url)
        elif "application/rss+xml" in low or "application/atom+xml" in low:
            rss.append(url)
    for m in ANCHOR.finditer(body):
        href = m.group(1).decode("utf-8", "replace")
        if href.lower().endswith(".ics") or href.lower().startswith("webcal:"):
            ics.append(urllib.parse.urljoin(base, href))
    return sorted(set(ics))[:5], sorted(set(rss))[:5]


def match_map(body_text, table):
    return sorted(k for k, pat in table.items() if re.search(pat, body_text, re.I))


def looks_client_rendered(body):
    """Distinguish "no structure" from "no HTML at all".

    A venue whose events are drawn by JavaScript is not a parsing problem, it is
    a fetching problem, and a stdlib-only pipeline cannot reach it however good
    the parser is. The tell is a document that is mostly <script> and carries
    almost no links -- an SPA shell. Sites with __NEXT_DATA__ or __NUXT__ are
    excluded, because those embed their page data as JSON right there in the
    HTML and are readable after all.
    """
    if b"__NEXT_DATA__" in body or b"__NUXT__" in body:
        return False
    if len(body) < 500:
        return False
    script_bytes = sum(len(m.group(0)) for m in SCRIPT_BLOCK.finditer(body))
    anchors = len(ANCHOR.findall(body))
    return anchors < 8 and script_bytes > 0.5 * len(body)


def same_host_links(body, base):
    host = urllib.parse.urlsplit(base).netloc
    out = []
    for m in ANCHOR.finditer(body):
        href = m.group(1).decode("utf-8", "replace").strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urllib.parse.urljoin(base, href)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme in ("http", "https") and parts.netloc == host:
            out.append(urllib.parse.urlunsplit(parts._replace(fragment="")))
    return out


def pick_listing(links):
    """Shallowest event-ish path wins -- that is the listing, not an entry."""
    cands = [u for u in links if EVENT_PATH.search(urllib.parse.urlsplit(u).path)]
    if not cands:
        return None
    return sorted(set(cands), key=lambda u: (u.count("/"), len(u)))[0]


def pick_detail(links, listing):
    """A link deeper than the listing itself. That is where Event JSON-LD is."""
    base_depth = urllib.parse.urlsplit(listing).path.rstrip("/").count("/")
    cands = [u for u in links
             if EVENT_PATH.search(urllib.parse.urlsplit(u).path)
             and urllib.parse.urlsplit(u).path.rstrip("/").count("/") > base_depth]
    if not cands:
        return None
    return sorted(set(cands), key=len)[0]


# ---------------------------------------------------------------- the probe

def census_site(v, delay):
    """Up to four requests: landing, listing, detail, and one endpoint guess."""
    rec = {"name": v["name"], "url": v["url"], "host": v["host"], "kind": v["kind"],
           "osm_id": v["osm_id"], "status": None, "final_url": None, "error": None,
           "listing_url": None, "detail_url": None, "signals": {}}

    r = http.get_full(v["url"], delay=delay)
    rec["status"], rec["error"], rec["final_url"] = r["status"], r["error"], r["url"]
    if not r["body"]:
        return rec

    body, base = r["body"], r["url"]
    text = body.decode("utf-8", "replace")
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(base))

    ics, rss = feed_links(body, base)
    sig = {
        "ics": ics,
        "rss": rss,
        "jsonld_home": sorted(jsonld_types(body)),
        "platforms": match_map(text, PLATFORMS),
        "cms": match_map(text, CMS),
        "js_rendered": looks_client_rendered(body),
        "wp": bool(re.search(r"""rel=["']https://api\.w\.org/["']""", text)),
        "jsonld_detail": [],
        "jsonld_event": False,
        "tribe": None,
        "squarespace_json": None,
    }

    # Landing -> listing -> detail. The listing page usually carries better
    # feed links than the homepage does, so re-check feeds there too.
    links = same_host_links(body, base)
    listing = pick_listing(links)
    detail = None
    if listing:
        rec["listing_url"] = listing
        lr = http.get_full(listing, delay=delay)
        if lr["body"]:
            l_ics, l_rss = feed_links(lr["body"], lr["url"])
            sig["ics"] = sorted(set(sig["ics"]) | set(l_ics))[:5]
            sig["rss"] = sorted(set(sig["rss"]) | set(l_rss))[:5]
            l_links = same_host_links(lr["body"], lr["url"])
            if sig["js_rendered"] and not looks_client_rendered(lr["body"]):
                sig["js_rendered"] = False
            detail = pick_detail(l_links, listing) or pick_detail(links, listing)

    if detail:
        rec["detail_url"] = detail
        dr = http.get_full(detail, delay=delay)
        if dr["body"]:
            types = jsonld_types(dr["body"])
            sig["jsonld_detail"] = sorted(types)
            sig["jsonld_event"] = bool(types & EVENT_TYPES)

    # One endpoint guess, chosen by what the HTML already told us. Guessing
    # blind at every host would quadruple the request count to learn nothing.
    if sig["wp"]:
        t = http.get_full(f"{origin}/wp-json/tribe/events/v1/events?per_page=1", delay=delay)
        if t["status"] == 200 and "json" in t["ctype"]:
            try:
                sig["tribe"] = json.loads(t["body"].decode("utf-8", "replace")).get("total")
            except Exception:
                sig["tribe"] = True
        else:
            sig["tribe"] = False
    elif "squarespace" in sig["cms"]:
        # Squarespace serves any page as JSON with ?format=json. If that holds
        # it is effectively a free API for a large slice of small venues.
        target = detail or listing or base
        sep = "&" if "?" in target else "?"
        s = http.get_full(f"{target}{sep}format=json", delay=delay)
        sig["squarespace_json"] = bool(s["status"] == 200 and "json" in s["ctype"])

    rec["signals"] = sig
    return rec


# ---------------------------------------------------------------- inputs

def load_venues(city):
    """OSM places -> one candidate per host. Facebook pages are not websites."""
    path = os.path.join(ROOT, "raw", city, "osm-places.json")
    with open(path) as f:
        elements = json.load(f)["elements"]

    seen, out = set(), []
    for e in elements:
        tags = e.get("tags") or {}
        site = (tags.get("website") or tags.get("contact:website") or "").strip()
        if not site:
            continue
        if not site.startswith(("http://", "https://")):
            site = "https://" + site.lstrip("/")
        if SOCIAL_ONLY.match(site):
            continue
        host = urllib.parse.urlsplit(site).netloc.lower()
        if not host or host in seen:
            continue
        seen.add(host)
        kind = next((f"{k}={tags[k]}" for k in ("amenity", "leisure", "tourism", "shop")
                     if tags.get(k)), "unknown")
        out.append({"name": tags.get("name") or host, "url": site, "host": host,
                    "kind": kind, "osm_id": e.get("id")})
    return out


def out_path(city):
    return os.path.join(ROOT, "data", city, "census.json")


def save(city, records):
    os.makedirs(os.path.dirname(out_path(city)), exist_ok=True)
    with open(out_path(city), "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                   "count": len(records), "sites": records}, f, indent=1)


def load_existing(city):
    try:
        with open(out_path(city)) as f:
            return json.load(f)["sites"]
    except Exception:
        return []


# ---------------------------------------------------------------- reporting

def report(records):
    n = len(records)
    if not n:
        print("no records -- run the census first")
        return
    reached = [r for r in records if r.get("status") == 200]

    def pct(k):
        return f"{k:5d}  {100.0 * k / n:4.0f}%"

    print(f"\n{n} sites probed, {len(reached)} reachable\n")
    print("REACHABILITY")
    print(f"  200                    {pct(len(reached))}")
    print(f"  dead / error           {pct(sum(1 for r in records if r.get('status') is None))}")
    print(f"  robots-denied          {pct(sum(1 for r in records if r.get('error') == 'robots-denied'))}")
    print(f"  4xx / 5xx              {pct(sum(1 for r in records if r.get('status') and r['status'] != 200))}")

    def sig(r, k):
        return (r.get("signals") or {}).get(k)

    print("\nEXTRACTION CAPABILITY  (share of all probed)")
    rows = [
        ("JSON-LD Event on detail", sum(1 for r in reached if sig(r, "jsonld_event"))),
        ("ICS feed advertised", sum(1 for r in reached if sig(r, "ics"))),
        ("RSS / Atom feed", sum(1 for r in reached if sig(r, "rss"))),
        ("WordPress REST", sum(1 for r in reached if sig(r, "wp"))),
        ("  ...of which Tribe", sum(1 for r in reached if sig(r, "tribe"))),
        ("Squarespace ?format=json", sum(1 for r in reached if sig(r, "squarespace_json"))),
        ("any JSON-LD at all", sum(1 for r in reached if sig(r, "jsonld_home") or sig(r, "jsonld_detail"))),
        ("client-rendered (unreadable)", sum(1 for r in reached if sig(r, "js_rendered"))),
        ("no event listing page found", sum(1 for r in reached if not r.get("listing_url"))),
    ]
    for label, k in rows:
        print(f"  {label:<30} {pct(k)}")

    for title, table in (("TICKETING PLATFORMS LINKED", "platforms"), ("SITE PLATFORM", "cms")):
        counts = {}
        for r in reached:
            for name in sig(r, table) or []:
                counts[name] = counts.get(name, 0) + 1
        print(f"\n{title}")
        for name, k in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<30} {pct(k)}")

    covered = [r for r in reached
               if sig(r, "jsonld_event") or sig(r, "ics") or sig(r, "tribe")
               or sig(r, "squarespace_json") or sig(r, "platforms")]
    print(f"\nReachable by at least one generic adapter: {len(covered)} / {n} "
          f"({100.0 * len(covered) / n:.0f}%)")
    print("Write adapters top-down from the tables above.\n")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--limit", type=int, help="probe only the first N venues")
    ap.add_argument("--shard", help="i/n -- split the run across machines or nights")
    ap.add_argument("--delay", type=float, default=1.5, help="per-host seconds")
    ap.add_argument("--resume", action="store_true", help="skip hosts already recorded")
    ap.add_argument("--report", action="store_true", help="re-print the table, no network")
    args = ap.parse_args()

    if args.report:
        report(load_existing(args.city))
        return 0

    venues = load_venues(args.city)
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        venues = [v for k, v in enumerate(venues) if k % n == i]

    records = load_existing(args.city) if args.resume else []
    done = {r["host"] for r in records}
    todo = [v for v in venues if v["host"] not in done]
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(venues)} venues with websites; {len(todo)} to probe "
          f"({len(done)} already recorded)")

    for i, v in enumerate(todo, 1):
        try:
            rec = census_site(v, args.delay)
        except KeyboardInterrupt:
            print("\ninterrupted -- saving what we have")
            break
        except Exception as e:                     # one bad host is not a run
            rec = {"name": v["name"], "url": v["url"], "host": v["host"],
                   "kind": v["kind"], "osm_id": v["osm_id"], "status": None,
                   "error": f"{type(e).__name__}: {e}", "signals": {}}
        records.append(rec)

        s = rec.get("signals") or {}
        hits = [k for k in ("ics", "rss", "wp", "jsonld_event", "squarespace_json") if s.get(k)]
        hits += s.get("platforms") or []
        print(f"  [{i}/{len(todo)}] {rec['status'] or rec['error']:>14}  "
              f"{v['host'][:38]:<38} {','.join(hits) or '-'}")

        if i % FLUSH_EVERY == 0:
            save(args.city, records)

    save(args.city, records)
    print(f"\nwrote {len(records)} records -> {out_path(args.city)}")
    report(records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
