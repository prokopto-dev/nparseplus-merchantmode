#!/usr/bin/env python
"""Generate the README screenshots offscreen. One-shot, but rerunnable.

    uv run python tools/capture_screenshots.py
    uv run python tools/capture_screenshots.py --only window--sell,settings--merchant

Mirrors the approach in the app repo's ``tools/capture_screenshots.py``: each
widget is built under ``QT_QPA_PLATFORM=offscreen``, populated with
synthetic-but-realistic data, and captured with ``QWidget.grab()`` into
``assets/screenshots/<name>.png``. Real renders, not mockups — so the README
cannot drift away from what the plugin actually draws.

Needs the host app installed, since ``PluginWindow`` resolves from it::

    uv pip install -e ".[dev]"

The seeded data is chosen to show the states that are hard to describe in prose:
a nearly-full byte budget on the Sell tab, and all four ID-provenance badges
side by side on the Buy tab — including the CONFLICT case that a real user
would otherwise only meet when a link opened the wrong item.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Must be set before any PySide6 import: QWidget.grab() renders the widget tree
# into a QPixmap and works headless; QScreen.grabWindow() returns blank offscreen.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "assets" / "screenshots"

sys.path.insert(0, str(REPO_ROOT))

# A frozen naive-local clock so timestamps render stably run to run (the whole
# pipeline compares naive datetimes — never introduce tz-aware values here).
NOW = datetime(2026, 7, 30, 21, 0, 0)

# PluginWindow is translucent, so a grab has an alpha channel; compositing over
# a solid panel colour is what makes the rounded corners look right.
PANEL_BACKDROP = "#1b1d23"

# Tall enough for the Sell tab's scope pickers and status line, and for the
# Market tab's chart-over-figures detail panel, which is what actually sets the
# floor — resize() cannot go below a layout's minimum, so a size that's too
# small silently captures a different window than the one it asked for. If this
# starts failing the smoke test, the window's minimum grew: read the new number
# off the failure rather than nudging this one until it sticks.
WINDOW_SIZE = (700, 700)
SETTINGS_SIZE = (520, 420)

# Top-level widgets have no QObject parent, so the only strong reference is the
# local in each cap function. Once that drops the widget is collected, and
# destroying it offscreen can segfault mid-run. Retain them for the process.
_ALIVE: list = []

# Two characters, because a merchant advertises for the whole account. The mule
# is deliberately dumped weeks ago so the Sell tab shows a staleness warning —
# a bag slot from a month back is a guess, and the UI should say so.
DUMPS = {
    "Xantik": (
        timedelta(hours=2),
        "\n".join(
            (
                "Location\tName\tID\tCount\tSlots",
                "Back\tCloak of Flames\t11621\t1\t0",
                "Chest\tFungus Covered Scale Tunic\t2735\t1\t0",
                "Feet\tJourneyman's Boots\t4576\t1\t0",
                "General1\tBag of the Tinkerers\t17403\t1\t10",
                "General1-Slot1\tFine Steel Long Sword\t5350\t1\t0",
                "General1-Slot2\tBone Chips\t13073\t14\t0",
            )
        ),
    ),
    "Mulebank": (
        timedelta(days=31),
        "\n".join(
            (
                "Location\tName\tID\tCount\tSlots",
                "General1-Slot1\tRubicite Breastplate\t9876\t1\t0",
                "General1-Slot2\tRusty Long Sword\t5019\t1\t0",
                "General2\tLarge Bag\t17969\t1\t8",
            )
        ),
    ),
}

# (name, price) for the items ticked in the Sell tab — spanning both characters.
SELLING = [
    ("Cloak of Flames", "5k"),
    ("Fungus Covered Scale Tunic", "12k"),
    ("Journeyman's Boots", "3k"),
    ("Fine Steel Long Sword", "50pp"),
    ("Rubicite Breastplate", ""),  # blank on purpose: Fill prices has work to do
]

# "Circlet of Shadow" is deliberately unknown to every source, so the Buy tab
# demonstrates the "no ID yet" state alongside the resolved ones.
WANTED = [
    "Manastone",
    "Guise of the Deceiver",
    "Rubicite Breastplate",
    "Yaulp IV",
    "Circlet of Shadow",
]

# Auction lines the plugin will parse into its price history, newest last.
#
# Cloak of Flames gets an evening's worth on purpose: it is the item the Market
# shot lands on, and one sighting draws a chart with a single dot on it, which
# documents nothing. The asks deliberately run 4k to 9k, because a split market
# is both common on P99 and the exact thing a median hides — the shot should
# show the plugin saying so rather than the README claiming it does.
AUCTIONS = [
    ("Tradesman", "WTS Manastone 42k | Fine Steel Long Sword 50pp"),
    ("Cheapseller", "WTS Cloak of Flames 4k"),
    ("Buyerguy", "WTB Guise of the Deceiver 98k"),
    ("Twinkfunder", "WTS Cloak of Flames 4.2k"),
    ("Vendorbot", "WTS Bone Chips 2pp ea. | Rusty Long Sword 5pp"),
    ("Barterer", "WTS Cloak of Flames 5.5k"),
    ("Optimist", "WTS Cloak of Flames 9k"),
    ("Xantik", "WTS Cloak of Flames 5k"),
    ("Richguy", "WTB Rubicite Breastplate 60k WTS Yaulp IV 400pp"),
]


def _keep(widget):
    """Retain a top-level widget so it is never GC'd mid-run."""
    _ALIVE.append(widget)
    return widget


def capture(widget, name: str, *, size=None, backdrop: str = PANEL_BACKDROP, pad: int = 0) -> Path:
    """grab() ``widget``, composite it over a solid backdrop, save the PNG."""
    from PySide6.QtWidgets import QApplication

    if size is not None:
        widget.resize(*size)
    widget.show()
    QApplication.processEvents()
    QApplication.processEvents()
    path = _composite_and_save(widget.grab(), name, backdrop, pad)
    widget.hide()
    return path


def _composite_and_save(src, name: str, backdrop: str, pad: int) -> Path:
    from PySide6.QtGui import QColor, QPainter, QPixmap

    out = QPixmap(src.width() + 2 * pad, src.height() + 2 * pad)
    out.fill(QColor(backdrop))
    painter = QPainter(out)
    painter.drawPixmap(pad, pad, src)  # SourceOver: composites the grab's alpha
    painter.end()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.png"
    out.save(str(path))
    print(f"  wrote {name}.png ({out.width()}x{out.height()})")
    return path


class _FakePrice:
    """Shaped like a PigParse ``ItemPrice`` — only the fields we read.

    The whole WTS block, not just the 6-month average: the Market tab shows
    every averaging window beside its sample count, and a seed that filled in
    one of them would produce a screenshot of an empty table.
    """

    def __init__(
        self,
        item_name: str,
        eq_item_id: int | None,
        average: int,
        *,
        samples: int = 0,
        last_seen: datetime | None = None,
    ) -> None:
        self.item_name = item_name
        self.eq_item_id = eq_item_id
        # Narrower windows run slightly hot and thinner, the way a real market
        # does; the counts taper the same way.
        self.total_wts_last_30_days_average = round(average * 1.08)
        self.total_wts_last_30_days_count = max(0, samples // 6)
        self.total_wts_last_90_days_average = round(average * 1.03)
        self.total_wts_last_90_days_count = max(0, samples // 3)
        self.total_wts_last_6_months_average = average
        self.total_wts_last_6_months_count = samples
        self.total_wts_auction_average = round(average * 0.96)
        self.total_wts_auction_count = samples * 4
        self.last_wts_seen = last_seen


def build_plugin(tmp_dir: Path):
    """A plugin seeded with a dump, listings, wanted items, and price history."""
    from nparseplus_sdk.testing import FakePluginContext

    from merchant_mode import MerchantModePlugin, create_plugin
    from merchant_mode.macros import Listing

    ctx = FakePluginContext(MerchantModePlugin.meta)
    plugin = create_plugin()
    plugin.activate(ctx)

    tmp_dir.mkdir(parents=True, exist_ok=True)
    for character, (age, text) in DUMPS.items():
        dump = tmp_dir / f"{character}-Inventory.txt"
        dump.write_text(text, encoding="utf-8")
        plugin.load_dump(dump, character=character, server="green", captured_at=NOW - age)

    holders = {holding.name: holding for holding in plugin.holdings()}
    plugin.set_listings(
        [
            Listing(holders[name].item_id, name, price, character=holders[name].character)
            for name, price in SELLING
            if name in holders
        ]
    )
    plugin.set_wanted(WANTED)

    # Spread over an evening rather than a minute apart: the price chart plots
    # these against time, and nine sightings inside ten minutes draws a vertical
    # smear instead of a trend.
    for offset, (sender, content) in enumerate(AUCTIONS):
        plugin.observe_auction(
            content,
            timestamp=NOW - timedelta(minutes=25 * (len(AUCTIONS) - offset)),
            sender=sender,
        )

    # Prices arrive the way they really do — through the apply half of a submit.
    # The ids are chosen to produce one badge of each kind on the Buy tab:
    #   Guise    agrees with the dump?  no dump entry -> unverified
    #   Fungi    disagrees with the dump              -> CONFLICT
    #   Cloak    agrees with the dump                 -> confirmed
    #   Rubicite no price record at all               -> no ID yet
    plugin._apply_prices(
        [
            _FakePrice("Manastone", 4567, 42000, samples=36, last_seen=NOW - timedelta(days=2)),
            _FakePrice("Guise of the Deceiver", 1234, 98000, samples=18),
            _FakePrice("Yaulp IV", 3312, 400, samples=9),
            _FakePrice(
                "Cloak of Flames", 11621, 5200, samples=54, last_seen=NOW - timedelta(hours=6)
            ),
            _FakePrice("Fungus Covered Scale Tunic", 9999, 12500, samples=27),
        ],
        server="green",
    )
    return plugin, ctx


def build_window(plugin, ctx):
    from nparseplus.config.settings import Settings
    from nparseplus.ui import theme
    from nparseplus_sdk.plugin import PluginWindowContext

    theme.set_theme("dark")
    spec = ctx.windows[0]
    wctx = PluginWindowContext(
        settings=Settings(),
        window_key=f"plugin.merchant-mode.{spec.key}",
        title=spec.title,
        default_geometry=spec.default_geometry,
        on_save=lambda: None,
    )
    return _keep(spec.factory(wctx))


def cap_tab(window, index: int, name: str) -> Path:
    window._tabs.setCurrentIndex(index)
    window._rendered_version = -1
    window.refresh()
    return capture(window, name, size=WINDOW_SIZE)


def cap_settings(ctx, name: str = "settings--merchant-mode") -> Path:
    page = _keep(ctx.settings_pages[0].builder(None))
    return capture(page, name, size=SETTINGS_SIZE)


def cap_market(window, name: str = "window--market") -> Path:
    """The Market tab mid-search, because empty is not what it looks like."""
    from PySide6.QtCore import Qt

    window._tabs.setCurrentIndex(3)
    window._search_entry.setText("cloak of f")
    # Land on the one the seed has prices for — a shot of the detail panel
    # saying "no data yet" documents nothing.
    matches = window._results.findItems("Cloak of Flames", Qt.MatchFlag.MatchExactly)
    window._results.setCurrentItem(matches[0] if matches else window._results.item(0))
    window._rendered_version = -1
    window.refresh()
    return capture(window, name, size=WINDOW_SIZE)


def cap_find(window, name: str = "window--find") -> Path:
    """The Find tab answering a half-remembered name.

    ``long sword`` is chosen because two different characters hold one and the
    mule's is a month stale — so the shot carries the two things prose struggles
    with: the answer pools across characters, and one of the answers is old.
    """
    window._tabs.setCurrentIndex(1)
    window._find_entry.setText("long sword")
    window._rendered_version = -1
    window.refresh()
    return capture(window, name, size=WINDOW_SIZE)


def cap_dumps(window, name: str = "window--dumps") -> Path:
    """The Dumps tab, with one fresh dump and one a month gone."""
    window._tabs.setCurrentIndex(4)
    window._rendered_version = -1
    window.refresh()
    return capture(window, name, size=WINDOW_SIZE)


SHOTS = {
    "window--sell": lambda w, c: cap_tab(w, 0, "window--sell"),
    "window--find": lambda w, c: cap_find(w),
    "window--buy": lambda w, c: cap_tab(w, 2, "window--buy"),
    "window--market": lambda w, c: cap_market(w),
    "window--dumps": lambda w, c: cap_dumps(w),
    "settings--merchant-mode": lambda w, c: cap_settings(c),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate README screenshots offscreen.")
    parser.add_argument("--only", help="comma-separated screenshot names to (re)generate")
    args = parser.parse_args()
    only = set(args.only.split(",")) if args.only else None

    import tempfile

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        plugin, ctx = build_plugin(Path(tmp))
        window = build_window(plugin, ctx)
        for name, shot in SHOTS.items():
            if only is None or name in only:
                shot(window, ctx)
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
