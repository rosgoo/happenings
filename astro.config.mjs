import { defineConfig } from 'astro/config';

// Static output. The whole site is generated from data/<city>/*.json at build
// time -- no runtime database, no server. The commit is the deploy.
export default defineConfig({
  // The only place the domain is written down. Everything that needs an
  // absolute URL reads it back off `import.meta.env.SITE`.
  site: 'https://happenings.town',
  output: 'static',
  trailingSlash: 'ignore',
  build: { format: 'directory' },
});
