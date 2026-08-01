// Every persistent entity gets a crawl path that needs no JavaScript. Events
// are deliberately absent: they have no pages, because a URL whose content
// expires accumulates nothing.
import { neighborhoods, categories, venuesWithEvents, upcoming } from '../lib/data.js';

const SITE = 'https://happenings.nyc';

export const GET = () => {
  const urls = [
    { loc: '/nyc', pri: '1.0' },
    { loc: '/nyc/neighborhoods', pri: '0.8' },
    { loc: '/nyc/venues', pri: '0.8' },
    { loc: '/nyc/about', pri: '0.4' },
  ];
  for (const c of categories()) urls.push({ loc: `/nyc/${c.slug}`, pri: '0.9' });
  // Same guard as the page builder: an edition must have earned its URL.
  for (const h of neighborhoods(3)) {
    const evs = upcoming.filter((e) => e.neighborhood === h.slug);
    if (evs.length < 3 || new Set(evs.map((e) => e.venue_id)).size < 2) continue;
    urls.push({ loc: `/nyc/${h.slug}`, pri: '0.9' });
  }
  for (const v of venuesWithEvents(2)) urls.push({ loc: `/nyc/venues/${v.id}`, pri: '0.7' });

  const today = new Date().toISOString().slice(0, 10);
  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.map((u) => `  <url><loc>${SITE}${u.loc}</loc><lastmod>${today}</lastmod><priority>${u.pri}</priority></url>`).join('\n')}
</urlset>`;
  return new Response(body, { headers: { 'content-type': 'application/xml' } });
};
