"""Tests for fill.py — pure helpers + mocked network fetchers."""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path

import pytest
import responses
from PIL import Image

import fill

# --- Pure helpers --------------------------------------------------------


class TestSlug:
    def test_basic(self):
        assert fill.slug("Sol Ring") == "Sol_Ring"

    def test_keeps_dots_dashes_underscores(self):
        assert fill.slug("Yargle_and-Multani.x") == "Yargle_and-Multani.x"

    def test_strips_leading_trailing_underscores(self):
        # The regex collapses non-alnum runs to _, then strip removes edges.
        assert fill.slug("__Sol Ring!!__") == "Sol_Ring"

    def test_double_faced_separator(self):
        # // becomes _ — the slug for face-pairs is intentionally flat.
        assert fill.slug("Venat, Heart of Hydaelyn // Hydaelyn, the Mothercrystal").startswith(
            "Venat_Heart_of_Hydaelyn_Hydaelyn_the_Mothercrystal"
        )

    def test_truncates_at_80(self):
        long = "x" * 200
        assert len(fill.slug(long)) == 80

    def test_only_garbage_returns_card(self):
        assert fill.slug("!@#$%^&*") == "card"

    def test_empty(self):
        assert fill.slug("") == "card"


class TestDetectSource:
    def test_archidekt_url(self):
        assert fill.detect_source("https://archidekt.com/decks/21170685/the_slop_pile") == (
            "archidekt",
            ("21170685",),
        )

    def test_archidekt_url_trailing_path(self):
        assert fill.detect_source("https://archidekt.com/decks/123/foo/bar") == (
            "archidekt",
            ("123",),
        )

    def test_moxfield_url(self):
        assert fill.detect_source("https://www.moxfield.com/decks/3HyL6_kzbk-sFMs2fchzsg") == (
            "moxfield",
            ("3HyL6_kzbk-sFMs2fchzsg",),
        )

    def test_tappedout_url(self):
        assert fill.detect_source(
            "https://tappedout.net/mtg-decks/budget-mono-blue-control-1/"
        ) == (
            "tappedout",
            ("budget-mono-blue-control-1",),
        )

    def test_deckstats_url(self):
        assert fill.detect_source("https://deckstats.net/decks/126143/4305047-some-name") == (
            "deckstats",
            ("126143", "4305047"),
        )

    def test_mtggoldfish_url(self):
        assert fill.detect_source("https://www.mtggoldfish.com/deck/7593392") == (
            "mtggoldfish",
            ("7593392",),
        )

    def test_numeric_id_falls_to_archidekt(self):
        assert fill.detect_source("21170685") == ("archidekt", ("21170685",))

    def test_alphanumeric_id_falls_to_moxfield(self):
        assert fill.detect_source("3HyL6_kzbk-sFMs2fchzsg") == (
            "moxfield",
            ("3HyL6_kzbk-sFMs2fchzsg",),
        )

    def test_short_alphanumeric_rejected(self):
        # 11-char ids could be ambiguous; require ≥12 to avoid false positives.
        with pytest.raises(SystemExit):
            fill.detect_source("abc123")

    def test_garbage_raises(self):
        with pytest.raises(SystemExit):
            fill.detect_source("???")

    def test_whitespace_trimmed(self):
        assert fill.detect_source("  21170685  \n") == ("archidekt", ("21170685",))


class TestParseDecklist:
    def test_basic_lines(self):
        text = "1 Sol Ring\n4 Lightning Bolt\n1 Forest"
        assert fill._parse_decklist(text) == [
            (1, "Sol Ring", None, None),
            (4, "Lightning Bolt", None, None),
            (1, "Forest", None, None),
        ]

    def test_x_separator(self):
        assert fill._parse_decklist("4x Lightning Bolt") == [(4, "Lightning Bolt", None, None)]

    def test_set_and_collector(self):
        result = fill._parse_decklist("1 Sol Ring (CMM) 343")
        assert result == [(1, "Sol Ring", "cmm", "343")]

    def test_set_only_no_collector(self):
        # The (SET) anchor without a trailing number still matches; collector is None.
        assert fill._parse_decklist("1 Sol Ring (CMM)") == [(1, "Sol Ring", "cmm", None)]

    def test_skips_sideboard_section(self):
        text = "1 Sol Ring\n\nSideboard\n2 Negate\n1 Counterspell"
        assert fill._parse_decklist(text) == [(1, "Sol Ring", None, None)]

    def test_skips_maybeboard_and_tokens(self):
        text = "1 Sol Ring\nMaybeboard:\n1 Bosco\nTokens\n1 Treasure"
        assert fill._parse_decklist(text) == [(1, "Sol Ring", None, None)]

    def test_returns_to_main_after_section(self):
        # Many tools emit "Deck" / "Mainboard" before the main list.
        text = "Sideboard\n2 Negate\nMain\n1 Sol Ring\n4 Lightning Bolt"
        assert fill._parse_decklist(text) == [
            (1, "Sol Ring", None, None),
            (4, "Lightning Bolt", None, None),
        ]

    def test_inline_sb_prefix_skipped(self):
        # MTGO-style "SB:" sideboard markers are excluded even outside a section.
        assert fill._parse_decklist("1 Sol Ring\nSB: 2 Negate") == [(1, "Sol Ring", None, None)]

    def test_blank_lines_and_comments(self):
        text = "// MTGA export\n\n1 Sol Ring\n# a comment\n4 Lightning Bolt"
        assert fill._parse_decklist(text) == [
            (1, "Sol Ring", None, None),
            (4, "Lightning Bolt", None, None),
        ]

    def test_two_word_card_isnt_split_into_collector(self):
        # Earlier-version regex bug: "1 Sol Ring" parsed as qty=1, name="Sol", collector="Ring".
        result = fill._parse_decklist("1 Sol Ring")
        assert result == [(1, "Sol Ring", None, None)]

    def test_dfc_name(self):
        assert fill._parse_decklist(
            "1 Venat, Heart of Hydaelyn // Hydaelyn, the Mothercrystal"
        ) == [
            (1, "Venat, Heart of Hydaelyn // Hydaelyn, the Mothercrystal", None, None),
        ]

    def test_unparseable_line_skipped(self):
        text = "garbage line\n1 Sol Ring\nmore garbage"
        assert fill._parse_decklist(text) == [(1, "Sol Ring", None, None)]

    def test_empty(self):
        assert fill._parse_decklist("") == []


class TestScryfallWait:
    def test_first_call_no_sleep(self):
        # The very first call may sleep depending on prior state; clamp the
        # module-level state to ensure no wait, then verify a fast call.
        fill._scryfall_last_call = 0.0
        start = time.monotonic()
        fill._scryfall_wait()
        elapsed = time.monotonic() - start
        assert elapsed < 0.05  # well under the 100ms interval

    def test_back_to_back_calls_throttled(self):
        # Two calls in immediate succession: the second waits for the interval.
        fill._scryfall_last_call = 0.0
        fill._scryfall_wait()
        start = time.monotonic()
        fill._scryfall_wait()
        elapsed = time.monotonic() - start
        # Allow a tiny scheduling slack but require at least most of the interval.
        assert elapsed >= fill._SCRYFALL_MIN_INTERVAL * 0.9


# --- Image pipeline ------------------------------------------------------


class TestPadBleed:
    def _solid_image(self, w=745, h=1040, color=(50, 100, 200)):
        return Image.new("RGB", (w, h), color)

    def test_output_dimensions(self):
        img = self._solid_image()
        out = fill.pad_bleed(img, dpi=300, bleed_mm=2.0)
        # 2.48" + 2*(2/25.4)" at 300 dpi
        expected_w = int(round(2.48 * 300)) + 2 * int(round((2 / 25.4) * 300))
        expected_h = int(round(3.46 * 300)) + 2 * int(round((2 / 25.4) * 300))
        assert out.size == (expected_w, expected_h)

    def test_no_bleed_short_circuits(self):
        img = self._solid_image()
        out = fill.pad_bleed(img, dpi=300, bleed_mm=0)
        # Just the resized art, no extension.
        assert out.size == (int(round(2.48 * 300)), int(round(3.46 * 300)))

    def test_corner_color_matches_inner_border(self):
        # A border-coloured frame around a different-coloured centre — the
        # bleed corner should pick up the BORDER colour, not the centre,
        # because we sample inset (past the rounded-corner area) into the border.
        border = (10, 10, 10)
        img = Image.new("RGB", (745, 1040), border)
        # Paint the centre a different colour, but leave the outer 12% as border.
        centre_color = (255, 255, 255)
        centre = Image.new("RGB", (520, 815), centre_color)
        img.paste(centre, (112, 112))
        out = fill.pad_bleed(img, dpi=300, bleed_mm=2.0)
        # Sample top-left corner pixel of the bleed canvas: should be border, not white.
        assert out.getpixel((1, 1)) != centre_color
        # Allow JPEG-style tolerance (resampling may shift by a few units).
        r, g, b = out.getpixel((1, 1))
        assert r < 60 and g < 60 and b < 60


class TestMakeDefaultBack:
    def test_returns_padded_image(self):
        img = fill.make_default_back(dpi=300, bleed_mm=2.0)
        expected_w = int(round(2.48 * 300)) + 2 * int(round((2 / 25.4) * 300))
        expected_h = int(round(3.46 * 300)) + 2 * int(round((2 / 25.4) * 300))
        assert img.size == (expected_w, expected_h)


# --- Mocked fetchers -----------------------------------------------------


@pytest.fixture
def archidekt_payload():
    return {
        "name": "Test Deck",
        "categories": [
            {"name": "Commander", "includedInDeck": True},
            {"name": "Maybeboard", "includedInDeck": False},
            {"name": "Land", "includedInDeck": True},
        ],
        "cards": [
            {
                "categories": ["Commander"],
                "quantity": 1,
                "card": {
                    "uid": "abc-123",
                    "oracleCard": {"name": "Jodah, the Unifier"},
                    "edition": {"editioncode": "sld"},
                    "collectorNumber": "1",
                },
            },
            {
                # primary = Land, also tagged Maybeboard — should be INCLUDED
                # (mirrors the real bug in production).
                "categories": ["Land", "Maybeboard"],
                "quantity": 1,
                "card": {
                    "uid": "def-456",
                    "oracleCard": {"name": "City of Brass"},
                    "edition": {"editioncode": "med"},
                    "collectorNumber": "23",
                },
            },
            {
                # primary = Maybeboard — should be SKIPPED.
                "categories": ["Maybeboard"],
                "quantity": 1,
                "card": {
                    "uid": "ghi-789",
                    "oracleCard": {"name": "Bosco, Just a Bear"},
                    "edition": {"editioncode": "lci"},
                    "collectorNumber": "5",
                },
            },
        ],
    }


@responses.activate
def test_fetch_archidekt_skips_only_excluded_primary(archidekt_payload):
    responses.add(
        responses.GET,
        "https://archidekt.com/api/decks/123/",
        json=archidekt_payload,
        status=200,
    )
    jobs = fill._fetch_archidekt("123")
    names = sorted(j.name for j in jobs)
    assert names == ["City of Brass", "Jodah, the Unifier"]


@responses.activate
def test_fetch_archidekt_4xx_raises_systemexit():
    responses.add(
        responses.GET,
        "https://archidekt.com/api/decks/999/",
        json={"detail": "Not found"},
        status=404,
    )
    with pytest.raises(SystemExit, match="Archidekt returned 404"):
        fill._fetch_archidekt("999")


@responses.activate
def test_fetch_moxfield_walks_deck_boards_and_sorts():
    payload = {
        "name": "Test Mox",
        "boards": {
            "commanders": {
                "cards": {"k1": {"quantity": 1, "card": {"name": "Zora", "scryfall_id": "uid-z"}}}
            },
            "mainboard": {
                "cards": {
                    "k2": {
                        "quantity": 4,
                        "card": {"name": "Beta", "scryfall_id": "uid-b", "set": "m21", "cn": "1"},
                    },
                    "k3": {"quantity": 1, "card": {"name": "Alpha", "scryfall_id": "uid-a"}},
                }
            },
            "sideboard": {
                "cards": {
                    "sb": {"quantity": 1, "card": {"name": "Skipped", "scryfall_id": "uid-s"}}
                }
            },
            "tokens": {
                "cards": {
                    "tk": {"quantity": 1, "card": {"name": "AlsoSkipped", "scryfall_id": "uid-t"}}
                }
            },
        },
    }
    responses.add(
        responses.GET,
        "https://api2.moxfield.com/v3/decks/all/abc",
        json=payload,
        status=200,
    )
    jobs = fill._fetch_moxfield("abc")
    # Commanders first (board ordering), then mainboard sorted by name.
    assert [(j.name, j.qty) for j in jobs] == [
        ("Zora", 1),
        ("Alpha", 1),
        ("Beta", 4),
    ]
    assert jobs[0].scryfall_uid == "uid-z"


@responses.activate
def test_fetch_tappedout_returns_jobs():
    decklist = "3 Lightning Bolt\n1 Sol Ring\n\nSideboard\n2 Negate\n"
    responses.add(
        responses.GET,
        "https://tappedout.net/mtg-decks/test-slug/",
        body=decklist,
        status=200,
        content_type="text/plain",
    )
    # Mock Scryfall name-resolution.
    for name, uid in [("Lightning Bolt", "lb-uid"), ("Sol Ring", "sr-uid")]:
        responses.add(
            responses.GET,
            "https://api.scryfall.com/cards/named",
            json={"id": uid, "name": name},
            status=200,
        )
    jobs = fill._fetch_tappedout("test-slug")
    assert {(j.name, j.qty) for j in jobs} == {("Lightning Bolt", 3), ("Sol Ring", 1)}


@responses.activate
def test_scryfall_lookup_set_and_collector_first():
    """When set+collector are known, hit the more specific endpoint first
    and skip the named fallback."""
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/m21/162",
        json={"id": "uid-set-cn"},
        status=200,
    )
    uid = fill._scryfall_lookup_named("Lightning Bolt", "m21", "162", fill.requests.Session())
    assert uid == "uid-set-cn"


@responses.activate
def test_scryfall_lookup_falls_back_when_set_unknown():
    """Set+collector misses; named-with-set 404s; named-only finally hits."""
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/zzz/999",
        json={"details": "no such card"},
        status=404,
    )
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/named",
        json={"details": "no such printing"},
        status=404,
    )
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/named",
        json={"id": "uid-fallback"},
        status=200,
    )
    uid = fill._scryfall_lookup_named("Sol Ring", "zzz", "999", fill.requests.Session())
    assert uid == "uid-fallback"


def test_make_default_back_missing_file_raises(monkeypatch, tmp_path):
    """If the bundled default_back.png is missing, give a clear error."""
    monkeypatch.setattr(fill, "DEFAULT_BACK_FILE", tmp_path / "absent.png")
    with pytest.raises(FileNotFoundError, match="Bundled default back missing"):
        fill.make_default_back(dpi=300, bleed_mm=2.0)


@responses.activate
def test_jobs_from_decklist_warns_on_unresolved(capsys):
    text = "1 Real Card\n1 Bogus Made Up Card\n"
    # First call resolves; second returns 404.
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/named",
        json={"id": "uid-1"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/named",
        json={"details": "not found"},
        status=404,
    )
    jobs = fill._jobs_from_decklist(text)
    assert len(jobs) == 1
    assert jobs[0].name == "Real Card"
    out = capsys.readouterr().out
    assert "could not be resolved" in out
    assert "Bogus Made Up Card" in out


@responses.activate
def test_scryfall_card_payload_caches():
    """A second call for the same UID hits the cache, not the network."""
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/uid-cache",
        json={"id": "uid-cache", "name": "Cached"},
        status=200,
    )
    fill._scryfall_payload_cache.clear()
    s = fill.requests.Session()
    a = fill.scryfall_card_payload("uid-cache", s)
    b = fill.scryfall_card_payload("uid-cache", s)
    assert a is b
    assert len(responses.calls) == 1  # only one HTTP request


@responses.activate
def test_scryfall_token_refs_extracts_tokens_only():
    fill._scryfall_payload_cache.clear()
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/bitter",
        json={
            "id": "bitter",
            "name": "Bitterblossom",
            "all_parts": [
                {"id": "tok-1", "name": "Faerie Rogue", "component": "token"},
                {"id": "self", "name": "Bitterblossom", "component": "combo_piece"},
                {"id": "tok-2", "name": "Treasure", "component": "token"},
            ],
        },
        status=200,
    )
    refs = fill.scryfall_token_refs("bitter", fill.requests.Session())
    assert refs == [("tok-1", "Faerie Rogue"), ("tok-2", "Treasure")]


@responses.activate
def test_scryfall_token_refs_no_tokens():
    fill._scryfall_payload_cache.clear()
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/bolt",
        json={"id": "bolt", "name": "Lightning Bolt"},
        status=200,
    )
    assert fill.scryfall_token_refs("bolt", fill.requests.Session()) == []


@responses.activate
def test_scryfall_token_refs_skips_self_reference():
    """A combo_piece self-reference must not be returned as a token."""
    fill._scryfall_payload_cache.clear()
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/self-ref",
        json={
            "id": "self-ref",
            "name": "Self",
            "all_parts": [
                {"id": "self-ref", "name": "Self", "component": "combo_piece"},
            ],
        },
        status=200,
    )
    assert fill.scryfall_token_refs("self-ref", fill.requests.Session()) == []


@responses.activate
def test_token_discovery_dedupes_across_cards():
    """Two main-deck cards both producing the same token UID yield one
    entry — exercises the dedupe-by-UID logic the way main() drives it."""
    fill._scryfall_payload_cache.clear()
    for parent_id in ["card-a", "card-b"]:
        responses.add(
            responses.GET,
            f"https://api.scryfall.com/cards/{parent_id}",
            json={
                "id": parent_id,
                "name": parent_id,
                "all_parts": [
                    {"id": "treasure-uid", "name": "Treasure", "component": "token"},
                ],
            },
            status=200,
        )
    s = fill.requests.Session()
    seen: dict[str, fill.CardJob] = {}
    for parent in ["card-a", "card-b"]:
        for tok_uid, tok_name in fill.scryfall_token_refs(parent, s):
            if tok_uid not in seen:
                seen[tok_uid] = fill.CardJob(
                    name=f"{tok_name} (token)",
                    qty=1,
                    scryfall_uid=tok_uid,
                    custom_image_url=None,
                    set_code=None,
                    collector_number=None,
                )
    assert list(seen) == ["treasure-uid"]
    assert seen["treasure-uid"].name == "Treasure (token)"


@responses.activate
def test_discover_tokens_end_to_end():
    """`_discover_tokens` walks every job, skips no-UID jobs without an HTTP
    call, dedupes tokens across cards, and reports network failures rather
    than aborting the run. Exercises the production code path the way
    main() drives it."""
    fill._scryfall_payload_cache.clear()
    # card-a and card-b both emit the same Treasure UID — must dedupe.
    for cid in ("card-a", "card-b"):
        responses.add(
            responses.GET,
            f"https://api.scryfall.com/cards/{cid}",
            json={
                "id": cid,
                "all_parts": [
                    {"id": "treasure-uid", "name": "Treasure", "component": "token"},
                ],
            },
            status=200,
        )
    # card-c: 500 error → recorded as failure but doesn't abort discovery.
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/card-c",
        json={"err": "boom"},
        status=500,
    )

    def J(name, uid):
        return fill.CardJob(
            name=name,
            qty=1,
            scryfall_uid=uid,
            custom_image_url=None,
            set_code=None,
            collector_number=None,
        )

    jobs = [
        J("CardA", "card-a"),
        J("Custom", None),  # no UID — skipped without HTTP
        J("CardB", "card-b"),
        J("CardC", "card-c"),
    ]
    new_jobs, failures = fill._discover_tokens(jobs, fill.requests.Session())

    assert [j.name for j in new_jobs] == ["Treasure (token)"]
    assert new_jobs[0].scryfall_uid == "treasure-uid"
    assert len(failures) == 1
    assert "CardC" in failures[0]
    # No HTTP call for the Custom job: only card-a, card-b, card-c got hit.
    assert len(responses.calls) == 3


# --- Token pairing -------------------------------------------------------


def _tok(name, uid):
    return fill.CardJob(
        name=name,
        qty=1,
        scryfall_uid=uid,
        custom_image_url=None,
        set_code=None,
        collector_number=None,
    )


class TestPairTokens:
    def test_empty(self):
        assert fill._pair_tokens([]) == []

    def test_single(self):
        # 1 token → 1 card, unpaired (pair_back_uid stays None so the
        # default playtest back wins for the second face).
        out = fill._pair_tokens([_tok("Treasure", "uid-t")])
        assert len(out) == 1
        assert out[0].name == "Treasure"
        assert out[0].scryfall_uid == "uid-t"
        assert out[0].pair_back_uid is None

    def test_pair(self):
        out = fill._pair_tokens([_tok("Treasure", "uid-t"), _tok("Goblin", "uid-g")])
        assert len(out) == 1
        assert out[0].name == "Treasure / Goblin"
        assert out[0].scryfall_uid == "uid-t"
        assert out[0].pair_back_uid == "uid-g"

    def test_odd_three(self):
        out = fill._pair_tokens([_tok("A", "uid-a"), _tok("B", "uid-b"), _tok("C", "uid-c")])
        # Cards 0: A/B, 1: C with no pair → default back applies.
        assert [c.name for c in out] == ["A / B", "C"]
        assert out[0].pair_back_uid == "uid-b"
        assert out[1].pair_back_uid is None

    def test_even_four(self):
        out = fill._pair_tokens([_tok("A", "1"), _tok("B", "2"), _tok("C", "3"), _tok("D", "4")])
        assert [(c.name, c.pair_back_uid) for c in out] == [
            ("A / B", "2"),
            ("C / D", "4"),
        ]


@responses.activate
def test_resolve_urls_pair_back_uses_other_card_front():
    """When pair_back_uid is set, the back URL becomes that other card's
    front face (tokens are single-faced, so no DFC subtleties)."""
    fill._scryfall_payload_cache.clear()
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/front-token",
        json={"id": "front-token", "image_uris": {"png": "https://x/front.png"}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/back-token",
        json={"id": "back-token", "image_uris": {"png": "https://x/back.png"}},
        status=200,
    )
    job = fill.CardJob(
        name="Front / Back",
        qty=1,
        scryfall_uid="front-token",
        custom_image_url=None,
        set_code=None,
        collector_number=None,
        pair_back_uid="back-token",
    )
    front, back = fill.resolve_urls(job, Path("/tmp/no-overrides"), fill.requests.Session())
    assert front == "https://x/front.png"
    assert back == "https://x/back.png"


def test_cloudflare_blocked_raises_with_helpful_message():
    with pytest.raises(SystemExit, match="Cloudflare.*--decklist"):
        fill._fetch_cloudflare_blocked("Deckstats", "126143", "4305047")


# --- Scryfall image-URL resolver ----------------------------------------


def _scryfall_payload(layout="normal", image="https://x/front.png", back_image=None):
    """Helper to construct a Scryfall card payload."""
    if back_image is None:
        return {"layout": layout, "image_uris": {"png": image}}
    return {
        "layout": layout,
        "card_faces": [
            {"name": "Front", "image_uris": {"png": image}},
            {"name": "Back", "image_uris": {"png": back_image}},
        ],
    }


@responses.activate
def test_scryfall_image_urls_single_face():
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/uid-1",
        json=_scryfall_payload(),
        status=200,
    )
    front, back = fill.scryfall_image_urls("uid-1", fill.requests.Session())
    assert front == "https://x/front.png"
    assert back is None


@responses.activate
def test_scryfall_image_urls_transform_returns_both_faces():
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/uid-2",
        json=_scryfall_payload(
            layout="transform", image="https://x/v.png", back_image="https://x/h.png"
        ),
        status=200,
    )
    front, back = fill.scryfall_image_urls("uid-2", fill.requests.Session())
    assert front == "https://x/v.png"
    assert back == "https://x/h.png"


@pytest.mark.parametrize("layout", ["split", "flip", "adventure", "aftermath", "fuse"])
@responses.activate
def test_scryfall_image_urls_single_piece_layouts_drop_back(layout):
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/uid-3",
        json=_scryfall_payload(
            layout=layout, image="https://x/f.png", back_image="https://x/b.png"
        ),
        status=200,
    )
    front, back = fill.scryfall_image_urls("uid-3", fill.requests.Session())
    # Single-piece layouts have two faces in the Scryfall data but print as
    # one physical card, so we drop the back to avoid duplicating it.
    assert front == "https://x/f.png"
    assert back is None


@responses.activate
def test_scryfall_image_urls_no_image_uris_raises():
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/uid-4",
        json={"layout": "normal", "name": "Mystery"},
        status=200,
    )
    with pytest.raises(RuntimeError, match="No image_uris"):
        fill.scryfall_image_urls("uid-4", fill.requests.Session())


# --- Override resolution -------------------------------------------------


class TestResolveUrls:
    def test_override_front_wins_over_scryfall(self, tmp_path):
        override = tmp_path / "Sol_Ring.png"
        override.write_bytes(b"fake png")
        job = fill.CardJob(
            name="Sol Ring",
            qty=1,
            scryfall_uid="uid-1",
            custom_image_url=None,
            set_code=None,
            collector_number=None,
        )
        front, back = fill.resolve_urls(job, tmp_path, fill.requests.Session())
        assert front.startswith("file://") and front.endswith("Sol_Ring.png")
        assert back is None  # no override-back exists

    def test_override_back_paired_with_override_front(self, tmp_path):
        (tmp_path / "Vraska.png").write_bytes(b"f")
        (tmp_path / "Vraska.back.png").write_bytes(b"b")
        job = fill.CardJob(
            name="Vraska",
            qty=1,
            scryfall_uid="uid-1",
            custom_image_url=None,
            set_code=None,
            collector_number=None,
        )
        front, back = fill.resolve_urls(job, tmp_path, fill.requests.Session())
        assert front.endswith("Vraska.png")
        assert back is not None and back.endswith("Vraska.back.png")

    def test_custom_image_url_when_no_override(self, tmp_path):
        job = fill.CardJob(
            name="Custom",
            qty=1,
            scryfall_uid=None,
            custom_image_url="https://example.com/custom.png",
            set_code=None,
            collector_number=None,
        )
        front, back = fill.resolve_urls(job, tmp_path, fill.requests.Session())
        assert front == "https://example.com/custom.png"
        assert back is None

    def test_no_source_at_all_raises(self, tmp_path):
        job = fill.CardJob(
            name="Mystery",
            qty=1,
            scryfall_uid=None,
            custom_image_url=None,
            set_code=None,
            collector_number=None,
        )
        with pytest.raises(RuntimeError, match="No image source"):
            fill.resolve_urls(job, tmp_path, fill.requests.Session())


# --- fetch_image (file:// path) -----------------------------------------


def test_fetch_image_reads_local_file(tmp_path):
    img_path = tmp_path / "x.png"
    Image.new("RGB", (10, 20), (1, 2, 3)).save(img_path)
    out = fill.fetch_image(f"file://{img_path}", fill.requests.Session())
    assert out.size == (10, 20)


@responses.activate
def test_fetch_image_downloads_url():
    buf = BytesIO()
    Image.new("RGB", (8, 16), (4, 5, 6)).save(buf, "PNG")
    responses.add(
        responses.GET,
        "https://x/img.png",
        body=buf.getvalue(),
        status=200,
        content_type="image/png",
    )
    out = fill.fetch_image("https://x/img.png", fill.requests.Session())
    assert out.size == (8, 16)


# --- fetch_deck dispatcher ----------------------------------------------


@responses.activate
def test_fetch_deck_dispatches_to_archidekt(monkeypatch):
    called = {}

    def fake(deck_id):
        called["archidekt"] = deck_id
        return []

    monkeypatch.setattr(fill, "_fetch_archidekt", fake)
    fill.fetch_deck("https://archidekt.com/decks/42")
    assert called == {"archidekt": "42"}


def test_fetch_deck_dispatches_cloudflare_helpfully(monkeypatch):
    with pytest.raises(SystemExit, match="Cloudflare"):
        fill.fetch_deck("https://deckstats.net/decks/1/2")
