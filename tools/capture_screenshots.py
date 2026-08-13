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


class FrozenClock(datetime):
    """``datetime`` whose ``now()`` is always :data:`NOW`.

    A subclass rather than a stub, because the modules this replaces the name
    in also parse and format with it, and only ``now()`` should move.
    """

    @classmethod
    def now(cls, tz=None):
        return NOW


def freeze_clock() -> None:
    """Point every ``merchant_mode`` module's ``datetime`` at :class:`FrozenClock`.

    Freezing the seed's timestamps was only ever half the job. The plugin
    stamps a fetch time when prices arrive and the window measures every age at
    paint time, both from ``datetime.now()`` — so the shots dated their data
    from July and measured it from whenever the tool happened to run. Two weeks
    after a capture the Sell tab's fresh character had silently gone stale,
    every row grew a "(13d old)" it is not supposed to have, the Market tab
    printed today's date as its fetch time, and the README's prose — one fresh
    character, one mule a month gone — described a screenshot that no longer
    showed it. A screenshot that changes when nothing changed is not a render
    of the plugin, it is a render of the calendar.

    Process-wide, which is what a one-shot capture script wants. It is still
    paired with :func:`thaw_clock`, because the smoke tests import this module
    into a session that goes on to run the rest of the suite — and a plugin test
    measuring a dump's age against a July afternoon fails in a way that points
    anywhere but here.
    """
    import merchant_mode.window  # noqa: F401 - imported for the patch below

    for name, module in sys.modules.items():
        if name.startswith("merchant_mode") and getattr(module, "datetime", None) is datetime:
            module.datetime = FrozenClock


def thaw_clock() -> None:
    """Undo :func:`freeze_clock`. Symmetric, so it needs no bookkeeping."""
    for name, module in sys.modules.items():
        if name.startswith("merchant_mode") and getattr(module, "datetime", None) is FrozenClock:
            module.datetime = datetime

# A grab can carry an alpha channel (rounded corners, a widget that has not
# filled its own background), and compositing over a solid colour is what keeps
# those pixels from saving as a checkerboard. Deliberately not the chrome's
# ``surface``: a backdrop identical to the window's own ground would hide a
# window that had stopped painting one.
PANEL_BACKDROP = "#1b1d23"

# Tall enough for the Sell tab's scope pickers and status line, and for the
# Market tab's chart-over-figures detail panel, which is what actually sets the
# floor — resize() cannot go below a layout's minimum, so a size that's too
# small silently captures a different window than the one it asked for. If this
# starts failing the smoke test, the window's minimum grew: read the new number
# off the failure rather than nudging this one until it sticks.
WINDOW_SIZE = (700, 700)

# Grown from 420 when the markup and rounding rows landed. Word-wrapped labels
# do not clip when a form is too short, they *overlap* — every explanatory
# paragraph reports one line as its minimum and then draws the three it needs
# straight over the row beneath. So this is not a taste number: it is
# ``page.layout().heightForWidth(520)``, which is 490, plus a little air. Read
# it off the layout again rather than guessing if a control is ever added.
SETTINGS_SIZE = (520, 500)

# Top-level widgets have no QObject parent, so the only strong reference is the
# local in each cap function. Once that drops the widget is collected, and
# destroying it offscreen can segfault mid-run. Retain them for the process.
_ALIVE: list = []

# Two characters, because a merchant advertises for the whole account. The mule
# is deliberately dumped weeks ago so the Sell tab shows a staleness warning —
# a bag slot from a month back is a guess, and the UI should say so.
#
# They also arrive by the two different routes, which is the Dumps tab's Source
# column's entire reason for existing: Xantik through the file dialog, Mulebank
# the way nParse+ now files a dump on its own. A seed where both said "By hand"
# would leave the README claiming the tab tells them apart while showing a shot
# in which it never does.
BY_HAND, AUTOMATIC = "manual", "host"

DUMPS = {
    "Xantik": (
        BY_HAND,
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
        AUTOMATIC,
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

# Filter rules for the Filters shot, chosen to show all three states at once:
# a broad rule catching real junk, the exception that spares the one bag worth
# selling, and a rule that catches nothing — which is what a typo looks like.
FILTERS = [
    ("bag", "contains", "hide"),
    ("Bag of the Tinkerers", "exact", "keep"),
    ("Rusty", "prefix", "hide"),
    ("Spiderling Silk", "exact", "hide"),
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


def _as_snapshot(character: str, text: str) -> str:
    """The same rows, in the shape nParse+ stores a dump it filed itself.

    One set of item rows for both routes: a seed that spelled the mule's bags
    out twice would drift the moment one copy was edited, and the point of the
    two routes here is the provenance, not the contents.
    """
    import json

    header, *rows = (line.split("\t") for line in text.splitlines())
    del header  # Location, Name, ID, Count, Slots — positional below
    return json.dumps(
        {
            "schema_version": 1,
            "character": character,
            "kind": "inventory",
            "items": [
                {
                    "location_name": location,
                    "name": name,
                    "item_id": int(item_id),
                    "count": int(count),
                    "slots": int(slots),
                }
                for location, name, item_id, count, slots in rows
            ],
        }
    )


def build_plugin(tmp_dir: Path):
    """A plugin seeded with a dump, listings, wanted items, and price history."""
    from nparseplus_sdk.testing import FakePluginContext

    from merchant_mode import MerchantModePlugin, create_plugin
    from merchant_mode.filters import Action, FilterRule, Match
    from merchant_mode.macros import Listing

    freeze_clock()
    ctx = FakePluginContext(MerchantModePlugin.meta)
    plugin = create_plugin()
    plugin.activate(ctx)

    tmp_dir.mkdir(parents=True, exist_ok=True)
    for character, (origin, age, text) in DUMPS.items():
        if origin == BY_HAND:
            dump = tmp_dir / f"{character}-Inventory.txt"
            dump.write_text(text, encoding="utf-8")
            plugin.load_dump(dump, character=character, server="green", captured_at=NOW - age)
        else:
            # What the host hands over is a path to a stored snapshot, not the
            # original text file — so the seed writes the document rather than
            # calling an ingest helper with the TSV, and the plugin reads it
            # exactly as it does in the app.
            snapshot = tmp_dir / f"{character}-inventory.json"
            snapshot.write_text(_as_snapshot(character, text), encoding="utf-8")
            plugin.ingest_dump_snapshot(
                snapshot, character=character, server="green", captured_at=NOW - age
            )

    holders = {holding.name: holding for holding in plugin.holdings()}
    plugin.set_listings(
        [
            Listing(holders[name].item_id, name, price, character=holders[name].character)
            for name, price in SELLING
            if name in holders
        ]
    )
    plugin.set_wanted(WANTED)
    plugin.add_filters(
        [
            FilterRule(pattern, Match(match), Action(action))
            for pattern, match, action in FILTERS
        ]
    )

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


def dress_app(app, settings):
    """Put the QApplication in the state ``nparseplus.app.create_app`` leaves it.

    Four lines, and without them every shot here is a lie. A bare offscreen
    QApplication uses the platform's default style and its *light* palette, so
    the screenshots this tool produced before v2.0.0 showed a light window that
    no nParse+ user has seen since — the plugin reads its body ink out of the
    QPalette, and offscreen that ink is black.

    So: Fusion (which honours QPalette fully and identically on every platform,
    per ``app.py:237-242``), the app palette and narrow app sheet built from the
    active skin, and the bundled Noto Sans faces the type roles name. This is
    host-internal API, which shipped plugin code may not touch — a dev tool that
    runs only where the app is installed may, and using the host's real chrome
    here rather than the plugin's mirror of it is deliberate: if
    ``merchant_mode/chrome.py`` ever drifts, these shots are where it shows.
    """
    import os

    from nparseplus.helpers import resource_path
    from nparseplus.ui import chromewidgets, skins
    from PySide6.QtGui import QFontDatabase

    app.setStyle("Fusion")
    skins.set_skin(settings.general.skin)
    chromewidgets.apply_app_chrome(app, settings.general.font_size)
    for face in ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"):
        QFontDatabase.addApplicationFont(resource_path(os.path.join("data", "fonts", face)))


def build_window(plugin, ctx):
    """The window as the app builds it, under the app's default skin.

    ``theme.set_theme("dark")`` used to stand here. nParse+ v2.0.0 deleted the
    light theme and the function with it — there is one palette now, and the
    skin (``settings.general.skin``, Duxa by default) is what varies. A default
    ``Settings`` therefore already describes what a new user sees, and the
    window dresses itself from it in ``apply_chrome``.
    """
    from nparseplus.config.settings import Settings
    from nparseplus_sdk import PluginWindowContext
    from PySide6.QtWidgets import QApplication

    freeze_clock()  # again, for the window module, which loads with the factory
    settings = Settings()
    dress_app(QApplication.instance(), settings)
    spec = ctx.windows[0]
    wctx = PluginWindowContext(
        settings=settings,
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
    """The plugin's page as it appears inside the host's Settings window.

    The page itself sets no stylesheet — it is parented into a host window that
    is already wearing one, and the host's sheet is what gives its explanatory
    paragraphs the muted ``ChromeHint`` role. Applying that same sheet here is
    what makes this a screenshot of the page in its window rather than of a
    widget in a vacuum.
    """
    from nparseplus.config.settings import Settings
    from nparseplus.ui import chrome, skins, theme

    page = _keep(ctx.settings_pages[0].builder(None))
    page.setStyleSheet(
        chrome.window_style(skins.skin(), theme.palette(), Settings().general.font_size)
    )
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


def cap_filters(window, name: str = "window--filters") -> Path:
    """The Filters tab with rules that actually catch something.

    The seed adds three: a broad one, the exception that spares the bag worth
    selling, and one that matches nothing — the three states the Hiding column
    exists to tell apart.
    """
    window._tabs.setCurrentIndex(5)
    window._rendered_version = -1
    window.refresh()
    return capture(window, name, size=WINDOW_SIZE)


SHOTS = {
    "window--sell": lambda w, c: cap_tab(w, 0, "window--sell"),
    "window--find": lambda w, c: cap_find(w),
    "window--buy": lambda w, c: cap_tab(w, 2, "window--buy"),
    "window--market": lambda w, c: cap_market(w),
    "window--dumps": lambda w, c: cap_dumps(w),
    "window--filters": lambda w, c: cap_filters(w),
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
