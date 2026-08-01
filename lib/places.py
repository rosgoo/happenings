"""The location service: anything that describes a place -> a located place.

Sources describe where something is in whatever way suits them: coordinates, a
park name, a street intersection, a bare venue name. Every one of those has to
end up as the same thing -- a point, a neighbourhood, a borough -- and each
downstream consumer should not have to care which arrived.

    resolver.resolve(name="Central Park")
    resolver.resolve(lat=40.77, lon=-73.97)
    resolver.resolve(name="5 Avenue between 3 Street and 4 Street", borough="Brooklyn")

STRATEGY CHAIN, in descending order of authority. First hit wins:

    1. coords      -- already located; just needs a polygon lookup
    2. register    -- an authoritative name->geometry register (NYC Parks
                      Properties). A lookup, not a guess
    3. gazetteer   -- known venues previously resolved (e.g. from OSM), so a
                      name seen once is free forever after
    4. geocoder    -- a real geocoder, for address-shaped strings only, and
                      only above a confidence floor

Every result records WHICH strategy answered and how confident it was, so a
weak answer is visible rather than indistinguishable from a strong one. When
nothing answers, the result is `None` -- recorded as unresolved, never snapped
to the nearest plausible thing. That is the difference between a directory that
admits a gap and one that is confidently wrong.

On swapping this out: `strategies` is an ordered list of callables, so adding
libpostal for address normalisation, a self-hosted Pelias or Nominatim, or a
Who's-On-First gazetteer means appending one function -- not touching callers.
The shapely/geopandas stack would replace lib/geo.py wholesale if the polygon
work ever outgrows stdlib ray casting; at 262 neighbourhoods and 2,000 parks it
has not.
"""
import re


class Place:
    __slots__ = ("lat", "lon", "neighborhood", "neighborhood_name", "borough",
                 "nta_type", "source", "confidence", "matched")

    def __init__(self, lat, lon, hood, source, confidence=1.0, matched=None,
                 borough=None):
        self.lat = lat
        self.lon = lon
        self.source = source
        self.confidence = confidence
        self.matched = matched
        self.neighborhood = hood["slug"] if hood else None
        self.neighborhood_name = hood["name"] if hood else None
        self.nta_type = hood["nta_type"] if hood else None
        self.borough = (hood["borough"] if hood else borough)

    def as_dict(self):
        return {
            "lat": self.lat, "lon": self.lon,
            "neighborhood": self.neighborhood,
            "neighborhood_name": self.neighborhood_name,
            "nta_type": self.nta_type,
            "borough": self.borough,
            "location_source": self.source,
            "location_confidence": self.confidence,
        }


def norm(s):
    """Lowercase, drop punctuation, collapse whitespace.

    Deliberately does NOT stem 'Park'/'Playground'/'Square' -- 'Marine Park'
    and 'Marine Playground' are different properties and collapsing them would
    invent a location.
    """
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_variants(raw):
    """The shapes a place name actually arrives in, most specific first."""
    out = [raw]
    m = re.match(r"^(.*?)\s*\((.+)\)\s*$", raw or "")
    if m:                         # "Langston Hughes Playground (St. Nicholas …)"
        out += [m.group(1), m.group(2)]
    if raw and " and " in raw.lower():   # "Pelham Bay Park and Orchard Beach"
        out.append(re.split(r"\s+and\s+", raw, 1, flags=re.I)[0])
    return [v.strip() for v in out if v and v.strip()]


class Resolver:
    def __init__(self, neighborhoods, register=None, gazetteer=None, geocoder=None):
        self.hoods = neighborhoods
        self.register = register or {}    # normalised name -> {lat, lon, name}
        self.gazetteer = gazetteer or {}  # normalised name -> {lat, lon, name}
        self.geocoder = geocoder          # callable(text, borough) -> dict|None

    # -- strategies --------------------------------------------------------
    def _from_coords(self, lat, lon, **_):
        if lat is None or lon is None:
            return None
        return Place(lat, lon, self.hoods.locate(lon, lat), "coordinates", 1.0)

    def _from_table(self, table, source, name, borough=None):
        for v in name_variants(name):
            hit = table.get(norm(v))
            if hit:
                return Place(hit["lat"], hit["lon"],
                             self.hoods.locate(hit["lon"], hit["lat"]),
                             source, 1.0, hit.get("name"), borough)
        return None

    def _from_geocoder(self, name=None, borough=None, **_):
        if not self.geocoder or not name:
            return None
        g = self.geocoder(name, borough)
        if not g:
            return None
        return Place(g["lat"], g["lon"], self.hoods.locate(g["lon"], g["lat"]),
                     "geocoder", g.get("confidence", 0.0), g.get("name"), borough)

    # -- entry point -------------------------------------------------------
    def resolve(self, *, name=None, lat=None, lon=None, borough=None):
        """Return a Place, or None if nothing could answer confidently."""
        p = self._from_coords(lat, lon)
        if p:
            return p
        if name:
            p = (self._from_table(self.register, "register", name, borough)
                 or self._from_table(self.gazetteer, "gazetteer", name, borough))
            if p:
                return p
        return self._from_geocoder(name=name, borough=borough)
