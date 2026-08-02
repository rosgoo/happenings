#!/usr/bin/env python3
"""Follow-up probe -- is Tribe's ICS export on, even where its REST API is not?

The census measured ICS at 4% (30 of 698 readable venues) and concluded the
format was not worth building on. That number is almost certainly wrong, for a
reason visible in the URLs it found: 28 of the 30 end in `?ical=1`, which is The
Events Calendar's export signature. So the plugin IS installed on those sites --
and the census only counted a feed when the site advertised it in a `<link
rel=alternate>` tag, which most themes never bother to emit.

Meanwhile the census found 188 sites running WordPress and only 20 answering on
Tribe's REST endpoint. If the ICS export is on by default the way the REST API
demonstrably is not, the real ICS number is a multiple of 4% and the adapter
ordering in `engineering.md` changes.

So: ask the 161 WordPress sites with no advertised feed directly, at up to three
URLs each.

  {listing}?ical=1                      the site's own events archive
  {origin}/events/?ical=1               Tribe's default archive slug
  {origin}/?post_type=tribe_events&ical=1   slug-independent fallback

The last one is the important one. A venue that renamed its archive to
/calendar/ or /whats-on/ still answers there, and guessing slugs one at a time
is how you undercount a format twice in a row.

Counting a hit honestly matters more than finding one. The census's own lesson
was that raw capability counts overstate -- 54% of sites carried JSON-LD and 7%
carried a JSON-LD *Event*. The same trap is here: an installed plugin serving an
empty calendar is not a source. So a response only counts if it parses as
VCALENDAR, and it is scored by how many VEVENTs it holds with a start date in
the future. An ICS feed whose last event was in 2019 is a dead calendar and is
reported as such, separately.

The 30 feeds the census already found are re-fetched too, unprobed assumptions
being what got us here. If a third of the known 4% turn out to be empty, that
baseline needs restating before anything is built on it.

    python3 ical.py                  # the 161 WordPress sites, ~7 min
    python3 ical.py --scope all      # every readable site with no known feed
    python3 ical.py --report         # re-print the table, no network
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402
from census import EVENT_PATH, is_blocked, load_existing  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
FLUSH_EVERY = 25

ICS_ACCEPT = "text/calendar,text/plain;q=0.9,*/*;q=0.8"

VEVENT_SPLIT = "BEGIN:VEVENT"
DTSTART = re.compile(r"^DTSTART[^:\r\n]*:(\d{8})", re.M)
RRULE = re.compile(r"^RRULE:(.*)$", re.M)
UNTIL = re.compile(r"UNTIL=(\d{8})")


# ---------------------------------------------------------------- reading ICS

def unfold(text):
    """RFC 5545 line folding: a leading space or tab continues the line above.

    Long SUMMARY and DESCRIPTION values are folded at 75 octets by most
    exporters, and DTSTART occasionally lands mid-fold on the ones that count
    bytes wrong. Unfolding first means the line-anchored patterns below see
    whole properties.
    """
    return re.sub(r"\r?\n[ \t]", "", text)


def summarise(body, today):
    """Is this actually a calendar, and does anything in it still lie ahead?

    Returns None when the response is not ICS at all -- which is the common
    case. WordPress answers an unknown query string with the homepage at 200,
    so status codes cannot be trusted here and the body has to be read.
    """
    text = body.decode("utf-8", "replace").lstrip("﻿").strip()
    if "BEGIN:VCALENDAR" not in text[:2000]:
        return None

    text = unfold(text)
    blocks = text.split(VEVENT_SPLIT)[1:]
    future = recurring = undated = 0
    latest = None

    for b in blocks:
        m = DTSTART.search(b)
        if not m:
            undated += 1
            continue
        d = m.group(1)
        if latest is None or d > latest:
            latest = d
        if d >= today:
            future += 1
            continue
        # A weekly series that started in 2023 and has no UNTIL is still
        # running. Scoring it by DTSTART alone would file every recurring
        # program in the city as expired.
        r = RRULE.search(b)
        if r:
            u = UNTIL.search(r.group(1))
            if not u or u.group(1) >= today:
                recurring += 1

    return {"events": len(blocks), "future": future, "recurring": recurring,
            "undated": undated, "latest": latest,
            "live": future + recurring > 0}


# ---------------------------------------------------------------- candidates

def with_ical(url):
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}ical=1"


def candidates(rec):
    """Up to three guesses, most-specific first so hits cost one request."""
    base = rec.get("final_url") or rec["url"]
    parts = urllib.parse.urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"

    urls = []
    listing = rec.get("listing_url")
    if listing and EVENT_PATH.search(urllib.parse.urlsplit(listing).path):
        urls.append(with_ical(listing))
    urls.append(f"{origin}/events/?ical=1")
    urls.append(f"{origin}/?post_type=tribe_events&ical=1")

    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def probe(rec, urls, delay, today):
    """Try each candidate until one parses as a calendar."""
    out = {"host": rec["host"], "name": rec["name"], "tried": [],
           "hit": None, "feed": None, "error": None}

    for url in urls:
        r = http.get_full(url, delay=delay, accept=ICS_ACCEPT)
        out["tried"].append({"url": url, "status": r["status"],
                             "ctype": r["ctype"].split(";")[0] or None,
                             "error": r["error"]})
        if not r["body"]:
            continue
        feed = summarise(r["body"], today)
        if feed:
            out["hit"], out["feed"] = r["url"], feed
            return out
    return out


# ---------------------------------------------------------------- population

def population(sites, scope):
    """Who to ask. Everything here is readable and has no feed we already know."""
    def sig(r, k):
        return (r.get("signals") or {}).get(k)

    reached = [r for r in sites if r.get("signals") and not is_blocked(r)]
    todo = [r for r in reached if not sig(r, "ics")]
    if scope == "wp":
        todo = [r for r in todo if sig(r, "wp")]
    elif scope == "listing":
        todo = [r for r in todo if r.get("listing_url")]
    return todo, [r for r in reached if sig(r, "ics")], len(reached)


def known_feeds(sites):
    """The 30 the census already found, as (record, url) pairs.

    Unescaped here as well as at the source, because census.json was written
    before that fix and still holds `&#038;` in the URLs it captured.
    """
    out = []
    for r in sites:
        for u in (r.get("signals") or {}).get("ics") or []:
            u = html.unescape(u)
            if u.lower().startswith("webcal:"):
                u = "https:" + u[7:]
            out.append((r, u))
            break
    return out


# ---------------------------------------------------------------- io

def out_path(city):
    return os.path.join(ROOT, "data", city, "ical.json")


def save(city, payload):
    os.makedirs(os.path.dirname(out_path(city)), exist_ok=True)
    with open(out_path(city), "w") as f:
        json.dump(payload, f, indent=1)


def load(city):
    try:
        with open(out_path(city)) as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- reporting

def report(payload):
    probed = payload.get("probed") or []
    verified = payload.get("verified") or []
    reached = payload.get("reached") or 0
    scope = payload.get("scope")

    hits = [p for p in probed if p.get("hit")]
    live = [p for p in hits if p["feed"]["live"]]

    print(f"\n{len(probed)} sites probed (scope={scope}), "
          f"{payload.get('requests', 0)} requests\n")

    print("BLIND ?ical=1 PROBE")
    print(f"  parsed as VCALENDAR           {len(hits):5d}  "
          f"{100.0 * len(hits) / len(probed) if probed else 0:4.0f}%")
    print(f"  ...with a live event          {len(live):5d}  "
          f"{100.0 * len(live) / len(probed) if probed else 0:4.0f}%")
    if hits:
        empty = [p for p in hits if not p["feed"]["live"]]
        print(f"  ...installed but empty/stale  {len(empty):5d}")
        by_url = {}
        for p in hits:
            shape = ("listing?ical=1" if "post_type" not in p["hit"]
                     and "/events/?ical" not in p["hit"] else
                     "/events/?ical=1" if "/events/?ical" in p["hit"]
                     else "?post_type=tribe_events")
            by_url[shape] = by_url.get(shape, 0) + 1
        print("\n  which guess found it")
        for k, v in sorted(by_url.items(), key=lambda kv: -kv[1]):
            print(f"    {k:<32} {v:5d}")

    if verified:
        vhits = [v for v in verified if v.get("feed")]
        vlive = [v for v in vhits if v["feed"]["live"]]
        print(f"\nKNOWN FEEDS RE-CHECKED  (the census's advertised {len(verified)})")
        print(f"  still parse                   {len(vhits):5d}")
        print(f"  ...with a live event          {len(vlive):5d}")
        dead = [v for v in vhits if not v["feed"]["live"]]
        for v in dead[:10]:
            print(f"    stale: {v['host'][:34]:<34} last {v['feed']['latest']}")
        notcal = [v for v in verified if not v.get("feed")]
        if notcal:
            print(f"  advertised a feed, served something else  {len(notcal):5d}")
            for v in notcal[:12]:
                print(f"    {str(v.get('ctype')):<12} {v.get('bytes', 0):>7}b  {v['host'][:40]}")

    # The number that replaces the census's 4%. Denominator is every readable
    # venue, so it is directly comparable to the table in engineering.md.
    advertised = payload.get("advertised", 0)
    if reached:
        print(f"\nCOMPARED TO THE CENSUS  (denominator: {reached} readable)")
        print(f"  census: ICS advertised in <link>  {advertised:5d}  "
              f"{100.0 * advertised / reached:4.0f}%")
        if verified:
            live_known = len([v for v in verified if v.get("feed") and v["feed"]["live"]])
            print(f"  ...of those, actually live        {live_known:5d}  "
                  f"{100.0 * live_known / reached:4.0f}%")
            total = live_known + len(live)
            print(f"  after this probe, live ICS        {total:5d}  "
                  f"{100.0 * total / reached:4.0f}%")
        else:
            print(f"  new live feeds found here         {len(live):5d}  "
                  f"(known feeds not re-checked)")

    if live:
        ranked = sorted(live, key=lambda p: -p["feed"]["future"])
        print(f"\nBIGGEST NEW FEEDS")
        for p in ranked[:20]:
            f = p["feed"]
            print(f"  {f['future']:4d} upcoming  {p['name'][:36]:<36} {p['hit'][:64]}")
    print()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--scope", default="wp", choices=("wp", "listing", "all"),
                    help="wp: sites with WordPress detected (the hypothesis)")
    ap.add_argument("--limit", type=int, help="probe only the first N")
    ap.add_argument("--delay", type=float, default=1.5, help="per-host seconds")
    ap.add_argument("--skip-verify", action="store_true",
                    help="do not re-check the feeds the census already found")
    ap.add_argument("--verify-only", action="store_true",
                    help="re-check the known feeds, keeping the blind results on disk")
    ap.add_argument("--report", action="store_true", help="re-print the table, no network")
    args = ap.parse_args()

    if args.report:
        payload = load(args.city)
        if not payload:
            print("no ical.json -- run the probe first")
            return 1
        report(payload)
        return 0

    sites = load_existing(args.city)
    if not sites:
        print("no census.json -- run census.py first")
        return 1

    todo, with_ics, reached = population(sites, args.scope)
    if args.limit:
        todo = todo[:args.limit]

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "scope": args.scope, "reached": reached,
               "advertised": len(with_ics), "requests": 0,
               "probed": [], "verified": []}

    if args.verify_only:
        # The blind sweep costs 400 requests and the verify pass costs 30. When
        # only the URL handling changed, redoing the sweep is 400 requests spent
        # to reproduce a file we already have.
        prior = load(args.city) or {}
        payload["probed"] = prior.get("probed") or []
        payload["requests"] = 0
        payload["scope"] = prior.get("scope", args.scope)
        todo = []

    print(f"{reached} readable in census; {len(with_ics)} already advertise ICS; "
          f"{len(todo)} to probe blind\n")

    for i, rec in enumerate(todo, 1):
        try:
            p = probe(rec, candidates(rec), args.delay, today)
        except KeyboardInterrupt:
            print("\ninterrupted -- saving what we have")
            break
        except Exception as e:
            p = {"host": rec["host"], "name": rec["name"], "tried": [],
                 "hit": None, "feed": None, "error": f"{type(e).__name__}: {e}"}
        payload["probed"].append(p)
        payload["requests"] += len(p["tried"])

        f = p.get("feed")
        mark = (f"HIT {f['future']:>4} upcoming" if f and f["live"]
                else "hit  (empty/stale)" if f else "-")
        print(f"  [{i}/{len(todo)}] {mark:<20} {p['host'][:44]}")

        if i % FLUSH_EVERY == 0:
            save(args.city, payload)

    if not args.skip_verify:
        known = known_feeds(with_ics)
        print(f"\nre-checking {len(known)} feeds the census already found\n")
        for i, (rec, url) in enumerate(known, 1):
            r = http.get_full(url, delay=args.delay, accept=ICS_ACCEPT)
            payload["requests"] += 1
            feed = summarise(r["body"], today) if r["body"] else None
            # Keep the shape of a non-answer. "Advertised a feed and serves 0
            # bytes of HTML" and "advertised a feed that is a real calendar with
            # nothing in it" are different failures, and only one is worth a
            # retry with a different URL.
            payload["verified"].append({"host": rec["host"], "name": rec["name"],
                                        "url": url, "status": r["status"],
                                        "ctype": r["ctype"].split(";")[0] or None,
                                        "bytes": len(r["body"] or b""),
                                        "feed": feed})
            f = feed
            mark = (f"{f['future']:>4} upcoming" if f and f["live"]
                    else "empty/stale" if f else "not a calendar")
            print(f"  [{i}/{len(known)}] {mark:<20} {rec['host'][:44]}")

    save(args.city, payload)
    print(f"\nwrote {out_path(args.city)}")
    report(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
