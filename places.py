#!/usr/bin/env python3
"""Places -- the things that are OPEN, as opposed to the things that are ON.

Museums, galleries, cinemas, theatres, music venues, arcades, escape rooms,
bowling, climbing, ice rinks. A scheduled event answers "what's on tonight"; a
place answers "what could we do", which is a different question people ask just
as often and which no events feed covers.

It is also the only source in this build that carries PHOTOGRAPHS.

    OpenStreetMap (Overpass)  -> the places themselves, with coordinates,
                                 website, opening hours. Keyless, free.
    Commons REST              -> file metadata (author, description page) for
                                 images OSM itself names, via
                                 api.wikimedia.org, which publishes no
                                 robots.txt. Keyless, free.

ON WIKIDATA IMAGES, and why they are opt-in:

Most places here carry a Wikidata id, and Wikidata's P18 property would yield a
photo for roughly two thirds of them. Getting it means calling
wikidata.org/w/api.php -- and that host's robots.txt says `Disallow: /w/` for
`User-agent: *`, with no exception for the API. Wikimedia's own API policy
invites programmatic use with a descriptive User-Agent, and essentially every
Wikidata consumer does exactly this, so the two documents point in different
directions.

This pipeline honours robots.txt by default, so it does NOT call it unless
asked. `--wikidata-images` is the repo owner deciding otherwise, explicitly,
and it is the only place in this codebase that skips the check. It sends the
descriptive contact User-Agent Wikimedia's API policy asks for.

An image whose author cannot be established is dropped rather than shown
uncredited.

Images are referenced at source and never rehosted -- the same rule as
everything else here, and simultaneously the correct rights position and the
cheapest bandwidth policy.

    python3 places.py            # fetch + resolve + write
    python3 places.py --no-fetch # rebuild from the cached Overpass response
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import http  # noqa: E402
from lib.geo import Neighborhoods  # noqa: E402
from lib.places import Resolver  # noqa: E402
import subway  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
# Overpass instances, in preference order. overpass-api.de publishes
# `Disallow: /api/`, so it is NOT used -- the community mirrors serve no
# robots.txt at all, which per RFC 9309 means unrestricted. The instance is
# chosen at runtime by asking robots, not assumed.
OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
WIKIDATA = "https://www.wikidata.org/w/api.php"
COMMONS = "https://commons.wikimedia.org/w/api.php"

# OSM tag -> our kind. A closed vocabulary, same discipline as the event
# taxonomy: the facet has to stay governable.
KINDS = {
    ("tourism", "museum"): ("museum", "Museum"),
    ("tourism", "gallery"): ("gallery", "Gallery"),
    ("tourism", "aquarium"): ("aquarium", "Aquarium"),
    ("tourism", "zoo"): ("zoo", "Zoo"),
    ("amenity", "theatre"): ("theatre", "Theatre"),
    ("amenity", "cinema"): ("cinema", "Cinema"),
    ("amenity", "arts_centre"): ("arts-centre", "Arts centre"),
    ("amenity", "nightclub"): ("nightclub", "Nightclub"),
    ("amenity", "music_venue"): ("music-venue", "Music venue"),
    ("leisure", "escape_game"): ("escape-room", "Escape room"),
    ("leisure", "amusement_arcade"): ("arcade", "Arcade"),
    ("leisure", "bowling_alley"): ("bowling", "Bowling"),
    ("leisure", "ice_rink"): ("ice-rink", "Ice rink"),
    ("leisure", "sports_centre"): ("climbing", "Climbing / sports"),
}

QUERY = """[out:json][timeout:180];
(
%s
);
out center tags;"""


def build_query(bbox, pairs):
    s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
    box = f"({s},{w},{n},{e})"
    lines = [f'  nwr["{k}"="{v}"]{box};' for (k, v) in pairs]
    return QUERY % "\n".join(lines)


def load_overrides(cdir):
    """Per-city corrections to what OSM says about a place.

    OSM is the name of record here, and it is occasionally wrong -- a business
    gets tagged off a signboard, or by somebody who misheard it. Hand-editing
    data/ does not fix that, because the next run of this script overwrites the
    file; the correction has to live where the data is built.

    Keyed by OSM id, which is the one identifier that survives a rename. `was`
    records the value being corrected: when OSM stops matching it, either
    somebody fixed it upstream or the place genuinely changed its name, and
    both want a human rather than a silent overwrite. It warns instead of
    failing, so a stale entry can never break a build.

    An entry here is a stopgap. The durable fix is an edit to OSM itself, which
    then makes the entry redundant -- and the `was` mismatch is what tells you
    the day that happened.
    """
    path = os.path.join(cdir, "place-overrides.json")
    if not os.path.exists(path):
        return {}
    return json.load(open(path))


def slugify(s, maxlen=70):
    out = []
    for ch in (s or "").lower():
        out.append(ch if ch.isalnum() else "-")
    return re.sub(r"-+", "-", "".join(out)).strip("-")[:maxlen].strip("-")


def fetch_osm(city, bbox):
    """Query Overpass via http.get, so the robots check actually applies.

    An earlier version POSTed with urllib directly and therefore skipped the
    check entirely -- which is exactly how a polite crawler stops being one
    without anyone noticing. Every outbound request goes through lib.http.
    """
    # One tag pair per request. A single 14-pair city-wide query is heavy
    # enough that public instances time it out; split, it is trivial for them
    # and a failure costs one category rather than the whole run.
    mirrors = [b for b in OVERPASS_MIRRORS if http.allowed(f"{b}?data=x")]
    skipped = [urllib.parse.urlsplit(b).netloc
               for b in OVERPASS_MIRRORS if f"{b}?data=x" and not http.allowed(f"{b}?data=x")]
    for h in skipped:
        print(f"  skip {h}: robots.txt disallows it")
    if not mirrors:
        raise RuntimeError("every known Overpass instance disallows this in robots.txt")

    print(f"overpass: {len(KINDS)} tag pairs over {city}, one request each")
    els, failed = [], []
    for (k, v) in KINDS:
        q = urllib.parse.urlencode({"data": build_query(bbox, [(k, v)])})
        got = None
        for base in mirrors:
            try:
                got = http.get_json(f"{base}?{q}", delay=1.5, timeout=180, retries=1)
                break
            except Exception:
                continue
        if got is None:
            failed.append(f"{k}={v}")
            continue
        n = got.get("elements", [])
        els.extend(n)
        print(f"    {k}={v}: {len(n)}")
    if failed:
        # Never silently. A category that failed to fetch is a coverage hole,
        # and a hole nobody is told about reads as "there is nothing there".
        print(f"  WARNING: {len(failed)} tag pairs failed: {', '.join(failed)}")
    print(f"  {len(els)} elements total")
    return els


WM_REST = "https://api.wikimedia.org/core/v1/commons/file/"
WP_HTML = "https://api.wikimedia.org/core/v1/wikipedia/{lang}/page/{title}/html"

# The infobox photo, from the article HTML. Precise because a human put it
# there -- unlike searching Commons by name, which cheerfully returns a Kharkiv
# museum for "Museum of Sex". Precision is the whole reason this stage exists.
INFOBOX_IMG = re.compile(
    r'<img[^>]+src="(?://|https?://)upload\.wikimedia\.org/wikipedia/commons/'
    r'(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^"/?#]+)', re.I)
LOGO_ISH = re.compile(r"\.(svg|png)$|logo|seal|icon|map|locator", re.I)


def wikipedia_lead_image(tag):
    """OSM `wikipedia=en:Some Article` -> a Commons filename, or None.

    Reads only the head of the article HTML via a Range request, and skips
    logos, seals and locator maps -- they are images, but they are not a
    photograph of the place.
    """
    tag = (tag or "").strip()
    if not tag:
        return None
    lang, _, title = tag.partition(":")
    if not title or len(lang) > 3:
        lang, title = "en", tag
    url = WP_HTML.format(lang=lang, title=urllib.parse.quote(title.replace(" ", "_"), safe=""))
    try:
        body = http.get(url, delay=1.2, timeout=45, retries=1,
                        max_bytes=260_000).decode("utf-8", "replace")
    except Exception:
        return None
    for m in INFOBOX_IMG.finditer(body):
        fn = urllib.parse.unquote(m.group(1))
        fn = re.sub(r"^\d+px-", "", fn)
        if LOGO_ISH.search(fn):
            continue
        return fn
    return None


def osm_image_ref(tags):
    """An image reference OSM itself carries -- a human tagged it, so it is
    precise in a way that searching by name never is.

    Only Wikimedia references are usable: a Google Drive link or a random host
    has no licence we can establish and no guarantee of staying up.
    """
    for key in ("image", "wikimedia_commons"):
        v = (tags.get(key) or "").strip()
        if not v:
            continue
        if v.startswith("File:"):
            return v[5:]
        m = re.match(r"https?://upload\.wikimedia\.org/wikipedia/commons/"
                     r"(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/?#]+)", v)
        if m:
            return urllib.parse.unquote(m.group(1))
        m = re.match(r"https?://\w+\.wikipedia\.org/wiki/File:(.+)$", v)
        if m:
            return urllib.parse.unquote(m.group(1))
    return None


COMMONS_THUMB = re.compile(
    r"^(https://upload\.wikimedia\.org/wikipedia/commons)/(?:thumb/)?"
    r"([0-9a-f]/[0-9a-f]{2}/[^/]+?)(?:/\d+px-[^/]+)?$")


def thumb_url(url, width=500):
    """Ask Commons for a card-sized rendition instead of the original.

    The REST API's "preferred" URL is routinely 300-400 KB, which is absurd in
    a 230px card -- 176 of those is 60 MB of someone else's bandwidth to draw
    one screen.

    Commons only serves a per-file list of widths, so a rewritten URL is a
    guess and has to be VERIFIED. verify_images() below does that and falls
    back to the unrewritten URL; nothing ships unchecked.
    """
    if not url:
        return url
    m = COMMONS_THUMB.match(url)
    if not m:
        return url
    base, rest = m.group(1), m.group(2)
    name = rest.rsplit("/", 1)[-1]
    if name.lower().endswith(".svg"):
        return url
    return f"{base}/thumb/{rest}/{width}px-{name}"


def verify_images(places, delay=0.25):
    """HEAD every image and keep only the ones that actually resolve.

    Commons rejects widths outside a per-file list, so the rewritten thumbnail
    may 400. Fall back to the full-size URL rather than shipping a broken card
    -- and if even that fails, null the image so the category block renders
    immediately instead of after a browser timeout. A credit line under a
    missing photo is worse than no photo.
    """
    import time
    checked, fixed, dropped = 0, 0, 0
    for p in places:
        url = p.get("image_url")
        if not url:
            continue
        for cand in (url, re.sub(r"/thumb/(.+)/\d+px-[^/]+$", r"/\1", url)):
            try:
                req = urllib.request.Request(
                    cand, headers={"User-Agent": http.UA}, method="HEAD")
                with urllib.request.urlopen(req, timeout=20) as r:
                    if r.status == 200:
                        if cand != url:
                            p["image_url"] = cand
                            fixed += 1
                        break
            except Exception:
                continue
            finally:
                time.sleep(delay)
        else:
            p.update(image_url=None, image_credit=None,
                     image_license=None, image_page=None, image_source=None)
            dropped += 1
        checked += 1
    print(f"  verified {checked} images · {fixed} fell back to full size · "
          f"{dropped} dropped")


def commons_file(filename):
    """Metadata for one Commons file via the REST API on api.wikimedia.org --
    a host that publishes no robots.txt, unlike the /w/ action API."""
    url = WM_REST + urllib.parse.quote(f"File:{filename}", safe=":")
    try:
        d = http.get_json(url, delay=1.0, timeout=30)
    except Exception:
        return None
    if "title" not in d:
        return None
    author = ((d.get("latest") or {}).get("user") or {}).get("name")
    if not author:
        return None      # unattributable -> unusable
    pref = (d.get("preferred") or {}).get("url")
    thumb = (d.get("thumbnail") or {}).get("url")
    return {"url": thumb_url(pref or thumb or (d.get("original") or {}).get("url")),
            "credit": author, "page": d.get("file_description_url")}


def wikidata_images(qids):
    """Batch P18 lookups. 50 ids per call is the API's documented limit."""
    out = {}
    for i in range(0, len(qids), 50):
        chunk = qids[i:i + 50]
        url = WIKIDATA + "?" + urllib.parse.urlencode(
            {"action": "wbgetentities", "ids": "|".join(chunk),
             "props": "claims", "format": "json"})
        try:
            # check_robots=False ONLY here: see the module docstring. An
            # operator opted in via --wikidata-images.
            data = http.get_json(url, delay=1.2, timeout=45, check_robots=False)
        except Exception as e:
            print(f"  wikidata batch failed: {e}")
            continue
        for qid, ent in (data.get("entities") or {}).items():
            claims = (ent.get("claims") or {}).get("P18")
            if claims:
                try:
                    out[qid] = claims[0]["mainsnak"]["datavalue"]["value"]
                except (KeyError, TypeError):
                    pass
        print(f"  wikidata {min(i + 50, len(qids))}/{len(qids)} -> {len(out)} images")
    return out


def commons_credits(filenames):
    """Licence and author for each file. CC requires attribution, so a file we
    cannot attribute is not usable and gets dropped."""
    out = {}
    files = list(filenames)
    for i in range(0, len(files), 40):
        chunk = files[i:i + 40]
        url = COMMONS + "?" + urllib.parse.urlencode(
            {"action": "query", "titles": "|".join(f"File:{f}" for f in chunk),
             "prop": "imageinfo", "iiprop": "extmetadata|url",
             "iiurlwidth": "800", "format": "json"})
        try:
            data = http.get_json(url, delay=1.2, timeout=45, check_robots=False)
        except Exception as e:
            print(f"  commons batch failed: {e}")
            continue
        for page in (data.get("query", {}).get("pages") or {}).values():
            ii = (page.get("imageinfo") or [{}])[0]
            meta = ii.get("extmetadata") or {}
            title = (page.get("title") or "")[5:]
            lic = (meta.get("LicenseShortName") or {}).get("value")
            artist = (meta.get("Artist") or {}).get("value")
            if not lic:
                continue
            out[title] = {
                "license": re.sub(r"<[^>]+>", "", lic).strip(),
                "credit": re.sub(r"<[^>]+>", "", artist or "").strip()[:120] or None,
                "thumb": ii.get("thumburl"),
            }
        print(f"  commons {min(i + 40, len(files))}/{len(files)} -> {len(out)} credited")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--verify-delay", type=float, default=0.7,
                    help="pause between image HEAD checks; too fast reads as "
                         "a burst and gets throttled, which looks like a "
                         "broken image but isn't")
    ap.add_argument("--image-limit", type=int, default=400,
                    help="cap article fetches per run; the cache makes it resumable")
    ap.add_argument("--wikidata-images", action="store_true",
                    help="also resolve photos via Wikidata P18. See the module "
                         "docstring: wikidata.org/robots.txt disallows /w/, so "
                         "this is a deliberate choice, not a default.")
    args = ap.parse_args()

    cdir = os.path.join(ROOT, "cities", args.city)
    ddir = os.path.join(ROOT, "data", args.city)
    rdir = os.path.join(ROOT, "raw", args.city)
    os.makedirs(ddir, exist_ok=True)
    os.makedirs(rdir, exist_ok=True)
    cfg = json.load(open(os.path.join(cdir, "city.json")))
    overrides = load_overrides(cdir)

    raw_path = os.path.join(rdir, "osm-places.json")
    if args.no_fetch and os.path.exists(raw_path):
        els = json.load(open(raw_path))["elements"]
        print(f"places: {len(els)} elements from cache")
    else:
        els = fetch_osm(args.city, cfg["bbox"])
        with open(raw_path, "w") as f:
            json.dump({"elements": els}, f)

    hoods = Neighborhoods(json.load(open(os.path.join(cdir, "neighborhoods.geojson"))))
    resolver = Resolver(hoods)
    stations = subway.load(args.city)
    print(f"  subway: {len(stations)} stations")

    # -- shape the OSM elements -------------------------------------------
    seen, places = set(), []
    stats = Counter()
    for el in els:
        t = el.get("tags") or {}
        osm_id = f"{el.get('type')}/{el.get('id')}"
        name = (t.get("name") or "").strip()

        ov = overrides.get(osm_id)
        if ov:
            was = ov.get("was")
            if was is not None and was != name:
                print(f"  ! {osm_id}: OSM now says {name!r}, but the override "
                      f"was written against {was!r} -- recheck "
                      f"cities/{args.city}/place-overrides.json; upstream may "
                      f"already be fixed")
            name = (ov.get("name") or name).strip()
            stats["overridden"] += 1

        if not name:
            stats["unnamed"] += 1
            continue
        kind = kind_label = None
        for (k, v), (slug, label) in KINDS.items():
            if t.get(k) == v:
                kind, kind_label = slug, label
                break
        if not kind:
            continue
        # sports_centre is only interesting when it is actually climbing
        if kind == "climbing" and "climbing" not in json.dumps(t).lower():
            continue

        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            stats["no-coords"] += 1
            continue

        slug = slugify(name)
        key = (slug, round(lat, 4))
        if not slug or key in seen:
            stats["dupe"] += 1
            continue
        seen.add(key)

        p = resolver.resolve(lat=lat, lon=lon)
        places.append({
            "id": slug, "name": name, "kind": kind, "kind_label": kind_label,
            # Same nearest-station treatment events get. A museum you cannot
            # work out how to reach is not a place you are going to go, and
            # without this a place could not answer the station filter at all.
            "subway": stations.nearest(lat, lon, limit=1),
            "website": t.get("website") or t.get("contact:website"),
            "opening_hours": t.get("opening_hours"),
            "wikidata": t.get("wikidata"),
            "osm_image": osm_image_ref(t),
            "wikipedia": t.get("wikipedia"),
            "image_url": None, "image_credit": None, "image_license": None,
            "image_page": None, "image_source": None,
            "osm_id": osm_id,
            **(p.as_dict() if p else {}),
        })
        stats[f"kind:{kind}"] += 1

    # -- attach images -----------------------------------------------------
    if not args.no_images:
        # Default path: only images OSM itself names. Precise, robots-clean,
        # and small.
        named = sorted({p["osm_image"] for p in places if p.get("osm_image")})
        print(f"  {len(named)} places carry an image reference in OSM itself")
        by_file = {}
        for fn in named:
            info = commons_file(fn)
            if info:
                by_file[fn] = info
            else:
                stats["image-unattributable"] += 1
        for p in places:
            info = by_file.get(p.get("osm_image") or "")
            if info:
                p.update(image_url=info["url"], image_credit=info["credit"],
                         image_license="see Commons file page",
                         image_page=info["page"], image_source="osm+commons")
                stats["with-image"] += 1

        # Second pass: the Wikipedia article's infobox photo, for places OSM
        # links to an article. api.wikimedia.org publishes no robots.txt, so
        # unlike the Wikidata action API this needs no exception.
        cache_path = os.path.join(ddir, "image-cache.json")
        icache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
        want = [p for p in places if p.get("wikipedia") and not p.get("image_url")]
        print(f"  {len(want)} places link to a Wikipedia article")
        for i, p in enumerate(want[:args.image_limit], 1):
            key = p["wikipedia"]
            if key not in icache:
                icache[key] = wikipedia_lead_image(key)
                if i % 25 == 0:
                    print(f"    …{i}/{min(len(want), args.image_limit)}")
                    with open(cache_path, "w") as f:
                        json.dump(icache, f, indent=0, sort_keys=True)
            fn = icache.get(key)
            if not fn:
                continue
            info = by_file.get(fn) or commons_file(fn)
            if not info:
                stats["image-unattributable"] += 1
                continue
            by_file[fn] = info
            p.update(image_url=info["url"], image_credit=info["credit"],
                     image_license="see Commons file page",
                     image_page=info["page"], image_source="wikipedia-infobox")
            stats["with-image"] += 1
        with open(cache_path, "w") as f:
            json.dump(icache, f, indent=0, sort_keys=True)

    if args.wikidata_images:
        qids = sorted({p["wikidata"] for p in places
                       if p.get("wikidata") and not p.get("image_url")})
        print(f"  {len(qids)} places carry a wikidata id")
        imgs = wikidata_images(qids)
        credits = commons_credits(sorted(set(imgs.values())))
        for p in places:
            fn = imgs.get(p.get("wikidata") or "")
            if not fn:
                continue
            cr = credits.get(fn)
            if not cr:
                # No attribution -> not usable. Dropping is the correct
                # outcome, not showing it uncredited.
                stats["image-unattributable"] += 1
                continue
            if p.get("image_url"):
                continue
            p["image_url"] = thumb_url(cr.get("thumb")) or (
                "https://commons.wikimedia.org/wiki/Special:FilePath/"
                + urllib.parse.quote(fn) + "?width=800")
            p["image_credit"] = cr["credit"]
            p["image_license"] = cr["license"]
            p["image_source"] = "wikidata:P18/commons"
            stats["with-image"] += 1

    if not args.no_images:
        verify_images(places, delay=args.verify_delay)

    places.sort(key=lambda p: (p["kind"], p["name"]))
    with open(os.path.join(ddir, "places.json"), "w") as f:
        json.dump(places, f, separators=(",", ":"))

    withimg = sum(1 for p in places if p["image_url"])
    withhood = sum(1 for p in places if p.get("neighborhood"))
    print(f"\n  {len(places)} places · {withimg} with a credited photo "
          f"({100 * withimg // max(1, len(places))}%) · "
          f"{withhood} placed to a neighbourhood "
          f"({100 * withhood // max(1, len(places))}%)")
    for k, v in stats.most_common(18):
        print(f"    {v:>5}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
