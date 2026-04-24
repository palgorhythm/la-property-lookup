const REPO = 'palgorhythm/la-property-lookup';
const TTL_MS = 24 * 60 * 60 * 1000; // 24 hours

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

function ghHeaders(pat) {
  return {
    'Authorization': `Bearer ${pat}`,
    'Accept': 'application/vnd.github+json',
    'Content-Type': 'application/json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'la-property-lookup-worker',
  };
}

function validateAddress(address) {
  if (!address || address.trim().length < 5) return 'address is required';
  // Require city, state, zip — must contain at least one comma and end with digits
  if (!address.includes(',')) return 'address must include city, state, and zip (e.g. "1923 Preston Ave, Los Angeles, CA 90026")';
  if (!/\d{5}/.test(address)) return 'address must include a 5-digit zip code';
  return null;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: CORS });
    }

    // POST /lookup  body: { address } or ?address=
    if (request.method === 'POST' && url.pathname === '/lookup') {
      let address;
      try {
        const body = await request.json();
        address = body.address;
      } catch {
        address = url.searchParams.get('address');
      }

      const err = validateAddress(address);
      if (err) return Response.json({ error: err }, { status: 400, headers: CORS });

      const key = Date.now().toString();

      const res = await fetch(
        `https://api.github.com/repos/${REPO}/actions/workflows/lookup.yml/dispatches`,
        {
          method: 'POST',
          headers: ghHeaders(env.GITHUB_PAT),
          body: JSON.stringify({ ref: 'main', inputs: { address: address.trim(), output_key: key } }),
        }
      );

      if (!res.ok) {
        const text = await res.text();
        return Response.json({ error: `failed to trigger workflow: ${text}` }, { status: 502, headers: CORS });
      }

      return Response.json({ job_id: key, address: address.trim() }, { headers: CORS });
    }

    // GET /result/:key
    if (request.method === 'GET' && url.pathname.startsWith('/result/')) {
      const key = url.pathname.split('/').pop();

      if (!/^\d+$/.test(key)) {
        return Response.json({ error: 'invalid job_id' }, { status: 400, headers: CORS });
      }

      const res = await fetch(
        `https://api.github.com/repos/${REPO}/contents/output/${key}.md`,
        { headers: ghHeaders(env.GITHUB_PAT) }
      );

      if (res.status === 404) {
        return Response.json({ status: 'pending' }, { status: 202, headers: CORS });
      }

      if (!res.ok) {
        return Response.json({ error: 'failed to fetch result' }, { status: 502, headers: CORS });
      }

      const data = await res.json();
      const content = atob(data.content.replace(/\n/g, ''));

      return new Response(content, {
        headers: { ...CORS, 'Content-Type': 'text/markdown; charset=utf-8' },
      });
    }

    return Response.json({ error: 'not found' }, { status: 404, headers: CORS });
  },

  // Cron: runs every 6 hours, triggers the cleanup workflow
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      fetch(
        `https://api.github.com/repos/${REPO}/actions/workflows/cleanup.yml/dispatches`,
        {
          method: 'POST',
          headers: ghHeaders(env.GITHUB_PAT),
          body: JSON.stringify({ ref: 'main' }),
        }
      )
    );
  },
};
