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
            "venue_host": v["host"], "venue_name": v["name"],
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
            "categories": [], "tags": [],
            "venue_address": e["location"],
            "venue_lat": e["lat"], "venue_lon": e["lon"],
        })
    return out


ADAPTERS = {"socrata": fetch_socrata, "squarespace": fetch_squarespace,
            "ticketmaster": fetch_ticketmaster, "luma": fetch_luma,
            "drupal": fetch_drupal, "wordpress": fetch_wordpress}


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
