# happenings

A friendly, faceted index of what's actually going on in New York — aggregated
from public event feeds, normalised into one schema, browsable by date, time of
day, neighbourhood, category and venue.

The premise: answering "free thing, Greenpoint-ish, Thursday after work"
currently means checking eight walled gardens. That's a faceted query over a
normalised index, which is a thing software is good at and nobody has built for
this city.

Sibling project to [gearherd](https://github.com/rosgoo/gearherd), and a
deliberate inversion of it — same pipeline discipline, much friendlier face.

## Shape

**Flat files are the source of truth; everything else is derived.** The load
step is a commit, the commit is the deploy, and git is the database history,
the backup and the audit log for free. No runtime database.

```
sources.json ──> fetch.py ──> raw/<source>/*.json
                                     │
                                     ▼
                              normalize.py ────> data/events.json
                                     │           data/venues.json
                                     │           data/series.json
                                     ▼           data/rejected.json
                            resolve_venues.py    (entity resolution)
                                     │
                                     ▼
                                 dedupe.py       (cross-source collapse)
                                     │
                                     ▼
                             track_events.py ──> data/event-history.jsonl
                                     │           data/event-state.json
                                     ▼
                              validate.py ────── exit 1 blocks the commit
                                     │
                                     ▼
                               Astro build
```

## The entity model

Events are ephemeral, so **there is no page per event**. Five entity types,
and the persistent four are what get URLs:

| Entity | Lifecycle | URL |
|---|---|---|
| Event | dead after `end_utc` | none — renders inside the pages below |
| Venue | years | `/venues/<venue>/` |
| Series | semi-persistent | `/series/<series>/` |
| Neighborhood | permanent | `/<neighborhood>/` |
| Category > sub | permanent, closed vocabulary | `/<category>/<subcategory>/` |

Crossed pages (`/greenpoint/techno/`) only generate when they earn it — ≥3
events in the trailing 90 days, ≥1 upcoming, ≥2 distinct venues. Everything
else redirects to the parent facet. Programmatic cross-products with nothing on
them are doorway pages, and getting that wrong poisons the domain.

## Sources (v1)

| Adapter | Source |
|---|---|
| `socrata` | NYC Open Data — Permitted Events, Parks Events Listing |
| `ticketmaster` | Discovery API |
| `tribe` | `/wp-json/tribe/events/v1/events` — The Events Calendar WP plugin |
| `jsonld` | schema.org `Event` blocks |
| `ics` | iCalendar feeds |
| `squarespace` | Squarespace events JSON |

Meetup and Eventbrite closed their public discovery APIs deliberately; neither
is scraped. Anything that writes a `raw/`-shaped file slots into Transform
unchanged.

## Status

Design complete, nothing built. Phase 0 is three probes that decide the plan:
read the Ticketmaster terms, measure Tribe REST hit rate across 40 NYC venues,
and confirm the NYC Open Data sets are usable as listings.

Design docs live in Grove under `happenings/` — `design.md` (what it is) and
`engineering.md` (how it gets built, and in what order).

## Legal posture

Inherited from gearherd, deliberately:

- Only endpoints published for everyone. No login-gated data, no CAPTCHA
  solving, no proxy rotation — circumventing a technical barrier is what turns
  reading public data into a losing position.
- `robots.txt` honoured, requests paced and self-identifying, `Retry-After`
  respected.
- Facts only — title, time, venue, price, category. No marketing copy
  reproduced, images referenced at source and never rehosted.
- Every listing links back to where it came from. happenings is a router, not
  a destination.
