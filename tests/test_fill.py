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


class TestIsBasicLand:
    def test_canonical_basics(self):
        for n in ["Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"]:
            assert fill.is_basic_land(n)

    def test_snow_covered(self):
        assert fill.is_basic_land("Snow-Covered Forest")
        assert fill.is_basic_land("snow-covered island")  # case-insensitive

    def test_whitespace_tolerated(self):
        assert fill.is_basic_land("  Plains  ")

    def test_non_basic_lands_excluded(self):
        # Real lands that contain a basic name as a substring must NOT match.
        assert not fill.is_basic_land("Snow-Covered Mountain Pass")
        assert not fill.is_basic_land("Forest of Lost Souls")
        assert not fill.is_basic_land("Sol Ring")
        assert not fill.is_basic_land("")


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

    def test_strips_leading_dots(self):
        # Defense in depth — a card "named" .. or ./foo would otherwise
        # produce "..png" / "._foo.png" filenames that lean on dot semantics.
        assert fill.slug("..") == "card"
        assert fill.slug(".hidden") == "hidden"
        assert fill.slug("../traversal") == "traversal"


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

    def test_edhrec_url(self):
        assert fill.detect_source("https://edhrec.com/deckpreview/k39SkKNDKaQEan_AX8CJ8A") == (
            "edhrec",
            ("k39SkKNDKaQEan_AX8CJ8A",),
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

    def test_scryfall_url_with_user(self):
        # Canonical Scryfall deck URLs include the @user segment.
        assert fill.detect_source(
            "https://scryfall.com/@user/decks/12345678-1234-1234-1234-123456789012"
        ) == ("scryfall", ("12345678-1234-1234-1234-123456789012",))

    def test_scryfall_url_without_user(self):
        # Bare /decks/<uuid> form is also accepted.
        assert fill.detect_source(
            "https://scryfall.com/decks/abcdef01-2345-6789-abcd-ef0123456789"
        ) == ("scryfall", ("abcdef01-2345-6789-abcd-ef0123456789",))

    def test_scryfall_url_rejects_non_uuid(self):
        # The Scryfall regex insists on the full 8-4-4-4-12 hex shape so
        # garbage like `/decks/abc12345xyz0` doesn't false-positive. The
        # alphanumeric-id fallback only applies to bare ids (not URLs),
        # so a Scryfall URL with a non-UUID falls through to SystemExit.
        with pytest.raises(SystemExit):
            fill.detect_source("https://scryfall.com/decks/abc12345xyz0")

    def test_deckbox_url(self):
        assert fill.detect_source("https://deckbox.org/sets/3456789") == (
            "deckbox",
            ("3456789",),
        )

    def test_deckbox_url_with_slug_trailer(self):
        assert fill.detect_source("https://deckbox.org/sets/3456789/my-deck") == (
            "deckbox",
            ("3456789",),
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
        text = "1 Sol Ring\nMaybeboard:\n1 Counterspell\nTokens\n1 Treasure"
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

    def test_mtgo_dek_xml_routed_to_xml_parser(self):
        text = """<?xml version="1.0" encoding="utf-8"?>
<Deck>
  <Cards Quantity="4" Sideboard="false" Name="Lightning Bolt" />
  <Cards Quantity="1" Sideboard="false" Name="Sol Ring" />
  <Cards Quantity="2" Sideboard="true" Name="Negate" />
</Deck>"""
        assert fill._parse_decklist(text) == [
            (4, "Lightning Bolt", None, None),
            (1, "Sol Ring", None, None),
        ]

    def test_mtgo_dek_with_namespace(self):
        # Real MTGO exports include xsi/xsd namespaces on the root.
        text = """<Deck xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Cards Quantity="1" Sideboard="false" Name="Sol Ring" />
</Deck>"""
        assert fill._parse_decklist(text) == [(1, "Sol Ring", None, None)]

    def test_mtgo_dek_without_cards_elements_raises(self):
        # A well-formed XML document that doesn't contain any <Cards>
        # children — e.g. a third-party tool that renamed the element.
        # Returning [] silently would build a 0-card deck without telling
        # the user we didn't recognise their schema.
        text = '<?xml version="1.0"?><Deck><Card Quantity="1" Name="Sol Ring"/></Deck>'
        with pytest.raises(SystemExit, match="no <Cards>"):
            fill._parse_decklist(text)

    def test_mtgo_dek_malformed_raises(self):
        import pytest

        with pytest.raises(SystemExit, match="Could not parse MTGO"):
            fill._parse_decklist("<?xml version='1.0'?><Deck><Cards")


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


class TestMakeDefaultBack:
    def test_returns_image(self):
        img = fill.make_default_back()
        # Bundled meme back is a normal-sized card image; just assert it loaded.
        assert img.mode == "RGB"
        assert img.size[0] > 0 and img.size[1] > 0


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
                    "oracleCard": {"name": "Yidris, Maelstrom Wielder"},
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
def test_fetch_edhrec_extracts_next_data():
    """EDHREC's deckpreview page embeds the deck as a list of plain
    `"N Card Name"` strings inside __NEXT_DATA__. We extract and route
    through the existing decklist parser."""
    next_data = {
        "props": {
            "pageProps": {
                "data": {
                    "deck": ["3 Lightning Bolt", "1 Sol Ring"],
                }
            }
        }
    }
    html = (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + __import__("json").dumps(next_data)
        + "</script></body></html>"
    )
    responses.add(
        responses.GET,
        "https://edhrec.com/deckpreview/abc123",
        body=html,
        status=200,
        content_type="text/html",
    )
    for name, uid in [("Lightning Bolt", "lb-uid"), ("Sol Ring", "sr-uid")]:
        responses.add(
            responses.GET,
            "https://api.scryfall.com/cards/named",
            json={"id": uid, "name": name},
            status=200,
        )
    jobs = fill._fetch_edhrec("abc123")
    assert {(j.name, j.qty) for j in jobs} == {("Lightning Bolt", 3), ("Sol Ring", 1)}


@responses.activate
def test_fetch_edhrec_missing_blob_errors():
    responses.add(
        responses.GET,
        "https://edhrec.com/deckpreview/abc123",
        body="<html>no next data here</html>",
        status=200,
        content_type="text/html",
    )
    with pytest.raises(SystemExit, match="__NEXT_DATA__"):
        fill._fetch_edhrec("abc123")


def _edhrec_html_with_payload(payload):
    import json as _json

    return (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + _json.dumps(payload)
        + "</script></body></html>"
    )


@responses.activate
def test_fetch_edhrec_null_data_surfaces_clean_error():
    """EDHREC returns the page with `pageProps.data: null` for deleted /
    private decks. The chained subscript would crash on TypeError; we
    catch it and emit a friendly extraction error instead."""
    responses.add(
        responses.GET,
        "https://edhrec.com/deckpreview/gone",
        body=_edhrec_html_with_payload({"props": {"pageProps": {"data": None}}}),
        status=200,
        content_type="text/html",
    )
    with pytest.raises(SystemExit, match="Could not extract decklist"):
        fill._fetch_edhrec("gone")


@responses.activate
def test_fetch_edhrec_schema_drift_detected():
    """If EDHREC ever ships objects in `deck` instead of strings, str()
    would silently produce '[object Object]'-equivalents and we'd build
    a deck of zero cards. Detect the shape change up front."""
    responses.add(
        responses.GET,
        "https://edhrec.com/deckpreview/changed",
        body=_edhrec_html_with_payload(
            {"props": {"pageProps": {"data": {"deck": [{"name": "Sol Ring", "qty": 1}]}}}}
        ),
        status=200,
        content_type="text/html",
    )
    with pytest.raises(SystemExit, match="EDHREC deck shape changed"):
        fill._fetch_edhrec("changed")


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
        fill.make_default_back()


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
    assert refs == [("tok-1", "Faerie Rogue", ""), ("tok-2", "Treasure", "")]


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
        for tok_uid, tok_name, _tok_type in fill.scryfall_token_refs(parent, s):
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


@responses.activate
def test_discover_tokens_dedupes_by_name_across_uids():
    """Different Scryfall printings of the same token (e.g. "Treasure" from
    different sets) carry distinct UIDs but identical art. Dedupe by name
    so --pair-tokens can't land copy/copy on a single card."""
    fill._scryfall_payload_cache.clear()
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/card-a",
        json={
            "id": "card-a",
            "all_parts": [
                {"id": "treasure-uid-1", "name": "Treasure", "component": "token"},
            ],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/card-b",
        json={
            "id": "card-b",
            "all_parts": [
                {"id": "treasure-uid-2", "name": "Treasure", "component": "token"},
            ],
        },
        status=200,
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

    new_jobs, failures = fill._discover_tokens(
        [J("CardA", "card-a"), J("CardB", "card-b")],
        fill.requests.Session(),
    )

    assert failures == []
    assert [j.name for j in new_jobs] == ["Treasure (token)"]
    assert new_jobs[0].scryfall_uid == "treasure-uid-1"


@responses.activate
def test_discover_tokens_keeps_distinct_types_with_same_name():
    """Two tokens that share a name but have different type_lines (e.g. the
    1/1 W flying Spirit vs. the Kamigawa colorless Spirit) must NOT
    collapse — their art is distinct."""
    fill._scryfall_payload_cache.clear()
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/card-a",
        json={
            "id": "card-a",
            "all_parts": [
                {
                    "id": "spirit-w",
                    "name": "Spirit",
                    "type_line": "Token Creature — Spirit",
                    "component": "token",
                },
            ],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/card-b",
        json={
            "id": "card-b",
            "all_parts": [
                {
                    "id": "spirit-c",
                    "name": "Spirit",
                    "type_line": "Token Artifact Creature — Spirit",
                    "component": "token",
                },
            ],
        },
        status=200,
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

    new_jobs, failures = fill._discover_tokens(
        [J("A", "card-a"), J("B", "card-b")],
        fill.requests.Session(),
    )

    assert failures == []
    assert {j.scryfall_uid for j in new_jobs} == {"spirit-w", "spirit-c"}


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


class TestApplyTokenJobs:
    """Gating rules for --pair-tokens / --pair-backs combinations."""

    def test_no_tokens_returns_empty(self):
        assert fill._apply_token_jobs([], pair_tokens=True, pair_backs=True) == ([], None)

    def test_pair_tokens_without_pair_backs_warns_and_falls_back(self):
        toks = [_tok("A", "1"), _tok("B", "2"), _tok("C", "3")]
        out, warn = fill._apply_token_jobs(toks, pair_tokens=True, pair_backs=False)
        assert out == toks  # single-sided fallback, no pairing
        assert warn and "pair-backs" in warn

    def test_pair_tokens_with_one_token_skips_pairing(self):
        # Pairing needs ≥2 to be meaningful; with 1 we just emit the
        # single-sided card and let the default-back path fill the second face.
        toks = [_tok("Solo", "uid")]
        out, warn = fill._apply_token_jobs(toks, pair_tokens=True, pair_backs=True)
        assert out == toks
        assert warn is None

    def test_pair_tokens_full_path_pairs(self):
        toks = [_tok("A", "1"), _tok("B", "2"), _tok("C", "3"), _tok("D", "4")]
        out, warn = fill._apply_token_jobs(toks, pair_tokens=True, pair_backs=True)
        assert [c.name for c in out] == ["A / B", "C / D"]
        assert warn is None

    def test_pair_tokens_off_returns_singles(self):
        toks = [_tok("A", "1"), _tok("B", "2")]
        out, warn = fill._apply_token_jobs(toks, pair_tokens=False, pair_backs=True)
        assert out == toks
        assert warn is None


@responses.activate
def test_resolve_urls_back_override_wins_over_pair_back_uid(tmp_path):
    """An explicit `<slug>.back.png` is more intentional than an inferred
    pair_back_uid; the user file must win."""
    fill._scryfall_payload_cache.clear()
    front_file = tmp_path / "Test_Card.png"
    back_file = tmp_path / "Test_Card.back.png"
    front_file.write_bytes(b"front")
    back_file.write_bytes(b"back")
    job = fill.CardJob(
        name="Test Card",
        qty=1,
        scryfall_uid=None,
        custom_image_url=None,
        set_code=None,
        collector_number=None,
        pair_back_uid="some-other-uid",  # would normally fetch; should be ignored
    )
    front, back = fill.resolve_urls(job, tmp_path, fill.requests.Session())
    assert front.endswith("Test_Card.png")
    assert back is not None and back.endswith("Test_Card.back.png")
    # Crucially, no Scryfall request was made.
    assert len(responses.calls) == 0


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


class TestFetchImageSchemeAllowlist:
    @responses.activate
    def test_https_passes_through(self):
        # End-to-end: an https URL is not rejected by the allowlist and
        # actually proceeds to the HTTP layer (mocked here).
        buf = BytesIO()
        Image.new("RGB", (4, 6)).save(buf, "PNG")
        responses.add(
            responses.GET,
            "https://api.scryfall.com/img.png",
            body=buf.getvalue(),
            status=200,
            content_type="image/png",
        )
        out = fill.fetch_image("https://api.scryfall.com/img.png", fill.requests.Session())
        assert out.size == (4, 6)

    def test_file_passes_through(self, tmp_path):
        p = tmp_path / "x.png"
        Image.new("RGB", (3, 5)).save(p)
        out = fill.fetch_image(f"file://{p}", fill.requests.Session())
        assert out.size == (3, 5)

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/img.png",
            "data:image/png;base64,iVBORw0KGg=",
            "ftp://internal/img.png",
            "javascript:alert(1)",
            "HTTPS://api.scryfall.com/img.png",  # case-sensitive on purpose
            "https",  # bare scheme, no separator
            "",
            "  https://leading-space.example/img.png",
        ],
    )
    def test_rejects(self, url):
        with pytest.raises(RuntimeError, match="disallowed scheme"):
            fill.fetch_image(url, fill.requests.Session())


class TestScrubSource:
    def test_keeps_https_url(self):
        assert (
            fill._scrub_source("https://cards.scryfall.io/png/x.png")
            == "https://cards.scryfall.io/png/x.png"
        )

    def test_strips_absolute_path(self):
        # Local override paths leak the user's home; manifest gets only the basename.
        out = fill._scrub_source("file:///home/jlyons/secret/overrides/Sol_Ring.png")
        assert out == "file://Sol_Ring.png"
        assert "/home/" not in out

    def test_strips_alternate_override_dir(self):
        # The fix doesn't lie: arbitrary --overrides paths still scrub down
        # to just the basename, no implied `overrides/` prefix.
        out = fill._scrub_source("file:///home/alice/private-cards/Sol_Ring.png")
        assert out == "file://Sol_Ring.png"
        assert "alice" not in out and "private" not in out

    def test_relative_file_url(self):
        # Edge case: relative path inside file:// (no leading slash). Path.name
        # still extracts the basename.
        assert fill._scrub_source("file://relative/Sol_Ring.png") == "file://Sol_Ring.png"

    def test_passes_none(self):
        assert fill._scrub_source(None) is None


def test_cloudflare_blocked_raises_with_helpful_message():
    with pytest.raises(SystemExit, match="Cloudflare.*--decklist"):
        fill._fetch_cloudflare_blocked("Deckstats", "126143", "4305047")


# --- Scryfall + Deckbox text-export fetchers -----------------------------


@responses.activate
def test_fetch_scryfall_resolves_via_decklist_parser():
    """Scryfall ships the deck as plain text from the export endpoint."""
    deck_id = "12345678-1234-1234-1234-123456789012"
    responses.add(
        responses.GET,
        f"https://api.scryfall.com/decks/{deck_id}/export/text",
        body="3 Lightning Bolt\n1 Sol Ring\n",
        status=200,
        content_type="text/plain",
    )
    for name, uid in [("Lightning Bolt", "lb-uid"), ("Sol Ring", "sr-uid")]:
        responses.add(
            responses.GET,
            "https://api.scryfall.com/cards/named",
            json={"id": uid, "name": name},
            status=200,
        )
    jobs = fill._fetch_scryfall(deck_id)
    assert {(j.name, j.qty) for j in jobs} == {("Lightning Bolt", 3), ("Sol Ring", 1)}


@responses.activate
def test_fetch_scryfall_4xx_raises():
    deck_id = "00000000-0000-0000-0000-000000000000"
    responses.add(
        responses.GET,
        f"https://api.scryfall.com/decks/{deck_id}/export/text",
        json={"details": "Not found"},
        status=404,
    )
    with pytest.raises(SystemExit, match="Scryfall returned 404"):
        fill._fetch_scryfall(deck_id)


@responses.activate
def test_fetch_deckbox_resolves_via_decklist_parser():
    responses.add(
        responses.GET,
        "https://deckbox.org/sets/123/export?format=tcg",
        body="2 Lightning Bolt\n1 Sol Ring\n",
        status=200,
        content_type="text/plain",
    )
    for name, uid in [("Lightning Bolt", "lb-uid"), ("Sol Ring", "sr-uid")]:
        responses.add(
            responses.GET,
            "https://api.scryfall.com/cards/named",
            json={"id": uid, "name": name},
            status=200,
        )
    jobs = fill._fetch_deckbox("123")
    assert {(j.name, j.qty) for j in jobs} == {("Lightning Bolt", 2), ("Sol Ring", 1)}


@responses.activate
def test_fetch_deckbox_private_set_detected_via_html_redirect():
    """Private Deckbox sets redirect to a login HTML page; without the
    explicit redirect-detection guard the parser would silently produce
    zero cards and the user would have no idea why."""
    responses.add(
        responses.GET,
        "https://deckbox.org/sets/999/export?format=tcg",
        body="<html><body>Please log in</body></html>",
        status=200,
        content_type="text/html",
    )
    with pytest.raises(SystemExit, match="looks private"):
        fill._fetch_deckbox("999")


# --- ManaBox / generic CSV decklist parsing -----------------------------


class TestLooksLikeCsv:
    def test_manabox_header_detected(self):
        text = (
            "Name,Set code,Set name,Collector number,Foil,Rarity,Quantity\n"
            '"Sol Ring","cmm","Commander Masters","343","normal","uncommon","1"\n'
        )
        assert fill._looks_like_csv(text)

    def test_card_with_comma_in_name_isnt_csv(self):
        # `Yidris, Maelstrom Wielder` contains a comma but isn't a CSV header.
        # Without the both-name-and-quantity gate, this would false-positive.
        assert not fill._looks_like_csv("1 Yidris, Maelstrom Wielder\n4 Lightning Bolt\n")

    def test_quantity_only_header_isnt_csv(self):
        # A header with `Quantity` but no `Name` shouldn't trigger CSV mode.
        assert not fill._looks_like_csv("Quantity,SomethingElse\n1,foo\n")

    def test_blank_text_isnt_csv(self):
        assert not fill._looks_like_csv("")


class TestParseCsvDecklist:
    def test_basic_manabox_export(self):
        text = (
            "Name,Set code,Collector number,Quantity\n"
            '"Sol Ring","cmm","343","1"\n'
            '"Lightning Bolt","m21","162","4"\n'
        )
        assert fill._parse_csv_decklist(text) == [
            (1, "Sol Ring", "cmm", "343"),
            (4, "Lightning Bolt", "m21", "162"),
        ]

    def test_section_column_skips_sideboard(self):
        text = 'Name,Quantity,Section\n"Sol Ring","1","Mainboard"\n"Negate","2","Sideboard"\n'
        assert fill._parse_csv_decklist(text) == [(1, "Sol Ring", None, None)]

    def test_zero_or_invalid_qty_skipped(self):
        text = "Name,Quantity\nGood,1\nBad,abc\nZero,0\n"
        assert fill._parse_csv_decklist(text) == [(1, "Good", None, None)]

    def test_case_insensitive_headers(self):
        text = "name,QTY\nSol Ring,1\n"
        assert fill._parse_csv_decklist(text) == [(1, "Sol Ring", None, None)]

    def test_missing_required_columns_returns_empty(self):
        # No quantity column → can't build a meaningful decklist.
        text = "Name,SetCode\nSol Ring,cmm\n"
        assert fill._parse_csv_decklist(text) == []


class TestParseDecklistRoutesToCsv:
    """The dispatcher in `_parse_decklist` recognises CSV input and
    routes to the CSV parser instead of the line-by-line parser."""

    def test_csv_routed_to_csv_parser(self):
        text = "Name,Quantity\nSol Ring,1\nLightning Bolt,4\n"
        assert fill._parse_decklist(text) == [
            (1, "Sol Ring", None, None),
            (4, "Lightning Bolt", None, None),
        ]

    def test_csv_with_section_excludes_sideboard(self):
        text = "Name,Quantity,Section\nSol Ring,1,Mainboard\nNegate,2,Sideboard\n"
        assert fill._parse_decklist(text) == [(1, "Sol Ring", None, None)]


# --- Oracle-text token heuristic ----------------------------------------


class TestExtractTokenPhrases:
    def test_named_token(self):
        assert fill._extract_token_phrases("Create a Treasure token.") == [("Treasure", None)]

    def test_creature_token_with_pt(self):
        text = "Create a 1/1 white Soldier creature token."
        assert fill._extract_token_phrases(text) == [("1/1 white Soldier creature", None)]

    def test_multiple_matches(self):
        text = (
            "Whenever this attacks, create a Treasure token. "
            "When it dies, create three 1/1 white Spirit creature tokens with flying."
        )
        # The 'with flying' clause sits AFTER `tokens` so it's not captured.
        assert fill._extract_token_phrases(text) == [
            ("Treasure", None),
            ("1/1 white Spirit creature", None),
        ]

    def test_named_clause_after_token_captured(self):
        # Pre-2020 phrasing: token's actual name is AFTER the word `token`.
        # Without the named-clause capture we'd query for "colorless artifact"
        # and miss every Treasure on Ixalan-era cards.
        text = "Create a colorless artifact token named Treasure."
        assert fill._extract_token_phrases(text) == [("colorless artifact", "Treasure")]

    def test_named_clause_creature_token(self):
        # Tuktuk the Explorer's named-token phrasing — multi-word name.
        text = "Create a 5/5 red Goblin Golem artifact creature token named Tuktuk the Returned."
        assert fill._extract_token_phrases(text) == [
            ("5/5 red Goblin Golem artifact creature", "Tuktuk the Returned")
        ]

    def test_up_to_n_quantifier(self):
        text = "Create up to two 1/1 white Soldier creature tokens."
        assert fill._extract_token_phrases(text) == [("1/1 white Soldier creature", None)]

    def test_no_match(self):
        assert fill._extract_token_phrases("Lightning Bolt deals 3 damage to any target.") == []

    def test_empty_input(self):
        assert fill._extract_token_phrases("") == []
        assert fill._extract_token_phrases(None) == []  # type: ignore[arg-type]


class TestTokenPhraseToQuery:
    def test_named_token_uses_name_match(self):
        q = fill._token_phrase_to_query("Treasure")
        assert q is not None and "is:token" in q and 'name:"Treasure"' in q

    def test_pt_creature_includes_color_type_pt(self):
        q = fill._token_phrase_to_query("1/1 white Soldier creature")
        assert q is not None
        assert "is:token" in q
        assert "pt:1/1" in q
        assert "c=w" in q
        assert "t:soldier" in q

    def test_multicolor_creature(self):
        q = fill._token_phrase_to_query("2/2 black and red Zombie creature")
        # Both colors emitted as a single c= clause.
        assert q is not None
        assert "c=br" in q or "c=rb" in q
        assert "t:zombie" in q

    def test_named_clause_overrides_descriptor(self):
        # When a `named X` clause was captured it's the real token name —
        # use it instead of the descriptor.
        q = fill._token_phrase_to_query("colorless artifact", named="Treasure")
        assert q == 'is:token name:"Treasure"'

    def test_named_clause_overrides_pt_descriptor(self):
        # `Tuktuk the Returned` is the actual token name; the 5/5 red Goblin
        # Golem descriptor is a worse search target.
        q = fill._token_phrase_to_query(
            "5/5 red Goblin Golem artifact creature", named="Tuktuk the Returned"
        )
        assert q == 'is:token name:"Tuktuk the Returned"'

    def test_filler_words_stripped_from_named_fallback(self):
        # "create a tapped Treasure token" → captures "tapped Treasure" as
        # the descriptor. Without filler-word stripping this would search
        # for `name:"tapped Treasure"` and silently miss every real Treasure.
        q = fill._token_phrase_to_query("tapped Treasure")
        assert q == 'is:token name:"Treasure"'

    def test_descriptor_strips_to_nothing_returns_none(self):
        # "colorless artifact" with no `named X` clause: every word is
        # filler/color. No useful query — return None so the caller can
        # skip cleanly instead of issuing an empty Scryfall search.
        assert fill._token_phrase_to_query("colorless artifact") is None


@responses.activate
def test_resolve_token_phrase_picks_first_search_hit():
    fill._scryfall_payload_cache.clear()
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/search",
        json={"data": [{"id": "treasure-token-uid", "name": "Treasure"}]},
        status=200,
    )
    uid, error = fill._resolve_token_phrase("Treasure", None, fill.requests.Session())
    assert uid == "treasure-token-uid"
    assert error is None


@responses.activate
def test_resolve_token_phrase_returns_none_on_404():
    """A 404 from Scryfall search means a clean 'no such token' result —
    return (None, None), not a transient error. Caller can cache the miss."""
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/search",
        json={"object": "error", "code": "not_found"},
        status=404,
    )
    uid, error = fill._resolve_token_phrase("Imaginary", None, fill.requests.Session())
    assert uid is None
    assert error is None


def test_resolve_token_phrase_skips_empty_query():
    # Descriptor reduces to nothing actionable → skip the search entirely
    # (no HTTP request) and return a clean miss. responses isn't activated
    # so any HTTP call would error — verifies no network was hit.
    uid, error = fill._resolve_token_phrase("colorless artifact", None, fill.requests.Session())
    assert uid is None
    assert error is None


@responses.activate
def test_resolve_token_phrase_surfaces_5xx_as_transient():
    """A 5xx response is a transient failure — return an error string so
    the caller can record it on `failures[]` instead of silently caching
    None and suppressing every later card minting the same token."""
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/search",
        json={"err": "server"},
        status=503,
    )
    uid, error = fill._resolve_token_phrase("Treasure", None, fill.requests.Session())
    assert uid is None
    assert error is not None and "503" in error


@responses.activate
def test_resolve_token_phrase_uses_named_clause_when_present():
    """Verify that when `named` is passed, the search query targets that
    name (not the descriptor). Catches a regression where the named arg
    is dropped on the way to query-builder."""
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/search",
        json={"data": [{"id": "treasure-uid", "name": "Treasure"}]},
        status=200,
    )
    uid, error = fill._resolve_token_phrase(
        "colorless artifact", "Treasure", fill.requests.Session()
    )
    assert uid == "treasure-uid"
    assert error is None
    # The query in the URL must contain Treasure, not "colorless artifact".
    last_url = responses.calls[-1].request.url
    assert "Treasure" in last_url
    assert "colorless+artifact" not in last_url and "colorless%20artifact" not in last_url


@responses.activate
def test_discover_tokens_thorough_extends_all_parts_results():
    """Thorough mode picks up tokens that all_parts misses by oracle-text
    scan, while still emitting tokens that all_parts already names."""
    fill._scryfall_payload_cache.clear()
    # Card A: all_parts has the token, oracle_text doesn't mention it.
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/has-allparts",
        json={
            "id": "has-allparts",
            "all_parts": [
                {"id": "treasure-uid", "name": "Treasure", "component": "token"},
            ],
            "oracle_text": "Some unrelated text.",
        },
        status=200,
    )
    # Card B: all_parts is empty (Scryfall metadata gap), but oracle text
    # mentions creating a Food token. Thorough mode should rescue this.
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/missing-allparts",
        json={
            "id": "missing-allparts",
            "all_parts": [],
            "oracle_text": "When this enters, create a Food token.",
        },
        status=200,
    )
    # Search returns the Food token.
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/search",
        json={"data": [{"id": "food-uid", "name": "Food"}]},
        status=200,
    )
    # Token payload lookup for the resolved Food UID — needs name+type
    # for the dedupe key.
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/food-uid",
        json={"id": "food-uid", "name": "Food", "type_line": "Token Artifact — Food"},
        status=200,
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

    jobs = [J("HasAllparts", "has-allparts"), J("MissingAllparts", "missing-allparts")]
    new_jobs, failures = fill._discover_tokens(jobs, fill.requests.Session(), thorough=True)

    # Both tokens present — Treasure from all_parts, Food from oracle scan.
    names = sorted(j.name for j in new_jobs)
    assert names == ["Food (token)", "Treasure (token)"]
    assert failures == []


@responses.activate
def test_discover_tokens_thorough_caches_phrase_lookups():
    """Two cards minting the same Treasure token via oracle text should
    only burn one Scryfall search request between them."""
    fill._scryfall_payload_cache.clear()
    for cid in ("card-x", "card-y"):
        responses.add(
            responses.GET,
            f"https://api.scryfall.com/cards/{cid}",
            json={
                "id": cid,
                "all_parts": [],  # force the oracle path to do the work
                "oracle_text": "Whenever this attacks, create a Treasure token.",
            },
            status=200,
        )
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/search",
        json={"data": [{"id": "treasure-uid", "name": "Treasure"}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/treasure-uid",
        json={"id": "treasure-uid", "name": "Treasure", "type_line": "Token Artifact — Treasure"},
        status=200,
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

    new_jobs, failures = fill._discover_tokens(
        [J("X", "card-x"), J("Y", "card-y")],
        fill.requests.Session(),
        thorough=True,
    )

    assert [j.name for j in new_jobs] == ["Treasure (token)"]
    assert failures == []
    # Count search requests — phrase cache must collapse two cards' worth
    # of "Treasure" mentions into a single search.
    search_calls = [c for c in responses.calls if "/cards/search" in c.request.url]
    assert len(search_calls) == 1


@responses.activate
def test_discover_tokens_default_does_not_oracle_scan(monkeypatch):
    """Without thorough=True, oracle-text scanning must NOT run — even if
    the regex would match. Regression guard against accidentally flipping
    the default. Important: the loop has to actually iterate a job, so we
    pass a real CardJob whose payload mocks include `oracle_text`. If the
    test passed an empty job list the assertion would be vacuously true
    even with the default flipped."""
    fill._scryfall_payload_cache.clear()
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/has-oracle",
        json={
            "id": "has-oracle",
            "oracle_text": "Create a Treasure token.",  # would match if scanned
            "all_parts": [],
        },
        status=200,
    )
    called = {"count": 0}

    def fake_extract(_text):
        called["count"] += 1
        return [("Treasure", None)]

    monkeypatch.setattr(fill, "_extract_token_phrases", fake_extract)

    job = fill.CardJob(
        name="HasOracle",
        qty=1,
        scryfall_uid="has-oracle",
        custom_image_url=None,
        set_code=None,
        collector_number=None,
    )
    fill._discover_tokens([job], fill.requests.Session())  # thorough defaults False
    assert called["count"] == 0


@responses.activate
def test_discover_tokens_thorough_surfaces_transient_errors():
    """A 5xx during oracle-scan must land in `failures[]` rather than
    silently caching None — the user opted into thorough mode and a
    transient blip would otherwise suppress every later card minting the
    same token. This is the regression guarded by PR #43 follow-up."""
    fill._scryfall_payload_cache.clear()
    # Two cards both creating a Treasure token via oracle text.
    for cid in ("card-a", "card-b"):
        responses.add(
            responses.GET,
            f"https://api.scryfall.com/cards/{cid}",
            json={
                "id": cid,
                "all_parts": [],
                "oracle_text": "Create a Treasure token.",
            },
            status=200,
        )
    # First search call: 503 transient. Second call (because we DON'T cache
    # the failure): also 503 — both surface as failures.
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/search",
        json={"err": "boom"},
        status=503,
    )
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/search",
        json={"err": "boom"},
        status=503,
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

    new_jobs, failures = fill._discover_tokens(
        [J("CardA", "card-a"), J("CardB", "card-b")],
        fill.requests.Session(),
        thorough=True,
    )

    assert new_jobs == []
    # Both cards surfaced their own transient — proves the cache was NOT
    # poisoned with None on the first failure.
    assert len(failures) == 2
    assert all("503" in f for f in failures)


@responses.activate
def test_discover_tokens_thorough_caches_legitimate_misses():
    """A clean Scryfall 404 ("no such token") IS cached — we don't want to
    re-spend a search request per card on a phrase that genuinely won't
    resolve. Symmetric counterpart to the transient-error test above."""
    fill._scryfall_payload_cache.clear()
    for cid in ("card-a", "card-b"):
        responses.add(
            responses.GET,
            f"https://api.scryfall.com/cards/{cid}",
            json={
                "id": cid,
                "all_parts": [],
                "oracle_text": "Create a Treasure token.",
            },
            status=200,
        )
    # 404 for the first search; if the cache works, no second search fires.
    responses.add(
        responses.GET,
        "https://api.scryfall.com/cards/search",
        json={"object": "error"},
        status=404,
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

    new_jobs, failures = fill._discover_tokens(
        [J("CardA", "card-a"), J("CardB", "card-b")],
        fill.requests.Session(),
        thorough=True,
    )

    assert new_jobs == []
    assert failures == []
    search_calls = [c for c in responses.calls if "/cards/search" in c.request.url]
    assert len(search_calls) == 1  # second card hit the cached 404


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
