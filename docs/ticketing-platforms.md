# Ticketing platforms — what's readable, and on whose terms

An audit of the consumer ticketing platforms that are *not* Eventbrite or
Ticketmaster, asking the only two questions that matter here: **can we
enumerate their NYC events, and are we allowed to?**

Both halves are load-bearing. Luma is in the index because its Subscribe
button is a real ICS feed — a published surface used as published. Eventbrite
is out because the only routes left were an ownership-scoped API we have no
standing in and an internal search endpoint the terms forbid. Several
platforms below are technically wide open and still fail the second question,
which makes them Eventbrite, not Luma.

Probed 2026-08-02 from a datacenter IP. Anything marked "blocked" may behave
differently from a residential address; that is noted where it matters.

## Summary

| Platform | Enumerable? | Permitted? | Verdict |
|---|---|---|---|
| **DICE** | Yes — sitemap, 1,539 NYC events | robots `Allow: /`, sitemaps published | **Built** |
| TicketWeb / Universe / Front Gate | Already ours | Ticketmaster subsidiaries | **Audit, don't build** |
| Meetup | Yes — 12/page | robots allows the find page | Possible, low yield |
| Songkick | Yes — 50/page | UA fingerprinting, 406s | Fragile |
| Resident Advisor | Yes — open GraphQL, 598/30d | Terms forbid reproduction | **Out** |
| Partiful | Barely — 5 NYC events | Terms forbid scraping | **Out** |
| Viewcy | Only via framework internals | Terms forbid automated collection | **Out** |
| Posh | No — Cloudflare 403 | — | Out |
| Shotgun | No — 429 on everything | robots permissive | Out |
| AXS / See Tickets / Etix / Tixr | No | Organizer-scoped APIs only | Out |
| Bandsintown | No | Artists and their reps only | Out |
| Fever | No Event markup | — | Out |

## DICE — the one worth building

DICE is the biggest single gap in the index. It is the ticketer for exactly
the rooms the shipped index misses: Elsewhere, Baby's All Right, Music Hall of
Williamsburg, the whole tier of venue that is too small for Ticketmaster and
too professional for a Squarespace calendar.

It is also, unusually, the one that says yes.

    User-agent: *
    Content-Signal: search=yes,ai-train=no,use=reference
    Allow: /
    Disallow: /api/

    Sitemap: https://dice.fm/sitemaps/sitemap.xml
    Sitemap: https://dice.fm/sitemaps/venues/sitemap.xml
    Sitemap: https://dice.fm/sitemaps/artists/sitemap.xml

Three sitemaps, **21,211 event URLs**, `lastmod` refreshed daily. The city is
in the slug, so the NYC subset is a substring match on a file we already have —
no fetching to find out what to fetch:

    817  *-new-york-city-tickets
    647  *-new-york-tickets
     75  *-brooklyn-tickets

**1,539 NYC events**, 7.3% of DICE's global catalogue. An earlier draft of this
audit said ~722, because it grepped for `-new-york-tickets` and never asked
what else was there — `-new-york-city-tickets` is the larger bucket and was
invisible to the question as posed. Counting the whole `-<city>-tickets` suffix
instead of a guessed one found it.

The tokens are also why the filter is configured rather than derived.
`-york-tickets` looks like a harmless generalisation and is not: 53 of those
are a venue in York, England.

Every event page then carries a schema.org `MusicEvent` block, and it is the
richest shape in the audit:

```json
{
  "@type": "MusicEvent",
  "name": "Ginja: Ethan Tomas' Birthday Edition",
  "startDate": "2026-08-22T22:00:00-04:00",
  "endDate": "2026-08-23T04:00:00-04:00",
  "location": {
    "@type": "Place",
    "name": "SILO Brooklyn",
    "address": "90 Scott Ave, Brooklyn, NY 11237, USA",
    "geo": {"latitude": 40.7105086, "longitude": -73.922873}
  },
  "organizer": {"@type": "Organization", "name": "SILO Brooklyn & Furtado Global"}
}
```

Coordinates and a ticket URL, which are the two fields the shipped index is
thinnest on, arriving already parsed. Structured data is markup a site
publishes *so that* it gets read; reading it is the use it was put there for.

Two things to get right:

* **Use the sitemap, not `/browse`.** `dice.fm/browse` geolocates the caller
  and 307s to whatever city it thinks you are in — from CI that was Columbus,
  Ohio. The browse route would silently index the wrong city. The sitemap has
  no such opinion.

* **Honour `use=reference`.** DICE's content signal permits search indexing and
  reference, and refuses model training. Link out to the DICE page and keep the
  description short rather than republishing it whole; that is the difference
  between referencing a listing and mirroring it. `Disallow: /api/` also means
  `dice.fm/api/` stays untouched — the sitemap and the page markup are the
  front door, and they are enough.

Shape it like `squarespace.py`: discovery separated from fetching, the NYC
slug list committed as a registry, `fetch.py` reading the registry on each run.

## TicketWeb, Universe, Front Gate — already in the index

These are Ticketmaster subsidiaries, and Ticketmaster's Discovery API returns
events from all of its platforms by default. `fetch_ticketmaster` does not set
the `source` parameter, so it is already pulling them — TicketWeb in particular
tickets a lot of mid-size NYC rooms that look absent from the index but may not
be.

**Not verified here** — `TICKETMASTER_API_KEY` isn't set in this environment, so
this rests on Ticketmaster's documentation rather than on a response. Before
writing any adapter for these, facet an existing NYC query on `source` and
count. It costs one request and may retire three imagined integrations.

## Meetup — legal, thin

Two routes, neither good:

* `/find/?location=us--ny--New%20York&source=EVENTS` embeds **12 complete
  schema.org `Event` objects** per page, and `robots.txt` permits the path.
  Twelve per request is a poor exchange rate but an honest one.
* The sitemaps enumerate roughly **180,000 event URLs** across 12 gzipped files
  — and are not segmented by city. Finding NYC's share means fetching all of
  them. That is not an option, it is a denial-of-service with extra steps.

The official GraphQL API needs an active **Meetup Pro subscription** to create
an OAuth consumer, and approval is discretionary on top. The old per-group
iCal and RSS feeds are gone — `/events/ical/` now 404s, though `robots.txt`
still disallows the paths, which is a fossil rather than a feed.

Worth doing only after DICE, and only if the community-events gap still looks
open once Luma's registry has had time to accumulate.

## Songkick — good data behind a fingerprinter

`/metro-areas/7644-us-new-york` returns **50 `MusicEvent` objects** with street
address, postal code, geo coordinates and performers. Genuinely good.

But Songkick is fingerprinting: `robots.txt` and `/developer` both returned
**406 to every user agent tried**, including ordinary browser strings. A source
that refuses to serve its own robots file is telling you something, and an
adapter built on the one path that currently answers would break without
warning. The API is by application with a stated ~30-working-day turnaround and
discretionary approval.

Skip unless the DICE and Ticketmaster routes leave a music-shaped hole.

## Resident Advisor — the Eventbrite of this audit

RA's GraphQL endpoint takes unauthenticated POSTs. No key, no header games:

```
POST https://ra.co/graphql
{"operationName":"GET_EVENT_LISTINGS",
 "variables":{"filters":{"areas":{"eq":8},
   "listingDate":{"gte":"2026-08-02","lte":"2026-08-31"}}, ...}}
```

Area 8 is New York City. That query returns **598 events over the next 30
days**, with title, date, start time, venue and area — the nightlife listings
nothing else in the index covers. `robots.txt` disallows `/api/` but says
nothing about `/graphql`. The 30-day ceiling per query is the only technical
limit.

And the terms close it:

> you are not permitted to use, or cause others to use, any automated system or
> software to extract content or data from our Website for commercial purposes

> automated devices, scripts, bots, spiders, crawlers or scrapers (except for
> standard search engine technologies)

> You may only view, print out, use, quote from and cite our Website and its
> contents for your own personal, non-commercial use

The commercial-purposes clause is arguable for a free index. The reproduction
clause is not: republishing RA's listings is the specific thing it names. This
is the same call Eventbrite got, on the same reasoning, and it should be made
the same way — written down, with the working query recorded so nobody
rediscovers the open endpoint in six months and assumes open means allowed.

## Partiful — right category, no door

Partiful is the correct instinct. It covers the social layer nothing else here
touches, and its public event pages are rich: `__NEXT_DATA__` carries the full
event object — start and end, timezone, a structured `locationInfo` with
address lines, neighbourhood and maps URLs, guest counts, host list — alongside
a schema.org `Event` block.

The problem is that there is nothing to enumerate. `/explore` is the entire
public discovery surface, and it holds exactly **three regions (NYC, LA, SF) at
five events each**. Not five pages. Five events. There is no sitemap —
`/sitemap.xml` 404s — and event pages are `nofollow`.

Five NYC events per load, against Luma's twenty, would not repay the Luma
merge-and-accumulate machinery. And the terms settle it regardless:

> engage in or use any data mining, robots, scraping, or similar data gathering
> or extraction methods

This is also true to the product. Partiful events are invitations; the public
"events you can crash" set is small on purpose. The thing that makes Partiful
worth wanting is the thing that keeps it out of an index.

## Viewcy — the exact inverse of DICE

Worth its own section because it is the most *wanted* platform in this audit
and still a no. Viewcy ticket the small-room jazz and global-music tier that
almost nothing else here reaches: Barbès in Park Slope, Drom in the East
Village, Brooklyn Maqam, Brooklyn Raga Massive. That programming is close to
invisible in the shipped index.

`robots.txt` is the friendliest of any platform audited — only `/auth`,
`/register`, `/manage` and `/tickets` are disallowed, which is to say the
checkout, and nothing else. No content signal, no AI-crawler blocks.

Then every surface underneath it turns out not to exist:

* **The advertised sitemap is not there.** `robots.txt` points at
  `https://viewcy.com/sitemap.xml.gz`; that URL returns 59 KB of HTML — the
  app shell — with a 200. So does `/sitemap.xml`, and so does
  `/sitemap-0.xml`, and so does any other path. The app answers 200 to
  everything, which means **a status code carries no information here** and
  `ical.py`'s lesson applies exactly: a 200 is not a feed. (Routes under
  `/api/` do 404 honestly, which is how you can tell the difference.)

* **There is no structured data at all.** Not on the home page, not on a venue
  page, not on an event page. No `ld+json`, no microdata, no dates in the
  OpenGraph tags.

* **The data exists, but only inside the framework.** An event page carries
  everything worth having — `startsAt`, `timezone`, `address`, `latitude`,
  `longitude` — escaped inside Next.js RSC flight chunks
  (`self.__next_f.push`). That is a React server-components wire format, not a
  publication. It is versioned to the framework rather than to Viewcy, so it
  changes when they upgrade a dependency, with no notice and no reason to give
  any.

Discovery is thin on top of that: the home page links 99 organiser slugs and
81 events, behind `load more` pagination, and there is no city index — so
reaching a full picture means crawling organiser by organiser.

And the terms close it regardless:

> scrape or collect content from the Service via automated means

So: the most permissive robots.txt in the audit, sitting over a site that
publishes nothing to read and forbids reading it anyway. DICE says `Allow: /`
and then hands you a sitemap and schema.org markup; Viewcy says `Allow: /` and
hands you a single-page app. **The permission and the surface are different
questions, and Viewcy is the clearest case of one without the other.**

The honest route here is not a scraper. It is an email — Viewcy's venues are
small rooms who benefit from being listed, and the platform's business is
getting people to the event, which is the alignment test the README already
uses to pick partners.

## Posh and Shotgun — walls

**Posh** returned **403 to every path** including `/explore` — Cloudflare bot
protection, enforced rather than merely declared. Its published robots is the
same permissive Cloudflare template DICE serves; the difference is that Posh
actually blocks. No public read API. The only offers are third-party resellers
(Apify, Bright Data) reselling data they scraped through the same wall.

**Shotgun** is the inverse and no better: `robots.txt` is fully permissive with
a sitemap, and the site returned **429 to every request** across repeated
attempts. Its API is organizer-scoped — an Organizer ID and a token generated
from your own dashboard — which is the Eventbrite shape exactly: an API for
people who sell through it, nothing for people who want to read it.

Both may be reachable from a residential IP. Neither has a route that would
still be there in a year.

## AXS, See Tickets, Etix, Tixr — B2B by design

AXS and See Tickets returned 403; Etix returned a 202 JS challenge. No
schema.org Event markup on any landing page. Tixr's API is by application
through its developer portal, granted to "verified event organizers, venues,
and approved technology partners."

All four are the same shape — a ticketing API for clients, no reader API, and
no structured data left on the page for anyone else. There is no version of
this that works without a business relationship.

## Fever

The NYC page carries `ld+json`, but only `Corporation`, `WebSite` and
`ItemList` — no `Event` objects. The inventory is mostly first-party
experiences (Candlelight and similar) rather than the city's programming.
Low priority independent of access.

## What to do

1. **Facet the existing Ticketmaster pull on `source`.** One request. May show
   TicketWeb and Universe are already in the index.
2. ~~**Build the DICE adapter.**~~ **Done** — `dice.py` for discovery,
   `fetch_dice` and `Builder.dice` for the rest. 1,539 NYC events, with
   coordinates, from a site that publishes a sitemap and says `Allow: /`.
   `lastmod` on every sitemap entry means a second run re-reads only what
   changed.
3. **Write RA, Partiful and Viewcy down as refused**, next to Eventbrite, with
   the queries that work — so the finding is that they were checked and
   declined, not that nobody looked.
4. **Ask Viewcy.** It is the one refusal here worth a conversation rather than
   a workaround: small NYC rooms, programming the index genuinely lacks, and a
   business model that wants listings to travel.
4. **Leave Meetup and Songkick** until the gap they'd fill is still visible
   after 1 and 2.
