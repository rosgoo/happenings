"""Point-in-polygon against a city's neighbourhood polygons.

Neighbourhood is a DERIVED field, resolved once at build time and stored on the
record -- never computed in the browser. A venue gets exactly one canonical
neighbourhood, because URLs, dedup and canonical tags all depend on it.

Stdlib only: ray casting with a bbox pre-filter. At NYC's ~260 NTAs and a few
thousand events this is milliseconds, and it avoids a shapely dependency in a
pipeline that is meant to run anywhere.
"""


def _rings(geom):
    """Normalise Polygon / MultiPolygon into a list of (outer, [holes])."""
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon":
        polys = [coords]
    elif t == "MultiPolygon":
        polys = coords
    else:
        return []
    out = []
    for p in polys:
        if p:
            out.append((p[0], p[1:]))
    return out


def _bbox(rings):
    xs, ys = [], []
    for outer, _ in rings:
        for x, y in outer:
            xs.append(x)
            ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)


def _in_ring(x, y, ring):
    """Standard ray casting. Points exactly on an edge are undefined-but-stable,
    which is fine: no NYC venue sits on an NTA boundary to six decimals."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-16) + xi:
                inside = not inside
        j = i
    return inside


class Neighborhoods:
    """Loads a GeoJSON FeatureCollection and answers 'which one contains this?'"""

    def __init__(self, geojson):
        self.areas = []
        for f in geojson.get("features", []):
            props = f.get("properties", {})
            rings = _rings(f.get("geometry") or {})
            if not rings:
                continue
            self.areas.append(
                {
                    "name": props.get("name"),
                    "slug": props.get("slug"),
                    "borough": props.get("borough"),
                    "nta_type": props.get("nta_type"),
                    "rings": rings,
                    "bbox": _bbox(rings),
                }
            )

    def __len__(self):
        return len(self.areas)

    def locate(self, lon, lat):
        """Return the containing area dict, or None. None is a real answer --
        a point outside every polygon (the harbour, a mismapped coordinate) is
        recorded as unknown rather than snapped to the nearest guess."""
        for a in self.areas:
            x0, y0, x1, y1 = a["bbox"]
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            for outer, holes in a["rings"]:
                if _in_ring(lon, lat, outer) and not any(_in_ring(lon, lat, h) for h in holes):
                    return a
        return None
