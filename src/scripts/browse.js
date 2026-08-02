/* The browse island.
   One JSON, filtered in the browser. At a few thousand events this is instant
   and needs no server; shard by month when it stops being. All filter state
   lives in the URL, so every view is a shareable link. */

import { openEvent, openPlace } from './detail.js';

const CATS = {
  music: 'Music', sports: 'Sports', 'food-drink': 'Food & Drink', art: 'Art',
  performance: 'Performance', learning: 'Learning', community: 'Community',
  outdoors: 'Outdoors',
};
const PAPER_ON = ['performance', 'learning'];
const PAGE = 250;

// The MTA's own trunk colours. Mirrored from lib/data.js so the island and the
// server-rendered half agree; eight trunks is small enough that a shared build
// artifact would cost more than it saves.
const ROUTE_COLOR = {
  1: '#EE352E', 2: '#EE352E', 3: '#EE352E',
  4: '#00933C', 5: '#00933C', 6: '#00933C',
  7: '#B933AD',
  A: '#0039A6', C: '#0039A6', E: '#0039A6',
  B: '#FF6319', D: '#FF6319', F: '#FF6319', M: '#FF6319',
  G: '#6CBE45', J: '#996633', Z: '#996633', L: '#A7A9AC',
  N: '#FCCC0A', Q: '#FCCC0A', R: '#FCCC0A', W: '#FCCC0A',
  S: '#808183', SIR: '#0039A6',
};
const ROUTE_ORDER = ['1', '2', '3', '4', '5', '6', '7', 'A', 'C', 'E', 'B', 'D',
  'F', 'M', 'G', 'J', 'Z', 'L', 'N', 'Q', 'R', 'W', 'S', 'SIR'];
const routeOn = (r) => (['N', 'Q', 'R', 'W', 'L', 'S'].includes(r) ? '#000' : '#fff');

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const on = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// The heading's date belongs to the reader, not to whenever the site was last
// built. Runs before anything else and outside the island, so it is correct
// even on a page that has no listings on it.
const todayEl = $('#todaydate');
if (todayEl) {
  todayEl.textContent = new Date().toLocaleDateString('en-US',
    { weekday: 'long', month: 'long', day: 'numeric' });
}

const root = $('#browse');
if (root) {
  const scope = JSON.parse(root.dataset.scope || '{}');
  const state = {
    day: null, days: 7, bands: new Set(), cats: new Set(),
    hoods: new Set(), routes: new Set(), venues: new Set(),
    stations: new Set(), q: '', view: 'list', limit: PAGE,
  };
  let ALL = [];
  // The line index, and every station on it. Seeded from the events so the rail
  // works before the index lands, then replaced by the real thing -- the events
  // know the stations they are near, just not what order they come in.
  let LINES = [];
  let STOPS = {};
  let PLACES = [];
  let placeLimit = 12;

  // Places answer WHAT, WHERE and WHICH TRAIN. They cannot answer WHEN -- a
  // gallery has no start time -- so day, time band and venue are not applied.
  // Silently ignoring three filters would make the section look stuck, so the
  // note under the heading names the ones that are doing the work.
  const PLACE_FILTERS = [
    ['cats', 'category'], ['hoods', 'neighborhood'],
    ['routes', 'line'], ['stations', 'station'],
  ];
  function placeMatches(p) {
    if (state.cats.size && !state.cats.has(p.c)) return false;
    if (state.hoods.size && !(p.h && state.hoods.has(p.h))) return false;
    if (state.routes.size &&
        !(p.sw && p.sw.r.some((r) => state.routes.has(r)))) return false;
    if (state.stations.size && !(p.sw && state.stations.has(p.sw.i))) return false;
    if (state.q) {
      const q = state.q.toLowerCase();
      if (![p.t, p.kl, p.hn, p.bo, p.sw && p.sw.n]
        .some((s) => (s || '').toLowerCase().includes(q))) return false;
    }
    return true;
  }

  function placeCardHTML(p) {
    const title = p.u
      ? `<a href="${esc(p.u)}" rel="noopener">${esc(p.t)}</a>` : esc(p.t);
    const where = p.hn
      ? `<a href="/nyc/${esc(p.h)}">${esc(p.hn)}</a>` : esc(p.bo || '');
    return `<div class="pcard openable" data-id="${esc(p.i)}" tabindex="0"
      role="button" aria-label="Details for ${esc(p.t)}">
      <div class="ph" style="background:var(--c-${p.c})">
        ${p.im ? `<img src="${esc(p.im)}" alt="${esc(p.t)}" loading="lazy"
             referrerpolicy="no-referrer" decoding="async" />` : ''}
        <span class="kindtag">${esc(p.kl || p.k)}</span>
        ${p.im ? '' : `<span class="fb">${esc(p.kl || p.k)}</span>`}
      </div>
      <div class="pb">
        <h3>${title}</h3>
        <div class="w">${where}${p.sw ? ' · ' + stopHTML(p.sw) : ''}</div>
      </div></div>`;
  }

  function renderPlaces() {
    const sec = $('#placesec');
    if (!sec || !PLACES.length) return;
    const hits = PLACES.filter(placeMatches);
    const active = PLACE_FILTERS.filter(([k]) => state[k].size).map(([, n]) => n);
    const note = $('#placenote');

    if (!hits.length) {
      // A section that vanishes is better than one showing places that
      // contradict the filters -- but say why, or it reads as a bug.
      sec.classList.remove('hide');
      $('#places').innerHTML =
        `<div class="empty">No places match ${active.length
          ? 'this ' + active.join(' and ') : 'these filters'}.</div>`;
      if (note) note.textContent = '';
      $('#moreplaces').classList.add('hide');
      return;
    }

    sec.classList.remove('hide');
    if (note) {
      note.textContent = active.length
        ? `${hits.length.toLocaleString()} open near you by ${active.join(' and ')} — not filtered by date or time.`
        : `${hits.length.toLocaleString()} museums, galleries, cinemas and more.`;
    }
    $('#places').innerHTML = hits.slice(0, placeLimit).map(placeCardHTML).join('');
    const more = $('#moreplaces');
    more.classList.toggle('hide', hits.length <= placeLimit);
    if (hits.length > placeLimit) {
      more.textContent = `Show ${Math.min(12, hits.length - placeLimit)} more of ${hits.length.toLocaleString()}`;
    }
  }

  const bullet = (r) => `<span class="bullet${r === 'SIR' ? ' sir' : ''}" ` +
    `style="background:${ROUTE_COLOR[r] || '#555'};color:${routeOn(r)}">${esc(r)}</span>`;
  const stopHTML = (sw) => (sw
    ? `<span class="stop">${sw.r.map(bullet).join('')} ${esc(sw.n)}</span>` : '');

  const todayStr = () => {
    const d = new Date();
    return new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
  };
  const addDays = (s, n) => {
    const d = new Date(s + 'T12:00:00');
    d.setDate(d.getDate() + n);
    return new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
  };
  const fmtTime = (s) => {
    const t = s.slice(11);
    if (!t || t === '00:00') return '';
    let [h, m] = t.split(':').map(Number);
    const ap = h >= 12 ? 'pm' : 'am';
    h = h % 12 === 0 ? 12 : h % 12;
    return m === 0 ? `${h}${ap}` : `${h}:${String(m).padStart(2, '0')}${ap}`;
  };
  const fmtDay = (s) => new Date(s + 'T12:00:00').toLocaleDateString('en-US',
    { weekday: 'long', month: 'long', day: 'numeric' });

  // ---- URL <-> state ----
  function readURL() {
    const p = new URLSearchParams(location.search);
    // Today is the default view: the question this site answers is "what is on
    // tonight", and opening on a seven-day wall of 2,000 rows answers a
    // question nobody asked. `day=all` is how a widened range survives a
    // reload -- an absent parameter means "not chosen yet", not "cleared".
    const day = p.get('day');
    state.day = day === 'all' ? null : (day || todayStr());
    state.days = Number(p.get('days') || 7);
    state.q = p.get('q') || '';
    state.view = p.get('view') === 'grid' ? 'grid' : 'list';
    for (const [key, set] of [['band', state.bands], ['cat', state.cats],
      ['hood', state.hoods], ['route', state.routes], ['venue', state.venues],
      ['stop', state.stations]]) {
      set.clear();
      (p.get(key) || '').split(',').filter(Boolean).forEach((v) => set.add(v));
    }
  }
  function writeURL() {
    const p = new URLSearchParams();
    if (state.day) {
      if (state.day !== todayStr()) p.set('day', state.day);
    } else {
      p.set('day', 'all');
    }
    if (state.days !== 7) p.set('days', state.days);
    if (state.q) p.set('q', state.q);
    if (state.view !== 'list') p.set('view', state.view);
    if (state.bands.size) p.set('band', [...state.bands].join(','));
    if (state.cats.size) p.set('cat', [...state.cats].join(','));
    if (state.hoods.size) p.set('hood', [...state.hoods].join(','));
    if (state.routes.size) p.set('route', [...state.routes].join(','));
    if (state.venues.size) p.set('venue', [...state.venues].join(','));
    if (state.stations.size) p.set('stop', [...state.stations].join(','));
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  }

  // ---- filtering ----
  function matches(e) {
    const from = state.day || todayStr();
    const to = state.day ? state.day : addDays(from, state.days - 1);
    if (e.d < from || e.d > to) return false;
    if (state.bands.size && !state.bands.has(e.b)) return false;
    if (state.cats.size && !state.cats.has(e.c)) return false;
    if (state.hoods.size && !(e.h && state.hoods.has(e.h))) return false;
    // Line and station are ANDed with each other only in the sense that both
    // must pass; picking the L and then Bedford Av is the same answer as
    // Bedford Av alone, which is what someone doing it expects.
    if (state.routes.size &&
        !(e.sw && e.sw.r.some((r) => state.routes.has(r)))) return false;
    if (state.stations.size && !(e.sw && state.stations.has(e.sw.i))) return false;
    if (state.venues.size && !state.venues.has(e.v)) return false;
    if (state.q) {
      const q = state.q.toLowerCase();
      // Neighbourhood and station are searchable too. "Bushwick" and "Bedford
      // Av" are how people describe where they want to be, and searching only
      // titles and venues made both return nothing.
      const hay = [e.t, e.vn, e.hn, e.bo, e.sc, e.sw && e.sw.n];
      if (!hay.some((s) => (s || '').toLowerCase().includes(q))) return false;
    }
    return true;
  }

  // ---- render ----
  function rowHTML(e) {
    const time = fmtTime(e.s);
    const where = [
      e.u ? `<a href="${esc(e.u)}" rel="noopener">${esc(e.vn)}</a>` : esc(e.vn),
      e.hn ? `<a href="/nyc/${esc(e.h)}">${esc(e.hn)}</a>` : esc(e.bo || ''),
      stopHTML(e.sw),
    ].filter(Boolean).join(' <span aria-hidden="true">·</span> ');
    const title = e.u
      ? `<a href="${esc(e.u)}" rel="noopener">${esc(e.t)}</a>`
      : esc(e.t);
    return `<div class="row openable" data-id="${esc(e.i)}" tabindex="0"
      role="button" aria-label="Details for ${esc(e.t)}">
      <div class="t">${time || '<span style="color:var(--ink-2)">all day</span>'}</div>
      <div><div class="title">${title}</div><div class="where">${where}</div></div>
      <div class="right">
        <span class="tag" style="background:var(--c-${e.c});color:${PAPER_ON.includes(e.c) ? 'var(--paper)' : 'var(--ink)'}">${esc(CATS[e.c] || e.c)}</span>
      </div></div>`;
  }

  function cardHTML(e) {
    // Text first. The event's NAME is what anyone is actually reading, so it
    // gets the size and the top of the card. Category is a tag exactly as in
    // the list, plus a hairline down the left edge for scanning -- not a
    // full-bleed colour block, which at 250 cards is a wall of colour that
    // drowns the thing it is supposed to label.
    const on = PAPER_ON.includes(e.c) ? 'var(--paper)' : 'var(--ink)';
    const title = e.u ? `<a href="${esc(e.u)}" rel="noopener">${esc(e.t)}</a>` : esc(e.t);
    const when = new Date(e.d + 'T12:00:00')
      .toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
    const t = fmtTime(e.s);
    return `<div class="card openable" data-id="${esc(e.i)}" tabindex="0"
      role="button" style="--accent:var(--c-${e.c})">
      <div class="when">${when}${t ? ` · ${t}` : ' · all day'}</div>
      <h3>${title}</h3>
      <div class="w">${esc(e.vn)}${e.hn ? ' · ' + esc(e.hn) : ''}</div>
      <div class="foot">
        <span class="tag" style="background:var(--c-${e.c});color:${on}">${esc(CATS[e.c] || e.c)}</span>
        ${stopHTML(e.sw)}
      </div></div>`;
  }

  function activeCount() {
    // The day is not counted: today is the default, so counting it would show
    // "1 filter on" to someone who has touched nothing.
    return state.bands.size + state.cats.size + state.hoods.size +
      state.routes.size + state.venues.size + state.stations.size +
      (state.q ? 1 : 0);
  }

  // Live-filter a chip group by its own little search box. Chips carry
  // data-name so this never has to read rendered text, which includes the
  // count and would make "5" match half the list.
  function wireMiniSearch(inputSel, groupSel) {
    const input = $(inputSel), group = $(groupSel);
    if (!input || !group) return;
    input.addEventListener('input', () => {
      const q = input.value.trim().toLowerCase();
      $$(`${groupSel} button[data-v]`).forEach((b) =>
        b.classList.toggle('hide', !!q && !(b.dataset.name || '').includes(q)));
      // Boroughs with nothing left are hidden; a search that matches opens the
      // groups it matched, so results are not buried behind a closed summary.
      $$(`${groupSel} .boro`).forEach((d) => {
        const any = [...d.querySelectorAll('.ch')].some((b) => !b.classList.contains('hide'));
        d.classList.toggle('hide', !any);
        if (q && any) d.open = true;
      });
    });
  }

  // A collapsed borough must advertise that something inside it is on.
  function markPickedGroups() {
    $$('#hoods .boro').forEach((d) => {
      const picked = [...d.querySelectorAll('button[data-v]')]
        .some((b) => b.getAttribute('aria-pressed') === 'true');
      d.classList.toggle('haspicked', picked);
      if (picked) d.open = true;
    });
  }

  function render() {
    const hits = ALL.filter(matches);
    const label = `<b>${hits.length.toLocaleString()}</b> ${hits.length === 1 ? 'thing' : 'things'} to do`;
    $('#count').innerHTML = label;
    const dc = $('#dcount');
    if (dc) dc.innerHTML = label;

    // Filters live behind a button now, so the button has to say how many are
    // on -- an invisible active filter is how people conclude the data is wrong.
    const n = activeCount();
    const badge = $('#fcount');
    if (badge) {
      badge.textContent = String(n);
      badge.classList.toggle('hide', n === 0);
    }

    const shown = hits.slice(0, state.limit);
    const out = $('#results');

    if (!hits.length) {
      out.innerHTML = `<div class="empty"><b>Nothing here.</b>
        Try a wider date range, or clear a filter.</div>`;
    } else if (state.view === 'grid') {
      out.innerHTML = `<div class="cards">${shown.map(cardHTML).join('')}</div>`;
    } else {
      let html = '', day = null;
      for (const e of shown) {
        if (e.d !== day) {
          day = e.d;
          const n = hits.filter((x) => x.d === day).length;
          html += `<div class="daybar"><span>${fmtDay(day)}</span><span>${n}</span></div>`;
        }
        html += rowHTML(e);
      }
      out.innerHTML = html;
    }

    const more = $('#more');
    if (hits.length > state.limit) {
      more.classList.remove('hide');
      more.textContent = `Show ${Math.min(PAGE, hits.length - state.limit)} more of ${hits.length.toLocaleString()}`;
    } else {
      more.classList.add('hide');
    }
    $$('#views button').forEach((b) =>
      b.setAttribute('aria-pressed', String(b.dataset.view === state.view)));
    renderPlaces();
    writeURL();
  }

  function buildStrip() {
    const from = todayStr();
    const strip = $('#strip');
    const days = [];
    for (let i = 0; i < 14; i++) days.push(addDays(from, i));
    strip.innerHTML = days.map((d) => {
      const n = ALL.filter((e) => e.d === d).length;
      const dt = new Date(d + 'T12:00:00');
      return `<button data-day="${d}" aria-pressed="false">
        <div class="d">${i0(dt)}</div>
        <div class="n">${dt.getDate()}</div>
        <div class="c">${n}</div></button>`;
    }).join('');
    function i0(dt) {
      return dt.toDateString() === new Date().toDateString()
        ? 'Today' : dt.toLocaleDateString('en-US', { weekday: 'short' });
    }
    strip.addEventListener('click', (ev) => {
      const b = ev.target.closest('button');
      if (!b) return;
      state.day = state.day === b.dataset.day ? null : b.dataset.day;
      state.limit = PAGE;
      syncStrip();
      render();
    });
  }
  const syncStrip = () => $$('#strip button').forEach((b) =>
    b.setAttribute('aria-pressed', String(b.dataset.day === state.day)));

  // Only the trains that actually serve something in this scope. A route with
  // no events behind it is a dead control, and on an edition page most of the
  // system is exactly that.
  function buildRoutes() {
    const el = $('#routes');
    if (!el) return;
    const seen = new Set();
    for (const e of ALL) if (e.sw) for (const r of e.sw.r) seen.add(r);
    const list = ROUTE_ORDER.filter((r) => seen.has(r));
    if (!list.length) {
      el.closest('.dbody')?.querySelectorAll('.filterlabel').forEach((n) => {
        if (n.textContent === 'Near a train') n.remove();
      });
      return;
    }
    el.innerHTML = list.map((r) =>
      `<button class="ch" data-v="${esc(r)}" aria-pressed="false"
         aria-label="${esc(r)} train">${bullet(r)}</button>`).join('');
  }

  // How many events name each station as their nearest. The filter means "what
  // is nearest this stop", so this is the count that belongs on the row.
  function stationCounts() {
    const by = new Map();
    for (const e of ALL) if (e.sw) by.set(e.sw.i, (by.get(e.sw.i) || 0) + 1);
    return by;
  }

  // Times Sq-42 St serves twelve routes. Drawn in full they eat the row and the
  // station's own name ellipsises to "Ti…", which is the one thing the row
  // exists to say. Four bullets and a remainder.
  const MAXB = 4;
  function stationRow(id, name, routes, count) {
    const shown = routes.slice(0, MAXB).map(bullet).join('');
    const rest = routes.length > MAXB
      ? `<span class="more">+${routes.length - MAXB}</span>` : '';
    // A stop with nothing on is shown but not offered: picking it could only
    // ever return an empty page, and a control that does nothing is worse than
    // one that is visibly unavailable.
    const off = count ? '' : ' disabled class="off"';
    return `<button data-v="${esc(id)}" data-name="${esc(name.toLowerCase())}"
       aria-pressed="false"${off}
       title="${esc(name)} — ${esc(routes.join(' '))}">
       <span class="bullets">${shown}${rest}</span>
       <span class="lbl">${esc(name)}</span>
       <span class="n">${count || 'none'}</span>
     </button>`;
  }

  // With no line picked: the stations that have something on, busiest first.
  // With a line picked: that line, in riding order, every stop on it.
  //
  // The old list was only ever the former, which is why it read as arbitrary --
  // sorted by a number the reader cannot see the whole of, and silently missing
  // the two thirds of the system where nothing happens to be on. Order comes
  // from the schedule at build time; the browser only slices it.
  function renderStations() {
    const el = $('#stations');
    if (!el) return;
    const counts = stationCounts();
    const q = ($('#stationq')?.value || '').trim().toLowerCase();
    const hit = (name) => !q || name.toLowerCase().includes(q);

    let html = '';
    if (state.routes.size) {
      for (const ln of LINES.filter((l) => state.routes.has(l.r))) {
        const rows = ln.s
          .filter((id) => STOPS[id] && hit(STOPS[id][0]))
          .map((id) => stationRow(id, STOPS[id][0], STOPS[id][1], counts.get(id) || 0));
        if (!rows.length) continue;
        // Which line these stops belong to, because two lines can share a
        // bullet and a name: three different shuttles are all called S.
        html += `<div class="linehead">${bullet(ln.r)}<span>${esc(ln.n)}</span>
                 <span class="n">${ln.s.length} stops</span></div>` + rows.join('');
      }
    } else {
      html = [...counts.entries()]
        .filter(([id]) => STOPS[id] && hit(STOPS[id][0]))
        .sort((a, b) => b[1] - a[1] || STOPS[a[0]][0].localeCompare(STOPS[b[0]][0]))
        .map(([id, c]) => stationRow(id, STOPS[id][0], STOPS[id][1], c))
        .join('');
    }
    el.innerHTML = html || '<div class="empty">No stations match.</div>';
    syncChips('#stations', state.stations);
  }

  function wireChips(sel, set, after) {
    // A scoped page renders only the chip groups that make sense for it -- an
    // edition has no neighbourhood filter -- so every group is optional.
    // `.ch` for chip rows, bare buttons for pick lists.
    const el = $(sel);
    if (!el) return;
    el.addEventListener('click', (ev) => {
      const b = ev.target.closest('button');
      if (!b || !b.dataset.v) return;
      const v = b.dataset.v;
      set.has(v) ? set.delete(v) : set.add(v);
      b.setAttribute('aria-pressed', String(set.has(v)));
      state.limit = PAGE;
      markPickedGroups();
      if (after) after();
      render();
    });
  }
  const syncChips = (sel, set) => $$(`${sel} button[data-v]`).forEach((b) =>
    b.setAttribute('aria-pressed', String(set.has(b.dataset.v))));

  // ---- boot ----
  fetch(root.dataset.src).then((r) => r.json()).then((data) => {
    ALL = scope.hood ? data.filter((e) => e.h === scope.hood)
      : scope.cat ? data.filter((e) => e.c === scope.cat)
      : scope.venue ? data.filter((e) => e.v === scope.venue) : data;
    ALL.sort((a, b) => (a.s < b.s ? -1 : a.s > b.s ? 1 : 0));

    for (const e of ALL) if (e.sw && !STOPS[e.sw.i]) STOPS[e.sw.i] = [e.sw.n, e.sw.r];

    readURL();
    buildStrip();
    syncStrip();
    buildRoutes();
    renderStations();
    syncChips('#cats', state.cats);
    syncChips('#bands', state.bands);
    syncChips('#hoods', state.hoods);
    syncChips('#routes', state.routes);
    syncChips('#venues', state.venues);
    wireChips('#cats', state.cats);
    wireChips('#bands', state.bands);
    wireChips('#hoods', state.hoods);
    wireChips('#routes', state.routes, renderStations);
    wireChips('#venues', state.venues);
    wireChips('#stations', state.stations);
    wireMiniSearch('#hoodq', '#hoods');
    wireMiniSearch('#venueq', '#venues');
    on('#stationq', 'input', renderStations);
    markPickedGroups();

    // The order the stops come in is a property of the subway, not of tonight,
    // so it is fetched alongside rather than folded into the listings payload.
    // A failure here costs the line view and nothing else.
    if (root.dataset.lines) {
      fetch(root.dataset.lines).then((r) => r.json()).then((ix) => {
        LINES = ix.l || [];
        STOPS = { ...STOPS, ...(ix.s || {}) };
        renderStations();
      }).catch(() => {});
    }

    const q = $('#q');
    if (q) {
      q.value = state.q;
      q.addEventListener('input', () => {
        state.q = q.value.trim();
        state.limit = PAGE;
        render();
      });
    }
    // -- the filter rail, which is a drawer only on narrow screens --
    const drawer = $('#filters'), scrim = $('#scrim');
    const setDrawer = (open) => {
      if (!drawer) return;
      drawer.classList.toggle('on', open);
      scrim?.classList.toggle('on', open);
      document.body.classList.toggle('drawer-open', open);
    };
    on('#openfilters', 'click', () => setDrawer(true));
    on('#closefilters', 'click', () => setDrawer(false));
    on('#applyfilters', 'click', () => setDrawer(false));
    on('#scrim', 'click', () => setDrawer(false));
    // Keyboard parity: the rows are role=button, so they must answer the keys
    // a button answers.
    document.addEventListener('keydown', (ev) => {
      if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.classList?.contains('openable')) {
        ev.preventDefault();
        const inPlaces = ev.target.closest('#places');
        const hit = (inPlaces ? PLACES : ALL).find((x) => x.i === ev.target.dataset.id);
        if (hit) (inPlaces ? openPlace : openEvent)(hit);
      }
      if (ev.key === 'Escape') setDrawer(false);
      // `/` jumps to search. It no longer has to open anything -- the box is
      // on the page.
      if (ev.key === '/' && ev.target.tagName !== 'INPUT') {
        ev.preventDefault();
        $('#q')?.focus();
      }
    });
    on('#more', 'click', () => { state.limit += PAGE; render(); });

    on('#views', 'click', (ev) => {
      const b = ev.target.closest('button');
      if (!b) return;
      state.view = b.dataset.view;
      render();
    });
    on('#clear', 'click', () => {
      // Back to the default view, which is today -- not to an empty date range.
      // "Clear" should land where the page started, or it reads as a bug.
      state.day = todayStr(); state.q = ''; state.limit = PAGE;
      [state.bands, state.cats, state.hoods, state.routes, state.venues,
        state.stations].forEach((s) => s.clear());
      if (q) q.value = '';
      ['#hoodq', '#venueq', '#stationq'].forEach((s) => {
        const el = $(s);
        if (el) { el.value = ''; el.dispatchEvent(new Event('input')); }
      });
      syncStrip();
      ['#cats', '#bands', '#hoods', '#routes', '#venues', '#stations'].forEach((s) =>
        $$(`${s} button[data-v]`).forEach((b) => b.setAttribute('aria-pressed', 'false')));
      renderStations();
      markPickedGroups();
      render();
    });
    // The honest answer to indecision, and about twenty lines.
    on('#surprise', 'click', () => {
      const hits = ALL.filter(matches);
      if (!hits.length) return;
      const e = hits[Math.floor(Math.random() * hits.length)];
      const box = $('#surprised');
      box.innerHTML = `<div class="daybar"><span>Go to this</span>
        <span>${fmtDay(e.d)}</span></div>${rowHTML(e)}`;
      box.classList.remove('hide');
      box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });

    on('#moreplaces', 'click', () => { placeLimit += 12; renderPlaces(); });

    // Open the panel from anywhere on a row or card EXCEPT a real link. The
    // title and venue are still ordinary anchors that go straight to the
    // source; the rest of the row is the affordance for "tell me more first".
    const byId = (list, id) => list.find((x) => x.i === id);
    on('#results', 'click', (ev) => {
      if (ev.target.closest('a')) return;
      const el = ev.target.closest('[data-id]');
      const hit = el && byId(ALL, el.dataset.id);
      if (hit) openEvent(hit);
    });
    on('#places', 'click', (ev) => {
      if (ev.target.closest('a')) return;
      const el = ev.target.closest('[data-id]');
      const hit = el && byId(PLACES, el.dataset.id);
      if (hit) openPlace(hit);
    });

    render();
    document.documentElement.dataset.browseReady = '1';

    // Places load after the listings and never block them. A visitor who came
    // to see what is on tonight should not wait on 1,200 museums.
    const psrc = root.dataset.places;
    if (psrc) {
      fetch(psrc).then((r) => r.json()).then((data) => {
        PLACES = scope.hood ? data.filter((p) => p.h === scope.hood) : data;
        // Photographed places first: this section is the one place on the page
        // where an image is doing the work.
        PLACES.sort((a, b) => (b.im ? 1 : 0) - (a.im ? 1 : 0));
        renderPlaces();
      }).catch(() => {});
    }
  });
}
