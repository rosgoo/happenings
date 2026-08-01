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

export const PLACE_KINDS = {
  museum: 'Museum', gallery: 'Gallery', aquarium: 'Aquarium', zoo: 'Zoo',
  theatre: 'Theatre', cinema: 'Cinema', 'arts-centre': 'Arts centre',
  nightclub: 'Nightclub', 'music-venue': 'Music venue',
  'escape-room': 'Escape room', arcade: 'Arcade', bowling: 'Bowling',
  'ice-rink': 'Ice rink', climbing: 'Climbing',
};

// Places reuse the event category palette rather than inventing a second one --
// eight colours is already the whole system.
const PLACE_COLOR = {
  museum: 'art', gallery: 'art', aquarium: 'outdoors', zoo: 'outdoors',
  theatre: 'performance', cinema: 'art', 'arts-centre': 'art',
  nightclub: 'music', 'music-venue': 'music', 'escape-room': 'learning',
  arcade: 'community', bowling: 'sports', 'ice-rink': 'sports',
  climbing: 'sports',
};
export const placeColor = (kind) => `var(--c-${PLACE_COLOR[kind] || 'community'})`;

export function placesIn(hoodSlug, limit = 8) {
  return places.filter((p) => p.neighborhood === hoodSlug)
    .sort((a, b) => (b.image_url ? 1 : 0) - (a.image_url ? 1 : 0))
    .slice(0, limit);
}

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
export const slim = (list) => list.map((e) => ({
  i: e.id, t: e.title, s: e.start_local, d: e.date_local, b: e.time_band,
  v: e.venue_id, vn: e.venue_name, h: e.neighborhood, hn: e.neighborhood_name,
  bo: e.borough, c: e.category, sc: e.subcategory, u: e.url,
}));
