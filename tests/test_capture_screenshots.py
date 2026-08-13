"""Smoke test for ``tools/capture_screenshots.py`` (offscreen Qt).

Renders one shot into a tmp dir and asserts the PNG is present, correctly
sized, and not a blank grab. Skipped in CI, which installs the SDK alone.

The screenshot tool is how the README stays honest — the images are real
renders of the real window, so they cannot drift away from what the plugin
draws. That only holds if the tool keeps working, hence this test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="Qt not installed (SDK-only environment)")
pytest.importorskip("nparseplus", reason="host app not installed")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import capture_screenshots as cap  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _thawed():
    """Put the clock back after every test in this module.

    ``cap.build_plugin`` freezes ``datetime.now()`` across ``merchant_mode`` so
    the shots are dated from :data:`cap.NOW` rather than from whenever the tool
    ran. Left frozen, that leaks into the rest of the session: the next test to
    measure a dump's age gets one from a July afternoon and fails somewhere that
    points nowhere near this file.
    """
    yield
    cap.thaw_clock()


def test_sell_tab_capture(qt_app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cap, "OUT_DIR", tmp_path)
    plugin, ctx = cap.build_plugin(tmp_path / "seed")
    window = cap.build_window(plugin, ctx)
    cap.cap_tab(window, 0, "window--sell")

    out = tmp_path / "window--sell.png"
    assert out.exists()
    image = QImage(str(out))
    assert not image.isNull()
    assert (image.width(), image.height()) == cap.WINDOW_SIZE

    # A blank offscreen grab would be one flat colour; a populated window has
    # many (text, grid lines, checkboxes, the backdrop behind rounded corners).
    sampled = {
        image.pixel(x, y) for x in range(0, image.width(), 20) for y in range(0, image.height(), 20)
    }
    assert len(sampled) > 5


def test_the_seed_produces_the_badges_the_readme_shows(tmp_path) -> None:
    """The seeded data must actually exercise all four ID states.

    If it quietly stopped producing a CONFLICT, the README would keep claiming
    the plugin surfaces disagreements while showing a screenshot that doesn't.
    """
    from merchant_mode.catalog import IdStatus

    plugin, _ctx = cap.build_plugin(tmp_path / "seed")
    statuses = {
        name: plugin.resolve_id(name).status
        for name in ("Cloak of Flames", "Fungus Covered Scale Tunic", "Bone Chips")
    }
    assert statuses["Cloak of Flames"] is IdStatus.CONFIRMED
    assert statuses["Fungus Covered Scale Tunic"] is IdStatus.CONFLICT
    assert statuses["Bone Chips"] is IdStatus.OWNED
    assert plugin.resolve_id("Manastone").status is IdStatus.UNVERIFIED
    assert plugin.resolve_id("Circlet of Shadow") is None


def test_the_seed_spans_two_characters_with_one_going_stale(tmp_path) -> None:
    """The Sell shot exists to show cross-character pooling and staleness."""
    plugin, _ctx = cap.build_plugin(tmp_path / "seed")
    records = plugin.inventories()
    assert {record.character for record in records} == {"Xantik", "Mulebank"}
    assert [record.is_stale(cap.NOW) for record in records] == [False, True]
    # An item held by the mule, listed for sale by the merchant.
    holding = plugin.locate("Rubicite Breastplate")[0]
    assert holding.character == "Mulebank"
    assert "old" in holding.where(cap.NOW)


def test_the_dumps_shot_shows_both_ways_a_dump_arrives(tmp_path) -> None:
    """The Dumps tab's Source column exists to tell the two apart.

    If the seed ever loaded both by hand, the README would keep claiming the
    tab says where a row came from while showing a shot in which every row says
    the same thing.
    """
    from merchant_mode.inventory import ORIGIN_HOST, ORIGIN_MANUAL

    plugin, _ctx = cap.build_plugin(tmp_path / "seed")
    origins = {record.character: record.origin for record in plugin.inventories()}
    assert origins == {"Xantik": ORIGIN_MANUAL, "Mulebank": ORIGIN_HOST}


def test_the_shots_are_dated_from_the_frozen_clock_not_the_wall_clock(tmp_path) -> None:
    """Otherwise a shot changes when nothing changed.

    The seed dates its dumps from :data:`cap.NOW`; the window and the price
    record read ``datetime.now()`` when they render. Freeze one and not the
    other and the fresh character silently goes stale a week after capture.
    """
    from datetime import datetime

    from merchant_mode import window as window_module

    plugin, _ctx = cap.build_plugin(tmp_path / "seed")
    cap.freeze_clock()
    assert window_module.datetime.now() == cap.NOW
    assert datetime.now() != cap.NOW, "the real clock must be left alone"
    record = plugin.market_for("Cloak of Flames")
    assert record.fetched_at == cap.NOW


def test_the_sell_screenshot_shows_a_nearly_full_line_budget(tmp_path) -> None:
    # The point of the Sell shot is the byte meter; keep the seed near the cap.
    plugin, _ctx = cap.build_plugin(tmp_path / "seed")
    result = plugin.build()
    widest = max(
        len(line.encode("latin-1", "replace"))
        for social in result.socials
        for line in social["lines"]
    )
    assert 200 <= widest <= 255


def test_every_readme_shot_still_has_a_recipe() -> None:
    """The README embeds these by name; a renamed tab shouldn't orphan one."""
    assert set(cap.SHOTS) == {
        "window--sell",
        "window--find",
        "window--buy",
        "window--market",
        "window--dumps",
        "window--filters",
        "settings--merchant-mode",
    }


def test_the_seed_gives_the_market_shot_a_split_market(tmp_path) -> None:
    """The Market shot's argument is that a median can hide two markets.

    If the seeded auctions ever stopped disagreeing, the chart would draw a
    tidy cluster and the README would keep claiming the plugin catches this.
    """
    plugin, _ctx = cap.build_plugin(tmp_path / "seed")
    chart = plugin.chart_for("Cloak of Flames")
    assert chart.has_windows and chart.has_observations
    assert chart.sell.wide
    assert chart.sell.count >= 5


def test_the_find_shot_spans_two_characters_with_one_stale(tmp_path) -> None:
    """The Find shot exists to show pooling and age together."""
    plugin, _ctx = cap.build_plugin(tmp_path / "seed")
    found = plugin.find_holdings("long sword")
    assert {match.character for match in found} == {"Xantik", "Mulebank"}
    assert [match.is_stale(cap.NOW) for match in found].count(True) == 1


def test_the_filters_shot_shows_a_rule_of_each_kind(tmp_path) -> None:
    """The Filters shot's argument is that exceptions and typos both show.

    A broad rule catching junk, a KEEP sparing the one bag worth selling, and a
    rule matching nothing: if the seed stopped producing all three, the README
    would keep claiming the tab tells them apart.
    """
    plugin, _ctx = cap.build_plugin(tmp_path / "seed")
    rules = plugin.filters()
    assert rules.hidden("Large Bag")
    assert not rules.hidden("Bag of the Tinkerers")  # the KEEP exception
    assert plugin.hidden_count() > 0
    held = {holding.name for holding in plugin.holdings(include_filtered=True)}
    assert not any(rule.hits(cap_normalize(name)) for name in held for rule in _dead_rules(plugin))


def _dead_rules(plugin):
    """Rules the seed deliberately includes that catch nothing right now."""
    return [rule for rule in plugin.filter_rules() if rule.pattern == "Spiderling Silk"]


def cap_normalize(name: str) -> str:
    from merchant_mode.matching import normalize

    return normalize(name)
