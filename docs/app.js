// playtestproxy-fill frontend
// - Fetches an Archidekt deck (via CORS proxy because Archidekt's API is
//   locked to localhost:3000).
// - Resolves each card's image via Scryfall (which has open CORS).
// - Bundles the unmodified Scryfall PNGs into a ZIP for the user to drop
//   into TCGPlaytest, which expands the print bleed server-side.

// Clickjacking defence. GitHub Pages can't set X-Frame-Options and meta-CSP
// frame-ancestors is ignored by browsers; this is the only real fix for a
// static site we don't control headers on.
if (window.top !== window.self) {
  try { window.top.location = window.self.location; } catch { /* cross-origin block — already safe */ }
}

// Hard cap on parsed decklist entries. Lookups go through Scryfall's
// /cards/collection (75 ids / request, 500ms gap), so 2000 cards is
// ~27 batched calls — finite, but a 100k-line paste would still OOM the
// tab. Self-DoS only, but worth a cheap upfront bound.
const MAX_DECKLIST_ENTRIES = 2000;

const ARCHIDEKT = (id) => `https://archidekt.com/api/decks/${id}/`;
const MOXFIELD = (id) => `https://api2.moxfield.com/v3/decks/all/${id}`;
const TAPPEDOUT_TXT = (slug) => `https://tappedout.net/mtg-decks/${slug}/?fmt=txt`;
const SCRYFALL_DECK_EXPORT = (id) => `https://api.scryfall.com/decks/${id}/export/text`;
const DECKBOX_EXPORT = (id) => `https://deckbox.org/sets/${id}/export?format=tcg`;
// corsproxy.io's documented API is `?url=<encoded>`. Verified 2026-07-04
// from a real browser (deployed GitHub Pages origin AND localhost) against
// the Archidekt API: both this form and the legacy bare `?<encoded>` form
// returned the deck JSON. curl gets 403 from both — the proxy blocks
// non-browser clients, so re-verification must happen in a browser.
const CORS_PROXY = (url) => `https://corsproxy.io/?url=${encodeURIComponent(url)}`;

function cacheBust(url) {
  // Deck-builder APIs answer through corsproxy.io with
  // `cache-control: public, max-age=3600, s-maxage=3600`, so both the browser
  // AND corsproxy's shared edge cache would serve an hour-stale deck — hiding a
  // printing swap or custom-art edit the user just made upstream (reproduces
  // even in a private window, since the staleness lives in the shared cache).
  // A unique query param is a fresh cache key at every layer, forcing a real
  // refetch. Deck APIs ignore the unknown param.
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}_cb=${Date.now()}`;
}
const SCRYFALL = (uid) => `https://api.scryfall.com/cards/${uid}`;
const SCRYFALL_NAMED = "https://api.scryfall.com/cards/named";
const SCRYFALL_BY_SET = (set, cn) => `https://api.scryfall.com/cards/${set}/${cn}`;
const SCRYFALL_COLLECTION = "https://api.scryfall.com/cards/collection";
// Per Scryfall's docs: collection accepts up to 75 identifiers per request,
// and the /cards/* endpoints (named / search / random / collection) are
// limited to 2 req/sec — a 500ms gap between calls. Picking 550ms gives a
// small safety margin and avoids burning the 30-second 429 lockout the
// API applies on overage.
const SCRYFALL_COLLECTION_BATCH_SIZE = 75;
const SCRYFALL_NAMED_INTERVAL_MS = 550;
// Per-card /cards/named calls (the unresolved-name fallback chain) are one
// card per request, so we can stay comfortably under 2 req/sec with a short
// gap — Scryfall's docs explicitly recommend 50–100ms for single-card calls.
const SCRYFALL_SINGLE_INTERVAL_MS = 110;
// Scryfall locks out for 30s on a 429. If a response omits Retry-After we
// fall back to this (with a 2s cushion) so withRetry's last attempt lands
// after the lockout has cleared instead of inside it.
const SCRYFALL_429_FALLBACK_MS = 32_000;
const SCRYFALL_SEARCH = "https://api.scryfall.com/cards/search";

// Concurrent build workers (see BUILD_CONCURRENCY) all resolve card data
// through scryfallCard(). A per-caller sleep before each fetch paces that
// ONE caller, but does nothing to pace the aggregate request rate once
// several workers overlap — five workers each sleeping 80ms independently
// can still fire five requests in the same 80ms window. This promise-chain
// gate serializes actual Scryfall requests across every caller so the
// combined rate stays at one request per SCRYFALL_SINGLE_INTERVAL_MS
// regardless of concurrency.
let _apiGate = Promise.resolve();
function scryfallApiSlot() {
  const slot = _apiGate.then(() => new Promise((r) => setTimeout(r, SCRYFALL_SINGLE_INTERVAL_MS)));
  _apiGate = slot.catch(() => {});
  return slot;
}

const ARCHIDEKT_RE = /archidekt\.com\/decks\/(\d+)/i;
const MOXFIELD_RE = /moxfield\.com\/decks\/([A-Za-z0-9_-]{12,})/i;
const TAPPEDOUT_RE = /tappedout\.net\/mtg-decks\/([A-Za-z0-9_-]+)/i;
const EDHREC_RE = /edhrec\.com\/deckpreview\/([A-Za-z0-9_-]+)/i;
const DECKSTATS_RE = /deckstats\.net\/decks\/(\d+)\/(\d+)/i;
const MTGGOLDFISH_RE = /mtggoldfish\.com\/(?:deck|archetype)\/(\d+)/i;
// mtgdecks.net deck URLs end in `-<id>` (5+ digit numeric deck id) under a
// format folder. Capture the path-after-host as one group; the trailing
// `-<id>` anchor avoids matching archetype-listing URLs.
const MTGDECKS_RE =
  /mtgdecks\.net\/([A-Za-z0-9_-]+\/[A-Za-z0-9_.-]+?-\d{4,})/i;
// Scryfall deck UUIDs are the standard 8-4-4-4-12 hex shape; the `@<user>/`
// segment is canonical but optional in the URL.
const SCRYFALL_DECK_RE =
  /scryfall\.com\/(?:@[A-Za-z0-9_-]+\/)?decks\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
const DECKBOX_RE = /deckbox\.org\/sets\/(\d+)/i;
const MOXFIELD_DECK_BOARDS = ["commanders", "mainboard", "companions", "signatureSpells"];

// "1 Card Name" / "4x Lightning Bolt" / "1 Sol Ring (CMM) 343" — same as fill.py.
// Trailing "*F*"/"*E*" markers (foil/etched) are tolerated but discarded.
// Name is `.+?` (lazy) so card names with parens — `B.F.M. (Big Furry Monster)
// (UGL) 28`, `Hazmat Suit (Used)` — parse correctly; the lazy quantifier
// backtracks to the LAST `(SET) CN`-shaped tail at end of line.
const DECKLIST_LINE = /^\s*(?:SB:\s*)?(\d+)\s*[xX]?\s+(.+?)(?:\s+\(([A-Za-z0-9]{2,6})\)(?:\s+([\w★]+))?)?(?:\s+\*\w+\*)*\s*$/;
// `(\d+)` count suffix — Moxfield's format-specific exports tag every section
// as "Deck (99)", "Companion (0)" etc. Without it the unrecognised header
// keeps whatever inExcluded state the prior recognised header set, silently
// dropping the entire mainboard if Companion/Tokens appear first.
//
// Type-grouping headers (`Creatures`, `Lands`, `Spells`, etc.) are also matched
// so they don't fall through to the bare-name fallback as qty=1 cards. They're
// TRANSPARENT — recognised and skipped, but `inExcluded` is left alone.
const SECTION_HEADER = /^\s*(?:\/\/|#|--)?\s*(sideboard|maybeboard|considering|companion|tokens?|cut|extra|deck|main|mainboard|commanders?|creatures?|instants?|sorceries|sorcery|artifacts?|enchantments?|planeswalkers?|lands?|battles?|spells?)(?:\s+\(\d+\))?\s*:?\s*$/i;
// Transparent type-grouping headers — see fill.py:_TYPE_GROUP_HEADERS. Real
// card names like `Land Tax`, `Spell Pierce`, `Creature Guy` aren't affected
// because SECTION_HEADER anchors on the whole trimmed line.
const TYPE_GROUP_HEADERS = new Set([
  "creature", "creatures",
  "instant", "instants",
  "sorcery", "sorceries",
  "artifact", "artifacts",
  "enchantment", "enchantments",
  "planeswalker", "planeswalkers",
  "land", "lands",
  "battle", "battles",
  "spell", "spells",
]);

// DOM
const $ = (id) => document.getElementById(id);
const els = {
  input: $("deck-input"),
  skipSide: $("opt-skip-side"),
  skipBasics: $("opt-skip-basics"),
  go: $("go"),
  cancelBtn: $("cancel-btn"),
  status: $("status"),
  progress: $("progress"),
  result: $("result"),
  resultSummary: $("result-summary"),
  download: $("download-zip"),
  gallery: $("gallery"),
  failures: $("failures"),
  failuresList: $("failures-list"),
  retryBtn: $("retry-failures"),
  addAnotherBtn: $("add-another"),
  costEstimate: $("cost-estimate"),
  costEstimateText: $("cost-estimate-text"),
  shipDest: $("opt-ship-dest"),
  couponBanner: $("coupon-banner"),
  couponCode: $("coupon-code"),
  couponSave: $("coupon-save"),
  couponCopy: $("coupon-copy"),
  deckStats: $("deck-stats"),
  skipsSummary: $("skips-summary"),
  shareLink: $("share-link"),
};

// Set when the user clicks "Add another deck": the next build call merges
// into the existing zip / gallery / failures rather than starting over.
let appendMode = false;

let lastZipBlob = null;
let lastZipName = "deck.zip";

// The deck URL/id string used for the last successful FRESH URL-mode pass —
// null whenever the last pass was an append or came from the decklist tab,
// since a share link can only encode a single deck. Set in run(); read by
// buildShareUrl() and the show/hide check near the end of run().
let lastShareDeckInput = null;

// Set to a fresh AbortController at the start of every run()/retryFailures()
// pass. fetchBlob / fetchJsonScryfall / fetchJson pass its signal to fetch(),
// and withRetry checks/races it directly (see sleepAbortable) — a single
// module-level controller means every in-flight request across the whole
// concurrent build pool shares one on/off switch for Cancel.
let buildAbort = null;

function showCancelButton() {
  els.cancelBtn.hidden = false;
  els.cancelBtn.disabled = false;
}
function hideCancelButton() {
  els.cancelBtn.hidden = true;
}

function detectSource(input) {
  // Returns { source, args: [...] } or null. Mirrors fill.py:detect_source.
  const s = (input || "").trim();
  let m = s.match(ARCHIDEKT_RE);
  if (m) return { source: "archidekt", args: [m[1]] };
  m = s.match(MOXFIELD_RE);
  if (m) return { source: "moxfield", args: [m[1]] };
  m = s.match(SCRYFALL_DECK_RE);
  if (m) return { source: "scryfall", args: [m[1]] };
  m = s.match(DECKBOX_RE);
  if (m) return { source: "deckbox", args: [m[1]] };
  m = s.match(TAPPEDOUT_RE);
  if (m) return { source: "tappedout", args: [m[1]] };
  m = s.match(EDHREC_RE);
  if (m) return { source: "edhrec", args: [m[1]] };
  m = s.match(DECKSTATS_RE);
  if (m) return { source: "deckstats", args: [m[1], m[2]] };
  m = s.match(MTGGOLDFISH_RE);
  if (m) return { source: "mtggoldfish", args: [m[1]] };
  m = s.match(MTGDECKS_RE);
  if (m) return { source: "mtgdecks", args: [m[1]] };
  if (/^\d+$/.test(s)) return { source: "archidekt", args: [s] };
  if (/^[A-Za-z0-9_-]{12,}$/.test(s)) return { source: "moxfield", args: [s] };
  return null;
}

// Match against the canonical basic-land names + snow-covered variants.
// Mirrors fill.py:is_basic_land — kept verbatim so the two sides can't drift.
const BASIC_LAND_NAMES = new Set([
  "plains", "island", "swamp", "mountain", "forest", "wastes",
  "snow-covered plains", "snow-covered island", "snow-covered swamp",
  "snow-covered mountain", "snow-covered forest", "snow-covered wastes",
]);

function isBasicLand(name) {
  return BASIC_LAND_NAMES.has((name || "").trim().toLowerCase());
}

function slug(name) {
  // Strip any leading combination of `.` and `_` so traversal-flavoured
  // input (`..`, `./foo`, `../foo`) can't produce filenames that lean on
  // dot semantics. The slot prefix on the writer side already neutralises
  // real traversal, but rejecting up front is defense in depth.
  return name
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^[._]+/, "")
    .slice(0, 80) || "card";
}

function setStatus(msg, kind) {
  els.status.textContent = msg;
  els.status.style.color =
    kind === "error" ? "var(--danger)" : kind === "ok" ? "var(--ok)" : "var(--muted)";
}

function setProgress(done, total) {
  if (!total) {
    els.progress.hidden = true;
    return;
  }
  els.progress.hidden = false;
  els.progress.max = total;
  els.progress.value = done;
}

function sleepAbortable(ms) {
  // Same as a plain setTimeout-based sleep, but races the wait against
  // buildAbort's signal — used for withRetry's backoff/RateLimitError waits
  // so a Cancel click during a multi-second 429 lockout doesn't have to sit
  // through the whole delay before the pass notices it was cancelled.
  return new Promise((resolve, reject) => {
    const signal = buildAbort?.signal;
    if (signal?.aborted) {
      reject(new DOMException("cancelled", "AbortError"));
      return;
    }
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        reject(new DOMException("cancelled", "AbortError"));
      },
      { once: true },
    );
  });
}

async function withRetry(fn, attempts = 3, baseDelay = 400) {
  // 400ms base, exponential — defending against transient corsproxy /
  // Scryfall hiccups, not against logic bugs. 4xx is final and bypasses retry.
  // 429 throws a RateLimitError carrying the server's Retry-After (or our
  // 32s fallback) and overrides the exponential delay — Scryfall's 30s
  // lockout would eat both of the short retries otherwise.
  // Cancellation: checked before every attempt, and AbortError bypasses
  // retry the same way FatalFetchError does — a cancelled build shouldn't
  // burn its retry budget on a request that's already dead.
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    if (buildAbort?.signal.aborted) throw new DOMException("cancelled", "AbortError");
    try {
      return await fn();
    } catch (e) {
      if (e instanceof FatalFetchError || e.name === "AbortError") throw e;
      lastErr = e;
      if (i === attempts - 1) break;
      const delay =
        e instanceof RateLimitError ? e.delayMs : baseDelay * 2 ** i;
      await sleepAbortable(delay);
    }
  }
  throw lastErr;
}

class FatalFetchError extends Error {}  // 4xx — don't retry through proxy
class RateLimitError extends Error {
  // delayMs: how long withRetry should pause before the next attempt. Sourced
  // from the response's Retry-After header when present, otherwise from
  // SCRYFALL_429_FALLBACK_MS so we land outside the 30s lockout window.
  constructor(message, delayMs) {
    super(message);
    this.delayMs = delayMs;
  }
}

function parseRetryAfter(header) {
  // Retry-After is either delta-seconds or an HTTP-date (RFC 7231 §7.1.3).
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const when = Date.parse(header);
  if (!Number.isNaN(when)) return Math.max(0, when - Date.now());
  return null;
}

async function parseJsonStrict(r) {
  // corsproxy.io can return 200 with an HTML rate-limit page; r.json() then
  // throws "Unexpected token <" which is useless. Validate content-type.
  const ct = r.headers.get("content-type") || "";
  if (!ct.includes("json")) {
    const sample = (await r.text()).slice(0, 120);
    throw new Error(`expected JSON, got ${ct || "no content-type"} — "${sample}"`);
  }
  return r.json();
}

async function fetchJson(url) {
  return withRetry(async () => {
    let direct;
    try {
      direct = await fetch(url, { headers: { Accept: "application/json" }, signal: buildAbort?.signal });
    } catch {
      // Network/CORS failure — direct doesn't even produce a response.
      direct = null;
    }
    if (direct) {
      if (direct.ok) return parseJsonStrict(direct);
      if (direct.status >= 400 && direct.status < 500) {
        throw new FatalFetchError(`${direct.status} ${direct.statusText}`);
      }
      // 5xx → fall through to proxy as a transient retry.
    }
    const r = await fetch(CORS_PROXY(url), { headers: { Accept: "application/json" }, signal: buildAbort?.signal });
    if (!r.ok) {
      // The proxy faithfully forwards upstream status, so 4xx here is also
      // authoritative (deck not found / private). Surface it as fatal so
      // the user sees a clean message instead of "proxy 404".
      if (r.status >= 400 && r.status < 500) {
        throw new FatalFetchError(`${r.status} ${r.statusText}`);
      }
      throw new Error(`proxy ${r.status} ${r.statusText}`);
    }
    return parseJsonStrict(r);
  });
}

const SCRYFALL_IMG_RE =
  /^https:\/\/cards\.scryfall\.io\/(png|large|normal)\/(.+)\.(?:png|jpg)(\?.*)?$/;
const SCRYFALL_FORMATS = ["png", "large", "normal"]; // quality, highest first

function scryfallImageFallbacks(url) {
  // Scryfall's CDN occasionally serves a *cached* 404 (Cloudflare negative
  // cache, ~1yr TTL) for one exact image URL while the same card's other
  // formats are fine — the poisoned entry is keyed by the exact path. The
  // lower-quality formats live at different paths (different cache keys), and
  // are never larger than what was requested, so retrying them sidesteps a
  // poisoned entry.
  const m = SCRYFALL_IMG_RE.exec(url);
  if (!m) return [];
  const fmt = m[1];
  const path = m[2];
  const q = m[3] || "";
  return SCRYFALL_FORMATS.slice(SCRYFALL_FORMATS.indexOf(fmt) + 1).map(
    (f) => `https://cards.scryfall.io/${f}/${path}.${f === "png" ? "png" : "jpg"}${q}`
  );
}

function scryfallProxyFallback(url) {
  // Last resort when EVERY Scryfall format 404s — some edges negatively-cache
  // a 404 for all of a card's formats at once, and no cards.scryfall.io URL
  // variant can escape it. Re-fetch the original image through the
  // images.weserv.nl proxy, which pulls from Scryfall's origin via a different
  // edge (CORS-enabled). Only public card images ever transit it, and only
  // after the direct attempts have all failed.
  if (!/^https:\/\/cards\.scryfall\.io\//.test(url)) return null;
  return `https://images.weserv.nl/?url=${encodeURIComponent(url.replace(/^https:\/\//, ""))}`;
}

async function fetchBlob(url, { noCache = false } = {}) {
  // Scryfall CDN returns CORS * (verified 2026-04), so direct fetch works.
  // A 404 on a Scryfall PNG is usually a negatively-cached CDN miss, not a
  // genuinely missing image — fall back to the JPG variants (different cache
  // keys), then to an image proxy (different edge), before giving up. Non-404
  // errors retry the original via withRetry.
  const proxied = scryfallProxyFallback(url);
  const candidates = [url, ...scryfallImageFallbacks(url), ...(proxied ? [proxied] : [])];
  // Custom-art URLs (Archidekt) are stable, but their bytes change when the
  // user re-uploads. "reload" skips the browser HTTP cache so an edit upstream
  // is picked up on the next build; a query-string buster can't be used because
  // some hosts serve custom art from signed URLs (an extra param → 403).
  // Scryfall images are immutable, so they keep the default cache to make
  // rebuilds fast.
  const init = { signal: buildAbort?.signal, ...(noCache ? { cache: "reload" } : {}) };
  return withRetry(async () => {
    let last = "404 Not Found";
    for (const u of candidates) {
      const r = await fetch(u, init);
      if (r.ok) return r.blob();
      last = `${r.status} ${r.statusText}`;
      if (r.status !== 404) break;
    }
    throw new Error(`image ${last}`);
  });
}

async function fetchJsonScryfall(url) {
  // Scryfall has open CORS — never proxy. Footer promises "Scryfall is fetched
  // directly". The generic fetchJson() falls through to corsproxy.io on 5xx,
  // which would leak the user's full card list to the proxy operator during
  // any Scryfall outage. Retry direct instead; if Scryfall is down, the proxy
  // would just forward the same upstream error anyway.
  return withRetry(async () => {
    const r = await fetch(url, { headers: { Accept: "application/json" }, signal: buildAbort?.signal });
    if (r.ok) return parseJsonStrict(r);
    if (r.status === 429) {
      // RateLimitError makes withRetry wait out Scryfall's 30s lockout
      // instead of burning its retries at 400/800ms inside the window.
      const ra = parseRetryAfter(r.headers.get("Retry-After"));
      throw new RateLimitError(
        `Scryfall 429 Too Many Requests`,
        ra ?? SCRYFALL_429_FALLBACK_MS,
      );
    }
    if (r.status >= 400 && r.status < 500) {
      throw new FatalFetchError(`Scryfall ${r.status} ${r.statusText}`);
    }
    throw new Error(`Scryfall ${r.status} ${r.statusText}`);
  });
}

const SINGLE_PIECE_LAYOUTS = new Set(["split", "flip", "adventure", "aftermath", "fuse"]);

const BACK_PRESETS = {
  default: { path: "assets/default_back.png", label: '— bundled "Playtest Copy" proxy back —' },
  lord_of_the_proxies: { path: "assets/backs/lord_of_the_proxies.jpg", label: "— bundled Lord of the Proxies back —" },
  tcgplaytest: { path: "assets/backs/tcgplaytest.jpg", label: "— bundled TCGPlaytest logo back —" },
  wouldnt_proxy: { path: "assets/backs/wouldnt_proxy.png", label: '— bundled "You Wouldn\'t Proxy" meme back (low-res) —' },
};

function selectedBackPreset() {
  const checked = document.querySelector('input[name="back-preset"]:checked');
  return BACK_PRESETS[checked?.value] ? checked.value : "default";
}

async function loadCustomBackBlob() {
  // Order of preference: uploaded file → pasted URL → picked bundled preset.
  // A failure on the URL path is surfaced loudly to the user — silent
  // fallback to the bundled stock back is the wrong default.
  const fileEl = $("opt-back-file");
  if (fileEl.files && fileEl.files[0]) return fileEl.files[0];
  const urlEl = $("opt-back-url");
  const url = (urlEl.value || "").trim();
  if (url) {
    if (!/^https:\/\//i.test(url)) {
      throw new Error("Custom back URL must start with https://");
    }
    try {
      const r = await fetch(url);
      if (r.ok) return r.blob();
      // Most random hosts won't allow direct CORS; try the proxy before failing.
    } catch {
      // Fall through to proxy attempt.
    }
    const proxied = await fetch(CORS_PROXY(url));
    if (!proxied.ok) {
      throw new Error(`Custom-back URL failed (${proxied.status}). Check the URL or upload the image instead.`);
    }
    return proxied.blob();
  }
  const preset = BACK_PRESETS[selectedBackPreset()];
  const r = await fetch(preset.path);
  if (!r.ok) throw new Error(`${preset.path} missing from /assets`);
  return r.blob();
}

async function makeDefaultBackBlob() {
  // Hand the source bytes through verbatim — tcgplaytest applies bleed on
  // their end after upload, so we don't pre-pad anymore.
  return loadCustomBackBlob();
}

// --- Plain-text decklist support ----------------------------------------

function parseMtgoDek(text) {
  // MTGO `.dek` XML: <Cards CatID="..." Quantity="N" Sideboard="false" Name="..." />
  // We could route this through DOMParser, but CodeQL flags
  // DOMParser.parseFromString as an XSS sink even when we only read
  // attributes. The schema is trivial enough to extract with regex.
  const xmlEntities = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
  // Decode the five XML named entities + numeric refs (decimal AND hex —
  // some third-party .dek exporters emit `&#xC6;` for non-ASCII names like
  // "Æther Vial"; missing this loses the character and breaks Scryfall lookup).
  const decode = (s) =>
    s.replace(/&(?:(amp|lt|gt|quot|apos)|#(\d+)|#x([0-9a-fA-F]+));/g, (_, name, dec, hex) => {
      if (name) return xmlEntities[name];
      if (dec) return String.fromCodePoint(Number(dec));
      return String.fromCodePoint(parseInt(hex, 16));
    });
  const attr = (chunk, key) => {
    const m = chunk.match(new RegExp(`\\b${key}="([^"]*)"`));
    return m ? decode(m[1]) : "";
  };
  if (!/<Cards\b/.test(text)) {
    throw new Error("MTGO .dek had no <Cards> elements — unexpected schema.");
  }
  const out = [];
  for (const m of text.matchAll(/<Cards\b([^>]*?)\/?>/g)) {
    const chunk = m[1];
    if (attr(chunk, "Sideboard").toLowerCase() === "true") continue;
    const qty = Number(attr(chunk, "Quantity") || "0");
    const name = attr(chunk, "Name").trim();
    if (qty > 0 && name) out.push({ qty, name, set: null, cn: null });
  }
  return out;
}

// CSV detection: header row must carry both a 'name' and a 'quantity'
// column. A single comma in `Bosco, Just a Bear` on its own line must NOT
// flip the parser into CSV mode — that's why we require BOTH headers.
const CSV_NAME_HEADERS = new Set(["name", "card name", "card_name"]);
const CSV_QTY_HEADERS = new Set(["quantity", "qty", "count"]);
const CSV_SET_HEADERS = ["set code", "set_code", "set"];
const CSV_CN_HEADERS = ["collector number", "collector_number", "number", "collector"];
const CSV_SECTION_HEADERS = ["section", "board", "type"];
const CSV_EXCLUDED_SECTIONS = new Set(["sideboard", "maybeboard", "considering"]);

function looksLikeCsv(text) {
  const head = text.trimStart();
  const first = head.split(/\r?\n/, 1)[0].trim();
  if (!first.includes(",")) return false;
  const cols = first.split(",").map((c) => c.trim().replace(/^"|"$/g, "").toLowerCase());
  const hasName = cols.some((c) => CSV_NAME_HEADERS.has(c));
  const hasQty = cols.some((c) => CSV_QTY_HEADERS.has(c));
  return hasName && hasQty;
}

// Minimal RFC-4180-ish split. Handles quoted fields with embedded commas
// and `""` escaping. We don't need full CSV semantics — ManaBox / Deckbox
// exports stay within this subset.
function splitCsvLine(line) {
  const out = [];
  let cur = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inQuotes) {
      if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; }
      else if (c === '"') inQuotes = false;
      else cur += c;
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ",") {
      out.push(cur); cur = "";
    } else {
      cur += c;
    }
  }
  out.push(cur);
  return out;
}

function parseCsvDecklist(text, opts = {}) {
  const includeSideboard = opts.includeSideboard === true;
  // "considering" is always off-deck (Moxfield's "thinking about it" pile);
  // sideboard/maybeboard follow the user option.
  const excludedSections = includeSideboard
    ? new Set(["considering"])
    : CSV_EXCLUDED_SECTIONS;
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) return [];
  const headers = splitCsvLine(lines[0]).map((h) => h.trim().toLowerCase());
  const idx = (cands) => {
    for (const c of cands) {
      const i = headers.indexOf(c);
      if (i !== -1) return i;
    }
    return -1;
  };
  const nameIdx = idx([...CSV_NAME_HEADERS]);
  const qtyIdx = idx([...CSV_QTY_HEADERS]);
  if (nameIdx === -1 || qtyIdx === -1) return [];
  const setIdx = idx(CSV_SET_HEADERS);
  const cnIdx = idx(CSV_CN_HEADERS);
  const sectionIdx = idx(CSV_SECTION_HEADERS);

  const out = [];
  for (let i = 1; i < lines.length; i++) {
    const cells = splitCsvLine(lines[i]);
    if (sectionIdx !== -1) {
      const sec = (cells[sectionIdx] || "").trim().toLowerCase();
      if (excludedSections.has(sec)) continue;
    }
    const qty = Number((cells[qtyIdx] || "0").trim());
    const name = (cells[nameIdx] || "").trim();
    if (!Number.isFinite(qty) || qty <= 0 || !name) continue;
    const set = setIdx !== -1 ? ((cells[setIdx] || "").trim().toLowerCase() || null) : null;
    const cn = cnIdx !== -1 ? ((cells[cnIdx] || "").trim() || null) : null;
    out.push({ qty, name, set, cn });
  }
  return out;
}

// Parses a "collection" CSV (ManaBox export, or any CSV with Name +
// Quantity columns) into a Map<lowercased name, {left: n}> — the shared
// mutable counter lets the owned-copies subtraction in run() decrement one
// object regardless of which of a DFC's two names a job matched on. A DFC
// full name ("Front // Back") also gets a second key for just the front
// face, since decklists/exports commonly refer to a DFC by only one side.
function parseCollectionCsv(text) {
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) throw new Error("Collection file is empty.");
  const headers = splitCsvLine(lines[0]).map((h) => h.trim().toLowerCase());
  const idx = (cands) => {
    for (const c of cands) {
      const i = headers.indexOf(c);
      if (i !== -1) return i;
    }
    return -1;
  };
  const nameIdx = idx([...CSV_NAME_HEADERS]);
  const qtyIdx = idx([...CSV_QTY_HEADERS]);
  if (nameIdx === -1) {
    throw new Error("Collection CSV needs a Name column (e.g. a ManaBox export).");
  }
  const owned = new Map();
  for (let i = 1; i < lines.length; i++) {
    const cells = splitCsvLine(lines[i]);
    const name = (cells[nameIdx] || "").trim();
    if (!name) continue;
    const qty = qtyIdx === -1 ? 1 : Number((cells[qtyIdx] || "").trim()) || 1;
    if (!Number.isFinite(qty) || qty <= 0) continue;
    const key = name.toLowerCase();
    const counter = owned.get(key) || { left: 0 };
    counter.left += qty;
    owned.set(key, counter);
    if (key.includes(" // ")) {
      const frontKey = key.split(" // ")[0].trim();
      if (!owned.has(frontKey)) owned.set(frontKey, counter);
    }
  }
  return owned;
}

function parseDecklist(text, opts = {}) {
  const head = text.trimStart();
  if (head.startsWith("<?xml") || head.startsWith("<Deck")) {
    return parseMtgoDek(text);
  }
  if (looksLikeCsv(text)) {
    return parseCsvDecklist(text, opts);
  }
  // includeSideboard flips ONLY sideboard / maybeboard back into the deck.
  // Other off-deck sections (companion, tokens, considering, cut, extra) stay
  // excluded — tokens have their own printing pipeline, and the rest are
  // parking lots that aren't part of the played deck.
  const includeSideboard = opts.includeSideboard === true;
  const MAIN_SECTIONS = ["deck", "main", "mainboard", "commander", "commanders"];
  const out = [];
  let inExcluded = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) continue;
    const sec = line.match(SECTION_HEADER);
    if (sec) {
      const name = sec[1].toLowerCase();
      if (TYPE_GROUP_HEADERS.has(name)) {
        // Transparent type-grouping header — skip the line but leave
        // inExcluded alone so cards under `Creatures` / `Lands` etc. stay
        // in whatever section the prior structural header set.
        continue;
      }
      const isMain = MAIN_SECTIONS.includes(name);
      const isSideOrMaybe = name === "sideboard" || name === "maybeboard";
      inExcluded = !isMain && !(includeSideboard && isSideOrMaybe);
      continue;
    }
    if (line.trim().startsWith("//") || line.trim().startsWith("#")) continue;
    if (inExcluded) continue;
    if (line.trim().startsWith("SB:") && !includeSideboard) continue;
    const m = line.match(DECKLIST_LINE);
    if (m) {
      out.push({
        qty: Number(m[1]),
        name: m[2].trim().replace(/,$/, ""),
        set: (m[3] || "").toLowerCase() || null,
        cn: m[4] || null,
      });
      continue;
    }
    // Bare-name fallback (no leading quantity). Mirrors fill.py:_parse_decklist.
    // Lets users paste raw name-per-line lists (wiki dumps, Scryfall search,
    // set-completion lists) without prefixing each line with "1 ". Lines
    // whose first non-whitespace character is a digit are skipped — a typo
    // like "1Lightning" shouldn't silently become a card named "1Lightning".
    // An "SB:" prefix is stripped so a bare-name sideboard line is included
    // only when sideboard is on.
    let bare = line.trim();
    if (bare.startsWith("SB:")) bare = bare.slice(3).trim();
    if (!bare || /^\d/.test(bare)) continue;
    out.push({ qty: 1, name: bare, set: null, cn: null });
  }
  return out;
}

async function scryfallCollection(identifiers) {
  // POST /cards/collection — bulk lookup, up to 75 identifiers per call.
  // Wrapped in withRetry so transient errors and 429 lockouts back off.
  // Returns { matches, notFound }; not_found echoes the original identifier
  // shape (so callers can map echoed identifiers back to their inputs).
  return withRetry(async () => {
    const r = await fetch(SCRYFALL_COLLECTION, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({ identifiers }),
      // Every other fetch in the build path honours this; without it a
      // stalled Scryfall POST ignores Cancel and hangs the build outright.
      signal: buildAbort?.signal,
    });
    if (r.ok) {
      const json = await r.json();
      return { matches: json.data || [], notFound: json.not_found || [] };
    }
    if (r.status === 429) {
      const ra = parseRetryAfter(r.headers.get("Retry-After"));
      throw new RateLimitError(
        `Scryfall 429 Too Many Requests`,
        ra ?? SCRYFALL_429_FALLBACK_MS,
      );
    }
    if (r.status >= 500) throw new Error(`Scryfall ${r.status} ${r.statusText}`);
    throw new FatalFetchError(`Scryfall ${r.status} ${r.statusText}`);
  });
}

function indexScryfallCards(cards, target) {
  // Map each card's UID by every name it answers to (full name + each
  // card_face name), all lowercased. DFCs typed by either face hit the
  // same UID. Existing entries are not overwritten so the first match wins
  // when two prints share a face name.
  for (const card of cards) {
    if (!card?.id) continue;
    const keys = [];
    if (card.name) keys.push(card.name.toLowerCase());
    for (const face of card.card_faces || []) {
      if (face?.name) keys.push(face.name.toLowerCase());
    }
    for (const key of keys) {
      if (!target.has(key)) target.set(key, card.id);
    }
  }
}

// Key used to compare /cards/collection input identifiers against the response's
// `not_found` echo. `not_found` returns the identifier object verbatim, so a
// string key derived from its shape lets us pair inputs with their result.
function scryfallIdentifierKey(id) {
  if (id.set || id.collector_number) {
    return `s:${(id.set || "").toLowerCase()}/${(id.collector_number || "").toLowerCase()}`;
  }
  if (id.id) return `i:${id.id.toLowerCase()}`;
  if (id.oracle_id) return `o:${id.oracle_id.toLowerCase()}`;
  return `n:${(id.name || "").toLowerCase()}`;
}

function setCnKey(set, cn) {
  return `${(set || "").toLowerCase()}/${(cn || "").toLowerCase()}`;
}

async function scryfallResolveSingleName(name) {
  // Per-card fallback for inputs that /cards/collection rejected. The bulk
  // endpoint is stricter than /cards/named for some valid names — notably
  // `_____` (Unhinged) and joined split/flip names like `Curse of the Fire
  // Penguin // Curse of the Fire Penguin Creature`, both of which exist on
  // Scryfall but the bulk endpoint returns them in `not_found`.
  //
  // Three lookups, in order, each short-circuiting on success:
  //   1. /cards/named?exact   — rescues bulk-endpoint rejections.
  //   2. Trailing-CN split    — for bare inputs like `B.F.M. (Big Furry
  //      Monster) 28`, strip the trailing collector-number token, resolve
  //      the prefix to discover the set, then look up {set}/{cn} so 28 and
  //      29 map to distinct halves of BFM.
  //   3. /cards/named?fuzzy   — last resort. Rescues Un-set variant suffixes
  //      like `Sly Spy A` / `Everythingamajig A` by matching the closest
  //      card; the user loses variant specificity but gets a printable card
  //      instead of a silent failure.
  const tryGet = async (url) => {
    const r = await fetch(url);
    if (r.ok) return await r.json();
    if (r.status === 404) return null;
    if (r.status === 429) {
      const ra = parseRetryAfter(r.headers.get("Retry-After"));
      throw new RateLimitError(`Scryfall 429 Too Many Requests`, ra ?? SCRYFALL_429_FALLBACK_MS);
    }
    if (r.status >= 500) throw new Error(`Scryfall ${r.status} ${r.statusText}`);
    throw new FatalFetchError(`Scryfall ${r.status} ${r.statusText}`);
  };
  return withRetry(async () => {
    const exact = await tryGet(`${SCRYFALL_NAMED}?${new URLSearchParams({ exact: name })}`);
    if (exact?.id) return exact.id;

    const cnSplit = name.match(/^(.+?)\s+(\d+[a-zA-Z]?)\s*$/);
    if (cnSplit) {
      const prefix = cnSplit[1].trim();
      const cn = cnSplit[2].toLowerCase();
      let setHint = null;
      const prefExact = await tryGet(
        `${SCRYFALL_NAMED}?${new URLSearchParams({ exact: prefix })}`,
      );
      if (prefExact?.set) setHint = prefExact.set;
      if (!setHint) {
        const prefFuzzy = await tryGet(
          `${SCRYFALL_NAMED}?${new URLSearchParams({ fuzzy: prefix })}`,
        );
        if (prefFuzzy?.set) setHint = prefFuzzy.set;
      }
      if (setHint) {
        const specific = await tryGet(SCRYFALL_BY_SET(setHint, cn));
        if (specific?.id) return specific.id;
      }
    }

    const fuzzy = await tryGet(`${SCRYFALL_NAMED}?${new URLSearchParams({ fuzzy: name })}`);
    if (fuzzy?.id) return fuzzy.id;

    return null;
  });
}

function indexScryfallByInputs(chunkIdentifiers, chunkParsed, matches, notFound, uidByName, uidBySetCn) {
  // Scryfall's /cards/collection returns `data` in the same order as input
  // identifiers, minus any items echoed in `not_found`. Walk both arrays in
  // lockstep so we can pair each match with the user's *original* typed input
  // and index by it.
  //
  // Two indices are populated:
  //  - uidByName: keyed by the user's typed name. Fixes silent misses when
  //    Scryfall canonicalizes the input differently from how it was typed
  //    (e.g. `Saute` → `Sauté`, unquoted `Ach! Hans, Run!` → quoted form).
  //  - uidBySetCn: keyed by `set/cn`. Lets two parsed entries that share a
  //    name but differ only in collector number (B.F.M. UGL 28 vs UGL 29 —
  //    the two halves of Big Furry Monster) resolve to distinct UIDs.
  const notFoundCount = new Map();
  for (const id of notFound || []) {
    const k = scryfallIdentifierKey(id);
    notFoundCount.set(k, (notFoundCount.get(k) || 0) + 1);
  }
  let mi = 0;
  for (let ci = 0; ci < chunkIdentifiers.length; ci++) {
    const id = chunkIdentifiers[ci];
    const k = scryfallIdentifierKey(id);
    const missCount = notFoundCount.get(k) || 0;
    if (missCount > 0) {
      notFoundCount.set(k, missCount - 1);
      continue;
    }
    const card = matches[mi++];
    if (!card?.id) continue;
    const p = chunkParsed[ci];
    if (id.set && id.collector_number) {
      const key = setCnKey(id.set, id.collector_number);
      if (!uidBySetCn.has(key)) uidBySetCn.set(key, card.id);
    } else if (p?.name) {
      const key = p.name.toLowerCase();
      if (!uidByName.has(key)) uidByName.set(key, card.id);
    }
  }
}

async function buildJobsFromDecklist(text, opts, onProgress) {
  const parsed = parseDecklist(text, { includeSideboard: !(opts && opts.skipSide) });
  if (!parsed.length) throw new Error("Couldn't parse any cards from the decklist.");
  if (parsed.length > MAX_DECKLIST_ENTRIES) {
    throw new Error(
      `Decklist has ${parsed.length} entries — capped at ${MAX_DECKLIST_ENTRIES} ` +
      "to avoid running for hours and OOM-ing the tab. Split into smaller decks."
    );
  }
  // Cap total copies too — a single "2000000 Sol Ring" line is one entry but
  // would OOM the tab in processJob's per-copy loop just the same.
  const totalCopies = parsed.reduce((a, p) => a + p.qty, 0);
  if (totalCopies > MAX_DECKLIST_ENTRIES) {
    throw new Error(
      `Decklist asks for ${totalCopies} total copies — capped at ${MAX_DECKLIST_ENTRIES} ` +
      "to avoid running for hours and OOM-ing the tab. Split into smaller decks."
    );
  }

  // Bulk path: /cards/collection accepts {name} or {set, collector_number}
  // identifiers and returns up to 75 matches per request. For 480 cards
  // that's 7 requests instead of 480, far under Scryfall's 2 req/sec ceiling.
  const uidByLowerName = new Map();
  const uidBySetCn = new Map();
  let processed = 0;
  const tick = (name) => onProgress?.(++processed, parsed.length, name);

  const identifiers = parsed.map((p) =>
    p.set && p.cn ? { set: p.set, collector_number: p.cn } : { name: p.name },
  );
  for (let i = 0; i < identifiers.length; i += SCRYFALL_COLLECTION_BATCH_SIZE) {
    if (i > 0) await new Promise((r) => setTimeout(r, SCRYFALL_NAMED_INTERVAL_MS));
    const chunk = identifiers.slice(i, i + SCRYFALL_COLLECTION_BATCH_SIZE);
    const chunkParsed = parsed.slice(i, i + chunk.length);
    const sample = parsed[i]?.name || "";
    onProgress?.(processed + 1, parsed.length, sample);
    const { matches, notFound } = await scryfallCollection(chunk);
    indexScryfallCards(matches, uidByLowerName);
    indexScryfallByInputs(chunk, chunkParsed, matches, notFound, uidByLowerName, uidBySetCn);
    for (let k = 0; k < chunk.length; k++) tick(parsed[i + k]?.name || sample);
  }

  // Second pass: entries that pinned a set/cn but didn't resolve — retry
  // by bare name. Mirrors the exact-name fallback the old per-card helper
  // ran when (set, cn) returned 404 (typical: a set hint that's wrong for
  // the named card).
  const fallback = parsed.filter(
    (p) =>
      p.set && p.cn
        ? !uidBySetCn.has(setCnKey(p.set, p.cn)) && !uidByLowerName.has(p.name.toLowerCase())
        : false,
  );
  for (let i = 0; i < fallback.length; i += SCRYFALL_COLLECTION_BATCH_SIZE) {
    await new Promise((r) => setTimeout(r, SCRYFALL_NAMED_INTERVAL_MS));
    const chunk = fallback.slice(i, i + SCRYFALL_COLLECTION_BATCH_SIZE);
    const idChunk = chunk.map((p) => ({ name: p.name }));
    const { matches, notFound } = await scryfallCollection(idChunk);
    indexScryfallCards(matches, uidByLowerName);
    indexScryfallByInputs(idChunk, chunk, matches, notFound, uidByLowerName, uidBySetCn);
  }

  // Third pass: per-card /cards/named for items still unresolved after the
  // bulk lookups. /cards/collection rejects some names that /cards/named
  // accepts (e.g. `_____`, `Curse of the Fire Penguin // ...`); also covers
  // trailing-CN bare inputs (`B.F.M. (Big Furry Monster) 28`) and Un-set
  // variant suffixes (`Sly Spy A`). Deduped on lowercased name so a paste
  // with many copies of the same unresolved card hits Scryfall once.
  const stillUnresolvedNames = new Map();
  for (const p of parsed) {
    const key = p.name.toLowerCase();
    if (uidByLowerName.has(key)) continue;
    if (p.set && p.cn && uidBySetCn.has(setCnKey(p.set, p.cn))) continue;
    if (!stillUnresolvedNames.has(key)) stillUnresolvedNames.set(key, p.name);
  }
  let firstSingle = true;
  for (const [key, originalName] of stillUnresolvedNames) {
    if (!firstSingle) await new Promise((r) => setTimeout(r, SCRYFALL_SINGLE_INTERVAL_MS));
    firstSingle = false;
    try {
      const uid = await scryfallResolveSingleName(originalName);
      if (uid) uidByLowerName.set(key, uid);
    } catch (_) {
      // Transient failure — leave unresolved; user retry path still works.
    }
  }

  const jobs = [];
  const unresolved = [];
  for (const p of parsed) {
    // set+cn beats name: lets two parsed entries that share a name but pin
    // different collector numbers (BFM UGL 28 / 29) resolve to distinct UIDs.
    let uid =
      p.set && p.cn ? uidBySetCn.get(setCnKey(p.set, p.cn)) : undefined;
    if (!uid) uid = uidByLowerName.get(p.name.toLowerCase());
    if (uid) jobs.push({ name: p.name, qty: p.qty, uid, customUrl: null });
    else unresolved.push(p.name);
  }
  return { jobs, unresolved };
}

async function fetchEdhrecDecklist(deckHash) {
  // EDHREC's /deckpreview/<hash> embeds the decklist in __NEXT_DATA__ as a
  // list of plain "N Card Name" strings. Pulling the inline blob avoids the
  // rotating-buildId Next.js data endpoint.
  const url = `https://edhrec.com/deckpreview/${deckHash}`;
  let r;
  try { r = await fetch(url); } catch { r = null; }
  if (!r || !r.ok) {
    r = await fetch(CORS_PROXY(url));
    if (!r.ok) {
      if (r.status >= 400 && r.status < 500) {
        throw new FatalFetchError(`${r.status} ${r.statusText}`);
      }
      throw new Error(`EDHREC fetch failed: ${r.status}`);
    }
  }
  const html = await r.text();
  const m = html.match(/<script id="__NEXT_DATA__" type="application\/json">([\s\S]+?)<\/script>/);
  if (!m) {
    throw new Error("EDHREC page didn't include __NEXT_DATA__ — site may have changed shape.");
  }
  let payload;
  try {
    payload = JSON.parse(m[1]);
  } catch (e) {
    throw new Error(`EDHREC __NEXT_DATA__ wasn't valid JSON: ${e.message}`);
  }
  const deck = payload?.props?.pageProps?.data?.deck;
  if (!Array.isArray(deck) || !deck.length) {
    throw new Error("EDHREC payload had no `deck` list — page may be private or deleted.");
  }
  // Schema drift guard: EDHREC ships strings of the form "1 Sol Ring".
  // If they ever switch to objects, `String(x)` silently produces
  // "[object Object]" and the user sees "0 cards" with no clue why.
  if (typeof deck[0] !== "string" || !/^\s*\d+\s+\S/.test(deck[0])) {
    throw new Error("EDHREC deck shape changed — please open an issue.");
  }
  return deck.join("\n");
}

async function fetchProxiedText(url, label) {
  // Try direct first; some sources CORS-allow, most don't. Fall through to
  // corsproxy.io on failure. 4xx anywhere is fatal — the deck is private
  // or the id is wrong; retrying through the proxy won't help.
  let r;
  try {
    r = await fetch(url);
  } catch {
    r = null;
  }
  if (!r || !r.ok) {
    if (r && r.status >= 400 && r.status < 500) {
      throw new FatalFetchError(`${r.status} ${r.statusText}`);
    }
    r = await fetch(CORS_PROXY(url));
    if (!r.ok) {
      if (r.status >= 400 && r.status < 500) {
        throw new FatalFetchError(`${r.status} ${r.statusText}`);
      }
      throw new Error(`${label} fetch failed: ${r.status}`);
    }
  }
  return r.text();
}

async function fetchScryfallDeckText(deckId) {
  // Scryfall's /decks/<uuid>/export/text endpoint serves plain decklist
  // text — the same `N Card Name` shape the parser already handles.
  return fetchProxiedText(SCRYFALL_DECK_EXPORT(deckId), "Scryfall");
}

async function fetchDeckboxText(setId) {
  // Deckbox's `?format=tcg` export ships `N Card Name` lines. Three things
  // can come back as HTML instead:
  //   1. Deckbox login redirect (private set) — the common case
  //   2. corsproxy.io rate-limit page (200 with HTML body) — same hazard
  //      `parseJsonStrict` documents for the JSON path; a public set would
  //      be misreported as private without this branch
  //   3. Some unexpected upstream HTML — generic fallback
  // We sniff a wider window (500 chars) so `<!DOCTYPE html>` plus the
  // service-identifying string both fit before the cutoff.
  const text = await fetchProxiedText(DECKBOX_EXPORT(setId), "Deckbox");
  const head = text.trimStart().slice(0, 500).toLowerCase();
  if (head.includes("<html") || head.includes("<!doctype")) {
    if (head.includes("corsproxy")) {
      throw new Error(
        "corsproxy.io rate-limited the Deckbox request. Wait a minute and try again, " +
        "or copy the decklist into the Paste decklist tab."
      );
    }
    throw new Error(
      `Deckbox set '${setId}' looks private (export returned HTML). ` +
      "Make the set public, or copy the decklist into the Paste decklist tab.",
    );
  }
  return text;
}

// mtgdecks.net deck pages render every card as a `<tr class="cardItem"
// data-required="N" data-card-id="Name">` row, grouped into `<table>`s
// preceded by a `<th class="type X">` heading. The site sets
// `Access-Control-Allow-Origin: *`, so the browser fetches directly. We
// scrape the structured attributes and rebuild a plain-text decklist —
// the existing parseDecklist + buildJobsFromDecklist pipeline does the rest.
const MTGDECKS_TYPE_OR_CARD =
  /<th\b[^>]*class="type\s+([A-Za-z]+)"|<tr\b[^>]*class="cardItem"[^>]*>/gi;
const MTGDECKS_QTY = /data-required="(\d+)"/i;
const MTGDECKS_NAME = /data-card-id="([^"]+)"/i;

function decodeHtmlEntities(s) {
  // The card-id attribute can carry `&amp;` / `&#xC6;` for `Æ`-class names.
  // We reuse the minimal entity decoder from parseMtgoDek above.
  const xmlEntities = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
  return s.replace(/&(?:(amp|lt|gt|quot|apos)|#(\d+)|#x([0-9a-fA-F]+));/g, (_, n, dec, hex) => {
    if (n) return xmlEntities[n];
    if (dec) return String.fromCodePoint(Number(dec));
    return String.fromCodePoint(parseInt(hex, 16));
  });
}

async function fetchMtgdecksText(path) {
  const url = `https://mtgdecks.net/${path.replace(/^\/+/, "")}`;
  let r;
  try {
    r = await fetch(url);
  } catch {
    r = null;
  }
  if (!r || !r.ok) {
    if (r && r.status >= 400 && r.status < 500) {
      throw new FatalFetchError(`${r.status} ${r.statusText}`);
    }
    // Fallback to the corsproxy in case the direct CORS allowance ever flips.
    r = await fetch(CORS_PROXY(url));
    if (!r.ok) {
      if (r.status >= 400 && r.status < 500) {
        throw new FatalFetchError(`${r.status} ${r.statusText}`);
      }
      throw new Error(`mtgdecks.net fetch failed: ${r.status}`);
    }
  }
  const html = await r.text();
  const main = [];
  const side = [];
  let inSideboard = false;
  for (const m of html.matchAll(MTGDECKS_TYPE_OR_CARD)) {
    if (m[1] !== undefined) {
      inSideboard = m[1].toLowerCase() === "sideboard";
      continue;
    }
    const row = m[0];
    const qm = row.match(MTGDECKS_QTY);
    const nm = row.match(MTGDECKS_NAME);
    if (!qm || !nm) continue;
    const qty = Number(qm[1]);
    const name = decodeHtmlEntities(nm[1]).trim();
    if (!Number.isFinite(qty) || qty <= 0 || !name) continue;
    (inSideboard ? side : main).push(`${qty} ${name}`);
  }
  if (!main.length && !side.length) {
    throw new Error(
      "mtgdecks.net page parsed but no cards were found — the layout may have changed."
    );
  }
  let text = main.join("\n");
  if (side.length) text += "\n\nSideboard\n" + side.join("\n");
  return text;
}

async function fetchTappedOutText(slug) {
  // ?fmt=txt returns text/plain. Site CORS-blocks, so go through the proxy.
  const url = TAPPEDOUT_TXT(slug);
  let r;
  try {
    r = await fetch(url);
  } catch {
    r = null;
  }
  if (!r || !r.ok) {
    r = await fetch(CORS_PROXY(url));
    if (!r.ok) {
      if (r.status >= 400 && r.status < 500) {
        throw new FatalFetchError(`${r.status} ${r.statusText}`);
      }
      throw new Error(`TappedOut fetch failed: ${r.status}`);
    }
  }
  return r.text();
}

function buildJobsArchidekt(deck, opts) {
  // Inclusion follows each card's *primary* (first) category against
  // deck.categories[].includedInDeck.
  const excludedPrimary = new Set(
    (deck.categories || [])
      .filter((c) => c.includedInDeck === false)
      .map((c) => c.name)
  );
  const skipSide = !opts || opts.skipSide !== false;
  const jobs = [];
  for (const entry of deck.cards || []) {
    const cats = entry.categories || [];
    const primary = cats[0] || null;
    // Archidekt's `includedInDeck=false` categories are an explicit out-of-deck
    // signal (maybeboard / scratchpad). Always exclude them — the CLI does, and
    // gating on opts.skipSide silently leaked them into browser builds when the
    // user unchecked "Skip sideboard".
    if (primary && excludedPrimary.has(primary)) continue;
    // includedInDeck is NOT enough: Archidekt's built-in "Sideboard" category
    // ships with includedInDeck=true, so it never lands in excludedPrimary.
    // Skip cards whose primary is *named* Sideboard / Maybeboard only when the
    // "Skip Sideboard / Maybeboard" box is checked.
    if (skipSide && primary && /^(sideboard|maybeboard)$/i.test(primary)) continue;
    const card = entry.card || {};
    const oracle = card.oracleCard || {};
    const customUrl =
      card.customImageUrl || entry.customImageUrl || oracle.customImageUrl || null;
    const name = oracle.name || card.displayName || `card-${card.id}`;
    jobs.push({
      name,
      qty: Number(entry.quantity || 1),
      uid: card.uid || null,
      customUrl,
    });
  }
  return jobs;
}

function buildJobsMoxfield(deck) {
  // Moxfield's `cards` is a dict keyed by an internal id. Iteration order
  // isn't stable across responses, so we sort by name for deterministic
  // slot numbering — same convention as the Python fetcher.
  const jobs = [];
  const boards = deck.boards || {};
  for (const boardName of MOXFIELD_DECK_BOARDS) {
    const cards = (boards[boardName] || {}).cards || {};
    const entries = Object.values(cards).sort((a, b) => {
      const an = (a.card || {}).name || "";
      const bn = (b.card || {}).name || "";
      return an.localeCompare(bn);
    });
    for (const entry of entries) {
      const card = entry.card || {};
      jobs.push({
        name: card.name || `moxfield-${card.id}`,
        qty: Number(entry.quantity || 1),
        uid: card.scryfall_id || null,
        customUrl: null,
      });
    }
  }
  return jobs;
}

// Two-tier Scryfall payload cache: in-memory Map for the current build,
// persistent IndexedDB for cross-session reuse. Card metadata on Scryfall
// almost never changes, so a 7-day TTL is safe and saves ~100ms × 100 cards
// on a re-build of the same deck.
const _scryfallCache = new Map();
const SCRYFALL_DB_NAME = "playtestproxy-fill";
const SCRYFALL_STORE = "scryfall-cards";
const SCRYFALL_TTL_MS = 7 * 24 * 60 * 60 * 1000;
let _idbPromise = null;

function openIdb() {
  // Schema is implicit at version 1. Any schema change requires either a
  // version bump with a real onupgradeneeded migration, or a new DB name.
  if (_idbPromise) return _idbPromise;
  _idbPromise = new Promise((resolve) => {
    if (!("indexedDB" in self)) return resolve(null);  // Safari private mode etc.
    const req = indexedDB.open(SCRYFALL_DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(SCRYFALL_STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => resolve(null);  // fall through to network on any DB failure
    req.onblocked = () => resolve(null);
  });
  return _idbPromise;
}

async function idbGet(key) {
  const db = await openIdb();
  if (!db) return null;
  return new Promise((resolve) => {
    const tx = db.transaction(SCRYFALL_STORE, "readonly");
    const req = tx.objectStore(SCRYFALL_STORE).get(key);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => resolve(null);
  });
}

async function idbSet(key, value) {
  const db = await openIdb();
  if (!db) return;
  return new Promise((resolve) => {
    const tx = db.transaction(SCRYFALL_STORE, "readwrite");
    tx.objectStore(SCRYFALL_STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => resolve();  // best-effort, never block the build
    tx.onabort = () => resolve();
  });
}

function scryfallCard(uid) {
  // Store the *Promise* in the in-memory cache, not the resolved data. Two
  // concurrent callers asking for the same UID land on the same in-flight
  // request, so we never double-fetch even under rapid double-clicks.
  // Resolved Promises are cheap to await — same micro-task overhead as a
  // direct value return.
  const inflight = _scryfallCache.get(uid);
  if (inflight) return inflight;

  const promise = (async () => {
    const stored = await idbGet(uid);
    if (stored && Date.now() - stored.fetchedAt < SCRYFALL_TTL_MS) {
      return stored.data;
    }
    // Shared gate — concurrent build workers must pace through one queue,
    // not sleep independently (see scryfallApiSlot() for why).
    await scryfallApiSlot();
    const data = await fetchJsonScryfall(SCRYFALL(uid));
    // Best-effort persist; awaiting is cheap because IDB writes are async-batched.
    idbSet(uid, { data, fetchedAt: Date.now() });
    return data;
  })();

  _scryfallCache.set(uid, promise);
  // If the fetch fails, drop the rejected promise so a retry can try again
  // instead of seeing the cached failure forever.
  promise.catch(() => _scryfallCache.delete(uid));
  return promise;
}

async function _resolveSingle(job, quality = "png") {
  // Returns { front, back } using only this job's own card data — DFC-aware.
  // `quality` picks the Scryfall format: "png" (best) or "large" (~10x smaller).
  const pick = (uris) => uris[quality] || uris.png;
  if (job.customUrl) return { front: job.customUrl, back: null };
  if (!job.uid) throw new Error("no Scryfall UID and no custom image");
  const data = await scryfallCard(job.uid);
  if (data.image_uris) return { front: pick(data.image_uris), back: null };
  const faces = data.card_faces || [];
  if (faces.length && faces.every((f) => f.image_uris)) {
    if (SINGLE_PIECE_LAYOUTS.has(data.layout || "")) {
      return { front: pick(faces[0].image_uris), back: null };
    }
    return { front: pick(faces[0].image_uris), back: pick(faces[1].image_uris) };
  }
  if (faces.length && faces[0].image_uris) {
    return { front: pick(faces[0].image_uris), back: null };
  }
  throw new Error(`no image_uris for ${data.name || job.uid}`);
}

async function resolveUrls(job, quality = "png") {
  // Returns { front, back }. `back` is null unless this is a DFC OR the
  // job carries a `pairBackUid` (set by pairTokens() to print two tokens
  // back-to-back). The paired back is the OTHER token's front face — and
  // some "tokens" (e.g. werewolf transform tokens) are themselves DFCs
  // whose image lives on card_faces[0], not top-level image_uris. Reuse
  // _resolveSingle so we get the same DFC handling as a normal job.
  const own = await _resolveSingle(job, quality);
  if (job.pairBackUid) {
    const other = await _resolveSingle({ uid: job.pairBackUid, customUrl: null }, quality);
    return { front: own.front, back: other.front };
  }
  return own;
}

function pairTokens(tokens) {
  // Pair N tokens into ceil(N/2) cards: token A front, token B back.
  // The last card is unpaired if N is odd — pairBackUid stays null and
  // resolveUrls falls through to the default playtest back. Mirrors
  // fill.py:_pair_tokens.
  const paired = [];
  for (let i = 0; i < tokens.length; i += 2) {
    const a = tokens[i];
    const b = i + 1 < tokens.length ? tokens[i + 1] : null;
    paired.push({
      name: b ? `${a.name} / ${b.name}` : a.name,
      qty: 1,
      uid: a.uid,
      customUrl: null,
      pairBackUid: b ? b.uid : null,
      isToken: true,
    });
  }
  return paired;
}

// Oracle-text token heuristic. Mirrors fill.py:_extract_token_phrases.
// Magic oracle uses a small fixed quantifier vocabulary before "token";
// `g` flag is essential — multiple `create ... token` clauses per card.
const TOKEN_QUANTIFIERS =
  "a|an|x|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|" +
  "thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty";
// `up to N` prefix and `or more` suffix both appear in real oracle text.
// `named X` is captured as a separate group because real cards like
// "create a colorless artifact token named Treasure" hide the actual token
// name AFTER the word `token` — without this group we'd query the
// descriptor and miss every Treasure / Clue / Food the card mints. The
// {0,2} hard cap bounds the name at three words so "named Tuktuk the
// Returned that's a 5/5..." matches just the name and doesn't run the
// capture into the trailing clause.
const TOKEN_PHRASE_RE = new RegExp(
  String.raw`\bcreate[s]?\s+(?:up\s+to\s+)?(?:\d+|${TOKEN_QUANTIFIERS})(?:\s+or\s+more)?\s+(.{1,120}?)\s+token[s]?(?:\s+named\s+(\w[\w'-]*(?:\s+\w[\w'-]*){0,2}))?\b`,
  "gi",
);
const TOKEN_COLOR_WORDS = { white: "w", blue: "u", black: "b", red: "r", green: "g", colorless: "c" };
const TOKEN_FILLER_WORDS = new Set(["creature", "artifact", "enchantment", "and", "or", "tapped", "legendary"]);

function extractTokenPhrases(oracleText) {
  // Returns [{ descriptor, named: string|null }, ...]. `named` is the
  // optional capture from the `... token named X` clause.
  if (!oracleText) return [];
  const out = [];
  for (const m of oracleText.matchAll(TOKEN_PHRASE_RE)) {
    const desc = m[1].trim().replace(/\s+/g, " ");
    if (!desc) continue;
    const named = m[2] ? m[2].trim().replace(/\s+/g, " ") : null;
    out.push({ descriptor: desc, named });
  }
  return out;
}

function oracleTokenPhrases(payload) {
  // DFCs split oracle_text per face; single-faced cards keep it on the root.
  const texts = [];
  if (payload.oracle_text) texts.push(payload.oracle_text);
  for (const face of payload.card_faces || []) {
    if (face.oracle_text) texts.push(face.oracle_text);
  }
  const out = [];
  for (const t of texts) out.push(...extractTokenPhrases(t));
  return out;
}

function tokenPhraseToQuery(phrase, named) {
  // Three-tier priority — see fill.py:_token_phrase_to_query for the
  // canonical version. Returns null when the descriptor strips down to
  // nothing actionable so the caller can skip the search entirely.
  if (named) return `is:token name:"${named.trim().replace(/[.,;:]+$/, "")}"`;
  const p = phrase.trim().replace(/[.,;:]+$/, "");
  const ptMatch = p.match(/\b(\d+)\/(\d+)\b/);
  if (ptMatch) {
    const rest = p.replace(/\b\d+\/\d+\b/, " ");
    const words = rest.match(/[A-Za-z]+/g) || [];
    const colors = [];
    const types = [];
    for (const w of words) {
      const wl = w.toLowerCase();
      if (TOKEN_COLOR_WORDS[wl]) colors.push(TOKEN_COLOR_WORDS[wl]);
      else if (TOKEN_FILLER_WORDS.has(wl)) continue;
      else if (w[0] === w[0].toUpperCase()) types.push(wl);
    }
    const terms = ["is:token", `pt:${ptMatch[1]}/${ptMatch[2]}`];
    if (colors.length) terms.push(`c=${colors.join("")}`);
    for (const t of types) terms.push(`t:${t}`);
    return terms.join(" ");
  }
  // Bare-name path: strip filler/color words before quoting. "tapped
  // Treasure" → "Treasure"; "colorless artifact" → "" (no useful name —
  // signal to caller via null).
  const words = p.match(/[A-Za-z]+/g) || [];
  const keep = words.filter(
    (w) => !TOKEN_FILLER_WORDS.has(w.toLowerCase()) && !(w.toLowerCase() in TOKEN_COLOR_WORDS),
  );
  if (!keep.length) return null;
  return `is:token name:"${keep.join(" ")}"`;
}

async function resolveTokenPhrase(phrase, named) {
  // Returns { uid, error }:
  //   uid set, error null   → cache the hit
  //   uid null, error null  → clean miss (cache it; e.g. 404 / empty result
  //                           / unresolvable copy-of-X)
  //   uid null, error set   → TRANSIENT failure; caller MUST NOT cache,
  //                           and pushes the reason to failures[]. Without
  //                           this distinction a single 5xx on the first
  //                           card poisons the cache for every later card
  //                           minting the same token.
  const query = tokenPhraseToQuery(phrase, named);
  if (query == null) return { uid: null, error: null };
  // /cards/search is a /cards/* endpoint — 2 req/sec, same as collection —
  // so pace at the same 550ms interval (see SCRYFALL_NAMED_INTERVAL_MS).
  await new Promise((r) => setTimeout(r, SCRYFALL_NAMED_INTERVAL_MS));
  const url = `${SCRYFALL_SEARCH}?${new URLSearchParams({
    q: query,
    unique: "cards",
    order: "released",
  })}`;
  let data;
  try {
    // withRetry + RateLimitError: a 429 waits out Scryfall's lockout and
    // retries instead of becoming a terminal per-phrase failure.
    data = await withRetry(async () => {
      const r = await fetch(url);
      if (r.status === 404) return { data: [] };  // genuine miss
      if (r.status === 429) {
        const ra = parseRetryAfter(r.headers.get("Retry-After"));
        throw new RateLimitError(
          `Scryfall 429 Too Many Requests`,
          ra ?? SCRYFALL_429_FALLBACK_MS,
        );
      }
      if (r.status >= 500) throw new Error(`Scryfall search returned ${r.status}`);
      if (!r.ok) throw new FatalFetchError(`Scryfall search returned ${r.status}`);
      return r.json();
    });
  } catch (e) {
    return { uid: null, error: `Scryfall search failed for "${phrase}": ${e.message}` };
  }
  const cards = data.data || [];
  if (!cards.length) return { uid: null, error: null };
  return { uid: cards[0].id, error: null };
}

// Doubler oracle-text fingerprint — mirrors fill.py:_TOKEN_DOUBLER_RE.
// `twice that many` covers Doubling Season / Anointed Procession /
// Parallel Lives / Mondrak / Adrix and Nev / Primal Vigor; the
// `one more / one extra / one additional` variants catch Annie Joins
// Up. Heuristic, not exhaustive.
const TOKEN_DOUBLER_RE =
  /\b(?:twice that many|create[s]? one more|one (?:additional|extra) token|that many plus one)\b/i;

const TOKEN_QTY_STRATEGIES = ["one", "conservative", "standard", "aggressive"];
const TOKEN_QTY_CAPS = { conservative: 4, standard: 8, aggressive: 12 };

function oraclesFromPayload(payload) {
  // Same walk used by oracleTokenPhrases — pulled out so the doubler
  // scan can reuse the same data without a second function call shape.
  const out = [];
  if (payload.oracle_text) out.push(payload.oracle_text);
  for (const face of payload.card_faces || []) {
    if (face.oracle_text) out.push(face.oracle_text);
  }
  return out;
}

async function discoverTokens(jobs, opts = {}) {
  // Walk all_parts on every main-deck card and return one job per unique
  // token. Dedupes by (lowercased name, type_line) so different Scryfall
  // printings of the same token (e.g. "Treasure", "Faerie Rogue") collapse —
  // but legitimately distinct same-named tokens (e.g. 1/1 W flying Spirit
  // vs. Kamigawa colorless Spirit) stay separate.
  //
  // Returns { tokens, failures, minters, doublerCount }:
  //   - tokens: list of token CardJob-shaped objects (qty defaults to 1)
  //   - failures: list of {name, error} per-card-failure objects
  //   - minters: Map<tokenUid, Set<minter card name>> — used by
  //     applyTokenQty to scale the printed token count
  //   - doublerCount: number of deck cards whose oracle_text matches the
  //     token-doubler fingerprint (Doubling Season etc.)
  //
  // With { thorough: true } also regex-scans each card's oracle_text for
  // 'create ... token' phrases and resolves each via Scryfall search —
  // catches tokens missing from all_parts at the cost of one search
  // request per unique descriptor (cached so two cards minting the same
  // token only burn one round-trip). Transient search failures are pushed
  // to `failures` rather than silently caching null, so a single rate-limit
  // can't suppress every later card minting the same token.
  const { thorough = false } = opts;
  const seen = new Map();
  const failures = [];
  const phraseCache = new Map();
  const minters = new Map();
  let doublerCount = 0;
  for (const job of jobs) {
    if (!job.uid) continue;  // custom-art cards skip the Scryfall round-trip
    let data;
    try {
      data = await scryfallCard(job.uid);
    } catch (e) {
      failures.push({ name: job.name, error: e.message });
      continue;
    }
    // Doubler fingerprint check on the same payload — avoids a second
    // pass over the deck just to count Doubling Seasons. Each card
    // contributes at most 1 to the count even if the regex matches twice.
    if (oraclesFromPayload(data).some((t) => TOKEN_DOUBLER_RE.test(t))) {
      doublerCount += 1;
    }
    for (const part of data.all_parts || []) {
      if (part.component !== "token" || !part.id) continue;
      const name = (part.name || "").trim();
      if (!name) continue;  // skip nameless parts rather than collapse them
      const tline = (part.type_line || "").trim().toLowerCase();
      const key = `${name.toLowerCase()}|${tline}`;
      if (!seen.has(key)) {
        seen.set(key, {
          name: `${name} (token)`,
          qty: 1,
          uid: part.id,
          customUrl: null,
          isToken: true,
        });
      }
      if (!minters.has(part.id)) minters.set(part.id, new Set());
      minters.get(part.id).add(job.name);
    }
    if (!thorough) continue;
    // Token UIDs we've already taken — cross-checked alongside the
    // (name, type_line) key so a token resolved via oracle-scan can't
    // duplicate one already collected from all_parts if the type_line
    // strings happen to differ subtly between the two sources.
    const seenUids = new Set(Array.from(seen.values(), (j) => j.uid));
    for (const { descriptor, named } of oracleTokenPhrases(data)) {
      const cacheKey = `${descriptor.toLowerCase()}|${(named || "").toLowerCase()}`;
      if (!phraseCache.has(cacheKey)) {
        const { uid, error } = await resolveTokenPhrase(descriptor, named);
        if (error) {
          // Transient — DON'T cache (let the next card retry) and surface
          // the reason so the user knows the run was incomplete. Bare
          // job.name keeps the failure shape symmetric with the all_parts
          // path so the run() wrapper's "Token discovery: ..." prefix
          // doesn't get a redundant "(thorough)" suffix appended.
          failures.push({ name: job.name, error });
          continue;
        }
        phraseCache.set(cacheKey, uid);
      }
      const tokenUid = phraseCache.get(cacheKey);
      if (!tokenUid) continue;
      // Record this card as a minter even if we won't add a new token job
      // below — minter count still grows for smart-qty.
      if (!minters.has(tokenUid)) minters.set(tokenUid, new Set());
      minters.get(tokenUid).add(job.name);
      if (seenUids.has(tokenUid)) continue;
      let tok;
      try {
        tok = await scryfallCard(tokenUid);
      } catch (e) {
        failures.push({ name: job.name, error: `token ${tokenUid}: ${e.message}` });
        continue;
      }
      const tokName = (tok.name || "").trim();
      const tokType = (tok.type_line || "").trim().toLowerCase();
      if (!tokName) continue;
      const key = `${tokName.toLowerCase()}|${tokType}`;
      // Mark the UID seen unconditionally — even if the (name, type_line)
      // key already exists from all_parts, a later phrase resolving to
      // the same UID shouldn't trigger a redundant payload fetch.
      seenUids.add(tokenUid);
      if (!seen.has(key)) {
        seen.set(key, {
          name: `${tokName} (token)`,
          qty: 1,
          uid: tokenUid,
          customUrl: null,
          isToken: true,
        });
      }
    }
  }
  return { tokens: [...seen.values()], failures, minters, doublerCount };
}

function applyTokenQty(tokens, minters, doublerCount, strategy) {
  // Mirrors fill.py:_apply_token_qty. Mutates `tokens` in place.
  // See the Python docstring for the strategy semantics.
  if (strategy === "one") return;
  const cap = TOKEN_QTY_CAPS[strategy];
  if (!cap) throw new Error(`Unknown token-qty strategy: ${strategy}`);
  for (const job of tokens) {
    const minterSet = minters.get(job.uid);
    const minterCount = minterSet ? minterSet.size : 1;
    let qty;
    if (strategy === "conservative") {
      qty = minterCount;
    } else if (strategy === "standard") {
      qty = minterCount * (doublerCount > 0 ? 2 : 1);
    } else {  // aggressive
      qty = minterCount * 2 ** Math.min(doublerCount, 2);
    }
    job.qty = Math.max(1, Math.min(qty, cap));
  }
}

function expandTokenQty(tokens) {
  // Flatten qty>1 token jobs into qty=1 singles. Pair-tokens treats
  // each entry as one physical card; without expansion a smart-qty=3
  // Treasure would collapse into a single pair slot. Mirrors
  // fill.py:_expand_token_qty.
  const out = [];
  for (const job of tokens) {
    if (job.qty <= 1) {
      out.push(job);
      continue;
    }
    for (let i = 0; i < job.qty; i++) {
      out.push({ ...job, qty: 1 });
    }
  }
  return out;
}

// Cards build several at a time instead of one-at-a-time. This is safe
// because the only shared mutable state processJob touches — the Scryfall
// request pacing and state.slot — is already concurrency-safe: scryfallCard
// routes every request through the single scryfallApiSlot() gate, and
// state.slot is incremented in a synchronous loop with no `await`s, so each
// job's slot range is claimed atomically no matter how many jobs finish out
// of order.
const BUILD_CONCURRENCY = 5;

async function runPool(items, limit, worker) {
  let next = 0;
  const lanes = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const i = next++;
      await worker(items[i], i);
    }
  });
  await Promise.all(lanes);
}

async function processJob(state, job, opts, zip, gallery) {
  // Minimum-price filter: skip cards below the threshold before any image
  // fetch or slot assignment. Tokens are exempt — they aren't purchasable
  // substitutes, so a uid price has no meaningful bearing on them. Custom-
  // image cards are exempt too — they have no Scryfall uid/price at all.
  if (opts.minPrice > 0 && !job.isToken && !job.customUrl && job.uid) {
    const data = await scryfallCard(job.uid);
    const price = parseFloat(data.prices?.usd ?? data.prices?.usd_foil ?? data.prices?.usd_etched);
    if (Number.isFinite(price) && price < opts.minPrice) {
      return { skippedCheap: true, price };
    }
  }
  // state.slot is mutated to assign sequential slot numbers across all jobs
  // so fronts/<NNN>.* and backs/<NNN>.* stay aligned for tcgplaytest's
  // Sequential Backs feature.
  const { front, back } = await resolveUrls(job, opts.imageQuality);

  const frontBlob = await fetchBlob(front, { noCache: !!job.customUrl });
  const backBlob = back ? await fetchBlob(back) : null;

  const slugName = slug(job.name);
  // "large" quality serves Scryfall's JPG rendition — name those entries
  // .jpg so the bytes match the extension. The bundled/custom default back
  // stays .png; mixed extensions in one zip are fine because tcgplaytest
  // pairs sequential backs by order, not by name.
  const ext = opts.imageQuality === "large" ? "jpg" : "png";
  const written = [];
  for (let copy = 1; copy <= job.qty; copy++) {
    state.slot += 1;
    const slotStr = String(state.slot).padStart(3, "0");
    const base = `${slotStr}_${slugName}`;
    if (opts.pairBacks) {
      zip.file(`fronts/${base}.${ext}`, frontBlob);
      written.push(`fronts/${base}.${ext}`);
      // DFC face-2 images follow the selected quality; the default back is
      // always the PNG asset.
      const backName = backBlob ? `backs/${base}.${ext}` : `backs/${base}.png`;
      zip.file(backName, backBlob || state.defaultBack);
      written.push(backName);
    } else {
      // No pairing mode: emit fronts only at root. DFC backs become their
      // own separate cards (next to their fronts, suffixed _back) so the
      // user prints both faces as physical cards.
      zip.file(`${base}.${ext}`, frontBlob);
      written.push(`${base}.${ext}`);
      if (back) {
        const backBase = `${slotStr}_${slug(job.name + "_back")}`;
        zip.file(`${backBase}.${ext}`, backBlob);
        written.push(`${backBase}.${ext}`);
      }
    }
  }
  addThumb(gallery, frontBlob, `${job.name}${job.qty > 1 ? ` ×${job.qty}` : ""}`);
  if (back) {
    addThumb(gallery, backBlob, `${job.name} (back)`);
  }
  return written;
}

// Object URLs backing the gallery thumbnails. blob: URLs pin their blobs
// until explicitly revoked, so a fresh run must revoke these alongside
// clearing the gallery DOM or every past build's images stay in memory.
const galleryObjectUrls = [];

function clearGallery() {
  for (const u of galleryObjectUrls) URL.revokeObjectURL(u);
  galleryObjectUrls.length = 0;
  els.gallery.replaceChildren();
}

function addThumb(gallery, blob, label) {
  const wrap = document.createElement("div");
  wrap.className = "thumb";
  const img = document.createElement("img");
  img.alt = label;
  img.src = URL.createObjectURL(blob);
  galleryObjectUrls.push(img.src);
  const lab = document.createElement("div");
  lab.className = "label";
  lab.textContent = label;
  wrap.appendChild(img);
  wrap.appendChild(lab);
  gallery.appendChild(wrap);
}

// --- Cost estimator -----------------------------------------------------
// Pricing tiers transcribed from https://www.tcgplaytest.com/?view=pricing
// (volume-based per-card cost + flat shipping bands per destination).
// Frozen at the time of writing — if tcgplaytest changes their rates this
// needs an update.

const CARD_PRICE_TIERS = [
  { upTo: 144, perCard: 0.35, label: "Starter" },       // 1–144
  { upTo: 499, perCard: 0.30, label: "Playtest Set" },  // 145–499
  // The pricing page labels the middle tier "145 – 500" while also labelling
  // this one "500+", so 500 is claimed by both. Settled in the buyer's favour.
  { upTo: Infinity, perCard: 0.26, label: "Bulk" },
];

const SHIPPING = {
  us: {
    label: "US",
    bands: [
      { upTo: 100, cost: 6.95 },
      { upTo: 250, cost: 8.95 },
      { upTo: 500, cost: 12.95 },
      { upTo: 1000, cost: 18.95 },
      { upTo: 2000, cost: 29.95 },
      { upTo: Infinity, cost: 49.95 },
    ],
  },
  ca: {
    label: "Canada",
    bands: [
      { upTo: 100, cost: 12.95 },
      { upTo: 250, cost: 16.95 },
      { upTo: 500, cost: 24.95 },
      { upTo: 1000, cost: 34.95 },
      { upTo: 2000, cost: 54.95 },
      { upTo: Infinity, cost: 89.95 },
    ],
  },
  intl: {
    label: "International",
    bands: [
      { upTo: 100, cost: 16.95 },
      { upTo: 250, cost: 24.95 },
      { upTo: 500, cost: 34.95 },
      { upTo: 1000, cost: 54.95 },
      { upTo: 2000, cost: 89.95 },
      { upTo: Infinity, cost: 149.95 },
    ],
  },
};

// tcgplaytest promo code, redeemed on their Stripe checkout page. It comes
// off the card subtotal only — shipping is still charged in full.
const COUPON_CODE = "HUEY";
const COUPON_RATE = 0.10;

function pickTier(tiers, n) {
  return tiers.find((t) => n <= t.upTo);
}

function estimateCost(numCards, dest = "us") {
  const region = SHIPPING[dest] || SHIPPING.us;
  const tier = pickTier(CARD_PRICE_TIERS, numCards);
  const cards = numCards * tier.perCard;
  const discount = cards * COUPON_RATE;
  const shipping = pickTier(region.bands, numCards).cost;
  return {
    numCards,
    cards,
    discount,
    shipping,
    total: cards - discount + shipping,
    tier: tier.label,
    perCard: tier.perCard,
    dest: SHIPPING[dest] ? dest : "us",
    shipLabel: region.label,
  };
}

function fmt(n) {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

// Remembered so the shipping-destination <select> can re-render the estimate
// without rebuilding the deck.
let lastCostCards = 0;

function renderCostEstimate(numCards) {
  const el = els.costEstimate;
  if (!el) return;
  lastCostCards = numCards;
  if (!numCards) { el.hidden = true; els.couponBanner.hidden = true; return; }
  const e = estimateCost(numCards, els.shipDest ? els.shipDest.value : "us");
  // Build via DOM APIs so card-count / pricing data can never become an
  // injection vector even if a future change feeds untrusted input here.
  const text = els.costEstimateText;
  text.replaceChildren();
  // The headline is the post-discount number, so it names the code — without
  // that it reads as the undiscounted price.
  text.append(`Estimated TCGPlaytest cost with code ${COUPON_CODE}: `);
  const total = document.createElement("strong");
  total.textContent = fmt(e.total);
  text.append(total, " ");
  const detail = document.createElement("span");
  detail.className = "small";
  detail.textContent =
    `${e.numCards} cards · ${fmt(e.perCard)}/card (${e.tier} tier) · ` +
    `${fmt(e.cards)} cards − ${fmt(e.discount)} coupon + ` +
    `${fmt(e.shipping)} ${e.shipLabel} shipping. Tax not included.`;
  text.append(detail);
  el.hidden = false;
  els.couponCode.textContent = COUPON_CODE;
  els.couponSave.textContent = `· saves you ${fmt(e.discount)}`;
  els.couponBanner.hidden = false;
}

// --- Deck stats badge ---------------------------------------------------
// Aggregated post-build from the Scryfall payloads we already fetched
// during processJob. Pure read of `_scryfallCache` — no extra network.

const COLOR_ORDER = ["W", "U", "B", "R", "G"];

async function renderDeckStats(jobs) {
  const el = els.deckStats;
  if (!el) return;
  const colors = new Set();
  let cmcSum = 0;
  let cmcCount = 0;
  const types = { Creature: 0, Instant: 0, Sorcery: 0, Artifact: 0, Enchantment: 0, Planeswalker: 0, Land: 0 };
  let identified = 0;
  // Only count jobs that have a Scryfall UID we can look up — Archidekt
  // customs (no UID) aren't deckbuilding signals worth aggregating.
  const eligible = jobs.filter((j) => j.uid).length;
  for (const job of jobs) {
    if (!job.uid) continue;
    const inflight = _scryfallCache.get(job.uid);
    if (!inflight) continue;
    let card;
    try {
      card = await inflight;
    } catch {
      continue;
    }
    if (!card) continue;
    identified += 1;
    for (const c of card.color_identity || []) colors.add(c);
    const tline = (card.type_line || "").split("//")[0];
    const isLand = /\bLand\b/.test(tline);
    if (!isLand && typeof card.cmc === "number") {
      cmcSum += card.cmc * job.qty;
      cmcCount += job.qty;
    }
    for (const k of Object.keys(types)) {
      if (new RegExp(`\\b${k}\\b`).test(tline)) {
        types[k] += job.qty;
        break;
      }
    }
  }
  // Hide the badge if coverage is too thin to be meaningful — anything
  // below 80% of eligible jobs would mislead more than it informs.
  if (eligible === 0 || identified < eligible * 0.8) {
    el.hidden = true;
    return;
  }
  el.replaceChildren();

  const colorWrap = document.createElement("span");
  const colorLabel = document.createElement("span");
  colorLabel.className = "stat-label";
  colorLabel.textContent = "Identity:";
  colorWrap.append(colorLabel);
  const present = COLOR_ORDER.filter((c) => colors.has(c));
  if (!present.length) {
    const pip = document.createElement("span");
    pip.className = "pip C";
    pip.title = "Colorless";
    colorWrap.append(pip);
  } else {
    for (const c of present) {
      const pip = document.createElement("span");
      pip.className = `pip ${c}`;
      pip.title = c;
      colorWrap.append(pip);
    }
  }
  el.append(colorWrap);

  if (cmcCount) {
    const avg = document.createElement("span");
    const lbl = document.createElement("span");
    lbl.className = "stat-label";
    lbl.textContent = "Avg MV:";
    avg.append(lbl, (cmcSum / cmcCount).toFixed(2));
    el.append(avg);
  }

  const parts = Object.entries(types).filter(([, n]) => n > 0);
  for (const [name, n] of parts) {
    const span = document.createElement("span");
    const lbl = document.createElement("span");
    lbl.className = "stat-label";
    lbl.textContent = `${name}:`;
    span.append(lbl, String(n));
    el.append(span);
  }
  if (identified < eligible) {
    const note = document.createElement("span");
    note.className = "stat-label";
    note.textContent = `(based on ${identified}/${eligible} cards)`;
    el.append(note);
  }
  el.hidden = false;
}

// --- Build / retry context ----------------------------------------------
// Holds the running build's state across two boundaries: the retry button
// (re-runs failed image fetches into the same zip) and append mode (a
// second deck's cards land in the same zip with continuous slot numbers
// and merged stats / cost / token-dedup).
const retryCtx = {
  state: null,
  zip: null,
  opts: null,
  jobsLen: 0,
  deckLabel: "",
  jobs: [],
  // Collection-CSV "owned copies" pool (Map<name, {left: n}>, see
  // parseCollectionCsv) and the running skip logs — shared across append
  // passes so a batched multi-deck order only subtracts each owned copy once.
  owned: null,
  skippedOwned: [],
  skippedCheap: [],
};

async function rebuildZipBlob() {
  // Re-throw on failure: the caller writes a "Built N/M cards" success
  // summary right after this, and a stale `lastZipBlob` would mean the
  // user clicks Download and gets the pre-retry ZIP without warning.
  setStatus("Re-zipping...");
  lastZipBlob = await retryCtx.zip.generateAsync({ type: "blob", compression: "DEFLATE" });
}

function writeManifest(zip) {
  // Overwrite manifest.json with the current merged view (zip.file replaces).
  // Reads retryCtx + liveFailures, so callers must commit those first.
  // Called after every build pass AND after a retry pass — otherwise cards
  // recovered by retry stay listed as failures in the downloaded zip.
  zip.file(
    "manifest.json",
    JSON.stringify(
      {
        source: retryCtx.deckLabel,
        unique_cards: retryCtx.jobs.length,
        total_copies: retryCtx.jobs.reduce((a, j) => a + j.qty, 0),
        options: retryCtx.opts,
        failures: liveFailures,
        skipped_owned: retryCtx.skippedOwned,
        skipped_cheap: retryCtx.skippedCheap,
      },
      null,
      2,
    ),
  );
}

function zipImageCount(zip) {
  // zip.files includes JSZip's implicit directory entries (fronts/, backs/)
  // — count only real files, minus manifest.json, so the headline reflects
  // the number of card images.
  return Object.keys(zip.files).filter(
    (k) => !zip.files[k].dir && k !== "manifest.json",
  ).length;
}

function renderFailures(failures) {
  els.failuresList.replaceChildren();
  if (!failures.length) {
    els.failures.hidden = true;
    els.retryBtn.hidden = true;
    return;
  }
  els.failures.hidden = false;
  for (const f of failures) {
    const li = document.createElement("li");
    li.textContent = `${f.name} — ${f.error}`;
    els.failuresList.appendChild(li);
  }
  // Only show retry if at least one failure carries a retryable job (image
  // fetch failures during processJob). Pre-resolution failures and token
  // discovery failures don't.
  const retryable = failures.filter((f) => f.job).length;
  els.retryBtn.hidden = retryable === 0;
  els.retryBtn.textContent = `Retry ${retryable} failed card${retryable === 1 ? "" : "s"}`;
}

// Truncates a name list past ~6 entries so a big collection/price skip
// doesn't turn the summary line into a wall of text.
function truncateNames(names) {
  const shown = names.slice(0, 6);
  const extra = names.length - shown.length;
  return shown.join(", ") + (extra > 0 ? `, +${extra} more` : "");
}

function renderSkipsSummary() {
  const el = els.skipsSummary;
  if (!el) return;
  const owned = retryCtx.skippedOwned;
  const cheap = retryCtx.skippedCheap;
  if (!owned.length && !cheap.length) {
    el.hidden = true;
    return;
  }
  const parts = [];
  if (owned.length) {
    // Merge by name — the same card can be reduced across multiple jobs
    // (e.g. a main-deck copy and a commander copy) or across append passes.
    const byName = new Map();
    for (const o of owned) byName.set(o.name, (byName.get(o.name) || 0) + o.copies);
    const totalCopies = owned.reduce((a, o) => a + o.copies, 0);
    const names = [...byName.entries()].map(([name, copies]) => `${name} ×${copies}`);
    parts.push(`${totalCopies} copies you own (${truncateNames(names)})`);
  }
  if (cheap.length) {
    const totalCopies = cheap.reduce((a, c) => a + c.qty, 0);
    const names = cheap.map((c) => `${c.name} ($${Number.isFinite(c.price) ? c.price.toFixed(2) : "?"})`);
    const threshold = Number(retryCtx.opts?.minPrice) || 0;
    parts.push(`${totalCopies} card${totalCopies === 1 ? "" : "s"} under $${threshold.toFixed(2)} (${truncateNames(names)})`);
  }
  el.textContent = `Skipped ${parts.join(" and ")}.`;
  el.hidden = false;
}

let liveFailures = [];

async function retryFailures() {
  if (!retryCtx.state || !retryCtx.zip) return;
  els.retryBtn.disabled = true;
  buildAbort = new AbortController();
  showCancelButton();
  const remaining = [];
  const retryable = liveFailures.filter((f) => f.job);
  const passthrough = liveFailures.filter((f) => !f.job);
  for (let i = 0; i < retryable.length; i++) {
    const f = retryable[i];
    if (buildAbort.signal.aborted) {
      // Cancelled mid-retry — leave the rest as retryable failures instead
      // of spending a processJob call per item just to hit the same abort.
      remaining.push({ name: f.job.name, error: "cancelled — use Retry failed cards to resume", job: f.job });
      continue;
    }
    setStatus(`Retrying ${i + 1}/${retryable.length}: ${f.job.name}...`);
    // Fresh placeholder per retried card, appended at the gallery end —
    // retries always land in new slots, so there's no deck-order position
    // to preserve the way the initial build's per-job containers do.
    const container = document.createElement("div");
    container.className = "thumb-slot";
    els.gallery.appendChild(container);
    try {
      await processJob(retryCtx.state, f.job, retryCtx.opts, retryCtx.zip, container);
    } catch (e) {
      const cancelled = e.name === "AbortError" || buildAbort.signal.aborted;
      remaining.push({
        name: f.job.name,
        error: cancelled ? "cancelled — use Retry failed cards to resume" : e.message,
        job: f.job,
      });
      container.remove();
    }
  }
  liveFailures = [...passthrough, ...remaining];
  renderFailures(liveFailures);
  // Recovered cards changed the manifest's failure list and grew state.slot
  // — rewrite the manifest before re-zipping and refresh the cost estimate
  // and deck stats so the UI matches the new zip contents.
  writeManifest(retryCtx.zip);
  try {
    await rebuildZipBlob();
  } catch (e) {
    setStatus(`ZIP generation failed: ${e.message}`, "error");
    els.retryBtn.disabled = false;
    hideCancelButton();
    return;
  }
  const goodCount = retryCtx.jobsLen - liveFailures.filter((f) => f.job).length;
  els.resultSummary.textContent = `Built ${goodCount}/${retryCtx.jobsLen} cards (${
    zipImageCount(retryCtx.zip)
  } files in ZIP)`;
  renderCostEstimate(retryCtx.state.slot);
  await renderDeckStats(retryCtx.jobs);
  const wasCancelled = buildAbort.signal.aborted;
  setStatus(
    wasCancelled
      ? `Cancelled — ${remaining.length} still failing. Retry failed cards resumes the rest.`
      : remaining.length
      ? `Retried — ${remaining.length} still failing.`
      : "Retried — all recovered. Re-download the ZIP.",
    wasCancelled ? "" : remaining.length ? "error" : "ok",
  );
  els.retryBtn.disabled = false;
  hideCancelButton();
}

async function loadJobs(opts) {
  // Returns { jobs, deckLabel, unresolved? }. Throws on fatal user input.
  const mode = $("mode-text-pane").hidden ? "url" : "text";

  if (mode === "text") {
    const text = $("decklist-input").value;
    if (!text.trim()) throw new Error("Paste a decklist first.");
    setStatus("Resolving cards via Scryfall...");
    const total = (text.match(/^\s*\d+\s/gm) || []).length;
    setProgress(0, total || 1);
    const { jobs, unresolved } = await buildJobsFromDecklist(text, opts, (i, n, name) => {
      setProgress(i, n);
      setStatus(`Resolving ${i}/${n}: ${name}`);
    });
    return { jobs, deckLabel: "Pasted decklist", unresolved };
  }

  const detected = detectSource(els.input.value);
  if (!detected) {
    throw new Error(
      "Paste an Archidekt, Moxfield, Scryfall, Deckbox, TappedOut, EDHREC, or mtgdecks.net URL/id."
    );
  }
  const { source, args } = detected;

  if (source === "deckstats" || source === "mtggoldfish") {
    // Auto-fallback: flip the UI over to Paste decklist and pre-pop the
    // status with the correct breadcrumb. The user can then paste the
    // text export directly without re-typing the URL.
    const human = source === "deckstats" ? "Deckstats" : "MTGGoldfish";
    setMode("text");
    $("decklist-input").focus();
    throw new Error(
      `${human} sits behind a Cloudflare challenge — switched to Paste decklist. ` +
      "Open the deck on their site, copy the text export (MTGA/MTGO format), " +
      "paste it above, and click Fetch & build."
    );
  }

  if (
    source === "tappedout" ||
    source === "edhrec" ||
    source === "scryfall" ||
    source === "deckbox" ||
    source === "mtgdecks"
  ) {
    const labelMap = {
      tappedout: "TappedOut",
      edhrec: "EDHREC",
      scryfall: "Scryfall",
      deckbox: "Deckbox",
      mtgdecks: "mtgdecks.net",
    };
    const human = labelMap[source];
    setStatus(`Fetching decklist from ${human}...`);
    const fetcherMap = {
      tappedout: fetchTappedOutText,
      edhrec: fetchEdhrecDecklist,
      scryfall: fetchScryfallDeckText,
      deckbox: fetchDeckboxText,
      mtgdecks: fetchMtgdecksText,
    };
    const text = await fetcherMap[source](args[0]);
    setStatus("Resolving cards via Scryfall...");
    const total = (text.match(/^\s*\d+\s/gm) || []).length;
    setProgress(0, total || 1);
    const { jobs, unresolved } = await buildJobsFromDecklist(text, opts, (i, n, name) => {
      setProgress(i, n);
      setStatus(`Resolving ${i}/${n}: ${name}`);
    });
    return { jobs, deckLabel: `${human} · ${args[0]}`, unresolved };
  }

  const sourceLabel = source === "archidekt" ? "Archidekt" : "Moxfield";
  setStatus(`Fetching deck from ${sourceLabel}...`);
  const base = source === "archidekt" ? ARCHIDEKT(args[0]) : MOXFIELD(args[0]);
  // Default to the cached deck (cheap; spares corsproxy + the deck host). Only
  // bypass the cache when the user ticked "Force-refresh" — i.e. they just
  // edited the deck upstream and need the change to land now.
  const url = opts.freshDeck ? cacheBust(base) : base;
  let deck;
  try {
    deck = await fetchJson(url);
  } catch (e) {
    if (e instanceof FatalFetchError) {
      throw new Error(
        `${sourceLabel} returned ${e.message}. The deck doesn't exist, is private, or the URL is wrong.`
      );
    }
    throw new Error(`Failed to fetch deck: ${e.message}`);
  }
  // Cheap shape check before we trust the response. corsproxy.io can MITM
  // these endpoints; an injected payload that doesn't match expectations
  // should fail loudly here instead of feeding garbage downstream.
  if (source === "archidekt" && !Array.isArray(deck.cards)) {
    throw new Error("Archidekt response missing `cards` array — proxy or upstream tampering?");
  }
  if (source === "moxfield" && (!deck.boards || typeof deck.boards !== "object")) {
    throw new Error("Moxfield response missing `boards` object — proxy or upstream tampering?");
  }
  const jobs = source === "archidekt"
    ? buildJobsArchidekt(deck, opts)
    : buildJobsMoxfield(deck);
  return { jobs, deckLabel: deck.name || sourceLabel };
}

async function run() {
  els.go.disabled = true;
  buildAbort = new AbortController();
  showCancelButton();
  const append = appendMode && retryCtx.zip != null;
  // Don't consume `appendMode` yet — if loadJobs throws, the user should
  // still be in append mode so a corrected URL doesn't silently wipe the
  // first deck's progress.

  let zip, state, opts;
  if (append) {
    // Continue the existing build: same zip, slot counter, gallery, opts.
    // Caller picks up where the previous pass stopped, so a 100-card deck
    // followed by a 60-card deck yields slots 001-100 then 101-160.
    zip = retryCtx.zip;
    state = retryCtx.state;
    opts = retryCtx.opts;
  } else {
    els.result.hidden = true;
    els.failures.hidden = true;
    clearGallery();
    els.failuresList.replaceChildren();
    els.retryBtn.hidden = true;
    els.addAnotherBtn.hidden = true;
    els.costEstimate.hidden = true;
    els.couponBanner.hidden = true;
    els.deckStats.hidden = true;
    els.skipsSummary.hidden = true;
    liveFailures = [];
    retryCtx.state = null;
    retryCtx.zip = null;
    retryCtx.jobs = [];
    retryCtx.deckLabel = "";
    retryCtx.owned = null;
    retryCtx.skippedOwned = [];
    retryCtx.skippedCheap = [];
    // SPA: reset the Scryfall payload cache on every fresh run so the Map
    // doesn't grow unbounded across multiple builds in the same tab.
    _scryfallCache.clear();
    opts = {
      skipSide: els.skipSide.checked,
      skipBasics: els.skipBasics.checked,
      pairBacks: $("opt-pair-backs").checked,
      includeTokens: $("opt-tokens").checked,
      pairTokens: $("opt-pair-tokens").checked,
      tokensThorough: $("opt-tokens-thorough").checked,
      tokenQty: $("opt-token-qty").value || "one",
      imageQuality: $("opt-image-quality").value || "png",
      minPrice: parseFloat($("opt-min-price").value) || 0,
      freshDeck: $("opt-fresh-deck").checked,
    };
    zip = new JSZip();
    // A pasted custom-back URL can take seconds to fetch (possibly via the
    // CORS proxy) — say so before the await instead of sitting on a blank
    // status that looks like a hang.
    if (opts.pairBacks) setStatus("Fetching card back image...");
    state = {
      slot: 0,
      defaultBack: opts.pairBacks ? await makeDefaultBackBlob() : null,
    };
  }
  setProgress(0, 0);
  setStatus(append ? "Loading additional deck..." : "Loading deck...");

  let jobs;
  let deckLabel;
  let initialUnresolved = [];
  let newTokenFailures = [];
  // Failures from this pass only — accumulated into liveFailures at the end.
  const passFailures = [];
  try {
    // Collection-CSV parsing happens before any network work, on a fresh
    // pass only — append passes reuse retryCtx.owned as-is so the same
    // owned-copy pool is consumed across the whole batched order rather
    // than once per deck.
    if (!append) {
      const collectionFile = $("opt-collection-file").files?.[0];
      if (collectionFile) {
        const text = await collectionFile.text();
        retryCtx.owned = parseCollectionCsv(text);
      }
    }
    const loaded = await loadJobs(opts);
    jobs = loaded.jobs;
    deckLabel = loaded.deckLabel;
    initialUnresolved = loaded.unresolved || [];
  } catch (e) {
    // Cancelling during deck-fetch/decklist-resolution (before the build
    // pool even starts) has nothing retryable to show — no jobs, no zip —
    // so it's a plain neutral stop rather than the pool's soft-stop path.
    setStatus(e.name === "AbortError" ? "Cancelled." : e.message, e.name === "AbortError" ? "" : "error");
    els.go.disabled = false;
    hideCancelButton();
    return;
  }
  // loadJobs succeeded — now safe to consume the flag and reset the label.
  // Options unlock again: the next build either appends (re-locking on the
  // "Add another deck" click) or starts a fresh batch with the new values.
  // Share links only encode a single deck, so only a fresh URL-mode pass
  // (not append, not the decklist tab) leaves a shareable deck string behind.
  const urlMode = $("mode-text-pane").hidden;
  lastShareDeckInput = !append && urlMode ? els.input.value.trim() : null;
  appendMode = false;
  setOptionsLocked(false);
  els.go.textContent = "Fetch & build";

  // Collection subtraction: BEFORE skip-basics and token discovery, so
  // basics stay subtractable and tokens (added further below) are never
  // treated as owned. Matches by full card name, falling back to just the
  // front face for DFCs — see parseCollectionCsv for the mirrored keying.
  if (retryCtx.owned) {
    let skippedTotal = 0;
    const remaining = [];
    for (const job of jobs) {
      let counter = retryCtx.owned.get(job.name.toLowerCase());
      if (!counter && job.name.includes(" // ")) {
        counter = retryCtx.owned.get(job.name.toLowerCase().split(" // ")[0].trim());
      }
      if (counter && counter.left > 0) {
        const take = Math.min(counter.left, job.qty);
        if (take > 0) {
          counter.left -= take;
          job.qty -= take;
          skippedTotal += take;
          retryCtx.skippedOwned.push({ name: job.name, copies: take });
        }
      }
      if (job.qty > 0) remaining.push(job);
    }
    jobs = remaining;
    if (skippedTotal) deckLabel = `${deckLabel} (− ${skippedTotal} owned)`;
  }

  if (opts.skipBasics) {
    const before = jobs.reduce((a, j) => a + j.qty, 0);
    jobs = jobs.filter((j) => !isBasicLand(j.name));
    const skipped = before - jobs.reduce((a, j) => a + j.qty, 0);
    if (skipped) deckLabel = `${deckLabel} (− ${skipped} basics)`;
  }

  if (opts.includeTokens) {
    try {
      setStatus(
        opts.tokensThorough
          ? "Discovering tokens (thorough scan, slower)..."
          : "Discovering tokens / emblems...",
      );
      // Run discovery against the merged main deck so tokens already
      // included from the first pass don't get printed again on the second.
      const baseJobsForDiscovery = append ? [...retryCtx.jobs, ...jobs] : jobs;
      const { tokens, failures: tokenFailures, minters, doublerCount } = await discoverTokens(
        baseJobsForDiscovery,
        { thorough: opts.tokensThorough },
      );
      // Filter to tokens we haven't already added in a prior pass. Include
      // both `uid` and any `pairBackUid` so a paired-token back from pass 1
      // doesn't get re-emitted as a standalone token in pass 2.
      const existingTokenUids = new Set();
      for (const j of retryCtx.jobs || []) {
        if (!j.isToken) continue;
        if (j.uid) existingTokenUids.add(j.uid);
        if (j.pairBackUid) existingTokenUids.add(j.pairBackUid);
      }
      const fresh = tokens.filter((t) => !existingTokenUids.has(t.uid));
      if (fresh.length) {
        // Smart-qty: scale each token's qty by minter count (and optional
        // doubler multiplier) before pairing. Mutates `fresh` in place.
        applyTokenQty(fresh, minters, doublerCount, opts.tokenQty);
        const totalCopies = fresh.reduce((a, j) => a + j.qty, 0);
        if (opts.pairTokens && !opts.pairBacks) {
          passFailures.push({
            name: "Token pairing skipped",
            error: "Pair tokens needs Pair backs enabled — falling back to single-sided.",
          });
        }
        if (opts.pairTokens && opts.pairBacks) {
          // Expand qty>1 jobs into singles before pairing so each copy gets
          // its own pair slot. Mirrors fill.py:_apply_token_jobs.
          const expanded = expandTokenQty(fresh);
          if (expanded.length >= 2) {
            const paired = pairTokens(expanded);
            jobs = [...jobs, ...paired];
            deckLabel =
              `${deckLabel} (+ ${fresh.length} unique tokens` +
              (totalCopies !== fresh.length ? `, ${totalCopies} copies` : "") +
              ` → ${paired.length} cards)`;
          } else {
            jobs = [...jobs, ...expanded];
            deckLabel = `${deckLabel} (+ ${fresh.length} tokens)`;
          }
        } else {
          jobs = [...jobs, ...fresh];
          deckLabel =
            `${deckLabel} (+ ${fresh.length} unique tokens` +
            (totalCopies !== fresh.length ? `, ${totalCopies} copies` : "") +
            ")";
        }
      }
      newTokenFailures = tokenFailures;
    } catch (e) {
      if (e.name !== "AbortError") throw e;
      // Cancelled during token discovery. No jobs from this pass have been
      // committed to retryCtx yet, and tokens found so far aren't tracked
      // anywhere retryable — known limitation, accepted: a cancelled pass's
      // token discovery isn't resumable via Retry, only its main-deck build
      // is (once it reaches the pool below).
      setStatus("Cancelled.", "");
      els.go.disabled = false;
      hideCancelButton();
      return;
    }
  }

  setStatus(`${deckLabel} — ${jobs.length} unique cards. Building...`);
  setProgress(0, jobs.length);

  // Thumbnails now finish in COMPLETION order under the concurrency pool
  // below, not deck order, so each job gets a placeholder container up
  // front, appended to the gallery in deck order — addThumb (called from
  // inside processJob) fills a container in whenever that job finishes.
  // `.thumb-slot` is `display: contents` (see style.css) so it's an
  // invisible grouping node; its `.thumb` children remain the gallery
  // grid's direct items, keeping the layout identical to a plain append.
  const jobContainers = jobs.map(() => {
    const c = document.createElement("div");
    c.className = "thumb-slot";
    els.gallery.appendChild(c);
    return c;
  });

  // NOTE on slot numbers: state.slot (mutated inside processJob) is now
  // claimed in COMPLETION order rather than deck order, since jobs finish
  // concurrently. That's fine — processJob's per-job slot loop has no
  // `await`s, so each job's slot range is written atomically, keeping
  // fronts/<NNN> and backs/<NNN> paired correctly regardless of which job's
  // numbers land where. tcgplaytest's Sequential Backs feature only needs
  // that per-slot front/back pairing, not deck-order numbering. Cancelling
  // mid-pool can't leave "holes" either: a job either finishes and claims a
  // contiguous slot range, or never claims one at all.
  let done = 0;
  await runPool(jobs, BUILD_CONCURRENCY, async (job, i) => {
    // Stop picking up new work once cancelled. A job already mid-flight is
    // allowed to run to its next network call, which rejects on its own via
    // the aborted signal (see fetchBlob / fetchJsonScryfall / withRetry).
    if (buildAbort.signal.aborted) {
      passFailures.push({ name: job.name, error: "cancelled — use Retry failed cards to resume", job });
      jobContainers[i].remove();
    } else {
      try {
        const result = await processJob(state, job, opts, zip, jobContainers[i]);
        if (result && result.skippedCheap) {
          jobContainers[i].remove();
          retryCtx.skippedCheap.push({ name: job.name, qty: job.qty, price: result.price });
          job.skipped = true;
        }
      } catch (e) {
        const cancelled = e.name === "AbortError" || buildAbort.signal.aborted;
        passFailures.push({
          name: job.name,
          error: cancelled ? "cancelled — use Retry failed cards to resume" : e.message,
          job,
        });
        jobContainers[i].remove();
      }
    }
    done++;
    setProgress(done, jobs.length);
    setStatus(
      `Building... ${done}/${jobs.length} cards${passFailures.length ? ` (${passFailures.length} failed)` : ""}`,
    );
  });

  // Drop price-skipped jobs before merging so deck stats / total_copies /
  // token dedupe reflect only what actually got printed.
  jobs = jobs.filter((j) => !j.skipped);

  // Merge this pass into the running build.
  const allJobs = [...retryCtx.jobs, ...jobs];
  for (const name of initialUnresolved) {
    passFailures.push({ name, error: "could not resolve via Scryfall (check spelling / set code)" });
  }
  for (const f of newTokenFailures) {
    passFailures.push({ name: `Token discovery: ${f.name}`, error: f.error });
  }
  const mergedDeckLabel = append ? `${retryCtx.deckLabel} + ${deckLabel}` : deckLabel;

  // Commit retryCtx and the failure list BEFORE attempting zip gen. The
  // per-card files are already inside `zip` from the build loop above, so
  // retryCtx.jobs needs to reflect them; otherwise a zip-gen failure would
  // leave retryCtx pointing at a zip whose contents the dedupe code
  // doesn't know about. writeManifest reads this committed state.
  liveFailures = [...liveFailures, ...passFailures];
  retryCtx.state = state;
  retryCtx.zip = zip;
  retryCtx.opts = opts;
  retryCtx.jobs = allJobs;
  retryCtx.jobsLen = allJobs.length;
  retryCtx.deckLabel = mergedDeckLabel;

  writeManifest(zip);

  setStatus("Zipping...");
  try {
    lastZipBlob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
  } catch (e) {
    setStatus(`ZIP generation failed: ${e.message}`, "error");
    els.go.disabled = false;
    hideCancelButton();
    return;
  }
  lastZipName = `${slug(mergedDeckLabel)}_tcgplaytest.zip`;

  // Surface non-retryable failures (unresolved-by-name, token discovery)
  // in the headline too — otherwise "Built 100/100" hides the 3 unresolved
  // entries the user can see in the failures box below.
  const builtJobs = allJobs.length - liveFailures.filter((f) => f.job).length;
  const failTail = liveFailures.length
    ? ` — ${liveFailures.length} failure${liveFailures.length === 1 ? "" : "s"} below`
    : "";
  els.resultSummary.textContent =
    `Built ${builtJobs}/${allJobs.length} cards (${zipImageCount(zip)} files in ZIP)${failTail}`;
  els.result.hidden = false;
  els.addAnotherBtn.hidden = false;
  // Share links only encode one deck — hide the button unless this pass
  // left a shareable deck string behind (see the lastShareDeckInput note
  // set right after loadJobs succeeded, above).
  els.shareLink.hidden = !lastShareDeckInput;

  // state.slot is the number of physical cards we wrote — fronts only.
  // tcgplaytest charges per card, not per face, so a card with a custom
  // back still counts once. Non-US shipping varies and isn't surfaced.
  renderCostEstimate(state.slot);
  renderFailures(liveFailures);
  renderSkipsSummary();
  await renderDeckStats(allJobs);

  // Don't paint the success-green "ok" colour when the failures box has
  // entries — that contradicts the failure list right below.
  const hadFailures = liveFailures.length > 0;
  const wasCancelled = buildAbort.signal.aborted;
  if (wasCancelled) {
    // Soft-stop: no "ok" green (it didn't finish) and no "error" red (this
    // isn't a failure) — the partial ZIP already downloads fine and Retry
    // failed cards resumes exactly the cards the pool didn't get to.
    setStatus(`Cancelled — built ${builtJobs}/${allJobs.length} cards. Retry failed cards resumes the rest.`, "");
  } else {
    const message = append
      ? "Added. Click Download ZIP, or add another deck."
      : opts.pairBacks
        ? "Done. Click Download ZIP, then upload it as-is with TCGPlaytest's Upload Deck ZIP button."
        : "Done. Click Download ZIP, extract, and drag the images into TCGPlaytest's Fronts uploader.";
    setStatus(hadFailures ? `${message} (some cards failed — see below)` : message, hadFailures ? "" : "ok");
  }
  els.go.disabled = false;
  hideCancelButton();
}

els.go.addEventListener("click", () => {
  run().catch((e) => {
    setStatus(`Unexpected error: ${e.message}`, "error");
    els.go.disabled = false;
    hideCancelButton();
  });
});

els.retryBtn.addEventListener("click", () => {
  retryFailures().catch((e) => {
    setStatus(`Retry failed: ${e.message}`, "error");
    els.retryBtn.disabled = false;
    hideCancelButton();
  });
});

els.cancelBtn.addEventListener("click", () => {
  // Soft-stop: abort in-flight/queued requests via the shared signal and
  // stop the pool from starting new jobs. Disable immediately so a second
  // click can't fire while the pass is still winding down; run()/
  // retryFailures() hide the button entirely once they've finished.
  buildAbort?.abort();
  els.cancelBtn.disabled = true;
});

// Every option control. Disabled while append mode is armed: "Add to order"
// reuses retryCtx.opts from the first pass, so leaving the checkboxes
// interactive would let the UI silently lie about what the next pass does.
const OPTION_CONTROL_IDS = [
  "opt-skip-side", "opt-skip-basics", "opt-fresh-deck", "opt-pair-backs", "opt-tokens",
  "opt-pair-tokens", "opt-tokens-thorough", "opt-token-qty",
  "opt-image-quality", "opt-back-file", "opt-back-url",
  "opt-min-price", "opt-collection-file", "opt-collection-clear",
  "back-preset-default", "back-preset-lotp", "back-preset-tcg", "back-preset-meme",
];

function setOptionsLocked(locked) {
  for (const id of OPTION_CONTROL_IDS) $(id).disabled = locked;
  $("opts-locked-note").hidden = !locked;
}

els.addAnotherBtn.addEventListener("click", () => {
  // Arm append mode and bring the user back to the input. Build options
  // (pair-backs / tokens / skip-basics) are locked from the first pass —
  // the original `opts` lives on retryCtx and is reused on the next click.
  appendMode = true;
  setOptionsLocked(true);
  els.go.textContent = "Add to order";
  els.input.value = "";
  $("decklist-input").value = "";
  els.input.focus();
  // A share link encodes exactly one deck — once a second deck is queued
  // up, the last build's link no longer describes the whole batch.
  els.shareLink.hidden = true;
  setStatus("Paste another deck URL or decklist, then click Add to order.");
});

els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter") els.go.click();
});

$("opt-back-file").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  $("back-file-name").textContent = f
    ? f.name
    : BACK_PRESETS[selectedBackPreset()].label;
});

for (const radio of document.querySelectorAll('input[name="back-preset"]')) {
  radio.addEventListener("change", () => {
    const fileEl = $("opt-back-file");
    if (!(fileEl.files && fileEl.files[0])) {
      $("back-file-name").textContent = BACK_PRESETS[selectedBackPreset()].label;
    }
  });
}

$("opt-collection-file").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  $("collection-file-name").textContent = f ? f.name : "— none —";
  $("opt-collection-clear").hidden = !f;
});
$("opt-collection-clear").addEventListener("click", () => {
  $("opt-collection-file").value = "";
  $("collection-file-name").textContent = "— none —";
  $("opt-collection-clear").hidden = true;
});

// Token-option dependencies. `opt-tokens-thorough` and `opt-pair-tokens` are
// both refinements of `opt-tokens` — neither has any effect unless the
// parent is on. Without this wiring a user could check "Thorough token
// scan" alone and see no tokens added, with no UI signal explaining why.
// Mirrors the CLI's `--tokens-thorough has no effect without
// --include-tokens` notice.
function bindTokenChild(childId) {
  $(childId).addEventListener("change", (e) => {
    if (e.target.checked) $("opt-tokens").checked = true;
  });
}
bindTokenChild("opt-tokens-thorough");
bindTokenChild("opt-pair-tokens");
$("opt-tokens").addEventListener("change", (e) => {
  if (!e.target.checked) {
    $("opt-tokens-thorough").checked = false;
    $("opt-pair-tokens").checked = false;
    // Reset the qty select to "one" too — it's another no-op-without-parent
    // option, and silently retaining a non-default value across the parent
    // toggle would surprise the user on re-enable.
    $("opt-token-qty").value = "one";
  }
});
// `opt-token-qty` is a select rather than a checkbox; same dependency on
// opt-tokens. Picking any non-default value auto-enables the parent.
$("opt-token-qty").addEventListener("change", (e) => {
  if (e.target.value !== "one") $("opt-tokens").checked = true;
});

els.download.addEventListener("click", () => {
  if (!lastZipBlob) return;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(lastZipBlob);
  a.download = lastZipName;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
  }, 100);
});

els.shipDest.addEventListener("change", () => renderCostEstimate(lastCostCards));

// --- Mode toggle (URL/ID vs paste decklist) -----------------------------

function setMode(mode) {
  const urlActive = mode === "url";
  $("mode-url").classList.toggle("active", urlActive);
  $("mode-text").classList.toggle("active", !urlActive);
  $("mode-url").setAttribute("aria-selected", String(urlActive));
  $("mode-text").setAttribute("aria-selected", String(!urlActive));
  // Roving tabindex — the ARIA tabs pattern keeps exactly one tab in the
  // Tab order; Left/Right arrows (below) move between them.
  $("mode-url").tabIndex = urlActive ? 0 : -1;
  $("mode-text").tabIndex = urlActive ? -1 : 0;
  $("mode-url-pane").hidden = !urlActive;
  $("mode-text-pane").hidden = urlActive;
}
$("mode-url").addEventListener("click", () => setMode("url"));
$("mode-text").addEventListener("click", () => setMode("text"));
// Arrow-key navigation per the ARIA tabs pattern. With only two tabs,
// either arrow selects-and-focuses the other one (wrap-around).
for (const [id, otherMode] of [["mode-url", "text"], ["mode-text", "url"]]) {
  $(id).addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    setMode(otherMode);
    $(`mode-${otherMode}`).focus();
  });
}

// --- Shareable build links -----------------------------------------------
// Query params emitted by buildShareUrl() / read by the load-time parser
// below. Only non-default values are emitted, so a share link stays short.
// File uploads (custom back image, collection CSV) and pasted collection
// CSVs are local-only and can't be encoded — only the pasted custom-back
// URL variant is shareable.
//   deck      - the pasted deck URL/id (URL-mode only)
//   side=0    - "Skip Sideboard/Maybeboard" UNCHECKED (default: checked)
//   basics=1  - "Skip basic lands" CHECKED (default: unchecked)
//   backs=0   - "Pair backs" UNCHECKED (default: checked)
//   tokens=1  - "Include tokens" CHECKED (default: unchecked)
//   pt=0      - "Pair tokens" UNCHECKED (default: checked)
//   th=1      - "Thorough token scan" CHECKED (default: unchecked)
//   tq=<v>    - token quantity strategy, when not "one"
//   q=large   - image quality, when not "png"
//   minprice=<n> - minimum price filter, when > 0
//   backurl=<url> - pasted custom-back URL (not the uploaded-file variant)
function buildShareUrl() {
  const p = new URLSearchParams();
  p.set("deck", lastShareDeckInput);
  if (!els.skipSide.checked) p.set("side", "0");
  if (els.skipBasics.checked) p.set("basics", "1");
  if (!$("opt-pair-backs").checked) p.set("backs", "0");
  if ($("opt-tokens").checked) p.set("tokens", "1");
  if (!$("opt-pair-tokens").checked) p.set("pt", "0");
  if ($("opt-tokens-thorough").checked) p.set("th", "1");
  if ($("opt-token-qty").value !== "one") p.set("tq", $("opt-token-qty").value);
  if ($("opt-image-quality").value !== "png") p.set("q", $("opt-image-quality").value);
  const minPrice = parseFloat($("opt-min-price").value);
  if (minPrice > 0) p.set("minprice", String(minPrice));
  const backUrl = $("opt-back-url").value.trim();
  if (backUrl) p.set("backurl", backUrl);
  const preset = selectedBackPreset();
  if (preset !== "default") p.set("backpreset", preset);
  return `${location.origin}${location.pathname}?${p.toString()}`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard API unavailable/denied — fall back to the classic
    // hidden-textarea + execCommand("copy") trick.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch {
      // Best effort — nothing further we can do here.
    }
    document.body.removeChild(ta);
  }
}

// Copy `text`, then flash confirmation on the button that triggered it.
async function copyAndFlash(btn, text, flashLabel = "Copied!") {
  const restoreText = btn.textContent;
  await copyText(text);
  btn.textContent = flashLabel;
  setTimeout(() => { btn.textContent = restoreText; }, 1500);
}

els.shareLink.addEventListener("click", () =>
  copyAndFlash(els.shareLink, buildShareUrl())
);

els.couponCopy.addEventListener("click", () =>
  copyAndFlash(els.couponCopy, COUPON_CODE)
);

// On load: a `deck` param pre-fills the URL-mode input and switches to that
// tab; recognized option params are applied to their controls, dispatching
// `change` so dependency wiring (bindTokenChild etc.) reacts the same way a
// real click would. Unknown params are ignored. No auto-build — a shared
// link must never cause surprise network traffic.
(() => {
  const params = new URLSearchParams(location.search);
  const deck = params.get("deck");
  if (!deck) return;
  setMode("url");
  els.input.value = deck;
  const setChecked = (id, value) => {
    const el = $(id);
    el.checked = value;
    el.dispatchEvent(new Event("change"));
  };
  if (params.has("side")) setChecked("opt-skip-side", params.get("side") !== "0");
  if (params.has("basics")) setChecked("opt-skip-basics", params.get("basics") === "1");
  if (params.has("backs")) setChecked("opt-pair-backs", params.get("backs") !== "0");
  if (params.has("tokens")) setChecked("opt-tokens", params.get("tokens") === "1");
  if (params.has("pt")) setChecked("opt-pair-tokens", params.get("pt") !== "0");
  if (params.has("th")) setChecked("opt-tokens-thorough", params.get("th") === "1");
  if (params.has("tq")) {
    $("opt-token-qty").value = params.get("tq");
    $("opt-token-qty").dispatchEvent(new Event("change"));
  }
  if (params.has("q")) {
    $("opt-image-quality").value = params.get("q");
    $("opt-image-quality").dispatchEvent(new Event("change"));
  }
  if (params.has("minprice")) $("opt-min-price").value = params.get("minprice");
  if (params.has("backurl")) $("opt-back-url").value = params.get("backurl");
  if (params.has("backpreset") && BACK_PRESETS[params.get("backpreset")]) {
    const radio = document.querySelector(
      `input[name="back-preset"][value="${params.get("backpreset")}"]`
    );
    if (radio) { radio.checked = true; radio.dispatchEvent(new Event("change")); }
  }
  setStatus("Loaded shared build settings — click Fetch & build.");
})();

// --- Version footer ----------------------------------------------------
// version.json is written by the Pages workflow at deploy time.
// committed_at is preferred — it's when the change was merged to main.
// deployed_at is the workflow run time (close but not identical).

(async () => {
  const el = $("version-info");
  if (!el) return;
  try {
    const r = await fetch("version.json", { cache: "no-store" });
    if (!r.ok) throw new Error();
    const v = await r.json();
    const ts = v.committed_at || v.deployed_at;
    const t = new Date(ts).toLocaleString();
    const sha = (v.commit || "").slice(0, 7);
    // Build via DOM APIs so the commit field — written by the Pages
    // workflow but ultimately user-controllable in a malicious-PR scenario
    // — can never inject markup or a javascript: URL into the page.
    el.replaceChildren();
    el.append(`merged ${t}`);
    if (sha && /^[0-9a-f]{7,40}$/.test(v.commit)) {
      el.append(" · ");
      const a = document.createElement("a");
      a.href = `https://github.com/babyhuey/playtestproxy-fill/commit/${encodeURIComponent(v.commit)}`;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = sha;
      el.append(a);
    }
  } catch {
    el.textContent = "dev build";
  }
})();
