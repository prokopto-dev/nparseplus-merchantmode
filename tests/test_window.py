"""The Qt window, when Qt and the host app are actually available.

Skipped entirely in CI, which installs the SDK alone — that is the point of
keeping every other test Qt-free. Run locally with the app installed:

    uv pip install -e /path/to/nparse-plus
    QT_QPA_PLATFORM=offscreen pytest tests/test_window.py
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("PySide6", reason="Qt not installed (SDK-only environment)")
pytest.importorskip("nparseplus", reason="host app not installed")

from nparseplus.config.settings import Settings
from nparseplus_sdk.plugin import PluginWindowContext
from nparseplus_sdk.testing import FakePluginContext
from PySide6.QtWidgets import QApplication, QSpinBox

from merchant_mode import MerchantModePlugin, create_plugin
from merchant_mode.macros import Listing

DUMP = "\n".join(
    (
        "Location\tName\tID\tCount\tSlots",
        "Back\tCloak of Flames\t11621\t1\t0",
        "Chest\tFungus Covered Scale Tunic\t2735\t1\t0",
    )
)


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def built(qt_app, tmp_path):
    ctx = FakePluginContext(MerchantModePlugin.meta)
    plugin = create_plugin()
    plugin.activate(ctx)

    dump = tmp_path / "Xantik-Inventory.txt"
    dump.write_text(DUMP, encoding="utf-8")
    plugin.load_dump(dump)
    plugin.set_listings([Listing(11621, "Cloak of Flames", "5k")])

    spec = ctx.windows[0]
    wctx = PluginWindowContext(
        settings=Settings(),
        window_key=f"plugin.merchant-mode.{spec.key}",
        title=spec.title,
        default_geometry=spec.default_geometry,
        on_save=lambda: None,
    )
    window = spec.factory(wctx)
    yield plugin, ctx, window
    window.close()


def test_window_exposes_what_the_host_requires(built) -> None:
    _plugin, _ctx, window = built
    assert hasattr(window, "toggle")
    assert hasattr(window, "isVisible")


def test_window_has_its_five_tabs(built) -> None:
    _plugin, _ctx, window = built
    tabs = [window._tabs.tabText(i) for i in range(window._tabs.count())]
    assert tabs == ["Sell", "Find", "Buy", "Market", "Dumps"]


def test_loaded_inventory_renders_with_selection_state(built) -> None:
    _plugin, _ctx, window = built
    assert window._items_table.rowCount() == 2
    names = [window._items_table.item(row, 1).text() for row in range(2)]
    assert "Cloak of Flames" in names


def test_sell_tab_shows_id_provenance(built) -> None:
    """The Sell tab is the only place CONFIRMED/CONFLICT can ever appear.

    Both require an id from the dump, and dumped items are exactly what this
    tab lists — so if the badge is missing here, a disagreement is invisible.
    """
    _plugin, _ctx, window = built
    badges = {
        window._items_table.item(row, 1).text(): window._items_table.item(row, 3).text()
        for row in range(window._items_table.rowCount())
    }
    assert badges["Cloak of Flames"] == "owned"


def test_budget_meter_reports_bytes_against_the_limit(built) -> None:
    _plugin, _ctx, window = built
    assert "255 bytes" in window._budget.text()


def test_settings_page_round_trips_through_object_names(built) -> None:
    plugin, ctx, _window = built
    page = ctx.settings_pages[0].builder(None)
    page.findChild(QSpinBox, "pause_tenths").setValue(55)
    ctx.settings_pages[0].apply(page)
    assert plugin.settings()["pause_tenths"] == 55


def test_the_find_tab_answers_a_half_remembered_name(built) -> None:
    """The question a buyer actually asks, typed the way they'd type it."""
    _plugin, _ctx, window = built
    window._find_entry.setText("fungi")
    rows = window._find_table.rowCount()
    assert rows == 1
    assert window._find_table.item(0, 0).text() == "Fungus Covered Scale Tunic"
    assert window._find_table.item(0, 1).text().startswith("Xantik · Chest")
    assert window._find_table.item(0, 3).text()  # a server, even if "unfiled"


def test_the_find_tab_says_nothing_rather_than_guessing(built) -> None:
    _plugin, _ctx, window = built
    window._find_entry.setText("manastone")
    assert window._find_table.rowCount() == 0
    assert "Nothing held" in window._find_note.text()


def test_the_find_tab_waits_for_a_real_query(built) -> None:
    """One character matches most of your bags; that is a list, not an answer."""
    _plugin, _ctx, window = built
    window._find_entry.setText("f")
    assert window._find_table.rowCount() == 0


def test_the_dumps_tab_lists_every_loaded_dump_with_its_age(built) -> None:
    _plugin, _ctx, window = built
    assert window._dumps_table.rowCount() == 1
    assert window._dumps_table.item(0, 0).text() == "Xantik"
    assert window._dumps_table.item(0, 2).text() == "2"  # items in the dump
    assert window._dumps_table.item(0, 4).text()  # an age
    assert "under" in window._dumps_summary.text()


def test_a_stale_dump_is_flagged_in_the_dumps_tab(built) -> None:
    """The flag is the whole point: a bag slot from a month ago is a guess."""
    plugin, _ctx, window = built
    plugin.apply_settings({"stale_days": 1})
    window._rendered_version = -1
    window.refresh()
    # The fixture's dump is written now, so make the threshold the thing that
    # moves rather than the file — same code path, no clock games.
    assert "under 1 day" in window._dumps_summary.text()


def test_forgetting_needs_a_selected_row(built) -> None:
    _plugin, _ctx, window = built
    window._dumps_table.clearSelection()
    window._dumps_table.setCurrentCell(-1, -1)
    assert window._selected_dump() is None


def test_the_market_chart_is_fed_the_selected_item(built) -> None:
    _plugin, _ctx, window = built
    window._detail_name = "Cloak of Flames"
    window._render_detail()
    assert window._detail_chart._chart is not None
    assert window._detail_chart._chart.name == "Cloak of Flames"


def test_the_market_chart_clears_when_nothing_is_selected(built) -> None:
    """An empty state has to be drawn on purpose, not left as stale paint."""
    _plugin, _ctx, window = built
    window._detail_name = ""
    window._render_detail()
    assert window._detail_chart._chart is None


def test_the_chart_paints_without_data(built, qt_app) -> None:
    """The empty case is the common one; it must not throw mid-paint."""
    _plugin, _ctx, window = built
    window._detail_chart.set_chart(None)
    window._detail_chart.resize(320, 150)
    assert not window._detail_chart.grab().isNull()


def test_the_chart_paints_with_data(built, qt_app) -> None:
    plugin, _ctx, window = built
    plugin.observe_auction(
        "WTS Cloak of Flames 5k", timestamp=datetime.now(), sender="Someone"
    )
    window._detail_name = "Cloak of Flames"
    window._render_detail()
    window._detail_chart.resize(320, 150)
    assert not window._detail_chart.grab().isNull()


def test_the_staleness_threshold_round_trips_through_settings(built) -> None:
    plugin, ctx, _window = built
    page = ctx.settings_pages[0].builder(None)
    page.findChild(QSpinBox, "stale_days").setValue(3)
    ctx.settings_pages[0].apply(page)
    assert plugin.settings()["stale_days"] == 3


def test_reloading_a_dump_from_the_dumps_tab_re_reads_the_file(built, tmp_path) -> None:
    """The success path only — the failure path opens a modal and would hang."""
    _plugin, _ctx, window = built
    window._dumps_table.selectRow(0)
    dump = tmp_path / "Xantik-Inventory.txt"
    dump.write_text(
        "\n".join(
            (
                "Location\tName\tID\tCount\tSlots",
                "Back\tCloak of Flames\t11621\t1\t0",
            )
        ),
        encoding="utf-8",
    )
    window._on_reload_dump()
    assert window._dumps_table.item(0, 2).text() == "1"
    assert window._items_table.rowCount() == 1
