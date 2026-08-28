// Minimal CORS proxy for playtestproxy-fill's deck-builder fetches, plus
// anonymous usage counters (daily page views / unique browsers / builds by
// deck source — no IPs or identifiers stored, aggregate integers only).
// Deployed as `playtestproxy-cors` (see CORS_PROXY in docs/app.js):
//   cd worker && npx wrangler deploy
const ALLOWED_HOSTS = new Set([
  "api2.moxfield.com",
  "archidekt.com",
  "deckbox.org",
  "tappedout.net",
  "edhrec.com",
  "json.edhrec.com",
  "mtgdecks.net",
]);

// Bounded label sets so a stray/crafted ping can't mint unlimited KV keys.
const PING_EVENTS = new Set(["view", "build"]);
const PING_SOURCES = new Set([
  "archidekt", "moxfield", "scryfall", "deckbox", "tappedout",
  "edhrec", "mtgdecks", "decklist", "csv", "other",
]);

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Accept, Content-Type",
};

async function bump(env, key) {
  // KV has no atomic increment; a concurrent read-modify-write can drop a
  // count. At this site's traffic that's noise, and it keeps the whole
  // stack on free-tier KV instead of needing Durable Objects.
  const cur = parseInt((await env.STATS.get(key)) || "0", 10);
  await env.STATS.put(key, String(cur + 1));
}

async function handlePing(reqUrl, env) {
  const e = reqUrl.searchParams.get("e");
  if (!PING_EVENTS.has(e)) {
    return new Response("bad event", { status: 400, headers: CORS_HEADERS });
  }
  const day = new Date().toISOString().slice(0, 10);
  if (e === "view") {
    await bump(env, `d:${day}:views`);
    if (reqUrl.searchParams.get("new") === "1") {
      await bump(env, `d:${day}:uniques`);
    }
  } else {
    const src = reqUrl.searchParams.get("src");
    await bump(env, `d:${day}:builds:${PING_SOURCES.has(src) ? src : "other"}`);
  }
  return new Response(null, { status: 204, headers: CORS_HEADERS });
}

async function handleStats(env) {
  const days = {};
  let cursor;
  do {
    const page = await env.STATS.list({ prefix: "d:", cursor });
    for (const { name } of page.keys) {
      const [, day, ...rest] = name.split(":");
      const metric = rest.join(":");
      days[day] ??= { views: 0, uniques: 0, builds: {} };
      const n = parseInt((await env.STATS.get(name)) || "0", 10);
      if (metric === "views") days[day].views = n;
      else if (metric === "uniques") days[day].uniques = n;
      else if (metric.startsWith("builds:")) days[day].builds[metric.slice(7)] = n;
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  return new Response(JSON.stringify({ days }, null, 2), {
    headers: { ...CORS_HEADERS, "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    const reqUrl = new URL(req.url);
    if (reqUrl.pathname === "/ping") return handlePing(reqUrl, env);
    if (reqUrl.pathname === "/stats") return handleStats(env);
    const target = reqUrl.searchParams.get("url");
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
