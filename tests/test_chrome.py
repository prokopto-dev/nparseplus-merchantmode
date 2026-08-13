"""The mirrored design tokens, pinned.

``merchant_mode/chrome.py`` is a hand copy of values that live in the host's
``nparseplus/ui/{theme,skins,chrome}.py``, which this plugin may not import. A
copy drifts silently: the host repaints itself in some future release, this
window keeps wearing last year's gold, and nothing anywhere fails. So the
values are asserted literally here — not recomputed from the module, which
would assert nothing. When nParse+ changes a token, this file is the diff that
says so, and updating it is the deliberate act of accepting the change.

The upstream values are readable without the app installed::

    git -C <nparseplus checkout> show origin/master:src/nparseplus/ui/theme.py
    git -C <nparseplus checkout> show origin/master:src/nparseplus/ui/skins.py

There is deliberately no test that imports ``nparseplus.ui.chrome`` and
compares: it would skip in CI (where the host is absent), skip in the
validator, and therefore fail to run in exactly the two places that gate a
release. A literal is available everywhere.
"""

from __future__ import annotations

import pytest

from merchant_mode import chrome


def test_the_dark_palette_matches_the_host() -> None:
    """nparseplus/ui/theme.py:49-66, the only palette there is since v2.0.0."""
    assert chrome.DARK.text == "#dddddd"
    assert chrome.DARK.heading == "#ffffff"
    assert chrome.DARK.warning_text == "#ff5044"
    assert chrome.DARK.map_input_bg == "#050505"
    assert chrome.DARK.map_input_text == "white"
    assert chrome.DARK.map_input_border == "#333"
    assert chrome.DARK.surface == "#16171b"
    assert chrome.DARK.surface_alt == "#1d1f24"
    assert chrome.DARK.hint == "#8b8f9a"
    assert chrome.DARK.disabled == "#5a5e69"


def test_every_shipped_skin_is_mirrored() -> None:
    """config/settings.py:94 types ``general.skin`` as exactly these three, so
    a fourth key here (or a missing one) means the host has moved."""
    assert set(chrome.SKINS) == {"duxa", "velious", "ledger"}
    assert chrome.DEFAULT_SKIN == "duxa"


@pytest.mark.parametrize(
    ("name", "accent", "title_color", "band", "glass_border"),
    [
        # nparseplus/ui/skins.py:224, :217, :225, :213
        (
            "duxa",
            "#c8a951",
            "#d4b675",
            ("rgba(107, 90, 58, 77)", "rgba(107, 90, 58, 15)"),
            "#2b2519",
        ),
        # nparseplus/ui/skins.py:282, :275, :283, :271
        ("velious", "#e2c882", "#f0dcae", ("#5c4d31", "#332a1c"), "#6b5a3a"),
        # nparseplus/ui/skins.py:350, :342, :351, :338
        ("ledger", "#8a7549", "#8a7549", ("rgba(107, 90, 58, 56)",), "#2b2519"),
    ],
)
def test_skin_chrome_fields_match_the_host(
    name: str, accent: str, title_color: str, band: tuple[str, ...], glass_border: str
) -> None:
    skin = chrome.SKINS[name]
    assert skin.chrome_accent == accent
    assert skin.title_color == title_color
    assert skin.chrome_band == band
    assert skin.glass_border == glass_border


def test_the_layout_gutters_match_the_host() -> None:
    """nparseplus/ui/chrome.py:92-94 — px, not multipliers, on purpose."""
    assert chrome.PAGE_MARGINS == (10, 10, 10, 10)
    assert chrome.ROW_SPACING == 6
    assert chrome.SECTION_SPACING == 12


def test_the_type_roles_match_the_host() -> None:
    """Multipliers of general.font_size, never px — nparseplus/ui/chrome.py:78-86
    and skins.py:70."""
    assert (chrome.HINT_TEXT.scale, chrome.HINT_TEXT.weight) == (0.90, "normal")
    assert (chrome.CHROME_TITLE.scale, chrome.CHROME_TITLE.weight) == (1.15, "bold")
    assert (chrome.CHROME_BODY.scale, chrome.CHROME_BODY.weight) == (1.0, "normal")
    small = chrome.SMALL_DISPLAY
    assert (small.scale, small.weight, small.tracking_em) == (0.78, "bold", 0.18)
    assert chrome.NOTO_SANS == "Noto Sans"


def test_the_semantic_accents_match_the_host() -> None:
    """nparseplus/ui/chrome.py:103-112."""
    assert chrome.GOOD == "#2f9e6e"
    assert chrome.BAD == "#c0392b"
    assert chrome.ROLL == "#d99b2b"
    assert chrome.PILL_TEXT == "#ffffff"


def test_the_object_names_are_the_hosts_own() -> None:
    """nparseplus/ui/chrome.py:52-54. Same strings deliberately: a label wearing
    ``ChromeHint`` is already named right if the host ever dresses plugin
    windows itself."""
    assert (chrome.HINT, chrome.CAPTION, chrome.TITLE) == (
        "ChromeHint",
        "ChromeCaption",
        "ChromeTitle",
    )


# --- the derivation ---------------------------------------------------------


def test_the_skin_outranks_the_palette_for_text_on_the_band() -> None:
    """The one inversion in ``chrome_for`` (nparseplus/ui/chrome.py:158-161).

    Everywhere else the palette owns value and the skin owns hue. Here the band
    is dark under every skin, so text on it takes the skin's own caps colour —
    deriving it from ``heading`` would be the obvious simplification and would
    put white text on Velious's stone.
    """
    for name, skin in chrome.SKINS.items():
        assert chrome.chrome_for(name).accent_text == skin.title_color, name
        assert chrome.chrome_for(name).accent_text != chrome.DARK.heading or name == "duxa"


def test_the_palette_owns_value_under_every_skin() -> None:
    """The readability floor no skin may move (nparseplus/ui/chrome.py:13)."""
    for name in chrome.SKINS:
        ch = chrome.chrome_for(name)
        assert ch.surface == chrome.DARK.surface
        assert ch.text == chrome.DARK.text
        assert ch.field_bg == chrome.DARK.map_input_bg


def test_the_skin_owns_hue() -> None:
    accents = {chrome.chrome_for(name).accent for name in chrome.SKINS}
    assert len(accents) == 3, "three skins that share an accent is a copy error"


def test_field_tokens_alias_the_input_palette() -> None:
    """nparseplus/ui/theme.py:41-42 — ``map_input_*`` already means "a field"."""
    ch = chrome.chrome_for("duxa")
    assert (ch.field_bg, ch.field_text, ch.field_border) == (
        chrome.DARK.map_input_bg,
        chrome.DARK.map_input_text,
        chrome.DARK.map_input_border,
    )


def test_an_unknown_skin_falls_back_rather_than_raising() -> None:
    """A skin name this copy has never heard of is what a *new* host skin looks
    like from in here, and it arrives while a window factory is running."""
    assert chrome.chrome_for("moonstone") == chrome.chrome_for(chrome.DEFAULT_SKIN)
    assert chrome.chrome_for("") == chrome.chrome_for(chrome.DEFAULT_SKIN)


# --- the pure helpers -------------------------------------------------------


def test_one_stop_is_a_flat_colour_and_two_are_a_gradient() -> None:
    assert chrome.gradient(("#5c4d31",)) == "#5c4d31"
    assert chrome.gradient(()) == "transparent"
    assert chrome.gradient(("#5c4d31", "#332a1c")).startswith("qlineargradient(x1: 0, y1: 0")


def test_type_sizes_are_multiples_of_the_users_font_size() -> None:
    """The host's own rule (nparseplus/ui/skins.py:29-31): never px, so a user
    who sets 16pt gets a bigger window rather than a window with big tables and
    small hints."""
    assert chrome.px(12, 1.0) == 12
    assert chrome.px(16, 1.0) == 16
    assert chrome.px(16, 0.90) == 14
    # A floor, so a tiny scale at a small font size is still legible.
    assert chrome.px(6, 0.5) == 7


def test_tracking_is_resolved_to_px_because_qss_has_no_em() -> None:
    assert chrome.tracking(12, 0.78, 0.18) == "1.62px"


def test_typography_style_names_the_bundled_family() -> None:
    style = chrome.typography_style(12, chrome.CHROME_BODY)
    assert 'font-family: "Noto Sans";' in style
    assert "font-size: 12px;" in style
    assert "letter-spacing" not in style  # only emitted when the role tracks


# --- the composed sheet -----------------------------------------------------


def test_the_sheet_grounds_itself_before_it_styles_any_field() -> None:
    """Rule order is load-bearing (nparseplus/ui/chrome.py:240-247): QWidget and
    QLineEdit match a line edit with equal specificity, so the later rule wins.
    Ground last and every text field turns into the page background."""
    sheet = chrome.window_style("duxa", 12)
    assert sheet.index("QWidget {") < sheet.index("QLineEdit")


def test_the_sheet_covers_the_widgets_this_window_actually_has() -> None:
    sheet = chrome.window_style("velious", 12)
    for selector in (
        "QTabBar::tab",
        "QGroupBox",
        "QPushButton",
        "QComboBox",
        "QTableWidget",
        "QHeaderView::section",
        "QCheckBox::indicator",
        "QSplitter::handle",
        "QScrollBar::handle",
        f"#{chrome.HINT}",
    ):
        assert selector in sheet, selector


def test_the_sheet_omits_the_rule_groups_with_no_widget_behind_them() -> None:
    """A token with no widget behind it is one nobody notices has gone stale."""
    sheet = chrome.window_style("duxa", 12)
    for absent in ("QSlider", "QRadioButton", "ChromeBadge", "ChromeCard", "ChromeSidebar"):
        assert absent not in sheet, absent


def test_the_sheet_is_window_scoped_and_never_reaches_a_menu_or_tooltip() -> None:
    """Menus and tooltips are top-level windows of their own, so a window sheet
    could not reach them anyway — and the host already skins both at app scope
    (nparseplus/ui/chrome.py:410-416). Styling them here would be a second
    opinion that only shows up when the two disagree."""
    sheet = chrome.window_style("duxa", 12)
    assert "QMenu" not in sheet
    assert "QToolTip" not in sheet


def test_the_accent_reaches_the_sheet_under_every_skin() -> None:
    """The check that the skin is doing anything at all: three skins must
    produce three different sheets."""
    sheets = {name: chrome.window_style(name, 12) for name in chrome.SKINS}
    assert len(set(sheets.values())) == 3
    for name, sheet in sheets.items():
        assert chrome.SKINS[name].chrome_accent in sheet


def test_the_sheet_scales_with_the_font_size() -> None:
    assert "font-size: 16px;" in chrome.window_style("duxa", 16)
    assert "font-size: 16px;" not in chrome.window_style("duxa", 12)
