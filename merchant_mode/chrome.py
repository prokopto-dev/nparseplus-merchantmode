"""A local copy of the host's config-surface design tokens.

nParse+ v2.0.0 deleted the light theme, generated one app-wide stylesheet, and
started dressing every config window — Settings, the editors, the console — in
the active skin. Plugin windows get none of that. ``nparseplus_sdk.ui`` exports
exactly one name, ``PluginWindow``, and it applies no styling at all: its whole
body forwards to the overlay base class, which has never heard of a skin. So a
plugin window inherits Fusion, the dark QPalette, Noto Sans and the skinned
``QMenu``/``QToolTip`` (the app sheet is deliberately narrow — ``#Id`` selectors
plus a menu allowlist) and nothing else. Next to a skinned Settings window it
reads as a window from a different program.

The fix is to mirror the tokens here, because the alternative is to import
``nparseplus.ui.chrome``, and everything in ``nparseplus.ui`` is host-internal:
absent from CI, absent from ``nparseplus-plugin validate``, absent from a
frozen build's importable surface at the moment a window is being built, and
covered by no compatibility promise. ``tests/test_imports.py`` fails such an
import on sight.

**This is a copy, and a copy can drift.** Every constant below names the
upstream file and line it came from, and ``tests/test_chrome.py`` pins the
resulting values, so the day nParse+ repaints itself the divergence shows up as
a failing assertion and a readable diff rather than as a window that is quietly
the wrong colour. Read the current upstream values with::

    git -C <nparseplus checkout> show origin/master:src/nparseplus/ui/theme.py

Mirrored against nParse+ **v2.8.0**.

The rule the whole layer turns on, quoted from ``ui/chrome.py:13``:

    **the palette owns value, the skin owns hue.**

Ground, field backgrounds and body text always come from the palette — the
readability floor, which no skin may move. The skin contributes exactly one hue,
at the accent positions: selection, focus rings, group-box titles, hairlines,
the selected tab's underline. So a Velious user's Merchant Mode is
unmistakably Velious without ever putting gold text on a gold field.

Qt-free and stdlib-only, like the upstream module and for the same reason: it
is data plus pure functions returning stylesheet strings, so the whole thing is
testable without a live window. ``window.py`` is the only module here that
imports PySide6.
"""

from __future__ import annotations

from dataclasses import dataclass

# -- the object-name contract ---------------------------------------------------
# Mirrors ui/chrome.py:51-58. The strings are the host's own, deliberately: a
# label this window stamps ``ChromeHint`` on is already wearing the right name
# if the host ever does start dressing plugin windows itself, and a test can
# assert that every name the sheet styles is a name some widget wears.

HINT = "ChromeHint"  # ui/chrome.py:52 — de-emphasised caption under a field
CAPTION = "ChromeCaption"  # ui/chrome.py:53 — SECTION CAPS above a group
TITLE = "ChromeTitle"  # ui/chrome.py:54 — a chrome window's own heading

# ROOT/BADGE/CARD/SIDEBAR (ui/chrome.py:51, 55-57) are omitted: this window has
# no status pill, no pickable tile and no page sidebar, and a token with no
# widget behind it is a token nobody notices has gone stale.

NOTO_SANS = "Noto Sans"
"""ui/skins.py:52. The app registers the bundled regular and bold faces before
any window is constructed, so naming the family in every role keeps the window
deterministic where the desktop's default font differs by platform."""


@dataclass(frozen=True)
class Palette:
    """The app's colour *values* — ui/theme.py:27.

    A subset: the fields ``chrome_for`` reads, plus ``warning_text``. The full
    upstream palette also carries overlay tokens (``panel_bg``, ``bar_track``,
    the DPS header fills) that describe windows floating over EverQuest, which
    this plugin does not have and would only copy in order to let them rot.
    """

    name: str
    text: str  # ui/theme.py:30 — primary label colour
    heading: str  # ui/theme.py:31 — bold group/header text
    warning_text: str  # ui/theme.py:33 — the one red that survives this ground
    map_input_bg: str  # ui/theme.py:37 — "an input field", aliased by chrome_for
    map_input_text: str  # ui/theme.py:38
    map_input_border: str  # ui/theme.py:39
    surface: str  # ui/theme.py:43 — a config window's ground
    surface_alt: str  # ui/theme.py:44 — a raised strip on it
    hint: str  # ui/theme.py:45 — de-emphasised caption under a field
    disabled: str  # ui/theme.py:46 — a control the user cannot reach right now


DARK = Palette(
    # ui/theme.py:49-66. The only palette there is: the light alternative was
    # deleted in v2.0.0 (nParse+ renders on top of EverQuest, where a pale panel
    # is a flashbang), which is what lets this window stop asking the widget
    # palette which way round it is.
    name="dark",
    text="#dddddd",  # ui/theme.py:52
    heading="#ffffff",  # ui/theme.py:53
    warning_text="#ff5044",  # ui/theme.py:55
    map_input_bg="#050505",  # ui/theme.py:59
    map_input_text="white",  # ui/theme.py:60
    map_input_border="#333",  # ui/theme.py:61
    surface="#16171b",  # ui/theme.py:62
    surface_alt="#1d1f24",  # ui/theme.py:63
    hint="#8b8f9a",  # ui/theme.py:64
    disabled="#5a5e69",  # ui/theme.py:65
)


@dataclass(frozen=True)
class Skin:
    """The four fields of a host ``Skin`` that reach a config surface.

    Upstream's ``Skin`` (ui/skins.py:81) has around fifty, and the other
    forty-six are frame geometry, bar fills and title-bar gems — the parts that
    dress an overlay. ``chrome_for`` reads exactly these four, so exactly these
    four are worth keeping in sync.
    """

    name: str
    chrome_accent: str  # the one hue a skin lends the chrome
    title_color: str  # text that sits on the accent band
    chrome_band: tuple[str, ...]  # the selection band's fill
    glass_border: str  # hairline / border between sections


SKINS: dict[str, Skin] = {
    "duxa": Skin(
        # ui/skins.py:204-260. Thin double-line frame, black glass, tan caps.
        name="duxa",
        chrome_accent="#c8a951",  # ui/skins.py:224
        title_color="#d4b675",  # ui/skins.py:217
        chrome_band=("rgba(107, 90, 58, 77)", "rgba(107, 90, 58, 15)"),  # ui/skins.py:225
        glass_border="#2b2519",  # ui/skins.py:213
    ),
    "velious": Skin(
        # ui/skins.py:262-327. The full classic frame: beveled stone, gems,
        # engraved gold caps. Loudest of the three.
        name="velious",
        chrome_accent="#e2c882",  # ui/skins.py:282
        title_color="#f0dcae",  # ui/skins.py:275
        chrome_band=("#5c4d31", "#332a1c"),  # ui/skins.py:283
        glass_border="#6b5a3a",  # ui/skins.py:271
    ),
    "ledger": Skin(
        # ui/skins.py:329-386. The Duxa frame, quieter — least to scan.
        name="ledger",
        chrome_accent="#8a7549",  # ui/skins.py:350
        title_color="#8a7549",  # ui/skins.py:342
        chrome_band=("rgba(107, 90, 58, 56)",),  # ui/skins.py:351
        glass_border="#2b2519",  # ui/skins.py:338
    ),
}

DEFAULT_SKIN = "duxa"
"""config/settings.py:94 — ``general.skin`` defaults to Duxa. Also the fallback
for a name this copy has never heard of, which is what a *new* host skin looks
like from in here: an unknown name must dress the window in something rather
than raise inside a window factory."""

DEFAULT_FONT_SIZE = 12
"""config/settings.py:90 — ``general.font_size``. Only ever reached when the
host's settings object is absent, i.e. under the SDK's ``FakePluginContext``."""

# -- shared layout ---------------------------------------------------------------
# ui/chrome.py:92-94. Plain px on purpose: these are gutters and touch targets,
# not type, so they must NOT grow with the user's font size the way the
# typography multipliers below do.

PAGE_MARGINS = (10, 10, 10, 10)  # ui/chrome.py:92
ROW_SPACING = 6  # ui/chrome.py:93
SECTION_SPACING = 12  # ui/chrome.py:94

# -- semantic accents ------------------------------------------------------------
# ui/chrome.py:103-112. Named for what they MEAN rather than what they look
# like, so the token a site uses is the token it means.

GOOD = "#2f9e6e"  # ui/chrome.py:103
BAD = "#c0392b"  # ui/chrome.py:104
ROLL = "#d99b2b"  # ui/chrome.py:107 — also the "wants attention" tone
PILL_TEXT = "#ffffff"  # ui/chrome.py:112 — text on a filled accent


@dataclass(frozen=True)
class TypographyRole:
    """A type token resolved from ``general.font_size`` — ui/skins.py:56.

    ``tracking_em`` is stored proportionally and converted to px by
    :func:`typography_style`, because Qt stylesheets do not understand em.
    """

    scale: float
    weight: str = "normal"
    tracking_em: float = 0.0


SMALL_DISPLAY = TypographyRole(scale=0.78, weight="bold", tracking_em=0.18)
"""ui/skins.py:70. Tracked, compact caps — group-box titles, table headers, tab
labels. Shared with the overlays upstream rather than duplicated, on the
grounds that a private near-duplicate is how two surfaces drift apart."""

HINT_TEXT = TypographyRole(scale=0.90)  # ui/chrome.py:78
CHROME_TITLE = TypographyRole(scale=1.15, weight="bold")  # ui/chrome.py:82
CHROME_BODY = TypographyRole(scale=1.0)  # ui/chrome.py:86
"""Full size, unlike the overlays' 0.84 body — an overlay row is glanced at
over a raid, a merchant's window is read at a desk (ui/chrome.py:83-85)."""


@dataclass(frozen=True)
class Chrome:
    """What a :class:`Skin` and the :class:`Palette` together imply for a config
    surface — ui/chrome.py:115.

    Derived once by :func:`chrome_for` and handed to every rule builder, so the
    derivation is tested in one place rather than re-argued per stylesheet.
    """

    # -- hue, from the skin ----------------------------------------------
    accent: str  # selection, focus, group titles, hairlines
    accent_text: str  # text that sits ON the accent band
    band: tuple[str, ...]  # the selection band's fill
    rule: str  # hairline / border between sections

    # -- value, from the palette -----------------------------------------
    surface: str
    surface_alt: str
    text: str
    heading: str
    caption: str
    hint: str
    disabled: str
    field_bg: str
    field_text: str
    field_border: str

    # -- semantic, fixed --------------------------------------------------
    ok: str
    warn: str
    danger: str


def chrome_for(skin_name: str = DEFAULT_SKIN, colors: Palette = DARK) -> Chrome:
    """Resolve a skin name plus the palette into a config surface's tokens.

    Mirrors ui/chrome.py:148. Takes a *name* rather than a ``Skin`` because the
    host's ``Skin`` type is not importable from here — what actually crosses the
    boundary is ``settings.general.skin``, which is a string.
    """
    skin = SKINS.get(skin_name, SKINS[DEFAULT_SKIN])
    return Chrome(
        accent=skin.chrome_accent,
        # ui/chrome.py:158-161, and the one place the skin outranks the palette:
        # the band is dark under every skin, so text on it takes the skin's own
        # caps colour rather than the palette's, because the palette is not what
        # is behind it. Deriving this from ``colors.heading`` would be the
        # obvious simplification and would put white text on Velious's stone.
        accent_text=skin.title_color,
        band=skin.chrome_band,
        rule=skin.glass_border,
        surface=colors.surface,
        surface_alt=colors.surface_alt,
        text=colors.text,
        heading=colors.heading,
        caption=skin.chrome_accent,
        hint=colors.hint,
        disabled=colors.disabled,
        # These already mean "an input field in this theme" upstream, so
        # chrome_for aliases them rather than duplicating (ui/theme.py:41-42).
        field_bg=colors.map_input_bg,
        field_text=colors.map_input_text,
        field_border=colors.map_input_border,
        ok=GOOD,
        warn=ROLL,
        danger=BAD,
    )


# -- pure helpers ----------------------------------------------------------------


def gradient(stops: tuple[str, ...], horizontal: bool = False) -> str:
    """A Qt stylesheet fill for 1..n colour stops — ui/skins.py:465.

    One stop is a flat colour rather than a one-stop gradient, which Qt renders
    but no reader wants to look at in a stylesheet.
    """
    if not stops:
        return "transparent"
    if len(stops) == 1:
        return stops[0]
    axis = "x1: 0, y1: 0, x2: 1, y2: 0" if horizontal else "x1: 0, y1: 0, x2: 0, y2: 1"
    steps = ", ".join(
        f"stop: {index / (len(stops) - 1):.3f} {color}" for index, color in enumerate(stops)
    )
    return f"qlineargradient({axis}, {steps})"


def px(base_font_size: int, scale: float, minimum: int = 7) -> int:
    """A size multiplier resolved against the user's font size — ui/skins.py:492."""
    return max(minimum, round(base_font_size * scale))


def tracking(base_font_size: int, scale: float, em: float) -> str:
    """``letter-spacing`` in px for a size expressed in em — ui/skins.py:497.

    Qt stylesheets have no em unit, so the proportion is resolved here.
    """
    return f"{px(base_font_size, scale) * em:.2f}px"


def typography_style(base_font_size: int, role: TypographyRole, *, color: str | None = None) -> str:
    """QSS declarations for one typography role — ui/skins.py:502.

    A declaration body rather than a selector, so every rule below composes it
    with its own colours and borders instead of restating the family and scale.
    """
    style = (
        f'font-family: "{NOTO_SANS}";'
        f" font-size: {px(base_font_size, role.scale)}px;"
        f" font-weight: {role.weight};"
    )
    if role.tracking_em:
        style += f" letter-spacing: {tracking(base_font_size, role.scale, role.tracking_em)};"
    if color is not None:
        style += f" color: {color};"
    return style


def _focus(selectors: str) -> str:  # ui/chrome.py:584
    return ", ".join(f"{part.strip()}:focus" for part in selectors.split(","))


def _disabled(selectors: str) -> str:  # ui/chrome.py:588
    return ", ".join(f"{part.strip()}:disabled" for part in selectors.split(","))


def _selected(selectors: str) -> str:  # ui/chrome.py:592
    return ", ".join(f"{part.strip()}::item:selected" for part in selectors.split(","))


# -- stylesheet builders ---------------------------------------------------------
# One per concern, each taking an already-derived Chrome so the skin/palette
# split is resolved once rather than per sheet.


def ground_rules(ch: Chrome, font_size: int) -> str:
    """The window's own ground and default type — ui/chrome.py:240.

    MUST be emitted before every type-selector rule below. ``QWidget`` and
    ``QLineEdit`` match a QLineEdit with equal specificity, so within one sheet
    the later rule wins — put this last and every text field turns into the page
    background.
    """
    return (
        f"QWidget {{ background-color: {ch.surface}; color: {ch.text};"
        f" {typography_style(font_size, CHROME_BODY)} }}"
    )


def group_rules(ch: Chrome, font_size: int) -> str:
    """QSS for a titled section box — ui/chrome.py:254.

    ``margin-top`` makes room for the title, which is drawn in the margin; the
    paddings keep the box's content clear of the border on every side. Too
    little bottom padding and the last row in a group is clipped by the next
    group's edge.
    """
    return (
        f"QGroupBox {{ border: 1px solid {ch.rule}; border-radius: 3px;"
        f" margin-top: {SECTION_SPACING}px;"
        f" padding: {SECTION_SPACING}px {ROW_SPACING}px {ROW_SPACING}px {ROW_SPACING}px;"
        f" background: {ch.surface_alt}; }}"
        "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left;"
        f" left: {ROW_SPACING}px; padding: 0 4px;"
        f" {typography_style(font_size, SMALL_DISPLAY, color=ch.accent)} }}"
    )


def field_rules(ch: Chrome) -> str:
    """QSS for every text/number/choice input — ui/chrome.py:271.

    The focus ring is the skin's accent: the one place the window says which
    skin is active while you are actually using it.
    """
    inputs = "QLineEdit, QSpinBox, QComboBox"
    return (
        f"{inputs} {{ background-color: {ch.field_bg}; color: {ch.field_text};"
        f" border: 1px solid {ch.field_border}; border-radius: 3px; padding: 2px 4px;"
        f" selection-background-color: {ch.accent}; selection-color: {PILL_TEXT}; }}"
        f"{_focus(inputs)} {{ border: 1px solid {ch.accent}; }}"
        f"{_disabled(inputs)} {{ color: {ch.disabled}; }}"
        # The combo's popup is a separate view and inherits none of the above.
        f"QComboBox QAbstractItemView {{ background-color: {ch.field_bg};"
        f" color: {ch.field_text}; border: 1px solid {ch.field_border};"
        f" selection-background-color: {ch.accent}; selection-color: {PILL_TEXT}; }}"
    )


def button_rules(ch: Chrome, font_size: int) -> str:
    """QSS for push buttons — ui/chrome.py:292.

    Upstream also styles one emphasised button per row (``#ChromePrimary``).
    Not mirrored: nothing in this window is the one obvious action — Fill
    prices, Export and Remove are three different intentions, and promoting any
    of them would be the window guessing which one you came for.
    """
    return (
        f"QPushButton {{ background-color: {ch.surface_alt}; color: {ch.text};"
        f" border: 1px solid {ch.field_border}; border-radius: 3px;"
        f" padding: 3px 10px; {typography_style(font_size, CHROME_BODY)} }}"
        f"QPushButton:hover {{ border: 1px solid {ch.accent}; }}"
        f"QPushButton:pressed {{ background-color: {ch.field_bg}; }}"
        f"QPushButton:disabled {{ color: {ch.disabled};"
        f" border: 1px solid {ch.field_border}; }}"
    )


def view_rules(ch: Chrome, font_size: int) -> str:
    """QSS for the tables and lists — ui/chrome.py:307.

    ``alternate-background-color`` is what every ledger table in this window is
    banded with (see ``window._as_ledger``), so this rule is the one that
    decides whether those bands are visible at all.
    """
    views = "QListWidget, QTableWidget, QTableView, QListView"
    return (
        f"{views} {{ background-color: {ch.field_bg}; color: {ch.text};"
        f" border: 1px solid {ch.field_border}; border-radius: 3px;"
        f" alternate-background-color: {ch.surface_alt}; }}"
        f"{_selected(views)} {{ background-color: {gradient(ch.band)};"
        f" color: {ch.accent_text}; }}"
        f"QHeaderView::section {{ background-color: {ch.surface_alt}; color: {ch.accent};"
        f" border: none; border-bottom: 1px solid {ch.rule}; padding: 3px 6px;"
        f" {typography_style(font_size, SMALL_DISPLAY)} }}"
        "QHeaderView { background: transparent; }"
    )


def tab_rules(ch: Chrome, font_size: int) -> str:
    """QSS for the tab bar — ui/chrome.py:323.

    The six tabs are the window's whole navigation, so this is the rule group
    that does the most to make it look like part of the app.
    """
    return (
        f"QTabWidget::pane {{ border: 1px solid {ch.rule}; border-radius: 3px;"
        f" background: {ch.surface}; }}"
        f"QTabBar::tab {{ background: transparent; color: {ch.hint};"
        f" padding: 4px 12px; border-bottom: 2px solid transparent;"
        f" {typography_style(font_size, SMALL_DISPLAY)} }}"
        f"QTabBar::tab:selected {{ color: {ch.accent};"
        f" border-bottom: 2px solid {ch.accent}; }}"
        f"QTabBar::tab:hover {{ color: {ch.text}; }}"
    )


def misc_rules(ch: Chrome) -> str:
    """Check boxes and the Market tab's splitter — ui/chrome.py:360.

    The indicator is drawn here rather than left to Fusion. Fusion renders it
    from the palette, and this palette's Base is near-black, so an UNCHECKED box
    came out invisible against the page — "Show filtered" would read as a label
    with nothing to click. Checked is a filled accent square rather than a
    glyph: no image asset, and it stays legible at any font size.

    Upstream's radio and scroll-area rules are dropped; this window has neither.
    """
    box = "13px"
    return (
        f"QCheckBox {{ background: transparent; color: {ch.text}; spacing: 6px; }}"
        f"QCheckBox:disabled {{ color: {ch.disabled}; }}"
        f"QCheckBox::indicator {{ width: {box}; height: {box}; border-radius: 2px;"
        f" background-color: {ch.field_bg}; border: 1px solid {ch.field_border}; }}"
        f"QCheckBox::indicator:hover {{ border: 1px solid {ch.accent}; }}"
        f"QCheckBox::indicator:checked {{ background-color: {ch.accent};"
        f" border: 1px solid {ch.accent}; }}"
        f"QCheckBox::indicator:disabled {{ border: 1px solid {ch.disabled}; }}"
        f"QSplitter::handle {{ background: {ch.rule}; }}"
    )


def scrollbar_rules(ch: Chrome, width: int = 10) -> str:
    """QSS for the scrollbars — ui/chrome.py:389.

    Wider than the overlays' 6px: these are dragged with a mouse in a form, not
    glanced at over a raid.
    """
    return (
        f"QScrollBar:vertical {{ background: transparent; width: {width}px; margin: 0; }}"
        f"QScrollBar:horizontal {{ background: transparent; height: {width}px; margin: 0; }}"
        f"QScrollBar::handle {{ background: {ch.field_border}; border-radius: {width // 2}px;"
        " min-height: 24px; min-width: 24px; }"
        f"QScrollBar::handle:hover {{ background: {ch.accent}; }}"
        "QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }"
        "QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }"
    )


def window_style(skin_name: str = DEFAULT_SKIN, font_size: int = DEFAULT_FONT_SIZE) -> str:
    """The whole stylesheet for this window — ui/chrome.py:526.

    Scoped to the window, never to the application, and that is not tidiness:
    this sheet carries bare type selectors, and at app scope a ``QLabel`` rule
    here would land on the spell timers sitting on top of EverQuest. The host's
    own overlay sheet overrides only three properties on ``QLabel``, and Qt
    resolves stylesheet conflicts per property — anything not named there would
    leak straight through.

    Rule order is load-bearing; see :func:`ground_rules`.

    Composed from the rule groups this window has widgets for. Upstream also
    emits card, sidebar, slider and badge rules; nothing here is a pickable
    tile, a page list, a slider or a status pill.
    """
    ch = chrome_for(skin_name)
    return (
        ground_rules(ch, font_size)
        + f"#{HINT} {{ {typography_style(font_size, HINT_TEXT, color=ch.hint)}"
        " background: transparent; }"
        + f"#{CAPTION} {{ {typography_style(font_size, SMALL_DISPLAY, color=ch.caption)}"
        " background: transparent; }"
        + f"#{TITLE} {{ {typography_style(font_size, CHROME_TITLE, color=ch.heading)}"
        " background: transparent; }"
        + group_rules(ch, font_size)
        + field_rules(ch)
        + button_rules(ch, font_size)
        + view_rules(ch, font_size)
        + tab_rules(ch, font_size)
        + misc_rules(ch)
        + scrollbar_rules(ch)
    )
