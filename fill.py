"""Archidekt → TCGPlaytest image folder.

Usage:
    python fill.py <archidekt_deck_id> [-o out_dir]

Pipeline:
  1. Fetch deck JSON from Archidekt.
  2. For each card: resolve image URL (override > custom > Scryfall).
  3. Download and write the image as-is — Scryfall PNGs are already at
     the right aspect ratio. The upload step selects tcgplaytest's
     "No Bleed" option and they handle the print bleed on their end.
  4. Write one image per copy. With --pair-backs the output splits into
     out/fronts/ and out/backs/ with matching slot numbers, ready for
     tcgplaytest's Sequential Backs uploader.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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
    if s.isdigit():
        return "archidekt", (s,)
    if re.fullmatch(r"[A-Za-z0-9_-]{12,}", s):
        return "moxfield", (s,)
    raise SystemExit(
        f"Could not recognise '{input_str}' as a supported deck. "
        "Paste an Archidekt, Moxfield, Scryfall, Deckbox, TappedOut, EDHREC, "
        "Deckstats, or MTGGoldfish URL, or use --decklist with a path / '-' "
        "to pipe a plain decklist."
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
    excluded_primary = {
        c["name"] for c in (data.get("categories") or []) if c.get("includedInDeck") is False
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
                qty=int(entry.get("quantity") or 1),
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
        ([^()\n]+?)                              # card name (lazy)
        (?:\s+\(([A-Za-z0-9]{2,6})\)             # optional (SET) — anchors collector
           (?:\s+([\w*★]+))?                     # collector number, only if SET present
        )?
        \s*$""",
    re.VERBOSE,
)
_SECTION_HEADERS = re.compile(
    r"^\s*(?://|#|--)?\s*(sideboard|maybeboard|considering|companion|tokens?|cut|extra|deck|main|mainboard)\s*:?\s*$",
    re.I,
)


def _looks_like_csv(text: str) -> bool:
    """Header-row sniff for a ManaBox / generic CSV decklist. We require
    *both* a name and a quantity column on the first non-empty line —
    a single comma in a card name (`Yidris, Maelstrom Wielder`) on its
    own line must not flip the parser into CSV mode."""
    head = text.lstrip()
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
            qty = int(raw_qty)
        except ValueError:
            # ManaBox occasionally exports `1.0` or empty — surface the
            # count rather than silently dropping rows the user expected.
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
            in_excluded = sec.group(1).lower() not in {"deck", "main", "mainboard"}
            continue
        if line.lstrip().startswith(("//", "#")):
            continue
        if in_excluded or line.lstrip().startswith("SB:"):
            continue
        m = _DECKLIST_LINE.match(line)
        if not m:
            continue
        qty = int(m.group(1))
        name = m.group(2).strip().rstrip(",")
        set_code = (m.group(3) or "").lower() or None
        collector = m.group(4) or None
        out.append((qty, name, set_code, collector))
    return out


def _scryfall_lookup_named(
    name: str, set_code: str | None, collector: str | None, session: requests.Session
) -> str | None:
    """Resolve a card name to a Scryfall UID. Set+collector first (most
    specific), then exact-name lookup as fallback."""
    if set_code and collector:
        _scryfall_wait()
        r = session.get(
            f"https://api.scryfall.com/cards/{set_code}/{collector}",
            headers=UA,
            timeout=20,
        )
        if r.ok:
            return r.json().get("id")
    _scryfall_wait()
    params = {"exact": name}
    if set_code:
        params["set"] = set_code
    r = session.get(
        "https://api.scryfall.com/cards/named",
        headers=UA,
        params=params,
        timeout=20,
    )
    if r.ok:
        return r.json().get("id")
    if r.status_code == 404 and set_code:
        # The deck pinned a set we don't have; retry by name only.
        _scryfall_wait()
        r = session.get(
            "https://api.scryfall.com/cards/named",
            headers=UA,
            params={"exact": name},
            timeout=20,
        )
        if r.ok:
            return r.json().get("id")
    return None


def _jobs_from_decklist(text: str) -> list[CardJob]:
    """Parse decklist text and resolve each line via Scryfall name lookup."""
    parsed = _parse_decklist(text)
    if not parsed:
        raise SystemExit("Could not parse any cards from the decklist.")
    session = requests.Session()
    session.headers.update(UA)
    jobs: list[CardJob] = []
    unresolved: list[str] = []
    for qty, name, set_code, collector in parsed:
        uid = _scryfall_lookup_named(name, set_code, collector, session)
        if not uid:
            unresolved.append(name)
            continue
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
    _scryfall_wait()
    r = session.get(SCRYFALL_CARD.format(uid), timeout=30)
    r.raise_for_status()
    d = r.json()
    _scryfall_payload_cache[uid] = d
    return d


def scryfall_image_urls(uid: str, session: requests.Session) -> tuple[str, str | None]:
    """Return (front_png_url, back_png_url_or_None). For transform / MDFC cards
    this carries both faces so the caller can pair them per-slot."""
    d = scryfall_card_payload(uid, session)
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


def _apply_token_jobs(
    token_jobs: list[CardJob], pair_tokens: bool, pair_backs: bool
) -> tuple[list[CardJob], str | None]:
    """Decide how to fold discovered token jobs into the deck.

    Returns (jobs_to_append, warning_msg). Pure function, no I/O — exists
    so the gating rules (--pair-tokens needs --pair-backs; need ≥2 tokens
    to pair) are independently testable."""
    if not token_jobs:
        return [], None
    if pair_tokens and not pair_backs:
        return token_jobs, "--pair-tokens needs --pair-backs; falling back to single-sided"
    if pair_tokens and pair_backs and len(token_jobs) >= 2:
        return _pair_tokens(token_jobs), None
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
    """Walk every main-deck card's all_parts and return (new_token_jobs,
    failure_messages). Dedupes by (lowercased name, type_line) so
    different printings of "Treasure" / "Faerie Rogue" collapse — but
    legitimately distinct same-named tokens (e.g. the 1/1 W flying Spirit
    vs. the Kamigawa colorless Spirit) stay separate. Cards without a
    scryfall_uid (Archidekt customs) are skipped silently. Network /
    runtime failures on any one card are recorded but don't abort
    discovery.

    `thorough=True` additionally regex-scans each card's oracle_text for
    'create ... token' phrases and resolves each via Scryfall search. This
    catches tokens that Scryfall's all_parts metadata omits — at the cost
    of one search request per unique descriptor. The phrase→UID lookup is
    cached so two cards that mint the same token (e.g. both create
    Treasures) only burn one search request between them."""
    token_jobs: dict[tuple[str, str], CardJob] = {}
    failures: list[str] = []
    phrase_cache: dict[str, str | None] = {}
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
                if not token_uid or token_uid in seen_uids:
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
                if key not in token_jobs:
                    token_jobs[key] = CardJob(
                        name=f"{tok_name} (token)",
                        qty=1,
                        scryfall_uid=token_uid,
                        custom_image_url=None,
                        set_code=None,
                        collector_number=None,
                    )
                    seen_uids.add(token_uid)
    return list(token_jobs.values()), failures


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
    # mints. Cap the name at three words to stop the engine from greedy-
    # eating into a following clause ("named Tuktuk the Returned that's a
    # 5/5..." — name is "Tuktuk the Returned", not the rest).
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
      - (None, None) — search ran cleanly and found nothing (legitimate
        miss, e.g. unresolvable copy-of-X token); cache as None.
      - (None, "reason") — TRANSIENT failure (network error, 5xx, malformed
        JSON, or the phrase didn't yield a meaningful query). Caller MUST
        NOT cache this — a single early blip would otherwise poison every
        later card minting the same token. Reason gets pushed to the
        run-level failures list.

    Scryfall's 404 is treated as a clean "not found" because the search
    endpoint really does return 404 for zero-result queries."""
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
    job: CardJob, override_dir: Path, session: requests.Session
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
    if front_override.exists():
        front = f"file://{front_override.resolve()}"
        back = f"file://{back_override.resolve()}" if back_override.exists() else None
        if back:
            return front, back
    elif job.custom_image_url:
        front, back = job.custom_image_url, None
    elif job.scryfall_uid:
        front, back = scryfall_image_urls(job.scryfall_uid, session)
    else:
        raise RuntimeError(f"No image source for {job.name}")

    if job.pair_back_uid:
        # Tokens are always single-faced, so the second tuple element from
        # scryfall_image_urls is irrelevant.
        back, _ = scryfall_image_urls(job.pair_back_uid, session)
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
    r = session.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


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
            default_back_img = Image.open(args.default_back).convert("RGB")
        else:
            default_back_img = make_default_back()

    if args.decklist:
        if args.decklist == "-":
            text = sys.stdin.read()
        else:
            text = Path(args.decklist).read_text(encoding="utf-8")
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
    if args.include_tokens:
        if args.tokens_thorough:
            print("  (thorough token scan enabled — expect slower discovery)")
        token_jobs, token_failures = _discover_tokens(jobs, session, thorough=args.tokens_thorough)
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
            front_url, back_url = resolve_urls(job, overrides, session)
            front_img = fetch_image(front_url, session)
            back_img = fetch_image(back_url, session) if back_url else None
            return idx, job, front_img, back_img, front_url, back_url, None
        except network_errors as e:
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
        for _copy in range(1, job.qty + 1):
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
