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
        self.events, self.rejected = [], []
        self.venues = {}
        self.seen = set()
        self.stats = Counter()

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
            hood = self.hoods.locate(lon, lat) if (lat is not None and lon is not None) else None
            v = {
                "id": slug, "name": name, "kind": kind,
                "lat": lat, "lon": lon,
                "neighborhood": hood["slug"] if hood else None,
                "neighborhood_name": hood["name"] if hood else None,
                "neighborhood_source": "point-in-polygon" if hood else None,
                "nta_type": hood["nta_type"] if hood else None,
                "borough": (hood["borough"] if hood else borough),
                "borough_source": "point-in-polygon" if hood else ("source" if borough else None),
                "count": 0,
            }
            self.venues[slug] = v
        elif v["lat"] is None and lat is not None:
            # A later row carried coordinates the first one didn't. Upgrade.
            hood = self.hoods.locate(lon, lat)
            v.update(lat=lat, lon=lon)
            if hood:
                v.update(neighborhood=hood["slug"], neighborhood_name=hood["name"],
                         neighborhood_source="point-in-polygon", nta_type=hood["nta_type"],
                         borough=hood["borough"], borough_source="point-in-polygon")
        return v

    # -- emit --------------------------------------------------------------
    def add(self, *, title, start, end, venue, category, subcategory, cat_source,
            source_id, url=None, description=None, flags=None, price_min=None,
            is_free=None):
        if not title or not start or venue is None:
            self.stats["dropped_incomplete"] += 1
            return
        eid = event_id(venue["id"], start, title)
        if eid in self.seen:
            self.stats["deduped"] += 1
            return
        self.seen.add(eid)

        start_aware = start.replace(tzinfo=self.tz)
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
            "neighborhood": venue["neighborhood"],
            "neighborhood_name": venue["neighborhood_name"],
            "borough": venue["borough"],
            "lat": venue["lat"], "lon": venue["lon"],
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

    # -- run ---------------------------------------------------------------
    def run(self):
        raw_dir = os.path.join(ROOT, "raw", self.city)
        handlers = {"nyc-parks-upcoming": self.parks, "nyc-permitted-events": self.permits}
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
        venues.sort(key=lambda v: (-v["count"], v["name"]))

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
                "url": round(100 * sum(1 for e in self.events if e["url"]) /
                             max(1, len(self.events)), 1),
                "price": round(100 * sum(1 for e in self.events if e["is_free"] is not None) /
                               max(1, len(self.events)), 1),
            },
            "by_category": dict(cat_counts.most_common()),
            "by_neighborhood": dict(hood_counts.most_common()),
            "sources": used,
            "stats": dict(self.stats.most_common()),
        }

        out = os.path.join(ROOT, "data", self.city)
        os.makedirs(out, exist_ok=True)
        for name, payload in (("events", self.events), ("venues", venues),
                              ("rejected", self.rejected), ("meta", meta)):
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
