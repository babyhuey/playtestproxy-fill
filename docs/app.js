// playtestproxy-fill frontend
// - Fetches an Archidekt deck (via CORS proxy because Archidekt's API is
//   locked to localhost:3000).
// - Resolves each card's image via Scryfall (which has open CORS).
// - Pads with bleed via Canvas.
// - Bundles everything into a ZIP for the user to drop into TCGPlaytest.

const ARCHIDEKT = (id) => `https://archidekt.com/api/decks/${id}/`;
const CORS_PROXY = (url) => `https://corsproxy.io/?${encodeURIComponent(url)}`;
const SCRYFALL = (uid) => `https://api.scryfall.com/cards/${uid}`;

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
};

let lastZipBlob = null;
let lastZipName = "deck.zip";

function parseDeckId(s) {
  const trimmed = s.trim();
  const m = trimmed.match(/archidekt\.com\/decks\/(\d+)/i);
  if (m) return m[1];
  if (/^\d+$/.test(trimmed)) return trimmed;
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

function buildJobs(deck, opts) {
  // Archidekt defines deck inclusion via the primary (first) category of each
  // card. The deck-level `categories` array tells us which categories have
  // includedInDeck=false (e.g. Maybeboard, Sideboard, Cut). A card tagged
  // ["Land", "Maybeboard"] is still in the deck because primary = "Land".
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

async function resolveUrls(job) {
  // Returns { front, back }. `back` is null unless this is a DFC.
  if (job.customUrl) return { front: job.customUrl, back: null };
  if (!job.uid) throw new Error("no Scryfall UID and no custom image");
  await new Promise((r) => setTimeout(r, 80));
  const data = await fetchJson(SCRYFALL(job.uid));
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

async function run() {
  const id = parseDeckId(els.input.value);
  if (!id) {
    setStatus("Please paste an Archidekt deck URL or numeric id.", "error");
    return;
  }

  els.go.disabled = true;
  els.result.hidden = true;
  els.failures.hidden = true;
  els.gallery.innerHTML = "";
  els.failuresList.innerHTML = "";
  setProgress(0, 0);
  setStatus("Fetching deck from Archidekt...");

  let deck;
  try {
    deck = await fetchJson(ARCHIDEKT(id));
  } catch (e) {
    // 4xx is authoritative — the deck doesn't exist or is private. Don't
    // bury the user under generic "fetch failed" wording.
    if (e instanceof FatalFetchError) {
      setStatus(
        `Archidekt returned ${e.message}. The deck doesn't exist, is private, or the URL is wrong.`,
        "error"
      );
    } else {
      setStatus(`Failed to fetch deck: ${e.message}`, "error");
    }
    els.go.disabled = false;
    return;
  }

  const opts = {
    bleed: parseFloat(els.bleed.value) || 0,
    dpi: parseInt(els.dpi.value, 10) || 300,
    skipSide: els.skipSide.checked,
    pairBacks: $("opt-pair-backs").checked,
  };
  const jobs = buildJobs(deck, opts);
  const totalCopies = jobs.reduce((a, j) => a + j.qty, 0);
  setStatus(`Deck "${deck.name}" — ${jobs.length} unique cards, ${totalCopies} copies. Building...`);
  setProgress(0, jobs.length);

  const zip = new JSZip();
  const failures = [];
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
        deck_id: id,
        deck_name: deck.name,
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
  lastZipName = `${slug(deck.name || `deck_${id}`)}_tcgplaytest.zip`;

  const goodCount = jobs.length - failures.length;
  els.resultSummary.textContent = `Built ${goodCount}/${jobs.length} cards (${
    Object.keys(zip.files).length - 1
  } files in ZIP)`;
  els.result.hidden = false;

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
