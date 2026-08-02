// The line index for the browse island. Its own file for the same reason as
// places.json: the rail only needs it once a line is picked, and it describes
// the subway rather than the listings -- it does not change when the events do.
import { slimLines } from '../../lib/data.js';

export const GET = () =>
  new Response(JSON.stringify(slimLines()), {
    headers: { 'content-type': 'application/json' },
  });
