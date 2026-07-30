"""merchant_mode.macros — listings into packed, throttled socials."""

from __future__ import annotations

from merchant_mode.itemlink import DELIM, raw_len
from merchant_mode.macros import Listing, build_wts_socials
from merchant_mode.nicknames import NicknameTable
from merchant_mode.packing import LINE_LIMIT

CLOAK_OF_FLAMES = 11621


def listings(count: int, price: str = "5k") -> list[Listing]:
    return [Listing(CLOAK_OF_FLAMES, f"Item Number {n}", price) for n in range(count)]


def content_lines(social: dict) -> list[str]:
    return [line for line in social["lines"] if not line.startswith("/pause")]


def test_builds_a_social_from_listings() -> None:
    result = build_wts_socials([Listing(CLOAK_OF_FLAMES, "Cloak of Flames", "5k")])
    assert len(result.socials) == 1
    assert result.ok
    line = content_lines(result.socials[0])[0]
    assert line.startswith("/auc WTS ")
    assert line.endswith(" 5k")
    assert DELIM in line


def test_every_generated_line_respects_the_byte_limit() -> None:
    result = build_wts_socials(listings(40), max_socials=10)
    for social in result.socials:
        for line in social["lines"]:
            assert raw_len(line) <= LINE_LIMIT


def test_throttled_socials_carry_three_content_lines() -> None:
    result = build_wts_socials(listings(40), max_socials=10)
    assert all(len(content_lines(social)) <= 3 for social in result.socials)
    assert all(len(social["lines"]) <= 5 for social in result.socials)


def test_disabling_the_pause_restores_five_content_lines() -> None:
    throttled = build_wts_socials(listings(40), max_socials=1)
    open_throttle = build_wts_socials(listings(40), max_socials=1, pause_tenths=0)
    assert len(content_lines(throttled.socials[0])) == 3
    assert len(content_lines(open_throttle.socials[0])) == 5


def test_overflow_spills_into_further_socials_on_consecutive_buttons() -> None:
    result = build_wts_socials(listings(30), max_socials=4)
    assert len(result.socials) > 1
    assert [social["button"] for social in result.socials] == list(
        range(1, len(result.socials) + 1)
    )
    assert all(social["page"] == 1 for social in result.socials)


def test_items_beyond_the_social_budget_are_reported_not_dropped() -> None:
    result = build_wts_socials(listings(200), max_socials=1)
    assert len(result.socials) == 1
    assert result.unplaced
    assert not result.ok


def test_nicknames_shorten_the_display_text_and_fit_more_per_line() -> None:
    table = NicknameTable({"Cloak of Flames": "CoF"})
    named = [Listing(CLOAK_OF_FLAMES, "Cloak of Flames", "5k") for _ in range(9)]
    full = build_wts_socials(named, nicknames=table, abbreviate=False, max_socials=9)
    short = build_wts_socials(named, nicknames=table, abbreviate=True, max_socials=9)
    assert "CoF" in content_lines(short.socials[0])[0]
    assert "Cloak of Flames" in content_lines(full.socials[0])[0]
    # The gain is density, not fewer lines: both saturate the same line budget,
    # but the abbreviated line carries more items.
    links_per_line = content_lines(short.socials[0])[0].count(DELIM) // 2
    assert links_per_line > content_lines(full.socials[0])[0].count(DELIM) // 2


def test_a_listing_too_long_for_any_line_is_reported_not_truncated() -> None:
    huge = Listing(CLOAK_OF_FLAMES, "X" * 260, "5k")
    result = build_wts_socials([huge, Listing(CLOAK_OF_FLAMES, "Cloak of Flames", "5k")])
    assert [entry.label for entry in result.oversized] == ["X" * 260]
    assert result.socials  # the good one still packs
    for social in result.socials:
        for line in social["lines"]:
            assert raw_len(line) <= LINE_LIMIT


def test_a_listing_without_a_price_carries_no_suffix() -> None:
    result = build_wts_socials([Listing(CLOAK_OF_FLAMES, "Cloak of Flames")])
    assert content_lines(result.socials[0])[0].endswith(DELIM)


def test_no_listings_produces_no_socials() -> None:
    result = build_wts_socials([])
    assert result.socials == []
    assert result.ok


def test_a_custom_prefix_is_honoured() -> None:
    result = build_wts_socials(
        [Listing(CLOAK_OF_FLAMES, "Cloak of Flames", "5k")], prefix="/ooc WTS "
    )
    assert content_lines(result.socials[0])[0].startswith("/ooc WTS ")
