"""iCalendar, read far enough to build a listing. Stdlib only.

Not a general RFC 5545 implementation -- it reads VEVENTs and stops. What it
does handle is the part that actually bites when you point a parser at real
feeds in the wild:

  * **Line folding.** Long SUMMARY and DESCRIPTION values are folded at 75
    octets, and a naive line-anchored regex reads half a title.
  * **Three DTSTART shapes.** `...Z` is UTC, `;TZID=` is a wall-clock time in a
    named zone, and `;VALUE=DATE` is an all-day event with no time at all.
    Treating the third as midnight would sort every all-day event into the
    late-night band, so it is reported as a date and flagged.
  * **Escaping.** `\\,` `\\;` `\\n` are literal in ICS text values. Left alone
    they show up verbatim on the page.

Recurrence is deliberately NOT expanded. RRULE is returned as a raw string so a
caller can decide; expanding it correctly means honouring EXDATE, RDATE, UNTIL,
COUNT and DST transitions, and a wrong expansion silently invents events that
do not exist -- much worse here than missing a few that do.
"""
import re
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # pragma: no cover
    ZoneInfo = None

FOLD = re.compile(r"\r?\n[ \t]")
PROP = re.compile(r"^(?P<name>[A-Za-z0-9-]+)(?P<params>;[^:]*)?:(?P<value>.*)$")
TZID = re.compile(r"TZID=([^;:]+)", re.I)
UNESCAPE = {"\\n": "\n", "\\N": "\n", "\\,": ",", "\\;": ";", "\\\\": "\\"}
UNESC_RE = re.compile(r"\\[nN,;\\]")


def unfold(text):
    return FOLD.sub("", text)


def untext(v):
    return UNESC_RE.sub(lambda m: UNESCAPE[m.group(0)], v or "").strip()


def parse_dt(value, params, default_tz=None):
    """Return (datetime_or_date, all_day). Aware datetimes, always.

    An all-day value has no instant -- it is a date. Returning it as one lets
    the caller decide what a day means in its own timezone rather than having
    this guess midnight UTC.
    """
    v = (value or "").strip()
    if not v:
        return None, False
    if "VALUE=DATE" in (params or "").upper() and "DATE-TIME" not in (params or "").upper():
        try:
            return datetime.strptime(v, "%Y%m%d").date(), True
        except ValueError:
            return None, False
    try:
        if v.endswith("Z"):
            return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
        naive = datetime.strptime(v, "%Y%m%dT%H%M%S")
    except ValueError:
        try:
            return datetime.strptime(v, "%Y%m%d").date(), True
        except ValueError:
            return None, False

    tzname = None
    m = TZID.search(params or "")
    if m:
        tzname = m.group(1).strip().strip('"')
    tz = None
    if tzname and ZoneInfo:
        try:
            tz = ZoneInfo(tzname)
        except Exception:
            tz = None
    tz = tz or default_tz or timezone.utc
    return naive.replace(tzinfo=tz), False


def parse_geo(value):
    """GEO is `lat;lon`. Returns (lat, lon) or (None, None)."""
    try:
        lat, lon = (value or "").split(";", 1)
        return float(lat), float(lon)
    except (ValueError, AttributeError):
        return None, None


def events(text, default_tz=None):
    """Yield one dict per VEVENT.

    Keys: uid, summary, description, location, url, status, organizer, rrule,
    start, end, all_day, lat, lon. `start`/`end` are aware datetimes, or dates
    when `all_day`.
    """
    if not text:
        return []
    body = unfold(text)
    out = []
    for chunk in body.split("BEGIN:VEVENT")[1:]:
        chunk = chunk.split("END:VEVENT")[0]
        e = {"uid": None, "summary": None, "description": None, "location": None,
             "url": None, "status": None, "organizer": None, "rrule": None,
             "start": None, "end": None, "all_day": False, "lat": None, "lon": None}
        for line in chunk.splitlines():
            m = PROP.match(line.strip())
            if not m:
                continue
            name = m.group("name").upper()
            params = m.group("params") or ""
            value = m.group("value")
            if name == "DTSTART":
                e["start"], e["all_day"] = parse_dt(value, params, default_tz)
            elif name == "DTEND":
                e["end"], _ = parse_dt(value, params, default_tz)
            elif name == "SUMMARY":
                e["summary"] = untext(value)
            elif name == "DESCRIPTION":
                e["description"] = untext(value)
            elif name == "LOCATION":
                e["location"] = untext(value)
            elif name == "URL":
                e["url"] = value.strip()
            elif name == "UID":
                e["uid"] = value.strip()
            elif name == "STATUS":
                e["status"] = value.strip().upper()
            elif name == "GEO":
                e["lat"], e["lon"] = parse_geo(value)
            elif name == "ORGANIZER":
                cn = re.search(r'CN="?([^";:]+)"?', params)
                e["organizer"] = cn.group(1).strip() if cn else None
            elif name == "RRULE":
                e["rrule"] = value.strip()
        if e["start"] is not None:
            out.append(e)
    return out


def upcoming(items, now=None, horizon_days=None, tz=None):
    """Filter to events that have not finished yet, inside an optional horizon.

    An all-day event counts as running until the end of its day -- a festival on
    today's date should not vanish from the listing at 00:01.
    """
    now = now or datetime.now(timezone.utc)
    ceil = now + timedelta(days=horizon_days) if horizon_days else None
    out = []
    for e in items:
        s = e["start"]
        if isinstance(s, date) and not isinstance(s, datetime):
            s_dt = datetime(s.year, s.month, s.day, tzinfo=tz or timezone.utc)
            end_dt = s_dt + timedelta(days=1)
        else:
            s_dt = s
            end_dt = e["end"] if isinstance(e["end"], datetime) else s_dt
        if end_dt < now:
            continue
        if ceil and s_dt > ceil:
            continue
        out.append(e)
    return out
