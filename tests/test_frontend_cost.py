"""Frontend cost-estimator tests.

`estimateCost` is a pure function on `window` (app.js is a plain script,
not a module), so these drive the real page and call it directly rather
than reimplementing the pricing table in Python — a transcription bug in
app.js is exactly what these are meant to catch.

Pricing mirrors https://www.tcgplaytest.com/?view=pricing as of 2026-07-27.
"""

from __future__ import annotations

import pytest

from .test_frontend_cache import _http_server

# (cards, per-card rate, tier label) at and around every tier boundary.
TIER_CASES = [
    (1, 0.35, "Starter"),
    (144, 0.35, "Starter"),
    (145, 0.30, "Playtest Set"),
    (499, 0.30, "Playtest Set"),
    (500, 0.26, "Bulk"),
    (501, 0.26, "Bulk"),
]

# (destination, cards, flat shipping) at and around every band boundary.
SHIPPING_CASES = [
    ("us", 1, 6.95),
    ("us", 100, 6.95),
    ("us", 101, 8.95),
    ("us", 250, 8.95),
    ("us", 251, 12.95),
    ("us", 500, 12.95),
    ("us", 501, 18.95),
    ("us", 1000, 18.95),
    ("us", 1001, 29.95),
    ("us", 2000, 29.95),
    ("us", 2001, 49.95),
    ("ca", 1, 12.95),
    ("ca", 100, 12.95),
    ("ca", 101, 16.95),
    ("ca", 250, 16.95),
    ("ca", 251, 24.95),
    ("ca", 500, 24.95),
    ("ca", 501, 34.95),
    ("ca", 1000, 34.95),
    ("ca", 1001, 54.95),
    ("ca", 2000, 54.95),
    ("ca", 2001, 89.95),
    ("intl", 1, 16.95),
    ("intl", 100, 16.95),
    ("intl", 101, 24.95),
    ("intl", 250, 24.95),
    ("intl", 251, 34.95),
    ("intl", 500, 34.95),
    ("intl", 501, 54.95),
    ("intl", 1000, 54.95),
    ("intl", 1001, 89.95),
    ("intl", 2000, 89.95),
    ("intl", 2001, 149.95),
]


# Sync API can't run inside an asyncio loop; this guard makes it visible if
# someone runs the test under pytest-asyncio for unrelated reasons.
@pytest.fixture(autouse=True, scope="module")
def _no_running_loop():
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    pytest.skip("test must run with pytest's default sync runner")


@pytest.fixture(scope="module")
def page():
    """One browser for the whole module — nesting sync_playwright() contexts
    inside a module-scoped one trips its 'sync API inside asyncio loop' guard."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with _http_server() as port, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        # The coupon Copy button is asserted by reading the clipboard back.
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        pg = ctx.new_page()
        pg.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        yield pg
        browser.close()


@pytest.fixture(scope="module")
def estimate(page):
    """A callable that evaluates `estimateCost(n, dest)` on the live page."""

    def call(num_cards, dest=None):
        if dest is None:
            return page.evaluate("(n) => estimateCost(n)", num_cards)
        return page.evaluate("([n, d]) => estimateCost(n, d)", [num_cards, dest])

    return call


@pytest.mark.frontend
@pytest.mark.parametrize("cards,per_card,label", TIER_CASES)
def test_tier_boundaries(estimate, cards, per_card, label):
    e = estimate(cards)
    assert e["perCard"] == pytest.approx(per_card)
    assert e["tier"] == label
    assert e["cards"] == pytest.approx(cards * per_card)


@pytest.mark.frontend
@pytest.mark.parametrize("dest,cards,shipping", SHIPPING_CASES)
def test_shipping_bands(estimate, dest, cards, shipping):
    assert estimate(cards, dest)["shipping"] == pytest.approx(shipping)


@pytest.mark.frontend
def test_shipping_defaults_to_us(estimate):
    assert estimate(50) == estimate(50, "us")


@pytest.mark.frontend
@pytest.mark.parametrize("cards", [1, 144, 145, 500, 2001])
def test_coupon_discounts_cards_only(estimate, cards):
    """HUEY takes 10% off the card subtotal — never off shipping."""
    e = estimate(cards)
    assert e["discount"] == pytest.approx(e["cards"] * 0.10)
    assert e["total"] == pytest.approx(e["cards"] - e["discount"] + e["shipping"])


@pytest.mark.frontend
@pytest.mark.parametrize("dest", ["us", "ca", "intl"])
def test_coupon_leaves_shipping_untouched(estimate, dest):
    """Same deck, three destinations: the discount must not move with shipping."""
    e = estimate(200, dest)
    assert e["discount"] == pytest.approx(200 * 0.30 * 0.10)


@pytest.mark.frontend
def test_destination_select_rerenders_estimate(page):
    """Changing 'Ship to' re-prices without rebuilding the deck."""
    try:
        # renderCostEstimate only ever runs with the result panel on screen;
        # the <select> is inside it, so it must be visible to be operated.
        page.evaluate("() => { result.hidden = false; renderCostEstimate(100); }")
        us = page.inner_text("#cost-estimate-text")
        assert "US shipping" in us
        assert "$38.45" in us
        assert "with code HUEY" in us

        page.select_option("#opt-ship-dest", "intl")
        intl = page.inner_text("#cost-estimate-text")
        # 35.00 cards − 3.50 coupon + 16.95 international shipping.
        assert "International shipping" in intl
        assert "$48.45" in intl
    finally:
        # The page is shared module-wide; leave the control as we found it.
        page.select_option("#opt-ship-dest", "us")
        page.evaluate("() => { renderCostEstimate(0); result.hidden = true; }")


@pytest.mark.frontend
def test_estimate_total_is_labelled_as_post_coupon(page):
    """The headline number is the discounted price, so it must say so —
    otherwise it reads as the price before the code is applied."""
    try:
        page.evaluate("() => { result.hidden = false; renderCostEstimate(100); }")
        assert "cost with code HUEY" in page.inner_text("#cost-estimate-text")
    finally:
        page.evaluate("() => { renderCostEstimate(0); result.hidden = true; }")


@pytest.mark.frontend
def test_coupon_banner_shows_code_and_savings(page):
    try:
        page.evaluate("() => { result.hidden = false; renderCostEstimate(100); }")
        assert page.is_visible("#coupon-banner")
        assert page.inner_text("#coupon-code").strip() == "HUEY"
        assert "$3.50" in page.inner_text("#coupon-banner")
    finally:
        page.evaluate("() => { renderCostEstimate(0); result.hidden = true; }")


@pytest.mark.frontend
def test_coupon_banner_savings_ignore_shipping(page):
    """The discount is 10% of cards only, so switching destination must not
    move it even though the total changes."""
    try:
        page.evaluate("() => { result.hidden = false; renderCostEstimate(100); }")
        page.select_option("#opt-ship-dest", "intl")
        assert "$3.50" in page.inner_text("#coupon-banner")
        assert "$48.45" in page.inner_text("#cost-estimate-text")
    finally:
        page.select_option("#opt-ship-dest", "us")
        page.evaluate("() => { renderCostEstimate(0); result.hidden = true; }")


@pytest.mark.frontend
@pytest.mark.parametrize("sel", ["#coupon-banner", "#cost-estimate", "#deck-stats"])
def test_hidden_boxes_really_collapse(page, sel):
    """`.hidden = true` must actually remove the box. These are flex/grid
    elements, whose author `display:` rule outranks the UA sheet's rule for
    [hidden] — without an explicit guard they linger as empty bordered boxes.
    Asserted with the result panel open, or the ancestor hides them anyway
    and the assertion proves nothing."""
    try:
        page.evaluate("() => { result.hidden = false; renderCostEstimate(0); }")
        assert not page.is_visible(sel)
        assert page.eval_on_selector(sel, "e => getComputedStyle(e).display") == "none"
    finally:
        page.evaluate("() => { result.hidden = true; }")


@pytest.mark.frontend
def test_coupon_copy_button_copies_the_code(page):
    try:
        page.evaluate("() => { result.hidden = false; renderCostEstimate(100); }")
        page.click("#coupon-copy")
        assert page.evaluate("() => navigator.clipboard.readText()") == "HUEY"
        assert "Copied" in page.inner_text("#coupon-copy")
    finally:
        page.evaluate("() => { renderCostEstimate(0); result.hidden = true; }")


@pytest.mark.frontend
def test_worked_example(estimate):
    """100 Starter cards to the US: 35.00 cards − 3.50 coupon + 6.95 shipping."""
    e = estimate(100)
    assert e["cards"] == pytest.approx(35.00)
    assert e["discount"] == pytest.approx(3.50)
    assert e["shipping"] == pytest.approx(6.95)
    assert e["total"] == pytest.approx(38.45)
