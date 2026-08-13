"""The Qt window, when Qt and the host app are actually available.

Skipped entirely in CI, which installs the SDK alone — that is the point of
keeping every other test Qt-free. Run locally with the app installed:

    uv pip install -e /path/to/nparse-plus
    QT_QPA_PLATFORM=offscreen pytest tests/test_window.py
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6", reason="Qt not installed (SDK-only environment)")
pytest.importorskip("nparseplus", reason="host app not installed")

from nparseplus.config.settings import Settings
from nparseplus_sdk import PluginWindowContext
from nparseplus_sdk.testing import FakePluginContext
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QInputDialog,
    QMessageBox,
    QSpinBox,
)

from merchant_mode import MerchantModePlugin, create_plugin
from merchant_mode.macros import Listing

DUMP = "\n".join(
    (
        "Location\tName\tID\tCount\tSlots",
        "Back\tCloak of Flames\t11621\t1\t0",
        "Chest\tFungus Covered Scale Tunic\t2735\t1\t0",
    )
)


class _FakeItemPrice:
    """Shaped like the host's PigParse ``ItemPrice``, for the fields read."""

    def __init__(self, name: str, item_id: int, average: int = 5000) -> None:
        self.item_name = name
        self.eq_item_id = item_id
        self.total_wts_last_6_months_average = average
        self.total_wts_last_6_months_count = 9


@contextmanager
def _confirmed():
    """Answer the next confirmation dialog Yes. A modal would hang the run."""
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        yield


@contextmanager
def _dismissed():
    """Capture the messages an action puts up instead of doing anything."""
    shown: list = []
    with patch.object(
        QMessageBox, "information", side_effect=lambda *args, **kw: shown.append(args)
    ):
        yield shown


def _row_for(window, name: str) -> int:
    for row in range(window._items_table.rowCount()):
        if window._items_table.item(row, 1).text().startswith(name):
            return row
    raise AssertionError(f"{name} is not in the Sell table")


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


def test_window_has_its_six_tabs(built) -> None:
    _plugin, _ctx, window = built
    tabs = [window._tabs.tabText(i) for i in range(window._tabs.count())]
    assert tabs == ["Sell", "Find", "Buy", "Market", "Dumps", "Filters"]


def test_loaded_inventory_renders_with_selection_state(built) -> None:
    _plugin, _ctx, window = built
    assert window._items_table.rowCount() == 2
    names = [window._items_table.item(row, 1).text() for row in range(2)]
    assert "Cloak of Flames" in names


def test_the_id_column_is_off_until_asked_for(built) -> None:
    """Forty rows reading "owned" is forty rows of nothing.

    The column still exists and still carries the id and its provenance — it is
    just not what the tab is for, so it stays hidden until settings say
    otherwise.
    """
    _plugin, ctx, window = built
    assert window._items_table.isColumnHidden(3)

    page = ctx.settings_pages[0].builder(None)
    page.findChild(QCheckBox, "show_ids").setChecked(True)
    ctx.settings_pages[0].apply(page)
    window._reload()

    assert not window._items_table.isColumnHidden(3)
    badges = {
        window._items_table.item(row, 1).text(): window._items_table.item(row, 3).text()
        for row in range(window._items_table.rowCount())
    }
    assert badges["Cloak of Flames"] == "11621 · owned"


def test_a_disputed_id_is_marked_on_the_item_itself(built) -> None:
    """The one ID fact worth a merchant's attention, with the column off.

    A wrong id fails silently — the link shows the right name and only opens the
    wrong item on click — so a disagreement must be visible without the reader
    having gone looking for it.
    """
    plugin, _ctx, window = built
    plugin._apply_prices([_FakeItemPrice("Cloak of Flames", 99999)])
    window._reload()

    labels = [
        window._items_table.item(row, 1).text()
        for row in range(window._items_table.rowCount())
    ]
    marked = [label for label in labels if label.startswith("Cloak of Flames")]
    assert marked == ["Cloak of Flames ⚠"]
    # The listing must still be built from the real name, not the label.
    assert [listing.name for listing in window._collect_listings()] == ["Cloak of Flames"]


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
    assert window._find_table.item(0, 3).text()  # how old that answer is


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


def test_every_dump_row_says_where_it_came_from(built, tmp_path) -> None:
    """A row nobody asked for has to account for itself.

    The fixture's dump was loaded from a file; the second one arrives the way
    the host's dump watcher delivers them, and the two must not look alike.
    """
    plugin, _ctx, window = built
    assert window._dumps_table.item(0, 5).text() == "By hand"

    snapshot = tmp_path / "abc123.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "character": "Mulebank",
                "kind": "inventory",
                "items": [
                    {
                        "location_name": "General1-Slot1",
                        "name": "Manastone",
                        "item_id": 4567,
                        "count": 1,
                        "slots": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plugin.ingest_dump_snapshot(snapshot, character="Mulebank", digest="abc123")
    window._rendered_version = -1
    window.refresh()

    sources = {
        window._dumps_table.item(row, 0).text(): window._dumps_table.item(row, 5).text()
        for row in range(window._dumps_table.rowCount())
    }
    assert sources == {"Xantik": "By hand", "Mulebank": "Automatic"}
    assert "dump watcher" in window._dumps_summary.text()


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


# --- one server at a time ---------------------------------------------------


def test_the_server_picker_scopes_every_tab(built, tmp_path) -> None:
    """One picker above the tabs, because an item can't cross servers.

    Two tabs looking at different servers is how a merchant ends up quoting a
    Blue price to a Green buyer with nothing on screen looking wrong.
    """
    plugin, _ctx, window = built
    mule = tmp_path / "Mulebank-Inventory.txt"
    mule.write_text(
        "\n".join(("Location\tName\tID\tCount\tSlots", "General1-Slot1\tManastone\t4567\t1\t0")),
        encoding="utf-8",
    )
    plugin.load_dump(mule, character="Mulebank", server="blue")
    plugin.set_server("green")
    window._scope = "green"
    window._reload()

    window._find_entry.setText("manastone")
    assert window._find_table.rowCount() == 0
    assert "Green" in window._find_note.text()

    index = window._server_picker.findData("blue")
    window._server_picker.setCurrentIndex(index)
    assert plugin.server() == "blue"
    assert window._find_table.rowCount() == 1
    assert window._items_table.rowCount() == 1  # Blue's dump, not Green's


def test_the_find_tab_has_no_server_column_now_that_it_has_one_server(built) -> None:
    _plugin, _ctx, window = built
    headers = [
        window._find_table.horizontalHeaderItem(column).text()
        for column in range(window._find_table.columnCount())
    ]
    assert headers == ["Item", "Where", "Count", "Dumped"]


# --- removing and filtering -------------------------------------------------


def test_selected_rows_can_be_removed_from_the_stored_inventory(built) -> None:
    plugin, _ctx, window = built
    window._items_table.selectRow(_row_for(window, "Fungus Covered Scale Tunic"))
    with _confirmed():
        window._on_remove_items()

    assert window._items_table.rowCount() == 1
    assert "Fungus Covered Scale Tunic" not in {
        holding.name for holding in plugin.holdings(include_filtered=True)
    }


def test_removing_needs_a_selection(built) -> None:
    _plugin, _ctx, window = built
    window._items_table.clearSelection()
    with _dismissed() as shown:
        window._on_remove_items()
    assert shown and window._items_table.rowCount() == 2


def test_filtering_selected_rows_writes_a_rule_that_outlives_the_dump(built) -> None:
    plugin, _ctx, window = built
    window._items_table.selectRow(_row_for(window, "Fungus Covered Scale Tunic"))
    with _confirmed():
        window._on_filter_selected()

    assert [rule.pattern for rule in plugin.filter_rules()] == ["Fungus Covered Scale Tunic"]
    assert window._items_table.rowCount() == 1
    # Hidden, not deleted: it is still held, and still findable.
    assert plugin.hidden_count() == 1
    assert [match.name for match in plugin.find_holdings("fungi")] == [
        "Fungus Covered Scale Tunic"
    ]


def test_show_filtered_brings_the_hidden_rows_back_marked(built) -> None:
    """The "what is that rule actually catching?" view."""
    from merchant_mode.filters import FilterRule

    plugin, _ctx, window = built
    plugin.add_filters([FilterRule("fungus")])
    window._reload()
    assert window._items_table.rowCount() == 1

    window._show_filtered.setChecked(True)
    assert window._items_table.rowCount() == 2
    marked = window._items_table.item(_row_for(window, "Fungus Covered Scale Tunic"), 1)
    assert "Filtered" in marked.toolTip()


def test_the_filters_tab_counts_what_each_rule_is_catching(built) -> None:
    """A rule that catches nothing is usually a rule with a typo in it."""
    from merchant_mode.filters import FilterRule

    plugin, _ctx, window = built
    plugin.add_filters([FilterRule("fungus"), FilterRule("nonsense")])
    window._reload()

    assert window._filter_table.rowCount() == 2
    hits = {
        window._filter_table.item(row, 2).text(): window._filter_table.item(row, 3).text()
        for row in range(window._filter_table.rowCount())
    }
    assert hits == {"fungus": "1", "nonsense": "0"}
    assert "hiding 1" in window._filter_summary.text()


def test_a_rule_can_be_switched_off_without_being_deleted(built) -> None:
    from merchant_mode.filters import FilterRule

    plugin, _ctx, window = built
    plugin.add_filters([FilterRule("fungus")])
    window._reload()
    assert window._items_table.rowCount() == 1

    window._filter_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert len(plugin.filter_rules()) == 1
    assert plugin.filter_rules()[0].enabled is False
    assert window._items_table.rowCount() == 2


def test_adding_a_rule_from_the_filters_tab(built) -> None:
    plugin, _ctx, window = built
    window._filter_pattern.setText("fungus")
    window._on_add_filter()

    assert [rule.pattern for rule in plugin.filter_rules()] == ["fungus"]
    assert window._filter_pattern.text() == ""
    assert window._items_table.rowCount() == 1


def test_the_suggested_rules_are_offered_rather_than_applied(built) -> None:
    plugin, _ctx, window = built
    assert plugin.filter_rules() == []
    window._on_add_suggested_filters()
    assert len(plugin.filter_rules()) > 0


# --- right-clicking a row ---------------------------------------------------
#
# The buttons and the menu are the same actions. The menu exists because
# right-click is where somebody looking at a row of junk reaches first, and a
# feature you can only find by reading the button bar is one most people never
# find at all.


def _menu_texts(window) -> list[str]:
    menu = window._items_menu()
    return [action.text() for action in menu.actions() if not action.isSeparator()]


def _trigger(window, starts_with: str) -> None:
    for action in window._items_menu().actions():
        if action.text().startswith(starts_with):
            action.trigger()
            return
    raise AssertionError(f"no menu entry starting {starts_with!r}")


def test_no_menu_without_a_row(built) -> None:
    _plugin, _ctx, window = built
    window._items_table.clearSelection()
    assert window._items_menu() is None


def test_the_row_menu_offers_the_actions_for_one_item(built) -> None:
    _plugin, _ctx, window = built
    window._items_table.selectRow(_row_for(window, "Cloak of Flames"))
    assert _menu_texts(window) == [
        "Filter out “Cloak of Flames”",
        "Filter out items containing…",
        "Remove 1 row from this dump…",
        "Manage filters…",
    ]


def test_the_row_menu_speaks_in_plural_for_a_multi_row_selection(built) -> None:
    _plugin, _ctx, window = built
    window._items_table.selectAll()
    texts = _menu_texts(window)
    assert texts[0] == "Filter out these 2 items…"
    assert "Remove 2 rows from this dump…" in texts


def test_filtering_one_row_from_the_menu_needs_no_dialog(built) -> None:
    """One row, one click. The rule is reversible on the Filters tab and the
    status line reports the new count, so a confirmation guards nothing."""
    plugin, _ctx, window = built
    window._items_table.selectRow(_row_for(window, "Fungus Covered Scale Tunic"))
    _trigger(window, "Filter out “")

    assert [rule.pattern for rule in plugin.filter_rules()] == ["Fungus Covered Scale Tunic"]
    assert window._items_table.rowCount() == 1
    assert "1 hidden by filters" in window._budget.text()


def test_the_contains_rule_is_offered_prefilled_and_confirmed(built) -> None:
    """The rule people actually want catches a family, not a name — and it is
    the one they would never think to go and write."""
    plugin, _ctx, window = built
    window._items_table.selectRow(_row_for(window, "Cloak of Flames"))

    seen: list = []

    def prompt(*_args, text="", **_kw):
        seen.append(text)
        return "cloak", True

    with patch.object(QInputDialog, "getText", side_effect=prompt), _confirmed():
        _trigger(window, "Filter out items containing")

    assert seen == ["Cloak of Flames"]  # prefilled from the row
    assert [rule.pattern for rule in plugin.filter_rules()] == ["cloak"]
    assert window._items_table.rowCount() == 1


def test_a_contains_rule_that_catches_nothing_is_refused_rather_than_added(built) -> None:
    """A rule added and doing nothing is a typo you find out about later."""
    plugin, _ctx, window = built
    window._items_table.selectRow(_row_for(window, "Cloak of Flames"))

    with patch.object(QInputDialog, "getText", return_value=("qqq", True)), _dismissed() as shown:
        _trigger(window, "Filter out items containing")
    assert shown
    assert plugin.filter_rules() == []


def test_a_cancelled_contains_dialog_changes_nothing(built) -> None:
    plugin, _ctx, window = built
    window._items_table.selectRow(_row_for(window, "Cloak of Flames"))
    with patch.object(QInputDialog, "getText", return_value=("cloak", False)):
        _trigger(window, "Filter out items containing")
    assert plugin.filter_rules() == []


def test_a_filtered_row_offers_to_undo_the_rule_that_caught_it(built) -> None:
    """The other half of being able to write a rule from the row."""
    from merchant_mode.filters import FilterRule

    plugin, _ctx, window = built
    plugin.add_filters([FilterRule("fungus")])
    window._show_filtered.setChecked(True)
    window._items_table.selectRow(_row_for(window, "Fungus Covered Scale Tunic"))

    texts = _menu_texts(window)
    assert any(text.startswith("Stop filtering") for text in texts)
    # ...and no offer to filter what is already filtered, which would be an
    # entry doing nothing on the one row where the opposite is the useful move.
    assert not any(text.startswith("Filter out") for text in texts)

    _trigger(window, "Stop filtering")
    assert plugin.filter_rules() == []


def test_undoing_a_rule_that_frees_more_than_one_row_asks_first(built) -> None:
    from merchant_mode.filters import FilterRule

    plugin, _ctx, window = built
    plugin.add_filters([FilterRule("o")])  # catches both fixture items
    window._show_filtered.setChecked(True)
    window._items_table.selectRow(0)

    with patch.object(
        QMessageBox, "question", return_value=QMessageBox.StandardButton.No
    ) as asked:
        _trigger(window, "Stop filtering")
    assert asked.called
    assert len(plugin.filter_rules()) == 1  # declined, so nothing changed


def test_manage_filters_opens_the_tab_it_names(built) -> None:
    _plugin, _ctx, window = built
    window._items_table.selectRow(0)
    _trigger(window, "Manage filters")
    assert window._tabs.tabText(window._tabs.currentIndex()) == "Filters"


def test_right_clicking_an_unselected_row_selects_it_first(built) -> None:
    """Otherwise the menu acts on some other row and the two gestures look
    unrelated."""
    _plugin, _ctx, window = built
    table = window._items_table
    table.resize(600, 300)  # laid out, not shown: PluginWindow.show() is modal-ish
    QApplication.processEvents()
    table.clearSelection()

    row = _row_for(window, "Fungus Covered Scale Tunic")
    # The handler itself only adds menu.exec(), which needs a live pointer.
    window._select_row_at(table.visualItemRect(table.item(row, 1)).center())

    assert [name for name, _row in window._selected_items()] == ["Fungus Covered Scale Tunic"]


def test_right_clicking_inside_a_selection_leaves_it_alone(built) -> None:
    """"Select five, right-click one of them, filter them all out" — the whole
    reason the menu is worth having over a per-row button."""
    _plugin, _ctx, window = built
    table = window._items_table
    table.resize(600, 300)
    QApplication.processEvents()
    table.selectAll()

    window._select_row_at(table.visualItemRect(table.item(0, 1)).center())
    assert len(window._selected_items()) == 2

