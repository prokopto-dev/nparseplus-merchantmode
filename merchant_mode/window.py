"""Qt pieces of Merchant Mode (imported only inside the running app).

Everything Qt lives here and nowhere else, mirroring the host's own rule that
domain logic never imports PySide6. The window reads plugin state through
:meth:`MerchantModePlugin.snapshot`, polled on a timer and dirty-checked
against a version counter, so the driver thread and the GUI thread never share
a mutable object.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from nparseplus_sdk.ui import PluginWindow
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .catalog import IdStatus
from .itemlink import raw_len
from .macros import Listing
from .packing import LINE_LIMIT
from .pricing import Side
from .socialpack import MAX_PAUSE_TENTHS

if TYPE_CHECKING:
    from . import MerchantModePlugin

REFRESH_INTERVAL_MS = 1000

_STATUS_BADGE = {
    IdStatus.OWNED: "owned",
    IdStatus.CONFIRMED: "confirmed",
    IdStatus.UNVERIFIED: "unverified ?",
    IdStatus.CONFLICT: "CONFLICT !",
}


def _format_platinum(value: int | None) -> str:
    """PigParse averages are platinum ints; 0 or missing means never seen."""
    if not value or value <= 0:
        return "—"
    return f"{value:,}pp"


class MerchantModeWindow(PluginWindow):
    """Inventory picker, WTB list, and observed prices."""

    def __init__(self, wctx: Any, plugin: MerchantModePlugin) -> None:
        super().__init__(wctx)
        self._plugin = plugin
        self._rendered_version = -1

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._build_sell_tab(), "Sell")
        self._tabs.addTab(self._build_want_tab(), "Want")
        self._tabs.addTab(self._build_prices_tab(), "Prices")

        layout = QVBoxLayout()
        layout.addWidget(self._tabs)
        self.setLayout(layout)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start(REFRESH_INTERVAL_MS)

        self.refresh()
        self.restore_visibility()

    # --- construction ------------------------------------------------------
    def _build_sell_tab(self) -> QWidget:
        page = QWidget(self)

        load = QPushButton("Load inventory dump…", page)
        load.clicked.connect(self._on_load_dump)
        fill = QPushButton("Fill prices", page)
        fill.setToolTip(
            "Fill blank prices from what the channel has been paying, falling "
            "back to the PigParse average. Prices you typed are left alone."
        )
        fill.clicked.connect(self._on_fill_prices)
        export = QPushButton("Export macro pack", page)
        export.clicked.connect(self._on_export)

        buttons = QHBoxLayout()
        buttons.addWidget(load)
        buttons.addWidget(fill)
        buttons.addWidget(export)

        # The ID column matters more than it looks: CONFIRMED and CONFLICT can
        # only ever arise for items you own, so this tab is the only place a
        # disagreement can surface — and a wrong ID links the wrong item into
        # your auction without anything else on screen looking amiss.
        self._items_table = QTableWidget(0, 5, page)
        self._items_table.setHorizontalHeaderLabels(("Sell", "Item", "Price", "ID", "Where"))
        self._items_table.verticalHeader().setVisible(False)
        header = self._items_table.horizontalHeader()
        # Name and location are both free text and both worth reading, so they
        # share the slack; the fixed-shape columns take only what they need.
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self._items_table.itemChanged.connect(self._on_item_changed)

        self._budget = QLabel("No inventory loaded.", page)
        self._budget.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addLayout(buttons)
        layout.addWidget(self._items_table, 1)
        layout.addWidget(self._budget)
        page.setLayout(layout)
        return page

    def _build_want_tab(self) -> QWidget:
        page = QWidget(self)

        self._want_entry = QLineEdit(page)
        self._want_entry.setPlaceholderText("Item name, then Enter")
        self._want_entry.returnPressed.connect(self._on_add_wanted)
        remove = QPushButton("Remove selected", page)
        remove.clicked.connect(self._on_remove_wanted)

        self._want_list = QListWidget(page)
        self._want_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        note = QLabel(
            "These are items you don't own, so their IDs come from PigParse and "
            "stay unverified until a second source agrees. A wrong ID still shows "
            "the right name — it only misbehaves on click.",
            page,
        )
        note.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(self._want_entry)
        layout.addWidget(self._want_list, 1)
        layout.addWidget(remove)
        layout.addWidget(note)
        page.setLayout(layout)
        return page

    def _build_prices_tab(self) -> QWidget:
        page = QWidget(self)
        self._prices_table = QTableWidget(0, 4, page)
        self._prices_table.setHorizontalHeaderLabels(("Item", "Price", "Side", "Seller"))
        self._prices_table.verticalHeader().setVisible(False)
        self._prices_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._prices_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._prices_empty = QLabel("Nothing auctioned yet.", page)
        self._prices_empty.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(self._prices_empty)
        layout.addWidget(self._prices_table, 1)
        page.setLayout(layout)
        return page

    # --- refresh -----------------------------------------------------------
    def _on_refresh_tick(self) -> None:
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        state = self._plugin.snapshot()
        if state["version"] == self._rendered_version:
            return
        self._rendered_version = state["version"]
        self._render_items(state)
        self._render_wanted(state)
        self._render_prices(state)
        self._render_budget()

    def _render_items(self, state: dict) -> None:
        holdings = state["holdings"]
        priced = {listing.name.casefold(): listing.price for listing in state["listings"]}
        selected = set(priced)
        now = datetime.now()

        self._items_table.blockSignals(True)
        try:
            self._items_table.setRowCount(len(holdings))
            for row, holding in enumerate(holdings):
                item = holding.item
                check = QTableWidgetItem()
                check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(
                    Qt.CheckState.Checked
                    if item.name.casefold() in selected
                    else Qt.CheckState.Unchecked
                )
                check.setData(Qt.ItemDataRole.UserRole, item.item_id)
                check.setData(Qt.ItemDataRole.UserRole + 1, holding.character)
                self._items_table.setItem(row, 0, check)

                name = QTableWidgetItem(item.name)
                name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._items_table.setItem(row, 1, name)

                self._items_table.setItem(
                    row, 2, QTableWidgetItem(priced.get(item.name.casefold(), ""))
                )

                resolved = self._plugin.resolve_id(item.name)
                badge = QTableWidgetItem(
                    _STATUS_BADGE.get(resolved.status, "?") if resolved else "?"
                )
                badge.setFlags(badge.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if resolved is not None and resolved.status is IdStatus.CONFLICT:
                    badge.setToolTip(
                        f"Your dump says {resolved.item_id}, PigParse says "
                        f"{resolved.alternate_id}. The link will use "
                        f"{resolved.item_id} — click it in game to be sure."
                    )
                self._items_table.setItem(row, 3, badge)

                # Which character is holding it, and how old that knowledge is.
                # A dump is a photograph: the plugin cannot know you moved
                # something afterwards, so it shows the age rather than
                # presenting a week-old bag slot as present fact.
                where = QTableWidgetItem(holding.where(now))
                where.setFlags(where.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if holding.captured_at:
                    where.setToolTip(
                        f"{holding.character} on {holding.server or 'unknown server'} — "
                        f"dumped {holding.captured_at:%Y-%m-%d %H:%M}"
                    )
                self._items_table.setItem(row, 4, where)
        finally:
            self._items_table.blockSignals(False)

    def _render_wanted(self, state: dict) -> None:
        self._want_list.clear()
        for name in state["wanted"]:
            resolved = self._plugin.resolve_id(name)
            badge = _STATUS_BADGE.get(resolved.status, "unknown") if resolved else "no ID yet"
            # What it would cost to buy one, and where that figure came from —
            # an unattributed number is one the user has to take on faith.
            proposal = self._plugin.suggest_price(name, side=Side.BUY)
            price = f"{proposal.text} ({proposal.source.label})" if proposal.known else "—"
            self._want_list.addItem(f"{name}  [{badge}]  {price}")

    def _render_prices(self, state: dict) -> None:
        rows = state["history"]
        self._prices_empty.setVisible(not rows)
        self._prices_table.setVisible(bool(rows))
        self._prices_table.setRowCount(len(rows))
        for row, observation in enumerate(rows):
            self._prices_table.setItem(row, 0, QTableWidgetItem(observation.name))
            self._prices_table.setItem(
                row, 1, QTableWidgetItem(_format_platinum(observation.price))
            )
            self._prices_table.setItem(
                row, 2, QTableWidgetItem("WTB" if observation.wanted else "WTS")
            )
            self._prices_table.setItem(row, 3, QTableWidgetItem(observation.sender))

    def _render_budget(self) -> None:
        result = self._plugin.build()
        if not result.socials:
            self._budget.setText(self._sources_line() or "Tick items to sell and give them prices.")
            return
        widest = max(
            (raw_len(line) for social in result.socials for line in social["lines"]), default=0
        )
        parts = [
            f"{len(result.socials)} social(s), {result.line_count} lines, "
            f"widest {widest}/{LINE_LIMIT} bytes"
        ]
        if result.unplaced:
            parts.append(f"{len(result.unplaced)} item(s) didn't fit — raise max socials")
        if result.oversized:
            names = ", ".join(entry.label for entry in result.oversized[:3])
            parts.append(f"too long even alone: {names} — add a nickname")
        sources = self._sources_line()
        if sources:
            parts.append(sources)
        self._budget.setText(" · ".join(parts))

    def _sources_line(self) -> str:
        """Which characters' dumps are in play, and how fresh they are."""
        records = self._plugin.inventories()
        if not records:
            return ""
        now = datetime.now()
        stale = [record for record in records if record.is_stale(now)]
        summary = f"{len(records)} character(s) dumped"
        if stale:
            names = ", ".join(record.character for record in stale[:3])
            summary += f", {len(stale)} stale ({names})"
        return summary

    # --- actions -----------------------------------------------------------
    def _on_load_dump(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open inventory dump", "", "Inventory dumps (*.txt);;All files (*)"
        )
        if not path:
            return
        count = self._plugin.load_dump(path)
        if not count:
            QMessageBox.warning(
                self,
                "Merchant Mode",
                "That file isn't an /outputfile inventory dump, or held nothing sellable.",
            )
        self._rendered_version = -1
        self.refresh()

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        self._plugin.set_listings(self._collect_listings())
        self._render_budget()

    def _collect_listings(self) -> list[Listing]:
        listings: list[Listing] = []
        for row in range(self._items_table.rowCount()):
            check = self._items_table.item(row, 0)
            name = self._items_table.item(row, 1)
            price = self._items_table.item(row, 2)
            if check is None or name is None:
                continue
            if check.checkState() is not Qt.CheckState.Checked:
                continue
            listings.append(
                Listing(
                    item_id=int(check.data(Qt.ItemDataRole.UserRole) or 0),
                    name=name.text(),
                    price=price.text() if price is not None else "",
                    character=str(check.data(Qt.ItemDataRole.UserRole + 1) or ""),
                )
            )
        return listings

    def _on_fill_prices(self) -> None:
        self._plugin.set_listings(self._collect_listings())
        changed = self._plugin.fill_prices()
        self._rendered_version = -1
        self.refresh()
        if not changed:
            QMessageBox.information(
                self,
                "Merchant Mode",
                "Nothing to fill — every ticked item already has a price, or "
                "none of the blanks have been seen in /auction or priced by "
                "PigParse yet.",
            )

    def _on_export(self) -> None:
        listings = self._collect_listings()
        if not listings:
            QMessageBox.information(self, "Merchant Mode", "Tick some items first.")
            return
        path, result = self._plugin.export_pack(listings)
        message = [f"Wrote {len(result.socials)} social(s) to:\n{path}"]
        if result.unplaced:
            message.append(f"\n{len(result.unplaced)} item(s) didn't fit.")
        if result.oversized:
            message.append(f"\n{len(result.oversized)} item(s) were too long for a line.")
        message.append("\n\nImport it from the Macro Editor.")
        QMessageBox.information(self, "Merchant Mode", "".join(message))

    def _on_add_wanted(self) -> None:
        name = self._want_entry.text().strip()
        if not name:
            return
        state = self._plugin.snapshot()
        self._plugin.set_wanted([*state["wanted"], name])
        self._want_entry.clear()
        self._rendered_version = -1
        self.refresh()

    def _on_remove_wanted(self) -> None:
        drop = {item.row() for item in self._want_list.selectedIndexes()}
        if not drop:
            return
        state = self._plugin.snapshot()
        self._plugin.set_wanted(
            [name for index, name in enumerate(state["wanted"]) if index not in drop]
        )
        self._rendered_version = -1
        self.refresh()

    def showEvent(self, event) -> None:  # immediate repaint on reopen
        super().showEvent(event)
        self._rendered_version = -1
        self.refresh()


def build_settings_page(parent: QWidget | None, values: dict) -> QWidget:
    page = QWidget(parent)
    form = QFormLayout()

    pause = QSpinBox(page)
    pause.setRange(0, MAX_PAUSE_TENTHS)
    pause.setSuffix(" tenths of a second")
    pause.setValue(int(values.get("pause_tenths", 30)))
    pause.setObjectName("pause_tenths")
    form.addRow("Pause between macro lines", pause)

    pause_note = QLabel(
        "A social fires every line at once, so a pause keeps a multi-line macro "
        "from spamming the channel. It costs line slots: with a pause set, a "
        "social carries 3 content lines instead of 5. Set 0 to disable.",
        page,
    )
    pause_note.setWordWrap(True)
    form.addRow(pause_note)

    socials = QSpinBox(page)
    socials.setRange(1, 10)
    socials.setValue(int(values.get("max_socials", 4)))
    socials.setObjectName("max_socials")
    form.addRow("Max socials per export", socials)

    poll = QSpinBox(page)
    poll.setRange(60, 3600)
    poll.setSuffix(" s")
    poll.setValue(int(values.get("poll_seconds", 600)))
    poll.setObjectName("poll_seconds")
    form.addRow("Price poll interval", poll)

    abbreviate = QCheckBox("Abbreviate item names in links", page)
    abbreviate.setChecked(bool(values.get("abbreviate", True)))
    abbreviate.setObjectName("abbreviate")
    form.addRow(abbreviate)

    prefix = QLineEdit(page)
    prefix.setText(str(values.get("prefix", "/auc WTS ")))
    prefix.setObjectName("prefix")
    form.addRow("Line prefix", prefix)

    page.setLayout(form)
    return page


def read_settings_page(page: QWidget) -> dict:
    values: dict = {}
    for name in ("pause_tenths", "max_socials", "poll_seconds"):
        spin = page.findChild(QSpinBox, name)
        if spin is not None:
            values[name] = int(spin.value())
    check = page.findChild(QCheckBox, "abbreviate")
    if check is not None:
        values["abbreviate"] = bool(check.isChecked())
    prefix = page.findChild(QLineEdit, "prefix")
    if prefix is not None:
        values["prefix"] = prefix.text()
    return values
