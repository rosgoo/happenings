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
from lib import env, http, ics  # noqa: E402

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
        # $order is not optional. Socrata gives no ordering guarantee without
        # it, so offset paging over an unordered query re-reads some rows and
        # never reads others: paging the 2,059-row parks register unordered
        # returned 2,059 rows containing only 1,599 distinct ones, silently
        # losing 22% of the dataset. `:id` is the stable system column.
        q = {"$limit": PAGE, "$offset": offset, "$order": ":id"}
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


def fetch_squarespace(src, horizon_days=120):
    """Pull every discovered Squarespace events collection.

    Discovery is NOT done here -- `squarespace.py` writes the registry of venue
    to collection URL, and this reads it. Slugs change on the scale of a site
    redesign, not a day, so re-deriving them on every run would spend four
    requests per venue to learn what is already committed to the repo.

    One venue's failure is not the run's. This is 100+ independent small sites
    rather than one municipal API, so a dead host is the expected case and gets
    recorded in the row count rather than raising.
    """
    city = src.get("_city", "nyc")
    path = os.path.join(city_dir(city), "squarespace.json")
    try:
        with open(path) as f:
            registry = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"no {path} -- run `python3 squarespace.py` first")

    horizon_ms = (datetime.now(timezone.utc) + timedelta(days=horizon_days)).timestamp() * 1000
    floor_ms = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000

    rows, failed = [], 0
    targets = [(v, c) for v in registry.get("venues", [])
               for c in v.get("collections") or []]
    print(f"    {len(targets)} collections across "
          f"{len({v['host'] for v, _ in targets})} venues")

    for v, coll in targets:
        url = coll["url"]
        if "format=json" not in url:
            url += ("&" if "?" in url else "?") + "format=json"
        try:
            r = http.get_full(url, delay=1.0,
                              accept="application/json,text/javascript;q=0.9,*/*;q=0.8")
            if not r["body"] or "json" not in r["ctype"]:
                failed += 1
                continue
            blob = json.loads(r["body"].decode("utf-8", "replace"))
        except Exception:
            failed += 1
            continue

        # `upcoming`, not `items` -- a Squarespace events collection returns an
        # empty `items` and puts the records here. `past` is deliberately
        # dropped; this is a listings index, not an archive.
        for it in blob.get("upcoming") or []:
            start = it.get("startDate")
            if not isinstance(start, (int, float)) or not (floor_ms < start < horizon_ms):
                continue
            rows.append({
                "venue_host": v["host"],
                "venue_name": v["name"],
                "origin": v["origin"],
                "collection_url": coll["url"],
                "id": it.get("id"),
                "title": it.get("title"),
                "startDate": start,
                "endDate": it.get("endDate"),
                "fullUrl": it.get("fullUrl"),
                "excerpt": it.get("excerpt"),
                "assetUrl": it.get("assetUrl"),
                "location": it.get("location"),
                "tags": it.get("tags") or [],
                "categories": it.get("categories") or [],
            })

    if failed:
        print(f"    {failed} collections unreachable this run")
    return rows


def fetch_ticketmaster(src, horizon_days=30):
    """Ticketmaster Discovery API, sharded by day.

    Two hard limits shape this, and neither is negotiable:

      * **Deep paging stops at 1000 items** (`size * page < 1000`). The NYC DMA
        returns ~4,600 events inside 30 days, so a single query can physically
        only see a fifth of them. Any adapter that pages a broad query and
        reports success is silently dropping four events in five.
      * **5 requests/second, 5000/day.** Generous, but the shard count is what
        spends it, so shards are days rather than hours.

    A day in this market holds ~140 events, comfortably inside the cap with room
    for a bad Saturday, and 30 days costs ~35 requests. If a single day ever did
    exceed 1000 the count is logged rather than silently truncated -- a source
    that quietly stops being complete is the failure mode this whole pipeline is
    built to avoid.

    robots.txt is not consulted. This is a keyed, documented API being used as
    documented, not a crawl of their website -- the two are different acts, and
    `lib/http`'s robots check is aimed at the second.
    """
    key = env.require("TICKETMASTER_API_KEY",
                      "Free key: https://developer-acct.ticketmaster.com/")
    base = "https://app.ticketmaster.com/discovery/v2/events.json"
    horizon = min(horizon_days, src.get("horizon_days", 30))
    size = 200

    rows, seen = [], set()
    start = datetime.now(timezone.utc)
    print(f"    {horizon} days, one shard per day, dma={src.get('dma_id')}")

    for day in range(horizon):
        a = start + timedelta(days=day)
        b = a + timedelta(days=1)
        page = 0
        while True:
            params = {
                "apikey": key, "size": size, "page": page,
                "startDateTime": a.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": b.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sort": "date,asc",
            }
            if src.get("dma_id"):
                params["dmaId"] = src["dma_id"]
            if src.get("state_code"):
                params["stateCode"] = src["state_code"]
            url = base + "?" + urllib.parse.urlencode(params)
            # 5 req/s is the documented ceiling; 0.3s keeps a margin under it.
            # max_bytes is raised because a 200-event page exceeds the default
            # 800 KB read cap and a truncated body is unparseable JSON.
            r = http.get_full(url, delay=0.3, accept="application/json",
                              check_robots=False, max_bytes=8_000_000)
            if r["status"] != 200 or not r["body"]:
                print(f"    {a:%Y-%m-%d} page {page}: status {r['status']}")
                break
            try:
                blob = json.loads(r["body"].decode("utf-8", "replace"))
            except ValueError as e:
                print(f"    {a:%Y-%m-%d} page {page}: unparseable ({e})")
                break

            evs = (blob.get("_embedded") or {}).get("events") or []
            for e in evs:
                if e.get("id") and e["id"] not in seen:
                    seen.add(e["id"])
                    rows.append(e)

            p = blob.get("page") or {}
            total = p.get("totalElements") or 0
            if day == 0 and page == 0 and total > 1000:
                print(f"    WARNING {a:%Y-%m-%d} holds {total} events; "
                      f"deep paging caps at 1000 -- shard narrower than a day")
            page += 1
            if len(evs) < size or page * size >= min(1000, total):
                break

    print(f"    {len(rows)} unique events across {horizon} days")
    return rows


def fetch_luma(src, horizon_days=30):
    """Luma calendars, one published ICS feed each.

    Discovery lives in `luma.py` and its registry is committed; this only
    subscribes. The feeds carry full history -- one calendar returned 257
    VEVENTs for 5 upcoming events -- so the horizon filter is doing real work
    rather than tidying.

    ICS is parsed here rather than in normalize because the raw shape is text,
    not rows, and everything downstream in this pipeline reads JSON. The parsed
    events are stored unmapped: no category, no venue resolution, no renaming.
    That keeps the re-parse bargain intact for the mapping decisions, which are
    the ones that actually churn.
    """
    city = src.get("_city", "nyc")
    path = os.path.join(city_dir(city), "luma.json")
    try:
        with open(path) as f:
            registry = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"no {path} -- run `python3 luma.py` first")

    horizon = min(horizon_days, src.get("horizon_days", 30))
    cals = registry.get("calendars") or []
    print(f"    {len(cals)} calendars, horizon {horizon}d")

    rows, failed = [], 0
    for c in cals:
        url = f"https://api.lu.ma/ics/get?entity=calendar&id={c['id']}"
        try:
            r = http.get_full(url, delay=1.0, accept="text/calendar,*/*;q=0.8",
                              check_robots=False, max_bytes=8_000_000)
            if not r["body"]:
                failed += 1
                continue
            items = ics.upcoming(ics.events(r["body"].decode("utf-8", "replace")),
                                 horizon_days=horizon)
        except Exception:
            failed += 1
            continue

        for e in items:
            start = e["start"]
            rows.append({
                "calendar_id": c["id"],
                "calendar_name": c.get("name"),
                "uid": e["uid"],
                "summary": e["summary"],
                "description": e["description"],
                "location": e["location"],
                "organizer": e["organizer"],
                "status": e["status"],
                "lat": e["lat"], "lon": e["lon"],
                "all_day": e["all_day"],
                "start": start.isoformat() if start else None,
                "end": e["end"].isoformat() if e["end"] else None,
            })

    if failed:
        print(f"    {failed} calendars unreachable this run")
    print(f"    {len(rows)} events inside the horizon")
    return rows


ADAPTERS = {"socrata": fetch_socrata, "squarespace": fetch_squarespace,
            "ticketmaster": fetch_ticketmaster, "luma": fetch_luma}


def fetch_neighborhoods(city):
    """One-off. NTA polygons change on the decade, not the day, so this is not
    part of the regular run -- it writes a file that then just sits there."""
    cfg = load_city(city)["neighborhoods"]
    print(f"neighbourhoods: {cfg['host']}/{cfg['dataset']}")
    rows = []
    offset = 0
    while True:
        url = (f"https://{cfg['host']}/resource/{cfg['dataset']}.json"
               f"?$limit=500&$offset={offset}&$order=:id")
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
               f"?$limit=500&$offset={offset}&$order=:id")
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
        src.setdefault("_city", args.city)
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
