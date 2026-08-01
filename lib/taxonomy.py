"""The closed taxonomy, and classification into it.

Two rules inherited from gearherd, both load-bearing:

  1. Ground truth beats keywords. Both NYC sources carry their own taxonomy
     (Parks `categories`, permits `event_type`), so the primary path is MAPPING
     one vocabulary onto another. Keyword matching only runs when a source
     carries no usable label. Guessing from a title is the fallback, never the
     first move.
  2. No LLM. Thousands of events refreshed several times a day would make
     LLM classification a recurring bill scaling with volume, non-deterministic,
     and a violation of nothing-is-estimated. This is a dict.

The vocabulary is CLOSED. An open tag set makes crossed pages ungovernable and
the facet UI unusable. New subcategories get added deliberately, when the data
shows a cluster big enough to deserve a page.
"""

CATEGORIES = {
    "music":       ["concert", "dj-set", "open-mic", "classical", "jazz"],
    "sports":      ["fitness", "yoga", "running", "basketball", "swimming",
                    "cycling", "martial-arts", "sports-general"],
    "food-drink":  ["market", "tasting", "pop-up", "class"],
    "art":         ["exhibition", "film-screening", "arts-crafts", "public-art"],
    "performance": ["dance", "theatre", "comedy", "storytelling"],
    "learning":    ["talk", "workshop", "class", "history"],
    "community":   ["street-fair", "block-party", "parade", "volunteer",
                    "market", "health", "games", "religious"],
    "outdoors":    ["park-programming", "walking-tour", "nature", "waterfront",
                    "gardening"],
}

LABELS = {
    "music": "Music", "sports": "Sports", "food-drink": "Food & Drink",
    "art": "Art", "performance": "Performance", "learning": "Learning",
    "community": "Community", "outdoors": "Outdoors",
}

# Hex per category -- the single source of truth, mirrored in branding.html and
# src/styles/app.css. Contrast-checked; `on` says which text colour survives.
COLORS = {
    "music":       {"hex": "#FF3D8B", "on": "ink"},
    "sports":      {"hex": "#3FB65A", "on": "ink"},
    "food-drink":  {"hex": "#FF6A13", "on": "ink"},
    "art":         {"hex": "#00B4CE", "on": "ink"},
    "performance": {"hex": "#6B3FD4", "on": "paper"},
    "learning":    {"hex": "#1B54D6", "on": "paper"},
    "community":   {"hex": "#F2C500", "on": "ink"},
    "outdoors":    {"hex": "#7FB800", "on": "ink"},
}

# ---------------------------------------------------------------------------
# Ground truth: NYC Parks `categories` (pipe-separated, several per event).
# (priority, category, subcategory) -- highest priority wins, so "Basketball"
# beats the generic "Sports" that always accompanies it.
# ---------------------------------------------------------------------------
PARKS_MAP = {
    "basketball/netball":            (9, "sports", "basketball"),
    "swimming/aquatics":             (9, "sports", "swimming"),
    "running/jogging":               (9, "sports", "running"),
    "yoga & pilates classes":        (9, "sports", "yoga"),
    "bike/cycling":                  (9, "sports", "cycling"),
    "martial arts":                  (9, "sports", "martial-arts"),
    "dance classes":                 (8, "performance", "dance"),
    "dance":                         (7, "performance", "dance"),
    "film":                          (8, "art", "film-screening"),
    "arts & crafts":                 (8, "art", "arts-crafts"),
    "art":                           (6, "art", "public-art"),
    "music":                         (8, "music", "concert"),
    "concerts":                      (8, "music", "concert"),
    "theater":                       (8, "performance", "theatre"),
    "comedy":                        (8, "performance", "comedy"),
    "gardening":                     (8, "outdoors", "gardening"),
    "nature":                        (7, "outdoors", "nature"),
    "urban park rangers":            (6, "outdoors", "nature"),
    "birding":                       (8, "outdoors", "nature"),
    "waterfront":                    (7, "outdoors", "waterfront"),
    "walking":                       (7, "outdoors", "walking-tour"),
    "tours":                         (7, "outdoors", "walking-tour"),
    "history":                       (7, "learning", "history"),
    "education":                     (6, "learning", "talk"),
    "workshops":                     (7, "learning", "workshop"),
    "talks":                         (7, "learning", "talk"),
    "volunteer":                     (8, "community", "volunteer"),
    "it's my park":                  (8, "community", "volunteer"),
    "games":                         (6, "community", "games"),
    "food":                          (7, "food-drink", "tasting"),
    "markets":                       (8, "community", "market"),
    "exercise classes":              (5, "sports", "fitness"),
    "outdoor fitness":               (5, "sports", "fitness"),
    "shape up nyc":                  (5, "sports", "fitness"),
    "fitness":                       (4, "sports", "fitness"),
    "summer sports experience":      (4, "sports", "sports-general"),
    "recreation center programming": (2, "sports", "sports-general"),
    "sports":                        (3, "sports", "sports-general"),
}

# Audience tags, not categories. "Best for Kids" describes who an event is for,
# not what it is -- collapsing the two would make the facet lie.
PARKS_FLAGS = {
    "best for kids": "kids",
    "accessible activities": "accessible",
    "seniors": "seniors",
    "teens": "teens",
}

# ---------------------------------------------------------------------------
# Ground truth: permitted-events `event_type`.
# ---------------------------------------------------------------------------
PERMIT_MAP = {
    "block party":              ("community", "block-party"),
    "single block festival":    ("community", "block-party"),
    "street festival":          ("community", "street-fair"),
    "farmers market":           ("community", "market"),
    "sidewalk sale":            ("community", "market"),
    "parade":                   ("community", "parade"),
    "plaza event":              ("community", "street-fair"),
    "plaza partner event":      ("community", "street-fair"),
    "street event":             ("community", "street-fair"),
    "open street partner event":("community", "street-fair"),
    "health fair":              ("community", "health"),
    "religious event":          ("community", "religious"),
    "athletic race / tour":     ("sports", "running"),
    "open culture":             ("music", "concert"),
    "special event":            ("community", "street-fair"),
}

# Quarantined, with the reason recorded. These are permits, not things you can
# go to: a youth-league field booking is paperwork; a shooting permit is a film
# crew closing your street. Rejects land in data/<city>/rejected.json WITH their
# titles, so the classifier is audited by reading a file rather than re-deriving
# the evidence.
PERMIT_REJECT = {
    "sport - youth":                 "league field permit, not a public event",
    "sport - adult":                 "league field permit, not a public event",
    "production event":              "film/TV production permit",
    "shooting permit":               "film/TV production permit",
    "theater load in and load outs":  "logistics, not an event",
    "stationary demonstration":      "demonstration permit, not programming",
}

# Title-level rejects. The permitted-events feed is a PERMIT register, not a
# listings feed, so a share of its rows describe closures, logistics and
# placeholder paperwork rather than anything you could turn up to. Ground truth
# can't catch these -- "East Meadow Closure" is filed as a Special Event -- so
# the title gets a second look purely to throw things out.
import re as _re

TITLE_REJECT = [
    (_re.compile(r"\bclosur(e|es)\b|\bclosed\b|\bshut ?down\b", _re.I),
     "closure or shutdown, not an event"),
    (_re.compile(r"^\s*(parks? event|event|tbd|n/?a|test|permit|none|misc\.?)\s*$", _re.I),
     "placeholder title, no event described"),
    (_re.compile(r"\bmaintenance\b|\bload[- ]?(in|out)\b|\bbreak[- ]?down\b|"
                 r"\bset[- ]?up only\b|\bstaging\b", _re.I),
     "logistics, not an event"),
    (_re.compile(r"\bfilming\b|\bfilm shoot\b|\bphoto shoot\b", _re.I),
     "film/TV production"),
]


def rejected_title(title):
    """Return a reason string if this title is not a public event, else None."""
    t = (title or "").strip()
    if len(t) < 3:
        return "title too short to be an event"
    for pat, reason in TITLE_REJECT:
        if pat.search(t):
            return reason
    return None


# Last resort only, on titles that arrived with no usable source label.
TITLE_KEYWORDS = [
    (("farmers market", "greenmarket", "flea", "bazaar"), "community", "market"),
    (("parade",), "community", "parade"),
    (("block party",), "community", "block-party"),
    (("concert", "live music", "band", "orchestra", "philharmonic"), "music", "concert"),
    (("dj ", " dj", "dance party"), "music", "dj-set"),
    (("jazz",), "music", "jazz"),
    (("film", "movie", "screening", "cinema"), "art", "film-screening"),
    (("comedy", "stand-up", "standup"), "performance", "comedy"),
    (("theater", "theatre", "play "), "performance", "theatre"),
    (("dance",), "performance", "dance"),
    (("yoga", "pilates"), "sports", "yoga"),
    (("run ", "5k", "10k", "marathon"), "sports", "running"),
    (("bike", "cycling"), "sports", "cycling"),
    (("tour", "walk"), "outdoors", "walking-tour"),
    (("workshop",), "learning", "workshop"),
    (("lecture", "talk", "panel", "reading"), "learning", "talk"),
    (("volunteer", "cleanup", "clean-up"), "community", "volunteer"),
    (("festival", "fair", "fest"), "community", "street-fair"),
]


def valid(category, subcategory):
    return category in CATEGORIES and subcategory in CATEGORIES[category]


def from_parks(categories_str):
    """Map a Parks pipe-separated category string. Returns (cat, sub, flags, source)."""
    flags = []
    best = None
    for part in (categories_str or "").split("|"):
        key = part.strip().lower()
        if not key:
            continue
        if key in PARKS_FLAGS:
            flags.append(PARKS_FLAGS[key])
            continue
        hit = PARKS_MAP.get(key)
        if hit and (best is None or hit[0] > best[0]):
            best = hit
    if best and valid(best[1], best[2]):
        return best[1], best[2], flags, "parks:categories"
    return None, None, flags, None


def from_permit(event_type):
    """Map a permit event_type. Returns (cat, sub, source) or ('__reject__', reason, None)."""
    key = (event_type or "").strip().lower()
    if key in PERMIT_REJECT:
        return "__reject__", PERMIT_REJECT[key], None
    hit = PERMIT_MAP.get(key)
    if hit:
        return hit[0], hit[1], "permit:event_type"
    return None, None, None


def from_title(title):
    """Fallback only. Returns (cat, sub, source)."""
    t = (title or "").lower()
    for words, cat, sub in TITLE_KEYWORDS:
        if any(w in t for w in words):
            return cat, sub, "title"
    return None, None, None
