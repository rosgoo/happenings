// The client payload for the browse island. Prerendered to a static file --
// the "API" is a JSON on a CDN, which is the cheapest possible serving model.
import { upcoming, slim } from '../../lib/data.js';

export const GET = () =>
  new Response(JSON.stringify(slim(upcoming)), {
    headers: { 'content-type': 'application/json' },
  });
