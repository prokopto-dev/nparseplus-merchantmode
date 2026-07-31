"""Qt pieces of Merchant Mode (imported only inside the running app).

Everything Qt lives here and nowhere else, mirroring the host's own rule that
domain logic never imports PySide6. The window reads plugin state through
:meth:`MerchantModePlugin.snapshot`, polled on a timer and dirty-checked
against a version counter, so the driver thread and the GUI thread never share
a mutable object.

One server picker sits beside the tabs and scopes all of them, because items
cannot move between P99 servers: what you can list, what it is worth, what the
channel said about it and which mule is holding it are all questions about one
server, and a window where two tabs disagreed about which one would be a window
that quietly built the wrong macro.

Six tabs: **Sell** (what you own, by character), **Find** (which mule is
holding a thing), **Buy** (what you're looking for), **Market** (what anything
is worth, and how that's moving), **Dumps** (how old everything you're being
told actually is, across every server — the one deliberate exception), and
**Filters** (the items you never want to see again).
"""

from __future__ import annotations

from dataclasses import replace
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
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
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
from .filters import SUGGESTED_RULES, Action, FilterRule, Match
from .inventory import character_from_filename
from .itemlink import raw_len
from .macros import Listing
from .market import WINDOWS
from .matching import normalize
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

CONFLICT_MARK = " ⚠"
"""Appended to an item's name when its id is disputed.

The Sell tab used to carry a whole ID column to say this, and forty rows of
"owned" is forty rows of nothing — the id itself is a number you never type and
never read. What is worth knowing is the one case where the sources disagree,
because a wrong id fails *silently*: the link shows the right name and only
opens the wrong item when the buyer clicks it. So the exception gets a mark and
a tooltip, and the column is off unless you ask for it in settings."""

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


def _as_ledger(table: QTableWidget) -> None:
    """The shared look for every table of rows in this window.

    Banded rows instead of a full grid. Every one of these tables is read
    *across* — item, price, who's holding it — and a grid line between each
    pair of cells draws the eye down the columns instead, which is the shape of
    a spreadsheet rather than of a list you scan. Banding keeps the row whole
    and still tells adjacent rows apart, which is the one job the grid was
    doing.

    Not applied to the Market tab's window-averages table: that one genuinely
    is a matrix, read both ways, and the grid is what makes it legible.
    """
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    # A floor, not a fixed height: without grid lines the rows need the air to
    # stay distinguishable, and the max() leaves a larger font's own metrics
    # alone rather than squeezing it into a number measured at this one.
    table.verticalHeader().setDefaultSectionSize(
        max(table.verticalHeader().defaultSectionSize(), table.fontMetrics().height() + 10)
    )


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
        """Server the whole window is showing. ``None`` follows the plugin;
        ``""`` is the unfiled bucket, which is why this can't just be a plain
        string."""

        self._tabs = QTabWidget(self)
        self._tabs.setCornerWidget(self._build_server_bar(), Qt.Corner.TopRightCorner)
        self._tabs.addTab(self._build_sell_tab(), "Sell")
        self._tabs.addTab(self._build_find_tab(), "Find")
        self._tabs.addTab(self._build_buy_tab(), "Buy")
        self._tabs.addTab(self._build_market_tab(), "Market")
        self._tabs.addTab(self._build_dumps_tab(), "Dumps")
        # Kept as an attribute so the Sell tab's "Manage filters…" can reach it
        # by identity rather than by an index that shifts when a tab is added.
        self._filters_page = self._build_filters_tab()
        self._tabs.addTab(self._filters_page, "Filters")

        # Everything sits on one opaque panel. PluginWindow is translucent by
        # default — the right call for a spell timer floating over the game, and
        # the wrong one for this: any text that isn't inside the tab pane (the
        # server picker, the tab labels' own strip) was being drawn over
        # whatever happened to be behind the window, which on a dark EverQuest
        # night means dark grey on black. A merchant window is a document you
        # read for minutes at a time, not a HUD you glance at, so it gets a
        # background of its own and every label a guaranteed contrast against
        # it. The frame is what makes the tabs read as attached to a window
        # rather than floating in mid-air.
        panel = QFrame(self)
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel.setAutoFillBackground(True)
        panel_layout = QVBoxLayout(panel)
        panel_layout.addWidget(self._tabs)

        layout = QVBoxLayout()
        # No margin of its own: the panel is the window, and a gap around it
        # would put the translucency back as a border.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panel)
        self.setLayout(layout)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start(REFRESH_INTERVAL_MS)

        self.refresh()
        self.restore_visibility()

    # --- construction ------------------------------------------------------
    def _build_server_bar(self) -> QWidget:
        """The one control that scopes the whole window.

        Outside the tabs, which is not where it used to live: it stopped being
        the Sell tab's business once an item's server started deciding its
        price, its auctions, its WTB entry and where the Find tab looks. A
        picker that scopes every tab belongs where every tab can see it.

        It rides in the tab bar's corner rather than on a strip of its own.
        Two reasons, and the second is why it moved: a lone combo box on a
        full-width row reads as a floating fragment of toolbar, and the strip
        cost the window ~29px of minimum height, enough to push it past the
        700px the README's screenshots are captured at.

        No explanatory sentence beside it any more. It said items can't cross
        servers, which every P99 player already knows from playing; the
        tooltip keeps it for anyone who wonders why the window insists.
        """
        self._server_picker = QComboBox()
        self._server_picker.setToolTip(
            "Everything in this window is about one server: items can't be "
            "traded between them, so prices, auctions and inventories are all "
            "kept apart."
        )
        self._server_picker.currentIndexChanged.connect(self._on_server_picked)

        bar = QWidget()
        layout = QHBoxLayout(bar)
        # Flush with the tab bar: a corner widget sits inside the tab strip, and
        # a layout margin here would drop it below the tabs' baseline.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Server"))
        layout.addWidget(self._server_picker)
        return bar

    def _build_sell_tab(self) -> QWidget:
        page = QWidget(self)

        self._character_picker = QComboBox(page)
        self._character_picker.setToolTip("One character's bags, or everything on this server.")
        self._character_picker.currentIndexChanged.connect(self._on_character_picked)
        # Wide enough for a long character name and no wider. Stretched across
        # the window it read as a search bar rather than as a picker with six
        # entries in it, and a control's width is a claim about its contents.
        self._character_picker.setMinimumWidth(180)

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

        # Buttons at their own width, packed left, with the slack at the end.
        # Three buttons stretched to a third of the window each is the shape of
        # a dialog's footer, not of a toolbar, and it left "Fill prices" three
        # times the size of its label — which reads as three times the weight.
        buttons = QHBoxLayout()
        buttons.addWidget(load)
        buttons.addWidget(fill)
        buttons.addWidget(export)
        buttons.addStretch(1)

        # Two different gestures for the same complaint, and the difference is
        # the whole point. Remove crops this copy of the dump; Filter writes a
        # rule that survives the next twenty dumps on every character.
        remove = QPushButton("Remove selected", page)
        remove.setToolTip(
            "Drop the selected rows from the stored dump. Reloading that "
            "character brings them back — use Filter out for anything you never "
            "want to see again."
        )
        remove.clicked.connect(self._on_remove_items)
        hide = QPushButton("Filter out selected…", page)
        hide.setToolTip(
            "Write a rule that hides these item names, on every character and "
            "every dump from now on. Editable on the Filters tab."
        )
        hide.clicked.connect(self._on_filter_selected)

        self._show_filtered = QCheckBox("Show filtered", page)
        self._show_filtered.setToolTip(
            "Bring the filtered rows back into this list, marked as filtered — "
            "for checking what a rule is actually catching."
        )
        self._show_filtered.toggled.connect(self._on_show_filtered)

        # The checkbox sits with the picker, not with the buttons: both decide
        # what the table shows, while the buttons change what is in it. Mixed
        # into the button row it looked like a fourth thing you could press.
        scope = QHBoxLayout()
        scope.addWidget(QLabel("Character", page))
        scope.addWidget(self._character_picker)
        scope.addStretch(1)
        scope.addWidget(self._show_filtered)

        row = QHBoxLayout()
        row.addWidget(remove)
        row.addWidget(hide)
        row.addStretch(1)

        # ID is a column you never read: it is the same "owned" on every row of
        # a dumped inventory, and the number itself is one nobody types. The
        # one case worth surfacing — the sources disagreeing — rides on the
        # item's own name instead (see CONFLICT_MARK), and the column is here,
        # hidden, for anyone who turns it on in settings.
        self._items_table = QTableWidget(0, 5, page)
        self._items_table.setHorizontalHeaderLabels(("Sell", "Item", "Price", "ID", "Where"))
        self._items_table.verticalHeader().setVisible(False)
        self._items_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._items_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._items_table.setToolTip(
            "Tick what you're selling. Right-click a row for filtering and "
            "removal — the buttons above do the same thing."
        )
        # Right-click is where a merchant looking at a row of junk will reach
        # first, and a feature you can only find by reading the button bar is a
        # feature most people never find. Same actions, met halfway.
        self._items_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._items_table.customContextMenuRequested.connect(self._on_items_context_menu)
        header = self._items_table.horizontalHeader()
        # Name and location are both free text and both worth reading, so they
        # share the slack; the fixed-shape columns take only what they need.
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        _as_ledger(self._items_table)
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
        layout.addLayout(row)
        layout.addWidget(self._items_table, 1)
        layout.addWidget(self._budget)
        layout.addWidget(self._status)
        page.setLayout(layout)
        return page

    def _build_find_tab(self) -> QWidget:
        """"Do you have a Fungi?" — answered before the buyer wanders off.

        Scoped to the chosen server like everything else. The buyer asking is
        standing on one server and can only be sold to there, so a row naming a
        mule on another one is not an answer — it is a line to read past in the
        few seconds this tab exists to save.
        """
        page = QWidget(self)

        self._find_entry = QLineEdit(page)
        self._find_entry.setPlaceholderText(
            "Who's holding…? Part of a name, a nickname, an acronym"
        )
        self._find_entry.setToolTip(
            "Searches what you're holding on this server, not the item list — "
            "the Market tab does that. Nicknames and acronyms resolve the same "
            "way they do for pricing."
        )
        self._find_entry.textChanged.connect(self._on_find_typed)

        # No Server column: every row is on the server named beside the tabs, so
        # a column repeating it forty times is the ID column's mistake again.
        self._find_table = QTableWidget(0, 4, page)
        self._find_table.setHorizontalHeaderLabels(("Item", "Where", "Count", "Dumped"))
        self._find_table.verticalHeader().setVisible(False)
        self._find_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._find_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        _as_ledger(self._find_table)
        find_header = self._find_table.horizontalHeader()
        # Name and holder are both free text and both the answer; the rest take
        # only what they need.
        find_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        find_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3):
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
        _as_ledger(self._dumps_table)
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

    def _build_filters_tab(self) -> QWidget:
        """The list of things you never want to see in an inventory again.

        Most of a dump is not merchandise: food, drink, bone chips, the four
        starting daggers every toon rolls with, a rack of merchant bags. Every
        one of those sits between the two items you actually meant to
        advertise, and deleting them only works until that character dumps
        again. A rule outlives the dump.
        """
        page = QWidget(self)

        self._filter_table = QTableWidget(0, 4, page)
        self._filter_table.setHorizontalHeaderLabels(("On", "Rule", "Pattern", "Matches"))
        self._filter_table.verticalHeader().setVisible(False)
        self._filter_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._filter_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._filter_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        filter_header = self._filter_table.horizontalHeader()
        filter_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in (0, 1, 3):
            filter_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        _as_ledger(self._filter_table)
        self._filter_table.itemChanged.connect(self._on_filter_toggled)

        self._filter_pattern = QLineEdit(page)
        self._filter_pattern.setPlaceholderText("Item name, or part of one")
        self._filter_pattern.returnPressed.connect(self._on_add_filter)

        self._filter_match = QComboBox(page)
        for match in Match:
            self._filter_match.addItem(match.label, str(match))

        self._filter_action = QComboBox(page)
        # Hide first: it is what almost every rule is for, and a picker whose
        # default is the rare case is one people fix after the fact.
        for action in (Action.HIDE, Action.KEEP):
            self._filter_action.addItem(action.label, str(action))
        self._filter_action.setToolTip(
            "Keep is an exception, and it always wins: hide anything containing "
            "“bag”, keep “Bag of the Tinkerers”, in either order."
        )

        add = QPushButton("Add", page)
        add.clicked.connect(self._on_add_filter)

        # Reads as a sentence left to right — "Hide · contains · bag" — so the
        # row needs no label of its own, which is what keeps the window's
        # minimum width where the rest of the tabs put it.
        entry = QHBoxLayout()
        entry.addWidget(self._filter_action)
        entry.addWidget(self._filter_match)
        entry.addWidget(self._filter_pattern, 1)
        entry.addWidget(add)

        remove = QPushButton("Remove selected", page)
        remove.clicked.connect(self._on_remove_filters)
        suggest_button = QPushButton("Add suggested rules", page)
        suggest_button.setToolTip(
            "A short starter list — merchant bags, newbie armour, food and "
            "drink. Nothing is applied until you add it, and every rule is "
            "yours to edit or delete."
        )
        suggest_button.clicked.connect(self._on_add_suggested_filters)

        buttons = QHBoxLayout()
        buttons.addWidget(remove)
        buttons.addWidget(suggest_button)
        buttons.addStretch(1)

        self._filter_summary = QLabel("", page)
        self._filter_summary.setWordWrap(True)

        note = QLabel(
            "Filters hide; they never delete. A filtered item stays in the "
            "dump, stays findable on the Find tab, and comes straight back when "
            "you switch a rule off — and every list that hides something says "
            "how many. Rules are account-wide rather than per server: junk is "
            "junk on every server.",
            page,
        )
        note.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addLayout(entry)
        layout.addWidget(self._filter_table, 1)
        layout.addLayout(buttons)
        layout.addWidget(self._filter_summary)
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
        # Packed left like every other button bar in the window rather than
        # stretched the width of the list above it.
        remove_row = QHBoxLayout()
        remove_row.addWidget(remove)
        remove_row.addStretch(1)

        self._want_list = QListWidget(page)
        self._want_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Banded like the tables: a WTB list is the same kind of thing, and a
        # window where half the lists are striped looks like two windows.
        self._want_list.setAlternatingRowColors(True)

        self._want_note = QLabel("", page)
        self._want_note.setWordWrap(True)

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
        layout.addLayout(remove_row)
        layout.addWidget(self._want_note)
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
        self._results.setAlternatingRowColors(True)
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
        _as_ledger(self._detail_seen)
        self._detail_seen.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )

        seen_box = QGroupBox("Seen in /auc", detail)
        seen_layout = QVBoxLayout()
        seen_layout.addWidget(self._detail_seen)
        seen_box.setLayout(seen_layout)

        detail_layout = QVBoxLayout()
        # The splitter and the page layout already inset this column from the
        # window edge; a third margin here is spacing paid twice, and on a
        # panel this tall it is 18px of the window's minimum height.
        detail_layout.setContentsMargins(0, 0, 0, 0)
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
        _as_ledger(self._prices_table)
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
        self._render_filters(state)
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
        """What the Sell table lists: this server, this character, unfiltered.

        ``include_filtered`` follows the checkbox rather than being fixed, so
        the same call backs both the normal view and the "what is that rule
        actually catching?" view.
        """
        return self._plugin.holdings(
            server=self._current_server(),
            character=self._current_character(),
            include_filtered=self._show_filtered.isChecked(),
        )

    def _render_items(self, state: dict) -> None:
        holdings = self._visible_holdings()
        priced = {listing.name.casefold(): listing.price for listing in state["listings"]}
        selected = set(priced)
        now = datetime.now()
        after = self._plugin.stale_after()
        rules = self._plugin.filters()
        warning = _warning_colour(self)
        ink = _ink(self)
        self._items_table.setColumnHidden(3, not self._plugin.settings().get("show_ids", False))

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
                # Everything the delete and filter buttons need to name this
                # exact row of this exact dump.
                check.setData(Qt.ItemDataRole.UserRole, item.item_id)
                check.setData(Qt.ItemDataRole.UserRole + 1, holding.character)
                check.setData(Qt.ItemDataRole.UserRole + 2, item.location)
                check.setData(Qt.ItemDataRole.UserRole + 3, holding.server)
                self._items_table.setItem(row, 0, check)

                resolved = self._plugin.resolve_id(item.name)
                disputed = resolved is not None and resolved.status is IdStatus.CONFLICT
                hidden_by = rules.reason(item.name)

                name = QTableWidgetItem(item.name + (CONFLICT_MARK if disputed else ""))
                name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
                # The listing is built from this cell, so the real name lives in
                # the data rather than the text — the marks are for the reader.
                name.setData(Qt.ItemDataRole.UserRole, item.name)
                if disputed:
                    name.setForeground(warning)
                    name.setToolTip(
                        f"Your dump says id {resolved.item_id}, PigParse says "
                        f"{resolved.alternate_id}. The link will use "
                        f"{resolved.item_id} — click it in game to be sure. A "
                        "wrong id shows the right name and only misbehaves on "
                        "click."
                    )
                elif hidden_by is not None:
                    # Only reachable with "Show filtered" on, which is exactly
                    # when you want to know which rule caught this.
                    name.setForeground(_alpha(ink, 110))
                    name.setToolTip(f"Filtered — {hidden_by.describe()}")
                self._items_table.setItem(row, 1, name)

                self._items_table.setItem(
                    row, 2, QTableWidgetItem(priced.get(item.name.casefold(), ""))
                )

                badge = QTableWidgetItem(
                    f"{item.item_id} · {_STATUS_BADGE.get(resolved.status, '?')}"
                    if resolved
                    else str(item.item_id)
                )
                badge.setFlags(badge.flags() & ~Qt.ItemFlag.ItemIsEditable)
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
        server = _server_label(self._current_server())
        # The WTB list is per server because the prices beside it are, and a
        # buy price from the wrong server is a number you'd act on.
        self._want_note.setText(f"Your {server} buy list — each server keeps its own.")
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
        """Which characters' dumps are in play, how fresh they are, and what
        is being kept out of the list.

        Lives on the Sell tab rather than only on Dumps because a warning you
        have to open a panel to see is a warning you meet after the mistake —
        and the same goes for the filter count. A list quietly missing rows is
        worse than a cluttered one, so the number is never left unsaid.
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
        hidden = self._plugin.hidden_count(
            server=self._current_server(), character=self._current_character()
        )
        if hidden and not self._show_filtered.isChecked():
            summary += f" · {hidden} hidden by filters"
        return summary

    # --- find --------------------------------------------------------------
    def _on_find_typed(self, _text: str) -> None:
        self._render_found()

    def _render_found(self) -> None:
        query = self._find_entry.text().strip()
        now = datetime.now()
        after = self._plugin.stale_after()
        where = self._current_server()
        server = _server_label(where)

        if len(query) < MIN_FIND_QUERY:
            self._find_table.setRowCount(0)
            loaded = len(self._plugin.inventories(server=where))
            self._find_note.setText(
                f"Type part of an item name. Searches the {_plural(loaded, 'dump')} "
                f"loaded for {server} — a buyer on {server} can't be sold a mule's "
                "Fungi from another server, so this doesn't offer you one."
                if loaded
                else f"No dumps loaded for {server} — load one on the Sell tab."
            )
            return

        matches = self._plugin.find_holdings(query)
        self._find_table.setRowCount(len(matches))
        warning = _warning_colour(self)
        for row, match in enumerate(matches):
            self._find_table.setItem(row, 0, _read_only(match.name))
            self._find_table.setItem(row, 1, _read_only(match.where()))
            self._find_table.setItem(row, 2, _read_only(f"{match.count:,}"))

            stale = match.is_stale(now, after=after)
            age = _read_only(f"{match.age_text(now)} ago" + (" · stale" if stale else ""))
            age.setToolTip(
                f"{match.character} on {_server_label(match.server)} — "
                f"dumped {match.holding.captured_at:%Y-%m-%d %H:%M}"
            )
            if stale:
                age.setForeground(warning)
            self._find_table.setItem(row, 3, age)

        if matches:
            kinds = {match.kind.label for match in matches}
            self._find_note.setText(
                f"{_plural(len(matches), 'holding')} on {server} — matched by "
                f"{', '.join(sorted(kinds))}. A location is only as fresh as the "
                "dump it came from."
            )
        else:
            self._find_note.setText(
                f"Nothing held on {server} matches “{query}”. This searches your "
                "bags on this server — use the Market tab to look up an item you "
                "don't own, and the picker above to look at another server."
            )

    # --- filters -----------------------------------------------------------
    def _render_filters(self, state: dict) -> None:
        rules = state.get("filters", [])
        held = self._plugin.holdings(
            server=self._current_server(),
            character=self._current_character(),
            include_filtered=True,
        )
        names = [holding.name for holding in held]

        self._filter_table.blockSignals(True)
        try:
            self._filter_table.setRowCount(len(rules))
            for row, rule in enumerate(rules):
                switch = QTableWidgetItem()
                switch.setFlags(
                    (switch.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    & ~Qt.ItemFlag.ItemIsEditable
                )
                switch.setCheckState(
                    Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked
                )
                self._filter_table.setItem(row, 0, switch)

                summary = _read_only(f"{rule.action.label} · {rule.match.label}")
                summary.setToolTip(rule.describe())
                self._filter_table.setItem(row, 1, summary)
                self._filter_table.setItem(row, 2, _read_only(rule.pattern))

                # What this rule is doing to the inventory in front of you,
                # rather than in the abstract: a rule that catches nothing is
                # usually a rule with a typo in it. "Matches" rather than
                # "hiding" because a KEEP rule matches in order not to hide.
                caught = sum(1 for name in names if rule.hits(normalize(name)))
                hits = _read_only(f"{caught:,}" if rule.enabled else "—")
                if rule.enabled and not caught:
                    hits.setToolTip("Matches nothing you're currently holding here.")
                self._filter_table.setItem(row, 3, hits)
        finally:
            self._filter_table.blockSignals(False)

        hidden = self._plugin.hidden_count(
            server=self._current_server(), character=self._current_character()
        )
        if not rules:
            self._filter_summary.setText(
                "No rules yet. Add one above, or select the junk on the Sell tab "
                "and press “Filter out selected”."
            )
        else:
            self._filter_summary.setText(
                f"{_plural(len(rules), 'rule')} · hiding {hidden} of "
                f"{_plural(len(names), 'held item')} on "
                f"{_server_label(self._current_server())}."
            )

    def _selected_filter_rows(self) -> list[int]:
        return sorted({index.row() for index in self._filter_table.selectedIndexes()})

    def _on_add_filter(self) -> None:
        pattern = self._filter_pattern.text().strip()
        if not pattern:
            return
        rule = FilterRule(
            pattern=pattern,
            match=Match(str(self._filter_match.currentData())),
            action=Action(str(self._filter_action.currentData())),
        )
        if not self._plugin.add_filters([rule]):
            QMessageBox.information(
                self, "Merchant Mode", "That rule is already in the list (or matches nothing)."
            )
            return
        self._filter_pattern.clear()
        self._reload()

    def _on_remove_filters(self) -> None:
        rows = self._selected_filter_rows()
        if not rows:
            QMessageBox.information(self, "Merchant Mode", "Pick a rule to remove.")
            return
        self._plugin.remove_filters(rows)
        self._reload()

    def _on_add_suggested_filters(self) -> None:
        added = self._plugin.add_filters(list(SUGGESTED_RULES))
        self._reload()
        if not added:
            QMessageBox.information(
                self, "Merchant Mode", "Every suggested rule is already in your list."
            )

    def _on_filter_toggled(self, item: QTableWidgetItem) -> None:
        """A rule switched on or off in place — the list itself is unchanged.

        Off rather than deleted is the point: "is this the rule hiding my
        Fungi?" is a question you answer by turning one off for a moment, not
        by retyping it afterwards.
        """
        if item.column() != 0:
            return
        rules = self._plugin.filter_rules()
        row = item.row()
        if not 0 <= row < len(rules):
            return
        wanted = item.checkState() is Qt.CheckState.Checked
        if rules[row].enabled == wanted:
            return
        rules[row] = replace(rules[row], enabled=wanted)
        self._plugin.set_filters(rules)
        self._reload()

    def _on_show_filtered(self, _checked: bool) -> None:
        self._reload()

    def _on_filter_selected(self) -> None:
        """Turn the selected rows into rules that outlive the dump."""
        names = sorted({name for name, _row in self._selected_items()})
        if not names:
            QMessageBox.information(
                self, "Merchant Mode", "Select the rows you never want to see again."
            )
            return
        listed = "\n".join(f"  · {name}" for name in names[:8])
        if len(names) > 8:
            listed += f"\n  … and {len(names) - 8} more"
        confirm = QMessageBox.question(
            self,
            "Merchant Mode",
            f"Hide {_plural(len(names), 'item')} from every inventory list?\n\n"
            f"{listed}\n\n"
            "Nothing is deleted — these stay in your dumps and stay findable. "
            "Edit or undo it on the Filters tab.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        added = self._plugin.add_filters([FilterRule(name, Match.EXACT) for name in names])
        self._reload()
        if not added:
            QMessageBox.information(self, "Merchant Mode", "Those were already filtered.")

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
            self._reload()
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
        self._reload()

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
        # The server belongs in the heading, not a footnote: PigParse keys its
        # averages on it and the sightings below were heard on one channel, so
        # every number on this panel is an answer about one server only.
        title = f"{name}   ·   {_server_label(self._current_server())}"
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
        self._reload()

    def _on_feed_selected(self) -> None:
        row = self._prices_table.currentRow()
        item = self._prices_table.item(row, 0) if row >= 0 else None
        if item is not None:
            self._detail_name = item.text()
            self._render_detail()

    # --- actions -----------------------------------------------------------
    def _reload(self) -> None:
        """Force a repaint on the next refresh. The version counter suppresses
        redraws that would change nothing, and every action here changes
        something the counter can't see."""
        self._rendered_version = -1
        self.refresh()

    def _on_server_picked(self, _index: int) -> None:
        self._scope = self._current_server()
        # Everything in the window follows this: prices, auctions, the WTB
        # list, the holdings search and the macro pack. A tab left looking at
        # another server would be wrong in a way nothing on screen reveals.
        self._plugin.set_server(self._scope)
        self._reload()

    def _on_character_picked(self, _index: int) -> None:
        state = self._plugin.snapshot()
        self._render_items(state)
        self._render_budget()

    def _selected_items(self) -> list[tuple[str, tuple[str, str, str, int]]]:
        """``(name, (character, server, location, item_id))`` per selected row.

        Read off the row's own stored data rather than its text: the name cell
        can carry a conflict mark, and a listing built from what the label says
        would be a listing for an item that doesn't exist.
        """
        found: list[tuple[str, tuple[str, str, str, int]]] = []
        for row in sorted({index.row() for index in self._items_table.selectedIndexes()}):
            check = self._items_table.item(row, 0)
            name = self._items_table.item(row, 1)
            if check is None or name is None:
                continue
            found.append(
                (
                    str(name.data(Qt.ItemDataRole.UserRole) or name.text()),
                    (
                        str(check.data(Qt.ItemDataRole.UserRole + 1) or ""),
                        str(check.data(Qt.ItemDataRole.UserRole + 3) or ""),
                        str(check.data(Qt.ItemDataRole.UserRole + 2) or ""),
                        int(check.data(Qt.ItemDataRole.UserRole) or 0),
                    ),
                )
            )
        return found

    # --- right-click on the Sell table -------------------------------------
    def _on_items_context_menu(self, point) -> None:
        """Pop the row menu where the pointer is."""
        self._select_row_at(point)
        menu = self._items_menu()
        if menu is not None:
            menu.exec(self._items_table.viewport().mapToGlobal(point))

    def _select_row_at(self, point) -> None:
        """Make the row under ``point`` the selection, unless it already is.

        A row the user right-clicked but had not selected becomes the selection
        first, the way every table in every other application behaves —
        otherwise the menu would act on some other row and the two gestures
        would look unrelated. An existing multi-row selection is left alone,
        which is what makes "select five, right-click one of them, filter them
        all out" work.
        """
        row = self._items_table.rowAt(point.y())
        if row < 0:
            return
        model = self._items_table.selectionModel()
        if model is None or not model.isRowSelected(row):
            self._items_table.selectRow(row)

    def _items_menu(self) -> QMenu | None:
        """The row menu, built from the current selection.

        Split out from the event handler so it can be inspected and triggered
        without a pointer — a menu that only exists inside ``exec()`` is a menu
        no test can read.
        """
        chosen = self._selected_items()
        if not chosen:
            return None
        names = sorted({name for name, _row in chosen})
        menu = QMenu(self._items_table)
        rules = self._plugin.filters()
        already = [name for name in names if rules.hidden(name)]

        # Offering to filter something already filtered is an entry that does
        # nothing, on the one row where the useful action is the opposite one.
        if len(already) < len(names):
            if len(names) == 1:
                # One row, one click, no dialog: the rule is reversible on the
                # Filters tab and the status line reports the new hidden count,
                # so a confirmation here would be friction protecting nothing.
                filter_out = menu.addAction(f"Filter out “{names[0]}”")
                filter_out.triggered.connect(lambda: self._filter_out_exactly(names))
            else:
                filter_out = menu.addAction(f"Filter out these {len(names)} items…")
                filter_out.triggered.connect(self._on_filter_selected)

            # The rule that catches a family rather than a name — "any bag",
            # "any rusty anything" — which is the rule people actually want and
            # the one they would never think to go and write.
            pattern = menu.addAction("Filter out items containing…")
            pattern.triggered.connect(lambda: self._filter_out_containing(names[0]))

        caught = [rule for rule in (rules.reason(name) for name in already) if rule is not None]
        if caught:
            # Only reachable with "Show filtered" on. Being able to undo a rule
            # from the row it is acting on is the other half of being able to
            # write one there.
            culprit = caught[0]
            stop = menu.addAction(f"Stop filtering ({culprit.describe()})")
            stop.triggered.connect(lambda: self._stop_filtering(culprit))

        menu.addSeparator()
        remove = menu.addAction(f"Remove {_plural(len(chosen), 'row')} from this dump…")
        remove.triggered.connect(self._on_remove_items)

        menu.addSeparator()
        manage = menu.addAction("Manage filters…")
        manage.triggered.connect(self._show_filters_tab)
        return menu

    def _show_filters_tab(self) -> None:
        self._tabs.setCurrentWidget(self._filters_page)

    def _filter_out_exactly(self, names: list[str]) -> None:
        added = self._plugin.add_filters([FilterRule(name, Match.EXACT) for name in names])
        self._reload()
        if not added:
            QMessageBox.information(self, "Merchant Mode", "That was already filtered.")

    def _filter_out_containing(self, seed: str) -> None:
        """Ask for the substring, offering the item's name as a starting point.

        Prefilled and selected rather than blank: the useful rule is almost
        always a word out of the name in front of you, and typing it back in
        from memory is how a good idea becomes not worth the bother.
        """
        pattern, accepted = QInputDialog.getText(
            self,
            "Merchant Mode",
            "Hide every item whose name contains:",
            text=seed,
        )
        if not accepted or not pattern.strip():
            return
        rule = FilterRule(pattern.strip(), Match.CONTAINS)
        catches = [
            holding.name
            for holding in self._plugin.holdings(
                server=self._current_server(), include_filtered=True
            )
            if rule.hits(normalize(holding.name))
        ]
        if not catches:
            QMessageBox.information(
                self,
                "Merchant Mode",
                f"Nothing you're holding on this server matches “{pattern.strip()}”.\n\n"
                "The rule would be added and do nothing — check the spelling.",
            )
            return
        listed = "\n".join(f"  · {name}" for name in sorted(set(catches))[:8])
        if len(set(catches)) > 8:
            listed += f"\n  … and {len(set(catches)) - 8} more"
        confirm = QMessageBox.question(
            self,
            "Merchant Mode",
            f"Hide {_plural(len(catches), 'held item')} matching “{pattern.strip()}”?\n\n"
            f"{listed}\n\n"
            "Nothing is deleted, and future dumps are caught by the same rule. "
            "Spare one of them with a Keep rule on the Filters tab.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._plugin.add_filters([rule])
        self._reload()

    def _stop_filtering(self, rule: FilterRule) -> None:
        """Drop one rule, warning when it is hiding more than the row clicked."""
        rules = self._plugin.filter_rules()
        indices = [index for index, other in enumerate(rules) if other.identity == rule.identity]
        if not indices:
            return
        also = [
            holding.name
            for holding in self._plugin.holdings(
                server=self._current_server(), include_filtered=True
            )
            if rule.hits(normalize(holding.name))
        ]
        if len(set(also)) > 1:
            confirm = QMessageBox.question(
                self,
                "Merchant Mode",
                f"{rule.describe()}\n\n"
                f"Removing it brings back {_plural(len(set(also)), 'item')} here, "
                "not just this one.",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self._plugin.remove_filters(indices)
        self._reload()

    def _on_remove_items(self) -> None:
        """Drop the selected rows from the stored dump.

        The counterpart to a filter rule and deliberately weaker than one: this
        crops the copy of the dump the plugin is holding, and the next reload of
        that character brings the rows straight back. Said in the dialog,
        because a delete that silently un-deletes itself is worse than no
        delete at all.
        """
        chosen = self._selected_items()
        if not chosen:
            QMessageBox.information(self, "Merchant Mode", "Select the rows you want gone.")
            return
        names = sorted({name for name, _row in chosen})
        listed = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
        confirm = QMessageBox.question(
            self,
            "Merchant Mode",
            f"Remove {_plural(len(chosen), 'row')} from the stored inventory?\n\n"
            f"{listed}\n\n"
            "This edits the loaded dump, not the file — reloading that character "
            "brings them back. For something you never want to see again, use "
            "“Filter out selected”.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._plugin.remove_items([row for _name, row in chosen])
        self._reload()

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
        self._reload()

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        self._plugin.set_listings(self._collect_listings())
        self._render_budget()

    def _collect_listings(self) -> list[Listing]:
        """Ticked rows, merged with ticks made under another character.

        The table only shows one character at a time, so reading it alone would
        silently drop every item ticked on a different mule the moment the
        picker moved. Ticks on *another server* aren't merged and mustn't be —
        they live in that server's own list.
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
                    # Never the label: it may carry a conflict mark.
                    name=str(name.data(Qt.ItemDataRole.UserRole) or name.text()),
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
        self._reload()

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
        self._reload()

    def _on_remove_wanted(self) -> None:
        drop = {item.row() for item in self._want_list.selectedIndexes()}
        if not drop:
            return
        state = self._plugin.snapshot()
        self._plugin.set_wanted(
            [name for index, name in enumerate(state["wanted"]) if index not in drop]
        )
        self._reload()

    def showEvent(self, event) -> None:  # immediate repaint on reopen
        super().showEvent(event)
        self._reload()


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

    show_ids = QCheckBox("Show the item ID column on the Sell tab", page)
    show_ids.setChecked(bool(values.get("show_ids", False)))
    show_ids.setObjectName("show_ids")
    form.addRow(show_ids)

    ids_note = QLabel(
        "Off by default: for a dumped inventory the column reads “owned” on "
        "every row, and the id itself is a number you never type. The one case "
        "worth knowing about — your dump and PigParse naming different ids for "
        "the same item — marks the item's own name with ⚠ either way.",
        page,
    )
    ids_note.setWordWrap(True)
    form.addRow(ids_note)

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
    for name in ("abbreviate", "show_ids"):
        check = page.findChild(QCheckBox, name)
        if check is not None:
            values[name] = bool(check.isChecked())
    prefix = page.findChild(QLineEdit, "prefix")
    if prefix is not None:
        values["prefix"] = prefix.text()
    return values
