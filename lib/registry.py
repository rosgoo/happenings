"""Ask a CMS what content types it has, instead of guessing which plugin it runs.

Every earlier probe in this repo guessed. The census looked for an ICS feed the
site advertised in `<link rel=alternate>`; `ical.py` guessed three archive slugs;
the first tribe probe checked one plugin's route. All three ask *"is it the
plugin I have in mind?"* and file "no" as "no feed exists". Those are different
sentences, and the difference cost 78 venues.

Two endpoints answer the question directly, with no key and no advertisement:

  WordPress   /wp-json/wp/v2/types    every registered post type + its rest_base
  Drupal      /jsonapi                every node type as a JSON:API resource

The slugs are the argument for it. Real ones on NYC institutions: `walkingtours`,
`performancearchive`, `show-programs`, `lpr_events`, `ch_events`, `mec-events`,
`online-exhibitions`. Nothing reaches those by guessing.

Three grades of answer, and conflating them is how the count goes wrong:

  registered   the type exists. Says nothing about whether it answers.
  answering    /wp-json/wp/v2/{base} returns rows -- titles, links, bodies.
  dated        a plugin registered its date meta in REST, so the payload holds
               the real start: `_EventStartDate`, `WooCommerceEventsDate`,
               `_eventorganiser_schedule_start_start`.

Only the third is a feed. The top-level `date`/`date_gmt` on a WP REST row are
the *post publish* timestamps -- when the page went up, not when the event runs.
A filter that accepts them (or `modified`) reports a feed that is not there.
"""
import json
import re

from . import http

# Wide on purpose. A false positive costs one request; a false negative costs a
# venue, and undercounting is the failure this module exists to correct.
EVENTISH = re.compile(
    r"event|screening|program|performance|show|calendar|class|exhibit|concert|"
    r"workshop|tour|product|ticket", re.I)

# Registered by plugins that are not calendars. `jp_*` is Jetpack's internal
# bookkeeping, `tec_calendar_embed` holds a shortcode, and `tribe_*_tickets` are
# ticket products attached to an event rather than the event itself.
NOISE = {
    "jp_pay_product", "jp_act_log_event", "jp_product_search",
    "tec_calendar_embed", "tribe_rsvp_tickets", "tribe_tpp_tickets",
    "single-class-cancell", "dt_slideshow", "block-showcase",
}

# Meta keys that carry a real event start. POST_DATE is the trap: those four are
# properties of the web page, not of the event.
DATE_META = re.compile(r"date|start|time|when", re.I)
POST_DATE = {"date", "date_gmt", "modified", "modified_gmt"}

JSON_ACCEPT = "application/json"
JSONAPI_ACCEPT = "application/vnd.api+json,application/json;q=0.9"


def _json(url, delay, accept=JSON_ACCEPT, max_bytes=2_000_000, check_robots=True):
    """Fetch and parse, or None. A probe's failures are answers, not errors."""
    r = http.get_full(url, delay=delay, accept=accept, max_bytes=max_bytes,
                      check_robots=check_robots)
    if r["status"] != 200 or "json" not in (r["ctype"] or ""):
        return None, r
    try:
        return json.loads(r["body"].decode("utf-8", "replace")), r
    except ValueError:
        return None, r


def wp_types(origin, delay=1.0):
    """Registered post types that might hold events -> {slug: rest_base}.

    None means the registry did not answer at all, which is different from an
    empty dict (it answered and has nothing).
    """
    d, _ = _json(f"{origin}/wp-json/wp/v2/types", delay)
    if not isinstance(d, dict):
        return None
    return {k: v.get("rest_base") for k, v in d.items()
            if isinstance(v, dict) and v.get("rest_base")
            and EVENTISH.search(k) and k not in NOISE}


def drupal_types(origin, delay=1.0):
    """Event-ish JSON:API node types, e.g. ['node--event', 'node--screening']."""
    d, _ = _json(f"{origin}/jsonapi", delay, accept=JSONAPI_ACCEPT)
    if not isinstance(d, dict):
        return None
    links = d.get("links") or {}
    return sorted(k for k in links
                  if k.startswith("node--") and EVENTISH.search(k))


def wp_collection(origin, base, delay=1.0, per_page=3):
    """Does /wp-json/wp/v2/{base} actually answer, and with what?

    Returns None when it does not. `event_date_meta` is the field that decides
    whether this is a feed or a list of web pages.
    """
    url = f"{origin}/wp-json/wp/v2/{base}?per_page={per_page}"
    rows, r = _json(url, delay, max_bytes=1_500_000)
    if not isinstance(rows, list) or not rows:
        return None
    sample = rows[0]
    meta = sample.get("meta") or {}
    # Header names are case-insensitive per RFC 9110; the casing is the
    # server's choice. X-WP-Total gives a collection's size in one request
    # rather than paging to find out.
    total = next((v for k, v in (r.get("headers") or {}).items()
                  if k.lower() == "x-wp-total"), None)
    return {
        "base": base,
        "total": total,
        "returned": len(rows),
        "event_date_meta": sorted(k for k in meta
                                  if DATE_META.search(k) and k not in POST_DATE),
        "sample_title": (sample.get("title") or {}).get("rendered", "")[:80],
        "sample_link": sample.get("link", ""),
    }


def tribe_rest(origin, delay=1.0, per_page=1):
    """The Events Calendar's own route -> event count, or None.

    Worth a separate probe because Tribe keeps `_EventStartDate` OFF `wp/v2` and
    serves the whole event here instead. A site can look dateless at the generic
    endpoint and be a complete feed at this one.
    """
    d, _ = _json(f"{origin}/wp-json/tribe/events/v1/events?per_page={per_page}",
                 delay)
    if not isinstance(d, dict):
        return None
    total = d.get("total")
    return total if isinstance(total, int) else len(d.get("events") or [])


TRIBE_ICS_PATHS = ("/events/?ical=1", "/?post_type=tribe_events&ical=1")


def tribe_ics(origin, delay=1.0, listing=None):
    """A working ICS export -> {url, vevents}, or None.

    Only counts if the body really is a VCALENDAR. Tribe's export path answers
    200 with an HTML page when the plugin is absent, and `ical.py` learned the
    hard way that a 200 is not a feed. The site's own archive is tried first
    where the census found one -- a venue that renamed /events to /whats-on
    still answers there, and the slug-independent form catches the rest.
    """
    paths = list(TRIBE_ICS_PATHS)
    if listing:
        sep = "&" if "?" in listing else "?"
        paths.insert(0, listing + sep + "ical=1")
    for path in paths:
        url = path if path.startswith("http") else origin + path
        r = http.get_full(url, delay=delay, accept="text/calendar,*/*;q=0.8")
        body = r["body"] or b""
        if r["status"] == 200 and body.lstrip()[:15] == b"BEGIN:VCALENDAR":
            return {"url": url, "vevents": body.count(b"BEGIN:VEVENT")}
    return None
