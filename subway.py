#!/usr/bin/env python3
"""Subway stations -- the way New Yorkers actually describe where something is.

Nobody says "Bushwick (East)". They say "off the Jefferson St L". A listing
index that knows neighbourhoods but not trains is describing the city in a
vocabulary its readers do not use.

Source is MTA Subway Stations on the state portal (`data.ny.gov/39hk-dx4f`),
which is the current canonical one -- the old NYC Open Data station file is
gone (404). It carries `stop_name`, `daytime_routes` (space-separated, e.g.
"N W"), `line`, `borough`, GTFS coordinates and ADA flags.

Two things this writes, both consumed at build time and neither computed in the
browser:

  * `cities/<city>/subway.json` -- the station register: name, routes, point,
    plus `lines`: every line's stops in the order you ride them.
  * Nothing else. Nearest-station is derived in `normalize.py` from these
    points, so it stays consistent with however venues got placed.

The station feed knows which trains serve a stop but not what comes next, and
a list of stops in no particular order is a directory, not a subway line. Order
comes from the MTA's GTFS schedule (`trips` + `stop_times`), which is the only
published statement of what follows what.

ON THE SCHEDULE FETCH, and why it skips robots.txt:

The schedule is published as a zip on an S3 bucket, which is where the MTA's
own developer page sends you. Asking that bucket for /robots.txt gets a 403
with an S3 `AccessDenied` body, because the key does not exist and listing the
bucket is not public. This pipeline reads a 403 there as "the whole host is off
limits" -- the right default, since on a normal site that is what it means, but
here it is a storage bucket answering a question about a file it does not have,
not a statement about crawlers.

So this one call passes `check_robots=False`, for a single known URL that the
publisher documents as the way to get the data, fetched once per register
rebuild rather than crawled. Everything else here goes through the check.

Distances are computed on an equirectangular approximation rather than the
haversine. At NYC's latitude, over the ~1km distances that matter for "is there
a train near this", the error is centimetres, and it avoids a trig call per
station per venue.

    python3 subway.py                # fetch and write the register
    python3 subway.py --report       # what is on disk, no network
"""
import argparse
import collections
import csv
import io
import json
import math
import os
import sys
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
HOST = "data.ny.gov"
DATASET = "39hk-dx4f"

GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip"

# Southbound. Every trip in the feed carries a direction, and taking one of them
# is what turns a set of stops into a sequence. 1 is the railroad's "south",
# which for the crosstown lines means the direction the map is drawn in: the L
# from 8 Av out to Canarsie, the 7 from Flushing in to Hudson Yards.
SOUTH = "1"

# Express variants are not separate lines to a rider -- the 6X is a 6 that skips
# stops, not a train you can catch somewhere the 6 does not go. Folded into the
# parent, whose local pattern is the complete one.
FOLD = {"FX": "F", "6X": "6", "7X": "7"}

BOROUGH = {"M": "Manhattan", "Bk": "Brooklyn", "B": "Brooklyn", "Q": "Queens",
           "Bx": "Bronx", "X": "Bronx", "SI": "Staten Island", "R": "Staten Island"}

# Degrees -> metres at NYC's latitude. Latitude is ~constant; longitude shrinks
# by cos(lat), which at 40.7 degrees is a 24% difference -- large enough that
# ignoring it would rank an east-west neighbour as closer than it is.
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(40.7))


def meters_between(lat1, lon1, lat2, lon2):
    dy = (lat1 - lat2) * M_PER_DEG_LAT
    dx = (lon1 - lon2) * M_PER_DEG_LON
    return math.hypot(dx, dy)


def fetch(delay=1.0):
    rows, offset = [], 0
    while True:
        url = (f"https://{HOST}/resource/{DATASET}.json"
               f"?$limit=500&$offset={offset}&$order=:id")
        batch = http.get_json(url, delay=delay)
        rows.extend(batch)
        if len(batch) < 500:
            break
        offset += 500

    out = []
    for r in rows:
        try:
            lat = float(r["gtfs_latitude"])
            lon = float(r["gtfs_longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        routes = [x for x in (r.get("daytime_routes") or "").split() if x]
        out.append({
            "id": r.get("gtfs_stop_id"),
            "complex_id": r.get("complex_id"),
            "name": r.get("stop_name"),
            "routes": routes,
            "line": r.get("line"),
            "borough": BOROUGH.get(r.get("borough"), r.get("borough")),
            "lat": lat, "lon": lon,
            "ada": r.get("ada") in ("1", "2", 1, 2),
        })
    return out


def dedupe(stations):
    """One entry per physical station, with every route that serves it.

    The feed is one row per GTFS stop, so a complex like Times Sq-42 St appears
    several times with different route lists. Merging on `complex_id` is what
    makes "nearest station" mean a place a rider would name, and what lets the
    route list be complete rather than whichever platform happened to be
    closest.
    """
    merged = {}
    for s in stations:
        key = s["complex_id"] or s["id"]
        m = merged.get(key)
        if not m:
            # `stops` keeps every GTFS id that merged in here. The schedule
            # names platforms, not complexes, so without this there is no way
            # back from a stop in `stop_times` to the station a rider picked --
            # and the id we keep is whichever platform the feed listed first,
            # which for 14 St-Union Sq is the Lexington Av one even to somebody
            # waiting for the L.
            merged[key] = dict(s, routes=list(s["routes"]), stops=[s["id"]])
            continue
        for r in s["routes"]:
            if r not in m["routes"]:
                m["routes"].append(r)
        if s["id"] not in m["stops"]:
            m["stops"].append(s["id"])
        # Keep the northern/western-most point as the nominal one; any platform
        # is within a block, so this only needs to be stable, not optimal.
        if (s["lat"], s["lon"]) < (m["lat"], m["lon"]):
            m["lat"], m["lon"] = s["lat"], s["lon"]
        m["ada"] = m["ada"] or s["ada"]
    for m in merged.values():
        m["routes"].sort()
        m["stops"].sort()
    return sorted(merged.values(), key=lambda s: (s["borough"] or "", s["name"] or ""))


def linearise(patterns):
    """One running order from many stopping patterns.

    No single trip visits every stop on a line. The A alone runs to Far
    Rockaway, to Lefferts Blvd, and to Rockaway Park, and whichever of those
    you picked as "the" A would silently lose the dozen stations on the others.
    So every pattern contributes edges to a graph of what-follows-what, and the
    line is that graph flattened.

    Kahn's algorithm, taking whichever available stop sits earliest along the
    line by its average position across the patterns that contain it. That
    tie-break is what decides where a branch goes: without it the order is
    arbitrary among stops that never share a trip, and Lefferts Blvd could land
    in the middle of the Rockaways.
    """
    nxt = collections.defaultdict(set)
    indeg = collections.Counter()
    where = collections.defaultdict(list)
    for stops in patterns:
        span = max(1, len(stops) - 1)
        for i, s in enumerate(stops):
            where[s].append(i / span)
            indeg.setdefault(s, 0)
        for a, b in zip(stops, stops[1:]):
            if b not in nxt[a]:
                nxt[a].add(b)
                indeg[b] += 1

    rank = {s: sum(v) / len(v) for s, v in where.items()}
    ready = sorted([s for s in indeg if not indeg[s]], key=lambda s: rank[s])
    out = []
    while ready:
        s = ready.pop(0)
        out.append(s)
        for b in sorted(nxt[s], key=lambda b: rank[b]):
            indeg[b] -= 1
            if not indeg[b]:
                ready.append(b)
        ready.sort(key=lambda s: rank[s])
    # A cycle would mean the schedule says A precedes B and B precedes A, which
    # southbound it cannot. Appending the remainder keeps every stop rather than
    # dropping it, and the count in --report is what would show it.
    if len(out) < len(indeg):
        out += [s for s in sorted(indeg, key=lambda s: rank[s]) if s not in set(out)]
    return out


def gtfs_lines(delay=1.0):
    """Every line's stops in riding order, from the published schedule.

    Returns `{route_id: {"name": ..., "route": bullet, "stops": [...]}}` keyed
    by GTFS route, with platform ids resolved to their parent station.
    """
    # check_robots=False ONLY here: see the module docstring. The bucket 403s
    # on a robots.txt it does not have, which is not a crawl policy.
    raw = http.get(GTFS_URL, delay=delay, timeout=120, check_robots=False)
    z = zipfile.ZipFile(io.BytesIO(raw))

    def rows(name):
        return csv.DictReader(io.TextIOWrapper(z.open(name), "utf-8"))

    # The schedule stops at platforms ("101S"); the register knows stations
    # ("101"). parent_station is the join.
    parent = {r["stop_id"]: r["parent_station"] for r in rows("stops.txt")
              if r["parent_station"]}

    meta, keep = {}, {}
    for r in rows("routes.txt"):
        rid = FOLD.get(r["route_id"], r["route_id"])
        meta.setdefault(rid, {"name": r["route_long_name"],
                              "route": r["route_short_name"]})
    for r in rows("trips.txt"):
        if r["direction_id"] == SOUTH:
            keep[r["trip_id"]] = FOLD.get(r["route_id"], r["route_id"])

    # stop_times is 36 MB and the rows of a trip are contiguous, so it is read
    # as a stream and reduced to distinct patterns -- there are a few thousand
    # trips per line and a handful of shapes among them.
    pats = collections.defaultdict(set)
    trip, seq = None, []

    def flush():
        if trip in keep and seq:
            pats[keep[trip]].add(tuple(seq))

    for r in rows("stop_times.txt"):
        if r["trip_id"] != trip:
            flush()
            trip, seq = r["trip_id"], []
        if trip in keep:
            sid = r["stop_id"]
            seq.append(parent.get(sid, sid))
    flush()

    return {rid: dict(meta.get(rid, {"name": rid, "route": rid}),
                      stops=linearise(p)) for rid, p in pats.items()}


def attach_lines(stations, gtfs):
    """Join the schedule's order to the register's membership.

    The two feeds disagree about what a line is, and the register is the one to
    believe. The schedule carries put-ins, lay-ups and late-night reroutes, so
    merging its patterns wholesale gives the W forty-seven stops -- the W serves
    twenty-three. Which trains serve a station is `daytime_routes`, which is
    already what the bullets on the site say; the schedule is asked only what
    order they come in.
    """
    key_of = {sid: s["id"] for s in stations for sid in s["stops"]}
    routes_at = {s["id"]: s["routes"] for s in stations}

    out = []
    for rid, line in sorted(gtfs.items()):
        seen, stops = set(), []
        for sid in line["stops"]:
            key = key_of.get(sid)
            if key is None or key in seen:
                continue
            if line["route"] not in routes_at[key]:
                continue
            seen.add(key)
            stops.append(key)
        if stops:
            out.append({"id": rid, "route": line["route"], "name": line["name"],
                        "stations": stops})
    return out


class Stations:
    """Nearest-station lookup over the register."""

    def __init__(self, stations):
        self.stations = stations

    def __len__(self):
        return len(self.stations)

    def nearest(self, lat, lon, limit=1, max_m=1600):
        """Closest stations within `max_m`, nearest first.

        1600 m is a mile -- past that, "nearest subway" stops being useful
        information and starts being a technically-true fact about Breezy
        Point. Beyond the cutoff the honest answer is nothing.
        """
        if lat is None or lon is None:
            return []
        out = []
        for s in self.stations:
            d = meters_between(lat, lon, s["lat"], s["lon"])
            if d <= max_m:
                out.append((d, s))
        out.sort(key=lambda t: t[0])
        return [{"id": s["id"], "name": s["name"], "routes": s["routes"],
                 "meters": round(d)} for d, s in out[:limit]]


def load(city):
    path = os.path.join(ROOT, "cities", city, "subway.json")
    try:
        with open(path) as f:
            return Stations(json.load(f)["stations"])
    except Exception:
        return Stations([])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    path = os.path.join(ROOT, "cities", args.city, "subway.json")
    if args.report:
        st = load(args.city)
        print(f"{len(st)} stations on disk")
        for s in st.stations[:15]:
            print(f"   {s['name'][:34]:<34} {','.join(s['routes']):<14} {s['borough']}")
        try:
            with open(path) as f:
                lines = json.load(f).get("lines", [])
        except Exception:
            lines = []
        print(f"{len(lines)} lines")
        for ln in lines:
            ends = f"{ln['stations'][0]} .. {ln['stations'][-1]}"
            print(f"   {ln['route']:<4} {len(ln['stations']):>3} stops  "
                  f"{ln['name'][:30]:<30} {ends}")
        return 0

    raw = fetch(args.delay)
    stations = dedupe(raw)
    lines = attach_lines(stations, gtfs_lines(args.delay))

    # Every station should sit on a line. One that does not is unreachable from
    # the line index -- a stop nobody can browse to -- so it is worth saying out
    # loud rather than discovering as a hole in the page.
    placed = {k for ln in lines for k in ln["stations"]}
    orphans = [s for s in stations if s["id"] not in placed]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                   "source": f"{HOST}/{DATASET}", "schedule": GTFS_URL,
                   "attribution": "Metropolitan Transportation Authority",
                   "count": len(stations), "lines": lines,
                   "stations": stations}, f, indent=1)
    print(f"{len(raw)} GTFS stops -> {len(stations)} stations -> {path}")
    routes = sorted({r for s in stations for r in s["routes"]})
    print(f"routes seen ({len(routes)}): {' '.join(routes)}")
    print(f"{len(lines)} lines, {sum(len(ln['stations']) for ln in lines)} stops in order")
    if orphans:
        print(f"{len(orphans)} stations on no line:")
        for s in orphans:
            print(f"   {s['name'][:34]:<34} {','.join(s['routes'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
