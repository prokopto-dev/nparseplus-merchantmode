"""Qt pieces of Merchant Mode (imported only inside the running app).

Everything Qt lives here and nowhere else, mirroring the host's own rule that
domain logic never imports PySide6. The window reads plugin state through
:meth:`MerchantModePlugin.snapshot`, polled on a timer and dirty-checked
against a version counter, so the driver thread and the GUI thread never share
a mutable object.

Three tabs: **Sell** (what you own, scoped to one server and character),
**Buy** (what you're looking for), and **Market** (what anything is worth).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from nparseplus_sdk.ui import PluginWindow
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .catalog import IdStatus
from .inventory import character_from_filename
from .itemlink import raw_len
from .macros import Listing
from .market import WINDOWS
from .packing import LINE_LIMIT
from .pricing import Side, format_price
from .servers import SERVERS, label_for, normalize_key
from .socialpack import MAX_PAUSE_TENTHS

if TYPE_CHECKING:
    from . import MerchantModePlugin

REFRESH_INTERVAL_MS = 1000

ALL_CHARACTERS = "All characters"
"""Sentinel row in the character picker. There is deliberately no equivalent
for servers: items on different servers can't be sold to the same buyer, so an
all-servers list is one you'd have to mentally re-filter on every read."""

UNKNOWN_SERVER = "Unfiled (no server)"
"""Label for dumps that carry no server — v2 storage, or a dump loaded before
a server was ever chosen. They stay listed and stay obviously unfiled; hiding
them would make a loaded dump look like a failed load."""

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


def _read_only(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class DumpDetailsDialog(QDialog):
    """Asks who this dump belongs to, and where.

    Shown when a dump is loaded with no live session to ask — which is the
    normal case, because you dump inventory and then read it with EQ closed.
    Without a server the dump can't be filed under one in the Sell tab and its
    items can't be priced at all, PigParse being keyed on server.
    """

    def __init__(self, parent: QWidget | None, *, character: str, server: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Load inventory dump")

        self._character = QLineEdit(character, self)
        self._character.setPlaceholderText("Character name")

        self._server = QComboBox(self)
        for entry in SERVERS:
            self._server.addItem(entry.label, entry.key)
        chosen = self._server.findData(normalize_key(server))
        self._server.setCurrentIndex(chosen if chosen >= 0 else 0)

        note = QLabel(
            "Prices are per server, so a dump without one can't be priced. "
            "This is remembered for next time.",
            self,
        )
        note.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Character", self._character)
        form.addRow("Server", self._server)
        form.addRow(note)
        form.addRow(buttons)
        self.setLayout(form)

    def character(self) -> str:
        return self._character.text().strip()

    def server(self) -> str:
        return str(self._server.currentData() or "")


class MerchantModeWindow(PluginWindow):
    """Inventory picker, WTB list, and the market."""

    def __init__(self, wctx: Any, plugin: MerchantModePlugin) -> None:
        super().__init__(wctx)
        self._plugin = plugin
        self._rendered_version = -1
        self._detail_name = ""
        self._scope: str | None = None
        """Server the Sell tab is showing. ``None`` follows the plugin; ``""``
        is the unfiled bucket, which is why this can't just be a plain string."""

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._build_sell_tab(), "Sell")
        self._tabs.addTab(self._build_buy_tab(), "Buy")
        self._tabs.addTab(self._build_market_tab(), "Market")

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

        # Server first, then character: the server decides which prices apply
        # and which characters even exist, so it can't be the inner scope.
        self._server_picker = QComboBox(page)
        self._server_picker.setToolTip(
            "Which server these items are on. Prices are per server."
        )
        self._server_picker.currentIndexChanged.connect(self._on_server_picked)

        self._character_picker = QComboBox(page)
        self._character_picker.setToolTip("One character's bags, or everything on this server.")
        self._character_picker.currentIndexChanged.connect(self._on_character_picked)

        scope = QHBoxLayout()
        scope.addWidget(QLabel("Server", page))
        scope.addWidget(self._server_picker, 1)
        scope.addWidget(QLabel("Character", page))
        scope.addWidget(self._character_picker, 2)

        load = QPushButton("Load inventory dump…", page)
        load.clicked.connect(self._on_load_dump)
        fill = QPushButton("Fill prices", page)
        fill.setToolTip(
            "Fill blank prices from what the channel has been paying, falling "
            "back to PigParse — fetching from PigParse if nothing is known yet. "
            "Prices you typed are left alone."
        )
        fill.clicked.connect(self._on_fill_prices)
        export = QPushButton("Export macro pack…", page)
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

        # Separate from the budget line on purpose: this one is about what the
        # plugin just did or couldn't do, and it used to have nowhere to go.
        self._status = QLabel("", page)
        self._status.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addLayout(scope)
        layout.addLayout(buttons)
        layout.addWidget(self._items_table, 1)
        layout.addWidget(self._budget)
        layout.addWidget(self._status)
        page.setLayout(layout)
        return page

    def _build_buy_tab(self) -> QWidget:
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

    def _build_market_tab(self) -> QWidget:
        page = QWidget(self)

        self._search_entry = QLineEdit(page)
        self._search_entry.setPlaceholderText("Search any item by name…")
        self._search_entry.textChanged.connect(self._on_search_typed)
        self._search_entry.returnPressed.connect(self._on_search_submitted)
        lookup = QPushButton("Look up", page)
        lookup.setToolTip("Ask PigParse about the selected item.")
        lookup.clicked.connect(self._on_search_submitted)

        search_row = QHBoxLayout()
        search_row.addWidget(self._search_entry, 1)
        search_row.addWidget(lookup)

        self._results = QListWidget(page)
        self._results.currentTextChanged.connect(self._on_result_selected)
        self._results.itemActivated.connect(lambda _item: self._on_search_submitted())

        detail = QWidget(page)
        self._detail_title = QLabel("Search for an item.", detail)
        self._detail_title.setWordWrap(True)
        self._detail_note = QLabel("", detail)
        self._detail_note.setWordWrap(True)
        # A word-wrapped QLabel reports a single line as its minimum height and
        # then draws however many it actually needs, so a two-line provenance
        # string paints straight over the table above it. Reserve the space.
        self._detail_note.setMinimumHeight(self._detail_note.fontMetrics().height() * 2 + 4)

        # One row per averaging window. Counts sit beside averages because an
        # average without its sample size is not an answer: "5k" from one sale
        # in February and "5k" from forty last week call for different asks.
        self._detail_table = QTableWidget(len(WINDOWS), 3, detail)
        self._detail_table.setHorizontalHeaderLabels(("Window", "WTS average", "Sales"))
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._detail_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for row, window in enumerate(WINDOWS):
            self._detail_table.setItem(row, 0, _read_only(window.label))
            self._detail_table.setItem(row, 1, _read_only("—"))
            self._detail_table.setItem(row, 2, _read_only("—"))
        self._detail_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._detail_seen = QTableWidget(0, 4, detail)
        self._detail_seen.setHorizontalHeaderLabels(("When", "Price", "Side", "Seller"))
        self._detail_seen.verticalHeader().setVisible(False)
        self._detail_seen.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._detail_seen.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )

        seen_box = QGroupBox("Seen in /auc", detail)
        seen_layout = QVBoxLayout()
        seen_layout.addWidget(self._detail_seen)
        seen_box.setLayout(seen_layout)

        detail_layout = QVBoxLayout()
        detail_layout.addWidget(self._detail_title)
        detail_layout.addWidget(self._detail_table)
        detail_layout.addWidget(self._detail_note)
        detail_layout.addWidget(seen_box, 1)
        detail.setLayout(detail_layout)

        split = QSplitter(Qt.Orientation.Horizontal, page)
        split.addWidget(self._results)
        split.addWidget(detail)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)

        self._prices_table = QTableWidget(0, 4, page)
        self._prices_table.setHorizontalHeaderLabels(("Item", "Price", "Side", "Seller"))
        self._prices_table.verticalHeader().setVisible(False)
        self._prices_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._prices_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._prices_table.itemSelectionChanged.connect(self._on_feed_selected)
        self._prices_empty = QLabel(
            "Nothing auctioned yet — this fills in as nParse+ parses /auction traffic.", page
        )
        self._prices_empty.setWordWrap(True)

        feed_box = QGroupBox("Recent /auc traffic", page)
        feed_layout = QVBoxLayout()
        feed_layout.addWidget(self._prices_empty)
        feed_layout.addWidget(self._prices_table)
        feed_box.setLayout(feed_layout)

        layout = QVBoxLayout()
        layout.addLayout(search_row)
        layout.addWidget(split, 2)
        layout.addWidget(feed_box, 1)
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
        self._render_scope(state)
        self._render_items(state)
        self._render_wanted(state)
        self._render_prices(state)
        self._render_budget()
        self._render_detail()
        self._status.setText(state.get("status", ""))

    def _render_scope(self, state: dict) -> None:
        """Repopulate the server/character pickers without losing the choice."""
        servers = self._plugin.dumped_servers()
        # An explicit pick wins over the plugin's idea of the current server,
        # because "" is a legitimate pick (the unfiled bucket) and would
        # otherwise be indistinguishable from "nothing chosen".
        if self._scope is not None:
            chosen = self._scope
        else:
            chosen = normalize_key(state.get("server")) or (servers[0] if servers else "")
        # Before the first dump there is nothing to list, so offer the lot —
        # picking a server up front is what makes a price lookup possible.
        options = servers or [entry.key for entry in SERVERS]
        if chosen not in options:
            options = [chosen, *options]

        self._server_picker.blockSignals(True)
        try:
            self._server_picker.clear()
            for key in options:
                self._server_picker.addItem(label_for(key) or UNKNOWN_SERVER, key)
            index = self._server_picker.findData(chosen)
            if index >= 0:
                self._server_picker.setCurrentIndex(index)
        finally:
            self._server_picker.blockSignals(False)

        characters = self._plugin.characters_on(self._current_server())
        previous = self._current_character()
        self._character_picker.blockSignals(True)
        try:
            self._character_picker.clear()
            self._character_picker.addItem(ALL_CHARACTERS, "")
            for name in characters:
                self._character_picker.addItem(name, name)
            index = self._character_picker.findData(previous)
            self._character_picker.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._character_picker.blockSignals(False)

    def _current_server(self) -> str:
        return str(self._server_picker.currentData() or "")

    def _current_character(self) -> str:
        return str(self._character_picker.currentData() or "")

    def _visible_holdings(self) -> list:
        return self._plugin.holdings(
            server=self._current_server(), character=self._current_character()
        )

    def _render_items(self, state: dict) -> None:
        holdings = self._visible_holdings()
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
                        f"{holding.character} on {label_for(holding.server) or 'unknown server'} — "
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
            self._prices_table.setItem(row, 0, _read_only(observation.name))
            self._prices_table.setItem(row, 1, _read_only(_format_platinum(observation.price)))
            self._prices_table.setItem(row, 2, _read_only("WTB" if observation.wanted else "WTS"))
            self._prices_table.setItem(row, 3, _read_only(observation.sender))

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
        records = self._plugin.inventories(server=self._current_server())
        if not records:
            return ""
        now = datetime.now()
        stale = [record for record in records if record.is_stale(now)]
        summary = f"{len(records)} character(s) dumped"
        if stale:
            names = ", ".join(record.character for record in stale[:3])
            summary += f", {len(stale)} stale ({names})"
        return summary

    # --- market detail -----------------------------------------------------
    def _size_detail_table(self) -> None:
        """Pin the window table to exactly its four rows.

        Measured from the view rather than computed at construction: row
        heights aren't final until the widget has been through a style pass,
        and guessing early clips the last row under the label beneath it.
        """
        height = (
            self._detail_table.horizontalHeader().height()
            + self._detail_table.verticalHeader().length()
            + 2 * self._detail_table.frameWidth()
        )
        if height > 0 and self._detail_table.height() != height:
            self._detail_table.setFixedHeight(height)

    def _render_detail(self) -> None:
        self._size_detail_table()
        name = self._detail_name
        if not name:
            self._detail_title.setText("Search for an item.")
            source = self._plugin.index_source()
            self._detail_note.setText(
                f"Names come from the {source}, plus everything you've dumped or "
                "overheard. Anything you type is looked up as-is."
                if source
                else "Search any item by name — anything you type is looked up as-is."
            )
            for row in range(len(WINDOWS)):
                self._detail_table.item(row, 1).setText("—")
                self._detail_table.item(row, 2).setText("—")
            self._detail_seen.setRowCount(0)
            return

        record = self._plugin.market_for(name)
        resolved = self._plugin.resolve_id(name)
        title = name
        if resolved is not None:
            title += f"   ·   id {resolved.item_id} ({_STATUS_BADGE.get(resolved.status, '?')})"
        self._detail_title.setText(title)

        for row, window in enumerate(WINDOWS):
            average = record.average(window.key) if record else 0
            count = record.count(window.key) if record else 0
            self._detail_table.item(row, 1).setText(_format_platinum(average))
            self._detail_table.item(row, 2).setText(f"{count:,}" if count else "—")

        if record is None:
            self._detail_note.setText(
                "No PigParse data yet — press Look up to fetch it."
                if self._plugin.server()
                else "No PigParse data yet, and no server chosen — pick one on the Sell tab."
            )
        else:
            best = record.best()
            parts = []
            if best is not None:
                price, window = best
                parts.append(f"Suggested: {format_price(price)} (best of {window.label})")
            if record.last_seen:
                parts.append(f"last WTS seen {record.last_seen:%Y-%m-%d}")
            if record.fetched_at:
                parts.append(f"fetched {record.fetched_at:%Y-%m-%d %H:%M}")
            self._detail_note.setText(" · ".join(parts) or "PigParse knows this item by name only.")

        observations = self._plugin.observations_for(name, limit=50)
        self._detail_seen.setRowCount(len(observations))
        for row, observation in enumerate(observations):
            self._detail_seen.setItem(row, 0, _read_only(f"{observation.timestamp:%m-%d %H:%M}"))
            self._detail_seen.setItem(row, 1, _read_only(_format_platinum(observation.price)))
            self._detail_seen.setItem(row, 2, _read_only("WTB" if observation.wanted else "WTS"))
            self._detail_seen.setItem(row, 3, _read_only(observation.sender))

    def _on_search_typed(self, text: str) -> None:
        query = text.strip()
        self._results.clear()
        if len(query) < 2:
            return
        self._results.addItems(self._plugin.search_items(query, limit=100))

    def _on_result_selected(self, text: str) -> None:
        if text:
            self._detail_name = text
            self._render_detail()

    def _on_search_submitted(self) -> None:
        """Fetch whatever is selected, or whatever was typed.

        Typed text wins when nothing is selected, so an item the index has
        never heard of still reaches PigParse — the index saves typing, it
        doesn't decide what exists.
        """
        current = self._results.currentItem()
        name = (current.text() if current is not None else "") or self._search_entry.text().strip()
        if not name:
            return
        self._detail_name = name
        self._plugin.request_prices([name])
        self._rendered_version = -1
        self.refresh()

    def _on_feed_selected(self) -> None:
        row = self._prices_table.currentRow()
        item = self._prices_table.item(row, 0) if row >= 0 else None
        if item is not None:
            self._detail_name = item.text()
            self._render_detail()

    # --- actions -----------------------------------------------------------
    def _on_server_picked(self, _index: int) -> None:
        self._scope = self._current_server()
        # Keep the plugin's pricing server in step with what's on screen: a
        # price fetched against a server you aren't looking at is wrong in a
        # way nothing on screen would reveal.
        self._plugin.set_server(self._scope)
        self._rendered_version = -1
        self.refresh()

    def _on_character_picked(self, _index: int) -> None:
        state = self._plugin.snapshot()
        self._render_items(state)
        self._render_budget()

    def _on_load_dump(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open inventory dump", "", "Inventory dumps (*.txt);;All files (*)"
        )
        if not path:
            return

        character = self._plugin.active_character()
        server = self._plugin.server()
        if not character or not server:
            # Nothing live to ask, so ask the user. Guessing the server here
            # would file the dump under the wrong one and price it wrongly,
            # and neither mistake looks like a mistake on screen.
            dialog = DumpDetailsDialog(
                self,
                character=character or character_from_filename(path),
                server=server or self._current_server(),
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            character, server = dialog.character(), dialog.server()

        count = self._plugin.load_dump(path, character=character, server=server)
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
        """Ticked rows, merged with ticks made under another scope.

        The table only shows one server and character at a time, so reading it
        alone would silently drop every item ticked on a different mule the
        moment the picker moved.
        """
        visible = {holding.name.casefold() for holding in self._visible_holdings()}
        listings = [
            listing
            for listing in self._plugin.snapshot()["listings"]
            if listing.name.casefold() not in visible
        ]
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
        """Fill from what's known, then go and ask about whatever wasn't.

        The old version only ever read two local caches, both of which are
        empty until a background poll happens to have run — which is why this
        button looked broken. Now the miss is the thing that triggers a fetch.
        """
        self._plugin.set_listings(self._collect_listings())
        changed = self._plugin.fill_prices()
        missing = self._plugin.unpriced_listings()
        self._rendered_version = -1
        self.refresh()

        if missing and self._plugin.request_prices(missing):
            return  # the status line reports it; the timer picks up the result
        if changed or missing:
            return  # filled something, or the fetch declined and said why
        QMessageBox.information(
            self,
            "Merchant Mode",
            "Nothing to fill — every ticked item already has a price.",
        )

    def _on_export(self) -> None:
        listings = self._collect_listings()
        if not listings:
            QMessageBox.information(self, "Merchant Mode", "Tick some items first.")
            return
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "Export Macro Pack",
            self._plugin.suggested_pack_filename(),
            "Macro packs (*.json)",
        )
        if not path:
            return
        try:
            written, result = self._plugin.export_pack(listings, path=path)
        except OSError as exc:
            QMessageBox.warning(self, "Merchant Mode", f"Could not write the file:\n{exc}")
            return
        message = [f"Wrote {len(result.socials)} social(s) to:\n{written}"]
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
