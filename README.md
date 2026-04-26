# playtestproxy-fill

Pull a deck from Archidekt and produce print-ready images for tcgplaytest.com,
with optional Playwright auto-upload.

Verified end-to-end on `https://archidekt.com/decks/21170685/` (95 unique
cards, 99 print files including double-faced backs, draft saved).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install requests Pillow playwright
.venv/bin/playwright install chromium
```

## Step 1 — Generate images

```bash
.venv/bin/python fill.py <archidekt_deck_id> -o out
```

The deck id is the number in `https://archidekt.com/decks/<id>/...`.

What this does:
1. Fetches the deck JSON from Archidekt's API.
2. For each card, picks an image source in this order:
   1. `overrides/<card_slug>.png` if present (drop your own art here).
   2. Archidekt-hosted custom image if the card is a custom.
   3. Scryfall PNG for the **specific printing recorded in the deck** (so
      Secret Lair / alt-art selections come through faithfully).
3. **Double-faced cards** (transform / MDFC) emit one image per face — both
   faces become physical playtest cards. Single-piece layouts (split, flip,
   adventure, aftermath) emit only the front because the card is one piece.
4. Pads each image with **2mm bleed** (edge-replicated) — matches
   tcgplaytest's printing spec exactly.
5. Skips Sideboard / Maybeboard.
6. Writes one PNG per copy to `out/`, plus `out/manifest.json`.

### Overrides — for cards whose art you want from somewhere else

Drop a PNG into `overrides/` named after the card slug:

```
overrides/Mana_Crypt.png         # replaces art for "Mana Crypt"
overrides/Sol_Ring.png           # replaces art for "Sol Ring"
```

The slug uses `[A-Za-z0-9._-]`; everything else collapses to `_`. Slug names
match the prefix of the generated files in `out/` (e.g. `045_Sol_Ring.png` →
override slug is `Sol_Ring`).

## Step 2 — Upload

### Manual (simplest)

Open <https://www.tcgplaytest.com/?view=design> in your normal browser and
drag-drop the contents of `out/` into the Fronts uploader. When the
"Do Your Images Have Print Bleed?" modal appears, choose
**"3mm Bleed added"** (the closest option — our images have 2mm, the site
trims any excess). Then click "Got It" on the trim confirmation. Finish
Customize Back / Preview / Add to Cart.

### Automated

```bash
.venv/bin/python upload.py out/ --user-data ./browser_profile
```

Opens Chromium, uploads all images, picks the bleed option, dismisses the
trim confirmation, clicks Save Draft. Use `--headless` for no window.
`--user-data` keeps Chromium state (incl. saved draft) between runs.

Verified flow:
- Set files on hidden `<input type=file accept="image/*" multiple>`.
- Watch for the "Do Your Images Have Print Bleed?" modal (appears
  asynchronously after Canvas2D processing) and click "3mm Bleed added".
- Wait for "Applying Bleed Settings..." progress to finish.
- Click "Got It" on "Bleed Trimmed Successfully".
- Click "Save Draft".

## Flags

```
fill.py <deck_id>
  -o, --out DIR          Output dir (default: out)
  --overrides DIR        Override-images dir (default: overrides)
  --dpi N                Output DPI (default: 300)
  --bleed-mm FLOAT       Bleed in mm (default: 2.0)
  --no-bleed             Skip bleed padding (raw Scryfall images)
  --workers N            Parallel downloads (default: 6)

upload.py <images_dir>
  --headless             Run without a visible browser window
  --user-data DIR        Persistent browser profile
  --no-save-draft        Don't click Save Draft at the end
  --batch-size N         Upload in chunks of N (default: 0 = all at once)
```

## Notes

- The Archidekt API endpoint `https://archidekt.com/api/decks/<id>/` is
  undocumented but stable. Each card entry has the Scryfall UID under
  `card.uid`; we resolve images via Scryfall by UID.
- tcgplaytest's design page processes uploads client-side via Canvas2D.
  Very large decks should be fine with the default "all at once" batch,
  but if you see drops, try `--batch-size 50`.
