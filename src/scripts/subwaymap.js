/* The stop search on /nyc/subway.

   Every one of the 767 stops is already in the HTML, so this filters the DOM it
   was handed instead of fetching an index and re-rendering. That is the whole
   budget: no payload, no first-keystroke delay, and the page a crawler or a
   no-JS visitor gets is the complete one.

   Three things are searchable, because on this page they are the same question
   asked three ways: the stop ("bedford"), the line ("canarsie", "L"), and the
   neighbourhood the stop stands in ("bushwick"). Matching a line keeps all of
   its stops -- somebody who typed the line's name wants to read the line, not
   the one stop whose name happens to contain the letters. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const input = $('#sq');
if (input) {
  const count = $('#scount');
  const nomatch = $('#noline');
  const cards = $$('.linecard').map((card) => {
    const line = card.dataset.l || '';
    const route = line.split(' ')[0];
    return {
      card,
      route,
      name: line.slice(route.length + 1),
      meta: $('.linemeta', card),
      stops: $$('.stops li', card).map((li) => ({
        li, i: li.dataset.i, s: li.dataset.s || '',
        hood: li.dataset.h || '', h: (li.dataset.h || '').toLowerCase(),
        via: null,
      })),
    };
  });
  const total = new Set(cards.flatMap((c) => c.stops.map((s) => s.i))).size;

  // Built on demand rather than shipped on all 767 rows: it is only ever shown
  // on the handful a neighbourhood search matched.
  function mark(st, on) {
    if (on && !st.via) {
      st.via = document.createElement('span');
      st.via.className = 'via';
      st.via.textContent = st.hood;
      st.li.insertBefore(st.via, $('.n', st.li));
    } else if (!on && st.via) {
      st.via.remove();
      st.via = null;
    }
  }

  function apply() {
    const q = input.value.trim().toLowerCase();
    const seen = new Set();
    let openLines = 0;

    // A single character is a line and nothing else. Nobody searches a station
    // one letter at a time, and every way of matching stops on one letter is
    // useless: as a substring "l" is in 289 of the 445 names, and even as a
    // word start "a" is 197, because almost every station in New York is on an
    // Av. So "a" is the A train, full stop.
    //
    // Two characters match the start of a word -- Bedford, Bergen, Beverly for
    // "be". Past that the loose match is the useful one, so that "ockaway"
    // still finds Rockaway Blvd while somebody is halfway through typing it.
    const rx = q.length === 2 &&
      new RegExp(`\\b${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`);
    const hitStop = (s) => (q.length === 1 ? false : rx ? rx.test(s) : s.includes(q));

    for (const c of cards) {
      // A bullet is one or three characters, so a bare "l" can only ever mean
      // the L -- matched against the route itself, never as a substring, or it
      // would also find the " l" in every line whose name ends "Local".
      const wholeLine = !!q &&
        (c.route === q || (q.length > 3 && c.name.includes(q)));
      let shown = 0;
      for (const st of c.stops) {
        const byName = !q || wholeLine || hitStop(st.s);
        // "bedford" turns up Utica Av, which is in Bedford-Stuyvesant and is
        // the right answer to "what can I get to around Bedford" -- but on a
        // row that only says "Utica Av" it looks like the filter misfired. So
        // a stop matched on its neighbourhood says which one.
        const byHood = !byName && !!q && !!st.h && hitStop(st.h);
        st.li.classList.toggle('hide', !(byName || byHood));
        mark(st, byHood);
        if (byName || byHood) { shown++; seen.add(st.i); }
      }
      c.card.classList.toggle('hide', !shown);
      if (shown) openLines++;
      // The card's own count has to follow the filter. Left at the line's true
      // total it would say "45 stops" over a list of three, which is the kind
      // of number that makes a reader distrust every other number on the page.
      if (c.meta) {
        c.meta.textContent = (!q || shown === c.stops.length)
          ? c.meta.dataset.full
          : `${shown} of ${c.stops.length} stops`;
      }
    }

    nomatch?.classList.toggle('hide', openLines > 0);
    if (count) {
      // Stations, not rows: Times Sq is one place to stand, whether you counted
      // it once or on all twelve of the lines that serve it.
      count.textContent = !q
        ? `${total} stations on ${cards.length} lines`
        : `${seen.size} station${seen.size === 1 ? '' : 's'} on ` +
          `${openLines} line${openLines === 1 ? '' : 's'}`;
    }
    writeURL(q);
  }

  // Same bargain as the rest of the site: the filter lives in the URL, so a
  // search is a link somebody can send.
  function writeURL(q) {
    const p = new URLSearchParams(location.search);
    if (q) p.set('q', q); else p.delete('q');
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  }

  input.value = new URLSearchParams(location.search).get('q') || '';
  input.addEventListener('input', apply);
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') { input.value = ''; apply(); }
  });
  // `/` focuses the search, as it does on the listings.
  document.addEventListener('keydown', (ev) => {
    if (ev.key === '/' && ev.target.tagName !== 'INPUT') {
      ev.preventDefault();
      input.focus();
    }
  });
  apply();
}
