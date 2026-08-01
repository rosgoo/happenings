# happenings

A friendly, faceted index of what's actually going on in New York — aggregated
from public event feeds, normalised into one schema, browsable by day, time of
day, neighbourhood, category and venue.

The premise: answering "free thing, Park Slope-ish, Thursday after work"
currently means checking eight walled gardens. That's a faceted query over a
normalised index, which is a thing software is good at and nobody has built for
this city.

Sibling project to gearherd, and a deliberate inversion of it — same pipeline
discipline, much friendlier face.

**Status: v1 running on city open data.** 6,100 events · 1,120 venues ·
196 neighbourhoods · 1,200 places · 577 static pages. 70% of events are placed
to a neighbourhood; 176 places carry a credited photo. The ticketed venues
aren't in it yet — see *What's next*.

## Run it

```bash
python3 fetch.py --neighborhoods   # one-off: NTA polygons (changes on the decade)
python3 fetch.py --parks           # one-off: park polygons (parks don't move)
python3 fetch.py                   # extract   -> raw/nyc/
python3 geocode.py                 # place it  -> data/nyc/geocode-cache.json
python3 normalize.py               # transform -> data/nyc/
python3 validate.py                # quality gate; exit 1 blocks the commit
python3 places.py                  # things that are OPEN, with photos

npm install
npm run dev                        # http://localhost:4321/nyc
```

`npm run pipeline` runs the three Python stages in order. They are stdlib-only
and deliberately stay that way — the crawl has to run anywhere.

## Shape

**Flat files are the source of truth; everything else is derived.** The load
step is a commit, the commit is the deploy, and git is the database history,
the backup and the audit log for free. No runtime database.

```
cities/nyc/{city,sources}.json ──> fetch.py ──> raw/nyc/*.json
                                                     │
                                                     ▼
                                              normalize.py ──> data/nyc/events.json
                                                     │                   venues.json
                                                     │                   rejected.json
                                                     ▼                   meta.json
                                              validate.py ─── exit 1 blocks the commit
                                                     │
                                                     ▼
                                               Astro build
```

A **city is a config directory, not a code branch** — `cities/<slug>/` holds the
config, polygons and source registry; everything in `lib/` is city-agnostic.
`validate.py` greps for timezone and city-name literals outside `cities/` and
fails the build on a leak, because that is the drift that turns multi-city into
a rewrite later. It has already caught one.

## The entity model

Events are ephemeral, so **there is no page per event**. The persistent
entities get the URLs:

| Entity | Lifecycle | URL |
|---|---|---|
| Event | dead after it happens | none — renders inside the pages below |
| Venue | years | `/nyc/venues/<venue>/` |
| Neighborhood | permanent | `/nyc/<neighborhood>/` |
| Category | permanent, closed vocabulary | `/nyc/<category>/` |

Neighbourhood editions only generate when they earn it — ≥3 upcoming events from
≥2 distinct venues. Programmatic pages with nothing on them are doorway pages,
and getting that wrong poisons the domain.

## The quality gate

`validate.py` runs before the commit, because the commit deploys. Its
**structural** half is inherited from gearherd. Its **regression** half is not:
gearherd's "count must not fall 25%" assumes a stable entity set, but here the
set turns over weekly and a Monday always looks catastrophic beside a Saturday.

It's replaced by a **per-source freshness gate**, because the failure mode is
different — a broken adapter here produces *nothing*, silently, forever. Any
enabled source that goes quiet fails the build.

## Sources

| Tier | Adapter | Source | In v1 |
|---|---|---|---|
| 2 | `socrata` | NYC Parks Public Events (`w3wp-dpdi`) | **yes** |
| 2 | `socrata` | NYC Permitted Events (`tvpp-9vvx`) | **yes** |
| 0 | `predicthq`, `seatgeek` | aggregators — free tiers, need a key | no |
| 1 | `ics`, `jsonld`, `localist`, `custom` | institutions and arts venues | no |
| 3 | `ticketmaster` | Discovery API — needs a key, read the terms | no |

`fudw-fgrp` and `6v4b-5gp4` are **dead** (newest rows 2019) and are deliberately
absent rather than silently returning nothing.

Eventbrite is skipped: `robots.txt` permits event pages, but they closed public
search deliberately in 2020 and the ToS needs a real read first.

## Placing things on a map

`lib/places.py` is the location service: anything that describes a place —
coordinates, a park name, a street intersection — resolves through one
interface to a point, a neighbourhood and a borough. Strategies are tried in
descending order of authority and each result records which one answered:

| Strategy | What it is | Notes |
|---|---|---|
| `coordinates` | already located | just needs the polygon lookup |
| `register` | NYC Parks Properties polygons | authoritative lookup, not a guess. Does the heavy lifting: 76% of unplaced rows |
| `gazetteer` | names resolved on an earlier run | a name seen once is free forever |
| `geocoder` | NYC Planning Labs GeoSearch, keyless | address-shaped strings only, above a confidence floor |

Matching is **exact after normalisation** — no edit distance, no
nearest-neighbour. Unresolved goes to `geocode-unresolved.csv`, never silently
into the data. This took neighbourhood coverage from 20% to **70%**.

Swapping in libpostal, a self-hosted Pelias/Nominatim, or shapely for the
polygon work means appending one strategy, not touching callers.

## What the data honestly is
- **0% of events state a price**, so there is no price filter and nothing is marked free.
  Most of these events almost certainly are free — asserting it would be
  guessing, and an event with no stated price is *unknown*, not free.
- **26,000 rows are quarantined** in `data/nyc/rejected.json` with their titles,
  so the classifier is audited by reading a file. Mostly youth/adult league field
  permits: real permits, but not things you can turn up to.

## Images

`places.py` is the only source here that carries photographs. OpenStreetMap
supplies the places; a linked Wikipedia article's infobox supplies the photo;
the Commons REST API supplies the author. **An image whose author cannot be
established is dropped rather than shown uncredited.**

Everything is hotlinked at source and never rehosted — simultaneously the
correct rights position and the cheapest possible bandwidth policy. Places
without a photo get their flat category block, which is the design rather than
an apology.

Two things worth knowing:

- **`api.wikimedia.org` publishes no robots.txt; `wikidata.org` disallows
  `/w/`.** So the Wikidata P18 route is behind `--wikidata-images`, off by
  default. Their API policy invites API use with a descriptive User-Agent, so
  the two documents disagree — that is a judgement for whoever runs this, not
  something the pipeline should assume.
- **Article HTML is fetched with a `Range` header**, ~260 KB instead of 1.3 MB.
  Pulling a megabyte of someone else's bandwidth to read one `<img>` is waste.

## What's next

1. **PredictHQ free key** — one signup measures how much of the long tail is
   already solved before any custom adapter gets written.
2. **Tier 1 venues** — Pioneer Works, Roulette, LPR, IFC, the university lecture
   calendars. They're the reason the project exists and none are in yet.
3. **Subway filter** — designed, not built. Stations don't move, so it's a
   one-time static join against GTFS.
4. **Series detection**, then neighbourhood **reach** (nobody lives inside a
   polygon).
5. **The last 30% of geocoding** — 518 unresolved locations, mostly street
   intersections GeoSearch missed. `geocode-unresolved.csv` is ranked by how
   many events ride on each.

## Legal posture

- Only endpoints published for everyone. No login-gated data, no CAPTCHA
  solving, no proxy rotation.
- `robots.txt` honoured, requests paced and self-identifying, `Retry-After`
  respected.
- Facts only — title, time, place, category. Every listing links back to its
  source. happenings is a router, not a destination.
- Nothing is estimated. Missing shows as missing, and `/nyc/about` publishes the
  coverage numbers.
