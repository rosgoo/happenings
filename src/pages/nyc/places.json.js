// The client payload for places. Separate file from events.json on purpose:
// most visits never scroll to the places section, and merging them would put
// 1,200 records nobody asked for into the critical path of the listings.
import { places, slimPlaces } from '../../lib/data.js';

export const GET = () =>
  new Response(JSON.stringify(slimPlaces(places)), {
    headers: { 'content-type': 'application/json' },
  });
