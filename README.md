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

**Status: v1, city open data plus the ticketed tier.** 9,315 events ·
1,215 venues · 199 neighbourhoods · 1,200 places · 737 static pages. 92% of
events are placed to a neighbourhood; 176 places carry a credited photo. DICE
covers the independent music and nightlife rooms — Elsewhere, Union Pool, Saint
Vitus, House of Yes, Public Records — which is the tier that sells through
neither Ticketmaster nor its own calendar. The rest of *What's next* stands.

## Run it

```bash
python3 fetch.py --neighborhoods   # one-off: NTA polygons (changes on the decade)
python3 fetch.py --parks           # one-off: park polygons (parks don't move)
python3 dice.py                    # discover  -> cities/nyc/dice.json (before every fetch)
python3 jsonld.py                  # discover  -> cities/nyc/jsonld.json (weekly)
python3 fetch.py                   # extract   -> raw/nyc/
python3 geocode.py                 # place it  -> data/nyc/geocode-cache.json
python3 normalize.py               # transform -> data/nyc/
python3 validate.py                # quality gate; exit 1 blocks the commit
python3 places.py                  # things that are OPEN, with photos

npm install
npm run dev                        # http://localhost:4321/nyc
```

`npm run pipeline` runs the four Python stages in order — `dice.py` included,
because its registry is replaced rather than merged and a stale one both misses
tonight's shows and keeps listing cancelled ones. They are stdlib-only and
deliberately stay that way — the crawl has to run anywhere.

The **discovery** scripts — `squarespace.py`, `luma.py`, `jsonld.py`, `dice.py`
— write committed registries that `fetch.py` then reads, so finding a source is
separated from reading it. Only `dice.py` belongs in the regular run: a
Squarespace slug changes when a site is redesigned, a Luma roster only grows,
and `jsonld.py` re-learns which venues publish a whole calendar in their markup
— all answers that move when a site is rebuilt. DICE events are born and die
daily, so its registry is rebuilt each time rather than merged. It costs four
requests.

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
| 0 | `ticketmaster` | Ticketmaster Discovery — free key, aligned incentives | **yes** |
| 0 | ~~`seatgeek`~~ | sitemaps are 180,000 URLs and not segmented by city | no |
| 0 | `dice` | DICE — sitemap + schema.org, independent music and nightlife | **yes** |
| 1 | `wordpress` | venue calendars on The Events Calendar, REST or ICS | **yes** |
| 1 | `jsonld` | venues publishing their calendar as schema.org markup | **yes** |
| 1 | `drupal` | Brooklyn Public Library, every branch | **yes** |
| 1 | `localist`, `custom` | university and institutional calendars | no |
| — | ~~`predicthq`~~ | great coverage, but its licence restricts public display | no |

`fudw-fgrp` and `6v4b-5gp4` are **dead** (newest rows 2019) and are deliberately
absent rather than silently returning nothing.

Eventbrite is skipped: `robots.txt` permits event pages, but they closed public
search deliberately in 2020 and the ToS needs a real read first.

The rest of the consumer ticketing layer is audited in
[`docs/ticketing-platforms.md`](docs/ticketing-platforms.md) — which of them can
be enumerated, and which of them permit it. Resident Advisor and Partiful are
written down there as **refused rather than unexamined**: both are technically
readable, and both forbid it.

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

## Picking data partners

The rule this project learned the hard way, after briefly ranking PredictHQ
first:

> **Use sources whose business is getting people to the event. Avoid sources
> whose business is selling the event data.**

A ticketing or artist-promotion API *wants* you to display listings and link
out — that is how they get paid, and it is exactly what happenings does on
every row. A demand-intelligence vendor sells exclusivity to customers who
never show the data to anyone; a free public mirror of their product is against
their interest, and their licence says so. PredictHQ's terms prohibit
"republish … display … make available or distribute" their data except as a
plan specifically grants, and display is plan-gated. Great coverage, wrong
shape for a public index.

## What's next

1. **Ticketmaster Discovery key** — free, self-serve, instant, 5,000 calls/day.
   Built for third parties to make event-discovery experiences, so displaying
   and linking is the point rather than the exception.
2. **SeatGeek**, then **Songkick / Bandsintown** — same logic, same alignment.
3. **Tier 1 venues** — Pioneer Works, Roulette, LPR, IFC, the university lecture
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
- **Content signals honoured too.** DICE publishes
  `search=yes,ai-train=no,use=reference`; happenings indexes and links back, and
  the description is cut to one line so a listing stays a reference rather than
  a mirror. A site that answers a question it was never asked is still entitled
  to say what the answer may be used for.
- Facts only — title, time, place, category. Every listing links back to its
  source. happenings is a router, not a destination.
- Nothing is estimated. Missing shows as missing, and `/nyc/about` publishes the
  coverage numbers.
