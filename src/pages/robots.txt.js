// A route rather than a file in public/, for one reason: the Sitemap line is
// an absolute URL, and a static robots.txt is where a domain string goes stale
// silently. This reads the same `site` the sitemap does.
const SITE = import.meta.env.SITE;

export const GET = () =>
  new Response(
    `User-agent: *
Allow: /
Sitemap: ${SITE}/sitemap.xml
`,
    { headers: { 'content-type': 'text/plain' } },
  );
