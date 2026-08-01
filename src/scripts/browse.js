/* The browse island.
   One JSON, filtered in the browser. At a few thousand events this is instant
   and needs no server; shard by month when it stops being. All filter state
   lives in the URL, so every view is a shareable link. */

const CATS = {
  music: 'Music', sports: 'Sports', 'food-drink': 'Food & Drink', art: 'Art',
  performance: 'Performance', learning: 'Learning', community: 'Community',
  outdoors: 'Outdoors',
};
const PAPER_ON = ['performance', 'learning'];
const PAGE = 250;

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const on = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const root = $('#browse');
if (root) {
  const scope = JSON.parse(root.dataset.scope || '{}');
  const state = {
    day: null, days: 7, bands: new Set(), cats: new Set(),
    hoods: new Set(), q: '', view: 'list', limit: PAGE,
  };
  let ALL = [];

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
    state.day = p.get('day');
    state.days = Number(p.get('days') || 7);
    state.q = p.get('q') || '';
    state.view = p.get('view') === 'grid' ? 'grid' : 'list';
    for (const [key, set] of [['band', state.bands], ['cat', state.cats], ['hood', state.hoods]]) {
      set.clear();
      (p.get(key) || '').split(',').filter(Boolean).forEach((v) => set.add(v));
    }
  }
  function writeURL() {
    const p = new URLSearchParams();
    if (state.day) p.set('day', state.day);
    if (state.days !== 7) p.set('days', state.days);
    if (state.q) p.set('q', state.q);
    if (state.view !== 'list') p.set('view', state.view);
    if (state.bands.size) p.set('band', [...state.bands].join(','));
    if (state.cats.size) p.set('cat', [...state.cats].join(','));
    if (state.hoods.size) p.set('hood', [...state.hoods].join(','));
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
    if (state.q) {
      const q = state.q.toLowerCase();
      if (!(e.t.toLowerCase().includes(q) || (e.vn || '').toLowerCase().includes(q))) return false;
    }
    return true;
  }

  // ---- render ----
  function rowHTML(e) {
    const time = fmtTime(e.s);
    const where = [
      e.u ? `<a href="${esc(e.u)}" rel="noopener">${esc(e.vn)}</a>` : esc(e.vn),
      e.hn ? `<a href="/nyc/${esc(e.h)}">${esc(e.hn)}</a>` : esc(e.bo || ''),
    ].filter(Boolean).join(' <span aria-hidden="true">·</span> ');
    const title = e.u
      ? `<a href="${esc(e.u)}" rel="noopener">${esc(e.t)}</a>`
      : esc(e.t);
    return `<div class="row">
      <div class="t">${time || '<span style="color:var(--ink-2)">all day</span>'}</div>
      <div><div class="title">${title}</div><div class="where">${where}</div></div>
      <div class="right">
        <span class="tag" style="background:var(--c-${e.c});color:${PAPER_ON.includes(e.c) ? 'var(--paper)' : 'var(--ink)'}">${esc(CATS[e.c] || e.c)}</span>
      </div></div>`;
  }

  function cardHTML(e) {
    // No source in this build carries an image, so every card renders the
    // fallback -- a flat category block, which is a design element rather than
    // an apology. It shows the CATEGORY, not the title: the title already sits
    // below it as the link, and repeating it just reads as a bug.
    const ink = PAPER_ON.includes(e.c) ? 'var(--paper)' : 'var(--ink)';
    const title = e.u ? `<a href="${esc(e.u)}" rel="noopener">${esc(e.t)}</a>` : esc(e.t);
    return `<div class="card">
      <div class="img" style="background:var(--c-${e.c});color:${ink}">
        <div class="fb">${esc(CATS[e.c] || e.c)}</div></div>
      <div class="body">
        <div class="t">${new Date(e.d + 'T12:00:00').toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })} ${fmtTime(e.s)}</div>
        <h3>${title}</h3>
        <div class="w">${esc(e.vn)}${e.hn ? ' · ' + esc(e.hn) : ''}</div>
      </div></div>`;
  }

  function render() {
    const hits = ALL.filter(matches);
    $('#count').innerHTML = `<b>${hits.length.toLocaleString()}</b> ${hits.length === 1 ? 'thing' : 'things'} to do`;

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

  function wireChips(sel, set) {
    // A scoped page renders only the chip groups that make sense for it -- an
    // edition has no neighbourhood filter -- so every group is optional.
    const el = $(sel);
    if (!el) return;
    el.addEventListener('click', (ev) => {
      const b = ev.target.closest('.ch');
      if (!b) return;
      const v = b.dataset.v;
      set.has(v) ? set.delete(v) : set.add(v);
      b.setAttribute('aria-pressed', String(set.has(v)));
      state.limit = PAGE;
      render();
    });
  }
  const syncChips = (sel, set) => $$(`${sel} .ch`).forEach((b) =>
    b.setAttribute('aria-pressed', String(set.has(b.dataset.v))));

  // ---- boot ----
  fetch(root.dataset.src).then((r) => r.json()).then((data) => {
    ALL = scope.hood ? data.filter((e) => e.h === scope.hood)
      : scope.cat ? data.filter((e) => e.c === scope.cat)
      : scope.venue ? data.filter((e) => e.v === scope.venue) : data;
    ALL.sort((a, b) => (a.s < b.s ? -1 : a.s > b.s ? 1 : 0));

    readURL();
    buildStrip();
    syncStrip();
    syncChips('#cats', state.cats);
    syncChips('#bands', state.bands);
    syncChips('#hoods', state.hoods);
    wireChips('#cats', state.cats);
    wireChips('#bands', state.bands);
    wireChips('#hoods', state.hoods);

    const q = $('#q');
    if (q) {
      q.value = state.q;
      q.addEventListener('input', () => {
        state.q = q.value.trim();
        state.limit = PAGE;
        render();
      });
    }
    on('#more', 'click', () => { state.limit += PAGE; render(); });
    on('#views', 'click', (ev) => {
      const b = ev.target.closest('button');
      if (!b) return;
      state.view = b.dataset.view;
      render();
    });
    on('#clear', 'click', () => {
      state.day = null; state.q = ''; state.limit = PAGE;
      [state.bands, state.cats, state.hoods].forEach((s) => s.clear());
      if (q) q.value = '';
      syncStrip();
      ['#cats', '#bands', '#hoods'].forEach((s) =>
        $$(`${s} .ch`).forEach((b) => b.setAttribute('aria-pressed', 'false')));
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

    render();
    document.documentElement.dataset.browseReady = '1';
  });
}
