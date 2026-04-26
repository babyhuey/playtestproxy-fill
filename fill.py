"""Archidekt → TCGPlaytest image folder.

Usage:
    python fill.py <archidekt_deck_id> [-o out_dir] [--dpi 300] [--no-bleed]

Pipeline:
  1. Fetch deck JSON from Archidekt.
  2. For each card: resolve image URL (override > custom > Scryfall).
  3. Download.
  4. Pad with 3mm bleed (edge-extend) so TCGPlaytest's bleed expectation is satisfied.
  5. Write one image per copy: out/<NN>_<safename>.png
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageOps

ARCHIDEKT_DECK = "https://archidekt.com/api/decks/{}/"
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


def fetch_deck(deck_id: str) -> list[CardJob]:
    r = requests.get(ARCHIDEKT_DECK.format(deck_id), headers=UA, timeout=30)
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
        custom_url = (
            card.get("customImageUrl")
            or entry.get("customImageUrl")
            or oracle.get("customImageUrl")
        )
        uid = card.get("uid")
        ed = card.get("edition") or {}
        jobs.append(
            CardJob(
                name=oracle.get("name") or card.get("displayName") or f"card-{card.get('id')}",
                qty=int(entry.get("quantity") or 1),
                scryfall_uid=uid if uid and not custom_url else None,
                custom_image_url=custom_url,
                set_code=ed.get("editioncode"),
                collector_number=card.get("collectorNumber"),
            )
        )
    return jobs


SINGLE_PIECE_LAYOUTS = {"split", "flip", "adventure", "aftermath", "fuse"}


def scryfall_image_urls(uid: str, session: requests.Session) -> tuple[str, Optional[str]]:
    """Return (front_png_url, back_png_url_or_None). For transform / MDFC cards
    this carries both faces so the caller can pair them per-slot."""
    time.sleep(0.08)
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
    """Resize art to 2.48"x3.46" then extend each edge by bleed_mm using
    edge replication (no background color, just stretch the outer pixels).
    Final canvas: (2.48 + 2*bleed)" x (3.46 + 2*bleed)".
    """
    art_w = int(round(CARD_W_IN * dpi))
    art_h = int(round(CARD_H_IN * dpi))
    bleed_px = int(round((bleed_mm / MM_PER_IN) * dpi))
    art = img.resize((art_w, art_h), Image.LANCZOS)
    canvas_w = art_w + 2 * bleed_px
    canvas_h = art_h + 2 * bleed_px
    # Edge replicate via PIL: paste art, then fill borders by stretching edges.
    canvas = Image.new("RGB", (canvas_w, canvas_h))
    canvas.paste(art, (bleed_px, bleed_px))
    if bleed_px > 0:
        # top
        top = art.crop((0, 0, art_w, 1)).resize((art_w, bleed_px))
        canvas.paste(top, (bleed_px, 0))
        # bottom
        bot = art.crop((0, art_h - 1, art_w, art_h)).resize((art_w, bleed_px))
        canvas.paste(bot, (bleed_px, canvas_h - bleed_px))
        # left
        left = art.crop((0, 0, 1, art_h)).resize((bleed_px, art_h))
        canvas.paste(left, (0, bleed_px))
        # right
        right = art.crop((art_w - 1, 0, art_w, art_h)).resize((bleed_px, art_h))
        canvas.paste(right, (canvas_w - bleed_px, bleed_px))
        # corners — pick the corner pixel
        for cx, cy, sx, sy in [
            (0, 0, 0, 0),
            (canvas_w - bleed_px, 0, art_w - 1, 0),
            (0, canvas_h - bleed_px, 0, art_h - 1),
            (canvas_w - bleed_px, canvas_h - bleed_px, art_w - 1, art_h - 1),
        ]:
            corner = art.crop((sx, sy, sx + 1, sy + 1)).resize((bleed_px, bleed_px))
            canvas.paste(corner, (cx, cy))
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
    ap.add_argument("deck_id", help="Archidekt deck id (the number in the URL)")
    ap.add_argument("-o", "--out", default="out", help="Output directory")
    ap.add_argument("--overrides", default="overrides", help="Override images dir")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--no-bleed", action="store_true",
                    help="Skip bleed padding (use TCGPlaytest XML path instead)")
    ap.add_argument("--bleed-mm", type=float, default=BLEED_MM)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--pair-backs", action="store_true",
                    help="Emit out/backs/ with paired back images for each slot "
                         "(DFC face-2 for transforms, default playtest back for "
                         "everything else). Suitable for tcgplaytest's "
                         "Sequential Backs feature.")
    ap.add_argument("--default-back",
                    help="PNG to use as the back for non-DFC cards when --pair-backs "
                         "is set. If omitted, a generated 'PLAYTEST' placeholder is used.")
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

    print(f"Fetching deck {args.deck_id}...")
    jobs = fetch_deck(args.deck_id)
    total_cards = sum(j.qty for j in jobs)
    print(f"  {len(jobs)} unique cards, {total_cards} total copies"
          + (" (with paired backs)" if args.pair_backs else ""))

    session = requests.Session()
    session.headers.update(UA)

    manifest: list[dict] = []
    failures: list[tuple[str, str]] = []

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
        except Exception as e:
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
        manifest.append({
            "name": job.name,
            "qty": job.qty,
            "has_back": back_img is not None,
            "front_source": front_url,
            "back_source": back_url,
            "files": files,
        })
        note = " (DFC, paired back)" if back_img is not None else ""
        print(f"  ok   {tag}{note}")

    (out_dir / "manifest.json").write_text(json.dumps(
        {
            "deck_id": args.deck_id,
            "paired_backs": args.pair_backs,
            "cards": manifest,
            "failures": failures,
        },
        indent=2,
    ))
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
