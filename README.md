# playtestproxy-fill

Pull a deck from Archidekt or Moxfield and produce print-ready images for tcgplaytest.com.
Includes a Python CLI, a Playwright auto-uploader, and a no-install web
frontend at <https://babyhuey.github.io/playtestproxy-fill/>.

Verified end-to-end on `https://archidekt.com/decks/21170685/`: 99 unique
cards (Commander deck), 100 main-deck copies, 100 paired backs (3 actual
DFC face-2 backs + 97 default backs), Sequential Backs upload aligned.

## Web frontend (no install)

Just visit <https://babyhuey.github.io/playtestproxy-fill/>, paste your
Archidekt or Moxfield deck URL, click Fetch & build, download the ZIP, drag the
`fronts/` and `backs/` folders into tcgplaytest.

The frontend has the same options as the CLI, including a custom-back
upload (or URL paste).

## CLI setup

```bash
python3 -m venv .venv
.venv/bin/pip install requests Pillow playwright
.venv/bin/playwright install chromium
```

## Step 1 — Generate images

```bash
.venv/bin/python fill.py <deck_url_or_id> -o out --pair-backs
```

Recognised inputs:
- An Archidekt URL (`https://archidekt.com/decks/21170685/...`) or numeric id.
- A Moxfield URL (`https://www.moxfield.com/decks/3HyL6_kzbk-sFMs2fchzsg`)
  or alphanumeric public id.
- A TappedOut URL (`https://tappedout.net/mtg-decks/<slug>/`).
- A plain decklist via `--decklist <path>` (or `--decklist -` for stdin).
  Accepts MTG Arena exports, "1 Card Name" lines, optional `(SET) NUM`
  trailers, and Sideboard/Maybeboard sections (skipped).

Deckstats and MTGGoldfish URLs are recognised but blocked behind
Cloudflare JS challenges. The tool prints a clear message asking you to
copy the deck text and use `--decklist` instead.

What this does:
1. Fetches the deck from Archidekt or Moxfield's API. Card inclusion follows Archidekt or Moxfield's
   per-deck `categories[].includedInDeck` against each card's *primary*
   (first) category — so a Land tagged "Maybeboard" only counts if its
   primary category is excluded.
2. For each card, picks an image source in this order:
   1. `overrides/<card_slug>.png` if present (drop your own art here).
      `overrides/<card_slug>.back.png` overrides a DFC's back face.
   2. Archidekt or Moxfield-hosted custom image if the card is a custom.
   3. Scryfall PNG for the **specific printing recorded in the deck**
      (Secret Lair / alt-art selections come through faithfully).
3. Transform/MDFC cards yield two faces — front for the front uploader,
   back paired in slot order. Single-piece layouts (split, flip, adventure,
   aftermath) emit only the front because the card is one physical piece.
4. Pads each image with **2mm bleed** (edge-replicated, sampled inset from
   rounded corners so transparent corners don't bleed white).
5. Writes one PNG per card slot, plus `manifest.json`.

### Output layout

Without `--pair-backs`:
```
out/
  001_<name>.png
  002_<name>.png
  …
  manifest.json
```

With `--pair-backs` (recommended for any deck that has DFCs):
```
out/
  fronts/
    001_<name>.png
    002_<name>.png
    …
  backs/
    001_<back-name>.png    # DFC face-2 art for transform/MDFC
    002_<name>.png         # default back for non-DFC slots
    …
  manifest.json
```

Slot numbers in the two folders match — that's what tcgplaytest's
"Sequential Backs" feature requires.

### Overrides

Drop a PNG into `overrides/` named after the card slug:

```
overrides/Mana_Crypt.png        # replaces art for "Mana Crypt"
overrides/Vraska.back.png       # replaces only the back face of "Vraska"
```

The slug uses `[A-Za-z0-9._-]`; everything else collapses to `_`. The slug
matches the suffix of the generated filename (e.g. `045_Sol_Ring.png` →
override slug `Sol_Ring`).

### Default back (non-DFC slots)

The bundled `assets/default_back.png` (the "You Wouldn't Proxy a Magic
Card" meme back) is used by default. Override with `--default-back PATH`.
The web frontend has both a file picker and a URL paste field for the same
purpose.

## Step 2 — Upload

### Manual

Open <https://www.tcgplaytest.com/?view=design> and:

1. Drag everything in `out/fronts/` (or `out/` if no pairing) into the
   Fronts uploader.
2. When the "Do Your Images Have Print Bleed?" modal appears, choose
   **"3mm Bleed added"** — the closest option to our 2mm output. The site
   silently trims the 1mm excess.
3. Click "Got It" on "Bleed Trimmed Successfully".
4. Click Next → Customize Back. If you used `--pair-backs`, drag everything
   in `out/backs/` into the **Sequential Backs** uploader (image N → slot N).
5. Finish Preview → Add to Cart.

### Automated

```bash
.venv/bin/python upload.py out/ --user-data ./browser_profile
```

Drives the whole flow above. Detects `out/fronts/` + `out/backs/` and
walks both steps automatically. `--user-data` keeps Chromium state
(including the saved draft) between runs.

## Flags

```
fill.py <deck_id>
  -o, --out DIR          Output dir (default: out)
  --overrides DIR        Override-images dir (default: overrides)
  --dpi N                Output DPI (default: 300)
  --bleed-mm FLOAT       Bleed in mm (default: 2.0)
  --no-bleed             Skip bleed padding
  --workers N            Parallel downloads (default: 6)
  --pair-backs           Emit out/fronts/ + out/backs/ for Sequential Backs
  --default-back PATH    Custom default back for non-DFC cards (with --pair-backs)

upload.py <images_dir>
  --headless             Run without a visible browser window
  --user-data DIR        Persistent browser profile
  --no-save-draft        Don't click Save Draft at the end
  --batch-size N         Upload in chunks of N (default: 0 = all at once)
```

## Notes

- The Archidekt or Moxfield API (`https://archidekt.com/api/decks/<id>/`) is undocumented
  but stable. CORS is locked to localhost:3000, so the web frontend routes
  Archidekt or Moxfield through corsproxy.io. Scryfall has open CORS.
- Scryfall calls are globally rate-limited to ~10/s (their published cap)
  via a thread-shared lock.
- tcgplaytest's design page is fully client-side (Canvas2D + IndexedDB);
  there's no upload to their server until checkout, so you can't hand a
  draft from one origin to another. The upload step has to happen in the
  same browser tab as the editor.
