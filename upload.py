"""Drive tcgplaytest.com's design page and bulk-upload images from a folder.

Usage:
    python upload.py <images_dir> [--headless] [--user-data ./browser_profile]

If <images_dir> contains `fronts/` and `backs/` subfolders (output of
`fill.py --pair-backs`), the script also uploads the backs via the
Sequential Backs feature on Step 2 — pairing each back with the card
in the same slot. Otherwise it just uploads fronts.

Sequence (learned by inspecting the live site):
  1. Set files on the image input on Step 1 (Customize Front).
  2. Click "3mm Bleed added" on the bleed-prompt modal (matches our padded
     output from fill.py:pad_bleed).
  3. Wait for "Applying Bleed Settings..." to clear; dismiss "Got It" toast.
  4. If backs/ exists: click Next, find the Sequential Backs file input,
     set files, wait for processing.
  5. Save Draft. Leave browser open with --user-data so it persists.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from playwright.async_api import async_playwright, Page

DESIGN_URL = "https://www.tcgplaytest.com/?view=design"
EXTS = {".png", ".jpg", ".jpeg"}


def collect_files(d: Path) -> list[Path]:
    files = sorted(p for p in d.iterdir() if p.suffix.lower() in EXTS and p.is_file())
    if not files:
        raise SystemExit(f"No PNG/JPG files in {d}")
    return files


async def get_card_count(page: Page) -> int | None:
    """Read the 'N Cards' indicator from the page text. None if missing."""
    text = await page.evaluate("() => document.body.innerText")
    m = re.search(r"(\d+)\s+Cards?\s*•", text)
    return int(m.group(1)) if m else None


async def wait_for_processing_done(page: Page, expected: int, timeout_s: int = 180) -> int:
    """Poll until the 'Applying Bleed Settings...' overlay is gone AND the count
    reaches `expected` (or stops climbing for several seconds)."""
    last = -1
    stable_for = 0
    for i in range(timeout_s):
        # Detect the processing overlay
        processing = await page.evaluate(
            "() => document.body.innerText.includes('Applying Bleed Settings')"
            " || document.body.innerText.includes('Processing cards')"
        )
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
    file input. Returns True on success."""
    # Navigate to the back step.
    try:
        await page.get_by_role("button", name=re.compile("Next.*Customize Back", re.I)).click(timeout=5000)
    except Exception as e:
        print(f"  could not click Next→Customize Back: {e}")
        return False
    await page.wait_for_timeout(2500)

    # The back step has multiple file inputs. We want the "Sequential Backs"
    # one specifically — labelled with "Upload Sequential Backs" / "(N cards)".
    inputs = await page.query_selector_all('input[type="file"]')
    target = None
    for el in inputs:
        accept = (await el.get_attribute("accept")) or ""
        multiple = await el.get_attribute("multiple")
        if "image" in accept.lower() and multiple is not None:
            target = el  # the multi-image input is Sequential Backs
            break
    if target is None:
        # fall back: the second image input on this step
        image_inputs = []
        for el in inputs:
            a = (await el.get_attribute("accept")) or ""
            if "image" in a.lower():
                image_inputs.append(el)
        if len(image_inputs) >= 2:
            target = image_inputs[1]
    if target is None:
        print("  could not find Sequential Backs input")
        return False

    print(f"  uploading {len(back_files)} sequential backs...")
    await target.set_input_files([str(p) for p in back_files])
    # Wait for Bleed-Settings processing. The back step has its own variant
    # of the modal: "Does Your Card Back Have Print Bleed?".
    for _ in range(60):
        visible = await page.evaluate(
            "() => Array.from(document.querySelectorAll('*'))"
            ".some(e => /(Do Your Images Have Print Bleed|Does Your Card Back Have Print Bleed)/"
            ".test(e.innerText || ''))"
        )
        if visible:
            try:
                await page.get_by_text("3mm Bleed added", exact=False).first.click(timeout=3000)
                print("  selected: 3mm Bleed added (backs)")
                break
            except Exception:
                pass
        await asyncio.sleep(1)
    for _ in range(180):
        still = await page.evaluate(
            "() => document.body.innerText.includes('Applying Bleed Settings')"
            " || document.body.innerText.includes('Processing card')"
        )
        if not still:
            break
        await asyncio.sleep(1)
    # Dismiss any "Got It" / confirmation modal.
    for label in ("Got It", "OK", "Got it!"):
        try:
            await page.get_by_role("button", name=label).click(timeout=2000)
            break
        except Exception:
            pass
    await asyncio.sleep(2)
    return True


async def run(images_dir: Path, headed: bool, user_data: Path | None,
              save_draft: bool, batch_size: int) -> int:
    # Detect fronts/backs layout from `fill.py --pair-backs`. If absent,
    # treat the dir itself as fronts.
    fronts_dir = images_dir / "fronts" if (images_dir / "fronts").is_dir() else images_dir
    backs_dir = images_dir / "backs" if (images_dir / "backs").is_dir() else None
    files = collect_files(fronts_dir)
    back_files = collect_files(backs_dir) if backs_dir else []
    if backs_dir and len(back_files) != len(files):
        print(f"WARN: front count ({len(files)}) != back count ({len(back_files)}); "
              "Sequential Backs requires matching counts.")
    print(f"Found {len(files)} fronts in {fronts_dir}"
          + (f" + {len(back_files)} backs in {backs_dir}" if backs_dir else ""))

    async with async_playwright() as p:
        if user_data:
            user_data.mkdir(parents=True, exist_ok=True)
            ctx = await p.chromium.launch_persistent_context(
                str(user_data), headless=not headed,
                viewport={"width": 1500, "height": 1000},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            browser = None
        else:
            browser = await p.chromium.launch(headless=not headed)
            ctx = await browser.new_context(viewport={"width": 1500, "height": 1000})
            page = await ctx.new_page()

        await page.goto(DESIGN_URL, wait_until="networkidle")

        # Cookie banner.
        try:
            await page.get_by_role("button", name="Accept").click(timeout=3000)
        except Exception:
            pass

        # The Fronts uploader: a styled dropzone with a hidden <input type=file
        # accept="image/*" multiple>. Set files directly.
        inputs = await page.query_selector_all('input[type="file"]')
        front_input = None
        for el in inputs:
            accept = (await el.get_attribute("accept")) or ""
            if "image" in accept.lower():
                front_input = el
                break
        if front_input is None:
            raise SystemExit("Could not find image file input")

        # The site processes uploads in-browser (Canvas2D readback). Very large
        # batches can stall it. Upload in chunks if requested.
        chunks: list[list[Path]] = []
        if batch_size > 0 and len(files) > batch_size:
            for i in range(0, len(files), batch_size):
                chunks.append(files[i : i + batch_size])
        else:
            chunks = [files]

        # The "Do Your Images Have Print Bleed?" modal can appear at any point
        # after set_input_files (sometimes immediately, sometimes after the
        # canvas2d processing finishes). Run a watcher in parallel that clicks
        # "3mm Bleed added" whenever the modal becomes visible.
        clicked_modal = {"done": False}

        async def modal_watcher() -> None:
            for _ in range(120):  # up to ~2 min
                if clicked_modal["done"]:
                    return
                try:
                    visible = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('*'))"
                        ".some(e => (e.innerText || '').includes('Do Your Images Have Print Bleed'))"
                    )
                except Exception:
                    visible = False
                if visible:
                    try:
                        await page.get_by_text("3mm Bleed added", exact=False).first.click(timeout=3000)
                        clicked_modal["done"] = True
                        print("  selected: 3mm Bleed added (images already have bleed)")
                        return
                    except Exception as e:
                        print(f"  modal click failed: {e}")
                await asyncio.sleep(1)

        watcher_task = asyncio.create_task(modal_watcher())

        running_total = 0
        for ci, chunk in enumerate(chunks, 1):
            print(f"[{ci}/{len(chunks)}] uploading {len(chunk)} files...")
            await front_input.set_input_files([str(p) for p in chunk])
            running_total += len(chunk)
            count = await wait_for_processing_done(page, running_total)
            print(f"  count: {count}/{running_total}")

        # Give the watcher a few more seconds in case the modal appears late.
        for _ in range(15):
            if clicked_modal["done"]:
                break
            await asyncio.sleep(1)
        watcher_task.cancel()
        try:
            await watcher_task
        except (asyncio.CancelledError, Exception):
            pass

        # After the modal click, the site runs "Applying Bleed Settings..."
        # then shows a "Bleed Trimmed Successfully" confirmation.
        for _ in range(120):
            still = await page.evaluate(
                "() => document.body.innerText.includes('Applying Bleed Settings')"
                " || document.body.innerText.includes('Processing cards')"
            )
            if not still:
                break
            await asyncio.sleep(1)
        # Dismiss the "Bleed Trimmed Successfully" modal if present.
        for label in ("Got It", "OK", "Got it!"):
            try:
                await page.get_by_role("button", name=label).click(timeout=2000)
                print(f"  dismissed: {label}")
                break
            except Exception:
                pass

        # Final verification.
        final = await get_card_count(page) or 0
        print(f"\nFINAL: {final}/{len(files)} fronts registered")

        # If we have paired backs, drive the Sequential Backs step.
        if backs_dir and back_files and final == len(files):
            await upload_sequential_backs(page, back_files)

        if save_draft:
            try:
                await page.get_by_role("button", name=re.compile("Save Draft", re.I)).click(timeout=5000)
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
    ap.add_argument("--headless", action="store_true",
                    help="Run without a visible browser window")
    ap.add_argument("--user-data", default=None,
                    help="Persistent browser profile dir (lets draft survive)")
    ap.add_argument("--no-save-draft", action="store_true",
                    help="Do not click Save Draft after upload")
    ap.add_argument("--batch-size", type=int, default=0,
                    help="Upload in chunks of N (0 = all at once)")
    args = ap.parse_args()

    images = Path(args.images_dir)
    if not images.is_dir():
        print(f"Not a directory: {images}", file=sys.stderr)
        return 1

    return asyncio.run(run(
        images,
        headed=not args.headless,
        user_data=Path(args.user_data) if args.user_data else None,
        save_draft=not args.no_save_draft,
        batch_size=args.batch_size,
    ))


if __name__ == "__main__":
    sys.exit(main())
