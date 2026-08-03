/* The places island.

   The page server-renders the first 60 cards so a crawler and a no-JS visitor
   see real content. On load this takes over with the full 1,204 from
   /nyc/places.json -- filtering a 60-card slice would have been a filter that
   silently could not reach 95% of the data, which is worse than no filter. */

import { openPlace } from './detail.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

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
const routeOn = (r) => (['N', 'Q', 'R', 'W', 'L', 'S'].includes(r) ? '#000' : '#fff');
const bullet = (r) => `<span class="bullet${r === 'SIR' ? ' sir' : ''}" ` +
  `style="background:${ROUTE_COLOR[r] || '#555'};color:${routeOn(r)}">${esc(r)}</span>`;

const PAGE = 60;
const root = $('#placebrowse');

// The band bits lib/hours.py packs into each hex digit of the week mask, one
// digit per day from Monday.
const BAND_BIT = { morning: 1, afternoon: 2, evening: 4, late: 8 };

if (root) {
  const state = {
    kinds: new Set(), boros: new Set(), routes: new Set(), bands: new Set(),
    photo: false, q: '', limit: PAGE,
  };
  let ALL = [];
  const todayIdx = (new Date().getDay() + 6) % 7;   // getDay() is Sunday-first

  function readURL() {
    const p = new URLSearchParams(location.search);
    state.q = p.get('q') || '';
    state.photo = p.get('photo') === '1';
    for (const [key, set] of [['kind', state.kinds], ['boro', state.boros],
      ['route', state.routes], ['band', state.bands]]) {
      set.clear();
      (p.get(key) || '').split(',').filter(Boolean).forEach((v) => set.add(v));
    }
  }
  function writeURL() {
    const p = new URLSearchParams();
    if (state.q) p.set('q', state.q);
    if (state.photo) p.set('photo', '1');
    if (state.kinds.size) p.set('kind', [...state.kinds].join(','));
    if (state.boros.size) p.set('boro', [...state.boros].join(','));
    if (state.routes.size) p.set('route', [...state.routes].join(','));
    if (state.bands.size) p.set('band', [...state.bands].join(','));
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  }

  const matches = (p) => {
    if (state.kinds.size && !state.kinds.has(p.k)) return false;
    if (state.boros.size && !state.boros.has(p.bo)) return false;
    if (state.routes.size &&
        !(p.sw && p.sw.r.some((r) => state.routes.has(r)))) return false;
    if (state.photo && !p.im) return false;
    // Open TODAY in the chosen band. Unlike the section under the listings,
    // this one drops the places whose hours nobody has recorded: the chip
    // there is context on a browse, and here it is the question being asked,
    // and "open this evening" cannot honestly include a place that does not
    // say. The count line says how many that leaves out.
    if (state.bands.size) {
      if (!p.hm) return false;
      const bits = parseInt(p.hm[todayIdx], 16);
      if (![...state.bands].some((b) => bits & BAND_BIT[b])) return false;
    }
    if (state.q) {
      const q = state.q.toLowerCase();
      if (![p.t, p.kl, p.hn, p.bo, p.sw && p.sw.n]
        .some((s) => (s || '').toLowerCase().includes(q))) return false;
    }
    return true;
  };

  const cardHTML = (p) => {
    const title = p.u ? `<a href="${esc(p.u)}" rel="noopener">${esc(p.t)}</a>` : esc(p.t);
    const where = p.hn
      ? `<a href="/nyc/${esc(p.h)}" style="color:var(--ink-2)">${esc(p.hn)}</a>`
      : esc(p.bo || '');
    const stop = p.sw
      ? ` · <span class="stop">${p.sw.r.map(bullet).join('')} ${esc(p.sw.n)}</span>` : '';
    return `<div class="pcard openable" id="${esc(p.i)}" data-id="${esc(p.i)}"
      tabindex="0" role="button" aria-label="Details for ${esc(p.t)}">
      <div class="ph" style="background:var(--c-${p.c})">
        ${p.im ? `<img src="${esc(p.im)}" alt="${esc(p.t)}" loading="lazy"
              referrerpolicy="no-referrer" decoding="async" />` : ''}
        <span class="kindtag">${esc(p.kl || p.k)}</span>
        ${p.im ? '' : `<span class="fb">${esc(p.kl || p.k)}</span>`}
      </div>
      <div class="pb"><h3>${title}</h3><div class="w">${where}${stop}</div></div>
    </div>`;
  };

  function render() {
    const hits = ALL.filter(matches);
    const quiet = state.bands.size ? ALL.filter((p) => !p.hm).length : 0;
    $('#pcount').innerHTML =
      `<b>${hits.length.toLocaleString()}</b> ${hits.length === 1 ? 'place' : 'places'}`
      + (quiet ? ` <span style="color:var(--ink-2)">· ${quiet.toLocaleString()}
         more list no hours and are not counted</span>` : '');
    const box = $('#pcards');
    box.innerHTML = hits.length
      ? hits.slice(0, state.limit).map(cardHTML).join('')
      : '<div class="empty"><b>Nothing here.</b> Try clearing a filter.</div>';
    const more = $('#pmore');
    more.classList.toggle('hide', hits.length <= state.limit);
    if (hits.length > state.limit) {
      more.textContent =
        `Show ${Math.min(PAGE, hits.length - state.limit)} more of ${hits.length.toLocaleString()}`;
    }
    writeURL();
  }

  function wire(sel, set) {
    const el = $(sel);
    if (!el) return;
    el.addEventListener('click', (ev) => {
      const b = ev.target.closest('.ch');
      if (!b || !b.dataset.v) return;
      const v = b.dataset.v;
      set.has(v) ? set.delete(v) : set.add(v);
      b.setAttribute('aria-pressed', String(set.has(v)));
      state.limit = PAGE;
      render();
    });
  }
  const sync = (sel, set) => $$(`${sel} .ch`).forEach((b) =>
    b.setAttribute('aria-pressed', String(set.has(b.dataset.v))));

  fetch('/nyc/places.json').then((r) => r.json()).then((data) => {
    // Photographed first, same order the server rendered, so taking over does
    // not visibly reshuffle the page.
    ALL = data.sort((a, b) => (b.im ? 1 : 0) - (a.im ? 1 : 0));
    readURL();
    ['#pkinds', '#pboros', '#proutes', '#pbands'].forEach((s, i) =>
      sync(s, [state.kinds, state.boros, state.routes, state.bands][i]));
    wire('#pkinds', state.kinds);
    wire('#pboros', state.boros);
    wire('#proutes', state.routes);
    wire('#pbands', state.bands);

    const q = $('#pq');
    q.value = state.q;
    q.addEventListener('input', () => {
      state.q = q.value.trim();
      state.limit = PAGE;
      render();
    });
    const photo = $('#pphoto');
    photo.setAttribute('aria-pressed', String(state.photo));
    photo.addEventListener('click', () => {
      state.photo = !state.photo;
      photo.setAttribute('aria-pressed', String(state.photo));
      state.limit = PAGE;
      render();
    });
    $('#pmore').addEventListener('click', () => { state.limit += PAGE; render(); });
    // Same rule as the listings: anywhere on the card except a real link.
    $('#pcards').addEventListener('click', (ev) => {
      if (ev.target.closest('a')) return;
      const el = ev.target.closest('[data-id]');
      const hit = el && ALL.find((x) => x.i === el.dataset.id);
      if (hit) openPlace(hit);
    });
    document.addEventListener('keydown', (ev) => {
      if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.classList?.contains('openable')) {
        ev.preventDefault();
        const hit = ALL.find((x) => x.i === ev.target.dataset.id);
        if (hit) openPlace(hit);
      }
    });
    $('#pclear').addEventListener('click', () => {
      state.q = ''; state.photo = false; state.limit = PAGE;
      [state.kinds, state.boros, state.routes, state.bands].forEach((s) => s.clear());
      q.value = '';
      photo.setAttribute('aria-pressed', 'false');
      ['#pkinds', '#pboros', '#proutes', '#pbands'].forEach((s) =>
        $$(`${s} .ch`).forEach((b) => b.setAttribute('aria-pressed', 'false')));
      render();
    });
    render();
  }).catch(() => {});
}
