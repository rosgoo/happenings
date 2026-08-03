/* Vercel Web Analytics — the custom half.

   Pageviews come from the <Analytics /> component in the base layout and need
   nothing from us. This file answers the two questions a directory actually
   has to answer about itself: which listings people open, and where they go
   when they leave. This site is a router rather than a destination, so the
   click that leaves is the one worth counting -- an outbound rate that falls
   is the index failing at its only job.

   Loaded from the base layout, so the outbound listener is on every page,
   including the ones that ship no other JavaScript. */

import { track } from '@vercel/analytics';

// Vercel's collector drops any name, key or value over 255 characters, and
// allows two properties per event on Pro -- eight on Web Analytics Plus and
// Enterprise. Everything past the limit is dropped silently at their end, so
// the cut happens here instead, and each property list below is written in the
// order we would miss it. Raising the plan is this one number.
const PROPS = 2;

// Analytics is never allowed to be the reason a link stops working. Every path
// out of here is wrapped, because `track` runs before a navigation that is
// already committed and there is no user-visible way to recover if it throws.
function send(name, props) {
  try {
    track(name, Object.fromEntries(
      Object.entries(props)
        .filter(([, v]) => v !== null && v !== undefined && v !== '')
        .slice(0, PROPS)
        .map(([k, v]) => [k, typeof v === 'string' ? v.slice(0, 255) : v])));
  } catch { /* ignore */ }
}

// Opening the panel is the strongest signal of interest we can read without
// following anyone off the site: it is deliberate, it names one event, and it
// happens whether or not that event has a link to leave by. Title first,
// because "popular events" is a list of titles.
export const trackEvent = (e) => send('Event opened', {
  title: e.t, venue: e.vn, category: e.c, neighborhood: e.hn, date: e.d,
});

export const trackPlace = (p) => send('Place opened', {
  name: p.t, kind: p.kl || p.k, neighborhood: p.hn,
});

// Where a handoff happened, for reading the funnel back: a source link clicked
// from the panel means the summary did its job, from the list means the title
// alone was enough.
const zone = (el) => (el.closest('#detail') ? 'panel'
  : el.closest('#results, #surprised') ? 'listing'
    : el.closest('#places, #pcards') ? 'places'
      : 'page');

/* One delegated listener rather than a handler per render site. The listings,
   the places grid, the panel and the server-rendered no-JS fallback all build
   their own anchors, several of them from template strings, and instrumenting
   each one means the next one added is silently untracked. Cross-origin is the
   test, so this needs no list of what counts as a source. */
function outbound(ev) {
  // auxclick fires for the middle button too, which is a real open-in-new-tab
  // and not a click we should let pass unlogged.
  if (ev.button !== 0 && ev.button !== 1) return;
  const a = ev.target.closest('a[href]');
  if (!a) return;

  let url;
  try { url = new URL(a.href); } catch { return; }
  // mailto:, tel: and javascript: are not handoffs to another site.
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return;
  if (url.host === location.host) return;

  // The panel's links are labelled by function -- "Full details & tickets",
  // "Wikipedia" -- so their own text says nothing about what was being read.
  // It sets data-label to the subject; everywhere else the link text is the
  // event or venue name already, which is exactly the label we want.
  const label = a.closest('[data-label]')?.dataset.label
    || a.textContent.trim().replace(/\s+/g, ' ');

  send('Outbound', {
    host: url.host.replace(/^www\./, ''),
    label,
    zone: zone(a),
    path: location.pathname,
  });
}

document.addEventListener('click', outbound);
document.addEventListener('auxclick', outbound);
