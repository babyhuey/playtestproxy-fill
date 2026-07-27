"""Archidekt → TCGPlaytest image folder.

Usage:
    python fill.py <archidekt_deck_id> [-o out_dir]

Pipeline:
  1. Fetch deck JSON from Archidekt.
  2. For each card: resolve image URL (override > custom > Scryfall).
  3. Download and write the image as-is — Scryfall PNGs are already at
     the right aspect ratio. tcgplaytest expands the print bleed on their
     end after upload, so nothing here pre-pads.
  4. Write one image per copy. With --pair-backs the output splits into
     out/fronts/ and out/backs/ with matching slot numbers, ready for
     tcgplaytest's Sequential Backs uploader.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path

import requests
from PIL import Image, ImageOps  # noqa: F401  (ImageOps reserved for future use)

ARCHIDEKT_DECK = "https://archidekt.com/api/decks/{}/"
MOXFIELD_DECK = "https://api2.moxfield.com/v3/decks/all/{}"
SCRYFALL_CARD = "https://api.scryfall.com/cards/{}"
SCRYFALL_DECK_EXPORT = "https://api.scryfall.com/decks/{}/export/text"
DECKBOX_EXPORT = "https://deckbox.org/sets/{}/export?format=tcg"

UA = {"User-Agent": "playtestproxy-fill/0.1 (+local tool)"}


@dataclass
class CardJob:
    name: str
    qty: int
    scryfall_uid: str | None
    custom_image_url: str | None
    set_code: str | None
    collector_number: str | None
    # When set (only by --pair-tokens), forces this UID's image as the
    # card's back instead of the DFC face-2 / default playtest back. Used
    # to print two unrelated tokens back-to-back on a single card so a
    # 10-token deck pays for 5 cards instead of 10.
    pair_back_uid: str | None = None


# The five basic-land names plus colorless Wastes, in both regular and
# snow-covered variants. Matching on name is sufficient because Scryfall's
# basic-land prints all carry these exact names regardless of set / frame /
# alt art (Secret Lair etc.). Detecting via Scryfall `type_line` would
# require fetching every card's payload up front, which we'd rather avoid.
_BASIC_LAND_NAMES = frozenset(
    {
        "plains",
        "island",
        "swamp",
        "mountain",
        "forest",
        "wastes",
        "snow-covered plains",
        "snow-covered island",
        "snow-covered swamp",
        "snow-covered mountain",
        "snow-covered forest",
        "snow-covered wastes",
    }
)


def is_basic_land(name: str) -> bool:
    return name.strip().lower() in _BASIC_LAND_NAMES


def slug(name: str) -> str:
    """Filename-safe normalisation. Strips any leading combination of
    `.` and `_` so path-traversal-flavoured input (`..`, `./foo`,
    `../traversal`) can't produce filenames like `..png` that look benign
    but lean on `.` semantics. The numeric slot prefix later already
    neutralises real traversal, but rejecting up front is defense in depth."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    s = re.sub(r"^[._]+", "", s)
    return s[:80] or "card"


# Source detection ---------------------------------------------------------
# Each source: regex(es) that match its deck URL, plus a fetcher that takes
# the bare id and returns CardJob list. The dispatcher picks the first match;
# a pure-numeric id falls through to Archidekt for backwards compat.

_ARCHIDEKT_RE = re.compile(r"archidekt\.com/decks/(\d+)", re.I)
_MOXFIELD_RE = re.compile(r"moxfield\.com/decks/([A-Za-z0-9_-]{12,})", re.I)
# Scryfall deck UUIDs are the standard 8-4-4-4-12 hex shape. The optional
# `@<user>/` segment is part of the canonical URL but stripping it lets us
# support both `scryfall.com/@user/decks/<id>` and `scryfall.com/decks/<id>`.
_SCRYFALL_DECK_RE = re.compile(
    r"scryfall\.com/(?:@[A-Za-z0-9_-]+/)?decks/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
# Deckbox uses numeric set ids in the URL (`/sets/<n>`); the slug after isn't
# required for the export endpoint.
_DECKBOX_RE = re.compile(r"deckbox\.org/sets/(\d+)", re.I)
# mtgdecks.net deck URLs look like
# `mtgdecks.net/<Format>/<archetype-slug>-decklist-by-<player>-<id>` (where
# `<id>` is a 5+ digit numeric deck id). The full path-after-host is what we
# fetch, so capture it as one group; the trailing `-<id>` anchor avoids
# matching unrelated archetype-listing URLs.
_MTGDECKS_RE = re.compile(
    r"mtgdecks\.net/([A-Za-z0-9_-]+/[A-Za-z0-9_.-]+?-\d{4,})",
    re.I,
)


def detect_source(input_str: str) -> tuple[str, tuple[str, ...]]:
    """Return (source_name, args_for_fetcher) for a deck URL or bare id.

    Recognised:
      - Archidekt URL  -> ("archidekt",  (numeric_id,))
      - Moxfield URL   -> ("moxfield",   (public_id,))
      - Scryfall URL   -> ("scryfall",   (uuid,))
      - Deckbox URL    -> ("deckbox",    (numeric_id,))
      - TappedOut URL  -> ("tappedout",  (slug,))
      - Deckstats URL  -> ("deckstats",  (owner_id, deck_id))
      - MTGGoldfish URL-> ("mtggoldfish",(deck_id,))
      - mtgdecks URL   -> ("mtgdecks",   (path,))
      - Numeric id     -> ("archidekt",  (id,))   (legacy CLI usage)
      - Alphanumeric id-> ("moxfield",   (id,))
    """
    s = input_str.strip()
    m = _ARCHIDEKT_RE.search(s)
    if m:
        return "archidekt", (m.group(1),)
    m = _MOXFIELD_RE.search(s)
    if m:
        return "moxfield", (m.group(1),)
    m = _SCRYFALL_DECK_RE.search(s)
    if m:
        return "scryfall", (m.group(1),)
    m = _DECKBOX_RE.search(s)
    if m:
        return "deckbox", (m.group(1),)
    m = _DECKSTATS_RE.search(s)
    if m:
        return "deckstats", (m.group(1), m.group(2))
    m = _TAPPEDOUT_RE.search(s)
    if m:
        return "tappedout", (m.group(1),)
    m = _EDHREC_RE.search(s)
    if m:
        return "edhrec", (m.group(1),)
    m = _MTGGOLDFISH_RE.search(s)
    if m:
        return "mtggoldfish", (m.group(1),)
    m = _MTGDECKS_RE.search(s)
    if m:
        return "mtgdecks", (m.group(1),)
    if s.isdigit():
        return "archidekt", (s,)
    if re.fullmatch(r"[A-Za-z0-9_-]{12,}", s):
        return "moxfield", (s,)
    raise SystemExit(
        f"Could not recognise '{input_str}' as a supported deck. "
        "Paste an Archidekt, Moxfield, Scryfall, Deckbox, TappedOut, EDHREC, "
        "Deckstats, MTGGoldfish, or mtgdecks.net URL, or use --decklist with a "
        "path / '-' to pipe a plain decklist."
    )


def fetch_deck(input_str: str) -> list[CardJob]:
    source, args = detect_source(input_str)
    fetchers = {
        "archidekt": _fetch_archidekt,
        "moxfield": _fetch_moxfield,
        "scryfall": _fetch_scryfall,
        "deckbox": _fetch_deckbox,
        "tappedout": _fetch_tappedout,
        "edhrec": _fetch_edhrec,
        "deckstats": lambda *a: _fetch_cloudflare_blocked("Deckstats", *a),
        "mtggoldfish": lambda *a: _fetch_cloudflare_blocked("MTGGoldfish", *a),
        "mtgdecks": _fetch_mtgdecks,
    }
    fn = fetchers.get(source)
    if fn is None:
        raise SystemExit(f"Unknown source: {source}")
    return fn(*args)


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
    #
    # includedInDeck alone is NOT enough: Archidekt's built-in "Sideboard"
    # category ships with includedInDeck=True, so sideboards would leak in.
    # We also skip any primary category *named* Sideboard / Maybeboard.
    excluded_primary = {
        c["name"] for c in (data.get("categories") or []) if c.get("includedInDeck") is False
    }
    jobs: list[CardJob] = []
    for entry in data.get("cards", []):
        cats = entry.get("categories") or []
        primary = cats[0] if cats else None
        if primary in excluded_primary or (
            primary and primary.lower() in {"sideboard", "maybeboard"}
        ):
            continue
        # Missing quantity defaults to 1, but an explicit 0 (or negative)
        # means "none in deck" — mirror the CSV parser's qty<=0 skip rather
        # than coercing 0 to 1 copy.
        raw_qty = entry.get("quantity")
        qty = 1 if raw_qty is None else int(raw_qty)
        if qty <= 0:
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
                name=oracle.get("name") or card.get("displayName") or f"card-{card.get('id')}",
                qty=qty,
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
    r = requests.get(MOXFIELD_DECK.format(public_id), headers=_MOXFIELD_HEADERS, timeout=30)
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
        for entry in sorted(cards.values(), key=lambda e: (e.get("card") or {}).get("name") or ""):
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


# Plain-text decklist support ---------------------------------------------
# Used for: direct user paste, TappedOut, MTGGoldfish, Deckstats text export,
# MTG Arena export, anything that comes out as "1 Card Name" lines.

_DECKLIST_LINE = re.compile(
    r"""^\s*
        (?:SB:\s*)?                              # Sideboard prefix (some exporters)
        (\d+)                                    # quantity
        \s*[xX]?\s+                              # 'x' separator optional
        (.+?)                                    # card name (lazy; allows parens
                                                 # so "B.F.M. (Big Furry Monster)
                                                 # (UGL) 28" parses correctly —
                                                 # the lazy quantifier backtracks
                                                 # to the LAST `(SET) CN`-shaped
                                                 # tail at end of line)
        (?:\s+\(([A-Za-z0-9]{2,6})\)             # optional (SET) — anchors collector
           (?:\s+([\w★]+))?                      # collector number, only if SET present
        )?
        (?:\s+\*\w+\*)*                          # trailing *F*/*E*/etc. foil/etched markers
        \s*$""",
    re.VERBOSE,
)
# `(\d+)` count suffix — Moxfield's format-specific exports tag every section as
# "Deck (99)", "Companion (0)" etc. Without it, the unrecognised header keeps
# whatever `in_excluded` state the prior recognised header set, which silently
# drops the entire mainboard if Companion/Tokens appear first.
#
# Type-grouping headers (`Creatures`, `Lands`, `Spells`, etc.) are also matched
# so they don't fall through to the bare-name fallback and become qty=1 cards.
# They're handled as TRANSPARENT headers — recognised and skipped, but the
# `in_excluded` state isn't touched, so the cards under them stay in whatever
# section (deck / sideboard / etc.) the prior structural header set.
_SECTION_HEADERS = re.compile(
    r"^\s*(?://|#|--)?\s*"
    r"(sideboard|maybeboard|considering|companion|tokens?|cut|extra"
    r"|deck|main|mainboard|commanders?"
    r"|creatures?|instants?|sorceries|sorcery|artifacts?"
    r"|enchantments?|planeswalkers?|lands?|battles?|spells?)"
    r"(?:\s+\(\d+\))?\s*:?\s*$",
    re.I,
)
# Transparent type-grouping headers — recognised so they don't become bogus
# qty=1 cards, but they don't flip `in_excluded`. Real card names like
# `Land Tax`, `Spell Pierce`, `Creature Guy` aren't affected because the
# section regex anchors `^…$` on the whole trimmed line — only a bare
# `Creatures` or `Lands (24)` matches.
_TYPE_GROUP_HEADERS = frozenset(
    {
        "creature",
        "creatures",
        "instant",
        "instants",
        "sorcery",
        "sorceries",
        "artifact",
        "artifacts",
        "enchantment",
        "enchantments",
        "planeswalker",
        "planeswalkers",
        "land",
        "lands",
        "battle",
        "battles",
        "spell",
        "spells",
    }
)


def _looks_like_csv(text: str) -> bool:
    """Header-row sniff for a ManaBox / generic CSV decklist. We require
    *both* a name and a quantity column on the first non-empty line —
    a single comma in a card name (`Yidris, Maelstrom Wielder`) on its
    own line must not flip the parser into CSV mode.

    ManaBox occasionally ships UTF-8 BOM-prefixed exports; Python's default
    `lstrip()` does NOT strip `\\ufeff`, so the BOM would otherwise glue
    onto the first header (`"\\ufeffName"`) and silently break detection.
    The explicit BOM strip below covers that case."""
    head = text.lstrip("﻿").lstrip()
    first = head.split("\n", 1)[0].strip()
    if "," not in first:
        return False
    columns = [c.strip().strip('"').lower() for c in first.split(",")]
    has_name = any(c in {"name", "card name", "card_name"} for c in columns)
    has_qty = any(c in {"quantity", "qty", "count"} for c in columns)
    return has_name and has_qty


# ManaBox 'Section' / 'Board' values that the deck shouldn't include.
# ManaBox uses title-cased values; we lowercase before comparing.
_CSV_EXCLUDED_SECTIONS = frozenset({"sideboard", "maybeboard", "considering"})


def _parse_csv_decklist(text: str) -> list[tuple[int, str, str | None, str | None]]:
    """Parse a CSV decklist (e.g. ManaBox export). Column header names are
    matched case-insensitively against the known aliases. Rows whose
    Section/Board column flags Sideboard / Maybeboard are skipped, mirroring
    the plain-text parser. Returns the same tuple shape as `_parse_decklist`.
    """
    import csv
    from io import StringIO

    # Strip a leading UTF-8 BOM so the first header doesn't read as
    # `﻿Name` (which would never match the lower-cased lookup
    # below). ManaBox ships these on some platforms.
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        return []
    # Header lookup is case- and whitespace-insensitive — different ManaBox
    # versions ship "Set code" vs "Set Code" vs "set_code".
    headers = {(f or "").strip().lower(): f for f in reader.fieldnames}

    def pick(*aliases: str) -> str | None:
        for a in aliases:
            if a in headers:
                return headers[a]
        return None

    name_key = pick("name", "card name", "card_name")
    qty_key = pick("quantity", "qty", "count")
    if not name_key or not qty_key:
        return []
    set_key = pick("set code", "set_code", "set")
    cn_key = pick("collector number", "collector_number", "number", "collector")
    section_key = pick("section", "board", "type")

    out: list[tuple[int, str, str | None, str | None]] = []
    bad_qty_rows: list[str] = []  # non-integer Quantity values, surfaced after parse
    for row in reader:
        if section_key:
            sec = (row.get(section_key) or "").strip().lower()
            if sec in _CSV_EXCLUDED_SECTIONS:
                continue
        raw_qty = str(row.get(qty_key) or "0").strip()
        try:
            # `int(float(...))` so ManaBox-style "1.0" parses as 1 (matches
            # the JS `Number(...)` parity). True garbage ("abc", "x") still
            # raises ValueError → row surfaces in the WARNING below.
            qty = int(float(raw_qty))
        except ValueError:
            name_for_log = (row.get(name_key) or "?").strip() or "?"
            bad_qty_rows.append(f"{name_for_log!r} (qty={raw_qty!r})")
            continue
        name = (row.get(name_key) or "").strip()
        if qty <= 0 or not name:
            continue
        set_code = (row.get(set_key) or "").strip().lower() or None if set_key else None
        cn = (row.get(cn_key) or "").strip() or None if cn_key else None
        out.append((qty, name, set_code, cn))
    if bad_qty_rows:
        # Match the existing "WARNING: ..." surface used by `_jobs_from_decklist`
        # for unresolved cards. stdout is fine — the CLI prints progress here too.
        print(f"WARNING: skipped {len(bad_qty_rows)} CSV row(s) with non-integer Quantity:")
        for entry in bad_qty_rows[:10]:
            print(f"  - {entry}")
        if len(bad_qty_rows) > 10:
            print(f"  ... and {len(bad_qty_rows) - 10} more")
    return out


def _parse_mtgo_dek(text: str) -> list[tuple[int, str, str | None, str | None]]:
    """Parse Magic Online .dek XML. Returns the same tuple shape as
    `_parse_decklist`. stdlib `ET.fromstring` is safe here for our use:
    Python 3.7.1+ disables external entity resolution by default, and the
    only data we read is element attributes."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise SystemExit(f"Could not parse MTGO .dek XML: {e}") from e
    out: list[tuple[int, str, str | None, str | None]] = []
    saw_cards_element = False
    for el in root.iter():
        if not el.tag.endswith("Cards"):
            continue
        saw_cards_element = True
        if (el.attrib.get("Sideboard") or "").lower() == "true":
            continue
        try:
            qty = int(el.attrib.get("Quantity", "0"))
        except ValueError:
            continue
        name = (el.attrib.get("Name") or "").strip()
        if qty <= 0 or not name:
            continue
        out.append((qty, name, None, None))
    if not saw_cards_element:
        raise SystemExit("MTGO .dek had no <Cards> elements — unexpected schema variant.")
    return out


def _parse_decklist(text: str) -> list[tuple[int, str, str | None, str | None]]:
    """Return [(qty, name, set_code|None, collector|None), ...] for the
    main deck only. Sideboard / maybeboard / token sections are skipped.
    Auto-detects MTGO `.dek` XML and ManaBox-style CSV by their distinctive
    leading bytes / header row."""
    if text.lstrip().startswith("<?xml") or "<Deck" in text[:200]:
        return _parse_mtgo_dek(text)
    if _looks_like_csv(text):
        return _parse_csv_decklist(text)
    out: list[tuple[int, str, str | None, str | None]] = []
    in_excluded = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        sec = _SECTION_HEADERS.match(line)
        if sec:
            name = sec.group(1).lower()
            if name in _TYPE_GROUP_HEADERS:
                # Transparent — skip the header line but leave `in_excluded`
                # alone so cards under `Creatures` / `Lands` etc. stay in
                # whatever section (deck / sideboard) the prior structural
                # header set.
                continue
            in_excluded = name not in {
                "deck",
                "main",
                "mainboard",
                "commander",
                "commanders",
            }
            continue
        if line.lstrip().startswith(("//", "#")):
            continue
        if in_excluded or line.lstrip().startswith("SB:"):
            continue
        m = _DECKLIST_LINE.match(line)
        if m:
            qty = int(m.group(1))
            name = m.group(2).strip().rstrip(",")
            set_code = (m.group(3) or "").lower() or None
            collector = m.group(4) or None
            out.append((qty, name, set_code, collector))
            continue
        # Bare-name fallback (no leading quantity). Lets users paste raw
        # name-per-line lists — wiki dumps, Scryfall search results, set-
        # completion lists — without prefixing each line with "1 ". Lines
        # whose first non-whitespace character is a digit are skipped — a
        # typo like "1Lightning" shouldn't become a card named "1Lightning".
        bare = line.strip()
        if bare and not bare[0].isdigit():
            out.append((1, bare, None, None))
    return out


# Scryfall caps /cards/collection at 75 identifiers per request. The endpoint
# is rate-limited at 2 req/sec (500ms gap) like /cards/named, so a 480-card
# deck resolves in ~7 batched calls instead of 480 per-card lookups.
_SCRYFALL_COLLECTION_BATCH_SIZE = 75
# Explicit inter-batch sleep between /cards/collection calls. The global
# `_scryfall_wait` is calibrated for /cards/<uid> (10 req/sec); collection
# needs the 2 req/sec floor instead, so the loop in `_jobs_from_decklist`
# sleeps this long between batches. Mirrors `SCRYFALL_NAMED_INTERVAL_MS = 550`
# in docs/app.js — the 50ms margin over 500ms keeps us safely under the cap.
_SCRYFALL_COLLECTION_INTERVAL = 0.55


def _index_scryfall_cards(cards: list[dict], target: dict[str, str]) -> None:
    """Index `cards` by every name each one answers to (full name + each
    card-face name), lowercased. DFCs typed by either face resolve to the
    same UID. First-write wins so duplicate face names across prints don't
    clobber each other."""
    for card in cards:
        uid = card.get("id")
        if not uid:
            continue
        keys: list[str] = []
        name = card.get("name")
        if name:
            keys.append(name.lower())
        for face in card.get("card_faces") or []:
            face_name = (face or {}).get("name")
            if face_name:
                keys.append(face_name.lower())
        for key in keys:
            target.setdefault(key, uid)


def _scryfall_identifier_key(identifier: dict) -> tuple[str, str, str]:
    """Hashable, case-insensitive key for a /cards/collection identifier.
    Used to match the `not_found` echoes (which repeat the identifier
    verbatim) back against the identifiers we sent."""
    if "set" in identifier:
        return (
            "set",
            (identifier.get("set") or "").lower(),
            str(identifier.get("collector_number") or "").lower(),
        )
    return ("name", (identifier.get("name") or "").lower(), "")


def _index_scryfall_by_inputs(
    identifiers: list[dict],
    parsed: list[tuple[int, str, str | None, str | None]],
    matches: list[dict],
    not_found: list[dict],
    uid_by_lower_name: dict[str, str],
    uid_by_set_cn: dict[tuple[str, str], str],
) -> None:
    """Pair each /cards/collection match back with the identifier that
    produced it. Scryfall returns `data` in request order with the
    `not_found` entries removed, so walking both lists in lockstep —
    skipping identifiers echoed in `not_found` — recovers the mapping.
    Mirrors `indexScryfallByInputs` in docs/app.js.

    Populates two indices:
      - uid_by_set_cn, keyed by (set, collector_number): two entries that
        share a card name but pin different printings resolve to distinct
        UIDs instead of both collapsing onto the first result.
      - uid_by_lower_name, keyed by the user's *typed* name: fixes silent
        misses when Scryfall canonicalizes the name differently from how
        it was typed (diacritics, curly apostrophes)."""
    remaining: dict[tuple[str, str, str], int] = {}
    for ident in not_found:
        key = _scryfall_identifier_key(ident)
        remaining[key] = remaining.get(key, 0) + 1
    mi = 0
    for ident, (_, name, _set_code, _collector) in zip(identifiers, parsed, strict=True):
        key = _scryfall_identifier_key(ident)
        if remaining.get(key, 0) > 0:
            remaining[key] -= 1
            continue
        if mi >= len(matches):
            break  # defensive: fewer matches than identifiers minus not_found
        uid = matches[mi].get("id")
        mi += 1
        if not uid:
            continue
        if "set" in ident:
            uid_by_set_cn.setdefault((key[1], key[2]), uid)
        else:
            uid_by_lower_name.setdefault(name.lower(), uid)


# Scryfall's docs: a 429 imposes a 30-second IP lockout. 32s puts the next
# attempt safely outside that window when the response omits Retry-After.
_SCRYFALL_429_FALLBACK_SECONDS = 32.0


def _parse_retry_after(header: str | None) -> float | None:
    """Parse a Retry-After header (RFC 7231 §7.1.3). Accepts either
    delta-seconds or an HTTP-date. Returns seconds (float) or None when the
    header is missing or unparseable."""
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        pass
    else:
        return seconds if seconds >= 0 else None
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(header)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _scryfall_request_with_retry(do_request, attempts: int = 3) -> requests.Response:
    """Run `do_request()` (one Scryfall API call) behind the 100ms
    `_scryfall_wait` gate, retrying transient failures. A 429 honours the
    response's `Retry-After` header (or `_SCRYFALL_429_FALLBACK_SECONDS`
    when absent) — Scryfall's 30s lockout eats short exponential backoffs,
    so we wait the full window before the next attempt. 5xx and connection
    errors / timeouts get a short exponential backoff. Anything else (or
    exhausted attempts) raises. Mirrors `scryfallCollection` in docs/app.js."""
    last_error: requests.RequestException | None = None
    for attempt in range(attempts):
        _scryfall_wait()
        try:
            r = do_request()
        except (requests.ConnectionError, requests.Timeout) as err:
            last_error = err
            if attempt < attempts - 1:
                time.sleep(0.5 * (2**attempt))
                continue
            raise
        if r.status_code == 429 and attempt < attempts - 1:
            delay = _parse_retry_after(r.headers.get("Retry-After"))
            if delay is None:
                delay = _SCRYFALL_429_FALLBACK_SECONDS
            time.sleep(delay)
            continue
        try:
            r.raise_for_status()
        except requests.HTTPError as err:
            last_error = err
            if 500 <= r.status_code < 600 and attempt < attempts - 1:
                # Transient 5xx — short backoff then retry.
                time.sleep(0.5 * (2**attempt))
                continue
            raise
        return r
    # Loop exited without returning — re-raise the most recent error so the
    # caller sees the original HTTP failure rather than a silent empty result.
    assert last_error is not None
    raise last_error


def _scryfall_collection_lookup(
    identifiers: list[dict],
    session: requests.Session,
    attempts: int = 3,
) -> tuple[list[dict], list[dict]]:
    """POST a batch of identifiers (≤ 75) to /cards/collection. Returns
    (matches, not_found). Transient-failure handling (429 Retry-After,
    5xx / connection-error backoff) lives in `_scryfall_request_with_retry`."""
    r = _scryfall_request_with_retry(
        lambda: session.post(
            "https://api.scryfall.com/cards/collection",
            headers={**UA, "Content-Type": "application/json"},
            json={"identifiers": identifiers},
            timeout=30,
        ),
        attempts=attempts,
    )
    payload = r.json()
    return payload.get("data") or [], payload.get("not_found") or []


def _jobs_from_decklist(text: str) -> list[CardJob]:
    """Parse decklist text and resolve every line via Scryfall's batched
    `/cards/collection` endpoint. Falls back to a name-only retry for any
    entries that pinned a set/cn but didn't match — same recovery the old
    per-card helper did when (set, cn) returned 404."""
    parsed = _parse_decklist(text)
    if not parsed:
        raise SystemExit("Could not parse any cards from the decklist.")
    session = requests.Session()
    session.headers.update(UA)

    # Build the most-specific identifier we have for each parsed entry.
    identifiers: list[dict] = [
        {"set": s, "collector_number": c} if s and c else {"name": n} for _, n, s, c in parsed
    ]
    uid_by_lower_name: dict[str, str] = {}
    uid_by_set_cn: dict[tuple[str, str], str] = {}
    for i in range(0, len(identifiers), _SCRYFALL_COLLECTION_BATCH_SIZE):
        if i > 0:
            time.sleep(_SCRYFALL_COLLECTION_INTERVAL)
        chunk = identifiers[i : i + _SCRYFALL_COLLECTION_BATCH_SIZE]
        chunk_parsed = parsed[i : i + len(chunk)]
        matches, not_found = _scryfall_collection_lookup(chunk, session)
        _index_scryfall_cards(matches, uid_by_lower_name)
        _index_scryfall_by_inputs(
            chunk, chunk_parsed, matches, not_found, uid_by_lower_name, uid_by_set_cn
        )

    # Second pass for the (set, cn) entries that didn't resolve — retry as
    # bare name (typical: a set/collector hint that's wrong for the named
    # card). Sleep on every iteration (including the first) so the
    # transition from the last main-pass batch is also paced.
    fallback = [
        entry
        for entry in parsed
        if entry[2]
        and entry[3]
        and (entry[2].lower(), entry[3].lower()) not in uid_by_set_cn
        and entry[1].lower() not in uid_by_lower_name
    ]
    for i in range(0, len(fallback), _SCRYFALL_COLLECTION_BATCH_SIZE):
        time.sleep(_SCRYFALL_COLLECTION_INTERVAL)
        chunk_parsed = fallback[i : i + _SCRYFALL_COLLECTION_BATCH_SIZE]
        chunk = [{"name": entry[1]} for entry in chunk_parsed]
        matches, not_found = _scryfall_collection_lookup(chunk, session)
        _index_scryfall_cards(matches, uid_by_lower_name)
        _index_scryfall_by_inputs(
            chunk, chunk_parsed, matches, not_found, uid_by_lower_name, uid_by_set_cn
        )

    jobs: list[CardJob] = []
    unresolved: list[str] = []
    for qty, name, set_code, collector in parsed:
        # set+cn beats name: two entries sharing a name but pinning
        # different printings resolve to distinct UIDs.
        uid = None
        if set_code and collector:
            uid = uid_by_set_cn.get((set_code.lower(), collector.lower()))
        if not uid:
            uid = uid_by_lower_name.get(name.lower())
        if not uid:
            unresolved.append(name)
        jobs.append(
            CardJob(
                name=name,
                qty=qty,
                scryfall_uid=uid,
                custom_image_url=None,
                set_code=set_code,
                collector_number=collector,
            )
        )
    if unresolved:
        # Unresolved entries keep a CardJob with scryfall_uid=None: they
        # fail in the image pipeline ("No image source"), land in
        # manifest.json's failures list, and make the run exit non-zero —
        # a decklist typo must not silently shrink the printed deck.
        print(f"WARNING: {len(unresolved)} cards could not be resolved on Scryfall:")
        for n in unresolved:
            print(f"  - {n}")
    return jobs


_TAPPEDOUT_RE = re.compile(r"tappedout\.net/mtg-decks/([A-Za-z0-9_-]+)", re.I)
_EDHREC_RE = re.compile(r"edhrec\.com/deckpreview/([A-Za-z0-9_-]+)", re.I)
# Deckstats and MTGGoldfish use Cloudflare JS challenges that block plain
# python requests; the URL patterns are detected only so we can give the
# user a clear "paste the decklist instead" message.
_DECKSTATS_RE = re.compile(r"deckstats\.net/decks/(\d+)/(\d+)", re.I)
_MTGGOLDFISH_RE = re.compile(r"mtggoldfish\.com/(?:deck|archetype)/(\d+)", re.I)


def _fetch_tappedout(slug: str) -> list[CardJob]:
    url = f"https://tappedout.net/mtg-decks/{slug}/?fmt=txt"
    r = requests.get(url, headers=UA, timeout=30)
    if 400 <= r.status_code < 500:
        raise SystemExit(
            f"TappedOut returned {r.status_code} for deck '{slug}'. "
            "Check the slug and that the deck is public."
        )
    r.raise_for_status()
    return _jobs_from_decklist(r.text)


def _fetch_scryfall(deck_id: str) -> list[CardJob]:
    """Scryfall public decks ship a plain-text export at /decks/<uuid>/export/text
    with the same `N Card Name` shape the decklist parser already handles.
    Their CDN sets `Content-Type: text/plain` and CORS-allows `*`, so the web
    frontend hits the same endpoint without the proxy."""
    url = SCRYFALL_DECK_EXPORT.format(deck_id)
    r = requests.get(url, headers=UA, timeout=30)
    if 400 <= r.status_code < 500:
        raise SystemExit(
            f"Scryfall returned {r.status_code} for deck '{deck_id}'. "
            "Check the URL and that the deck is public."
        )
    r.raise_for_status()
    return _jobs_from_decklist(r.text)


def _fetch_deckbox(set_id: str) -> list[CardJob]:
    """Deckbox 'sets' (decks and binders) expose a plain-text export at
    /sets/<id>/export?format=tcg with `N Card Name` lines. Private sets
    redirect to a login page; the redirect is followed so we surface that
    as a 4xx-style 'check that the set is public' message rather than
    silently parsing the login HTML to zero cards."""
    url = DECKBOX_EXPORT.format(set_id)
    # allow_redirects=True is the default but we explicitly inspect the
    # final URL: Deckbox redirects private sets to /accounts/login/, which
    # responds 200 with HTML that the decklist parser would yield zero
    # entries from — surface it as a clear error instead.
    r = requests.get(url, headers=UA, timeout=30, allow_redirects=True)
    if 400 <= r.status_code < 500:
        raise SystemExit(
            f"Deckbox returned {r.status_code} for set '{set_id}'. "
            "Check the set id and that it's public."
        )
    r.raise_for_status()
    if "/login" in r.url or "<html" in r.text[:200].lower():
        raise SystemExit(
            f"Deckbox set '{set_id}' looks private (export redirected away from "
            "the text endpoint). Make the set public, or copy the decklist and "
            "use --decklist instead."
        )
    return _jobs_from_decklist(r.text)


_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
    re.S,
)


def _fetch_edhrec(deck_hash: str) -> list[CardJob]:
    """EDHREC's `/deckpreview/<hash>` page is a Next.js SSR app — the deck
    is embedded in the page's `__NEXT_DATA__` JSON blob as a list of plain
    `"N Card Name"` strings, which we hand straight to the decklist parser.

    The buildId-keyed `_next/data/.../*.json` endpoint serves the same
    payload but rotates on every EDHREC deploy. Reading the inline blob is
    stable and one network round-trip."""
    url = f"https://edhrec.com/deckpreview/{deck_hash}"
    r = requests.get(url, headers=UA, timeout=30)
    if 400 <= r.status_code < 500:
        raise SystemExit(
            f"EDHREC returned {r.status_code} for deck '{deck_hash}'. "
            "Check the URL — the hash is the segment after /deckpreview/."
        )
    r.raise_for_status()
    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        raise SystemExit(
            "EDHREC page didn't include the expected __NEXT_DATA__ blob — "
            "their site may have changed shape. Open an issue."
        )
    # TypeError covers EDHREC's "deleted/private" response where
    # `pageProps.data` is `null` and the next subscript fails on None.
    try:
        payload = json.loads(m.group(1))
        deck = payload["props"]["pageProps"]["data"]["deck"]
    except (KeyError, TypeError, ValueError) as e:
        raise SystemExit(f"Could not extract decklist from EDHREC: {e}") from e
    if not isinstance(deck, list) or not deck:
        raise SystemExit("EDHREC payload had no `deck` list — page may be private or deleted.")
    # Schema drift guard: EDHREC ships strings like "1 Sol Ring". If they
    # ever switch to objects, str() silently produces "[object Object]"-
    # equivalents and the decklist parser would emit a deck of 0 cards.
    if not isinstance(deck[0], str) or not re.match(r"^\s*\d+\s+\S", deck[0]):
        raise SystemExit("EDHREC deck shape changed — please open an issue.")
    return _jobs_from_decklist("\n".join(deck))


def _fetch_cloudflare_blocked(site: str, *_: str) -> list[CardJob]:
    raise SystemExit(
        f"{site} sits behind a Cloudflare JS challenge that blocks programmatic "
        "fetches. Open the deck in your browser, copy the decklist, and run with "
        "--decklist - (or save to a file). The text parser handles the standard "
        "MTGA / 'N Card Name' format."
    )


# mtgdecks.net deck pages render every card as a `<tr class="cardItem"
# data-required="N" data-card-id="Name">` row, grouped into `<table>`s
# preceded by a `<th class="type X">` heading. We extract those structured
# attributes and rebuild a plain text decklist so the existing
# `_parse_decklist` / `_jobs_from_decklist` pipeline does the rest. The site
# does not expose a direct text-export endpoint; the lazy-loaded `#arena` tab
# is JS-rendered from the same row data, so scraping the rendered table is
# the most stable surface.
_MTGDECKS_TYPE_OR_CARD = re.compile(
    r'<th\b[^>]*class="type\s+([A-Za-z]+)"'
    r"|"
    r'<tr\b[^>]*class="cardItem"[^>]*>',
    re.I,
)
_MTGDECKS_QTY = re.compile(r'data-required="(\d+)"', re.I)
_MTGDECKS_NAME = re.compile(r'data-card-id="([^"]+)"', re.I)


def _fetch_mtgdecks(path: str) -> list[CardJob]:
    url = f"https://mtgdecks.net/{path.lstrip('/')}"
    r = requests.get(url, headers=UA, timeout=30)
    if 400 <= r.status_code < 500:
        raise SystemExit(
            f"mtgdecks.net returned {r.status_code} for {url}. "
            "Check that the deck URL is correct and the page is public."
        )
    r.raise_for_status()
    main_lines: list[str] = []
    side_lines: list[str] = []
    in_sideboard = False
    for m in _MTGDECKS_TYPE_OR_CARD.finditer(r.text):
        ttype = m.group(1)
        if ttype is not None:
            in_sideboard = ttype.lower() == "sideboard"
            continue
        row = m.group(0)
        qm = _MTGDECKS_QTY.search(row)
        nm = _MTGDECKS_NAME.search(row)
        if not qm or not nm:
            continue
        qty = int(qm.group(1))
        name = html.unescape(nm.group(1)).strip()
        if qty <= 0 or not name:
            continue
        (side_lines if in_sideboard else main_lines).append(f"{qty} {name}")
    if not main_lines and not side_lines:
        raise SystemExit(
            f"mtgdecks.net page parsed but no cards were found at {url}. "
            "The page layout may have changed — please open an issue."
        )
    text = "\n".join(main_lines)
    if side_lines:
        # Emit the sideboard with a section header. `_parse_decklist` always
        # excludes sideboards in the CLI (consistent with --decklist behavior);
        # the frontend's "Skip Sideboard / Maybeboard" checkbox can opt in.
        text += "\n\nSideboard\n" + "\n".join(side_lines)
    return _jobs_from_decklist(text)


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


_scryfall_payload_cache: dict[str, dict] = {}


def scryfall_card_payload(uid: str, session: requests.Session) -> dict:
    """Fetch + cache the full Scryfall card payload for `uid`. Multiple
    callers (image lookup + token discovery) share the same response so we
    don't double-spend the rate-limit budget."""
    cached = _scryfall_payload_cache.get(uid)
    if cached is not None:
        return cached
    r = _scryfall_request_with_retry(lambda: session.get(SCRYFALL_CARD.format(uid), timeout=30))
    d = r.json()
    _scryfall_payload_cache[uid] = d
    return d


def scryfall_image_urls(
    uid: str, session: requests.Session, quality: str = "png"
) -> tuple[str, str | None]:
    """Return (front_url, back_url_or_None). For transform / MDFC cards this
    carries both faces so the caller can pair them per-slot. `quality` picks the
    Scryfall format: "png" (best) or "large" (JPG, ~10x smaller)."""

    def pick(uris: dict) -> str:
        return uris.get(quality) or uris["png"]

    d = scryfall_card_payload(uid, session)
    layout = d.get("layout", "")
    if "image_uris" in d:
        return pick(d["image_uris"]), None
    faces = d.get("card_faces") or []
    if faces and all("image_uris" in f for f in faces):
        if layout in SINGLE_PIECE_LAYOUTS:
            return pick(faces[0]["image_uris"]), None
        return pick(faces[0]["image_uris"]), pick(faces[1]["image_uris"])
    if faces and "image_uris" in faces[0]:
        return pick(faces[0]["image_uris"]), None
    raise RuntimeError(f"No image_uris for {uid} ({d.get('name')})")


def _apply_token_jobs(
    token_jobs: list[CardJob], pair_tokens: bool, pair_backs: bool
) -> tuple[list[CardJob], str | None]:
    """Decide how to fold discovered token jobs into the deck.

    Returns (jobs_to_append, warning_msg). Pure function, no I/O — exists
    so the gating rules (--pair-tokens needs --pair-backs; need ≥2 tokens
    to pair) are independently testable.

    When pairing, qty>1 token jobs are first expanded into singles so
    each copy gets its own pair slot — otherwise smart-qty's extra
    Treasures would collapse into one slot and quietly disappear."""
    if not token_jobs:
        return [], None
    if pair_tokens and not pair_backs:
        return token_jobs, "--pair-tokens needs --pair-backs; falling back to single-sided"
    if pair_tokens and pair_backs:
        expanded = _expand_token_qty(token_jobs)
        if len(expanded) >= 2:
            return _pair_tokens(expanded), None
        return expanded, None
    return token_jobs, None


def _pair_tokens(tokens: list[CardJob]) -> list[CardJob]:
    """Pair N unique tokens into ceil(N/2) cards: token A front, token B back.
    Last card if N is odd: token N front, default playtest back (unset
    pair_back_uid → resolve_urls falls through to the default-back path).

    Each returned CardJob keeps token A's image as its front and uses
    pair_back_uid to point at token B. Names are flattened to 'A / B' so
    the output filenames identify both halves."""
    paired: list[CardJob] = []
    for i in range(0, len(tokens), 2):
        a = tokens[i]
        b = tokens[i + 1] if i + 1 < len(tokens) else None
        paired.append(
            CardJob(
                name=f"{a.name} / {b.name}" if b else a.name,
                qty=1,
                scryfall_uid=a.scryfall_uid,
                custom_image_url=None,
                set_code=None,
                collector_number=None,
                pair_back_uid=b.scryfall_uid if b else None,
            )
        )
    return paired


def _discover_tokens(
    jobs: list[CardJob],
    session: requests.Session,
    thorough: bool = False,
) -> tuple[list[CardJob], list[str]]:
    """Backward-compatible wrapper around `_discover_tokens_with_sources`
    that drops the per-token minter map. Existing callers / tests that
    only need (jobs, failures) keep their original shape."""
    token_jobs, _minters, failures = _discover_tokens_with_sources(jobs, session, thorough=thorough)
    return token_jobs, failures


def _discover_tokens_with_sources(
    jobs: list[CardJob],
    session: requests.Session,
    thorough: bool = False,
) -> tuple[list[CardJob], dict[str, set[str]], list[str]]:
    """Walk every main-deck card's all_parts and return (new_token_jobs,
    minters_by_uid, failure_messages). Dedupes by (lowercased name,
    type_line) so different printings of "Treasure" / "Faerie Rogue"
    collapse — but legitimately distinct same-named tokens (e.g. the 1/1
    W flying Spirit vs. the Kamigawa colorless Spirit) stay separate.
    Cards without a scryfall_uid (Archidekt customs) are skipped
    silently. Network / runtime failures on any one card are recorded
    but don't abort discovery.

    `minters_by_uid` is keyed by the token's Scryfall UID (matches
    `CardJob.scryfall_uid`) and gives the set of deck-card names that
    mint that token. `_apply_token_qty` uses the per-token minter count
    to size the printed output when the user opts into smart quantities.
    A token whose every minter happens to share a name still ends up
    with `minter_count == 1` — the rare-but-possible mirror-deck case.

    `thorough=True` additionally regex-scans each card's oracle_text for
    'create ... token' phrases and resolves each via Scryfall search. This
    catches tokens that Scryfall's all_parts metadata omits — at the cost
    of one search request per unique descriptor. The phrase→UID lookup is
    cached so two cards that mint the same token (e.g. both create
    Treasures) only burn one search request between them."""
    token_jobs: dict[tuple[str, str], CardJob] = {}
    minters: dict[str, set[str]] = {}
    failures: list[str] = []
    # Cache key is (descriptor.lower(), (named or "").lower()) so phrases
    # that share a descriptor but differ only in the `named X` clause stay
    # distinct (an absent `named` becomes the empty string, not None, so
    # the tuple is hashable without special-casing).
    phrase_cache: dict[tuple[str, str], str | None] = {}
    for job in jobs:
        if not job.scryfall_uid:
            continue
        try:
            refs = scryfall_token_refs(job.scryfall_uid, session)
        except (requests.RequestException, RuntimeError) as e:
            failures.append(f"{job.name}: {e}")
            continue
        for token_uid, token_name, token_type in refs:
            key = (token_name.strip().lower(), token_type.strip().lower())
            if key not in token_jobs:
                token_jobs[key] = CardJob(
                    name=f"{token_name} (token)",
                    qty=1,
                    scryfall_uid=token_uid,
                    custom_image_url=None,
                    set_code=None,
                    collector_number=None,
                )
            minters.setdefault(token_uid, set()).add(job.name)
        if thorough:
            try:
                payload = scryfall_card_payload(job.scryfall_uid, session)
            except (requests.RequestException, RuntimeError):
                # Already recorded above by scryfall_token_refs if it failed
                # there; payload caching means a second failure is the same
                # underlying network/HTTP error.
                continue
            # Token UIDs we've already accepted — checked alongside the
            # (name, type_line) dedupe so a token resolved via oracle-scan
            # can't double up with the same token resolved via all_parts
            # if their name/type strings happened to differ subtly.
            seen_uids = {j.scryfall_uid for j in token_jobs.values()}
            for descriptor, named in _oracle_token_phrases(payload):
                cache_key = (descriptor.lower(), (named or "").lower())
                if cache_key not in phrase_cache:
                    uid, error = _resolve_token_phrase(descriptor, named, session)
                    if error is not None:
                        # Transient — DON'T cache (we want a retry on the
                        # next card minting this token). Record so the
                        # user knows the run was incomplete.
                        failures.append(f"{job.name} (thorough): {error}")
                        continue
                    phrase_cache[cache_key] = uid
                token_uid = phrase_cache[cache_key]
                if not token_uid:
                    continue
                # Record this card as a minter even if we won't add a
                # new token job below — minter count still grows.
                minters.setdefault(token_uid, set()).add(job.name)
                if token_uid in seen_uids:
                    continue
                try:
                    tok = scryfall_card_payload(token_uid, session)
                except (requests.RequestException, RuntimeError) as e:
                    # The phrase resolved to a UID, but that UID's payload
                    # fetch failed — surface it. Without this the user
                    # sees fewer tokens than thorough mode found and has
                    # no signal of why.
                    failures.append(f"{job.name} (thorough token {token_uid}): {e}")
                    continue
                tok_name = (tok.get("name") or "").strip()
                tok_type = (tok.get("type_line") or "").strip()
                if not tok_name:
                    continue
                key = (tok_name.lower(), tok_type.lower())
                # Mark the UID seen regardless of whether the (name, type_line)
                # key is new — if all_parts already inserted this token under
                # the same UID, a later phrase resolving to that UID still
                # shouldn't trigger a redundant payload fetch.
                seen_uids.add(token_uid)
                if key not in token_jobs:
                    token_jobs[key] = CardJob(
                        name=f"{tok_name} (token)",
                        qty=1,
                        scryfall_uid=token_uid,
                        custom_image_url=None,
                        set_code=None,
                        collector_number=None,
                    )
    return list(token_jobs.values()), minters, failures


# --- Smart token quantities ---------------------------------------------
# `--token-qty` lets the user scale the printed-token count by how many
# deck cards actually mint each token, optionally factoring in token-
# doubler effects (Doubling Season etc.). Default `one` keeps the
# pre-feature behaviour: exactly one of each unique token.

_TOKEN_QTY_STRATEGIES = ("one", "conservative", "standard", "aggressive")
_TOKEN_QTY_CAPS = {"conservative": 4, "standard": 8, "aggressive": 12}

# Doubler oracle-text fingerprint. "twice that many" covers Doubling Season,
# Anointed Procession, Parallel Lives, Mondrak, Adrix and Nev, Primal Vigor.
# The "one more / one extra" patterns catch Annie Joins Up. We deliberately
# don't try to match every token-multiplying card on Scryfall — the goal is
# a heuristic, not an exhaustive solver.
_TOKEN_DOUBLER_RE = re.compile(
    r"\b(?:twice that many|create[s]? one more|one (?:additional|extra) token|that many plus one)\b",
    re.IGNORECASE,
)


def _count_token_doublers(jobs: list[CardJob], session: requests.Session) -> int:
    """Count distinct deck cards whose oracle_text matches a token-doubler
    fingerprint (Doubling Season, Anointed Procession, Mondrak, Annie Joins
    Up, etc.). Used as a multiplier in `_apply_token_qty`. Cards we can't
    fetch a payload for are skipped silently — a missing doubler degrades
    to a smaller estimate, not a crash."""
    count = 0
    for job in jobs:
        if not job.scryfall_uid:
            continue
        try:
            payload = scryfall_card_payload(job.scryfall_uid, session)
        except (requests.RequestException, RuntimeError):
            continue
        oracles: list[str] = []
        if payload.get("oracle_text"):
            oracles.append(payload["oracle_text"])
        for face in payload.get("card_faces") or []:
            if face.get("oracle_text"):
                oracles.append(face["oracle_text"])
        if any(_TOKEN_DOUBLER_RE.search(t) for t in oracles):
            count += 1
    return count


def _apply_token_qty(
    token_jobs: list[CardJob],
    minters: dict[str, set[str]],
    doubler_count: int,
    strategy: str,
) -> None:
    """Mutate each token CardJob's `qty` according to the chosen strategy.

    Strategies:
      - "one": qty stays at 1 (the discovery default — no-op here).
      - "conservative": qty = number of distinct deck cards that mint this
        token, capped at 4. Doublers ignored.
      - "standard": minter count multiplied by 2 if any doubler is in the
        deck. Capped at 8.
      - "aggressive": minter count multiplied by 2 ** min(doubler_count, 2)
        — so 1 doubler → 2x, ≥2 doublers → 4x. Capped at 12.

    `minters` is keyed by token Scryfall UID and provides the set of
    minter card names per token; `len(minters[uid])` is the per-token
    minter count. A token whose UID isn't in `minters` (defensive — every
    token returned by discovery should be there) defaults to 1 minter."""
    if strategy == "one":
        return
    if strategy not in _TOKEN_QTY_CAPS:
        raise ValueError(f"Unknown token-qty strategy: {strategy!r}")
    cap = _TOKEN_QTY_CAPS[strategy]
    for job in token_jobs:
        minter_count = len(minters.get(job.scryfall_uid or "", {job.name}))
        if strategy == "conservative":
            qty = minter_count
        elif strategy == "standard":
            qty = minter_count * (2 if doubler_count > 0 else 1)
        else:  # aggressive
            qty = minter_count * 2 ** min(doubler_count, 2)
        job.qty = max(1, min(qty, cap))


def _expand_token_qty(token_jobs: list[CardJob]) -> list[CardJob]:
    """Flatten N-qty token jobs into N single-qty copies. `_pair_tokens`
    pairs the input list two-at-a-time, treating each entry as one
    physical card — without expansion a `qty=3` Treasure would collapse
    into a single pair slot, hiding two of the three copies the user
    asked for. Non-pair flow doesn't need this since the slot-writer
    already iterates `range(1, job.qty + 1)`."""
    expanded: list[CardJob] = []
    for job in token_jobs:
        if job.qty <= 1:
            expanded.append(job)
            continue
        for _ in range(job.qty):
            expanded.append(replace(job, qty=1))
    return expanded


# Magic oracle text uses a small fixed set of quantifiers before "token".
# `x` is a variable but always means "≥1 token created" — fine to count.
# Number digits ("create 4 Treasure tokens") are matched separately.
_TOKEN_QUANTIFIERS = (
    "a",
    "an",
    "x",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
)
_TOKEN_PHRASE_RE = re.compile(
    r"\bcreate[s]?\s+"
    # Optional "up to N ..." prefix — Magic uses both "create up to two
    # 1/1 Soldier tokens" and the bare "create two 1/1 ...".
    r"(?:up\s+to\s+)?"
    r"(?:\d+|" + "|".join(_TOKEN_QUANTIFIERS) + r")"
    r"(?:\s+or\s+more)?"
    # Lazy descriptor capped at 120 chars so a missing 'token' suffix can't
    # eat the rest of the oracle text. Whitespace+token closes the match.
    r"\s+(?P<descriptor>.{1,120}?)"
    r"\s+token[s]?"
    # Optional `named X` clause that appears AFTER `token`. Real cards like
    # "create a colorless artifact token named Treasure" hide the actual
    # token name here — without this capture we'd search for the descriptor
    # ("colorless artifact") and miss every Treasure / Clue / Food the card
    # mints. The {0,2} hard cap bounds the name at three words so a phrase
    # like "named Tuktuk the Returned that's a 5/5..." matches just the
    # name and doesn't run the capture into the trailing clause.
    r"(?:\s+named\s+(?P<named>\w[\w'-]*(?:\s+\w[\w'-]*){0,2}))?"
    r"\b",
    re.IGNORECASE,
)
_TOKEN_COLOR_WORDS = {
    "white": "w",
    "blue": "u",
    "black": "b",
    "red": "r",
    "green": "g",
    "colorless": "c",
}
# Words inside a creature-token descriptor that don't constrain identity
# and would derail the Scryfall search if treated as creature subtypes.
_TOKEN_FILLER_WORDS = frozenset(
    {"creature", "artifact", "enchantment", "and", "or", "tapped", "legendary"}
)


def _extract_token_phrases(oracle_text: str) -> list[tuple[str, str | None]]:
    """Pull `create N <descriptor> token [named X]` clauses out of oracle text.
    Returns a list of (descriptor, named_or_None) tuples — the descriptor is
    everything between the quantifier and `token` ('1/1 white Soldier creature'
    or 'Treasure'); `named` is the optional name clause that appears AFTER
    `token` ('colorless artifact token named Treasure' → ('colorless artifact',
    'Treasure')). Trailing 'with X' / 'that's a copy of X' clauses come after
    `token` and the regex's lazy capture skips them. Returns [] for empty/None
    input or no matches."""
    if not oracle_text:
        return []
    out: list[tuple[str, str | None]] = []
    for m in _TOKEN_PHRASE_RE.finditer(oracle_text):
        descriptor = re.sub(r"\s+", " ", m.group("descriptor").strip())
        if not descriptor:
            continue
        named = m.group("named")
        named = re.sub(r"\s+", " ", named.strip()) if named else None
        out.append((descriptor, named))
    return out


def _oracle_token_phrases(payload: dict) -> list[tuple[str, str | None]]:
    """Extract token-create phrases from a Scryfall card payload. Walks
    both the card-level oracle_text and per-face oracle_text (DFCs put
    their rules text on the faces, not the root). See `_extract_token_phrases`
    for the tuple shape."""
    texts: list[str] = []
    if payload.get("oracle_text"):
        texts.append(payload["oracle_text"])
    for face in payload.get("card_faces") or []:
        if face.get("oracle_text"):
            texts.append(face["oracle_text"])
    out: list[tuple[str, str | None]] = []
    for text in texts:
        out.extend(_extract_token_phrases(text))
    return out


def _token_phrase_to_query(phrase: str, named: str | None = None) -> str | None:
    """Convert a captured descriptor (and optional `named X` clause) into a
    Scryfall search query.

    Priority:
      - If `named` was captured, use it directly — it's the token's actual
        name and is more precise than the descriptor.
      - Else if the descriptor carries a P/T ('1/1 white Soldier creature') →
        creature-token query with pt + colors + creature subtypes.
      - Else bare name ('Treasure', 'Food') → exact-name query, with filler
        words ('tapped', 'legendary') and color words stripped first.
        Without that strip, "create a tapped Treasure token" emits
        `name:"tapped Treasure"`, which Scryfall returns nothing for.

    Returns None when the descriptor strips down to nothing actionable
    (e.g. "create a token that's a copy of X" leaves an empty descriptor)
    so the caller can skip cleanly instead of issuing a guaranteed-empty
    search."""
    if named:
        return f'is:token name:"{named.strip().rstrip(".,;:")}"'
    p = phrase.strip().rstrip(".,;:")
    pt_match = re.search(r"\b(\d+)/(\d+)\b", p)
    if pt_match:
        rest = re.sub(r"\b\d+/\d+\b", " ", p, count=1)
        words = re.findall(r"[A-Za-z]+", rest)
        colors: list[str] = []
        types: list[str] = []
        for w in words:
            wl = w.lower()
            if wl in _TOKEN_COLOR_WORDS:
                colors.append(_TOKEN_COLOR_WORDS[wl])
            elif wl in _TOKEN_FILLER_WORDS:
                continue
            elif w[0].isupper():
                types.append(wl)
        terms = ["is:token", f"pt:{pt_match.group(1)}/{pt_match.group(2)}"]
        if colors:
            # `c=` is "colors are exactly" — what we want for tokens, since
            # a 1/1 white Spirit isn't the same as a 1/1 white-and-blue
            # Spirit even though both are "white".
            terms.append(f"c={''.join(colors)}")
        for t in types:
            terms.append(f"t:{t}")
        return " ".join(terms)
    # Bare-name path: strip filler / color words before quoting. "tapped
    # Treasure" → "Treasure"; "colorless artifact" → "" (no useful name —
    # signal to caller via None).
    words = re.findall(r"[A-Za-z]+", p)
    keep = [
        w
        for w in words
        if w.lower() not in _TOKEN_FILLER_WORDS and w.lower() not in _TOKEN_COLOR_WORDS
    ]
    if not keep:
        return None
    return f'is:token name:"{" ".join(keep)}"'


def _resolve_token_phrase(
    phrase: str, named: str | None, session: requests.Session
) -> tuple[str | None, str | None]:
    """Resolve a captured oracle-text descriptor (plus optional `named X`)
    to a Scryfall token UID.

    Returns a (uid, error) tuple:
      - (uid, None) — Scryfall returned a hit; cache it.
      - (None, None) — clean miss; cache it. Three sub-cases:
          * Scryfall search returned 404 (zero results)
          * Scryfall search returned an empty `data` array
          * The descriptor stripped to nothing usable, so no search ran —
            a Scryfall round-trip wouldn't have helped, and re-trying on
            every later card with the same descriptor would just waste
            requests.
      - (None, "reason") — TRANSIENT failure (network error, 5xx, malformed
        JSON). Caller MUST NOT cache — a single early blip would otherwise
        poison every later card minting the same token. Reason gets pushed
        to the run-level failures list."""
    query = _token_phrase_to_query(phrase, named)
    if query is None:
        # Descriptor stripped to nothing meaningful — skip the search
        # entirely. Cache as None (not a transient error) so we don't
        # retry on every subsequent card with the same descriptor.
        return None, None
    _scryfall_wait()
    try:
        r = session.get(
            "https://api.scryfall.com/cards/search",
            params={"q": query, "unique": "cards", "order": "released"},
            headers=UA,
            timeout=20,
        )
    except requests.RequestException as e:
        return None, f"Scryfall search failed for {phrase!r}: {e}"
    if r.status_code == 404:
        return None, None  # genuine "no such token" — cache the miss
    if not r.ok:
        return None, f"Scryfall search returned {r.status_code} for {phrase!r}"
    try:
        data = r.json()
    except ValueError as e:
        return None, f"Scryfall search returned malformed JSON for {phrase!r}: {e}"
    cards = data.get("data") or []
    if not cards:
        return None, None
    return cards[0].get("id"), None


def scryfall_token_refs(uid: str, session: requests.Session) -> list[tuple[str, str, str]]:
    """Return [(token_uid, token_name, token_type_line), ...] for tokens
    this card creates. Filters to `component == "token"`. Scryfall
    classifies emblems as tokens (with `layout == "emblem"`), so this
    catches both. Meld result cards use `component == "meld_result"` and
    are intentionally not included.

    `type_line` is included so the caller can dedupe by (name, type_line);
    name alone collides on legitimately distinct tokens like the 1/1 W
    flying Spirit vs. the Kamigawa colorless Spirit."""
    d = scryfall_card_payload(uid, session)
    return [
        (p["id"], p.get("name") or f"token-{p['id'][:8]}", p.get("type_line") or "")
        for p in (d.get("all_parts") or [])
        if p.get("component") == "token" and p.get("id")
    ]


def resolve_urls(
    job: CardJob, override_dir: Path, session: requests.Session, quality: str = "png"
) -> tuple[str, str | None]:
    """Return (front_url, back_url_or_None). Override files take precedence.
    Override convention: `<slug>.png` for the front, `<slug>.back.png` for the
    back face (DFC). If the override-back doesn't exist but a Scryfall back
    does, the Scryfall back is used.

    If `pair_back_uid` is set (from --pair-tokens) the back becomes the
    front face of that other Scryfall card. An explicit `<slug>.back.png`
    override file still wins, since the user dropping a file in is more
    intentional than an inferred token-pairing."""
    front_override = override_dir / f"{slug(job.name)}.png"
    back_override = override_dir / f"{slug(job.name)}.back.png"

    # Resolve front and back independently so a front-only override on a DFC
    # still picks up the Scryfall back face. Earlier shape collapsed to
    # `(front, None)` whenever front_override existed without back_override,
    # silently dropping the transform back.
    need_scryfall_front = (
        not front_override.exists() and not job.custom_image_url and job.scryfall_uid
    )
    need_scryfall_back = not back_override.exists() and not job.pair_back_uid and job.scryfall_uid
    scry_front: str | None = None
    scry_back: str | None = None
    if need_scryfall_front or need_scryfall_back:
        scry_front, scry_back = scryfall_image_urls(job.scryfall_uid, session, quality)

    if front_override.exists():
        front = f"file://{front_override.resolve()}"
    elif job.custom_image_url:
        front = job.custom_image_url
    elif need_scryfall_front:
        front = scry_front
    else:
        raise RuntimeError(f"No image source for {job.name}")

    if back_override.exists():
        back = f"file://{back_override.resolve()}"
    elif job.pair_back_uid:
        # Tokens are usually single-faced, so the second tuple element from
        # scryfall_image_urls is irrelevant for the common case.
        back, _ = scryfall_image_urls(job.pair_back_uid, session, quality)
    elif need_scryfall_back:
        back = scry_back
    else:
        back = None
    return front, back


_ALLOWED_IMAGE_SCHEMES = ("https://", "file://")


def _scrub_source(url: str | None) -> str | None:
    """Strip the absolute-path portion of a file:// URL down to its basename
    so manifest.json doesn't leak `/home/<user>/...` if shared. We drop the
    folder entirely rather than implying `overrides/` — the actual override
    dir is configurable via --overrides and could itself be sensitive
    (e.g. /home/alice/private-cards/)."""
    if url and url.startswith("file://"):
        return f"file://{Path(url[7:]).name}"
    return url


_SCRYFALL_IMG_RE = re.compile(
    r"^https://cards\.scryfall\.io/(?P<fmt>png|large|normal)/(?P<path>.+)\.(?:png|jpg)(?P<q>\?.*)?$"
)
_SCRYFALL_FORMATS = ("png", "large", "normal")  # quality, highest first


def _scryfall_image_fallbacks(url: str) -> list[str]:
    """Lower-quality formats of the same Scryfall card image.

    Scryfall's CDN occasionally serves a *cached* 404 (Cloudflare negative
    cache, ~1yr TTL) for one exact image URL while the same card's other
    formats are fine — the poisoned entry is keyed by the exact path. Lower
    formats live at different paths (different cache keys), and are never larger
    than what was requested, so retrying them sidesteps a poisoned entry."""
    m = _SCRYFALL_IMG_RE.match(url)
    if not m:
        return []
    fmt, path, q = m.group("fmt"), m.group("path"), m.group("q") or ""
    lower = _SCRYFALL_FORMATS[_SCRYFALL_FORMATS.index(fmt) + 1 :]
    return [
        f"https://cards.scryfall.io/{f}/{path}.{'png' if f == 'png' else 'jpg'}{q}" for f in lower
    ]


def _scryfall_proxy_fallback(url: str) -> str | None:
    """Last resort when EVERY Scryfall format 404s — some edges negatively-cache
    a 404 for all of a card's formats at once, and no cards.scryfall.io URL
    variant can escape it. images.weserv.nl pulls the original image from
    Scryfall's origin via a different edge. Only public card images transit it,
    and only after the direct attempts have all failed."""
    if not url.startswith("https://cards.scryfall.io/"):
        return None
    return "https://images.weserv.nl/?url=" + requests.utils.quote(url[len("https://") :], safe="")


_IMAGE_FETCH_ATTEMPTS = 3


def _get_image_with_retry(url: str, session: requests.Session) -> requests.Response:
    """GET one image URL, retrying transient failures (connection errors,
    timeouts, 429, 5xx) with a brief backoff. 404s and other 4xx return
    immediately — `fetch_image`'s fallback ladder owns those."""
    for attempt in range(_IMAGE_FETCH_ATTEMPTS - 1):
        try:
            r = session.get(url, headers=UA, timeout=60)
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(0.5 * (2**attempt))
            continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            time.sleep(0.5 * (2**attempt))
            continue
        return r
    # Final attempt: no more retries — surface whatever happens to the caller.
    return session.get(url, headers=UA, timeout=60)


def fetch_image(url: str, session: requests.Session) -> Image.Image:
    """Fetch a card image from disk (override) or HTTPS (Scryfall / Archidekt
    custom). Other schemes are rejected — an adversarial deck JSON could
    otherwise send a `customImageUrl` of `http://169.254.169.254/...` or
    `ftp://internal-host/...` and turn this CLI into an SSRF helper. The
    user runs this on their own machine so blast radius is small, but
    rejecting upfront is cheap and right."""
    if not url.startswith(_ALLOWED_IMAGE_SCHEMES):
        raise RuntimeError(f"Refusing image URL with disallowed scheme: {url!r}")
    if url.startswith("file://"):
        return Image.open(url[7:]).convert("RGB")
    # A 404 on a Scryfall image is usually a negatively-cached CDN miss, not a
    # genuinely missing image — try the lower-quality variants (different cache
    # keys), then an image proxy (different edge), before giving up.
    candidates = [url, *_scryfall_image_fallbacks(url)]
    proxied = _scryfall_proxy_fallback(url)
    if proxied:
        candidates.append(proxied)
    r = _get_image_with_retry(candidates[0], session)
    for alt in candidates[1:]:
        if r.status_code != 404:
            break
        r = _get_image_with_retry(alt, session)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _save_png_once(img: Image.Image, path: Path, written: dict[int, Path]) -> None:
    """Write `img` to `path` as PNG. The first write of each unique image
    object pays the (expensive, optimize=True) encode; later writes byte-copy
    the first file. `written` maps id(img) → first written path and is shared
    across the run, so per-copy duplicates and the shared default back all
    reuse a single encode."""
    first = written.get(id(img))
    if first is None:
        img.save(path, "PNG", optimize=True)
        written[id(img)] = path
    else:
        shutil.copyfile(first, path)


def _read_decklist_text(path: Path) -> str:
    """Read a decklist file as UTF-8, falling back to cp1252 for Windows
    exports with smart quotes. Both failing means it isn't a text file."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="cp1252")
        except UnicodeDecodeError as e:
            raise SystemExit(f"Could not decode {path} as UTF-8 or cp1252: {e}") from e


DEFAULT_BACK_FILE = Path(__file__).parent / "assets" / "default_back.png"


def make_default_back() -> Image.Image:
    """Default back image: the bundled `assets/default_back.png` (the
    "You Wouldn't Proxy a Magic Card" meme back). Override via
    --default-back."""
    if not DEFAULT_BACK_FILE.exists():
        raise FileNotFoundError(
            f"Bundled default back missing: {DEFAULT_BACK_FILE}. "
            "Pass --default-back to provide your own."
        )
    return Image.open(DEFAULT_BACK_FILE).convert("RGB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "deck",
        nargs="?",
        help="Deck URL or id. Recognised: Archidekt, Moxfield, Scryfall, "
        "Deckbox, TappedOut, EDHREC, Deckstats, or MTGGoldfish URL — or a "
        "bare Archidekt numeric id / Moxfield public id. Omit when using "
        "--decklist.",
    )
    ap.add_argument(
        "--decklist",
        help="Path to a plain-text decklist (or '-' to read from stdin). "
        "Lines like '1 Sol Ring' or '4 Lightning Bolt (M21) 162'. "
        "Sections labelled Sideboard / Maybeboard / Tokens are skipped.",
    )
    ap.add_argument("-o", "--out", default="out", help="Output directory")
    ap.add_argument("--overrides", default="overrides", help="Override images dir")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument(
        "--skip-basic-lands",
        action="store_true",
        help="Drop basic lands (Plains/Island/Swamp/Mountain/Forest/Wastes "
        "and their snow-covered variants) from the output. Use when you "
        "already have basics in your collection and don't want to print "
        "more of them.",
    )
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
    ap.add_argument(
        "--include-tokens",
        action="store_true",
        help="Append every token / emblem the main deck creates (one of each "
        "unique token, looked up from Scryfall's all_parts) after the deck.",
    )
    ap.add_argument(
        "--pair-tokens",
        action="store_true",
        help="With --include-tokens and --pair-backs, print two tokens "
        "back-to-back on the same physical card (you only ever need one "
        "face up at a time). Cuts token cost roughly in half.",
    )
    ap.add_argument(
        "--tokens-thorough",
        action="store_true",
        help="With --include-tokens, also regex-scan each card's oracle_text "
        "for 'create ... token' phrases and resolve each via Scryfall search. "
        "Catches tokens that Scryfall's all_parts metadata omits, but is "
        "much slower (one extra search request per unique descriptor).",
    )
    ap.add_argument(
        "--token-qty",
        choices=list(_TOKEN_QTY_STRATEGIES),
        default="one",
        help="How many of each token to print (only with --include-tokens). "
        "'one' = 1 of each (default). 'conservative' = number of distinct "
        "deck cards minting this token, cap 4. 'standard' = same, doubled "
        "if any doubler (Doubling Season etc.) is in the deck, cap 8. "
        "'aggressive' = 4x for two or more doublers, cap 12.",
    )
    ap.add_argument(
        "--image-quality",
        choices=("png", "large"),
        default="png",
        help="Scryfall image format: 'png' (best quality, ~1.4 MB/card; default) "
        "or 'large' (JPG, ~10x smaller and faster to download).",
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
    default_back_img: Image.Image | None = None
    if args.pair_backs:
        if args.default_back:
            default_back_path = Path(args.default_back)
            if not default_back_path.is_file():
                raise SystemExit(f"--default-back file not found: {default_back_path}")
            default_back_img = Image.open(default_back_path).convert("RGB")
        else:
            default_back_img = make_default_back()

    if args.decklist:
        if args.decklist == "-":
            text = sys.stdin.read()
        else:
            text = _read_decklist_text(Path(args.decklist))
        print(f"Parsing decklist ({len(text.splitlines())} lines)...")
        jobs = _jobs_from_decklist(text)
    elif args.deck:
        print(f"Fetching deck {args.deck}...")
        jobs = fetch_deck(args.deck)
    else:
        # ap.error sys.exits but CodeQL doesn't model that, so raise
        # explicitly to keep `jobs` flow-sensitive.
        raise SystemExit("supply a deck URL/id or --decklist")
    if args.skip_basic_lands:
        before = sum(j.qty for j in jobs)
        jobs = [j for j in jobs if not is_basic_land(j.name)]
        skipped = before - sum(j.qty for j in jobs)
        if skipped:
            print(f"  skipping {skipped} basic land copies (--skip-basic-lands)")
    total_cards = sum(j.qty for j in jobs)
    print(
        f"  {len(jobs)} unique cards, {total_cards} total copies"
        + (" (with paired backs)" if args.pair_backs else "")
    )

    session = requests.Session()
    session.headers.update(UA)

    if args.tokens_thorough and not args.include_tokens:
        print("  (--tokens-thorough has no effect without --include-tokens; ignoring)")
    if args.token_qty != "one" and not args.include_tokens:
        print(f"  (--token-qty {args.token_qty} has no effect without --include-tokens; ignoring)")
    if args.include_tokens:
        if args.tokens_thorough:
            print("  (thorough token scan enabled — expect slower discovery)")
        token_jobs, minters, token_failures = _discover_tokens_with_sources(
            jobs, session, thorough=args.tokens_thorough
        )
        if args.token_qty != "one":
            doubler_count = _count_token_doublers(jobs, session)
            _apply_token_qty(token_jobs, minters, doubler_count, args.token_qty)
            total_copies = sum(j.qty for j in token_jobs)
            print(
                f"  (--token-qty {args.token_qty}: {total_copies} total token copies "
                f"across {len(token_jobs)} unique tokens"
                + (f", {doubler_count} doubler(s) detected" if doubler_count else "")
                + ")"
            )
        appended, warning = _apply_token_jobs(token_jobs, args.pair_tokens, args.pair_backs)
        if warning:
            print(f"  ({warning})")
        if appended:
            n_unique = len(token_jobs)
            n_appended = len(appended)
            if n_appended < n_unique:
                print(
                    f"  + {n_unique} unique tokens / emblems "
                    f"→ paired into {n_appended} double-sided cards"
                )
            else:
                print(f"  + {n_unique} unique tokens / emblems")
            jobs = list(jobs) + appended
        if token_failures:
            print(f"  (skipped token discovery on {len(token_failures)} cards)")

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
            front_url, back_url = resolve_urls(job, overrides, session, args.image_quality)
            if backs_dir is None:
                # Backs are only written with --pair-backs; skip the
                # (~1.4 MB per DFC) download when it would be discarded.
                back_url = None
            front_img = fetch_image(front_url, session)
            back_img = fetch_image(back_url, session) if back_url else None
            return idx, job, front_img, back_img, front_url, back_url, None
        except network_errors as e:
            return idx, job, None, None, None, None, f"ERROR: {e}"

    # Each card-copy gets its own slot number; slot indices match between
    # fronts/ and backs/ so tcgplaytest's Sequential Backs aligns correctly.
    slot = 0
    # id(image) → first written path, so repeat copies (and the shared
    # default back) byte-copy instead of re-running the PNG encoder.
    written_pngs: dict[int, Path] = {}
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
        for _copy in range(1, job.qty + 1):
            slot += 1
            base = f"{slot:03d}_{slug_name}"
            front_path = fronts_dir / f"{base}.png"
            _save_png_once(front_img, front_path, written_pngs)
            files.append(str(front_path.relative_to(out_dir)))
            if backs_dir is not None:
                this_back = back_img if back_img is not None else default_back_img
                back_path = backs_dir / f"{base}.png"
                _save_png_once(this_back, back_path, written_pngs)
                files.append(str(back_path.relative_to(out_dir)))
        # `file://` sources include the absolute local override path, which
        # leaks the user's home directory if they share the manifest. Scrub
        # to just the basename; HTTPS URLs stay as-is.
        manifest.append(
            {
                "name": job.name,
                "qty": job.qty,
                "has_back": back_img is not None,
                "front_source": _scrub_source(front_url),
                "back_source": _scrub_source(back_url),
                "files": files,
            }
        )
        note = " (DFC, paired back)" if back_img is not None else ""
        print(f"  ok   {tag}{note}")

    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "deck_id": args.deck or f"decklist:{args.decklist}",
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
