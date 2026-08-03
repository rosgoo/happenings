// Reads the pipeline's output at build time. Flat files in, static HTML out --
// there is no runtime database and nothing here runs in a browser.
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(process.cwd());
export const CITY = 'nyc';

const read = (name) =>
  JSON.parse(fs.readFileSync(path.join(ROOT, 'data', CITY, `${name}.json`), 'utf8'));

export const events = read('events');
export const venues = read('venues');
export const meta = read('meta');

// Places are a different entity from events: they are OPEN rather than ON.
// Optional, because the OSM stage may not have run.
export const places = (() => {
  try { return read('places'); } catch { return []; }
})();

// Mirrors the vocabulary in places.py. The second half is the escape-room
// tier: places you go DO something at, which is what "Bushwick, Saturday
// night" is asking for and what no events feed lists.
export const PLACE_KINDS = {
  museum: 'Museum', gallery: 'Gallery', aquarium: 'Aquarium', zoo: 'Zoo',
  theatre: 'Theatre', cinema: 'Cinema', 'arts-centre': 'Arts centre',
  nightclub: 'Nightclub', 'music-venue': 'Music venue',
  'comedy-club': 'Comedy club', dance: 'Dancing', karaoke: 'Karaoke',
  'escape-room': 'Escape room', arcade: 'Arcade', bowling: 'Bowling',
  'ice-rink': 'Ice rink', 'roller-rink': 'Roller rink', climbing: 'Climbing',
  obstacle: 'Ninja & obstacle', trampoline: 'Trampoline park',
  'mini-golf': 'Mini golf', 'laser-tag': 'Laser tag',
  'axe-throwing': 'Axe throwing', karting: 'Go-karting',
  'pool-hall': 'Pool hall', surfing: 'Surfing',
  'theme-park': 'Theme park', 'water-park': 'Water park', casino: 'Casino',
  makerspace: 'Maker space',
};

// Places reuse the event category palette rather than inventing a second one --
// eight colours is already the whole system.
const PLACE_COLOR = {
  museum: 'art', gallery: 'art', aquarium: 'outdoors', zoo: 'outdoors',
  theatre: 'performance', cinema: 'art', 'arts-centre': 'art',
  nightclub: 'music', 'music-venue': 'music', 'escape-room': 'learning',
  arcade: 'community', bowling: 'sports', 'ice-rink': 'sports',
  climbing: 'sports',
  'comedy-club': 'performance', dance: 'music', karaoke: 'music',
  'roller-rink': 'sports', obstacle: 'sports', trampoline: 'sports',
  'mini-golf': 'sports', 'laser-tag': 'sports', 'axe-throwing': 'sports',
  karting: 'sports', 'pool-hall': 'community', surfing: 'outdoors',
  'theme-park': 'outdoors', 'water-park': 'outdoors',
  casino: 'community', makerspace: 'learning',
};
export const placeColor = (kind) => `var(--c-${PLACE_COLOR[kind] || 'community'})`;
// A place's kind maps onto the event category vocabulary, which is what lets
// one "Art" filter mean both "art events" and "galleries". Exported so the
// island filters places by the same chips the listings use.
export const placeCategory = (kind) => PLACE_COLOR[kind] || 'community';

// The client payload for places. Same bargain as `slim`: short keys, nearest
// station only, and no image credit block -- the section renders names and
// kinds, and the photo wall lives on /nyc/places.
export const slimPlaces = (list) => list.map((p) => ({
  i: p.id, t: p.name, k: p.kind, kl: p.kind_label,
  h: p.neighborhood, hn: p.neighborhood_name, bo: p.borough,
  c: placeCategory(p.kind), u: p.website, im: p.image_url || null,
  la: p.lat, lo: p.lon, oh: p.opening_hours || null,
  // Seven hex digits, Monday first, four band bits each -- see lib/hours.py.
  // Null is UNKNOWN, and the island renders it as unknown rather than
  // assuming either way. `nb` orders the twelve that fit on screen.
  hm: p.hours_mask || null, nb: p.notability || 0,
  lb: p.listed_by || 'osm',
  wp: p.wikipedia || null,
  cr: p.image_credit || null, cp: p.image_page || null,
  sw: p.subway && p.subway[0]
    ? { i: p.subway[0].id, n: p.subway[0].name, r: p.subway[0].routes,
        m: p.subway[0].meters }
    : null,
}));

export function placesIn(hoodSlug, limit = 8) {
  return places.filter((p) => p.neighborhood === hoodSlug)
    .sort((a, b) => (b.image_url ? 1 : 0) - (a.image_url ? 1 : 0))
    .slice(0, limit);
}

// The system's own shape: every line, its stops in the order you ride them,
// and every station whether or not anything is on there tonight. Optional so a
// build from a pipeline that predates the line index still renders.
export const subway = (() => {
  try { return read('subway'); } catch { return { lines: [], stations: {} }; }
})();

// The client payload for the line index. Two-element arrays rather than named
// keys: this is 445 records of two fields each, and the field names would cost
// more than the data.
export const slimLines = () => ({
  l: subway.lines.map((ln) => ({ r: ln.route, n: ln.name, s: ln.stations })),
  s: Object.fromEntries(Object.entries(subway.stations)
    .map(([id, s]) => [id, [s.name, s.routes]])),
});

export const CATEGORY_LABELS = {
  music: 'Music', sports: 'Sports', 'food-drink': 'Food & Drink', art: 'Art',
  performance: 'Performance', learning: 'Learning', community: 'Community',
  outdoors: 'Outdoors',
};

export const catColor = (c) => `var(--c-${c})`;
// Six categories take ink, two take paper. Stored, so no component ever guesses.
export const catOn = (c) =>
  ['performance', 'learning'].includes(c) ? 'var(--paper)' : 'var(--ink)';

// "Now" is fixed once per build so every page on the site agrees about today.
export const NOW = new Date();
const todayStr = new Date(NOW.getTime() - NOW.getTimezoneOffset() * 60000)
  .toISOString().slice(0, 10);
export const TODAY = todayStr;

export const upcoming = events.filter((e) => e.date_local >= TODAY);

export function fmtTime(startLocal) {
  const t = startLocal.slice(11);
  if (!t || t === '00:00') return '';
  const [h, m] = t.split(':').map(Number);
  const ap = h >= 12 ? 'pm' : 'am';
  const hh = h % 12 === 0 ? 12 : h % 12;
  return m === 0 ? `${hh}${ap}` : `${hh}:${String(m).padStart(2, '0')}${ap}`;
}

export function fmtDay(dateStr) {
  const d = new Date(dateStr + 'T12:00:00');
  return d.toLocaleDateString('en-US', {
    weekday: 'long', month: 'long', day: 'numeric',
  });
}

// Neighbourhoods that actually have something on. An edition with nothing in it
// is worse than no edition -- the same instinct as the doorway-page guard.
export function neighborhoods(min = 1) {
  const byHood = new Map();
  for (const e of upcoming) {
    if (!e.neighborhood) continue;
    const cur = byHood.get(e.neighborhood) || {
      slug: e.neighborhood, name: e.neighborhood_name,
      borough: e.borough, count: 0,
    };
    cur.count++;
    byHood.set(e.neighborhood, cur);
  }
  return [...byHood.values()].filter((h) => h.count >= min)
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

export function categories() {
  const c = new Map();
  for (const e of upcoming) c.set(e.category, (c.get(e.category) || 0) + 1);
  return [...c.entries()].map(([slug, count]) => ({
    slug, count, name: CATEGORY_LABELS[slug] || slug,
  })).sort((a, b) => b.count - a.count);
}

export function venuesWithEvents(min = 1) {
  const counts = new Map();
  for (const e of upcoming) counts.set(e.venue_id, (counts.get(e.venue_id) || 0) + 1);
  return venues
    .map((v) => ({ ...v, upcoming: counts.get(v.id) || 0 }))
    .filter((v) => v.upcoming >= min)
    .sort((a, b) => b.upcoming - a.upcoming || a.name.localeCompare(b.name));
}

// The client payload. Short keys because this ships to every visitor: at ~6k
// events the difference between this and the full record is about a megabyte.
// Source ids are long and repeat on every record. One-letter codes, expanded
// to their attribution client-side.
export const SOURCE_CODE = {
  'nyc-ticketmaster': 'tm', 'nyc-permitted-events': 'pe',
  'nyc-parks-upcoming': 'pk', 'nyc-squarespace': 'sq', 'nyc-luma': 'lu',
};

export const slim = (list) => list.map((e) => ({
  i: e.id, t: e.title, s: e.start_local, d: e.date_local, b: e.time_band,
  v: e.venue_id, vn: e.venue_name, h: e.neighborhood, hn: e.neighborhood_name,
  bo: e.borough, c: e.category, sc: e.subcategory, u: e.url,
  // Detail-panel fields. Only `de` is expensive -- 14% of events carry a
  // description averaging 327 characters -- and it is the one thing that
  // answers "what actually is this", so it earns the ~200 KB.
  e: e.end_local || null,
  de: e.description || null,
  la: e.lat, lo: e.lon,
  src: SOURCE_CODE[e.source] || e.source,
  // Nearest station only. The id travels with it because station NAMES are not
  // unique -- there are several "86 St" and three "23 St" complexes on
  // different lines, so filtering by name would silently merge stations that
  // are miles apart.
  sw: e.subway && e.subway[0]
    ? { i: e.subway[0].id, n: e.subway[0].name, r: e.subway[0].routes,
        m: e.subway[0].meters }
    : null,
}));

// Neighbourhood records carry the trains that run through them. Optional so a
// build from an older pipeline still renders.
export const hoodInfo = (() => {
  try {
    const list = read('neighborhoods');
    return new Map(list.map((h) => [h.slug, h]));
  } catch { return new Map(); }
})();

// The MTA's own route colours, so a bullet reads as the train it names rather
// than as decoration. Grouped by trunk line exactly as the subway map is.
export const ROUTE_COLOR = {
  1: '#EE352E', 2: '#EE352E', 3: '#EE352E',
  4: '#00933C', 5: '#00933C', 6: '#00933C',
  7: '#B933AD',
  A: '#0039A6', C: '#0039A6', E: '#0039A6',
  B: '#FF6319', D: '#FF6319', F: '#FF6319', M: '#FF6319',
  G: '#6CBE45',
  J: '#996633', Z: '#996633',
  L: '#A7A9AC',
  N: '#FCCC0A', Q: '#FCCC0A', R: '#FCCC0A', W: '#FCCC0A',
  S: '#808183', SIR: '#0039A6',
};
// The yellow and grey bullets need dark type; every other trunk takes white.
export const routeOn = (r) =>
  ['N', 'Q', 'R', 'W', 'L', 'S'].includes(r) ? '#000' : '#fff';

// Trunk order, as the map draws it and as the filter rail lists it -- numbered
// lines, then each lettered trunk in colour order. Alphabetical would split the
// 8 Avenue trains across the list and put the L between the J and the M.
export const ROUTE_ORDER = ['1', '2', '3', '4', '5', '6', '7', 'A', 'C', 'E',
  'B', 'D', 'F', 'M', 'G', 'J', 'Z', 'L', 'N', 'Q', 'R', 'W', 'S', 'SIR'];
