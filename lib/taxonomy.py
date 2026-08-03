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
    # "Special Event" is deliberately absent. It is 5,988 of 33,052 permit rows
    # and says nothing: a Union Square greenmarket, a Classical Theatre of
    # Harlem performance and someone's birthday barbecue all arrive under it.
    # Mapping it to street-fair tagged 4,364 private permits as public street
    # fairs and made "community" 58% of the whole index. With no entry here it
    # must earn a category from its title, and is rejected if it cannot --
    # which is the same bargain every other unlabelled source gets.
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
    # A permit for a private gathering is not a listing. These five words are
    # the entire title on 1,793 permit rows -- "Celebration" 533 times,
    # "Miscellaneous" 523, "Picnic" 308, "Barbecue" 236, "Party" 181. Nobody
    # browsing tonight's events is looking for the 236th barbecue, and the
    # permit says nothing about it being open to anyone.
    (_re.compile(r"^\s*(celebration|miscellaneous|picnic|barbecue|bbq|party|"
                 r"gathering|reunion|gathering of friends|family gathering|"
                 r"birthday(\s+party)?|wedding|baby shower|memorial service)"
                 r"\s*$", _re.I),
     "generic private permit, not a public event"),
    (_re.compile(r"\bsummer camp\b|\bday camp\b|\bafter[- ]?school\b|"
                 r"\bteam practice\b|\btraining\b|\bclinic\b", _re.I),
     "camp or training programme, not a public event"),
    (_re.compile(r"\bpermit\b", _re.I),
     "permit record, not an event"),
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


# ---------------------------------------------------------------- libraries

# A library calendar is two things in one table: programmes you would go to, and
# services you would use. "Free Summer Meals" runs at 123 branches, the Period
# Pantry at 56, and neither is something anybody browses for -- they are a
# window during which a counter is staffed. Recurring adult instruction is the
# same shape from the other side: Free English Class is 123 rows and is
# genuinely valuable, but nobody picks it off a listings site on a Thursday
# night. It is a course you enrol in, and this index answers "what is on".
#
# Denied by the library's OWN tags rather than by title, because the tags are
# ground truth and titles vary ("Storyplay", "Story Play"). Every tag here was
# measured first: the number is how many of that tag's events fall in the
# bucket, and only tags at 88% or better are listed. `adult learning` (66%) and
# `techknowledge` (61%) are deliberately absent -- broad enough that denying
# them would take real programmes with them.
LIBRARY_REJECT_TAGS = {
    # standing services -- a counter, not a programme
    "summer meals": "meal service, not a programme",              # 195/196
    "free summer meals": "meal service, not a programme",         # 195/198
    "grab and go kits": "take-home kit, not a programme",         # 43/44
    "book sale": "ongoing shop, not a programme",                 # 22/22
    "Cycle Alliance": "supply pantry, not a programme",           # 88/95
    "legal resources": "advice service, not a programme",         # 24/25
    "Immigrant Services": "advice service, not a programme",      # 262/295
    # recurring adult instruction -- a course, not a listing
    "ESOL": "recurring language course",                          # 168/171
    "Welcome ESOL": "recurring language course",                  # 41/41
    "WeSpeakNYC": "recurring language course",                    # 123/123
    "english conversation group": "recurring language course",    # 87/93
    "citizenship": "recurring civics course",                     # 33/33
    "computer basics": "recurring computer course",               # 136/154
    "resume": "job-help service, not a programme",                # 88/105
}

# The services that carry no distinguishing tag. Deliberately narrow and
# anchored: these are named things, not a keyword sweep.
LIBRARY_REJECT_TITLE = _re.compile(
    r"\bnotary\b|\bask a tech\b|\bneighborhood tech help\b|\bfood pantry\b|"
    r"\bperiod pantry\b|\bblood drive\b|\bpassport (day|appointment|service)|"
    r"\btax (help|prep|assistance)\b|\bsnap (enrol|benefit|application)|"
    r"\bhealth insurance\b|\bgrab (and|&) go\b|\bbook sale\b", _re.I)


def rejected_library(title, tags):
    """A library record that is a service or a course, not an event to attend."""
    for tag in tags or []:
        reason = LIBRARY_REJECT_TAGS.get(str(tag).strip())
        if reason:
            return reason
    if LIBRARY_REJECT_TITLE.search(title or ""):
        return "standing service, not a programme"
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
    # The good half of "Special Event". Now that it no longer defaults to
    # street-fair, these are what let a real permit keep its place.
    (("greenmarket", "farm stand", "food market"), "community", "market"),
    (("circus", "performance", "performances"), "performance", "theatre"),
    (("fitness", "workout", "bootcamp", "zumba"), "sports", "fitness"),
    (("rowing", "kayak", "canoe", "sail"), "outdoors", "waterfront"),
    (("skate", "skateboard", "roller"), "sports", "sports-general"),
    (("health", "wellness", "vaccination", "screening clinic"), "community", "health"),
    # Added from the Squarespace venue calendars, where museum and gallery
    # programming was falling through entirely -- 53 of 75 events were rejected
    # as unclassified before these existed.
    (("opening reception", "opening of", "vernissage", "exhibition", "exhibit",
      "installation", "art auction"), "art", "exhibition"),
    (("crafting", "knit", "quilt", "carving", "ceramics", "printmaking",
      "collage", "zine"), "art", "arts-crafts"),
    (("shanty", "singalong", "sing-along", "recital", "chorus"), "music", "concert"),
    (("tasting", "whiskey", "wine", "beer", "cocktail", "bar crawl",
      "dinner", "brunch"), "food-drink", "tasting"),
    (("commemoration", "anniversary", "memorial", "history", "historian",
      "archive"), "learning", "history"),
    (("residency", "seminar", "symposium", "class", "course"), "learning", "class"),
    (("storytelling", "storytime", "story time"), "performance", "storytelling"),
    # Added from the WordPress venue calendars, where 189 of 861 events were
    # rejected as unclassified. Park and nature programming had no vocabulary
    # here at all, which is odd for a city whose largest event publisher is its
    # parks department -- these are generic terms, not one venue's jargon.
    (("nature", "audubon", "wildlife", "birding", "bird walk", "foraging",
      "stargazing"), "outdoors", "nature"),
    (("garden", "planting", "compost"), "outdoors", "gardening"),
    (("performing arts", "cabaret", "vaudeville", "burlesque",
      "improv"), "performance", "theatre"),
    (("smorgasburg", "night market", "food truck"), "food-drink", "market"),
    (("open mic",), "music", "open-mic"),
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


# ---------------------------------------------------------------------------
# Ground truth: Ticketmaster `classifications`, a segment/genre/subGenre triple.
# Genre is checked before segment because it is the specific one -- a segment of
# "Music" with a genre of "Jazz" should land on jazz, not the generic concert.
# ---------------------------------------------------------------------------
TM_GENRE = {
    "jazz":                ("music", "jazz"),
    "classical":           ("music", "classical"),
    "opera":               ("music", "classical"),
    "electronic":          ("music", "dj-set"),
    "dance/electronic":    ("music", "dj-set"),
    "theatre":             ("performance", "theatre"),
    "theater":             ("performance", "theatre"),
    "comedy":              ("performance", "comedy"),
    "dance":               ("performance", "dance"),
    "performance art":     ("performance", "theatre"),
    "circus & specialty acts": ("performance", "theatre"),
    "magic & illusion":    ("performance", "theatre"),
    "children's theatre":  ("performance", "theatre"),
    "fine art":            ("art", "exhibition"),
    "basketball":          ("sports", "basketball"),
    "tennis":              ("sports", "sports-general"),
    "boxing":              ("sports", "martial-arts"),
    "wrestling":           ("sports", "martial-arts"),
    "mixed martial arts":  ("sports", "martial-arts"),
    "cycling":             ("sports", "cycling"),
    "swimming":            ("sports", "swimming"),
    "running":             ("sports", "running"),
}

TM_SEGMENT = {
    "music":          ("music", "concert"),
    "sports":         ("sports", "sports-general"),
    "arts & theatre": ("performance", "theatre"),
    "film":           ("art", "film-screening"),
}


# A venue can classify an event its own listing refuses to. Ticketmaster files
# all of Broadway and Off-Broadway under the segment `Undefined` with no genre,
# so 627 shows at the Nederlander, the Lunt-Fontanne, Cherry Lane and New World
# Stages arrive with no category at all. The building is the evidence: a show at
# a theatre is theatre. This is inference from ground truth, not from a guess
# about the title.
VENUE_KIND = [
    (("theatre", "theater", "playhouse", "stage", "opera house"),
     ("performance", "theatre")),
    (("museum", "gallery"), ("art", "exhibition")),
    (("comedy cellar", "comedy club"), ("performance", "comedy")),
    (("stadium", "arena", "ballpark", "field house"), ("sports", "sports-general")),
]


def from_venue(venue_name):
    """Last-resort classification from the venue's own name. Returns (cat, sub, source)."""
    v = (venue_name or "").lower()
    for words, (cat, sub) in VENUE_KIND:
        if any(w in v for w in words):
            return cat, sub, "venue-name"
    return None, None, None


def from_ticketmaster(classifications):
    """Map a Ticketmaster classifications array. Returns (cat, sub, source).

    `Miscellaneous` and `Undefined` are deliberately not mapped here.
    `Miscellaneous` is Ticketmaster's bucket for everything from fan conventions
    to parking passes, and `Undefined` is where Broadway lives. Forcing either
    into a category from the segment alone would put noise on the page under a
    confident-looking tag, so both fall through to the venue and then the title.
    """
    for c in classifications or []:
        if not isinstance(c, dict):
            continue
        for key in ("subGenre", "genre"):
            name = ((c.get(key) or {}).get("name") or "").strip().lower()
            hit = TM_GENRE.get(name)
            if hit and valid(hit[0], hit[1]):
                return hit[0], hit[1], f"ticketmaster:{key}"
        seg = ((c.get("segment") or {}).get("name") or "").strip().lower()
        hit = TM_SEGMENT.get(seg)
        if hit and valid(hit[0], hit[1]):
            return hit[0], hit[1], "ticketmaster:segment"
    return None, None, None


# schema.org event subtypes -> our vocabulary. A page that marks itself up as a
# ScreeningEvent has stated what it is, and that outranks reading the title.
#
# MusicEvent is separated out because it is the weak signal -- a music
# ticketer's default rather than a statement -- and callers may want to rank it
# differently from the specific types. On DICE it covers 91% of listings.
SCHEMA_EVENT = {
    "screeningevent": ("art", "film-screening"),
    "exhibitionevent": ("art", "exhibition"),
    "comedyevent": ("performance", "comedy"),
    "theaterevent": ("performance", "theatre"),
    "danceevent": ("performance", "dance"),
    "sportsevent": ("sports", "sports-general"),
    "educationevent": ("learning", "talk"),
    "literaryevent": ("learning", "talk"),
    "foodevent": ("food-drink", "tasting"),
}

# The weak type, worth something only where the SOURCE is specific. On a music
# ticketer the base rate really is music; on a general listings feed this would
# be worthless and callers should not reach for it.
SCHEMA_DEFAULT = {"musicevent": ("music", "concert")}


def from_schema(type_):
    """A specific schema.org event subtype -> (cat, sub, source), or None.

    Generic types get nothing back; ask `from_schema_default` for those.
    """
    t = (type_ or "").strip().lower()
    hit = SCHEMA_EVENT.get(t)
    if hit and valid(hit[0], hit[1]):
        return hit[0], hit[1], f"schema:{t}"
    return None, None, None


def from_schema_default(type_):
    """A generic schema.org event type -> (cat, sub, source), or None."""
    t = (type_ or "").strip().lower()
    hit = SCHEMA_DEFAULT.get(t)
    if hit and valid(hit[0], hit[1]):
        return hit[0], hit[1], f"schema-default:{t}"
    return None, None, None


def from_title(title):
    """Fallback only. Returns (cat, sub, source)."""
    t = (title or "").lower()
    for words, cat, sub in TITLE_KEYWORDS:
        if any(w in t for w in words):
            return cat, sub, "title"
    return None, None, None
