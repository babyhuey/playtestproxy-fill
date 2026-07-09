"""Playwright integration test for the Scryfall IndexedDB cache.

Spins up `python -m http.server` on a free port, drives the live frontend
in a headless Chromium with a real Mozilla UA, and asserts:
  1. First build of a paste-mode deck writes one IndexedDB entry per UID.
  2. After a page reload (in-memory cache cleared, IDB persists), the
     second build of the same deck makes zero `/cards/<uid>` calls to
     Scryfall — confirming the cache is being read.

The test resolves a single canonical UID via Scryfall's name lookup
(Lightning Bolt) and only mocks at the Playwright route level. We don't
mock Scryfall — the test is live, but cheap (one card, one Scryfall hit).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


@contextlib.contextmanager
def _http_server():
    """Start `http.server` on a kernel-chosen port. Avoids the bind-then-rebind
    TOCTOU race by retrying a handful of port allocations if a busy CI runner
    snipes one of them between us closing the probe socket and `http.server`
    binding."""
    last_err = None
    for _ in range(8):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parent.parent / "docs",
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                # Bind failed; capture stderr and try a different port.
                last_err = (proc.stderr.read() or b"").decode("utf-8", "replace")[:400]
                break
            with socket.socket() as s:
                try:
                    s.connect(("127.0.0.1", port))
                    try:
                        yield port
                    finally:
                        proc.terminate()
                        proc.wait(timeout=5)
                    return
                except OSError:
                    time.sleep(0.1)
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)
    raise RuntimeError(f"http.server failed to bind after retries; last error: {last_err}")


@pytest.mark.frontend
def test_indexeddb_cache_avoids_repeat_uid_fetches():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with _http_server() as port, sync_playwright() as p:
        # Headless Chrome's default UA contains "HeadlessChrome", which some
        # upstreams (Moxfield) flag as a bot. Use a normal Chrome UA.
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

        # Drop any leftover IDB from a previous run so the count assertion
        # below isn't off-by-N if the test was interrupted.
        page.evaluate(
            """
            () => new Promise((resolve) => {
                const req = indexedDB.deleteDatabase('playtestproxy-fill');
                req.onsuccess = req.onerror = req.onblocked = () => resolve();
            })
            """
        )

        # First build: one paste-mode card. Must hit Scryfall, then write
        # exactly one IDB entry.
        page.click("#mode-text")
        page.fill("#decklist-input", "1 Lightning Bolt")
        page.click("#go")
        page.wait_for_selector("#result:not([hidden])", timeout=60000)

        idb_count = page.evaluate(
            """
            () => new Promise((resolve) => {
                const req = indexedDB.open('playtestproxy-fill', 1);
                req.onsuccess = () => {
                    const tx = req.result.transaction('scryfall-cards', 'readonly');
                    const r = tx.objectStore('scryfall-cards').count();
                    r.onsuccess = () => resolve(r.result);
                    r.onerror = () => resolve(-1);
                };
                req.onerror = () => resolve(-2);
            })
            """
        )
        assert idb_count == 1, f"expected 1 IDB entry after first build, got {idb_count}"

        # Reload — clears in-memory cache; IDB persists.
        page.reload(wait_until="networkidle")

        # Watch for /cards/<uid> requests; the named-lookup endpoints
        # `/cards/named` and `/cards/collection` are not the cache path so
        # they're allowed (collection replaced named for batch resolution
        # in the bulk-lookup refactor).
        uid_calls: list[str] = []

        def on_request(req):
            url = req.url
            if (
                "api.scryfall.com/cards/" in url
                and "/named" not in url
                and "/collection" not in url
            ):
                uid_calls.append(url)

        page.on("request", on_request)

        page.click("#mode-text")
        page.fill("#decklist-input", "1 Lightning Bolt")
        page.click("#go")
        page.wait_for_selector("#result:not([hidden])", timeout=60000)

        assert uid_calls == [], (
            f"second build should hit IDB cache and make zero UID-fetch calls, "
            f"but saw {len(uid_calls)}: {uid_calls[:3]}"
        )

        browser.close()


# PNG bytes for a 1x1 transparent pixel — a valid image the build can decode.
_ONE_PX_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c62f8cfc0f01f0005000100ff9a6c2f6c0000000049454e"
    "44ae426082"
)


@pytest.mark.frontend
def test_custom_art_fetch_bypasses_http_cache():
    """Archidekt custom-art images can change at a stable URL when the user
    re-uploads. The build must fetch them with `cache: "reload"` so the browser
    HTTP cache doesn't pin the old bytes, while immutable Scryfall images keep
    the default cache.

    Playwright's route interception bypasses the browser HTTP cache, so we can't
    observe a stale-vs-fresh hit directly. Instead we wrap `window.fetch` and
    assert the cache mode the app *requests* per URL — the custom image forced
    to reload, the deck JSON left on the default cache.
    """
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    custom_url = "https://example.test/custom-art.png"
    deck = {
        "name": "Custom Test",
        "categories": [],
        "cards": [
            {
                "quantity": 1,
                "categories": ["Commander"],
                "card": {
                    "uid": None,
                    "customImageUrl": custom_url,
                    "oracleCard": {"name": "Custom Card"},
                    "id": 1,
                },
            }
        ],
    }

    with _http_server() as port, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        # Record every fetch's URL and requested cache mode. Playwright's request
        # object doesn't expose the fetch `cache` init, so wrap fetch ourselves.
        page.add_init_script(
            """
            (() => {
              const orig = window.fetch;
              window.__fetchLog = [];
              window.fetch = function (input, init) {
                const url = typeof input === 'string' ? input : (input && input.url) || String(input);
                window.__fetchLog.push({ url, cache: (init && init.cache) || null });
                return orig.apply(this, arguments);
              };
            })();
            """
        )
        page.route(
            "https://archidekt.com/api/decks/**",
            lambda r: r.fulfill(
                status=200,
                body=json.dumps(deck),
                headers={
                    "content-type": "application/json",
                    "access-control-allow-origin": "*",
                },
            ),
        )
        page.route(
            custom_url,
            lambda r: r.fulfill(
                status=200,
                body=_ONE_PX_PNG,
                headers={
                    "content-type": "image/png",
                    "access-control-allow-origin": "*",
                    "cache-control": "public, max-age=31536000, immutable",
                },
            ),
        )

        page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        page.fill("#deck-input", "archidekt.com/decks/999")
        page.click("#go")
        page.wait_for_selector("#result:not([hidden])", timeout=60000)

        log = page.evaluate("() => window.__fetchLog")
        custom = [e for e in log if e["url"] == custom_url]
        deck_calls = [e for e in log if "archidekt.com/api/decks" in e["url"]]

        assert custom, "custom-art image was never fetched"
        assert all(e["cache"] == "reload" for e in custom), (
            f"custom art must bypass the HTTP cache (cache:'reload'), got {custom}"
        )
        assert deck_calls and all(e["cache"] != "reload" for e in deck_calls), (
            f"deck JSON should not be force-reloaded, got {deck_calls}"
        )

        browser.close()


@pytest.mark.frontend
def test_deck_fetch_cache_bust_is_opt_in():
    """corsproxy.io serves the deck JSON with `cache-control: max-age=3600,
    s-maxage=3600`, so the browser AND corsproxy's shared edge can pin an
    hour-stale deck. By default the app uses that cache (cheap; spares corsproxy
    and the deck host). The "Force-refresh the deck" option appends a unique
    `_cb` param that busts the cache at every layer for that one build — for
    when the user just edited a printing/custom art upstream.

    Asserts: default build carries NO `_cb`; a forced build carries one; two
    forced builds use *different* busters (unique per build, not a constant).
    """
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    custom_url = "https://example.test/custom-art.png"
    # A single custom-art card so the build needs no live Scryfall round-trip.
    deck = {
        "name": "Custom Test",
        "categories": [],
        "cards": [
            {
                "quantity": 1,
                "categories": ["Commander"],
                "card": {
                    "uid": None,
                    "customImageUrl": custom_url,
                    "oracleCard": {"name": "Custom Card"},
                    "id": 1,
                },
            }
        ],
    }

    def deck_cb_values(log):
        import urllib.parse as up

        out = []
        for e in log:
            if "archidekt.com/api/decks" in e["url"]:
                qs = up.parse_qs(up.urlparse(e["url"]).query)
                out.append(qs.get("_cb", [None])[0])
        return out

    with _http_server() as port, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.add_init_script(
            """
            (() => {
              const orig = window.fetch;
              window.__fetchLog = [];
              window.fetch = function (input, init) {
                const url = typeof input === 'string' ? input : (input && input.url) || String(input);
                window.__fetchLog.push({ url, cache: (init && init.cache) || null });
                return orig.apply(this, arguments);
              };
            })();
            """
        )
        page.route(
            "https://archidekt.com/api/decks/**",
            lambda r: r.fulfill(
                status=200,
                body=json.dumps(deck),
                headers={
                    "content-type": "application/json",
                    "access-control-allow-origin": "*",
                },
            ),
        )
        page.route(
            custom_url,
            lambda r: r.fulfill(
                status=200,
                body=_ONE_PX_PNG,
                headers={
                    "content-type": "image/png",
                    "access-control-allow-origin": "*",
                },
            ),
        )

        def build_once(fresh):
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.fill("#deck-input", "archidekt.com/decks/999")
            # The checkbox lives in a collapsed <details>; set it directly. The
            # app reads `.checked` at build time, so no change event is needed.
            page.evaluate(
                "(v) => { document.getElementById('opt-fresh-deck').checked = v; }", fresh
            )
            page.click("#go")
            page.wait_for_selector("#result:not([hidden])", timeout=60000)
            return deck_cb_values(page.evaluate("() => window.__fetchLog"))

        default_cb = build_once(False)
        fresh1 = build_once(True)
        fresh2 = build_once(True)

        assert default_cb and all(v is None for v in default_cb), (
            f"default build must NOT cache-bust, got {default_cb}"
        )
        assert fresh1 and fresh1[0] is not None, f"forced build must cache-bust, got {fresh1}"
        assert fresh2 and fresh2[0] is not None, f"forced build must cache-bust, got {fresh2}"
        assert fresh1[0] != fresh2[0], (
            f"each forced build must use a unique buster, got {fresh1[0]} twice"
        )

        browser.close()


# Sync API can't run inside an asyncio loop; this guard makes it visible if
# someone runs the test under pytest-asyncio for unrelated reasons.
@pytest.fixture(autouse=True, scope="module")
def _no_running_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    pytest.skip("test must run with pytest's default sync runner")
