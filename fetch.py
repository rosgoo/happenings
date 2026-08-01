#!/usr/bin/env python3
"""Extract -- pull each source as-is and keep a copy.

Writes raw/<city>/<source-id>.json. Nothing is parsed here beyond paging; the
raw copy is preserved so every downstream parser change can be re-applied with
zero network. Never trust the source to still be there tomorrow.

    python3 fetch.py                 # every enabled source for nyc
    python3 fetch.py --city nyc
    python3 fetch.py --source nyc-parks-upcoming
    python3 fetch.py --neighborhoods # one-off: fetch NTA polygons
"""
import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
PAGE = 1000
MAX_PAGES = 60


def slugify(s):
    out = []
    for ch in (s or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_/&,'":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def city_dir(city):
    return os.path.join(ROOT, "cities", city)


def load_city(city):
    with open(os.path.join(city_dir(city), "city.json")) as f:
        return json.load(f)


def load_sources(city):
    with open(os.path.join(city_dir(city), "sources.json")) as f:
        return json.load(f)["sources"]


def fetch_socrata(src, horizon_days=120):
    """Page a Socrata dataset. Filters to upcoming rows where the source
    exposes a date field worth filtering on -- the permitted-events set holds
    two years of history and we only ever render forward."""
    base = f"https://{src['host']}/resource/{src['dataset']}.json"
    where = None
    field = src.get("where_field") or src.get("map", {}).get("start_date")
    if field:
        floor = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        ceil = (datetime.now(timezone.utc) + timedelta(days=horizon_days)).strftime("%Y-%m-%dT00:00:00")
        where = f"{field} > '{floor}' AND {field} < '{ceil}'"

    rows, offset = [], 0
    for _ in range(MAX_PAGES):
        q = {"$limit": PAGE, "$offset": offset}
        if where:
            q["$where"] = where
        url = base + "?" + urllib.parse.urlencode(q)
        batch = http.get_json(url, delay=1.0)
        if not isinstance(batch, list):
            raise RuntimeError(f"unexpected payload from {src['id']}: {str(batch)[:200]}")
        rows.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
        print(f"    …{len(rows)}")
    return rows


ADAPTERS = {"socrata": fetch_socrata}


def fetch_neighborhoods(city):
    """One-off. NTA polygons change on the decade, not the day, so this is not
    part of the regular run -- it writes a file that then just sits there."""
    cfg = load_city(city)["neighborhoods"]
    print(f"neighbourhoods: {cfg['host']}/{cfg['dataset']}")
    rows = []
    offset = 0
    while True:
        url = (f"https://{cfg['host']}/resource/{cfg['dataset']}.json"
               f"?$limit=500&$offset={offset}")
        batch = http.get_json(url, delay=1.0)
        rows.extend(batch)
        if len(batch) < 500:
            break
        offset += 500

    feats, seen = [], set()
    for r in rows:
        name = r.get(cfg["name_field"])
        geom = r.get(cfg["geom_field"])
        if not name or not geom:
            continue
        # Keep EVERY NTA type, not just residential. Type 0 is a residential
        # neighbourhood; 9 is a park, 7 a cemetery, 6 a special district. An
        # earlier pass filtered to type 0 and neighbourhood coverage collapsed,
        # because park events are by definition inside park polygons -- and
        # Prospect Park is a perfectly good edition for an events site, arguably
        # a better one than most. The type is recorded so the site can decide.
        slug = slugify(name)
        if slug in seen or not slug:
            continue
        seen.add(slug)
        feats.append({
            "type": "Feature",
            "properties": {"name": name, "slug": slug,
                           "borough": r.get(cfg["borough_field"]),
                           "nta_type": r.get("ntatype")},
            "geometry": geom,
        })

    out = os.path.join(city_dir(city), "neighborhoods.geojson")
    with open(out, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    size = os.path.getsize(out) / 1e6
    print(f"  wrote {len(feats)} neighbourhoods -> {out} ({size:.1f} MB)")


def fetch_parks(city):
    """One-off. Park polygons are the thing that places permitted events: that
    feed names a park rather than giving coordinates, and parks do not move."""
    cfg = load_city(city)["parks"]
    print(f"parks: {cfg['host']}/{cfg['dataset']}")
    rows, offset = [], 0
    while True:
        url = (f"https://{cfg['host']}/resource/{cfg['dataset']}.json"
               f"?$limit=500&$offset={offset}")
        batch = http.get_json(url, delay=1.0)
        rows.extend(batch)
        if len(batch) < 500:
            break
        offset += 500

    feats = []
    for r in rows:
        geom = r.get(cfg["geom_field"])
        if not geom:
            continue
        names = []
        for f in cfg["name_fields"]:
            v = (r.get(f) or "").strip()
            if v and v not in names:
                names.append(v)
        if not names:
            continue
        feats.append({
            "type": "Feature",
            "properties": {"names": names, "borough": r.get(cfg["borough_field"]),
                           "typecategory": r.get("typecategory")},
            "geometry": geom,
        })
    out = os.path.join(city_dir(city), "parks.geojson")
    with open(out, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    print(f"  wrote {len(feats)} park properties -> {out} "
          f"({os.path.getsize(out) / 1e6:.1f} MB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--source", help="fetch only this source id")
    ap.add_argument("--neighborhoods", action="store_true")
    ap.add_argument("--parks", action="store_true")
    ap.add_argument("--horizon", type=int, default=120, help="days forward to fetch")
    args = ap.parse_args()

    if args.neighborhoods:
        fetch_neighborhoods(args.city)
        return 0
    if args.parks:
        fetch_parks(args.city)
        return 0

    raw_dir = os.path.join(ROOT, "raw", args.city)
    os.makedirs(raw_dir, exist_ok=True)

    log, failures = {}, 0
    for src in load_sources(args.city):
        if not src.get("enabled"):
            continue
        if args.source and src["id"] != args.source:
            continue
        print(f"{src['id']}  ({src['label']})")
        try:
            rows = ADAPTERS[src["adapter"]](src, args.horizon)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            log[src["id"]] = {"ok": False, "error": f"{type(e).__name__}: {e}", "count": 0}
            failures += 1
            continue

        path = os.path.join(raw_dir, f"{src['id']}.json")
        with open(path, "w") as f:
            json.dump({"source": src, "fetched_at": datetime.now(timezone.utc).isoformat(),
                       "rows": rows}, f)
        print(f"  {len(rows)} rows -> {path}")
        log[src["id"]] = {"ok": True, "count": len(rows),
                          "fetched_at": datetime.now(timezone.utc).isoformat()}

    os.makedirs(os.path.join(ROOT, "data", args.city), exist_ok=True)
    with open(os.path.join(ROOT, "data", args.city, "fetch-log.json"), "w") as f:
        json.dump(log, f, indent=2)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
