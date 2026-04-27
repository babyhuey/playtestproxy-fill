// playtestproxy-fill frontend
// - Fetches an Archidekt deck (via CORS proxy because Archidekt's API is
//   locked to localhost:3000).
// - Resolves each card's image via Scryfall (which has open CORS).
// - Pads with bleed via Canvas.
// - Bundles everything into a ZIP for the user to drop into TCGPlaytest.

const ARCHIDEKT = (id) => `https://archidekt.com/api/decks/${id}/`;
const MOXFIELD = (id) => `https://api2.moxfield.com/v3/decks/all/${id}`;
const TAPPEDOUT_TXT = (slug) => `https://tappedout.net/mtg-decks/${slug}/?fmt=txt`;
const CORS_PROXY = (url) => `https://corsproxy.io/?${encodeURIComponent(url)}`;
const SCRYFALL = (uid) => `https://api.scryfall.com/cards/${uid}`;
const SCRYFALL_NAMED = "https://api.scryfall.com/cards/named";
const SCRYFALL_BY_SET = (set, cn) => `https://api.scryfall.com/cards/${set}/${cn}`;

const ARCHIDEKT_RE = /archidekt\.com\/decks\/(\d+)/i;
const MOXFIELD_RE = /moxfield\.com\/decks\/([A-Za-z0-9_-]{12,})/i;
const TAPPEDOUT_RE = /tappedout\.net\/mtg-decks\/([A-Za-z0-9_-]+)/i;
const DECKSTATS_RE = /deckstats\.net\/decks\/(\d+)\/(\d+)/i;
const MTGGOLDFISH_RE = /mtggoldfish\.com\/(?:deck|archetype)\/(\d+)/i;
const MOXFIELD_DECK_BOARDS = ["commanders", "mainboard", "companions", "signatureSpells"];

// "1 Card Name" / "4x Lightning Bolt" / "1 Sol Ring (CMM) 343" — same as fill.py.
const DECKLIST_LINE = /^\s*(?:SB:\s*)?(\d+)\s*[xX]?\s+(.+?)(?:\s+\(([A-Za-z0-9]{2,6})\)(?:\s+([\w*★]+))?)?\s*$/;
const SECTION_HEADER = /^\s*(?:\/\/|#|--)?\s*(sideboard|maybeboard|considering|companion|tokens?|cut|extra|deck|main|mainboard)\s*:?\s*$/i;

const CARD_W_IN = 2.48;
const CARD_H_IN = 3.46;
const MM_PER_IN = 25.4;

// DOM
const $ = (id) => document.getElementById(id);
const els = {
  input: $("deck-input"),
  bleed: $("opt-bleed"),
  dpi: $("opt-dpi"),
  skipSide: $("opt-skip-side"),
  dfc: $("opt-dfc"),
  go: $("go"),
  status: $("status"),
  progress: $("progress"),
  result: $("result"),
  resultSummary: $("result-summary"),
  download: $("download-zip"),
  gallery: $("gallery"),
  failures: $("failures"),
  failuresList: $("failures-list"),
  costEstimate: $("cost-estimate"),
};

let lastZipBlob = null;
let lastZipName = "deck.zip";

function detectSource(input) {
  // Returns { source, args: [...] } or null. Mirrors fill.py:detect_source.
  const s = (input || "").trim();
  let m = s.match(ARCHIDEKT_RE);
  if (m) return { source: "archidekt", args: [m[1]] };
  m = s.match(MOXFIELD_RE);
  if (m) return { source: "moxfield", args: [m[1]] };
  m = s.match(TAPPEDOUT_RE);
  if (m) return { source: "tappedout", args: [m[1]] };
  m = s.match(DECKSTATS_RE);
  if (m) return { source: "deckstats", args: [m[1], m[2]] };
  m = s.match(MTGGOLDFISH_RE);
  if (m) return { source: "mtggoldfish", args: [m[1]] };
  if (/^\d+$/.test(s)) return { source: "archidekt", args: [s] };
  if (/^[A-Za-z0-9_-]{12,}$/.test(s)) return { source: "moxfield", args: [s] };
  return null;
}

function slug(name) {
  return name
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "")
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

async function withRetry(fn, attempts = 3, baseDelay = 400) {
  // 400ms base, exponential — defending against transient corsproxy /
  // Scryfall hiccups, not against logic bugs. 4xx is final and bypasses retry.
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (e) {
      if (e instanceof FatalFetchError) throw e;
      lastErr = e;
      if (i === attempts - 1) break;
      await new Promise((r) => setTimeout(r, baseDelay * 2 ** i));
    }
  }
  throw lastErr;
}

class FatalFetchError extends Error {}  // 4xx — don't retry through proxy

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
      direct = await fetch(url, { headers: { Accept: "application/json" } });
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
    const r = await fetch(CORS_PROXY(url), { headers: { Accept: "application/json" } });
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

async function fetchBlob(url) {
  return withRetry(async () => {
    // Scryfall CDN returns CORS * (verified 2026-04), so direct fetch works.
    const r = await fetch(url);
    if (!r.ok) throw new Error(`image ${r.status} ${r.statusText}`);
    return r.blob();
  });
}

function blobToImage(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = (e) => {
      URL.revokeObjectURL(url);
      reject(e);
    };
    img.src = url;
  });
}

function padBleed(img, dpi, bleedMm) {
  // Edge-replicate bleed, sampled inset from the visual edge so transparent
  // rounded corners (which flatten to white on canvas) don't leak into the
  // bleed. Mirrors fill.py:pad_bleed.
  const artW = Math.round(CARD_W_IN * dpi);
  const artH = Math.round(CARD_H_IN * dpi);
  const bleed = Math.round((bleedMm / MM_PER_IN) * dpi);
  const W = artW + 2 * bleed;
  const H = artH + 2 * bleed;

  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(img, bleed, bleed, artW, artH);

  if (bleed > 0) {
    // ~2.5% inset is past MTG's rounded corner radius and inside the border.
    const inset = Math.max(8, Math.round(0.025 * Math.min(artW, artH)));

    // Edges — sample 1px slice offset by `inset` along the long axis so we
    // don't pull from the rounded-corner whitespace.
    ctx.drawImage(canvas,
      bleed + inset, bleed + inset, artW - 2 * inset, 1,
      bleed + inset, 0, artW - 2 * inset, bleed);
    ctx.drawImage(canvas,
      bleed + inset, bleed + artH - inset - 1, artW - 2 * inset, 1,
      bleed + inset, bleed + artH, artW - 2 * inset, bleed);
    ctx.drawImage(canvas,
      bleed + inset, bleed + inset, 1, artH - 2 * inset,
      0, bleed + inset, bleed, artH - 2 * inset);
    ctx.drawImage(canvas,
      bleed + artW - inset - 1, bleed + inset, 1, artH - 2 * inset,
      bleed + artW, bleed + inset, bleed, artH - 2 * inset);

    // Corner blocks: sample an inset×inset patch from just inside the art's
    // corner (after the rounded corner, in the visible border) and stretch
    // to fill the corner+inset destination square. Source x/y is in canvas
    // coords, so it must be offset by `bleed` to land inside the art region.
    const cs = inset;
    const sz = bleed + inset;
    const corners = [
      [0, 0,                                 bleed + inset,            bleed + inset],
      [bleed + artW - inset, 0,              bleed + artW - inset - cs, bleed + inset],
      [0, bleed + artH - inset,              bleed + inset,            bleed + artH - inset - cs],
      [bleed + artW - inset, bleed + artH - inset,
                                             bleed + artW - inset - cs, bleed + artH - inset - cs],
    ];
    for (const [dx, dy, sx, sy] of corners) {
      ctx.drawImage(canvas, sx, sy, cs, cs, dx, dy, sz, sz);
    }
  }

  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

const SINGLE_PIECE_LAYOUTS = new Set(["split", "flip", "adventure", "aftermath", "fuse"]);

async function loadCustomBackBlob() {
  // Order of preference: uploaded file → pasted URL → bundled default.
  // A failure on the URL path is surfaced loudly to the user — silent
  // fallback to the bundled meme back is the wrong default.
  const fileEl = $("opt-back-file");
  if (fileEl.files && fileEl.files[0]) return fileEl.files[0];
  const urlEl = $("opt-back-url");
  const url = (urlEl.value || "").trim();
  if (url) {
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
  const r = await fetch("assets/default_back.png");
  if (!r.ok) throw new Error("default_back.png missing from /assets");
  return r.blob();
}

async function makeDefaultBackBlob(dpi, bleedMm) {
  // Resize the source to card art dims and pad with bleed so it lines up
  // with the rest of the deck.
  const blob = await loadCustomBackBlob();
  const img = await blobToImage(blob);
  return padBleed(img, dpi, bleedMm);
}

// --- Plain-text decklist support ----------------------------------------

function parseDecklist(text) {
  const out = [];
  let inExcluded = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) continue;
    const sec = line.match(SECTION_HEADER);
    if (sec) {
      const name = sec[1].toLowerCase();
      inExcluded = !["deck", "main", "mainboard"].includes(name);
      continue;
    }
    if (line.trim().startsWith("//") || line.trim().startsWith("#")) continue;
    if (inExcluded || line.trim().startsWith("SB:")) continue;
    const m = line.match(DECKLIST_LINE);
    if (!m) continue;
    out.push({
      qty: Number(m[1]),
      name: m[2].trim().replace(/,$/, ""),
      set: (m[3] || "").toLowerCase() || null,
      cn: m[4] || null,
    });
  }
  return out;
}

async function scryfallLookupNamed(name, setCode, cn) {
  // Scryfall has open CORS, so direct fetch is fine.
  if (setCode && cn) {
    const r = await fetch(SCRYFALL_BY_SET(setCode, cn));
    if (r.ok) return (await r.json()).id;
  }
  const params = new URLSearchParams({ exact: name });
  if (setCode) params.set("set", setCode);
  let r = await fetch(`${SCRYFALL_NAMED}?${params}`);
  if (r.ok) return (await r.json()).id;
  if (r.status === 404 && setCode) {
    r = await fetch(`${SCRYFALL_NAMED}?exact=${encodeURIComponent(name)}`);
    if (r.ok) return (await r.json()).id;
  }
  return null;
}

async function buildJobsFromDecklist(text, onProgress) {
  const parsed = parseDecklist(text);
  if (!parsed.length) throw new Error("Couldn't parse any cards from the decklist.");
  const jobs = [];
  const unresolved = [];
  for (let i = 0; i < parsed.length; i++) {
    const p = parsed[i];
    onProgress?.(i + 1, parsed.length, p.name);
    // 80ms politeness gap — Scryfall asks for 50–100ms between requests.
    await new Promise((r) => setTimeout(r, 80));
    const uid = await scryfallLookupNamed(p.name, p.set, p.cn);
    if (!uid) {
      unresolved.push(p.name);
      continue;
    }
    jobs.push({ name: p.name, qty: p.qty, uid, customUrl: null });
  }
  return { jobs, unresolved };
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
  const jobs = [];
  for (const entry of deck.cards || []) {
    const cats = entry.categories || [];
    const primary = cats[0] || null;
    if (opts.skipSide && primary && excludedPrimary.has(primary)) continue;
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
    await new Promise((r) => setTimeout(r, 80));  // Scryfall rate-limit politeness
    const data = await fetchJson(SCRYFALL(uid));
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

async function _resolveSingle(job) {
  // Returns { front, back } using only this job's own card data — DFC-aware.
  if (job.customUrl) return { front: job.customUrl, back: null };
  if (!job.uid) throw new Error("no Scryfall UID and no custom image");
  const data = await scryfallCard(job.uid);
  if (data.image_uris) return { front: data.image_uris.png, back: null };
  const faces = data.card_faces || [];
  if (faces.length && faces.every((f) => f.image_uris)) {
    if (SINGLE_PIECE_LAYOUTS.has(data.layout || "")) {
      return { front: faces[0].image_uris.png, back: null };
    }
    return { front: faces[0].image_uris.png, back: faces[1].image_uris.png };
  }
  if (faces.length && faces[0].image_uris) {
    return { front: faces[0].image_uris.png, back: null };
  }
  throw new Error(`no image_uris for ${data.name || job.uid}`);
}

async function resolveUrls(job) {
  // Returns { front, back }. `back` is null unless this is a DFC OR the
  // job carries a `pairBackUid` (set by pairTokens() to print two tokens
  // back-to-back). Tokens are always single-faced, so the paired back is
  // just that other card's front face.
  const own = await _resolveSingle(job);
  if (job.pairBackUid) {
    const other = await scryfallCard(job.pairBackUid);
    const back = other.image_uris ? other.image_uris.png : null;
    return { front: own.front, back };
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
    });
  }
  return paired;
}

async function discoverTokens(jobs) {
  // Walk all_parts on every main-deck card, dedupe by Scryfall UID, and
  // return one token job per unique token. One physical token per design
  // is enough — copies don't add information.
  const seen = new Map();  // token UID → job
  let failed = 0;
  for (const job of jobs) {
    if (!job.uid) continue;  // custom-art cards skip the Scryfall round-trip
    let data;
    try {
      data = await scryfallCard(job.uid);
    } catch {
      failed += 1;
      continue;
    }
    for (const part of data.all_parts || []) {
      if (part.component !== "token" || !part.id) continue;
      if (!seen.has(part.id)) {
        seen.set(part.id, {
          name: `${part.name || "Token"} (token)`,
          qty: 1,
          uid: part.id,
          customUrl: null,
        });
      }
    }
  }
  return { tokens: [...seen.values()], failed };
}

async function processJob(state, job, opts, zip, gallery) {
  // state.slot is mutated to assign sequential slot numbers across all jobs
  // so fronts/<NNN>.png and backs/<NNN>.png stay aligned for tcgplaytest's
  // Sequential Backs feature.
  const { front, back } = await resolveUrls(job);

  const frontBlob = await fetchBlob(front);
  const frontImg = await blobToImage(frontBlob);
  const frontPadded = opts.bleed > 0 ? await padBleed(frontImg, opts.dpi, opts.bleed) : frontBlob;

  let backPadded = null;
  if (back) {
    const backBlob = await fetchBlob(back);
    const backImg = await blobToImage(backBlob);
    backPadded = opts.bleed > 0 ? await padBleed(backImg, opts.dpi, opts.bleed) : backBlob;
  }

  const slugName = slug(job.name);
  const written = [];
  for (let copy = 1; copy <= job.qty; copy++) {
    state.slot += 1;
    const slotStr = String(state.slot).padStart(3, "0");
    const base = `${slotStr}_${slugName}`;
    if (opts.pairBacks) {
      zip.file(`fronts/${base}.png`, frontPadded);
      written.push(`fronts/${base}.png`);
      const useBack = backPadded || state.defaultBack;
      zip.file(`backs/${base}.png`, useBack);
      written.push(`backs/${base}.png`);
    } else {
      // No pairing mode: emit fronts only at root. DFC backs become their
      // own separate cards (next to their fronts, suffixed _back) so the
      // user prints both faces as physical cards.
      zip.file(`${base}.png`, frontPadded);
      written.push(`${base}.png`);
      if (back) {
        const backBase = `${slotStr}_${slug(job.name + "_back")}`;
        zip.file(`${backBase}.png`, backPadded);
        written.push(`${backBase}.png`);
      }
    }
  }
  addThumb(gallery, frontPadded, `${job.name}${job.qty > 1 ? ` ×${job.qty}` : ""}`);
  if (back) {
    addThumb(gallery, backPadded, `${job.name} (back)`);
  }
  return written;
}

function addThumb(gallery, blob, label) {
  const wrap = document.createElement("div");
  wrap.className = "thumb";
  const img = document.createElement("img");
  img.alt = label;
  img.src = URL.createObjectURL(blob);
  const lab = document.createElement("div");
  lab.className = "label";
  lab.textContent = label;
  wrap.appendChild(img);
  wrap.appendChild(lab);
  gallery.appendChild(wrap);
}

// --- Cost estimator -----------------------------------------------------
// Pricing tiers transcribed from https://www.tcgplaytest.com/?view=pricing
// (volume-based per-card cost + flat US-shipping bands). Frozen at the
// time of writing — if tcgplaytest changes their rates this needs an update.

const CARD_PRICE_TIERS = [
  { upTo: 144, perCard: 0.35, label: "Starter" },       // 1–144
  { upTo: 499, perCard: 0.30, label: "Playtest Set" },  // 145–499
  { upTo: Infinity, perCard: 0.26, label: "Bulk" },     // 500+ (inclusive per pricing page)
];

const SHIPPING_US = [
  { upTo: 100, cost: 6.95 },
  { upTo: 250, cost: 8.95 },
  { upTo: 500, cost: 12.95 },
  { upTo: 1000, cost: 18.95 },
  { upTo: 2000, cost: 29.95 },
  { upTo: Infinity, cost: 49.95 },
];

function pickTier(tiers, n) {
  return tiers.find((t) => n <= t.upTo);
}

function estimateCost(numCards) {
  const tier = pickTier(CARD_PRICE_TIERS, numCards);
  const cards = numCards * tier.perCard;
  const shipping = pickTier(SHIPPING_US, numCards).cost;
  return { numCards, cards, shipping, total: cards + shipping, tier: tier.label, perCard: tier.perCard };
}

function fmt(n) {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function renderCostEstimate(numCards) {
  const el = els.costEstimate;
  if (!el) return;
  if (!numCards) { el.hidden = true; return; }
  const e = estimateCost(numCards);
  el.innerHTML =
    `Estimated TCGPlaytest cost: <strong>${fmt(e.total)}</strong> ` +
    `<span class="small">${e.numCards} cards · ${fmt(e.perCard)}/card (${e.tier} tier) · ${fmt(e.cards)} cards + ${fmt(e.shipping)} US shipping. ` +
    `Tax + non-US shipping not included.</span>`;
  el.hidden = false;
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
    const { jobs, unresolved } = await buildJobsFromDecklist(text, (i, n, name) => {
      setProgress(i, n);
      setStatus(`Resolving ${i}/${n}: ${name}`);
    });
    return { jobs, deckLabel: "Pasted decklist", unresolved };
  }

  const detected = detectSource(els.input.value);
  if (!detected) {
    throw new Error("Paste an Archidekt, Moxfield, or TappedOut URL/id.");
  }
  const { source, args } = detected;

  if (source === "deckstats" || source === "mtggoldfish") {
    throw new Error(
      `${source === "deckstats" ? "Deckstats" : "MTGGoldfish"} blocks programmatic ` +
      "fetches behind a Cloudflare challenge. Switch to “Paste decklist” and " +
      "paste the deck text from the site's export."
    );
  }

  if (source === "tappedout") {
    setStatus("Fetching decklist from TappedOut...");
    const text = await fetchTappedOutText(args[0]);
    setStatus("Resolving cards via Scryfall...");
    const total = (text.match(/^\s*\d+\s/gm) || []).length;
    setProgress(0, total || 1);
    const { jobs, unresolved } = await buildJobsFromDecklist(text, (i, n, name) => {
      setProgress(i, n);
      setStatus(`Resolving ${i}/${n}: ${name}`);
    });
    return { jobs, deckLabel: `TappedOut · ${args[0]}`, unresolved };
  }

  const sourceLabel = source === "archidekt" ? "Archidekt" : "Moxfield";
  setStatus(`Fetching deck from ${sourceLabel}...`);
  const url = source === "archidekt" ? ARCHIDEKT(args[0]) : MOXFIELD(args[0]);
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
  const jobs = source === "archidekt"
    ? buildJobsArchidekt(deck, opts)
    : buildJobsMoxfield(deck);
  return { jobs, deckLabel: deck.name || sourceLabel };
}

async function run() {
  els.go.disabled = true;
  els.result.hidden = true;
  els.failures.hidden = true;
  els.gallery.innerHTML = "";
  els.failuresList.innerHTML = "";
  els.costEstimate.hidden = true;
  // SPA: reset the Scryfall payload cache on every fresh run so the Map
  // doesn't grow unbounded across multiple builds in the same tab.
  _scryfallCache.clear();
  setProgress(0, 0);
  setStatus("Loading deck...");

  const opts = {
    bleed: parseFloat(els.bleed.value) || 0,
    dpi: parseInt(els.dpi.value, 10) || 300,
    skipSide: els.skipSide.checked,
    pairBacks: $("opt-pair-backs").checked,
    includeTokens: $("opt-tokens").checked,
    pairTokens: $("opt-pair-tokens").checked,
  };

  let jobs;
  let deckLabel;
  let initialUnresolved = [];
  let tokenDiscoveryFailed = 0;
  const failures = [];
  try {
    const loaded = await loadJobs(opts);
    jobs = loaded.jobs;
    deckLabel = loaded.deckLabel;
    initialUnresolved = loaded.unresolved || [];
  } catch (e) {
    setStatus(e.message, "error");
    els.go.disabled = false;
    return;
  }

  if (opts.includeTokens) {
    setStatus("Discovering tokens / emblems...");
    const { tokens, failed } = await discoverTokens(jobs);
    if (tokens.length) {
      // Pairing requires pair-backs because Sequential Backs is the only
      // mechanism we have to assign per-card backs in tcgplaytest. If the
      // user asked for pairing without pair-backs, surface a soft warning
      // in the failures box so they know why the cost didn't drop.
      if (opts.pairTokens && !opts.pairBacks) {
        failures.push({
          name: "Token pairing skipped",
          error: "Pair tokens needs Pair backs enabled — falling back to single-sided.",
        });
      }
      if (opts.pairTokens && opts.pairBacks && tokens.length >= 2) {
        const paired = pairTokens(tokens);
        jobs = [...jobs, ...paired];
        deckLabel = `${deckLabel} (+ ${tokens.length} tokens → ${paired.length} cards)`;
      } else {
        jobs = [...jobs, ...tokens];
        deckLabel = `${deckLabel} (+ ${tokens.length} tokens)`;
      }
    }
    if (failed) {
      // One aggregated row instead of N opaque "[token discovery]" entries.
      tokenDiscoveryFailed = failed;
    }
  }

  const totalCopies = jobs.reduce((a, j) => a + j.qty, 0);
  setStatus(`${deckLabel} — ${jobs.length} unique cards, ${totalCopies} copies. Building...`);
  setProgress(0, jobs.length);

  const zip = new JSZip();
  let done = 0;

  // Pre-generate the default back placeholder if pairing is on.
  const state = {
    slot: 0,
    defaultBack: opts.pairBacks ? await makeDefaultBackBlob(opts.dpi, opts.bleed) : null,
  };

  // Process sequentially to be polite to Scryfall and to keep memory low.
  for (let i = 0; i < jobs.length; i++) {
    try {
      await processJob(state, jobs[i], opts, zip, els.gallery);
    } catch (e) {
      failures.push({ name: jobs[i].name, error: e.message });
    }
    done++;
    setProgress(done, jobs.length);
    setStatus(
      `Building... ${done}/${jobs.length} cards${failures.length ? ` (${failures.length} failed)` : ""}`
    );
  }

  // Add manifest.
  zip.file(
    "manifest.json",
    JSON.stringify(
      {
        source: deckLabel,
        unique_cards: jobs.length,
        total_copies: totalCopies,
        options: opts,
        failures,
      },
      null,
      2
    )
  );

  setStatus("Zipping...");
  try {
    lastZipBlob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
  } catch (e) {
    setStatus(`ZIP generation failed: ${e.message}`, "error");
    els.go.disabled = false;
    return;
  }
  lastZipName = `${slug(deckLabel)}_tcgplaytest.zip`;

  const goodCount = jobs.length - failures.length;
  els.resultSummary.textContent = `Built ${goodCount}/${jobs.length} cards (${
    Object.keys(zip.files).length - 1
  } files in ZIP)`;
  els.result.hidden = false;

  // state.slot is the number of physical cards we wrote — fronts only.
  // tcgplaytest charges per card, not per face, so a card with a custom
  // back still counts once. Non-US shipping varies and isn't surfaced.
  renderCostEstimate(state.slot);

  for (const name of initialUnresolved) {
    failures.push({ name, error: "could not resolve via Scryfall (check spelling / set code)" });
  }
  if (tokenDiscoveryFailed) {
    failures.push({
      name: `Token discovery skipped on ${tokenDiscoveryFailed} card${tokenDiscoveryFailed === 1 ? "" : "s"}`,
      error: "Scryfall lookup failed; non-fatal — main deck still built.",
    });
  }
  if (failures.length) {
    els.failures.hidden = false;
    for (const f of failures) {
      const li = document.createElement("li");
      li.textContent = `${f.name} — ${f.error}`;
      els.failuresList.appendChild(li);
    }
  }

  setStatus("Done. Click Download ZIP, then drag-drop into TCGPlaytest.", "ok");
  els.go.disabled = false;
}

els.go.addEventListener("click", () => {
  run().catch((e) => {
    setStatus(`Unexpected error: ${e.message}`, "error");
    els.go.disabled = false;
  });
});

els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter") els.go.click();
});

$("opt-back-file").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  $("back-file-name").textContent = f
    ? f.name
    : '— bundled "You Wouldn\'t Proxy a Magic Card" —';
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

// --- Mode toggle (URL/ID vs paste decklist) -----------------------------

function setMode(mode) {
  const urlActive = mode === "url";
  $("mode-url").classList.toggle("active", urlActive);
  $("mode-text").classList.toggle("active", !urlActive);
  $("mode-url").setAttribute("aria-selected", String(urlActive));
  $("mode-text").setAttribute("aria-selected", String(!urlActive));
  $("mode-url-pane").hidden = !urlActive;
  $("mode-text-pane").hidden = urlActive;
}
$("mode-url").addEventListener("click", () => setMode("url"));
$("mode-text").addEventListener("click", () => setMode("text"));

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
    el.innerHTML = sha
      ? `merged ${t} · <a href="https://github.com/babyhuey/playtestproxy-fill/commit/${v.commit}" target="_blank" rel="noopener">${sha}</a>`
      : `merged ${t}`;
  } catch {
    el.textContent = "dev build";
  }
})();
