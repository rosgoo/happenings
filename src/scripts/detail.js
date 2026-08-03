/* The detail panel, shared by the listings island and the places page.

   A row in a list can only show a line and a half. Everything the pipeline
   knows about an event -- the end time, the description, how far the train
   actually is, which feed it came from -- was being thrown away at render.
   This panel is where that goes, and where the links live: the whole point of
   an index that credits its sources is that you can leave it for the real page.

   It injects its own markup so both entry points get the same panel without a
   shared component, and it reuses #scrim when the page already has one. */

// Opening the panel is counted here rather than at the two click handlers that
// call it, so the keyboard path and the places page are covered by the same
// line as the listings.
import { trackEvent, trackPlace } from './analytics.js';

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

const CATS = {
  music: 'Music', sports: 'Sports', 'food-drink': 'Food & Drink', art: 'Art',
  performance: 'Performance', learning: 'Learning', community: 'Community',
  outdoors: 'Outdoors',
};

// Attribution matters: this index republishes other people's listings, and a
// reader deserves to know whose. Codes are set in lib/data.js.
const SOURCES = {
  tm: ['Ticketmaster', 'https://www.ticketmaster.com/'],
  pe: ['NYC Office of Citywide Event Coordination and Management',
    'https://data.cityofnewyork.us/City-Government/NYC-Permitted-Event-Information/tvpp-9vvx'],
  pk: ['NYC Parks', 'https://www.nycgovparks.org/events'],
  sq: ['The venue’s own calendar', null],
  lu: ['Luma', 'https://luma.com/nyc'],
};

const fmtTime = (s) => {
  const t = (s || '').slice(11);
  if (!t || t === '00:00') return '';
  let [h, m] = t.split(':').map(Number);
  const ap = h >= 12 ? 'pm' : 'am';
  h = h % 12 === 0 ? 12 : h % 12;
  return m === 0 ? `${h}${ap}` : `${h}:${String(m).padStart(2, '0')}${ap}`;
};
const fmtLongDay = (d) => new Date(d + 'T12:00:00').toLocaleDateString('en-US',
  { weekday: 'long', month: 'long', day: 'numeric' });

// A distance in metres is not how anyone thinks about a walk. 80 m/min is the
// usual planning figure and it is close enough to be honest.
const walk = (m) => (m == null ? '' : `${Math.max(1, Math.round(m / 80))} min walk`);

let panel = null;
let scrim = null;

function ensure() {
  if (panel) return;
  panel = document.createElement('aside');
  panel.id = 'detail';
  panel.setAttribute('aria-hidden', 'true');
  panel.setAttribute('aria-label', 'Details');
  panel.innerHTML = `<div class="dhead"><b id="detailkind">Details</b>
      <button class="iconbtn" id="detailclose" aria-label="Close">×</button></div>
    <div class="dbody" id="detailbody"></div>`;
  document.body.appendChild(panel);

  scrim = document.querySelector('#scrim');
  if (!scrim) {
    scrim = document.createElement('div');
    scrim.id = 'scrim';
    document.body.appendChild(scrim);
  }
  // Hotlinked artwork rots, and in here a broken image is a grey box directly
  // above the title rather than a small gap in a grid. Capture phase because
  // `error` does not bubble, and registered on the panel once because the body
  // is replaced on every open.
  panel.addEventListener('error', (ev) => {
    const t = ev.target;
    if (t && t.tagName === 'IMG' && t.classList.contains('dimg')) t.remove();
  }, true);

  panel.querySelector('#detailclose').addEventListener('click', close);
  scrim.addEventListener('click', close);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && panel.classList.contains('on')) close();
  });
}

export function close() {
  if (!panel) return;
  panel.classList.remove('on');
  panel.setAttribute('aria-hidden', 'true');
  scrim.classList.remove('on');
  document.body.classList.remove('drawer-open');
}

function open(kindLabel, html, subject) {
  ensure();
  panel.querySelector('#detailkind').textContent = kindLabel;
  panel.querySelector('#detailbody').innerHTML = html;
  // What the panel is currently about, for the outbound listener in
  // analytics.js -- the links in here are labelled by function rather than by
  // subject, so their text cannot say which event was being read.
  panel.dataset.label = subject || '';
  panel.classList.add('on');
  panel.setAttribute('aria-hidden', 'false');
  scrim.classList.add('on');
  document.body.classList.add('drawer-open');
  panel.querySelector('#detailclose').focus();
}

const row = (label, value) => (value
  ? `<div class="drow"><div class="dk">${esc(label)}</div><div class="dv">${value}</div></div>`
  : '');

const mapLink = (lat, lon, name) => (lat && lon
  ? `<a href="https://www.google.com/maps/search/?api=1&query=${lat},${lon}" rel="noopener">
       Open in Maps</a> <span style="color:var(--ink-2)">(${esc(name || '')})</span>`
  : '');

export function openEvent(e) {
  const when = [fmtLongDay(e.d), fmtTime(e.s) ? `${fmtTime(e.s)}${e.e && fmtTime(e.e) ? `–${fmtTime(e.e)}` : ''}` : 'all day']
    .filter(Boolean).join(' · ');
  const [srcName, srcUrl] = SOURCES[e.src] || [e.src, null];

  const links = [
    e.u ? `<a class="ch act" href="${esc(e.u)}" rel="noopener">Full details &amp; tickets →</a>` : '',
    e.h ? `<a class="ch" href="/nyc/${esc(e.h)}">More in ${esc(e.hn)}</a>` : '',
    e.v ? `<a class="ch" href="/nyc/venues/${esc(e.v)}">More at ${esc(e.vn)}</a>` : '',
    e.c ? `<a class="ch" href="/nyc/${esc(e.c)}">More ${esc((CATS[e.c] || e.c).toLowerCase())}</a>` : '',
  ].filter(Boolean).join(' ');

  open('Event', `
    ${e.im ? `<img class="dimg" src="${esc(e.im)}" alt="" decoding="async" />` : ''}
    <h3 class="dtitle">${esc(e.t)}</h3>
    <div class="dtags">
      <span class="tag" style="background:var(--c-${e.c});color:${['performance', 'learning'].includes(e.c) ? 'var(--paper)' : 'var(--ink)'}">${esc(CATS[e.c] || e.c)}</span>
      ${e.sc ? `<span class="sub">${esc(String(e.sc).replace(/-/g, ' '))}</span>` : ''}
    </div>
    ${e.de ? `<p class="ddesc">${esc(e.de)}</p>` : ''}
    ${row('When', esc(when))}
    ${row('Where', [esc(e.vn), e.hn ? `<a href="/nyc/${esc(e.h)}">${esc(e.hn)}</a>` : esc(e.bo || '')].filter(Boolean).join(' · '))}
    ${row('Nearest stop', e.sw ? `${e.sw.r.map(bullet).join('')} ${esc(e.sw.n)}
        <span style="color:var(--ink-2)">· ${walk(e.sw.m)}</span>` : '')}
    ${row('Map', mapLink(e.la, e.lo, e.vn))}
    ${row('Listed by', srcUrl ? `<a href="${srcUrl}" rel="noopener">${esc(srcName)}</a>` : esc(srcName))}
    ${e.u ? '' : '<p class="dnote">This source doesn’t publish a link for the individual event.</p>'}
    <div class="dlinks">${links}</div>
  `, e.t);
  trackEvent(e);
}

export function openPlace(p) {
  const links = [
    p.u ? `<a class="ch act" href="${esc(p.u)}" rel="noopener">Visit website →</a>` : '',
    p.wp ? `<a class="ch" href="https://en.wikipedia.org/wiki/${encodeURIComponent(String(p.wp).replace(/^en:/, ''))}" rel="noopener">Wikipedia</a>` : '',
    p.h ? `<a class="ch" href="/nyc/${esc(p.h)}">What's on in ${esc(p.hn)}</a>` : '',
  ].filter(Boolean).join(' ');

  open('Place', `
    ${p.im ? `<img class="dimg" src="${esc(p.im)}" alt="${esc(p.t)}"
        referrerpolicy="no-referrer" decoding="async" />` : ''}
    <h3 class="dtitle">${esc(p.t)}</h3>
    <div class="dtags"><span class="tag"
      style="background:var(--c-${p.c});color:${['performance', 'learning'].includes(p.c) ? 'var(--paper)' : 'var(--ink)'}">${esc(p.kl || p.k)}</span></div>
    ${row('Where', [p.hn ? `<a href="/nyc/${esc(p.h)}">${esc(p.hn)}</a>` : '', esc(p.bo || '')].filter(Boolean).join(' · '))}
    ${row('Nearest stop', p.sw ? `${p.sw.r.map(bullet).join('')} ${esc(p.sw.n)}
        <span style="color:var(--ink-2)">· ${walk(p.sw.m)}</span>` : '')}
    ${row('Opening hours', p.oh ? `<code>${esc(p.oh)}</code>` : 'not listed')}
    ${row('Map', mapLink(p.la, p.lo, p.t))}
    ${row('Listed by', p.lb === 'curated'
      ? 'happenings — this place is not in OpenStreetMap yet'
      : '<a href="https://www.openstreetmap.org/" rel="noopener">OpenStreetMap</a>')}
    ${p.cr ? row('Photo', `${p.cp ? `<a href="${esc(p.cp)}" rel="noopener">${esc(p.cr)}</a>` : esc(p.cr)} · Wikimedia Commons`) : ''}
    ${p.oh ? '<p class="dnote">Hours are as recorded in OpenStreetMap and are not checked against the venue.</p>' : ''}
    <div class="dlinks">${links}</div>
  `, p.t);
  trackPlace(p);
}
