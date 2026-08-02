#!/usr/bin/env python3
"""Stop guessing which plugin a venue runs -- ask its CMS to list them.

The probing rules live in `lib/registry.py`, which `census.py` now also uses;
this script is the sweep and the report around them. It does three things the
census does not:

  1. Verifies. `/wp-json/wp/v2/types` lists types whether or not they answer, so
     a registry hit overstates. Every hit is re-asked at
     `/wp-json/wp/v2/{rest_base}` and only counts if rows come back.
  2. Grades. An answering endpoint is not a feed. Only a payload carrying plugin
     date meta -- `_EventStartDate`, `WooCommerceEventsDate`,
     `_eventorganiser_schedule_start_start` -- states when the event runs. The
     top-level `date` is when the web page was published.
  3. Writes the fetch registry. `cities/<city>/wordpress.json` names the venues
     with a feed worth subscribing to, which `fetch.py` then reads. Discovery
     and fetching are separate jobs on separate clocks, the same bargain
     `squarespace.py` and `luma.py` already make.

The Tribe re-test is the reason this exists. `coverage.md` recorded "Tribe REST
is not the backbone -- 0/10" from ten venues that, by its own last paragraph,
did not run the plugin. Asked of the sites that do: 21 of 33 answer.

    python3 posttypes.py                 # sweep, verify, write the registry
    python3 posttypes.py --no-registry   # sweep only
    python3 posttypes.py --report        # re-print the tables, no network
"""
import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import registry  # noqa: E402
from census import load_existing  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def probe(site, delay):
    """One site: what types exist, which answer, and with what dates."""
    origin = "https://" + site["host"]
    rec = {"host": site["host"], "name": site["name"],
           "wp_types": registry.wp_types(origin, delay),
           "drupal_types": None, "open": [], "tribe": None, "ics": None}

    if not rec["wp_types"]:
        rec["drupal_types"] = registry.drupal_types(origin, delay)
        return rec

    for slug, base in sorted(rec["wp_types"].items()):
        hit = registry.wp_collection(origin, base, delay)
        if hit:
            hit["slug"] = slug
            rec["open"].append(hit)

    # Tribe keeps its dates off wp/v2 and serves the whole event on its own
    # route, so a site can look dateless above and be a complete feed here.
    if "tribe_events" in rec["wp_types"]:
        rec["tribe"] = registry.tribe_rest(origin, delay)
        rec["ics"] = registry.tribe_ics(origin, delay,
                                        listing=site.get("listing_url"))
    return rec


# ------------------------------------------------------------ fetch registry

def _host(url):
    net = urllib.parse.urlsplit(url or "").netloc.lower()
    return net[4:] if net.startswith("www.") else net


def coordinates(city):
    """Two indexes into the committed places file, because one does not join.

    Not `raw/osm-places.json`: raw/ is gitignored and CI only caches it for the
    pipeline job, so a discovery run finds nothing there. places.json is
    committed and has already been through the neighbourhood resolver.

    But its `osm_id` is `"way/604729335"` while the census stores the bare int,
    so the obvious join matches zero of 798 rows. Index on the numeric part AND
    on the website host: the ids agree where both exist, and the host is the
    semantically correct key anyway -- it says this website belongs to this
    building, which is the claim being made.
    """
    path = os.path.join(ROOT, "data", city, "places.json")
    try:
        with open(path) as f:
            places = json.load(f)
    except FileNotFoundError:
        return {}, {}
    by_osm, by_host = {}, {}
    for p in places:
        if p.get("lat") is None:
            continue
        try:
            by_osm[int(str(p["osm_id"]).split("/")[-1])] = p
        except (KeyError, ValueError, TypeError):
            pass
        h = _host(p.get("website"))
        if h:
            by_host.setdefault(h, p)
    return by_osm, by_host


def build_registry(city, rows, sites):
    """The venues worth subscribing to, and by which route.

    Tribe REST beats ICS wherever both work: it carries the venue object,
    categories and cost, and it pages. ICS carries dates and text. A site with
    neither is not in here at all -- a registry of endpoints that return nothing
    is how `ical.py` ended up re-fetching 11 feeds that serve zero bytes.

    Coordinates ride along because these venues are buildings whose location we
    already know from OSM. Without them the NYC test in `normalize.py` has
    nothing to test, and the roster includes Hoboken, Newark and the
    Meadowlands -- the census swept an OSM bounding box, not a city.
    """
    sites_by_host = {s["host"]: s for s in sites}
    by_osm, by_host = coordinates(city)
    out = []
    for r in rows:
        tribe_ok = isinstance(r.get("tribe"), int) and r["tribe"] > 0
        ics_ok = bool(r.get("ics") and r["ics"]["vevents"] > 0)
        if not (tribe_ok or ics_ok):
            continue
        site = sites_by_host.get(r["host"], {})
        place = (by_osm.get(site.get("osm_id"))
                 or by_host.get(_host("https://" + r["host"])) or {})
        entry = {
            "host": r["host"],
            "name": r["name"],
            "origin": "https://" + r["host"],
            "lat": place.get("lat"),
            "lon": place.get("lon"),
            "borough": place.get("borough"),
        }
        if tribe_ok:
            entry["feed"] = "tribe"
            entry["url"] = f"https://{r['host']}/wp-json/tribe/events/v1/events"
            entry["events"] = r["tribe"]
        else:
            entry["feed"] = "ics"
            entry["url"] = r["ics"]["url"]
            entry["events"] = r["ics"]["vevents"]
        out.append(entry)
    out.sort(key=lambda e: (-e["events"], e["name"]))
    return out


def registry_path(city):
    return os.path.join(ROOT, "cities", city, "wordpress.json")


def save_registry(city, venues):
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "city": city, "venues": venues}
    with open(registry_path(city), "w") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")
    located = sum(1 for v in venues if v["lat"] is not None)
    print(f"\nregistry: {len(venues)} venues -> {registry_path(city)}")
    print(f"  {sum(1 for v in venues if v['feed'] == 'tribe')} tribe REST · "
          f"{sum(1 for v in venues if v['feed'] == 'ics')} ICS · "
          f"{located} with coordinates")
    if located < len(venues):
        print(f"  {len(venues) - located} have no OSM location and will be "
              f"placed by name or dropped as outside NYC")


# ------------------------------------------------------------------- report

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
    feeds = [s for s in live if any(o["event_date_meta"] for o in s["open"])]
    endpoints = sum(len(s["open"]) for s in live)
    total = sum(int(o["total"]) for s in live for o in s["open"]
                if o.get("total") and str(o["total"]).isdigit())

    print(f"\nprobed              {len(sites)}")
    print(f"registry hit        {len(reg)}   a type exists")
    print(f"endpoint answers    {len(live)}   {endpoints} endpoints, {total:,} rows")
    print(f"carries event date  {len(feeds)}   the date is in the payload")
    print("\nOnly the last number is a feed. The others are web pages.\n")

    for s in sorted(feeds, key=lambda s: s["name"]):
        for o in s["open"]:
            if o["event_date_meta"]:
                print(f"  {s['name'][:38]:40} {o['base']:22} "
                      f"n={str(o['total'] or o['returned']):>5}  "
                      f"{o['event_date_meta'][:2]}")

    tribe = [s for s in sites if s.get("wp_types")
             and "tribe_events" in s["wp_types"]]
    if tribe:
        rest = [s for s in tribe if isinstance(s["tribe"], int) and s["tribe"] > 0]
        ics = [s for s in tribe if s["ics"] and s["ics"]["vevents"] > 0]
        print(f"\nThe Events Calendar, on the {len(tribe)} sites that run it:")
        print(f"  tribe REST answering   {len(rest)}/{len(tribe)}")
        print(f"  ICS with >=1 VEVENT    {len(ics)}/{len(tribe)}")

    drupal = [s for s in sites if s.get("drupal_types")]
    if drupal:
        print(f"\nDrupal JSON:API ({len(drupal)}):")
        for s in drupal:
            print(f"  {s['name'][:38]:40} {s['drupal_types']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--delay", type=float, default=1.0, help="per-host seconds")
    ap.add_argument("--limit", type=int, help="probe only the first N")
    ap.add_argument("--no-registry", action="store_true",
                    help="sweep without rewriting cities/<city>/wordpress.json")
    ap.add_argument("--report", action="store_true",
                    help="re-print the tables, no network")
    args = ap.parse_args()

    if args.report:
        report(load(args.city))
        return

    sites = load_existing(args.city)
    live = [s for s in sites if s.get("status") and 200 <= s["status"] < 400]
    if args.limit:
        live = live[:args.limit]
    print(f"probing {len(live)} readable sites", file=sys.stderr)

    rows = []
    for i, site in enumerate(live, 1):
        rows.append(probe(site, args.delay))
        if i % 25 == 0:
            print(f"  {i}/{len(live)}", file=sys.stderr)

    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "probed": len(rows), "sites": rows}
    save(args.city, payload)
    report(payload)

    if not args.no_registry:
        save_registry(args.city, build_registry(args.city, rows, live))


if __name__ == "__main__":
    main()
