"""Drive tcgplaytest.com's design page and bulk-upload images from a folder.

Usage:
    python upload.py <images_dir> [--headless] [--user-data ./browser_profile]

If <images_dir> contains `fronts/` and `backs/` subfolders (output of
`fill.py --pair-backs`), the script also uploads the backs via the
Sequential Backs feature on Step 2 — pairing each back with the card
in the same slot. Otherwise it just uploads fronts.

Sequence (learned by inspecting the live site):
  1. Set files on the image input on Step 1 (Customize Front).
  2. Wait for per-card processing to finish; dismiss the "Got It" toast.
  3. If backs/ exists: click Next, find the Sequential Backs file input,
     set files, wait for processing.
  4. Save Draft. Leave browser open with --user-data so it persists.

tcgplaytest used to interrupt step 1 with a modal asking whether the
uploads already carried print bleed, which we answered "No Bleed". It now
expands bleed itself and never asks, so there is no modal to handle.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import Page, async_playwright

DESIGN_URL = "https://www.tcgplaytest.com/?view=design"
EXTS = {".png", ".jpg", ".jpeg"}

# UI strings hardcoded by tcgplaytest. Hoisted here so a future site update
# only needs one fix.
PROCESSING_TEXTS = ("Applying Bleed Settings", "Processing card")  # plural varies
SEQUENTIAL_BACKS_LABEL = "Sequential Backs"
DISMISS_LABELS = ("Got It", "Got it!", "OK")
NEXT_BACK_BUTTON = re.compile(r"Next.*Customize Back", re.I)
SAVE_DRAFT_BUTTON = re.compile(r"Save Draft", re.I)
COOKIE_ACCEPT = "Accept"


def _slot_sort_key(p: Path) -> tuple[int, int, str]:
    """Order files by their numeric slot prefix (`012_name.png`) so
    `1000_` sorts after `999_` — lexical order would misalign Sequential
    Backs on 1000+ card runs. Unprefixed files sort after, by name."""
    m = re.match(r"(\d+)_", p.name)
    return (0, int(m.group(1)), p.name) if m else (1, 0, p.name)


def collect_files(d: Path) -> list[Path]:
    files = sorted(
        (p for p in d.iterdir() if p.suffix.lower() in EXTS and p.is_file()),
        key=_slot_sort_key,
    )
    if not files:
        raise SystemExit(f"No PNG/JPG files in {d}")
    return files


async def get_card_count(page: Page) -> int | None:
    """Read the 'N Cards' indicator from the page text. None if missing."""
    text = await page.evaluate("() => document.body.innerText")
    m = re.search(r"(\d+)\s+Cards?\b", text)
    return int(m.group(1)) if m else None


async def _processing_visible(page: Page) -> bool:
    js = "(texts) => texts.some(t => document.body.innerText.includes(t))"
    return await page.evaluate(js, list(PROCESSING_TEXTS))


async def wait_for_processing_done(page: Page, expected: int, timeout_s: int = 180) -> int:
    """Poll until the processing overlay is gone AND the count reaches
    `expected` (or stops climbing for several seconds)."""
    last = -1
    stable_for = 0
    for _ in range(timeout_s):
        processing = await _processing_visible(page)
        count = await get_card_count(page)
        if count == expected and not processing:
            return count
        if count == last:
            stable_for += 1
        else:
            stable_for = 0
            last = count if count is not None else last
        if stable_for >= 6 and not processing and last >= 0:
            return last
        await page.wait_for_timeout(1000)
    return last if last >= 0 else 0


async def upload_sequential_backs(page: Page, back_files: list[Path]) -> bool:
    """On the Customize Back step, push back images into the Sequential Backs
    file input. Returns True once the input has accepted every file; False
    when a step couldn't be verified (missing input, short file count)."""
    try:
        await page.get_by_role("button", name=NEXT_BACK_BUTTON).click(timeout=5000)
    except Exception as e:
        print(f"  could not click Next→Customize Back: {e}")
        return False
    await page.wait_for_timeout(2500)

    # Both file inputs on this step are accept="image/*" multiple, so the
    # `multiple` attribute no longer tells them apart — and the single
    # common-back input ("Upload Back") comes first, so picking by attribute
    # sends every sequential back into the common-back slot and misaligns the
    # whole order. Pick the input most tightly wrapped by the labelled
    # "Sequential Backs" section instead.
    inputs = await page.query_selector_all('input[type="file"]')
    idx = await page.evaluate(
        """(label) => {
          const els = [...document.querySelectorAll('input[type=file]')];
          let best = -1, bestDepth = Infinity;
          els.forEach((el, i) => {
            let n = el.parentElement;
            for (let d = 0; n && d < 8; n = n.parentElement, d++) {
              if ((n.innerText || "").includes(label)) {
                if (d < bestDepth) { bestDepth = d; best = i; }
                break;
              }
            }
          });
          return best;
        }""",
        SEQUENTIAL_BACKS_LABEL,
    )
    if idx < 0 or idx >= len(inputs):
        print(f"  could not find the {SEQUENTIAL_BACKS_LABEL!r} input")
        return False
    target = inputs[idx]

    print(f"  uploading {len(back_files)} sequential backs...")
    await target.set_input_files([str(p) for p in back_files])
    # The site gives no completion signal for backs (the card count only
    # tracks fronts), so confirm against the input's own FileList.
    accepted = await target.evaluate("(el) => el.files.length")
    if accepted != len(back_files):
        print(f"  WARNING: input accepted {accepted}/{len(back_files)} backs")
    for _ in range(180):
        if not await _processing_visible(page):
            break
        await asyncio.sleep(1)
    # Dismiss any "Got It" / confirmation modal.
    for label in DISMISS_LABELS:
        try:
            await page.get_by_role("button", name=label).click(timeout=2000)
            break
        except Exception:
            pass
    await asyncio.sleep(2)
    return accepted == len(back_files)


async def run(
    images_dir: Path, headed: bool, user_data: Path | None, save_draft: bool, batch_size: int
) -> int:
    # Detect fronts/backs layout from `fill.py --pair-backs`. If absent,
    # treat the dir itself as fronts.
    fronts_dir = images_dir / "fronts" if (images_dir / "fronts").is_dir() else images_dir
    backs_dir = images_dir / "backs" if (images_dir / "backs").is_dir() else None
    files = collect_files(fronts_dir)
    back_files = collect_files(backs_dir) if backs_dir else []
    if backs_dir and len(back_files) != len(files):
        # Sequential Backs assigns image N → slot N. A count mismatch means
        # every card after the first divergence gets the wrong back; far
        # better to abort than silently misalign the order.
        raise SystemExit(
            f"front count ({len(files)}) != back count ({len(back_files)}) — "
            f"Sequential Backs needs matching counts. Re-run fill.py --pair-backs."
        )
    print(
        f"Found {len(files)} fronts in {fronts_dir}"
        + (f" + {len(back_files)} backs in {backs_dir}" if backs_dir else "")
    )

    async with async_playwright() as p:
        if user_data:
            user_data.mkdir(parents=True, exist_ok=True)
            ctx = await p.chromium.launch_persistent_context(
                str(user_data),
                headless=not headed,
                viewport={"width": 1500, "height": 1000},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            browser = None
        else:
            browser = await p.chromium.launch(headless=not headed)
            ctx = await browser.new_context(viewport={"width": 1500, "height": 1000})
            page = await ctx.new_page()

        await page.goto(DESIGN_URL, wait_until="networkidle")

        try:
            await page.get_by_role("button", name=COOKIE_ACCEPT).click(timeout=3000)
        except Exception:
            pass

        # The Fronts uploader: a styled dropzone with a hidden <input type=file
        # accept="image/*" multiple>. Set files directly. Re-queried per chunk
        # below — the element handle can go stale when the page re-renders
        # between batch uploads.
        async def find_front_input():
            inputs = await page.query_selector_all('input[type="file"]')
            for el in inputs:
                accept = (await el.get_attribute("accept")) or ""
                if "image" in accept.lower():
                    return el
            return None

        # The site processes uploads in-browser (Canvas2D readback). Very large
        # batches can stall it. Upload in chunks if requested.
        chunks: list[list[Path]] = []
        if batch_size > 0 and len(files) > batch_size:
            for i in range(0, len(files), batch_size):
                chunks.append(files[i : i + batch_size])
        else:
            chunks = [files]

        running_total = 0
        for ci, chunk in enumerate(chunks, 1):
            print(f"[{ci}/{len(chunks)}] uploading {len(chunk)} files...")
            front_input = await find_front_input()
            if front_input is None:
                raise SystemExit("Could not find image file input")
            await front_input.set_input_files([str(p) for p in chunk])
            running_total += len(chunk)
            count = await wait_for_processing_done(page, running_total)
            print(f"  count: {count}/{running_total}")

        for _ in range(120):
            if not await _processing_visible(page):
                break
            await asyncio.sleep(1)
        for label in DISMISS_LABELS:
            try:
                await page.get_by_role("button", name=label).click(timeout=2000)
                print(f"  dismissed: {label}")
                break
            except Exception:
                pass

        final = await get_card_count(page) or 0
        print(f"\nFINAL: {final}/{len(files)} fronts registered")
        if final != len(files):
            # Failing loud here is better than uploading misaligned backs
            # against a partially-loaded front deck.
            print(
                f"WARNING: only {final}/{len(files)} cards registered. "
                "Sequential Backs and Save Draft skipped — investigate the "
                "upload before continuing."
            )
        elif backs_dir and back_files:
            if not await upload_sequential_backs(page, back_files):
                print(
                    "WARNING: sequential backs could not be confirmed — verify "
                    "them in the browser before ordering."
                )

        if save_draft and final == len(files):
            try:
                await page.get_by_role("button", name=SAVE_DRAFT_BUTTON).click(timeout=5000)
                await page.wait_for_timeout(2000)
                print("Save Draft clicked.")
            except Exception as e:
                print(f"Save Draft not clicked: {e}")

        await page.screenshot(path=str(images_dir.parent / "upload_state.png"), full_page=False)
        print(f"Screenshot: {images_dir.parent / 'upload_state.png'}")

        if headed:
            print("\nBrowser left open. Finish in the UI; press Ctrl+C here when done.")
            try:
                while True:
                    await asyncio.sleep(60)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

        if browser:
            await browser.close()
        else:
            await ctx.close()
        return 0 if final == len(files) else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images_dir")
    ap.add_argument("--headless", action="store_true", help="Run without a visible browser window")
    ap.add_argument(
        "--user-data", default=None, help="Persistent browser profile dir (lets draft survive)"
    )
    ap.add_argument(
        "--no-save-draft", action="store_true", help="Do not click Save Draft after upload"
    )
    ap.add_argument(
        "--batch-size", type=int, default=0, help="Upload in chunks of N (0 = all at once)"
    )
    args = ap.parse_args()

    images = Path(args.images_dir)
    if not images.is_dir():
        print(f"Not a directory: {images}", file=sys.stderr)
        return 1

    return asyncio.run(
        run(
            images,
            headed=not args.headless,
            user_data=Path(args.user_data) if args.user_data else None,
            save_draft=not args.no_save_draft,
            batch_size=args.batch_size,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
