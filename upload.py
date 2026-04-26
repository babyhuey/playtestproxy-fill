"""Drive tcgplaytest.com's design page and bulk-upload images from a folder.

Usage:
    python upload.py <images_dir> [--headless] [--user-data ./browser_profile]

Opens a Chromium window, navigates to the design page, dismisses cookies,
and pushes every PNG/JPG in <images_dir> into the front uploader.

Sequence (learned by inspecting the live site):
  1. set_input_files on the hidden image input.
  2. The site shows a "Do Your Images Have Print Bleed?" modal — click
     "Standard Card (no bleed added)" because fill.py already bakes in 3mm.
  3. The site then runs "Applying Bleed Settings..." — wait until it's gone.
  4. Verify the card count text reflects the number of files uploaded.
  5. Leave the browser open so the user can finish customize back / preview /
     checkout. With --user-data the draft persists across runs.
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


async def run(images_dir: Path, headed: bool, user_data: Path | None,
              save_draft: bool, batch_size: int) -> int:
    files = collect_files(images_dir)
    print(f"Found {len(files)} images in {images_dir}")

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
        print(f"\nFINAL: {final}/{len(files)} cards registered")

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
