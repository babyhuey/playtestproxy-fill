// Minimal CORS proxy for playtestproxy-fill's deck-builder fetches.
// Only proxies the allowlisted deck hosts — not a general-purpose open proxy.
// Deployed as `playtestproxy-cors` (see CORS_PROXY in docs/app.js):
//   npx wrangler deploy worker/cors-proxy.js --name playtestproxy-cors \
//     --compatibility-date 2026-08-01
const ALLOWED_HOSTS = new Set([
  "api2.moxfield.com",
  "archidekt.com",
  "deckbox.org",
  "tappedout.net",
  "edhrec.com",
  "json.edhrec.com",
  "mtgdecks.net",
]);

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Accept, Content-Type",
};

export default {
  async fetch(req) {
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    const target = new URL(req.url).searchParams.get("url");
    let targetUrl;
    try {
      targetUrl = new URL(target);
    } catch {
      return new Response("bad url", { status: 400, headers: CORS_HEADERS });
    }
    if (targetUrl.protocol !== "https:" || !ALLOWED_HOSTS.has(targetUrl.hostname)) {
      return new Response("host not allowed", { status: 403, headers: CORS_HEADERS });
    }
    const upstream = await fetch(targetUrl, {
      headers: {
        "User-Agent":
          "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        Accept: req.headers.get("Accept") || "*/*",
      },
    });
    const headers = new Headers(CORS_HEADERS);
    const ct = upstream.headers.get("content-type");
    if (ct) headers.set("Content-Type", ct);
    return new Response(upstream.body, { status: upstream.status, headers });
  },
};
