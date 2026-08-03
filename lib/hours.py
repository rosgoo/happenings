"""OSM `opening_hours` -> the same four time bands events are filtered by.

A place is OPEN rather than ON, which is why the places section used to ignore
the time-of-day filter entirely. That is right for a gallery and wrong for a
comedy club: "evening, near Washington Square" should surface the room that
opens at seven, not the museum that shut at five. The only thing standing
between those two answers is the opening_hours string OSM already carries.

WHAT THIS PARSES, AND WHAT IT REFUSES TO

The opening_hours grammar is large -- month ranges, week numbers, nth-weekday
selectors, sunset offsets, holidays, comments. This reads the everyday subset:

    24/7
    Mo-Fr 09:00-17:00
    Tu-Su 10:00-17:00; Mo off
    Mo-Th 16:00-04:00; Fr 14:00-04:00; Sa-Su 12:00-04:00
    Th, Fr 19:00-24:00; Sa, Su 16:00-24:00
    09:00-12:00,13:00-18:00

Anything else -- a seasonal `Nov-Mar`, a `sunset-23:00`, a date range -- returns
None, meaning UNKNOWN. Unknown is not open and it is not closed, and the
front-end shows it as unknown. Guessing that an ice rink tagged `Nov-Mar` is
open in August would be exactly the kind of estimate this project doesn't make.

THE BANDS ARE normalize.band()'s BANDS, deliberately

    morning    00:00-11:59      evening    17:00-20:59
    afternoon  12:00-16:59      late       21:00-23:59

Including the part that reads oddly: an hour after midnight is `morning`, not
`late`, because that is how an event at 00:30 is already classified and one
vocabulary that is strange in the same way everywhere beats two that disagree.
So a bar open `Fr 14:00-04:00` is open Friday evening, Friday late, and
Saturday morning -- which is true, and is what "still open at 2am" has to mean
in a scheme where the day rolls at midnight.

    python3 lib/hours.py      # self-check
"""
import re

# (name, first hour, last hour exclusive) -- normalize.band()'s cut points.
BAND_HOURS = (("morning", 0, 12), ("afternoon", 12, 17),
              ("evening", 17, 21), ("late", 21, 24))
BAND_BIT = {name: 1 << i for i, (name, _, _) in enumerate(BAND_HOURS)}
ALL_BANDS = (1 << len(BAND_HOURS)) - 1

DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}

# A day selector: Mo, Mo-Fr, or a comma list of either. Spaces after the commas
# are common in the wild ("Th, Fr 19:00-24:00") and are not an error.
_DAY = r"(?:Mo|Tu|We|Th|Fr|Sa|Su)"
DAY_SELECTOR = re.compile(rf"^({_DAY}(?:-{_DAY})?(?:\s*,\s*{_DAY}(?:-{_DAY})?)*)\b")
TIME_RANGE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
COMMENT = re.compile(r'"[^"]*"')
# Holiday selectors say nothing about which weekdays a place is open. Dropping
# them is not the same as failing to understand them.
HOLIDAY = re.compile(r"\b(?:PH|SH)\b")


def _bands_between(start_min, end_min):
    """Bit mask per day for one interval, as (mask_today, mask_tomorrow).

    `end_min` may exceed 1440 -- a 16:00-04:00 bar closes on the next day, and
    the spill belongs to that day rather than being clipped away.
    """
    today = tomorrow = 0
    spill = end_min - 1440          # the part after midnight, from 00:00
    for name, h0, h1 in BAND_HOURS:
        b0, b1 = h0 * 60, h1 * 60
        if start_min < b1 and end_min > b0:
            today |= BAND_BIT[name]
        if spill > b0:
            tomorrow |= BAND_BIT[name]
    return today, tomorrow


def _parse_days(sel):
    """'Mo-We, Fr' -> {0, 1, 2, 4}. Ranges may wrap: Fr-Mo is four days."""
    out = set()
    for part in sel.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            i, j = DAY_INDEX[a.strip()], DAY_INDEX[b.strip()]
            n = (j - i) % 7
            out.update((i + k) % 7 for k in range(n + 1))
        else:
            out.add(DAY_INDEX[part])
    return out


def week_mask(spec):
    """`opening_hours` -> a 7-character hex mask, one digit per weekday from
    Monday, or None if the string is outside the subset above.

    Each digit is the OR of the bands open that day, so "open Tuesday evening"
    is `int(mask[1], 16) & BAND_BIT['evening']` in Python and the identical
    expression in the browser.
    """
    if not spec:
        return None
    spec = COMMENT.sub(" ", str(spec)).strip()
    if not spec:
        return None
    if re.fullmatch(r"24/7|00:00-24:00", spec.replace(" ", "")):
        return "f" * 7

    days = [0] * 7
    touched = False
    for rule in spec.split(";"):
        holiday = bool(HOLIDAY.search(rule))
        rule = HOLIDAY.sub(" ", rule).strip().strip(",").strip()
        if not rule:
            continue

        m = DAY_SELECTOR.match(rule)
        if holiday and not m:
            # `PH off` is about Thanksgiving, not about Tuesdays. Reading the
            # remainder as a bare all-week rule would have closed the place
            # for the entire week, which is how a holiday note turns into a
            # permanently shut venue.
            continue

        if m:
            try:
                which = _parse_days(m.group(1))
            except KeyError:
                return None
            # "Su,PH 10:00-18:00" leaves a dangling comma once PH is gone.
            rest = rule[m.end():].strip(" ,").strip()
        else:
            # A bare time range applies to the whole week: "07:00-22:00".
            which, rest = set(range(7)), rule

        low = rest.lower()
        if low in ("off", "closed"):
            for d in which:
                days[d] = 0
            touched = True
            continue
        if not rest or low in ("open", "unknown"):
            # "Mo-Fr open" claims a day with no hours on it. Unknown, not all
            # day -- and unknown for one rule is unknown for the whole string.
            return None

        for chunk in rest.split(","):
            tm = TIME_RANGE.match(chunk.strip())
            if not tm:
                return None            # month range, sunset, nth-weekday, ...
            h0, m0, h1, m1 = (int(x) for x in tm.groups())
            if h0 > 24 or h1 > 24 or m0 > 59 or m1 > 59:
                return None
            start = h0 * 60 + m0
            end = h1 * 60 + m1
            if end <= start:
                end += 1440            # crosses midnight
            today, tomorrow = _bands_between(start, end)
            for d in which:
                days[d] |= today
                days[(d + 1) % 7] |= tomorrow
            touched = True

    if not touched or not any(days):
        return None
    return "".join(f"{d:x}" for d in days)


def bands(mask):
    """Every band the place is open in at some point in the week."""
    if not mask:
        return []
    bits = 0
    for ch in mask:
        bits |= int(ch, 16)
    return [name for name, _, _ in BAND_HOURS if bits & BAND_BIT[name]]


def _selftest():
    cases = [
        ("24/7", "fffffff"),
        # A museum: afternoon and morning on the days it opens, nothing on Mo.
        ("Tu-Su 10:00-17:00", "0333333"),
        ("Mo-Fr 09:00-17:00", "3333300"),
        # Crossing midnight: afternoon-evening-late today, morning tomorrow --
        # every day, because the week wraps and Sunday's 4am spills into Monday.
        ("Mo-Th 16:00-04:00; Fr 14:00-04:00; Sa-Su 12:00-04:00", "fffffff"),
        ("Th, Fr 19:00-24:00; Sa, Su 16:00-24:00", "000ccee"),
        ("09:00-12:00,13:00-18:00", "7777777"),
        ("Mo-Su 20:30-04:00", "ddddddd"),
        # A holiday rule must not close the week, and must not eat its day.
        ("Mo-Fr 09:00-17:00; PH off", "3333300"),
        ("Su,PH 10:00-18:00", "0000007"),   # 18:00 reaches into the evening
        ("Mo-Su 12:00-4:00", "fffffff"),      # single-digit hour, past midnight
        # Refusals.
        ("Nov-Mar 10:00-22:00", None),
        ("sunset-23:00", None),
        ("Mo-Fr 09:00-17:00; Jan 01 off", None),
        ("", None),
        (None, None),
    ]
    bad = 0
    for spec, want in cases:
        got = week_mask(spec)
        if got != want:
            print(f"  FAIL {spec!r}: {got!r} != {want!r}")
            bad += 1
    # A bar open until 4am is open 'late' on the night you would go.
    m = week_mask("Fr 14:00-04:00")
    assert m and int(m[4], 16) & BAND_BIT["late"], m
    assert m and int(m[5], 16) & BAND_BIT["morning"], m
    assert not (int(m[4], 16) & BAND_BIT["morning"]), m
    print("hours: all good" if not bad else f"hours: {bad} failing")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
