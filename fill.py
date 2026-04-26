"""Archidekt → TCGPlaytest image folder.

Usage:
    python fill.py <archidekt_deck_id> [-o out_dir] [--dpi 300] [--no-bleed]

Pipeline:
  1. Fetch deck JSON from Archidekt.
  2. For each card: resolve image URL (override > custom > Scryfall).
  3. Download.
  4. Pad with 2mm bleed (edge-replicated, sampled inset from rounded corners).
     The upload step picks tcgplaytest's "3mm Bleed added" option — the closest
     match — and the site silently trims the 1mm difference.
  5. Write one image per copy. With --pair-backs the output splits into
     out/fronts/ and out/backs/ with matching slot numbers, ready for
     tcgplaytest's Sequential Backs uploader.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageOps  # noqa: F401  (ImageOps reserved for future use)

ARCHIDEKT_DECK = "https://archidekt.com/api/decks/{}/"
MOXFIELD_DECK = "https://api2.moxfield.com/v3/decks/all/{}"
SCRYFALL_CARD = "https://api.scryfall.com/cards/{}"

CARD_W_IN, CARD_H_IN = 2.48, 3.46  # MTG card art area target before bleed
BLEED_MM = 2.0  # tcgplaytest's printing spec is 2mm of bleed
MM_PER_IN = 25.4

UA = {"User-Agent": "playtestproxy-fill/0.1 (+local tool)"}


@dataclass
class CardJob:
    name: str
    qty: int
    scryfall_uid: Optional[str]
    custom_image_url: Optional[str]
    set_code: Optional[str]
    collector_number: Optional[str]


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return s[:80] or "card"


# Source detection ---------------------------------------------------------
# Each source: regex(es) that match its deck URL, plus a fetcher that takes
# the bare id and returns CardJob list. The dispatcher picks the first match;
# a pure-numeric id falls through to Archidekt for backwards compat.

_ARCHIDEKT_RE = re.compile(r"archidekt\.com/decks/(\d+)", re.I)
_MOXFIELD_RE = re.compile(r"moxfield\.com/decks/([A-Za-z0-9_-]{12,})", re.I)


def detect_source(input_str: str) -> tuple[str, str]:
    """Return (source_name, deck_id) for a deck URL or bare id.

    Recognised:
      - Archidekt URL -> ("archidekt", "<numeric>")
      - Moxfield URL  -> ("moxfield",  "<public-id>")
      - Numeric id    -> ("archidekt", id)   (legacy CLI usage)
      - Alphanumeric  -> ("moxfield",  id)
    """
    s = input_str.strip()
    m = _ARCHIDEKT_RE.search(s)
    if m:
        return "archidekt", m.group(1)
    m = _MOXFIELD_RE.search(s)
    if m:
        return "moxfield", m.group(1)
    if s.isdigit():
        return "archidekt", s
    if re.fullmatch(r"[A-Za-z0-9_-]{12,}", s):
        return "moxfield", s
    raise SystemExit(
        f"Could not recognise '{input_str}' as an Archidekt or Moxfield deck. "
        "Paste the full URL or the deck id."
    )


def fetch_deck(input_str: str) -> list[CardJob]:
    source, deck_id = detect_source(input_str)
    if source == "archidekt":
        return _fetch_archidekt(deck_id)
    if source == "moxfield":
        return _fetch_moxfield(deck_id)
    raise SystemExit(f"Unknown source: {source}")


def _fetch_archidekt(deck_id: str) -> list[CardJob]:
    r = requests.get(ARCHIDEKT_DECK.format(deck_id), headers=UA, timeout=30)
    if 400 <= r.status_code < 500:
        # Authoritative — deck doesn't exist or is private.
        raise SystemExit(
            f"Archidekt returned {r.status_code} for deck {deck_id}. "
            "Check that the deck exists and is public."
        )
    r.raise_for_status()
    data = r.json()
    # Archidekt determines whether a card counts toward the deck via its
    # *primary* (first) category. The deck-level `categories` array maps
    # category name → includedInDeck. A card with primary "Teenage Mutant Ninja
    # Turtles" but also tagged "Maybeboard" is in the deck; only cards whose
    # primary category is excluded are skipped.
    excluded_primary = {
        c["name"]
        for c in (data.get("categories") or [])
        if c.get("includedInDeck") is False
    }
    jobs: list[CardJob] = []
    for entry in data.get("cards", []):
        cats = entry.get("categories") or []
        primary = cats[0] if cats else None
        if primary in excluded_primary:
            continue
        card = entry.get("card") or {}
        oracle = card.get("oracleCard") or {}
        # Custom card detection: Archidekt customs typically have `customImageUrl` or
        # similar; fall back to detecting missing uid.
        # Archidekt's custom-card image lives under one of these keys
        # depending on which client created the deck.
        custom_url = (
            card.get("customImageUrl")
            or entry.get("customImageUrl")
            or oracle.get("customImageUrl")
        )
        uid = card.get("uid")
        ed = card.get("edition") or {}
        jobs.append(
            CardJob(
                name=oracle.get("name")
                or card.get("displayName")
                or f"card-{card.get('id')}",
                qty=int(entry.get("quantity") or 1),
                scryfall_uid=uid if uid and not custom_url else None,
                custom_image_url=custom_url,
                set_code=ed.get("editioncode"),
                collector_number=card.get("collectorNumber"),
            )
        )
    return jobs


# Moxfield boards that count toward the playtest deck. Tokens / planes /
# schemes / attractions / stickers / contraptions are gameplay accessories
# that aren't part of the deck proper; sideboard and maybeboard are
# explicit user-selected exclusions.
_MOXFIELD_DECK_BOARDS = ("commanders", "mainboard", "companions", "signatureSpells")

# Browser-style headers needed because the Moxfield API rejects plain-Python
# user-agents at the edge (Cloudflare).
_MOXFIELD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.moxfield.com",
    "Referer": "https://www.moxfield.com/",
}


def _fetch_moxfield(public_id: str) -> list[CardJob]:
    r = requests.get(
        MOXFIELD_DECK.format(public_id), headers=_MOXFIELD_HEADERS, timeout=30
    )
    if 400 <= r.status_code < 500:
        raise SystemExit(
            f"Moxfield returned {r.status_code} for deck {public_id}. "
            "Check that the deck exists and is public."
        )
    r.raise_for_status()
    data = r.json()
    boards = data.get("boards") or {}
    jobs: list[CardJob] = []
    for board_name in _MOXFIELD_DECK_BOARDS:
        cards = (boards.get(board_name) or {}).get("cards", {})
        # Moxfield's `cards` is a dict keyed by an internal id; iteration
        # order is not stable across API responses. Sort by card name for
        # deterministic slot numbering across runs.
        for entry in sorted(
            cards.values(), key=lambda e: (e.get("card") or {}).get("name") or ""
        ):
            card = entry.get("card") or {}
            uid = card.get("scryfall_id")
            jobs.append(
                CardJob(
                    name=card.get("name") or f"moxfield-{card.get('id')}",
                    qty=int(entry.get("quantity") or 1),
                    scryfall_uid=uid,
                    custom_image_url=None,  # Moxfield's custom-art workflow uses Scryfall
                    set_code=card.get("set"),
                    collector_number=card.get("cn"),
                )
            )
    return jobs


SINGLE_PIECE_LAYOUTS = {"split", "flip", "adventure", "aftermath", "fuse"}

# Scryfall asks for 50–100ms between API requests. With ThreadPoolExecutor a
# per-thread sleep would let `workers × 12.5/s` slip through, well over their
# 10/s ceiling, and trigger 429s that look like flaky failures. This lock
# serialises the rate-limit gate across all workers.
_scryfall_lock = threading.Lock()
_scryfall_last_call = 0.0
_SCRYFALL_MIN_INTERVAL = 0.10


def _scryfall_wait() -> None:
    global _scryfall_last_call
    with _scryfall_lock:
        delta = time.monotonic() - _scryfall_last_call
        if delta < _SCRYFALL_MIN_INTERVAL:
            time.sleep(_SCRYFALL_MIN_INTERVAL - delta)
        _scryfall_last_call = time.monotonic()


def scryfall_image_urls(
    uid: str, session: requests.Session
) -> tuple[str, Optional[str]]:
    """Return (front_png_url, back_png_url_or_None). For transform / MDFC cards
    this carries both faces so the caller can pair them per-slot."""
    _scryfall_wait()
    r = session.get(SCRYFALL_CARD.format(uid), timeout=30)
    r.raise_for_status()
    d = r.json()
    layout = d.get("layout", "")
    if "image_uris" in d:
        return d["image_uris"]["png"], None
    faces = d.get("card_faces") or []
    if faces and all("image_uris" in f for f in faces):
        if layout in SINGLE_PIECE_LAYOUTS:
            return faces[0]["image_uris"]["png"], None
        return faces[0]["image_uris"]["png"], faces[1]["image_uris"]["png"]
    if faces and "image_uris" in faces[0]:
        return faces[0]["image_uris"]["png"], None
    raise RuntimeError(f"No image_uris for {uid} ({d.get('name')})")


def resolve_urls(
    job: CardJob, override_dir: Path, session: requests.Session
) -> tuple[str, Optional[str]]:
    """Return (front_url, back_url_or_None). Override files take precedence.
    Override convention: `<slug>.png` for the front, `<slug>.back.png` for the
    back face (DFC). If the override-back doesn't exist but a Scryfall back
    does, the Scryfall back is used."""
    front_override = override_dir / f"{slug(job.name)}.png"
    back_override = override_dir / f"{slug(job.name)}.back.png"
    if front_override.exists():
        front = f"file://{front_override.resolve()}"
        back = f"file://{back_override.resolve()}" if back_override.exists() else None
        return front, back
    if job.custom_image_url:
        return job.custom_image_url, None
    if job.scryfall_uid:
        return scryfall_image_urls(job.scryfall_uid, session)
    raise RuntimeError(f"No image source for {job.name}")


def fetch_image(url: str, session: requests.Session) -> Image.Image:
    if url.startswith("file://"):
        return Image.open(url[7:]).convert("RGB")
    r = session.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def pad_bleed(img: Image.Image, dpi: int, bleed_mm: float) -> Image.Image:
    """Resize art to 2.48"x3.46" and extend each edge by bleed_mm using
    edge replication. We sample slightly inset from the visible edge — this
    avoids the rounded-corner artifact on Scryfall PNGs (transparent corners
    flatten to white in RGB, so sampling row 0 or column 0 picks up white
    ends; sampling a few pixels in lands inside the actual card border).
    Final canvas: (2.48 + 2*bleed)" x (3.46 + 2*bleed)"."""
    art_w = int(round(CARD_W_IN * dpi))
    art_h = int(round(CARD_H_IN * dpi))
    bleed_px = int(round((bleed_mm / MM_PER_IN) * dpi))
    art = img.resize((art_w, art_h), Image.LANCZOS)
    canvas = Image.new("RGB", (art_w + 2 * bleed_px, art_h + 2 * bleed_px))
    canvas.paste(art, (bleed_px, bleed_px))
    if bleed_px <= 0:
        return canvas

    # Inset = 2.5% of the shorter dim. ~20px at 300dpi — comfortably past
    # MTG's rounded-corner radius (~3% of card width) and into the border.
    inset = max(8, int(0.025 * min(art_w, art_h)))
    canvas_w, canvas_h = canvas.size

    # Edges — sample a 1px row/col, but offset by `inset` along the edge so
    # we never grab the rounded-corner whitespace.
    top = art.crop((inset, inset, art_w - inset, inset + 1)).resize(
        (art_w - 2 * inset, bleed_px)
    )
    canvas.paste(top, (bleed_px + inset, 0))
    bot = art.crop((inset, art_h - inset - 1, art_w - inset, art_h - inset)).resize(
        (art_w - 2 * inset, bleed_px)
    )
    canvas.paste(bot, (bleed_px + inset, canvas_h - bleed_px))
    left = art.crop((inset, inset, inset + 1, art_h - inset)).resize(
        (bleed_px, art_h - 2 * inset)
    )
    canvas.paste(left, (0, bleed_px + inset))
    right = art.crop((art_w - inset - 1, inset, art_w - inset, art_h - inset)).resize(
        (bleed_px, art_h - 2 * inset)
    )
    canvas.paste(right, (canvas_w - bleed_px, bleed_px + inset))

    # Corner regions: bleed_px + inset on each side. Sample an `inset × inset`
    # block from inside the border so the color matches the actual border tone
    # (black on most MTG cards, white on old-frame cards, gold on Secret Lairs,
    # etc.) rather than the transparent-corner-becomes-white artifact.
    cs = inset  # corner sample size
    for cx, cy, sx, sy in [
        (0, 0, inset, inset),
        (canvas_w - bleed_px - inset, 0, art_w - inset - cs, inset),
        (0, canvas_h - bleed_px - inset, inset, art_h - inset - cs),
        (
            canvas_w - bleed_px - inset,
            canvas_h - bleed_px - inset,
            art_w - inset - cs,
            art_h - inset - cs,
        ),
    ]:
        block = art.crop((sx, sy, sx + cs, sy + cs)).resize(
            (bleed_px + inset, bleed_px + inset)
        )
        canvas.paste(block, (cx, cy))
    return canvas


DEFAULT_BACK_FILE = Path(__file__).parent / "assets" / "default_back.png"


def make_default_back(dpi: int, bleed_mm: float) -> Image.Image:
    """Default back image: the bundled `assets/default_back.png` (the
    "You Wouldn't Proxy a Magic Card" meme back). Resized + bleed-padded
    to match the rest of the deck. Override via --default-back."""
    if not DEFAULT_BACK_FILE.exists():
        raise FileNotFoundError(
            f"Bundled default back missing: {DEFAULT_BACK_FILE}. "
            "Pass --default-back to provide your own."
        )
    img = Image.open(DEFAULT_BACK_FILE).convert("RGB")
    return pad_bleed(img, dpi, bleed_mm)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "deck",
        help="Deck URL or id. Recognised: Archidekt URL, Moxfield URL, a numeric "
        "Archidekt id, or a Moxfield public id.",
    )
    ap.add_argument("-o", "--out", default="out", help="Output directory")
    ap.add_argument("--overrides", default="overrides", help="Override images dir")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument(
        "--no-bleed",
        action="store_true",
        help="Skip bleed padding (use TCGPlaytest XML path instead)",
    )
    ap.add_argument("--bleed-mm", type=float, default=BLEED_MM)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument(
        "--pair-backs",
        action="store_true",
        help="Emit out/backs/ with paired back images for each slot "
        "(DFC face-2 for transforms, default playtest back for "
        "everything else). Suitable for tcgplaytest's "
        "Sequential Backs feature.",
    )
    ap.add_argument(
        "--default-back",
        help="Back image for non-DFC cards when --pair-backs is set. "
        "Defaults to assets/default_back.png (a meme back) "
        "shipped with the repo.",
    )
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    fronts_dir = out_dir / "fronts" if args.pair_backs else out_dir
    backs_dir = out_dir / "backs" if args.pair_backs else None
    fronts_dir.mkdir(parents=True, exist_ok=True)
    if backs_dir:
        backs_dir.mkdir(parents=True, exist_ok=True)
    overrides = Path(args.overrides)
    overrides.mkdir(exist_ok=True)

    # Resolve default back image if pairing.
    default_back_img: Optional[Image.Image] = None
    if args.pair_backs:
        if args.default_back:
            default_back_img = Image.open(args.default_back).convert("RGB")
            if not args.no_bleed:
                default_back_img = pad_bleed(default_back_img, args.dpi, args.bleed_mm)
        else:
            default_back_img = make_default_back(
                args.dpi, 0 if args.no_bleed else args.bleed_mm
            )

    print(f"Fetching deck {args.deck}...")
    jobs = fetch_deck(args.deck)
    total_cards = sum(j.qty for j in jobs)
    print(
        f"  {len(jobs)} unique cards, {total_cards} total copies"
        + (" (with paired backs)" if args.pair_backs else "")
    )

    session = requests.Session()
    session.headers.update(UA)

    manifest: list[dict] = []
    failures: list[tuple[str, str]] = []

    # Catch only the IO/network/decoder exceptions that genuinely correspond
    # to a per-card failure. Programming errors (TypeError, AttributeError,
    # etc.) propagate so they aren't silently logged as "card failed".
    network_errors = (
        requests.RequestException,
        OSError,
        RuntimeError,
        Image.UnidentifiedImageError,
    )

    def process(idx: int, job: CardJob):
        try:
            front_url, back_url = resolve_urls(job, overrides, session)
            front_img = fetch_image(front_url, session)
            if not args.no_bleed:
                front_img = pad_bleed(front_img, args.dpi, args.bleed_mm)
            back_img = None
            if back_url:
                back_img = fetch_image(back_url, session)
                if not args.no_bleed:
                    back_img = pad_bleed(back_img, args.dpi, args.bleed_mm)
            return idx, job, front_img, back_img, front_url, back_url, None
        except network_errors as e:
            return idx, job, None, None, None, None, f"ERROR: {e}"

    # Each card-copy gets its own slot number; slot indices match between
    # fronts/ and backs/ so tcgplaytest's Sequential Backs aligns correctly.
    slot = 0
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, i, j): (i, j) for i, j in enumerate(jobs)}
        for fut in as_completed(futures):
            results.append(fut.result())

    # Sort by job idx so output is deterministic.
    results.sort(key=lambda r: r[0])

    for idx, job, front_img, back_img, front_url, back_url, err in results:
        tag = f"[{idx + 1}/{len(jobs)}] {job.name} x{job.qty}"
        if front_img is None:
            print(f"  FAIL {tag}: {err}")
            failures.append((job.name, str(err)))
            continue
        slug_name = slug(job.name)
        files: list[str] = []
        for copy in range(1, job.qty + 1):
            slot += 1
            base = f"{slot:03d}_{slug_name}"
            front_path = fronts_dir / f"{base}.png"
            front_img.save(front_path, "PNG", optimize=True)
            files.append(str(front_path.relative_to(out_dir)))
            if backs_dir is not None:
                this_back = back_img if back_img is not None else default_back_img
                back_path = backs_dir / f"{base}.png"
                this_back.save(back_path, "PNG", optimize=True)
                files.append(str(back_path.relative_to(out_dir)))
        manifest.append(
            {
                "name": job.name,
                "qty": job.qty,
                "has_back": back_img is not None,
                "front_source": front_url,
                "back_source": back_url,
                "files": files,
            }
        )
        note = " (DFC, paired back)" if back_img is not None else ""
        print(f"  ok   {tag}{note}")

    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "deck_id": args.deck,
                "paired_backs": args.pair_backs,
                "cards": manifest,
                "failures": failures,
            },
            indent=2,
        )
    )
    if args.pair_backs:
        print(f"\nDone. {slot} card slots written to {fronts_dir}/ and {backs_dir}/")
    else:
        print(f"\nDone. {slot} files written to {fronts_dir}/")
    if failures:
        print(f"  {len(failures)} failures (see manifest.json)")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
