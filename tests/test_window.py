"""The Qt window, when Qt and the host app are actually available.

Skipped entirely in CI, which installs the SDK alone — that is the point of
keeping every other test Qt-free. Run locally with the app installed:

    uv pip install -e /path/to/nparse-plus
    QT_QPA_PLATFORM=offscreen pytest tests/test_window.py
"""

from __future__ import annotations

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


def test_window_has_the_three_tabs(built) -> None:
    _plugin, _ctx, window = built
    tabs = [window._tabs.tabText(i) for i in range(window._tabs.count())]
    assert tabs == ["Sell", "Want", "Prices"]


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
