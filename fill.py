"""Archidekt → TCGPlaytest image folder.

Usage:
    python fill.py <archidekt_deck_id> [-o out_dir] [--dpi 300] [--no-bleed]

Pipeline:
  1. Fetch deck JSON from Archidekt.
  2. For each card: resolve image URL (override > custom > Scryfall).
  3. Download.
  4. Pad with 2mm bleed (edge-replicated, sampled inset from rounded corners).
     The upload step picks tcgplaytest's "3mm Bleed added" option — the closest
     match — and the site silently trims the 1mm difference.
  5. Write one image per copy. With --pair-backs the output splits into
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

CARD_W_IN, CARD_H_IN = 2.48, 3.46  # MTG card art area target before bleed
BLEED_MM = 2.0  # tcgplaytest's printing spec is 2mm of bleed
MM_PER_IN = 25.4

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


def detect_source(input_str: str) -> tuple[str, tuple[str, ...]]:
    """Return (source_name, args_for_fetcher) for a deck URL or bare id.

    Recognised:
      - Archidekt URL  -> ("archidekt",  (numeric_id,))
      - Moxfield URL   -> ("moxfield",   (public_id,))
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
    m = _DECKSTATS_RE.search(s)
    if m:
        return "deckstats", (m.group(1), m.group(2))
    m = _TAPPEDOUT_RE.search(s)
    if m:
        return "tappedout", (m.group(1),)
    m = _MTGGOLDFISH_RE.search(s)
    if m:
        return "mtggoldfish", (m.group(1),)
    if s.isdigit():
        return "archidekt", (s,)
    if re.fullmatch(r"[A-Za-z0-9_-]{12,}", s):
        return "moxfield", (s,)
    raise SystemExit(
        f"Could not recognise '{input_str}' as a supported deck. "
        "Paste an Archidekt, Moxfield, TappedOut, Deckstats, or MTGGoldfish URL, "
        "or use --decklist with a path / '-' to pipe a plain decklist."
    )


def fetch_deck(input_str: str) -> list[CardJob]:
    source, args = detect_source(input_str)
    fetchers = {
        "archidekt": _fetch_archidekt,
        "moxfield": _fetch_moxfield,
        "tappedout": _fetch_tappedout,
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


def _parse_decklist(text: str) -> list[tuple[int, str, str | None, str | None]]:
    """Return [(qty, name, set_code|None, collector|None), ...] for the
    main deck only. Sideboard / maybeboard / token sections are skipped."""
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
    jobs: list[CardJob], session: requests.Session
) -> tuple[list[CardJob], list[str]]:
    """Walk every main-deck card's all_parts and return (new_token_jobs,
    failure_messages). Dedupes by lowercased token name so different
    Scryfall printings of "Treasure" or "Faerie Rogue" collapse to one
    job — otherwise --pair-tokens can put visually identical art on both
    sides of a card. Cards without a scryfall_uid (Archidekt customs) are
    skipped silently. Network/runtime failures on any one card are
    recorded but don't abort discovery."""
    token_jobs: dict[str, CardJob] = {}
    failures: list[str] = []
    for job in jobs:
        if not job.scryfall_uid:
            continue
        try:
            refs = scryfall_token_refs(job.scryfall_uid, session)
        except (requests.RequestException, RuntimeError) as e:
            failures.append(f"{job.name}: {e}")
            continue
        for token_uid, token_name in refs:
            key = token_name.strip().lower()
            if key not in token_jobs:
                token_jobs[key] = CardJob(
                    name=f"{token_name} (token)",
                    qty=1,
                    scryfall_uid=token_uid,
                    custom_image_url=None,
                    set_code=None,
                    collector_number=None,
                )
    return list(token_jobs.values()), failures


def scryfall_token_refs(uid: str, session: requests.Session) -> list[tuple[str, str]]:
    """Return [(token_uid, token_name), ...] for tokens this card creates.
    Filters to `component == "token"`. Scryfall classifies emblems as tokens
    (with `layout == "emblem"`), so this catches both. Meld result cards
    use `component == "meld_result"` and are intentionally not included."""
    d = scryfall_card_payload(uid, session)
    return [
        (p["id"], p.get("name") or f"token-{p['id'][:8]}")
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


def pad_bleed(img: Image.Image, dpi: int, bleed_mm: float) -> Image.Image:
    """Resize art to 2.48"x3.46" and extend each edge by bleed_mm using
    edge replication. We sample slightly inset from the visible edge — this
    avoids the rounded-corner artifact on Scryfall PNGs (transparent corners
    flatten to white in RGB, so sampling row 0 or column 0 picks up white
    ends; sampling a few pixels in lands inside the actual card border).
    Final canvas: (2.48 + 2*bleed)" x (3.46 + 2*bleed)"."""
    art_w = int(round(CARD_W_IN * dpi))
    art_h = int(round(CARD_H_IN * dpi))
    bleed_px = int(round((bleed_mm / MM_PER_IN) * dpi))
    art = img.resize((art_w, art_h), Image.LANCZOS)
    canvas = Image.new("RGB", (art_w + 2 * bleed_px, art_h + 2 * bleed_px))
    canvas.paste(art, (bleed_px, bleed_px))
    if bleed_px <= 0:
        return canvas

    # Inset = 2.5% of the shorter dim. ~20px at 300dpi — comfortably past
    # MTG's rounded-corner radius (~3% of card width) and into the border.
    inset = max(8, int(0.025 * min(art_w, art_h)))
    canvas_w, canvas_h = canvas.size

    # Edges — sample a 1px row/col, but offset by `inset` along the edge so
    # we never grab the rounded-corner whitespace.
    top = art.crop((inset, inset, art_w - inset, inset + 1)).resize((art_w - 2 * inset, bleed_px))
    canvas.paste(top, (bleed_px + inset, 0))
    bot = art.crop((inset, art_h - inset - 1, art_w - inset, art_h - inset)).resize(
        (art_w - 2 * inset, bleed_px)
    )
    canvas.paste(bot, (bleed_px + inset, canvas_h - bleed_px))
    left = art.crop((inset, inset, inset + 1, art_h - inset)).resize((bleed_px, art_h - 2 * inset))
    canvas.paste(left, (0, bleed_px + inset))
    right = art.crop((art_w - inset - 1, inset, art_w - inset, art_h - inset)).resize(
        (bleed_px, art_h - 2 * inset)
    )
    canvas.paste(right, (canvas_w - bleed_px, bleed_px + inset))

    # Corner regions: bleed_px + inset on each side. Sample an `inset × inset`
    # block from inside the border so the color matches the actual border tone
    # (black on most MTG cards, white on old-frame cards, gold on Secret Lairs,
    # etc.) rather than the transparent-corner-becomes-white artifact.
    cs = inset  # corner sample size
    for cx, cy, sx, sy in [
        (0, 0, inset, inset),
        (canvas_w - bleed_px - inset, 0, art_w - inset - cs, inset),
        (0, canvas_h - bleed_px - inset, inset, art_h - inset - cs),
        (
            canvas_w - bleed_px - inset,
            canvas_h - bleed_px - inset,
            art_w - inset - cs,
            art_h - inset - cs,
        ),
    ]:
        block = art.crop((sx, sy, sx + cs, sy + cs)).resize((bleed_px + inset, bleed_px + inset))
        canvas.paste(block, (cx, cy))
    return canvas


DEFAULT_BACK_FILE = Path(__file__).parent / "assets" / "default_back.png"


def make_default_back(dpi: int, bleed_mm: float) -> Image.Image:
    """Default back image: the bundled `assets/default_back.png` (the
    "You Wouldn't Proxy a Magic Card" meme back). Resized + bleed-padded
    to match the rest of the deck. Override via --default-back."""
    if not DEFAULT_BACK_FILE.exists():
        raise FileNotFoundError(
            f"Bundled default back missing: {DEFAULT_BACK_FILE}. "
            "Pass --default-back to provide your own."
        )
    img = Image.open(DEFAULT_BACK_FILE).convert("RGB")
    return pad_bleed(img, dpi, bleed_mm)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "deck",
        nargs="?",
        help="Deck URL or id. Recognised: Archidekt, Moxfield, TappedOut, "
        "Deckstats, or MTGGoldfish URL — or a bare Archidekt numeric id / "
        "Moxfield public id. Omit when using --decklist.",
    )
    ap.add_argument(
        "--decklist",
        help="Path to a plain-text decklist (or '-' to read from stdin). "
        "Lines like '1 Sol Ring' or '4 Lightning Bolt (M21) 162'. "
        "Sections labelled Sideboard / Maybeboard / Tokens are skipped.",
    )
    ap.add_argument("-o", "--out", default="out", help="Output directory")
    ap.add_argument("--overrides", default="overrides", help="Override images dir")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument(
        "--no-bleed",
        action="store_true",
        help="Skip bleed padding entirely (output is just the resized art)",
    )
    ap.add_argument("--bleed-mm", type=float, default=BLEED_MM)
    ap.add_argument("--workers", type=int, default=6)
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
            if not args.no_bleed:
                default_back_img = pad_bleed(default_back_img, args.dpi, args.bleed_mm)
        else:
            default_back_img = make_default_back(args.dpi, 0 if args.no_bleed else args.bleed_mm)

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
        ap.error("supply a deck URL/id or --decklist")
    total_cards = sum(j.qty for j in jobs)
    print(
        f"  {len(jobs)} unique cards, {total_cards} total copies"
        + (" (with paired backs)" if args.pair_backs else "")
    )

    session = requests.Session()
    session.headers.update(UA)

    if args.include_tokens:
        token_jobs, token_failures = _discover_tokens(jobs, session)
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
            if not args.no_bleed:
                front_img = pad_bleed(front_img, args.dpi, args.bleed_mm)
            back_img = None
            if back_url:
                back_img = fetch_image(back_url, session)
                if not args.no_bleed:
                    back_img = pad_bleed(back_img, args.dpi, args.bleed_mm)
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
