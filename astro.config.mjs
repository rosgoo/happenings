import { defineConfig } from 'astro/config';

// Static output. The whole site is generated from data/<city>/*.json at build
// time -- no runtime database, no server. The commit is the deploy.
export default defineConfig({
  // The only place the domain is written down. Everything that needs an
  // absolute URL reads it back off `import.meta.env.SITE`.
  //
  // With the www, because that is what Vercel actually serves -- the apex 308s
  // here. A canonical tag that points at a redirect is the exact ambiguity
  // canonical tags exist to remove. If the apex is ever made primary, this
  // line is the only edit.
  site: 'https://www.happenings.town',
  output: 'static',
  trailingSlash: 'ignore',
  build: { format: 'directory' },
});
