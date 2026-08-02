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

  * `cities/<city>/subway.json` -- the station register: name, routes, point.
  * Nothing else. Nearest-station is derived in `normalize.py` from these
    points, so it stays consistent with however venues got placed.

Distances are computed on an equirectangular approximation rather than the
haversine. At NYC's latitude, over the ~1km distances that matter for "is there
a train near this", the error is centimetres, and it avoids a trig call per
station per venue.

    python3 subway.py                # fetch and write the register
    python3 subway.py --report       # what is on disk, no network
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
HOST = "data.ny.gov"
DATASET = "39hk-dx4f"

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
            merged[key] = dict(s, routes=list(s["routes"]))
            continue
        for r in s["routes"]:
            if r not in m["routes"]:
                m["routes"].append(r)
        # Keep the northern/western-most point as the nominal one; any platform
        # is within a block, so this only needs to be stable, not optimal.
        if (s["lat"], s["lon"]) < (m["lat"], m["lon"]):
            m["lat"], m["lon"] = s["lat"], s["lon"]
        m["ada"] = m["ada"] or s["ada"]
    for m in merged.values():
        m["routes"].sort()
    return sorted(merged.values(), key=lambda s: (s["borough"] or "", s["name"] or ""))


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
        return 0

    raw = fetch(args.delay)
    stations = dedupe(raw)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                   "source": f"{HOST}/{DATASET}",
                   "attribution": "Metropolitan Transportation Authority",
                   "count": len(stations), "stations": stations}, f, indent=1)
    print(f"{len(raw)} GTFS stops -> {len(stations)} stations -> {path}")
    routes = sorted({r for s in stations for r in s["routes"]})
    print(f"routes seen ({len(routes)}): {' '.join(routes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
