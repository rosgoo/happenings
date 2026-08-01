import { defineConfig } from 'astro/config';

// Static output. The whole site is generated from data/<city>/*.json at build
// time -- no runtime database, no server. The commit is the deploy.
export default defineConfig({
  site: 'https://happenings.nyc',
  output: 'static',
  trailingSlash: 'ignore',
  build: { format: 'directory' },
});
