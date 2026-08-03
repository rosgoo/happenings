#!/usr/bin/env python3
"""Transform -- turn raw/ into one typed schema.

Rebuilds data/<city>/{events,venues,rejected,meta}.json from scratch every run.
Derived and ephemeral by design: everything here can be regenerated from raw/.

Two invariants carried from gearherd:
  * Every derived value records where it came from (`*_source`).
  * Anything that cannot be established is null. Never estimated. A directory
    that is confidently wrong is worse than one that admits a gap -- so an event
    with no stated price is `is_free: null`, not `false`, and not `true`.
"""
import argparse
import hashlib
import html as _html
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import taxonomy  # noqa: E402
from lib.geo import Neighborhoods  # noqa: E402
from lib.places import Resolver, norm as pnorm  # noqa: E402
from geocode import Parks  # noqa: E402
import subway  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def clean(s):
    """Sources hand back HTML-encoded text (`Zumba Gold&#174;`, `Lim&#243;n`).
    Decode ONCE here so the stored value is the real string -- escaping happens
    at render, and a pipeline that stores pre-escaped text double-escapes it."""
    if not s:
        return s
    return _html.unescape(str(s)).replace("\u00a0", " ").strip()


def slugify(s, maxlen=70):
    out = []
    for ch in (s or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_/&,'.:()":
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug[:maxlen].strip("-")


def parse_naive(s):
    """Socrata hands back naive local timestamps in a couple of shapes."""
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    if "." in s:
        s = s.split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


TAG = re.compile(r"<[^>]+>")


def strip_tags(s):
    """Squarespace excerpts are HTML fragments, not text.

    `clean` decodes entities but leaves `<p style="...">` in place, which would
    be stored and then escaped at render into visible markup. Tags go first,
    entities second -- the other order turns an encoded `&lt;b&gt;` into a tag
    and then eats it.
    """
    if not s:
        return None
    return clean(TAG.sub(" ", str(s))) or None


LUMA_URL = re.compile(r"https://(?:luma\.com|lu\.ma)/[A-Za-z0-9_-]+")

# Brooklyn Public Library files public programming and internal bookkeeping in
# one table, separated by `field_event_type_parent`. Only these are listings.
# The other values -- Meeting Room, In-Branch Tabling, Visit, Outreach -- are a
# room somebody reserved, a table in a lobby, and a school trip.
BPL_PUBLIC = {"Program"}


def to_local_naive(iso, tz):
    """An aware ISO instant -> naive wall-clock in the city's zone.

    `add` stamps the city timezone itself, so anything handed to it must
    already be local and naive. A date-only value (an all-day event) becomes
    midnight, which is the only reading available once a time is required.
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(tz).replace(tzinfo=None)


def from_epoch_ms(ms, tz):
    """Epoch milliseconds -> naive datetime in the city's zone.

    Squarespace stores an instant; `add` wants a local wall-clock time it can
    stamp with the city timezone. Going through UTC and converting is the only
    way that survives a source publishing from another zone.
    """
    if not isinstance(ms, (int, float)):
        return None
    try:
        return (datetime.fromtimestamp(ms / 1000.0, timezone.utc)
                .astimezone(tz).replace(tzinfo=None))
    except (ValueError, OSError, OverflowError):
        return None


def band(dt):
    """The time-of-day filter. 'Free thing on a Tuesday after work' is a query
    about a band of hours across arbitrary dates, not about a date."""
    h = dt.hour
    if h < 12:
        return "morning"
    if h < 17:
        return "afternoon"
    if h < 21:
        return "evening"
    return "late"


def event_id(venue_slug, start_local, title):
    key = f"{venue_slug}|{start_local.strftime('%Y-%m-%dT%H:%M')}|{slugify(title)[:40]}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


class Builder:
    def __init__(self, city):
        self.city = city
        cdir = os.path.join(ROOT, "cities", city)
        with open(os.path.join(cdir, "city.json")) as f:
            self.cfg = json.load(f)
        self.tz = ZoneInfo(self.cfg["tz"])
        with open(os.path.join(cdir, "neighborhoods.geojson")) as f:
            self.hoods = Neighborhoods(json.load(f))

        # geocode.py resolved the location strings that arrive without
        # coordinates -- park names against the city's own park register,
        # address-shaped strings against a geocoder. Load it as a gazetteer so
        # a name seen once is free forever. Absent cache = degrade to borough
        # only, never guess.
        gaz = {}
        gpath = os.path.join(ROOT, "data", city, "geocode-cache.json")
        if os.path.exists(gpath):
            with open(gpath) as f:
                for loc, hit in json.load(f).items():
                    if hit:
                        gaz[pnorm(loc)] = {"lat": hit["lat"], "lon": hit["lon"],
                                           "name": hit.get("matched")}
        # The city's own park register, as a strategy rather than only as an
        # input to geocode.py. `places.Resolver` has always documented a
        # `register` tier above the gazetteer, but nothing was passing one, so
        # the chain was really coords -> gazetteer -> nothing. Any park not
        # already in the cache fell through -- Washington Square Park among
        # them. This is a lookup against ground truth, needs no network, and
        # makes the resolver correct on a cold cache.
        register = {}
        ppath = os.path.join(cdir, "parks.geojson")
        if os.path.exists(ppath):
            for name, rec in Parks(ppath).by_name.items():
                register[name] = {"lat": rec["lat"], "lon": rec["lon"],
                                  "name": rec["name"]}

        self.resolver = Resolver(self.hoods, register=register, gazetteer=gaz)
        print(f"  register: {len(register)} park names · "
              f"gazetteer: {len(gaz)} located names")

        # Nearest subway. New Yorkers do not navigate by NTA name -- they say
        # "off the Jefferson St L". Resolved here so it is consistent with
        # however the venue got placed, and memoised on a ~11 m grid because
        # 4,600 events against 445 stations is two million distance checks and
        # events cluster hard on the same few venues.
        self.subway = subway.load(city)
        self._near_cache = {}
        print(f"  subway: {len(self.subway)} stations")
        self.events, self.rejected = [], []
        self.venues = {}
        self.seen = set()
        self.stats = Counter()

    def near_subway(self, lat, lon, limit=2):
        """Closest stations, or [] when there is no honest answer."""
        if lat is None or lon is None or not len(self.subway):
            return []
        key = (round(lat, 4), round(lon, 4), limit)
        hit = self._near_cache.get(key)
        if hit is None:
            hit = self.subway.nearest(lat, lon, limit=limit)
            self._near_cache[key] = hit
        return hit

    # -- venues ------------------------------------------------------------
    def venue(self, name, lat=None, lon=None, borough=None, kind="venue"):
        """Resolve or create a venue. Exactly one canonical neighbourhood per
        venue -- URLs, dedup and canonical tags all depend on it."""
        name = clean(name) or ""
        if not name:
            return None
        slug = slugify(name)
        if not slug:
            return None
        v = self.venues.get(slug)
        if v is None:
            p = self.resolver.resolve(name=name, lat=lat, lon=lon, borough=borough)
            v = {"id": slug, "name": name, "kind": kind, "count": 0,
                 "lat": None, "lon": None, "neighborhood": None,
                 "neighborhood_name": None, "nta_type": None,
                 "borough": borough, "location_source": None,
                 "location_confidence": None}
            if p:
                v.update(p.as_dict())
                v["borough"] = v["borough"] or borough
            self.venues[slug] = v
        elif v["lat"] is None and lat is not None:
            # A later row carried coordinates the first one didn't. Upgrade --
            # coordinates outrank every name-based strategy.
            p = self.resolver.resolve(lat=lat, lon=lon, borough=borough)
            if p:
                v.update(p.as_dict())
        return v

    # -- emit --------------------------------------------------------------
    def add(self, *, title, start, end, venue, category, subcategory, cat_source,
            source_id, url=None, description=None, flags=None, price_min=None,
            is_free=None, place=None):
        """`place` overrides the venue's location for THIS event.

        Venues keep exactly one canonical neighbourhood -- URLs and dedup depend
        on it -- and for a building that is simply true. It is not true of a
        touring host: Reading Rhythms runs the same night in Harlem, Bushwick,
        Williamsburg and Staten Island, and inheriting the venue's first-seen
        location filed all of them under The Battery. Where a source states
        where THIS event is, that wins for the event while the venue's own
        canonical location is left alone.
        """
        if not title or not start or venue is None:
            self.stats["dropped_incomplete"] += 1
            return
        eid = event_id(venue["id"], start, title)
        if eid in self.seen:
            self.stats["deduped"] += 1
            return
        self.seen.add(eid)

        start_aware = start.replace(tzinfo=self.tz)
        loc = dict(venue)
        if place is not None:
            loc.update(place.as_dict())
        self.events.append({
            "id": eid,
            "title": clean(title),
            "description": (clean(description) or "")[:400] or None,
            "start_utc": start_aware.astimezone(timezone.utc).isoformat(),
            "start_local": start.isoformat(timespec="minutes"),
            "end_local": end.isoformat(timespec="minutes") if end else None,
            "date_local": start.strftime("%Y-%m-%d"),
            "time_band": band(start),
            "tz": self.cfg["tz"],
            "venue_id": venue["id"],
            "venue_name": venue["name"],
            "neighborhood": loc["neighborhood"],
            "neighborhood_name": loc["neighborhood_name"],
            "borough": loc["borough"],
            "lat": loc["lat"], "lon": loc["lon"],
            "location_source": loc.get("location_source"),
            "subway": self.near_subway(loc["lat"], loc["lon"]),
            "category": category,
            "subcategory": subcategory,
            "category_source": cat_source,
            "flags": flags or [],
            # Neither MVP source states a price. null is the honest answer:
            # an event with no stated price is not free, it is unknown.
            "price_min": price_min,
            "is_free": is_free,
            "url": url,
            "source": source_id,
        })
        venue["count"] += 1
        self.stats[f"kept:{source_id}"] += 1

    def reject(self, source_id, title, reason, url=None):
        """Dead-letter queue. Rejects keep their titles and URLs so the
        classifier can be audited by reading a file."""
        self.rejected.append({"source": source_id, "title": title,
                              "reason": reason, "url": url})
        self.stats[f"rejected:{reason}"] += 1

    # -- adapters ----------------------------------------------------------
    def parks(self, src_id, rows):
        for r in rows:
            title = clean(r.get("title"))
            bad = taxonomy.rejected_title(title)
            if bad:
                self.reject(src_id, title, bad)
                continue
            start = parse_naive(r.get("starttime")) or parse_naive(r.get("startdate"))
            end = parse_naive(r.get("endtime"))
            if end and start and end < start:
                end += timedelta(days=1)
            if not start:
                self.reject(src_id, title, "unparseable start time")
                continue

            lat = lon = None
            coords = (r.get("coordinates") or "").split(",")
            if len(coords) == 2:
                try:
                    lat, lon = float(coords[0]), float(coords[1])
                except ValueError:
                    lat = lon = None

            name = r.get("location") or r.get("parknames")
            if not name:
                self.reject(src_id, title, "no location named")
                continue
            v = self.venue(name, lat, lon, kind="park")

            cat, sub, flags, csrc = taxonomy.from_parks(r.get("categories"))
            if not cat:
                cat, sub, csrc = taxonomy.from_title(title)
            if not cat:
                cat, sub, csrc = "outdoors", "park-programming", "default:parks"

            link = r.get("link")
            url = link.get("url") if isinstance(link, dict) else link
            if url and url.startswith("http://"):
                url = "https://" + url[7:]

            self.add(title=title, start=start, end=end, venue=v,
                     category=cat, subcategory=sub, cat_source=csrc,
                     source_id=src_id, url=url,
                     description=r.get("description"), flags=flags)

    def permits(self, src_id, rows):
        for r in rows:
            title = clean(r.get("event_name"))
            etype = r.get("event_type")
            bad = taxonomy.rejected_title(title)
            if bad:
                self.reject(src_id, title, bad)
                continue
            cat, sub, csrc = taxonomy.from_permit(etype)
            if cat == "__reject__":
                self.reject(src_id, title, sub)
                continue

            start = parse_naive(r.get("start_date_time"))
            end = parse_naive(r.get("end_date_time"))
            if end and start and end < start:
                end += timedelta(days=1)
            if not start:
                self.reject(src_id, title, "unparseable start time")
                continue

            # "Special Event" covers thousands of rows and says nothing. The
            # title is the only remaining signal, so it gets a look -- but only
            # after ground truth has failed to be specific.
            if (etype or "").strip().lower() == "special event":
                t_cat, t_sub, _ = taxonomy.from_title(title)
                if t_cat:
                    cat, sub, csrc = t_cat, t_sub, "permit+title"
            if not cat:
                cat, sub, csrc = taxonomy.from_title(title)
            if not cat:
                self.reject(src_id, title, f"unclassified event_type: {etype}")
                continue

            loc = (r.get("event_location") or "").split(":")[0].strip()
            v = self.venue(loc or "Unknown location", borough=r.get("event_borough"))
            self.add(title=title, start=start, end=end, venue=v,
                     category=cat, subcategory=sub, cat_source=csrc,
                     source_id=src_id)

    def squarespace(self, src_id, rows):
        """Venue-published events from Squarespace collections.

        The richest source wired so far: it carries coordinates and a real event
        URL, which are the two fields the index is thinnest on. Timestamps are
        epoch milliseconds in UTC and are converted to city-local naive, because
        that is what `add` expects and what every other adapter hands it.

        Price is still null. Squarespace models an event as a blog post with
        dates; the ticket price lives in prose in the excerpt when it exists at
        all, and guessing it is exactly the estimate this pipeline refuses to
        make.
        """
        for r in rows:
            title = clean(r.get("title"))
            bad = taxonomy.rejected_title(title)
            if bad:
                self.reject(src_id, title, bad, url=r.get("fullUrl"))
                continue

            start = from_epoch_ms(r.get("startDate"), self.tz)
            end = from_epoch_ms(r.get("endDate"), self.tz)
            if not start:
                self.reject(src_id, title, "unparseable start time")
                continue
            # An all-day event is exported as midnight-to-midnight, which would
            # render as "12:00 AM" and sort into the late-night band. Treat a
            # same-day midnight pair as dateless rather than as a 00:00 start.
            if end and end < start:
                end = None

            loc = r.get("location") or {}
            lat, lon = loc.get("mapLat"), loc.get("mapLng")
            try:
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                lat = lon = None
            v = self.venue(r.get("venue_name") or r.get("venue_host"),
                           lat, lon, kind="venue")

            # The venue's own tags are ground truth where they exist; the title
            # is the fallback. Neither is trusted to invent a category.
            cat = sub = csrc = None
            for word in (r.get("categories") or []) + (r.get("tags") or []):
                c, s, _ = taxonomy.from_title(str(word))
                if c:
                    cat, sub, csrc = c, s, "squarespace:tags"
                    break
            if not cat:
                cat, sub, csrc = taxonomy.from_title(title)
            if not cat:
                self.reject(src_id, title, "unclassified squarespace event",
                            url=r.get("fullUrl"))
                continue

            url = r.get("fullUrl") or ""
            if url.startswith("/"):
                url = (r.get("origin") or "").rstrip("/") + url
            self.add(title=title, start=start, end=end, venue=v,
                     category=cat, subcategory=sub, cat_source=csrc,
                     source_id=src_id, url=url or None,
                     description=strip_tags(r.get("excerpt")))

    def ticketmaster(self, src_id, rows):
        """Discovery API events. The first source here that states a price.

        Geography is the constraint that matters. The NYC DMA is a media market,
        not a city -- it reaches Newark, Long Island and lower Connecticut, and
        the sample that proved this adapter works was a Monster Jam at the
        Prudential Center. So an event is kept only if its venue's coordinates
        land inside an actual NYC neighbourhood polygon. The NTA file is already
        the arbiter of "where is this" everywhere else in the pipeline; here it
        doubles as the arbiter of "is this even our city".
        """
        # A standing exhibition sold as timed entry is one event per day, not
        # one per slot. The Balloon Museum alone ships 1,038 rows in a 30-day
        # window -- 34 admission times a day -- which would make a single
        # attraction 12% of the whole index and bury everything else. Collapse
        # to the first slot of each day; the listing answers "is this on today",
        # which is the question the index exists to answer.
        slots = Counter()
        for e in rows:
            st = ((e.get("dates") or {}).get("start") or {})
            slots[(clean(e.get("name")), st.get("localDate"))] += 1
        kept_day = set()

        for e in rows:
            title = clean(e.get("name"))
            bad = taxonomy.rejected_title(title)
            if bad:
                self.reject(src_id, title, bad, url=e.get("url"))
                continue

            dates = e.get("dates") or {}
            day_key = (title, (dates.get("start") or {}).get("localDate"))
            if slots[day_key] > 2:
                if day_key in kept_day:
                    self.stats["tm_timed_entry_collapsed"] += 1
                    continue
                kept_day.add(day_key)

            st = dates.get("start") or {}
            # TBA/TBD events have no time to sort or filter by. A listings index
            # is a promise about when -- an event that cannot answer that is not
            # a listing yet.
            if st.get("dateTBA") or st.get("dateTBD") or st.get("noSpecificTime"):
                self.reject(src_id, title, "date TBA/TBD", url=e.get("url"))
                continue
            status = ((dates.get("status") or {}).get("code") or "").lower()
            if status in ("cancelled", "canceled", "postponed"):
                self.reject(src_id, title, f"status: {status}", url=e.get("url"))
                continue

            start = parse_naive(f"{st.get('localDate')} {st.get('localTime') or '00:00:00'}")
            if not start:
                self.reject(src_id, title, "unparseable start time", url=e.get("url"))
                continue

            venues = (e.get("_embedded") or {}).get("venues") or []
            v0 = venues[0] if venues else {}
            loc = v0.get("location") or {}
            try:
                lat = float(loc["latitude"])
                lon = float(loc["longitude"])
            except (KeyError, TypeError, ValueError):
                self.reject(src_id, title, "venue has no coordinates", url=e.get("url"))
                continue

            v = self.venue(v0.get("name") or "Unknown venue", lat, lon, kind="venue")
            if not v or not v.get("neighborhood"):
                self.reject(src_id, title, "outside nyc", url=e.get("url"))
                continue

            cat, sub, csrc = taxonomy.from_ticketmaster(e.get("classifications"))
            if not cat:
                cat, sub, csrc = taxonomy.from_title(title)
            if not cat:
                # The building, when the listing will not say. Broadway arrives
                # from Ticketmaster with segment `Undefined` and no genre.
                cat, sub, csrc = taxonomy.from_venue(v["name"])
            if not cat:
                self.reject(src_id, title, "unclassified ticketmaster event",
                            url=e.get("url"))
                continue

            # priceRanges is present on a minority of events and is the only
            # stated price anywhere in this pipeline. Absent stays null.
            price_min = is_free = None
            pr = e.get("priceRanges") or []
            if pr:
                mins = [p.get("min") for p in pr if isinstance(p.get("min"), (int, float))]
                if mins:
                    price_min = min(mins)
                    is_free = price_min == 0

            self.add(title=title, start=start, end=None, venue=v,
                     category=cat, subcategory=sub, cat_source=csrc,
                     source_id=src_id, url=e.get("url"),
                     price_min=price_min, is_free=is_free)

    def luma(self, src_id, rows):
        """Luma calendars -- meetups, run clubs, reading groups, craft circles.

        The only source here covering organiser-led community programming, which
        is invisible to Open Data and unsellable through Ticketmaster. It also
        arrives with GEO coordinates on nearly every event, so it places well.

        The calendar name is the fallback classifier. An event called "Session 4"
        says nothing; "NYC Backgammon Club" says it is games. That is the same
        move as reading Broadway off the theatre's name -- context that the
        source states as fact, not a guess about the title.
        """
        for r in rows:
            title = clean(r.get("summary"))
            bad = taxonomy.rejected_title(title)
            if bad:
                self.reject(src_id, title, bad)
                continue
            if (r.get("status") or "").upper() == "CANCELLED":
                self.reject(src_id, title, "status: cancelled")
                continue

            start = to_local_naive(r.get("start"), self.tz)
            end = to_local_naive(r.get("end"), self.tz)
            if not start:
                self.reject(src_id, title, "unparseable start time")
                continue

            # A Luma calendar is a publisher, not a place. "Claude Community
            # Events" is one calendar carrying meetups in Medellín, Singapore,
            # Adelaide and Bengaluru, and subscribing to it as an NYC source put
            # a Medellín workshop on the page. Coordinates are the only thing
            # that says where an event is, so no coordinates means no listing.
            lat, lon = r.get("lat"), r.get("lon")
            if lat is None or lon is None:
                self.reject(src_id, title, "no coordinates, cannot place")
                continue

            # Test THIS event's coordinates, not the venue's. Venues are cached
            # by name and keep the first location they were seen at, which is
            # the right call for a building and the wrong one for a host that
            # runs meetups on four continents -- a cached NYC organiser would
            # otherwise wave through every foreign event that followed it.
            here = self.resolver.resolve(lat=lat, lon=lon)
            if not here or not here.as_dict().get("neighborhood"):
                self.reject(src_id, title, "outside nyc")
                continue

            # Hosts are the venue here. A run club is not a building, but it is
            # the stable name this event belongs to, and GEO does the placing.
            v = self.venue(r.get("organizer") or r.get("calendar_name") or "Luma",
                           lat, lon, kind="organizer")
            if not v:
                self.reject(src_id, title, "no venue could be resolved")
                continue

            cat, sub, csrc = taxonomy.from_title(title)
            if not cat:
                cat, sub, _ = taxonomy.from_title(r.get("calendar_name") or "")
                csrc = "luma:calendar-name" if cat else None
            if not cat:
                self.reject(src_id, title, "unclassified luma event")
                continue

            # The canonical link is inside the DESCRIPTION prose, not a URL
            # property -- Luma puts "Find more information on https://luma.com/x"
            # there and leaves URL unset.
            url = None
            m = LUMA_URL.search(r.get("description") or "")
            if m:
                url = m.group(0)
            elif (r.get("location") or "").startswith("http"):
                url = r["location"]

            self.add(title=title, start=start, end=end, venue=v, place=here,
                     category=cat, subcategory=sub, cat_source=csrc,
                     source_id=src_id, url=url,
                     description=r.get("description"))

    def dice(self, src_id, rows):
        """DICE -- independent music and nightlife, read from schema.org markup.

        The venue tier that sells through neither Ticketmaster nor its own site:
        Elsewhere, Union Pool, Saint Vitus, House of Yes, Public Records. Every
        event arrives with a street address AND coordinates, which makes this
        the best-placed source in the pipeline after Squarespace.

        Geography is settled the Ticketmaster way. The city filter in `dice.py`
        is a string match on a URL slug and is allowed to be generous; the
        coordinates in each event's own markup are the arbiter, and anything
        landing outside a real neighbourhood polygon is dropped. A slug is a
        label someone typed, a coordinate is a claim about the world.

        CLASSIFICATION ORDER IS THE INTERESTING PART, and it is not the obvious
        one. DICE marks specific things specifically -- ScreeningEvent,
        ComedyEvent -- and those beat the title, because a screening called
        "THE HUMAN OPERA" is not classical music and the title alone says it is.
        But its default is MusicEvent, covering 22 of 25 sampled listings, so
        that has to LOSE to the title or a trivia night at a music venue becomes
        a concert. Specific type, then title, then the generic type, then the
        building. Each step is a claim of decreasing confidence.

        Price is null. DICE states one on the page but not in the markup, and
        the markup is the part it publishes to be read. Reading the price out of
        the surrounding HTML would be scraping the page rather than using what
        the page offers, which is a different bargain from the one taken here.
        """
        for r in rows:
            title = clean(r.get("name"))
            bad = taxonomy.rejected_title(title)
            if bad:
                self.reject(src_id, title, bad, url=r.get("url"))
                continue

            status = str(r.get("eventStatus") or "").rsplit("/", 1)[-1].lower()
            if status in ("eventcancelled", "eventpostponed"):
                self.reject(src_id, title, f"status: {status}", url=r.get("url"))
                continue

            start = to_local_naive(r.get("startDate"), self.tz)
            end = to_local_naive(r.get("endDate"), self.tz)
            if not start:
                self.reject(src_id, title, "unparseable start time", url=r.get("url"))
                continue

            try:
                lat = float(r["lat"])
                lon = float(r["lon"])
            except (KeyError, TypeError, ValueError):
                self.reject(src_id, title, "no coordinates, cannot place",
                            url=r.get("url"))
                continue

            v = self.venue(clean(r.get("venue_name")) or "DICE", lat, lon,
                           kind="venue")
            if not v or not v.get("neighborhood"):
                self.reject(src_id, title, "outside nyc", url=r.get("url"))
                continue

            cat, sub, csrc = taxonomy.from_schema(r.get("type"))
            if not cat:
                cat, sub, csrc = taxonomy.from_title(title)
            if not cat:
                cat, sub, csrc = taxonomy.from_schema_default(r.get("type"))
            if not cat:
                cat, sub, csrc = taxonomy.from_venue(v["name"])
            if not cat:
                self.reject(src_id, title, "unclassified dice event",
                            url=r.get("url"))
                continue

            self.add(title=title, start=start, end=end, venue=v,
                     category=cat, subcategory=sub, cat_source=csrc,
                     source_id=src_id, url=r.get("url"),
                     description=r.get("description"))

    def bpl(self, src_id, rows):
        """Brooklyn Public Library -- every branch, one open Drupal JSON:API.

        The first library source, and the first where the *operational* record
        and the *public listing* are the same table. `field_event_type_parent`
        says which is which: `Program` is a public programme, while
        `Meeting Room` is a room somebody booked, `In-Branch Tabling` is a
        table in a lobby, `Visit` is a class trip. Publishing those would repeat
        the permits mistake exactly -- 4,364 private permits filed as public
        street fairs, because nobody asked what the record was *for*.

        Virtual events are dropped rather than placed. A Zoom talk has no
        neighbourhood, and this index is a claim about where.

        Timestamps arrive as local wall-clock already (fetch.py strips the fake
        UTC offset), so they are parsed naive and handed straight to `add`.
        """
        for r in rows:
            title = clean(r.get("title"))
            bad = taxonomy.rejected_title(title)
            if bad:
                self.reject(src_id, title, bad, url=self._bpl_url(r))
                continue

            kind = (r.get("kind") or "").strip()
            if kind and kind not in BPL_PUBLIC:
                self.reject(src_id, title, f"internal record: {kind}",
                            url=self._bpl_url(r))
                continue
            if r.get("virtual") and not r.get("hybrid"):
                self.reject(src_id, title, "virtual only, has no place",
                            url=self._bpl_url(r))
                continue

            start = parse_naive((r.get("start") or "").replace("+00:00", ""))
            end = parse_naive((r.get("end") or "").replace("+00:00", ""))
            if not start:
                self.reject(src_id, title, "unparseable start time")
                continue

            # "Brooklyn Heights Library, Gaming Room" is one building and one
            # room inside it. The room is not a venue -- keeping it would give
            # every branch a dozen venue pages and split its events across them.
            where = r.get("location") or (r.get("branches") or [None])[0]
            if not where:
                self.reject(src_id, title, "no branch named", url=self._bpl_url(r))
                continue
            branch = clean(where.split(",")[0])
            v = self.venue(branch, borough="Brooklyn", kind="library")

            # The tag vocabulary is the venue's own words -- "author talks",
            # "arts and crafts", "astronomy". Ground truth beats the title,
            # which is where a "Chess for Adults" reads as sport.
            cat = sub = csrc = None
            for tag in r.get("tags") or []:
                c, s, _ = taxonomy.from_title(str(tag))
                if c:
                    cat, sub, csrc = c, s, "bpl:tags"
                    break
            if not cat:
                cat, sub, csrc = taxonomy.from_title(title)
            if not cat:
                # A library programme with nothing else to go on is a library
                # programme. This is the one default here, and it is stated
                # rather than guessed: the source is a library's own calendar.
                cat, sub, csrc = "learning", "class", "default:bpl"

            flags = []
            for aud in (r.get("audience") or []) if isinstance(r.get("audience"), list) \
                    else [r.get("audience")]:
                if aud:
                    flags.append(slugify(str(aud)))

            self.add(title=title, start=start, end=end, venue=v,
                     category=cat, subcategory=sub, cat_source=csrc,
                     source_id=src_id, url=self._bpl_url(r),
                     description=strip_tags(r.get("body")), flags=flags)

    @staticmethod
    def _bpl_url(r):
        p = r.get("path")
        return f"https://www.bklynlibrary.org{p}" if p else None

    def wordpress(self, src_id, rows):
        """Venue calendars on The Events Calendar, REST or ICS.

        Location is the whole difficulty. Tribe's `venue` object is empty on
        most of these sites, so the building's OSM coordinates ride in on the
        registry instead -- which also settles the geography question, because
        the census swept a bounding box and the roster contains Hoboken, Newark
        Symphony Hall and the Meadowlands. An event whose venue does not land in
        an NYC neighbourhood is rejected, the same test Ticketmaster gets.

        Where the feed DOES state its own coordinates they win for that event:
        a venue that runs a walking tour or a second stage knows better than
        the building we geocoded.
        """
        for r in rows:
            title = clean(r.get("title"))
            bad = taxonomy.rejected_title(title)
            if bad:
                self.reject(src_id, title, bad, url=r.get("url"))
                continue
            if (r.get("status") or "").lower() not in ("", "publish", "confirmed"):
                self.reject(src_id, title, f"status: {r.get('status')}",
                            url=r.get("url"))
                continue

            start = parse_naive(r.get("start")) or to_local_naive(r.get("start"), self.tz)
            end = parse_naive(r.get("end")) or to_local_naive(r.get("end"), self.tz)
            if not start:
                self.reject(src_id, title, "unparseable start time", url=r.get("url"))
                continue

            lat, lon = r.get("venue_lat"), r.get("venue_lon")
            try:
                lat = float(lat) if lat not in (None, "") else None
                lon = float(lon) if lon not in (None, "") else None
            except (TypeError, ValueError):
                lat = lon = None
            place = None
            if lat is not None and lon is not None:
                place = self.resolver.resolve(lat=lat, lon=lon)

            v = self.venue(r.get("venue_name") or r.get("venue_host"),
                           r.get("lat"), r.get("lon"),
                           borough=r.get("borough"), kind="venue")
            if not v:
                self.reject(src_id, title, "no venue could be resolved",
                            url=r.get("url"))
                continue
            here = place or v
            hood = here.as_dict()["neighborhood"] if place else v.get("neighborhood")
            if not hood:
                self.reject(src_id, title, "outside nyc", url=r.get("url"))
                continue

            cat = sub = csrc = None
            for word in (r.get("categories") or []) + (r.get("tags") or []):
                c, s, _ = taxonomy.from_title(str(word))
                if c:
                    cat, sub, csrc = c, s, "wordpress:terms"
                    break
            if not cat:
                cat, sub, csrc = taxonomy.from_title(title)
            if not cat:
                cat, sub, csrc = taxonomy.from_venue(v["name"])
            if not cat:
                self.reject(src_id, title, "unclassified wordpress event",
                            url=r.get("url"))
                continue

            # `cost` is free text -- "$20", "Free", "$15 – $45", "". Only the
            # word free is unambiguous enough to act on, and everything else
            # stays null rather than becoming a parsed guess.
            is_free = None
            cost = (r.get("cost") or "").strip().lower()
            if cost in ("free", "free!", "$0", "0"):
                is_free = True

            self.add(title=title, start=start, end=end, venue=v,
                     place=place, category=cat, subcategory=sub, cat_source=csrc,
                     source_id=src_id, url=r.get("url"),
                     description=strip_tags(r.get("description")),
                     is_free=is_free)

    # -- run ---------------------------------------------------------------
    def run(self):
        raw_dir = os.path.join(ROOT, "raw", self.city)
        handlers = {"nyc-parks-upcoming": self.parks, "nyc-permitted-events": self.permits,
                    "nyc-squarespace": self.squarespace,
                    "nyc-ticketmaster": self.ticketmaster,
                    "nyc-luma": self.luma,
                    "nyc-dice": self.dice,
                    "nyc-bpl": self.bpl,
                    "nyc-wordpress": self.wordpress}
        used = []
        for fn in sorted(os.listdir(raw_dir)):
            if not fn.endswith(".json"):
                continue
            sid = fn[:-5]
            with open(os.path.join(raw_dir, fn)) as f:
                blob = json.load(f)
            h = handlers.get(sid)
            if not h:
                print(f"  no handler for {sid}; skipped")
                continue
            print(f"  {sid}: {len(blob['rows'])} raw rows")
            h(sid, blob["rows"])
            used.append({"id": sid, "label": blob["source"]["label"],
                         "attribution": blob["source"].get("attribution"),
                         "fetched_at": blob.get("fetched_at"),
                         "raw_rows": len(blob["rows"])})

        self.events.sort(key=lambda e: (e["start_utc"], e["title"]))
        venues = [v for v in self.venues.values() if v["count"] > 0]
        for v in venues:
            v["subway"] = self.near_subway(v["lat"], v["lon"])
        venues.sort(key=lambda v: (-v["count"], v["name"]))

        # Which stations stand INSIDE each neighbourhood, rather than which are
        # nearest to it. A neighbourhood is an area, and "the L runs through
        # here" is the claim worth making; nearest-station to a centroid would
        # put a train in a neighbourhood it only passes beside.
        hood_stations = defaultdict(list)
        for s in self.subway.stations:
            area = self.hoods.locate(s["lon"], s["lat"])
            if area:
                hood_stations[area["slug"]].append(s)

        hood_counts = Counter(e["neighborhood"] for e in self.events if e["neighborhood"])
        cat_counts = Counter(e["category"] for e in self.events)
        with_hood = sum(1 for e in self.events if e["neighborhood"])

        meta = {
            "city": self.city,
            "city_name": self.cfg["name"],
            "tz": self.cfg["tz"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "events": len(self.events),
                "venues": len(venues),
                "neighborhoods_with_events": len(hood_counts),
                "rejected": len(self.rejected),
            },
            "coverage": {
                "neighborhood": round(100 * with_hood / max(1, len(self.events)), 1),
                "coordinates": round(100 * sum(1 for e in self.events if e["lat"]) /
                                     max(1, len(self.events)), 1),
                "borough": round(100 * sum(1 for e in self.events if e["borough"]) /
                                 max(1, len(self.events)), 1),
                "url": round(100 * sum(1 for e in self.events if e["url"]) /
                             max(1, len(self.events)), 1),
                "price": round(100 * sum(1 for e in self.events if e["is_free"] is not None) /
                               max(1, len(self.events)), 1),
            },
            "by_category": dict(cat_counts.most_common()),
            "by_neighborhood": dict(hood_counts.most_common()),
            "by_location_source": dict(Counter(
                e.get("location_source") or "unresolved" for e in self.events).most_common()),
            "sources": used,
            "subway_stations": len(self.subway),
            "coverage_subway": round(
                100 * sum(1 for e in self.events if e.get("subway")) /
                max(1, len(self.events)), 1),
            "stats": dict(self.stats.most_common()),
        }

        # One record per neighbourhood that has something on, carrying the
        # trains that serve it. Written as its own file rather than folded into
        # meta: the UI reads it per edition page, and meta is already the
        # build's own report card.
        hoods_out = []
        for slug, n in hood_counts.most_common():
            sample = next((e for e in self.events if e["neighborhood"] == slug), None)
            stations = hood_stations.get(slug) or []
            routes = sorted({r for s in stations for r in s["routes"]})
            hoods_out.append({
                "slug": slug,
                "name": sample["neighborhood_name"] if sample else slug,
                "borough": sample["borough"] if sample else None,
                "count": n,
                "routes": routes,
                "stations": [{"name": s["name"], "routes": s["routes"],
                              "ada": s["ada"]} for s in
                             sorted(stations, key=lambda s: s["name"] or "")],
            })

        out = os.path.join(ROOT, "data", self.city)
        os.makedirs(out, exist_ok=True)
        for name, payload in (("events", self.events), ("venues", venues),
                              ("rejected", self.rejected),
                              ("neighborhoods", hoods_out), ("meta", meta)):
            with open(os.path.join(out, f"{name}.json"), "w") as f:
                json.dump(payload, f, separators=(",", ":") if name != "meta" else None,
                          indent=None if name != "meta" else 2)
        return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    args = ap.parse_args()

    print(f"normalize: {args.city}")
    meta = Builder(args.city).run()
    c, cov = meta["counts"], meta["coverage"]
    print(f"\n  {c['events']} events · {c['venues']} venues · "
          f"{c['neighborhoods_with_events']} neighbourhoods · {c['rejected']} rejected")
    print(f"  coverage: neighbourhood {cov['neighborhood']}% · coords {cov['coordinates']}% · "
          f"url {cov['url']}% · price {cov['price']}%")
    print("  categories: " + ", ".join(f"{k} {v}" for k, v in meta["by_category"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
