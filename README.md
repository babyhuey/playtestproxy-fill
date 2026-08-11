# playtestproxy-fill

Pull a deck from Archidekt, Moxfield, Scryfall, Deckbox, TappedOut, EDHREC,
mtgdecks.net, a pasted decklist, or a ManaBox / generic CSV export — and produce print-ready
images for [tcgplaytest.com](https://www.tcgplaytest.com/). Includes a Python
CLI, a Playwright auto-uploader, and a no-install web frontend at
<https://babyhuey.github.io/playtestproxy-fill/>.

Verified end-to-end on `https://archidekt.com/decks/21170685/` (commander
deck, 100 main-deck cards, 3 DFCs, paired backs uploaded via the
Sequential Backs feature).

## Web frontend (no install)

Visit <https://babyhuey.github.io/playtestproxy-fill/>, paste your deck
URL (or paste a plain decklist), click Fetch & build, download the ZIP,
and upload it as-is with tcgplaytest's **Upload Deck ZIP** button —
fronts and backs pair automatically by slot number.

The frontend covers the same options as the CLI:

- Skip Sideboard / Maybeboard
- Skip basic lands (use this if you already have a stack of basics)
- Pair backs (DFC face-2 + a default back for everything else)
- Include tokens / emblems (off by default)
- Pair tokens back-to-back (cuts the token portion of the bill in half)
- Thorough token scan — also reads each card's oracle text to catch tokens
  Scryfall's `all_parts` metadata sometimes omits (slower, opt-in)
- Token quantity — print one of each (default), or scale by deck-card minter
  count and optional doubler multiplier (Conservative / Standard / Aggressive)
- Image quality — PNG (best, default) or Large JPG (~10× smaller and faster)
- Custom default back — file upload, URL paste, or pick one of the
  bundled presets (Playtest Copy, Lord of the Proxies, TCGPlaytest logo,
  or the original "You Wouldn't Proxy" meme back)
- Skip cards you own — upload a ManaBox export or any CSV with Name +
  Quantity columns and owned copies are subtracted from the deck before
  building (matched by card name; printing and foil are ignored)
- Minimum price filter — only proxy cards worth at least $X (Scryfall USD
  for the exact printing); cards with no listed price are always kept

The `manifest.json` in the downloaded ZIP now also records `skipped_owned`
and `skipped_cheap` — the cards/copies the two filters above dropped, so
you have a record of what wasn't printed and why.

It also caches Scryfall card data in IndexedDB for 7 days, so re-builds
of the same deck are near-instant after the first run.

Card images download up to 5 at a time instead of one at a time, which
speeds up the network-bound download phase considerably on bigger decks —
a 100-card PNG build that used to fetch every image serially now typically
finishes in a fraction of the time. Scryfall's own metadata lookups still
pace through one shared queue at their ~9 requests/sec limit regardless of
how many images are downloading at once, so the rate cap is respected
either way.

A **Cancel** button appears next to Fetch & build while a build is
running. Clicking it stops new cards from starting; cards already built
stay in the ZIP, which downloads immediately, and the rest show up in the
failures list — click **Retry failed cards** to pick up where you left off.

After a deck builds, an **Add another deck** button appears next to
Download ZIP — click it to paste a second deck URL and append its cards
to the same order (continuous slot numbers, merged stats / cost
estimate, deduped tokens). Useful for batching multiple decks into one
tcgplaytest order.

The **cost estimate** under the results applies tcgplaytest's volume
pricing to your card count. A banner above it carries coupon code
**HUEY** with a Copy button — 10% off the card subtotal, which you paste
into their checkout — and the estimated total is the price *with* that
code applied. Shipping is charged in full on top; use the **Ship to**
dropdown to switch between the US, Canada and international bands. Tax
isn't included.

After a successful build from the Deck URL/ID tab (a fresh build, not an
appended one), a **Copy share link** button appears — it copies a URL
encoding the deck and any non-default options (tokens, image quality,
minimum price, custom back URL, etc.) so someone else can load the same
settings with one click. Loading a share link never auto-builds — it
just pre-fills the form and waits for Fetch & build. File uploads (custom
back image, collection CSV) are local to your machine and can't be
encoded in the link.

## CLI setup

Dependencies are managed with [Poetry](https://python-poetry.org/):

```bash
pipx install poetry                       # or: pip install --user poetry
poetry install --only main                # runtime deps only
poetry run playwright install chromium    # only needed for upload.py
```

For development (tests, lint, pre-commit hooks) run `poetry install` (all
groups) — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Step 1 — Generate images

```bash
poetry run python fill.py <deck_url_or_id> -o out --pair-backs
```

Recognised inputs:
- An **Archidekt** URL (`https://archidekt.com/decks/21170685/...`) or numeric id.
- A **Moxfield** URL (`https://www.moxfield.com/decks/3HyL6_kzbk-sFMs2fchzsg`)
  or alphanumeric public id.
- A **Scryfall** deck URL (`https://scryfall.com/@user/decks/<uuid>`).
  Pulls the deck via Scryfall's plain-text export endpoint.
- A **Deckbox** set URL (`https://deckbox.org/sets/<id>`). Uses Deckbox's
  TCG-format export. Private sets surface a clear error.
- A **TappedOut** URL (`https://tappedout.net/mtg-decks/<slug>/`).
- An **EDHREC** sample-deck URL
  (`https://edhrec.com/deckpreview/<hash>`). Reads the embedded
  `__NEXT_DATA__` blob — no rotating buildId chase.
- An **mtgdecks.net** deck URL
  (`https://mtgdecks.net/<Format>/<archetype-slug>-decklist-by-<player>-<id>`).
  Scrapes the rendered deck-view table for the structured `data-required` /
  `data-card-id` attributes; sideboard cards live in the
  `<th class="type Sideboard">` table and follow the standard CLI / frontend
  sideboard rules.
- A plain decklist via `--decklist <path>` (or `--decklist -` for stdin).
  Accepts MTG Arena exports, MTGO `.dek` XML, **ManaBox / generic CSV**
  with Name + Quantity columns (Section column auto-skipped for
  Sideboard / Maybeboard), "1 Card Name" lines, optional `(SET) NUM`
  trailers, and Sideboard / Maybeboard sections (skipped).

Deckstats and MTGGoldfish URLs are recognised but blocked behind
Cloudflare JS challenges. The tool prints a clear message asking you to
copy the deck text and use `--decklist` instead.

What this does:
1. Fetches the deck — Archidekt and Moxfield via their JSON APIs;
   Scryfall, Deckbox, TappedOut, and EDHREC via their text exports
   (and the EDHREC `__NEXT_DATA__` blob); mtgdecks.net by scraping the
   rendered deck-view table. Plain decklists / ManaBox CSVs come straight
   in via `--decklist`. Card inclusion on Archidekt
   follows the per-deck `categories[].includedInDeck` against each
   card's *primary* (first) category, so a Land tagged "Maybeboard"
   only counts if its primary category is excluded.
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
   dedupes by (token name, type_line), and appends one of each unique
   token at the end. With `--pair-tokens` (and `--pair-backs`), prints
   two unrelated tokens back-to-back on a single card — you only ever
   need one face up at a time, so this halves the token portion of the
   order. `--tokens-thorough` additionally regex-scans each card's
   oracle text for "create N … token [named X]" phrases and resolves
   each via Scryfall search; catches tokens missing from `all_parts`,
   at the cost of one extra search per unique descriptor (cached).
5. Writes one PNG per card slot (the unmodified Scryfall image), plus
   `manifest.json`. tcgplaytest applies the print-bleed expansion on
   their end after upload — no setting to pick.

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

The bundled `assets/default_back.png` (a community "PROXY / Playtest
Copy / NOT FOR SALE" back in the classic card-back style, found via
r/mpcproxies) is used by default. Override with `--default-back PATH` —
`assets/backs/` ships alternates (Lord of the Proxies, the TCGPlaytest
logo back, and the original low-res "You Wouldn't Proxy" meme back).
The web frontend has both a file picker and a URL paste field for the
same purpose.

## Step 2 — Upload

### Deck ZIP (easiest, needs --pair-backs)

tcgplaytest's **Upload Deck ZIP** button (Step 1 of the editor) takes a
zip with `fronts/` + `backs/` folders and pairs them by filename order —
exactly what `--pair-backs` produces. Zip the output and upload it in
one shot:

```bash
cd out && zip -r deck.zip fronts backs
```

Then open <https://www.tcgplaytest.com/?view=design>, click **Upload
Deck ZIP**, and continue at step 3 below. The web frontend's downloaded
ZIP (with paired backs) uploads as-is. Note the zip appends to whatever
draft is already in the editor.

### Manual

Open <https://www.tcgplaytest.com/?view=design> and:

1. Drag everything in `out/fronts/` (or `out/` if no pairing) into the
   Fronts uploader. tcgplaytest expands the print bleed server-side and
   no longer asks about it.
2. Click Next → Customize Back. If you used `--pair-backs`, drag
   everything in `out/backs/` into the **Sequential Backs** uploader
   (image N → slot N) — not the single "Upload Back" box above it.
3. Finish Preview → Add to Cart, and enter coupon code **HUEY** at
   checkout for 10% off the card subtotal.

The upload step has to happen in the same browser tab the editor was
opened in — tcgplaytest's design page is fully client-side and the
draft lives in that tab's storage until checkout.

### Automated

```bash
poetry run python upload.py out/ --user-data ./browser_profile
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
  --image-quality FORMAT  Scryfall image format: 'png' (best, ~1.4 MB/card;
                          default) or 'large' (JPG, ~10x smaller and faster)
  --skip-basic-lands      Drop basic lands from the output
  --pair-backs            Emit out/fronts/ + out/backs/ for Sequential Backs
  --default-back PATH     Custom default back for non-DFC cards (with --pair-backs)
  --include-tokens        Append one of each unique token / emblem the deck creates
  --pair-tokens           Print two tokens back-to-back (needs --include-tokens
                          and --pair-backs); cuts token cost roughly in half
  --tokens-thorough       With --include-tokens, also regex-scan oracle text for
                          'create … token' phrases and resolve each via Scryfall
                          search. Catches tokens missing from Scryfall's all_parts
                          metadata, but slower (one extra search per descriptor).
  --token-qty STRATEGY    How many of each token to print (only with
                          --include-tokens). 'one' = 1 of each (default).
                          'conservative' = number of distinct deck cards minting
                          this token, cap 4. 'standard' adds a 2× doubler
                          multiplier (Doubling Season / Anointed Procession /
                          Mondrak / Annie Joins Up etc.), cap 8. 'aggressive'
                          uses a 4× multiplier when 2 or more doublers are in
                          the deck, cap 12.

upload.py <images_dir>
  --headless              Run without a visible browser window
  --user-data DIR         Persistent browser profile (lets the draft survive)
  --no-save-draft         Don't click Save Draft at the end
  --batch-size N          Upload in chunks of N (default: 0 = all at once)
```

## Notes

- Deck-builder traffic from the web frontend that CORS-blocks
  (Archidekt, Moxfield, Deckbox, TappedOut, EDHREC) is routed through
  [corsproxy.io](https://corsproxy.io/); Scryfall and mtgdecks.net send
  `Access-Control-Allow-Origin: *` and are fetched directly (with a
  corsproxy fallback for mtgdecks in case that ever changes). The
  Archidekt / Moxfield JSON APIs are undocumented but have proven
  stable. The footer of the web app discloses the proxy relationship.
- Card images come straight from Scryfall's CDN. When the CDN returns a
  404 (usually a negatively-cached miss on one format), both the CLI and
  frontend retry the `large` / `normal` JPG variants, then fall back to
  the [images.weserv.nl](https://images.weserv.nl/) proxy (a different
  edge) before giving up on a card.
- Scryfall calls are globally rate-limited to ~10/s (their published cap)
  via a thread-shared lock in the CLI; the frontend routes every card
  lookup through one shared ~110ms gate, so the concurrent build pool
  (several cards download at once — see above) still caps out around 9
  requests/sec in aggregate, not per worker.
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

The bundled default back and the alternates in `assets/backs/` are
community-made proxy backs in the style of the classic card back
(shared on r/mpcproxies and similar communities), plus TCGPlaytest's
own logo back; they are fan art and no copyright claim is made on
them. Replace with `--default-back` if you'd rather not ship them.

If you believe content in this repo infringes your rights, please open
an issue or email the maintainer; we'll act in good faith.
