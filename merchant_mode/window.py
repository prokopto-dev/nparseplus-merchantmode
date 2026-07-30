"""Qt pieces of Merchant Mode (imported only inside the running app).

Everything Qt lives here and nowhere else, mirroring the host's own rule that
domain logic never imports PySide6. The window reads plugin state through
:meth:`MerchantModePlugin.snapshot`, polled on a timer and dirty-checked
against a version counter, so the driver thread and the GUI thread never share
a mutable object.

Five tabs: **Sell** (what you own, scoped to one server and character),
**Find** (which mule is holding a thing, across every server), **Buy** (what
you're looking for), **Market** (what anything is worth, and how that's
moving), and **Dumps** (how old everything you're being told actually is).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from nparseplus_sdk.ui import PluginWindow
from PySide6.QtCore import QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPalette, QPen
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
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .catalog import IdStatus
from .chartdata import PriceChart
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

MIN_FIND_QUERY = 2
"""Characters before the Find tab starts searching. One character matches most
of your bags, which is a list rather than an answer."""

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


def _server_label(key: str) -> str:
    return label_for(key) or UNKNOWN_SERVER


def _plural(count: int, word: str, plural: str = "") -> str:
    """``1 dump`` / ``2 dumps``. Used where the count is known and "dump(s)"
    would just be the code showing through."""
    return f"{count:,} {word if count == 1 else (plural or word + 's')}"


def _ink(widget: QWidget) -> QColor:
    """The colour this widget's own text is drawn in.

    Read from the widget palette rather than from ``nparseplus.ui.theme``,
    which would look like the more authoritative source and isn't: the host's
    stylesheet only names its own windows (``#SpellWidget``, ``#ParserWindow``
    and friends) and never sets a global colour, so a plugin window is drawn by
    the application palette. Taking the theme's word for it paints light ink on
    a light panel whenever the app's theme setting and the desktop's appearance
    disagree — which is a setting away at all times.
    """
    return widget.palette().color(QPalette.ColorRole.WindowText)


def _warning_colour(widget: QWidget) -> QColor:
    """A red that survives whichever background this widget is sitting on.

    Same reasoning as :func:`_ink`, and the same reason it isn't a constant: a
    warning drawn in the dark theme's red on a light panel is a warning nobody
    reads.
    """
    background = widget.palette().color(QPalette.ColorRole.Window)
    return QColor("#ff6b5e") if background.lightness() < 128 else QColor("#c62828")


def _alpha(colour: QColor, alpha: int) -> QColor:
    faded = QColor(colour)
    faded.setAlpha(max(0, min(255, alpha)))
    return faded


def _spread_line(chart: PriceChart) -> str:
    """What the live sightings looked like, in words.

    The chart draws the spread; this says it, because a median quoted without
    its range is the number that talks a seller into the wrong price and the
    range is exactly what the eye is worst at reading off a scatter.
    """
    spread = chart.sell or chart.buy
    if spread is None:
        return ""
    side = "WTS" if chart.sell is not None else "WTB"
    line = (
        f"{side} seen {format_price(spread.low)} to {format_price(spread.high)}, "
        f"median {format_price(spread.median)} over {_plural(spread.count, 'sighting')}"
    )
    if spread.wide:
        line += " — a split market; the median is hiding two different asks"
    return line


def _join_notes(parts: list[str], extra: str) -> str:
    """Provenance on one line, spread on the next. Two lines is the reserved
    height of the note label, so a third would paint over the table above it."""
    first = " · ".join(part for part in parts if part)
    return "\n".join(line for line in (first, extra) if line)


CHART_HEIGHT = 148
"""Tall enough for four bars, a scatter and two label strips; short enough that
the detail panel still has room for the tables the numbers live in."""

TABLE_FLOOR = 72
"""Minimum height for a table that shares a panel with the chart.

A QTableWidget asks for a generous height by default, and four of them stacked
in the Market tab pushed the whole window's minimum past the screen. They all
live in a splitter or under a stretch factor, so a small floor costs a couple
of visible rows at the smallest size and nothing at all at a normal one."""

_CHART_PAD = 8
_AXIS_WIDTH = 58
_CAPTION_ROOM = 13
_COUNT_ROOM = 13
_TICK_ROOM = 15
_PANEL_GAP = 14
_BARS_SHARE = 0.42
_SPREAD_COLUMN = 26
_BAR_GAP = 10
_MAX_BAR_WIDTH = 30
_DOT_RADIUS = 3.0

_FAINT = 55
"""Alpha for structure the eye should find only when it looks for it."""


class PriceChartWidget(QWidget):
    """The shape of an item's price, painted by hand.

    Hand-painted because the alternative is a dependency. Every module here
    except this one is stdlib-only, the plugin ships inside nParse+, and
    pulling matplotlib or pyqtgraph into a release zip to draw four bars and
    forty dots is out of proportion to the drawing. QtCharts would be free of
    that objection but is not guaranteed present in the host's Qt build, and a
    chart that renders on the developer's machine and not the user's is worse
    than one that renders everywhere.

    Nothing here decides anything. :mod:`merchant_mode.chartdata` has already
    worked out which windows are trustworthy, what the baseline is, and whether
    there is anything to draw at all; this method's whole job is to put that on
    screen without adding a claim the data doesn't make.

    Two panels share one platinum axis, which is the point: "am I asking above
    or below what the channel is doing tonight" is a comparison, and two
    independently-scaled panels would make it a guess.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chart: PriceChart | None = None
        self.setMinimumHeight(CHART_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_chart(self, chart: PriceChart | None) -> None:
        self._chart = chart
        self.update()

    # --- palette -----------------------------------------------------------
    def _ink(self) -> QColor:
        return _ink(self)

    def _warning(self) -> QColor:
        return _warning_colour(self)

    def _small_font(self):
        font = self.font()
        font.setPointSize(max(7, font.pointSize() - 2))
        return font

    # --- painting ----------------------------------------------------------
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = self._ink()
        painter.setFont(self._small_font())

        area = self.rect().adjusted(_CHART_PAD, _CHART_PAD, -_CHART_PAD, -_CHART_PAD)
        chart = self._chart
        if chart is None or chart.empty or chart.top <= 0:
            painter.setPen(_alpha(ink, 120))
            painter.drawText(
                area,
                Qt.AlignmentFlag.AlignCenter,
                "No price data yet — press Look up, or wait for the channel.",
            )
            painter.end()
            return

        plot_top = area.top() + _CAPTION_ROOM + _COUNT_ROOM
        plot_bottom = area.bottom() - _TICK_ROOM
        if plot_bottom - plot_top < 20:  # too short to mean anything; don't lie
            painter.end()
            return

        usable = area.width() - _AXIS_WIDTH - _PANEL_GAP
        bars_width = int(usable * _BARS_SHARE)
        bars = QRect(area.left() + _AXIS_WIDTH, plot_top, bars_width, plot_bottom - plot_top)
        seen = QRect(
            bars.right() + _PANEL_GAP, plot_top, usable - bars_width, plot_bottom - plot_top
        )

        def y_of(price: int) -> float:
            fraction = max(0.0, min(1.0, price / chart.top))
            return plot_bottom - fraction * (plot_bottom - plot_top)

        self._paint_axis(painter, ink, chart, area, plot_top, plot_bottom)
        self._paint_captions(painter, ink, bars, seen, area.top())
        self._paint_windows(painter, ink, chart, bars, plot_bottom, y_of)
        self._paint_observations(painter, ink, chart, seen, plot_bottom, y_of)
        painter.end()

    def _paint_axis(
        self,
        painter: QPainter,
        ink: QColor,
        chart: PriceChart,
        area: QRect,
        plot_top: int,
        plot_bottom: int,
    ) -> None:
        """Two labelled prices and a floor. Any more gridlines than this and the
        structure starts competing with the forty dots it exists to support."""
        painter.setPen(_alpha(ink, 130))
        for value, y in ((chart.top, plot_top), (0, plot_bottom)):
            painter.drawText(
                QRect(area.left(), int(y) - 8, _AXIS_WIDTH - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                format_price(value) or "0",
            )
        painter.setPen(QPen(_alpha(ink, _FAINT), 1))
        painter.drawLine(area.left() + _AXIS_WIDTH, plot_bottom, area.right(), plot_bottom)
        painter.drawLine(area.left() + _AXIS_WIDTH, plot_top, area.right(), plot_top)

    def _paint_captions(
        self, painter: QPainter, ink: QColor, bars: QRect, seen: QRect, top: int
    ) -> None:
        painter.setPen(_alpha(ink, 150))
        painter.drawText(
            QRect(bars.left(), top, bars.width(), _CAPTION_ROOM),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "PigParse WTS averages",
        )
        painter.drawText(
            QRect(seen.left(), top, seen.width(), _CAPTION_ROOM),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "Seen in /auc",
        )

    def _paint_windows(
        self,
        painter: QPainter,
        ink: QColor,
        chart: PriceChart,
        bars: QRect,
        plot_bottom: int,
        y_of,
    ) -> None:
        """One bar per averaging window, opacity carrying the sample count.

        A 30-day average on two sales must not look as solid as an all-time
        average on two hundred, so it doesn't: the fill alpha is
        :func:`~merchant_mode.chartdata.confidence`, and the count is printed
        above the bar as well, because opacity alone is a feeling and the
        number is the fact.
        """
        if not chart.has_windows:
            painter.setPen(_alpha(ink, 110))
            painter.drawText(bars, Qt.AlignmentFlag.AlignCenter, "no PigParse data")
            return

        slot = bars.width() / max(1, len(chart.windows))
        width = min(_MAX_BAR_WIDTH, max(6.0, slot - _BAR_GAP))
        painter.setPen(Qt.PenStyle.NoPen)
        for index, bar in enumerate(chart.windows):
            centre = bars.left() + slot * (index + 0.5)
            left = centre - width / 2

            if not bar.known:
                painter.setPen(QPen(_alpha(ink, 70), 1, Qt.PenStyle.DotLine))
                painter.drawLine(int(left), plot_bottom, int(left + width), plot_bottom)
                painter.setPen(Qt.PenStyle.NoPen)
            else:
                top = y_of(bar.average)
                painter.setBrush(QBrush(_alpha(ink, int(40 + 175 * bar.confidence))))
                painter.drawRect(QRect(int(left), int(top), int(width), int(plot_bottom - top)))
                painter.setPen(_alpha(ink, 200 if bar.well_sampled else 110))
                painter.drawText(
                    QRect(int(centre - slot / 2), int(top) - _COUNT_ROOM, int(slot), _COUNT_ROOM),
                    Qt.AlignmentFlag.AlignCenter,
                    f"{bar.count:,}",
                )
                painter.setPen(Qt.PenStyle.NoPen)

            painter.setPen(_alpha(ink, 150 if bar.known else 90))
            painter.drawText(
                QRect(int(centre - slot / 2), plot_bottom + 1, int(slot), _TICK_ROOM),
                Qt.AlignmentFlag.AlignCenter,
                bar.key,
            )
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_observations(
        self,
        painter: QPainter,
        ink: QColor,
        chart: PriceChart,
        seen: QRect,
        plot_bottom: int,
        y_of,
    ) -> None:
        """Live sightings over time, against the number they're being judged by.

        WTS is filled and WTB hollow because they are not samples of the same
        quantity — what someone will pay and what someone is asking are two
        different facts, and averaging them together is how a plugin talks a
        seller into the wrong price.
        """
        span = chart.span
        plot = QRect(seen.left(), seen.top(), max(10, seen.width() - _SPREAD_COLUMN), seen.height())

        if chart.baseline > 0:
            y = y_of(chart.baseline)
            painter.setPen(QPen(_alpha(ink, 120), 1, Qt.PenStyle.DashLine))
            painter.drawLine(seen.left(), int(y), seen.right(), int(y))
            painter.setPen(_alpha(ink, 150))
            painter.drawText(
                QRect(seen.left() + 2, int(y) - _COUNT_ROOM - 1, seen.width() - 4, _COUNT_ROOM),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                chart.baseline_label,
            )

        if span is None:
            painter.setPen(_alpha(ink, 110))
            painter.drawText(seen, Qt.AlignmentFlag.AlignCenter, "no sightings yet")
            return

        start, end = span
        total = (end - start).total_seconds()
        painter.setPen(QPen(_alpha(ink, 190), 1))
        for observation in chart.observations:
            # A single instant — or forty auctions in the same minute — has no
            # time axis to spread across, so they stack in the middle rather
            # than pretending to a spread they don't have.
            offset = (observation.timestamp - start).total_seconds()
            fraction = 0.5 if total <= 0 else offset / total
            centre = QPointF(
                plot.left() + fraction * plot.width(),
                y_of(observation.price),
            )
            painter.setBrush(
                Qt.BrushStyle.NoBrush if observation.wanted else QBrush(_alpha(ink, 190))
            )
            painter.drawEllipse(centre, _DOT_RADIUS, _DOT_RADIUS)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        self._paint_spread(painter, ink, chart, seen, y_of)

        painter.setPen(_alpha(ink, 130))
        fmt = "%m-%d" if total > 86400 else "%H:%M"
        painter.drawText(
            QRect(plot.left(), plot_bottom + 1, plot.width(), _TICK_ROOM),
            Qt.AlignmentFlag.AlignLeft,
            start.strftime(fmt),
        )
        if total > 0:
            painter.drawText(
                QRect(plot.left(), plot_bottom + 1, plot.width(), _TICK_ROOM),
                Qt.AlignmentFlag.AlignRight,
                end.strftime(fmt),
            )

    def _paint_spread(
        self, painter: QPainter, ink: QColor, chart: PriceChart, seen: QRect, y_of
    ) -> None:
        """Low, median and high as one column at the right edge.

        Median alone hides a split market, and two very different asks for the
        same item is a common pattern rather than a curiosity. When the spread
        is wide enough to be worth distrusting the median over, the column
        borrows the theme's warning colour and says so.
        """
        spread = chart.sell or chart.buy
        if spread is None:
            return
        colour = self._warning() if spread.wide else ink
        centre = seen.right() - _SPREAD_COLUMN / 2
        painter.setPen(QPen(_alpha(colour, 150), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(int(centre), int(y_of(spread.high)), int(centre), int(y_of(spread.low)))
        painter.setPen(QPen(_alpha(colour, 230), 2))
        painter.drawLine(
            int(centre - _SPREAD_COLUMN / 2 + 2),
            int(y_of(spread.median)),
            int(centre + _SPREAD_COLUMN / 2 - 2),
            int(y_of(spread.median)),
        )


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
        self._tabs.addTab(self._build_find_tab(), "Find")
        self._tabs.addTab(self._build_buy_tab(), "Buy")
        self._tabs.addTab(self._build_market_tab(), "Market")
        self._tabs.addTab(self._build_dumps_tab(), "Dumps")

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

    def _build_find_tab(self) -> QWidget:
        """"Do you have a Fungi?" — answered before the buyer wanders off.

        Deliberately unscoped where the Sell tab is scoped to one server: the
        question here is whether the item is anywhere on the account, so every
        row names its server rather than the list being filtered down to one.
        """
        page = QWidget(self)

        self._find_entry = QLineEdit(page)
        self._find_entry.setPlaceholderText(
            "Who's holding…? Part of a name, a nickname, an acronym"
        )
        self._find_entry.setToolTip(
            "Searches what you're holding, not the item list — the Market tab "
            "does that. Nicknames and acronyms resolve the same way they do "
            "for pricing."
        )
        self._find_entry.textChanged.connect(self._on_find_typed)

        self._find_table = QTableWidget(0, 5, page)
        self._find_table.setHorizontalHeaderLabels(("Item", "Where", "Count", "Server", "Dumped"))
        self._find_table.verticalHeader().setVisible(False)
        self._find_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._find_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        find_header = self._find_table.horizontalHeader()
        # Name and holder are both free text and both the answer; the rest take
        # only what they need.
        find_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        find_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4):
            find_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        self._find_note = QLabel("", page)
        self._find_note.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(self._find_entry)
        layout.addWidget(self._find_table, 1)
        layout.addWidget(self._find_note)
        page.setLayout(layout)
        return page

    def _build_dumps_tab(self) -> QWidget:
        """When each inventory was photographed, and how long ago that was.

        Every location the plugin shows is a moment that may be weeks gone. The
        plugin always knew that — the age was buried in one cell of the Sell
        table, and only once the dump was already past the threshold. This is
        the view you check *before* trusting any of it.
        """
        page = QWidget(self)

        self._dumps_table = QTableWidget(0, 5, page)
        self._dumps_table.setHorizontalHeaderLabels(
            ("Character", "Server", "Items", "Dumped", "Age")
        )
        self._dumps_table.verticalHeader().setVisible(False)
        self._dumps_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._dumps_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._dumps_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        dumps_header = self._dumps_table.horizontalHeader()
        dumps_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        dumps_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 4):
            dumps_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        reload_button = QPushButton("Reload selected", page)
        reload_button.setToolTip(
            "Re-read the dump from the file it came from. Seeing a stale row is "
            "the moment you want this, and re-finding the file in a dialog is "
            "the reason you wouldn't bother."
        )
        reload_button.clicked.connect(self._on_reload_dump)
        forget_button = QPushButton("Forget selected", page)
        forget_button.clicked.connect(self._on_forget_dump)

        buttons = QHBoxLayout()
        buttons.addWidget(reload_button)
        buttons.addWidget(forget_button)
        buttons.addStretch(1)

        self._dumps_summary = QLabel("No inventory loaded.", page)
        self._dumps_summary.setWordWrap(True)

        note = QLabel(
            "Ages come from the dump file's write time, not from the moment you "
            "typed /outputfile. They usually agree — but copying a dump between "
            "machines, restoring a backup, or a syncing folder can reset it, and "
            "then a two-month-old inventory will read as fresh. Staleness is "
            "advisory either way: the plugin cannot know you moved something an "
            "hour after dumping.",
            page,
        )
        note.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(self._dumps_table, 1)
        layout.addLayout(buttons)
        layout.addWidget(self._dumps_summary)
        layout.addWidget(note)
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
        self._results.setMinimumHeight(TABLE_FLOOR)
        self._results.currentTextChanged.connect(self._on_result_selected)
        self._results.itemActivated.connect(lambda _item: self._on_search_submitted())

        detail = QWidget(page)
        self._detail_title = QLabel("Search for an item.", detail)
        self._detail_title.setWordWrap(True)

        # The shape above the numbers, deliberately. Four averages and a live
        # feed have a direction and a spread, and neither survives being read
        # as a column of figures — 30d well above all-time means the item is
        # climbing, and the table alone makes you work that out arithmetically.
        self._detail_chart = PriceChartWidget(detail)

        self._detail_note = QLabel("", detail)
        self._detail_note.setWordWrap(True)
        # A word-wrapped QLabel reports a single line as its minimum height and
        # then draws however many it actually needs, so a two-line provenance
        # string paints straight over the table above it. Reserve the space —
        # three lines now, since the spread gets a line of its own.
        self._detail_note.setMinimumHeight(self._detail_note.fontMetrics().height() * 3 + 4)

        # One column per averaging window, in the same order and under the same
        # short keys the chart's bars use — the table is the exact figures for
        # the shape drawn directly above it, and a reader should not have to
        # transpose between the two. Counts sit under averages because an
        # average without its sample size is not an answer: "5k" from one sale
        # in February and "5k" from forty last week call for different asks.
        self._detail_table = QTableWidget(2, len(WINDOWS), detail)
        self._detail_table.setHorizontalHeaderLabels([window.key for window in WINDOWS])
        self._detail_table.setVerticalHeaderLabels(("WTS average", "Sales"))
        self._detail_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for column, window in enumerate(WINDOWS):
            self._detail_table.horizontalHeaderItem(column).setToolTip(window.label)
            self._detail_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
            self._detail_table.setItem(0, column, _read_only("—"))
            self._detail_table.setItem(1, column, _read_only("—"))
        self._detail_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._detail_seen = QTableWidget(0, 4, detail)
        self._detail_seen.setMinimumHeight(TABLE_FLOOR)
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
        detail_layout.addWidget(self._detail_chart)
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
        self._prices_table.setMinimumHeight(TABLE_FLOOR)
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
        self._render_found()
        self._render_dumps()
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
        after = self._plugin.stale_after()

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
                where = QTableWidgetItem(holding.where(now, after=after))
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
        """Which characters' dumps are in play, and how fresh they are.

        Lives on the Sell tab rather than only on Dumps because a warning you
        have to open a panel to see is a warning you meet after the mistake.
        """
        records = self._plugin.inventories(server=self._current_server())
        if not records:
            return ""
        now = datetime.now()
        after = self._plugin.stale_after()
        stale = [record for record in records if record.is_stale(now, after=after)]
        summary = f"{len(records)} character(s) dumped"
        if stale:
            names = ", ".join(record.character for record in stale[:3])
            summary += f", {len(stale)} stale ({names}) — see the Dumps tab"
        return summary

    # --- find --------------------------------------------------------------
    def _on_find_typed(self, _text: str) -> None:
        self._render_found()

    def _render_found(self) -> None:
        query = self._find_entry.text().strip()
        now = datetime.now()
        after = self._plugin.stale_after()

        if len(query) < MIN_FIND_QUERY:
            self._find_table.setRowCount(0)
            loaded = len(self._plugin.inventories())
            self._find_note.setText(
                f"Type part of an item name. Searches all {_plural(loaded, 'loaded dump')}, "
                "across every server — a Blue mule's Fungi is still an answer to "
                "'do you have one'."
                if loaded
                else "No dumps loaded yet — load one on the Sell tab."
            )
            return

        matches = self._plugin.find_holdings(query)
        self._find_table.setRowCount(len(matches))
        warning = _warning_colour(self)
        for row, match in enumerate(matches):
            self._find_table.setItem(row, 0, _read_only(match.name))
            self._find_table.setItem(row, 1, _read_only(match.where()))
            self._find_table.setItem(row, 2, _read_only(f"{match.count:,}"))
            self._find_table.setItem(row, 3, _read_only(_server_label(match.server)))

            stale = match.is_stale(now, after=after)
            age = _read_only(f"{match.age_text(now)} ago" + (" · stale" if stale else ""))
            age.setToolTip(
                f"{match.character} on {_server_label(match.server)} — "
                f"dumped {match.holding.captured_at:%Y-%m-%d %H:%M}"
            )
            if stale:
                age.setForeground(warning)
            self._find_table.setItem(row, 4, age)

        if matches:
            kinds = {match.kind.label for match in matches}
            self._find_note.setText(
                f"{_plural(len(matches), 'holding')} — matched by {', '.join(sorted(kinds))}. "
                "A location is only as fresh as the dump it came from."
            )
        else:
            self._find_note.setText(
                f"Nothing held matches “{query}”. This searches your bags — use "
                "the Market tab to look up an item you don't own."
            )

    # --- dumps -------------------------------------------------------------
    def _render_dumps(self) -> None:
        records = self._plugin.inventories()
        now = datetime.now()
        after = self._plugin.stale_after()
        warning = _warning_colour(self)

        selected = self._selected_dump()
        self._dumps_table.setRowCount(len(records))
        for row, record in enumerate(records):
            who = _read_only(record.character)
            # The key the action buttons need. Server is carried separately
            # because "" is a real bucket and is not the same as "unspecified".
            who.setData(Qt.ItemDataRole.UserRole, record.character)
            who.setData(Qt.ItemDataRole.UserRole + 1, record.server)
            self._dumps_table.setItem(row, 0, who)
            self._dumps_table.setItem(row, 1, _read_only(_server_label(record.server)))
            self._dumps_table.setItem(row, 2, _read_only(f"{len(record.items):,}"))

            # An absolute timestamp and a relative age, because they answer
            # different questions: "3d ago" is what you scan for, the timestamp
            # is what you check when the relative age looks wrong.
            stamp = _read_only(f"{record.captured_at:%Y-%m-%d %H:%M}")
            stamp.setToolTip(record.source_path or "Loaded from a file that isn't remembered.")
            self._dumps_table.setItem(row, 3, stamp)

            stale = record.is_stale(now, after=after)
            age = _read_only(record.age_text(now) + (" · stale" if stale else ""))
            if stale:
                age.setForeground(warning)
            self._dumps_table.setItem(row, 4, age)

            if (record.character, record.server) == selected:
                self._dumps_table.selectRow(row)

        stale_count = len(self._plugin.stale_dumps(now))
        days = max(1, round(after.total_seconds() / 86400))
        threshold = _plural(days, "day")
        if not records:
            self._dumps_summary.setText("No inventory loaded — load a dump on the Sell tab.")
        elif stale_count:
            self._dumps_summary.setText(
                f"{stale_count} of {_plural(len(records), 'dump')} "
                f"{'is' if stale_count == 1 else 'are'} over {threshold} old. "
                "Reload one and its bag slots are facts again."
            )
        elif len(records) == 1:
            self._dumps_summary.setText(f"The one loaded dump is under {threshold} old.")
        else:
            self._dumps_summary.setText(
                f"All {_plural(len(records), 'dump')} are under {threshold} old."
            )

    def _selected_dump(self) -> tuple[str, str] | None:
        """``(character, server)`` for the selected row, or ``None``."""
        row = self._dumps_table.currentRow()
        cell = self._dumps_table.item(row, 0) if row >= 0 else None
        if cell is None:
            return None
        return (
            str(cell.data(Qt.ItemDataRole.UserRole) or ""),
            str(cell.data(Qt.ItemDataRole.UserRole + 1) or ""),
        )

    def _on_reload_dump(self) -> None:
        chosen = self._selected_dump()
        if chosen is None:
            QMessageBox.information(self, "Merchant Mode", "Pick a dump to reload.")
            return
        character, server = chosen
        if self._plugin.reload_dump(character, server):
            self._rendered_version = -1
            self.refresh()
            return
        # No remembered path, or the file has moved. Say which, and offer the
        # dialog rather than leaving a button that silently does nothing.
        QMessageBox.information(
            self,
            "Merchant Mode",
            f"Couldn't re-read {character}'s dump from where it came from — the "
            "file has moved, or this dump predates the plugin remembering paths.\n\n"
            "Load it again from the Sell tab.",
        )

    def _on_forget_dump(self) -> None:
        chosen = self._selected_dump()
        if chosen is None:
            QMessageBox.information(self, "Merchant Mode", "Pick a dump to forget.")
            return
        character, server = chosen
        confirm = QMessageBox.question(
            self,
            "Merchant Mode",
            f"Forget {character}'s dump on {_server_label(server)}?\n\n"
            "Their items leave the Sell tab and any listings of theirs are dropped. "
            "Loading the dump again brings it all back.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._plugin.forget_character(character, server)
        self._rendered_version = -1
        self.refresh()

    # --- market detail -----------------------------------------------------
    def _size_detail_table(self) -> None:
        """Pin the window table to exactly its two rows.

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
            for column in range(len(WINDOWS)):
                self._detail_table.item(0, column).setText("—")
                self._detail_table.item(1, column).setText("—")
            self._detail_seen.setRowCount(0)
            self._detail_chart.set_chart(None)
            return

        chart = self._plugin.chart_for(name)
        self._detail_chart.set_chart(chart)
        record = self._plugin.market_for(name)
        resolved = self._plugin.resolve_id(name)
        title = name
        if resolved is not None:
            title += f"   ·   id {resolved.item_id} ({_STATUS_BADGE.get(resolved.status, '?')})"
        self._detail_title.setText(title)

        for column, window in enumerate(WINDOWS):
            average = record.average(window.key) if record else 0
            count = record.count(window.key) if record else 0
            self._detail_table.item(0, column).setText(_format_platinum(average))
            self._detail_table.item(1, column).setText(f"{count:,}" if count else "—")

        if record is None:
            self._detail_note.setText(
                _join_notes(
                    [
                        "No PigParse data yet — press Look up to fetch it."
                        if self._plugin.server()
                        else "No PigParse data yet, and no server chosen — "
                        "pick one on the Sell tab."
                    ],
                    _spread_line(chart),
                )
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
            self._detail_note.setText(
                _join_notes(
                    parts or ["PigParse knows this item by name only."], _spread_line(chart)
                )
            )

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

    stale = QSpinBox(page)
    stale.setRange(1, 90)
    stale.setSuffix(" days")
    stale.setValue(int(values.get("stale_days", 7)))
    stale.setObjectName("stale_days")
    form.addRow("Warn about dumps older than", stale)

    stale_note = QLabel(
        "Seven days is right for a mule that never moves and far too generous "
        "for a main. Nothing is blocked either way — the plugin cannot know you "
        "moved something an hour after dumping, so it shows the age and lets "
        "you judge.",
        page,
    )
    stale_note.setWordWrap(True)
    form.addRow(stale_note)

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
    for name in ("pause_tenths", "max_socials", "poll_seconds", "stale_days"):
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
