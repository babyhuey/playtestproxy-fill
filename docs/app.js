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

async function fetchJson(url) {
  // Try direct first; if it CORS-fails, try the proxy.
  try {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (r.ok) return r.json();
    if (r.status >= 400 && r.status < 500) {
      // Genuine error, no point proxying.
      throw new Error(`${r.status} ${r.statusText}`);
    }
    throw new Error(`status ${r.status}`);
  } catch {
    const r = await fetch(CORS_PROXY(url), { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`proxy ${r.status} ${r.statusText}`);
    return r.json();
  }
}

async function fetchBlob(url) {
  // Scryfall CDN has CORS *, so we can fetch images directly.
  const r = await fetch(url);
  if (!r.ok) throw new Error(`image ${r.status} ${r.statusText}`);
  return r.blob();
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
  const artW = Math.round(CARD_W_IN * dpi);
  const artH = Math.round(CARD_H_IN * dpi);
  const bleed = Math.round((bleedMm / MM_PER_IN) * dpi);
  const W = artW + 2 * bleed;
  const H = artH + 2 * bleed;

  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");
  // Center art.
  ctx.drawImage(img, bleed, bleed, artW, artH);

  if (bleed > 0) {
    // Top edge
    ctx.drawImage(canvas, bleed, bleed, artW, 1, bleed, 0, artW, bleed);
    // Bottom edge
    ctx.drawImage(canvas, bleed, bleed + artH - 1, artW, 1, bleed, bleed + artH, artW, bleed);
    // Left edge
    ctx.drawImage(canvas, bleed, bleed, 1, artH, 0, bleed, bleed, artH);
    // Right edge
    ctx.drawImage(canvas, bleed + artW - 1, bleed, 1, artH, bleed + artW, bleed, bleed, artH);
    // Corners (sample the corner pixel of the art)
    ctx.drawImage(canvas, bleed, bleed, 1, 1, 0, 0, bleed, bleed);
    ctx.drawImage(canvas, bleed + artW - 1, bleed, 1, 1, bleed + artW, 0, bleed, bleed);
    ctx.drawImage(canvas, bleed, bleed + artH - 1, 1, 1, 0, bleed + artH, bleed, bleed);
    ctx.drawImage(
      canvas, bleed + artW - 1, bleed + artH - 1, 1, 1,
      bleed + artW, bleed + artH, bleed, bleed
    );
  }

  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

const SINGLE_PIECE_LAYOUTS = new Set(["split", "flip", "adventure", "aftermath", "fuse"]);

function buildJobs(deck, opts) {
  const jobs = [];
  for (const entry of deck.cards || []) {
    const cats = entry.categories || [];
    if (opts.skipSide && (cats.includes("Sideboard") || cats.includes("Maybeboard"))) continue;
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

async function resolveFaces(job, opts) {
  if (job.customUrl) return [["", job.customUrl]];
  if (!job.uid) throw new Error("no Scryfall UID and no custom image");
  // Polite rate-limit: Scryfall asks 50-100ms.
  await new Promise((r) => setTimeout(r, 80));
  const data = await fetchJson(SCRYFALL(job.uid));
  if (data.image_uris) return [["", data.image_uris.png]];
  const faces = data.card_faces || [];
  if (faces.length && faces.every((f) => f.image_uris)) {
    if (SINGLE_PIECE_LAYOUTS.has(data.layout || "") || !opts.dfc) {
      return [["", faces[0].image_uris.png]];
    }
    return [
      [slug(faces[0].name || "front"), faces[0].image_uris.png],
      [slug(faces[1].name || "back"), faces[1].image_uris.png],
    ];
  }
  if (faces.length && faces[0].image_uris) return [["", faces[0].image_uris.png]];
  throw new Error(`no image_uris for ${data.name || job.uid}`);
}

async function processJob(idx, job, opts, zip, gallery) {
  const faces = await resolveFaces(job, opts);
  const base = `${String(idx + 1).padStart(3, "0")}_${slug(job.name)}`;
  const written = [];
  for (const [faceLabel, url] of faces) {
    const blob = await fetchBlob(url);
    const img = await blobToImage(blob);
    const padded = opts.bleed > 0 ? await padBleed(img, opts.dpi, opts.bleed) : blob;
    const facePart = faceLabel ? `_${faceLabel}` : "";
    for (let copy = 1; copy <= job.qty; copy++) {
      const copyPart = job.qty > 1 ? `_c${copy}` : "";
      const fname = `${base}${facePart}${copyPart}.png`;
      zip.file(fname, padded);
      written.push(fname);
    }
    // Add one thumb per face (not per copy) to keep gallery quick.
    addThumb(gallery, padded, `${job.name}${faceLabel ? " — " + faceLabel : ""}${job.qty > 1 ? ` ×${job.qty}` : ""}`);
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
    setStatus(`Failed to fetch deck: ${e.message}`, "error");
    els.go.disabled = false;
    return;
  }

  const opts = {
    bleed: parseFloat(els.bleed.value) || 0,
    dpi: parseInt(els.dpi.value, 10) || 300,
    skipSide: els.skipSide.checked,
    dfc: els.dfc.checked,
  };
  const jobs = buildJobs(deck, opts);
  const totalCopies = jobs.reduce((a, j) => a + j.qty, 0);
  setStatus(`Deck "${deck.name}" — ${jobs.length} unique cards, ${totalCopies} copies. Building...`);
  setProgress(0, jobs.length);

  const zip = new JSZip();
  const failures = [];
  let done = 0;

  // Process sequentially to be polite to Scryfall and to keep memory low.
  for (let i = 0; i < jobs.length; i++) {
    try {
      await processJob(i, jobs[i], opts, zip, els.gallery);
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
