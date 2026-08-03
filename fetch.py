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
import html
import json
import os
import re
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


def fetch_drupal(src, horizon_days=120):
    """A Drupal JSON:API resource, filtered forward and paged by cursor.

    Written for Brooklyn Public Library, whose `/jsonapi` is completely open --
    no key, server-side filtering, cursor pagination, one endpoint for every
    branch in the borough. Configured from sources.json rather than hard-coded
    so the next institution running Drupal costs a config block instead of a
    file, which is not speculative: the census found node--event on other hosts
    and BPL alone justifies the shape.

    THE TIMEZONE IS A LIE, and it is the one thing here that will silently
    corrupt every row if taken at face value. `field_date.value` comes back as
    `2026-08-03T10:00:00+00:00` for an event the site's own URL slug calls
    `...20260803-1000am`. Drupal is serialising a naive local wall-clock and
    stamping `+00:00` on it. Honouring that offset shifts every event four or
    five hours earlier -- a 10am story time lands at 6am, in the wrong time
    band, on a listing whose entire promise is when. So the offset is dropped
    here and the value is passed downstream as the local time it actually is.

    Verified rather than assumed: across twelve consecutive events at three
    distinct times, the hour in `field_date.value` matched the hour in
    `path.alias` every time. If the site ever starts sending real UTC this
    breaks loudly at 4am, which is the failure mode to prefer.
    """
    host = src["host"]
    base = f"https://{host}/jsonapi/{src['resource']}"
    date_field = src.get("date_field", "field_date")
    floor = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    ceil = (datetime.now(timezone.utc)
            + timedelta(days=min(horizon_days, src.get("horizon_days", 60)))
            ).strftime("%Y-%m-%d")

    q = {
        "filter[lo][condition][path]": f"{date_field}.value",
        "filter[lo][condition][operator]": ">",
        "filter[lo][condition][value]": floor,
        "filter[hi][condition][path]": f"{date_field}.value",
        "filter[hi][condition][operator]": "<",
        "filter[hi][condition][value]": ceil,
        "sort": f"{date_field}.value",
        "page[limit]": str(src.get("page_size", 50)),
    }
    if src.get("include"):
        q["include"] = ",".join(src["include"])
    url = base + "?" + urllib.parse.urlencode(q)

    max_pages = src.get("max_pages", MAX_PAGES)
    print(f"    {src['resource']} · {floor} .. {ceil}")
    rows, terms, pages = [], {}, 0
    while url and pages < max_pages:
        # Retry the page, not the fetch. Forty sequential pages against one
        # host will meet a timeout eventually, and losing 1,400 rows already in
        # hand because page 28 blinked is a worse answer than asking again.
        blob = None
        for attempt in range(3):
            # 1s between pages, not 0.5. Sixty sequential pages at half a
            # second made BPL start timing out around page 45 and stop
            # answering entirely by 61 -- which then took the companion branch
            # request down with it and left every event unplaceable. Going
            # slower is what makes the run complete.
            r = http.get_full(url, delay=1.0 + attempt,
                              accept="application/vnd.api+json,application/json;q=0.9",
                              max_bytes=8_000_000)
            if r["status"] == 200 and r["body"]:
                blob = json.loads(r["body"].decode("utf-8", "replace"))
                break
            print(f"    page {pages}: status {r['status']}, retry {attempt + 1}/3")
        if blob is None:
            # Stop, keep what came back, and say so loudly. `min_expected` in
            # sources.json is the gate that decides whether a short run is
            # still a usable one -- that judgement belongs to validate.py, not
            # to a network blip at page 28.
            print(f"    WARNING truncated at page {pages} after 3 attempts; "
                  f"keeping {len(rows)} rows")
            break
        if blob.get("errors"):
            raise RuntimeError(f"{src['id']}: {str(blob['errors'])[:200]}")

        # `included` is a flat sidecar of every referenced entity across the
        # whole page, so it is indexed once and carried across pages -- a
        # branch named on page 1 is still the branch on page 12.
        for i in blob.get("included") or []:
            terms[i["id"]] = {"type": i["type"],
                              "name": (i["attributes"].get("name")
                                       or i["attributes"].get("title"))}

        for it in blob.get("data") or []:
            a = it.get("attributes") or {}
            rel = it.get("relationships") or {}

            def named(field):
                d = (rel.get(field) or {}).get("data")
                if isinstance(d, list):
                    return [terms.get(x["id"], {}).get("name") for x in d
                            if terms.get(x["id"], {}).get("name")]
                if isinstance(d, dict):
                    return terms.get(d["id"], {}).get("name")
                return None

            date = a.get(date_field) or {}
            rows.append({
                "id": it.get("id"),
                "title": a.get("title"),
                "start": date.get("value"),
                "end": date.get("end_value"),
                "body": (a.get("body") or {}).get("summary")
                        or (a.get("body") or {}).get("value"),
                "path": (a.get("path") or {}).get("alias"),
                "virtual": bool(a.get("field_event_virtual")),
                "hybrid": bool(a.get("field_hybrid")),
                "offsite": bool(a.get("field_event_offsite")),
                "offsite_address": a.get("field_event_offsite_address"),
                "location": named("field_location"),
                "branches": named("field_participating_branches"),
                "kind": named("field_event_type_parent"),
                "audience": named("field_age"),
                "tags": named("field_tags") or [],
            })

        url = ((blob.get("links") or {}).get("next") or {}).get("href")
        pages += 1
        if pages % 10 == 0:
            print(f"    …{len(rows)}")

    if url and pages >= max_pages:
        print(f"    WARNING stopped at {max_pages} pages; more remain")

    # A branch name is not a location. "Brooklyn Heights Library" resolves
    # against neither the parks register nor an address geocoder, so every
    # library event would land with a borough and nothing finer -- which is the
    # exact gap geocode.py was written to close for permits. The companion
    # resource states each branch's street address; attach it and the existing
    # geocoder does the rest, once, cached forever.
    if src.get("places"):
        cfg = src["places"]
        addresses = _drupal_places(host, cfg)
        for r in rows:
            r["address"] = (_match_place(addresses, r.get("location"), cfg)
                            or _match_place(addresses, (r.get("branches") or [None])[0], cfg))
        placed = sum(1 for r in rows if r.get("address"))
        print(f"    {len(addresses)} places with an address · "
              f"{placed}/{len(rows)} events matched to one")

    print(f"    {len(rows)} events across {pages} pages")
    return rows


def _drupal_places(host, cfg):
    """name -> street address, from a companion JSON:API resource."""
    url = (f"https://{host}/jsonapi/{cfg['resource']}"
           f"?page%5Blimit%5D={cfg.get('page_size', 50)}")
    field = cfg.get("address_field", "field_address")
    out = {}
    for _ in range(MAX_PAGES):
        r = http.get_full(url, delay=0.5,
                          accept="application/vnd.api+json,application/json;q=0.9",
                          max_bytes=8_000_000)
        if r["status"] != 200 or not r["body"]:
            break
        blob = json.loads(r["body"].decode("utf-8", "replace"))
        for it in blob.get("data") or []:
            a = it.get("attributes") or {}
            addr = a.get(field) or {}
            line = (addr.get("address_line1") or "").strip()
            if not a.get("title") or not line:
                continue
            parts = [line, addr.get("locality"), addr.get("administrative_area"),
                     addr.get("postal_code")]
            # Keyed lowercase: the same building is "DeKalb Library" in the
            # branch list and "Dekalb, Auditorium" in an event.
            out[a["title"].strip().lower()] = ", ".join(p for p in parts if p)
        url = ((blob.get("links") or {}).get("next") or {}).get("href")
        if not url:
            break
    return out


def _match_place(addresses, name, cfg=None):
    """Exact after normalisation, in three tries. Never fuzzy.

    Event locations are room-qualified -- "Central Library, Info Commons Lab" --
    while the companion resource carries both room-level and building-level
    names. Try the full string first so a room with its own address keeps it,
    then the building.

    The third try is the publisher's own naming convention, and it is config
    rather than code because it is a fact about one source: BPL writes an event
    at "Saratoga, Meeting Room" whose branch record is titled "Saratoga
    Library". Verified against the branch list rather than assumed -- Saratoga,
    Leonard, Rugby and Cypress Hills all resolve this way, while Greenpoint and
    Red Hook Interim genuinely have no branch record and stay unplaced, which is
    the correct answer for them.

    Still exact matching, which is the rule geocode.py sets: case folding and a
    stated suffix are normalisation, not edit distance. Nothing here guesses
    which building someone meant.
    """
    if not name:
        return None
    stem = name.strip().split(",")[0].strip()
    tries = [name.strip(), stem]
    for suffix in (cfg or {}).get("name_suffixes") or []:
        if not stem.lower().endswith(suffix.strip().lower()):
            tries.append(stem + suffix)
    for t in tries:
        hit = addresses.get(t.lower())
        if hit:
            return hit
    return None


def fetch_wordpress(src, horizon_days=120):
    """Venue calendars on WordPress, by whichever route that venue answers on.

    Discovery is `posttypes.py`; this only subscribes. Two routes, because the
    plugin decides which one exists:

      tribe  /wp-json/tribe/events/v1/events -- structured, paged, and carries
             a venue object, categories and cost when the venue fills them in.
      ics    the same plugin's calendar export -- dates and text only.

    Tribe REST wins wherever both work. Where neither does, the venue is not in
    the registry at all; a source that answers with nothing is what made the
    earlier ICS sweep look like a 3% format.

    One venue's failure is not the run's. These are ~30 independent small sites,
    so a dead host is expected and gets counted rather than raised.
    """
    city = src.get("_city", "nyc")
    path = os.path.join(city_dir(city), "wordpress.json")
    try:
        with open(path) as f:
            venues = json.load(f)["venues"]
    except FileNotFoundError:
        raise RuntimeError(f"no {path} -- run `python3 posttypes.py` first")

    horizon = min(horizon_days, src.get("horizon_days", 90))
    # A curated `skip` is a decision, not a failure. cityparksfoundation.org
    # publishes SummerStage and It's My Park across the whole city with no
    # per-event location in the feed, so every one of its events would inherit
    # a single wrong point in Central Park. Losing them is honest; placing them
    # is not, and that is the same call luma.py makes on an event with no GEO.
    skipped = [v for v in venues if v.get("skip")]
    venues = [v for v in venues if not v.get("skip")]
    print(f"    {len(venues)} venues · "
          f"{sum(1 for v in venues if v['feed'] == 'tribe')} tribe, "
          f"{sum(1 for v in venues if v['feed'] == 'ics')} ics"
          + (f" · {len(skipped)} skipped by hand" if skipped else ""))
    for v in skipped:
        print(f"      skip {v['name']}: {v.get('why') or 'no reason recorded'}")

    rows, failed = [], 0
    for v in venues:
        try:
            got = (_tribe_rows(v, horizon) if v["feed"] == "tribe"
                   else _ics_rows(v, horizon))
        except Exception:
            failed += 1
            continue
        if not got:
            failed += 1
        rows.extend(got)

    if failed:
        print(f"    {failed} venues returned nothing this run")
    print(f"    {len(rows)} events inside the horizon")
    return rows


def _tribe_rows(v, horizon_days):
    """Page The Events Calendar's own route.

    `start_date`/`end_date` are already local wall-clock strings in the venue's
    timezone, which the payload states -- no conversion, and none wanted.
    """
    ceil = (datetime.now(timezone.utc) + timedelta(days=horizon_days)).strftime("%Y-%m-%d")
    out, page = [], 1
    while page <= 10:
        q = {"per_page": 50, "page": page, "start_date": "now", "end_date": ceil}
        r = http.get_full(v["url"] + "?" + urllib.parse.urlencode(q),
                          delay=1.0, accept="application/json", max_bytes=8_000_000)
        if r["status"] != 200 or "json" not in (r["ctype"] or ""):
            break
        blob = json.loads(r["body"].decode("utf-8", "replace"))
        evs = blob.get("events") or []
        for e in evs:
            venue = e.get("venue") or {}
            out.append({
                "venue_host": v["host"], "venue_name": v.get("venue_name") or v["name"],
                "lat": v.get("lat"), "lon": v.get("lon"),
                "borough": v.get("borough"),
                "feed": "tribe",
                "id": e.get("global_id") or e.get("id"),
                "title": e.get("title"),
                "url": e.get("url"),
                "start": e.get("start_date"),
                "end": e.get("end_date"),
                "all_day": bool(e.get("all_day")),
                "status": e.get("status"),
                "cost": e.get("cost") or None,
                "description": e.get("excerpt") or e.get("description"),
                "categories": [c.get("name") for c in e.get("categories") or []],
                "tags": [t.get("name") for t in e.get("tags") or []],
                # The venue's own address beats the registry's OSM point when
                # it is there -- a theatre that states a second stage knows
                # better than the building we geocoded.
                "venue_address": venue.get("address") if isinstance(venue, dict) else None,
                "venue_lat": venue.get("geo_lat") if isinstance(venue, dict) else None,
                "venue_lon": venue.get("geo_lng") if isinstance(venue, dict) else None,
            })
        if len(evs) < 50 or page >= (blob.get("total_pages") or 1):
            break
        page += 1
    return out


def _ics_rows(v, horizon_days):
    """The plugin's calendar export, for venues whose REST route is off."""
    r = http.get_full(v["url"], delay=1.0, accept="text/calendar,*/*;q=0.8",
                      max_bytes=8_000_000)
    if not r["body"]:
        return []
    items = ics.upcoming(ics.events(r["body"].decode("utf-8", "replace")),
                         horizon_days=horizon_days)
    out = []
    for e in items:
        start = e["start"]
        out.append({
            # `venue_name` overrides the OSM match, same as the tribe path --
            # it was missing here, so a curated name set for a venue that
            # happened to publish ICS rather than REST was silently ignored.
            "venue_host": v["host"], "venue_name": v.get("venue_name") or v["name"],
            "lat": v.get("lat"), "lon": v.get("lon"),
            "borough": v.get("borough"),
            "feed": "ics",
            "id": e["uid"],
            "title": e["summary"],
            "url": e["url"],
            "start": start.isoformat() if start else None,
            "end": e["end"].isoformat() if e["end"] else None,
            "all_day": e["all_day"],
            "status": e["status"],
            "cost": None,
            "description": e["description"],
            # CATEGORIES is the one classification ICS carries, and it is the
            # source's own word rather than ours. Mapped by the same
            # `wordpress:terms` tier that reads Tribe's category objects, so
            # both feeds of this adapter classify from what the venue said.
            "categories": e["categories"], "tags": [],
            "venue_address": e["location"],
            "venue_lat": e["lat"], "venue_lon": e["lon"],
        })
    return out


LD_JSON = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.I | re.S)

# Measured, not guessed: the event block sits ~180 KB into a ~350-395 KB page,
# so anything under ~250 KB truncates it away and the event vanishes with no
# error to notice. The headroom is the point.
DICE_BYTES = 500_000


def clean_ws(s):
    """Collapse whitespace and unescape entities in a markup-derived string.

    JSON-LD embedded in HTML routinely carries newlines and `&amp;` from the
    template that produced it, and a venue name that differs from itself by a
    line break is two venues by the time it reaches a slug.
    """
    if s is None:
        return None
    return re.sub(r"\s+", " ", html.unescape(str(s))).strip() or None


def _one(v):
    """schema.org properties are single-or-many; this takes the one.

    `location`, `organizer` and `geo` are all documented as accepting either an
    object or an array, and DICE uses both -- a co-promoted show carries two
    organizers. Assuming the object shape works on a sample and then dies on
    whichever event happens to be co-promoted, which is exactly how a 1,539-page
    run turns into one AttributeError and no output at all.
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, list):
        return next((x for x in v if isinstance(x, dict)), {})
    return {}


def _dice_event(body):
    """The schema.org Event block from a DICE event page, or None.

    A page carries four ld+json blocks -- Brand, WebSite, and the event -- so
    this selects on @type rather than taking the first. Anything ending in
    "Event" counts: DICE emits MusicEvent for most listings and plain Event for
    the rest, and a comedy or theatre night should not be dropped for being
    marked up more precisely.
    """
    for block in LD_JSON.findall(body):
        try:
            d = json.loads(block)
        except ValueError:
            continue
        if isinstance(d, dict) and _event_type(d):
            return d
    return None


def _ld_image(ev):
    """The event's own artwork URL, or None.

    schema.org `image` is single-or-many like everything else here, and it also
    admits an `ImageObject` where most sites write a bare string -- so all three
    shapes arrive in practice and only the URL is wanted. Hotlinked rather than
    mirrored, so this is the address of somebody else's file and not a copy of
    it; see `add`.
    """
    img = ev.get("image")
    if isinstance(img, list):
        img = next((i for i in img if i), None)
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl")
    img = str(img or "").strip()
    return img if img.startswith("http") else None


def _ld_walk(node, out):
    """Collect every schema.org Event in a document, however deeply nested.

    `_dice_event` can look at top-level blocks only because a DICE event page is
    one event and puts it there. A venue's *listing* page is the opposite shape:
    the events arrive inside `@graph`, or as the `item` of an `ItemList`, or as
    a bare array, and which one is a function of the plugin rather than of
    anything meaningful. Walking the whole document is the only version of this
    that does not need a rule per CMS.

    A dict counts as an event when its `@type` ends in Event AND it states a
    `startDate`. The second half matters: an `EventSeries` describing a run of
    shows, or an Event stub carrying only a name and a link, is markup about
    events rather than an event, and admitting it would put undated rows into a
    pipeline whose first question is always when.
    """
    if isinstance(node, dict):
        if _event_type(node) and node.get("startDate"):
            out.append(node)
        for v in node.values():
            _ld_walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _ld_walk(v, out)
    return out


def _ld_events(body):
    """Every dated schema.org Event on a page, de-duplicated.

    Sites routinely emit the same event twice -- once in a page-level `@graph`
    and once in a per-card block -- and a `subEvent` reachable from two parents
    arrives twice from the walk itself. Keyed on name+start+url rather than on
    object identity, because the two copies are usually equal in content and
    distinct in memory.
    """
    out = []
    for block in LD_JSON.findall(body):
        try:
            _ld_walk(json.loads(block.strip()), out)
        except ValueError:
            continue
    seen, uniq = set(), []
    for e in out:
        key = (str(e.get("name")), str(e.get("startDate")), str(e.get("url")))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    return uniq


HREF = re.compile(r'href=["\']([^"\'#]+)', re.I)


def _reg_domain(netloc):
    """Host without `www.`, for comparing a link to the page that carried it.

    Deliberately not a public-suffix implementation. The question here is only
    "is this link still on the venue's own site", and the case that matters is
    a gallery whose listing lives on michaelrosenfeldart.com and whose event
    pages live on michaelrosenfeld.com -- which this does NOT treat as the same
    host, and should not. That venue is handled by pointing the registry at the
    domain its events are actually on.
    """
    return (netloc or "").lower().split(":")[0].removeprefix("www.")


def _ld_links(body, base, prefix):
    """Event detail URLs on a listing page, in document order.

    `prefix` comes from the detail URL the census already found, so this is
    reading a known-good path shape rather than guessing at one. A venue whose
    events live under /shows/ and whose listing is /whats-on is handled without
    a rule, because the census went and looked.

    Same-host only, and the prefix root itself is excluded -- /events links back
    to the listing from every card on it, and following that is a request spent
    to re-read the page you are already holding.
    """
    seen, out = set(), []
    base_host = _reg_domain(urllib.parse.urlsplit(base).netloc)
    for href in HREF.findall(body):
        try:
            u = urllib.parse.urlsplit(urllib.parse.urljoin(base, href))
        except ValueError:
            continue
        if u.scheme not in ("http", "https"):
            continue
        if _reg_domain(u.netloc) != base_host:
            continue
        path = u.path.rstrip("/")
        if not path.startswith(prefix + "/"):
            continue
        clean = f"{u.scheme}://{u.netloc}{path}"
        if clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _event_type(ev):
    """The event subtype as a plain string, or None.

    `@type` may be a list, and the classifier downstream wants one word it can
    look up. Taking the most specific -- anything but bare `Event` -- keeps
    `["ComedyEvent", "Event"]` classifying as comedy rather than as nothing.
    """
    t = ev.get("@type")
    types = [x for x in (t if isinstance(t, list) else [t])
             if isinstance(x, str) and x.endswith("Event")]
    if not types:
        return None
    return next((x for x in types if x != "Event"), types[0])


def _dice_row(ev, entry, url, floor, cutoff):
    """One schema.org event -> a raw row, or None if it is outside the horizon.

    The horizon is applied here rather than in normalize so that it tests the
    same field the cache is keyed on. The sitemap carries past events too;
    their pages are fetched once and then served from cache forever.
    """
    start = ev.get("startDate")
    try:
        when = datetime.fromisoformat(str(start))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if not (floor <= when <= cutoff):
        return None

    loc = _one(ev.get("location"))
    geo = _one(loc.get("geo"))
    addr = loc.get("address")
    return {
        "url": ev.get("url") or url,
        "city_slug": entry.get("city_slug"),
        "name": ev.get("name"),
        "type": _event_type(ev),
        "startDate": start,
        "endDate": ev.get("endDate"),
        "eventStatus": ev.get("eventStatus"),
        "description": ev.get("description"),
        "venue_name": loc.get("name"),
        # A PostalAddress object where most events carry a plain string. Kept
        # for the record either way; placement uses the coordinates.
        "venue_address": addr if isinstance(addr, str) else _one(addr) or None,
        "lat": geo.get("latitude"),
        "lon": geo.get("longitude"),
        "organizer": _one(ev.get("organizer")).get("name"),
        "image": _ld_image(ev),
    }


def fetch_dice(src, horizon_days=60):
    """DICE events, read from the markup each page publishes for indexing.

    Discovery lives in `dice.py`; this reads its registry. Unlike the other
    registries here that one is cheap and short-lived, so it is meant to be
    re-run alongside this rather than once a season -- an event that has left
    the sitemap has passed or been cancelled.

    The cache is what makes this affordable. Every sitemap entry carries a
    `lastmod`, so an event whose timestamp has not moved since the last run is
    served from `data/<city>/dice-cache.json` without a request. A first run
    costs ~1,500 requests; a second costs however many events actually changed,
    which is usually a few dozen. Re-reading 1,500 unchanged pages a day
    because it was easier to write is the kind of thing the politeness in
    lib/http exists to prevent.

    Entries with no lastmod are re-read every run. That is the honest reading:
    absent is not unchanged, and there are few enough of them to be cheap.

    One venue's failure is not the run's, same as Squarespace -- but here a
    rising failure count means DICE has changed its markup, so it is reported
    rather than swallowed.
    """
    city = src.get("_city", "nyc")
    path = os.path.join(city_dir(city), "dice.json")
    try:
        with open(path) as f:
            registry = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"no {path} -- run `python3 dice.py` first")

    cache_path = os.path.join(ROOT, "data", city, "dice-cache.json")
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except Exception:
        cache = {}

    horizon = min(horizon_days, src.get("horizon_days", 60))
    cutoff = datetime.now(timezone.utc) + timedelta(days=horizon)
    floor = datetime.now(timezone.utc) - timedelta(days=1)

    events = registry.get("events") or []
    print(f"    {len(events)} events in the registry, horizon {horizon}d")

    rows, failed, malformed, hits, fetched = [], 0, 0, 0, 0
    fresh = {}
    for e in events:
        url, mod = e["url"], e.get("lastmod")
        hit = cache.get(url)
        if hit and mod and hit.get("lastmod") == mod:
            ev = hit.get("event")
            hits += 1
        else:
            fetched += 1
            try:
                r = http.get_full(url, delay=1.0, accept=http.HTML_ACCEPT,
                                  max_bytes=DICE_BYTES)
                if r["status"] != 200 or not r["body"]:
                    failed += 1
                    continue
                ev = _dice_event(r["body"].decode("utf-8", "replace"))
            except Exception:
                failed += 1
                continue
            if not ev:
                failed += 1
                continue
        if not ev:
            continue
        fresh[url] = {"lastmod": mod, "event": ev}

        # Wrapped because one event must never cost the run. The first version
        # of this was not, and a single listing with two organizers -- `.get` on
        # a list -- took down all 1,539 and wrote no output at all. Schema.org
        # is a vocabulary, not a schema: any property can arrive in a shape the
        # last thousand did not use.
        try:
            row = _dice_row(ev, e, url, floor, cutoff)
        except Exception:
            malformed += 1
            continue
        if row:
            rows.append(row)

    # `fresh` is built up rather than written back over `cache`, so an event
    # that has left the registry is evicted by simply not being copied across.
    # A cache that only ever grows would keep cancelled events alive forever.
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(fresh, f, indent=0, sort_keys=True)

    print(f"    {hits} from cache, {fetched} fetched, {failed} unreadable"
          + (f", {malformed} malformed" if malformed else ""))
    if fetched and failed > fetched * 0.2:
        print(f"    WARNING: {100.0 * failed / fetched:.0f}% of fetches yielded no "
              f"event block -- check whether the page markup has changed")
    print(f"    {len(rows)} events inside the horizon")
    return rows


BOROUGHS = {"brooklyn": "Brooklyn", "bronx": "Bronx", "the bronx": "Bronx",
            "queens": "Queens", "staten island": "Staten Island",
            "manhattan": "Manhattan"}


def _ld_address(loc):
    """A postal address as one string, for geocode.py to place.

    These events state a `PostalAddress` and no coordinates, which is the shape
    geocode.py already handles for the permits feed and the library. Composed
    here rather than there because only the adapter knows which of the many
    address-ish fields a source actually filled in.
    """
    addr = loc.get("address")
    if isinstance(addr, str):
        return clean_ws(addr)
    addr = _one(addr)
    parts = [addr.get("streetAddress"), addr.get("addressLocality"),
             addr.get("addressRegion")]
    return clean_ws(", ".join(str(p).strip() for p in parts if p and str(p).strip()))


def _ld_price(ev):
    """The lowest stated offer price, and whether it is free.

    Only what the markup says. An event with no `offers` is unknown rather than
    free -- the same rule every other adapter here follows -- but a stated 0 is
    a real claim and is kept as one.
    """
    best = None
    offers = ev.get("offers")
    for o in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(o, dict):
            continue
        for key in ("lowPrice", "price"):
            try:
                p = float(str(o.get(key)).replace("$", "").replace(",", ""))
            except (TypeError, ValueError):
                continue
            if best is None or p < best:
                best = p
    return best, (best == 0 if best is not None else None)


def _ld_row(ev, v, floor, cutoff, page_url=None):
    """One schema.org Event from a listing page -> a raw row, or None."""
    start = ev.get("startDate")
    when = _ld_when(start)
    if not when or not (floor <= when <= cutoff):
        return None

    loc = _one(ev.get("location"))
    geo = _one(loc.get("geo"))
    locality = str((_one(loc.get("address")) or {}).get("addressLocality") or "").strip().lower()
    price_min, is_free = _ld_price(ev)
    try:
        lat = float(geo["latitude"])
        lon = float(geo["longitude"])
    except (KeyError, TypeError, ValueError):
        lat = lon = None

    address = _ld_address(loc)
    # The building's own point, and ONLY where the calendar names a single
    # room. Alice Austen House marks its events up with a place name and no
    # PostalAddress, so there is nothing to geocode and nine real events would
    # be dropped for a gap in someone's template. New York Comedy Club names
    # three rooms four miles apart, and for it this fallback would be a
    # confident wrong answer -- so the rule is the count, not the venue.
    if lat is None and not address and len(v.get("locations") or []) <= 1:
        lat, lon = v.get("lat"), v.get("lon")

    return {
        "venue_host": v["host"],
        # Which name wins depends on the mode, because the two modes are
        # different claims. A LISTING page describes many rooms and says which
        # on every card, so the event's own room wins -- New York Comedy Club
        # runs three, four miles apart, and one name for all of them would file
        # the Upper West Side in the East Village. A DETAIL crawl is scoped by
        # `_ld_links` to one venue's own host, so every page is about that
        # venue, and there the per-page name is not extra information but
        # drift: the Skyscraper Museum calls itself "The Skyscraper Museum" on
        # three pages and "Skyscraper Museum" on a fourth, which is one museum
        # and was becoming two venue records with two URLs.
        "venue_name": (v.get("venue_name") or v["name"]
                       if v.get("mode") == "detail"
                       else clean_ws(loc.get("name")) or v.get("venue_name") or v["name"]),
        "address": address or None,
        "borough": BOROUGHS.get(locality) or v.get("borough"),
        "title": clean_ws(ev.get("name")),
        "type": _event_type(ev),
        "url": ev.get("url") or page_url or v["url"],
        # Emitted as ISO rather than as the source wrote it. The adapter has
        # already had to parse this to apply the horizon, and handing normalize
        # the raw `Apr 26, 2026` would make it solve the same problem again with
        # a different parser -- which is how a date ends up accepted here and
        # dropped there.
        "start": when.isoformat(),
        "end": (_ld_when(ev.get("endDate")) or "").isoformat()
               if _ld_when(ev.get("endDate")) else None,
        "status": ev.get("eventStatus"),
        "description": ev.get("description"),
        "price_min": price_min,
        "is_free": is_free,
        "image": _ld_image(ev),
        # Only when the markup states them. Absent here means geocode.py places
        # the address instead, which is why this must stay null rather than
        # borrowing the host's point.
        "lat": lat, "lon": lon,
    }


def _ld_stale(hit, floor, refresh_days=3, empty_days=14):
    """Should this cached detail page be read again?

    There is no `lastmod` here -- that is the whole difference from DICE -- so
    staleness is decided from the event itself rather than from what the server
    said, and the rule is asymmetric on purpose:

      * An event that has already happened is **never** re-read. Its page may
        change forever and it will still be over.
      * An event still ahead is re-read every few days, because the thing worth
        catching is a moved time or a cancellation, and it is a small set.
      * A page that yielded no Event is re-read rarely. Usually it is not an
        event page at all -- a season index that lives under /events/ -- and
        spending a request a day to re-learn that is the waste this exists to
        avoid.
    """
    at = hit.get("fetched_at")
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return True

    ev = hit.get("event")
    if not ev:
        return age > timedelta(days=empty_days)
    when = _ld_when(ev.get("startDate"))
    if when and when < floor:
        return False
    return age > timedelta(days=refresh_days)


# schema.org says startDate is ISO 8601. Real sites disagree, and they disagree
# silently: the Apollo publishes `Apr 26, 2026` and bitforms `01/23/2020`, both
# of which `fromisoformat` refuses, so the event was dropped with no error to
# notice. Ambiguity is the reason this list is short -- `01/02/2026` is January
# in the US and February most other places, and this index is a US city, so
# month-first is the reading. Anything not on this list stays unparsed rather
# than being guessed at.
_LD_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y/%m/%d",
               "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%m/%d/%Y %H:%M")


def _ld_when(value):
    """A schema.org date or date-time as an aware datetime, or None."""
    v = str(value or "").strip()
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v)
    except (TypeError, ValueError):
        d = None
        for fmt in _LD_FORMATS:
            try:
                d = datetime.strptime(v, fmt)
                break
            except ValueError:
                continue
        if d is None:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fetch_jsonld(src, horizon_days=60):
    """Venue calendars published as schema.org markup on the listing page.

    Discovery lives in `jsonld.py`; this reads its registry and fetches one page
    per venue. That is the entire cost -- these are sites whose listing markup
    already contains the calendar, which is why they were selected. A venue that
    puts one event per detail page is recorded by the discovery script and not
    fetched here, because harvesting it means link discovery plus a request per
    event and that is a different bargain.

    Nothing is guessed about location. Every event states a `PostalAddress`, so
    the address rides through to geocode.py the way the permits feed's street
    strings do, and each room a venue names is placed on its own. The
    alternative -- one point per host -- is what would put all three New York
    Comedy Club rooms at whichever one OSM happened to record.
    """
    city = src.get("_city", "nyc")
    path = os.path.join(city_dir(city), "jsonld.json")
    try:
        with open(path) as f:
            registry = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"no {path} -- run `python3 jsonld.py` first")

    horizon = min(horizon_days, src.get("horizon_days", 60))
    cutoff = datetime.now(timezone.utc) + timedelta(days=horizon)
    floor = datetime.now(timezone.utc) - timedelta(days=1)
    floor_n = min(registry.get("min_events", 2), 2)

    def readable(v):
        """A venue this adapter can actually get events out of.

        Listing venues need markup on the page. Detail venues need links to
        follow -- a site with Event markup and no reachable anchors is usually
        rendering its listing in JavaScript, and is recorded by the discovery
        script precisely so it is not silently retried every run.
        """
        if v.get("skip"):
            return False
        if v.get("mode") == "detail":
            return (v.get("links") or 0) > 0
        return v.get("events", 0) >= floor_n

    venues = [v for v in registry.get("venues") or [] if readable(v)]
    skipped = [v for v in registry.get("venues") or [] if v.get("skip")]
    n_det = sum(1 for v in venues if v.get("mode") == "detail")
    print(f"    {len(venues)} venues ({n_det} crawled per event), horizon "
          f"{horizon}d ({len(skipped)} skipped by hand)")

    cache_path = os.path.join(ROOT, "data", city, "jsonld-cache.json")
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except Exception:
        cache = {}

    max_detail = src.get("max_detail_per_venue", 25)
    rows, failed, fresh = [], 0, {}
    hits = fetched = 0

    def keep_cached(host):
        """Carry a venue's existing cache across when we could not re-read it.

        Eviction-by-omission is right for a venue whose listing we DID read: a
        page that has left it has passed or been cancelled. It is badly wrong
        for a venue whose listing simply failed this run, and the failure is
        not hypothetical -- The PIT stopped answering right after 60 sequential
        page reads, which dropped 60 cached pages and 17 live events, and would
        have re-crawled all of them next run to learn nothing new. A source
        being briefly unreachable is not evidence about its events.
        """
        want = _reg_domain(host)
        for url, hit in cache.items():
            if _reg_domain(urllib.parse.urlsplit(url).netloc) == want:
                fresh.setdefault(url, hit)

    for v in venues:
        try:
            r = http.get_full(v["url"], delay=1.0, accept=http.HTML_ACCEPT,
                              max_bytes=2_000_000)
            if r["status"] != 200 or not r["body"]:
                failed += 1
                keep_cached(v["host"])
                continue
            body = r["body"].decode("utf-8", "replace")
        except Exception:
            failed += 1
            keep_cached(v["host"])
            continue

        got = 0
        if v.get("mode") == "detail":
            links = _ld_links(body, r["url"] or v["url"], v.get("prefix") or "")
            budget = max_detail
            for url in links:
                hit = cache.get(url)
                if hit is not None and not _ld_stale(hit, floor):
                    ev = hit.get("event")
                    hits += 1
                    # Carried across whole, timestamp included. Re-stamping it
                    # on a cache hit would push the age back to zero every run
                    # and the refresh window would never open, so an event that
                    # moved or was cancelled would be served from cache until
                    # it left the listing.
                    fresh[url] = hit
                else:
                    if budget <= 0:
                        # Out of budget for this venue this run. The link is
                        # simply not carried into `fresh`, so the next run sees
                        # it as new and picks it up -- a first crawl spreads
                        # over a few runs instead of costing hundreds at once.
                        continue
                    budget -= 1
                    fetched += 1
                    try:
                        # Slower than the one-request-per-venue adapters on
                        # purpose. This is the only place here that asks a
                        # single small venue for dozens of pages in a row, and
                        # The PIT started refusing after 60 of them at a second
                        # apart. A gallery's web host is not Ticketmaster's.
                        d = http.get_full(url, delay=1.5, accept=http.HTML_ACCEPT,
                                          max_bytes=1_000_000)
                        ev = (_ld_events(d["body"].decode("utf-8", "replace"))
                              or [None])[0] if d["body"] else None
                    except Exception:
                        ev = None
                    fresh[url] = {"fetched_at": datetime.now(timezone.utc).isoformat(),
                                  "event": ev}
                if not ev:
                    continue
                try:
                    row = _ld_row(ev, v, floor, cutoff, page_url=url)
                except Exception:
                    continue
                if row and row["title"]:
                    rows.append(row)
                    got += 1
        else:
            for ev in _ld_events(body):
                # One event must never cost the venue, and one venue must never
                # cost the run. schema.org is a vocabulary, not a schema.
                try:
                    row = _ld_row(ev, v, floor, cutoff)
                except Exception:
                    continue
                if row and row["title"]:
                    rows.append(row)
                    got += 1
        if not got:
            failed += 1

    # Rebuilt rather than updated, so a page that has left its listing is
    # evicted by not being copied across. A cache that only grows keeps
    # cancelled shows alive, which is the DICE lesson without DICE's lastmod
    # to make it cheap.
    if fresh:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(fresh, f, indent=0, sort_keys=True)
        print(f"    detail pages: {hits} from cache, {fetched} fetched")

    if failed:
        print(f"    {failed} venues returned nothing this run")
    print(f"    {len(rows)} events inside the horizon")
    return rows


ADAPTERS = {"socrata": fetch_socrata, "squarespace": fetch_squarespace,
            "ticketmaster": fetch_ticketmaster, "luma": fetch_luma,
            "drupal": fetch_drupal, "wordpress": fetch_wordpress,
            "dice": fetch_dice, "jsonld": fetch_jsonld}


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
