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


# Sync API can't run inside an asyncio loop; this guard makes it visible if
# someone runs the test under pytest-asyncio for unrelated reasons.
@pytest.fixture(autouse=True, scope="module")
def _no_running_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    pytest.skip("test must run with pytest's default sync runner")
