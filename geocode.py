#!/usr/bin/env python3
"""Place the events that arrive without coordinates.

The permitted-events feed names a place -- "Central Park", "Coffey Park",
"5 Avenue between 3 Street and 4 Street" -- and gives no coordinates, which left
80% of events with a borough and nothing finer. Neighbourhood is the spine of
the site, so this is the stage that earns it.

Two resolvers, in strict order of authority:

  1. PARK NAME  -> NYC Parks Properties polygons. The overwhelming majority of
     unplaced events are in named parks (Central Park alone is 671), and the
     city publishes an authoritative polygon for each. This is a lookup against
     a definitive register, not a guess.
  2. STREET     -> NYC Planning Labs GeoSearch, keyless, and only for
     address-shaped strings no park name matched. Results below the configured
     confidence floor are discarded rather than accepted weakly.

Matching is EXACT after normalisation. No edit distance, no nearest-neighbour,
no "close enough" -- that is where a directory starts being confidently wrong.
Anything unresolved goes to a review CSV, never silently into the data.

This is the enrich.py pattern: expensive, cached, resumable. Re-running after a
fresh normalize costs nothing and makes no requests.

    python3 geocode.py                # resolve what isn't cached
    python3 geocode.py --no-street    # parks only, zero network
    python3 geocode.py --refresh      # ignore the cache and redo everything
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402
from lib.geo import _in_ring, _rings  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def norm(s):
    """Lowercase, strip punctuation and collapse whitespace. Deliberately does
    NOT stem 'Park'/'Playground' -- 'Marine Park' and 'Marine Playground' are
    different properties and collapsing them would invent a location."""
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def interior_point(geom):
    """A point guaranteed to sit inside the polygon.

    The centroid of a C-shaped or multi-part park can land outside it -- and a
    point outside the park lands in the wrong neighbourhood, which is the exact
    failure this whole stage exists to avoid. So: take the largest ring's
    centroid, verify containment, and fall back to a scanline midpoint if it
    misses.
    """
    rings = _rings(geom)
    if not rings:
        return None
    outer = max((r[0] for r in rings), key=len)
    n = len(outer)
    cx = sum(p[0] for p in outer) / n
    cy = sum(p[1] for p in outer) / n
    if _in_ring(cx, cy, outer):
        return cy, cx

    ys = sorted(p[1] for p in outer)
    y = ys[len(ys) // 2]
    xs = []
    for i in range(n):
        x1, y1 = outer[i][0], outer[i][1]
        x2, y2 = outer[(i + 1) % n][0], outer[(i + 1) % n][1]
        if (y1 > y) != (y2 > y):
            xs.append(x1 + (y - y1) * (x2 - x1) / ((y2 - y1) or 1e-16))
    xs.sort()
    if len(xs) >= 2:
        return y, (xs[0] + xs[1]) / 2
    return cy, cx


class Parks:
    """Name -> interior point, from the city's own park register."""

    def __init__(self, path):
        self.by_name = {}
        with open(path) as f:
            fc = json.load(f)
        for feat in fc["features"]:
            pt = interior_point(feat.get("geometry") or {})
            if not pt:
                continue
            props = feat["properties"]
            rec = {"lat": pt[0], "lon": pt[1],
                   "name": props["names"][0], "borough": props.get("borough")}
            for nm in props["names"]:
                self.by_name.setdefault(norm(nm), rec)

    def lookup(self, raw):
        """Try the whole string, then the documented shapes it arrives in."""
        cands = [raw]
        # "Langston Hughes Playground (St. Nicholas Playground North)" -- the
        # register may hold either the sign name or the parenthetical.
        m = re.match(r"^(.*?)\s*\((.+)\)\s*$", raw)
        if m:
            cands += [m.group(1), m.group(2)]
        # "Pelham Bay Park and Orchard Beach" -- two properties, one string.
        if " and " in raw.lower():
            cands.append(re.split(r"\s+and\s+", raw, 1, flags=re.I)[0])
        for c in cands:
            hit = self.by_name.get(norm(c))
            if hit:
                return hit
        return self.contains(raw)

    def contains(self, raw):
        """Unique-substring fallback, in either direction.

        Permits use the name on the sign; the register uses the name on the
        deed. "East River Park" is filed as "John V. Lindsay East River Park",
        and "East River Esplanade 36th-38th" is a stretch of "East River
        Esplanade". Exact matching misses both.

        Only ever returns a UNIQUE match, and only on a multi-word query. Two
        candidates means the name is genuinely ambiguous and the honest answer
        is nothing -- "Marine Park" must never quietly become "Marine Park
        Playground". This is the one inference in the chain, so it is bounded
        as tightly as it can be and still be useful.
        """
        q = norm(raw)
        if len(q.split()) < 2:
            return None
        hits = [rec for key, rec in self.by_name.items()
                if (q in key or key in q) and len(key.split()) >= 2]
        uniq = {(round(r["lat"], 6), round(r["lon"], 6)): r for r in hits}
        return next(iter(uniq.values())) if len(uniq) == 1 else None


STREETY = re.compile(
    r"\bbetween\b|\bbet\.|\bavenue\b|\bstreet\b|\bblvd\b|\bboulevard\b|"
    r"\broad\b|\bplaza\b|\bbroadway\b|^\d+\s|\bave\b|\bst\.?\b", re.I)


def street_query(raw):
    """Turn a permit location into something a geocoder can answer.

    'A between B and C' is not an address; the intersection 'A and B' is.
    """
    m = re.match(r"^(.*?)\s+between\s+(.+?)\s+and\s+(.+)$", raw, re.I)
    if m:
        return f"{m.group(1).strip()} and {m.group(2).strip()}"
    if STREETY.search(raw):
        return raw
    return None


def geosearch(cfg, text, borough=None):
    q = text if not borough else f"{text}, {borough}"
    url = cfg["url"] + "?" + urllib.parse.urlencode({"text": q, "size": 1})
    try:
        data = http.get_json(url, delay=1.1, retries=1, timeout=25)
    except Exception:
        return None
    feats = data.get("features") or []
    if not feats:
        return None
    p = feats[0].get("properties", {})
    conf = p.get("confidence") or 0
    if conf < cfg.get("min_confidence", 0.7):
        return None
    lon, lat = feats[0]["geometry"]["coordinates"]
    return {"lat": lat, "lon": lon, "name": p.get("label"), "confidence": conf}


BOROUGH_FULL = {"M": "Manhattan", "B": "Brooklyn", "Q": "Queens",
                "X": "Bronx", "R": "Staten Island"}


def targets(city):
    """Every location string that arrives without coordinates, with the borough
    the source stated, and how many events ride on it.

    Two shapes, because two kinds of source need placing. Permits name a place
    in `event_location` and give no coordinates. Institutional feeds name a
    building the resolver has never heard of -- "Brooklyn Heights Library" is
    not a park and is not an address -- so their adapter attaches the street
    address the source itself publishes, under `address`. Both arrive here as
    the same question: what are the coordinates of this string.
    """
    raw_dir = os.path.join(ROOT, "raw", city)
    want = {}
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(raw_dir, fn)) as f:
            blob = json.load(f)
        # raw/ also holds inputs that are not source pulls -- osm-places.json is
        # an OSM dump keyed by `elements`. Anything without `rows` is not an
        # event feed and has no locations to place.
        if not isinstance(blob, dict) or "rows" not in blob:
            continue
        for r in blob["rows"]:
            if r.get("coordinates") or r.get("lat"):
                continue
            loc = ((r.get("event_location") or r.get("address") or "")
                   .split(":")[0].strip())
            if not loc:
                continue
            e = want.setdefault(loc, {"n": 0, "borough": r.get("event_borough")})
            e["n"] += 1
    return want


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--no-street", action="store_true",
                    help="parks only; makes zero network requests")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--retry-unresolved", action="store_true",
                    help="re-ask only the locations cached as null, keeping every "
                         "hit already paid for")
    ap.add_argument("--limit", type=int, default=1200,
                    help="cap geocoder requests in one run (it is resumable)")
    args = ap.parse_args()

    cdir = os.path.join(ROOT, "cities", args.city)
    ddir = os.path.join(ROOT, "data", args.city)
    os.makedirs(ddir, exist_ok=True)
    cfg = json.load(open(os.path.join(cdir, "city.json")))
    parks = Parks(os.path.join(cdir, "parks.geojson"))
    print(f"geocode: {args.city} · {len(parks.by_name)} park names indexed")

    cache_path = os.path.join(ddir, "geocode-cache.json")
    cache = {} if args.refresh else (
        json.load(open(cache_path)) if os.path.exists(cache_path) else {})
    if args.retry_unresolved:
        # A cached null means "asked, no confident answer" -- which becomes
        # wrong the moment the matching rules improve. Drop only those.
        dropped = [k for k, v in cache.items() if not v]
        for k in dropped:
            del cache[k]
        print(f"  retrying {len(dropped)} previously unresolved locations")

    todo = targets(args.city)
    print(f"  {len(todo)} distinct unplaced locations "
          f"({sum(t['n'] for t in todo.values())} rows)")

    stats = Counter()
    requests = 0
    for loc, info in sorted(todo.items(), key=lambda kv: -kv[1]["n"]):
        if loc in cache and not args.refresh:
            stats[f"cached:{cache[loc]['source'] if cache[loc] else 'unresolved'}"] += 1
            continue

        hit = parks.lookup(loc)
        if hit:
            cache[loc] = {"lat": hit["lat"], "lon": hit["lon"],
                          "source": "parks-properties", "matched": hit["name"],
                          "confidence": 1.0}
            stats["parks"] += 1
            continue

        q = street_query(loc)
        if q and not args.no_street and requests < args.limit:
            requests += 1
            boro = BOROUGH_FULL.get(info.get("borough"), info.get("borough"))
            g = geosearch(cfg["geocoder"], q, boro)
            if g:
                cache[loc] = {"lat": g["lat"], "lon": g["lon"],
                              "source": "geosearch", "matched": g["name"],
                              "confidence": g["confidence"]}
                stats["geosearch"] += 1
                continue
            stats["geosearch-miss"] += 1
        elif q:
            stats["street-skipped"] += 1
            continue
        else:
            stats["not-addressable"] += 1

        # Explicitly cache the failure so a rerun doesn't re-ask, and so the
        # review file is complete. null means "asked, no confident answer" --
        # which is a different fact from "never looked".
        cache[loc] = None

    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=0, sort_keys=True)

    unresolved = [(loc, todo[loc]["n"], todo[loc].get("borough"))
                  for loc, v in cache.items() if v is None and loc in todo]
    unresolved.sort(key=lambda r: -r[1])
    with open(os.path.join(ddir, "geocode-unresolved.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["location", "events", "borough"])
        w.writerows(unresolved)

    resolved = sum(1 for v in cache.values() if v)
    rows_ok = sum(todo[l]["n"] for l, v in cache.items() if v and l in todo)
    rows_all = sum(t["n"] for t in todo.values()) or 1
    print(f"  resolved {resolved}/{len(todo)} locations "
          f"= {rows_ok}/{rows_all} rows ({100 * rows_ok / rows_all:.1f}%)")
    for k, v in stats.most_common():
        print(f"    {v:>6}  {k}")
    print(f"  {len(unresolved)} unresolved -> data/{args.city}/geocode-unresolved.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
