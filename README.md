# playtestproxy-fill

Pull a deck from Archidekt, Moxfield, TappedOut, EDHREC, or any pasted decklist
and produce print-ready images for [tcgplaytest.com](https://www.tcgplaytest.com/).
Includes a Python CLI, a Playwright auto-uploader, and a no-install web
frontend at <https://babyhuey.github.io/playtestproxy-fill/>.

Verified end-to-end on `https://archidekt.com/decks/21170685/` (commander
deck, 100 main-deck cards, 3 DFCs, paired backs uploaded via the
Sequential Backs feature).

## Web frontend (no install)

Visit <https://babyhuey.github.io/playtestproxy-fill/>, paste your deck
URL (or paste a plain decklist), click Fetch & build, download the ZIP,
extract it, and drag the `fronts/` and `backs/` folders into tcgplaytest.

The frontend covers the same options as the CLI:

- Skip Sideboard / Maybeboard
- Pair backs (DFC face-2 + a default back for everything else)
- Include tokens / emblems (off by default)
- Pair tokens back-to-back (cuts the token portion of the bill in half)
- Custom default back (file upload or URL paste)

It also caches Scryfall card data in IndexedDB for 7 days, so re-builds
of the same deck are near-instant after the first run.

## CLI setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium    # only needed for upload.py
```

For development (tests, lint, pre-commit hooks) use `dev-requirements.txt`
instead — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Step 1 — Generate images

```bash
.venv/bin/python fill.py <deck_url_or_id> -o out --pair-backs
```

Recognised inputs:
- An **Archidekt** URL (`https://archidekt.com/decks/21170685/...`) or numeric id.
- A **Moxfield** URL (`https://www.moxfield.com/decks/3HyL6_kzbk-sFMs2fchzsg`)
  or alphanumeric public id.
- A **TappedOut** URL (`https://tappedout.net/mtg-decks/<slug>/`).
- An **EDHREC** sample-deck URL
  (`https://edhrec.com/deckpreview/<hash>`). Reads the embedded
  `__NEXT_DATA__` blob — no rotating buildId chase.
- A plain decklist via `--decklist <path>` (or `--decklist -` for stdin).
  Accepts MTG Arena exports, "1 Card Name" lines, optional `(SET) NUM`
  trailers, and Sideboard / Maybeboard sections (skipped).

Deckstats and MTGGoldfish URLs are recognised but blocked behind
Cloudflare JS challenges. The tool prints a clear message asking you to
copy the deck text and use `--decklist` instead.

What this does:
1. Fetches the deck JSON from Archidekt / Moxfield, or the text export
   from TappedOut. Card inclusion follows Archidekt's per-deck
   `categories[].includedInDeck` against each card's *primary* (first)
   category, so a Land tagged "Maybeboard" only counts if its primary
   category is excluded.
2. For each card, picks an image source in this order:
   1. `overrides/<card_slug>.png` if present (drop your own art here).
      `overrides/<card_slug>.back.png` overrides a DFC's back face.
   2. Archidekt-hosted custom image if the card is a custom.
   3. Scryfall PNG for the **specific printing recorded in the deck**
      (Secret Lair / alt-art selections come through faithfully).
3. Transform / MDFC cards yield two faces — front for the front uploader,
   back paired in slot order. Single-piece layouts (split, flip,
   adventure, aftermath) emit only the front because the card is one
   physical piece.
4. With `--include-tokens`, walks each card's Scryfall `all_parts`,
   dedupes by token UID, and appends one of each unique token at the
   end. With `--pair-tokens` (and `--pair-backs`), prints two unrelated
   tokens back-to-back on a single card — you only ever need one face
   up at a time, so this halves the token portion of the order.
5. Writes one PNG per card slot (the unmodified Scryfall image), plus
   `manifest.json`. tcgplaytest applies the print-bleed expansion on
   their end after upload — pick **"No Bleed"** in their modal.

### Output layout

Without `--pair-backs`:
```
out/
  001_<name>.png
  002_<name>.png
  …
  manifest.json
```

With `--pair-backs` (recommended for any deck that has DFCs or tokens):
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

The slug uses `[A-Za-z0-9._-]`; everything else collapses to `_`. The
slug matches the suffix of the generated filename (e.g. `045_Sol_Ring.png`
→ override slug `Sol_Ring`).

### Default back (non-DFC slots)

The bundled `assets/default_back.png` (the "You Wouldn't Proxy a Magic
Card" meme back) is used by default. Override with `--default-back PATH`.
The web frontend has both a file picker and a URL paste field for the
same purpose.

## Step 2 — Upload

### Manual

Open <https://www.tcgplaytest.com/?view=design> and:

1. Drag everything in `out/fronts/` (or `out/` if no pairing) into the
   Fronts uploader.
2. When the "Do Your Images Have Print Bleed?" modal appears, choose
   **"No Bleed"** — tcgplaytest applies the print-bleed expansion
   server-side.
3. Click Next → Customize Back. If you used `--pair-backs`, drag
   everything in `out/backs/` into the **Sequential Backs** uploader
   (image N → slot N).
5. Finish Preview → Add to Cart.

The upload step has to happen in the same browser tab the editor was
opened in — tcgplaytest's design page is fully client-side and the
draft lives in that tab's storage until checkout.

### Automated

```bash
.venv/bin/python upload.py out/ --user-data ./browser_profile
```

Drives the whole flow above. Detects `out/fronts/` + `out/backs/` and
walks both steps automatically. `--user-data` keeps Chromium state
(including the saved draft) between runs.

## Flags

```
fill.py [<deck_url_or_id>]
  -o, --out DIR           Output directory (default: out)
  --decklist PATH         Plain-text decklist (or '-' for stdin) instead of a URL
  --overrides DIR         Override-images dir (default: overrides)
  --workers N             Parallel image downloads (default: 6)
  --pair-backs            Emit out/fronts/ + out/backs/ for Sequential Backs
  --default-back PATH     Custom default back for non-DFC cards (with --pair-backs)
  --include-tokens        Append one of each unique token / emblem the deck creates
  --pair-tokens           Print two tokens back-to-back (needs --include-tokens
                          and --pair-backs); cuts token cost roughly in half

upload.py <images_dir>
  --headless              Run without a visible browser window
  --user-data DIR         Persistent browser profile (lets the draft survive)
  --no-save-draft         Don't click Save Draft at the end
  --batch-size N          Upload in chunks of N (default: 0 = all at once)
```

## Notes

- The Archidekt / Moxfield JSON APIs are undocumented but stable. Both
  CORS-block, so the web frontend routes their calls through
  [corsproxy.io](https://corsproxy.io/). Scryfall has open CORS and is
  fetched directly. The footer of the web app discloses the proxy
  relationship.
- Scryfall calls are globally rate-limited to ~10/s (their published cap)
  via a thread-shared lock in the CLI; the frontend uses the same 80–100ms
  gate.
- Scryfall card metadata is cached in IndexedDB (browser) and an
  in-memory dict (CLI) with a 7-day TTL — re-builds of decks whose cards
  you've seen recently skip the metadata phase entirely.
- tcgplaytest's design page is fully client-side (Canvas2D + IndexedDB);
  there's no server-side draft until checkout. That's why the upload
  must happen in the same browser tab as the editor.
- The web app sets a meta CSP and a JS frame-buster — the page can't be
  iframed by a malicious site to phish your deck input.

## Legal

Magic: The Gathering, all card names, and all card images are © Wizards
of the Coast. This project is an unofficial tool for personal playtesting
use and is not affiliated with or endorsed by Wizards of the Coast. Card
images are fetched at runtime from the public
[Scryfall](https://scryfall.com/) API; please respect their
[terms of service](https://scryfall.com/docs/api).

The bundled `assets/default_back.png` ("You Wouldn't Proxy a Magic Card")
is fan parody — derived from a community meme, no copyright claim is
made on it. Replace with `--default-back` if you'd rather not ship it.

If you believe content in this repo infringes your rights, please open
an issue or email the maintainer; we'll act in good faith.
