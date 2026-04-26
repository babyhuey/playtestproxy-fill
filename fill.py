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


def scryfall_image_urls(uid: str, session: requests.Session) -> list[tuple[str, str]]:
    """Return [(face_label, png_url), ...]. Single-faced cards return one entry;
    double-faced cards (transform / MDFC) return two entries — front and back —
    so each face becomes its own physical playtest card."""
    time.sleep(0.08)
    r = session.get(SCRYFALL_CARD.format(uid), timeout=30)
    r.raise_for_status()
    d = r.json()
    layout = d.get("layout", "")
    if "image_uris" in d:
        return [("", d["image_uris"]["png"])]
    faces = d.get("card_faces") or []
    if faces and all("image_uris" in f for f in faces):
        # Layouts whose "back" isn't a real second card: split, flip, adventure,
        # aftermath — the card is one physical piece printed once.
        single_piece = {"split", "flip", "adventure", "aftermath", "fuse"}
        if layout in single_piece:
            return [("", faces[0]["image_uris"]["png"])]
        return [
            (slug(faces[0].get("name", "front")), faces[0]["image_uris"]["png"]),
            (slug(faces[1].get("name", "back")), faces[1]["image_uris"]["png"]),
        ]
    if faces and "image_uris" in faces[0]:
        return [("", faces[0]["image_uris"]["png"])]
    raise RuntimeError(f"No image_uris for {uid} ({d.get('name')})")


def resolve_urls(job: CardJob, override_dir: Path, session: requests.Session) -> list[tuple[str, str]]:
    """Return list of (face_label, url). One entry for single-faced cards,
    two for transform/MDFC."""
    candidate = override_dir / f"{slug(job.name)}.png"
    if candidate.exists():
        return [("", f"file://{candidate.resolve()}")]
    if job.custom_image_url:
        return [("", job.custom_image_url)]
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck_id", help="Archidekt deck id (the number in the URL)")
    ap.add_argument("-o", "--out", default="out", help="Output directory")
    ap.add_argument("--overrides", default="overrides", help="Override images dir (PNGs named <slug>.png)")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--no-bleed", action="store_true", help="Skip bleed padding (use TCGPlaytest XML path instead)")
    ap.add_argument("--bleed-mm", type=float, default=BLEED_MM)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    overrides = Path(args.overrides)
    overrides.mkdir(exist_ok=True)

    print(f"Fetching deck {args.deck_id}...")
    jobs = fetch_deck(args.deck_id)
    total_cards = sum(j.qty for j in jobs)
    print(f"  {len(jobs)} unique cards, {total_cards} total copies")

    session = requests.Session()
    session.headers.update(UA)

    manifest: list[dict] = []
    failures: list[tuple[str, str]] = []

    def process(idx: int, job: CardJob):
        try:
            face_urls = resolve_urls(job, overrides, session)
            faces = []
            for label, url in face_urls:
                img = fetch_image(url, session)
                if not args.no_bleed:
                    img = pad_bleed(img, args.dpi, args.bleed_mm)
                faces.append((label, img, url))
            return idx, job, faces, None
        except Exception as e:
            return idx, job, None, f"ERROR: {e}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process, i, j) for i, j in enumerate(jobs)]
        for fut in as_completed(futures):
            idx, job, faces, err = fut.result()
            tag = f"[{idx + 1}/{len(jobs)}] {job.name} x{job.qty}"
            if faces is None:
                print(f"  FAIL {tag}: {err}")
                failures.append((job.name, str(err)))
                continue
            base = f"{idx + 1:03d}_{slug(job.name)}"
            written: list[str] = []
            sources: list[str] = []
            for face_label, img, src in faces:
                face_part = f"_{face_label}" if face_label else ""
                for copy in range(1, job.qty + 1):
                    copy_part = f"_c{copy}" if job.qty > 1 else ""
                    fname = f"{base}{face_part}{copy_part}.png"
                    img.save(out_dir / fname, "PNG", optimize=True)
                    written.append(fname)
                sources.append(src)
            manifest.append({
                "name": job.name,
                "qty": job.qty,
                "faces": len(faces),
                "sources": sources,
                "files": written,
            })
            face_note = f" ({len(faces)} faces)" if len(faces) > 1 else ""
            print(f"  ok   {tag}{face_note}")

    (out_dir / "manifest.json").write_text(json.dumps(
        {"deck_id": args.deck_id, "cards": manifest, "failures": failures},
        indent=2,
    ))
    print(f"\nDone. {len(manifest)} cards, {sum(j.qty for j in jobs if j.name not in {f[0] for f in failures})} files written to {out_dir}/")
    if failures:
        print(f"  {len(failures)} failures (see manifest.json)")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
