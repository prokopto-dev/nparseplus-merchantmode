"""merchant_mode.pricing — suggesting a price, with its provenance."""

from __future__ import annotations

from datetime import datetime

from merchant_mode.auctions import PriceHistory
from merchant_mode.pricing import (
    PriceSource,
    Rounding,
    Side,
    Suggestion,
    adjust,
    describe_adjustment,
    format_price,
    suggest,
)

T0 = datetime(2026, 7, 30, 20, 0, 0)


def history_with(*lines: str) -> PriceHistory:
    history = PriceHistory()
    for line in lines:
        history.record(line, timestamp=T0)
    return history


# --- formatting ------------------------------------------------------------


def test_formats_prices_the_way_sellers_write_them() -> None:
    assert format_price(5000) == "5k"
    assert format_price(1500) == "1.5k"
    assert format_price(42000) == "42k"
    assert format_price(850) == "850pp"


def test_awkward_numbers_stay_in_plat_rather_than_faking_precision() -> None:
    assert format_price(1234) == "1234pp"


def test_unknown_prices_format_as_empty() -> None:
    assert format_price(None) == ""
    assert format_price(0) == ""


# --- source preference -----------------------------------------------------


def test_live_auctions_win_over_the_pigparse_average() -> None:
    history = history_with("WTS Manastone 40k")
    proposal = suggest("Manastone", history=history, averages={"manastone": 99000})
    assert proposal.price == 40000
    assert proposal.source is PriceSource.OBSERVED


def test_pigparse_fills_the_silence_when_nothing_has_been_seen() -> None:
    proposal = suggest("Manastone", history=PriceHistory(), averages={"manastone": 99000})
    assert proposal.price == 99000
    assert proposal.source is PriceSource.PIGPARSE


def test_the_other_side_of_the_trade_is_better_than_nothing() -> None:
    # Only WTB observations exist, but we're pricing a sale.
    history = history_with("WTB Guise of the Deceiver 90k")
    proposal = suggest("Guise of the Deceiver", history=history, side=Side.SELL)
    assert proposal.price == 90000
    assert proposal.source is PriceSource.OBSERVED_OPPOSITE


def test_matching_side_beats_the_opposite_side() -> None:
    history = history_with("WTS Manastone 40k", "WTB Manastone 10k")
    assert suggest("Manastone", history=history, side=Side.SELL).price == 40000
    assert suggest("Manastone", history=history, side=Side.BUY).price == 10000


def test_nothing_known_yields_an_empty_suggestion() -> None:
    proposal = suggest("Never Auctioned", history=PriceHistory(), averages={})
    assert proposal == Suggestion()
    assert not proposal.known
    assert proposal.text == ""
    assert proposal.source is PriceSource.NONE


# --- robustness ------------------------------------------------------------


def test_uses_the_median_so_one_optimist_cannot_move_the_price() -> None:
    # Four sane asks and one fantasy. A mean would read ~44k; the median holds.
    history = history_with(
        "WTS Fungi Tunic 20k",
        "WTS Fungi Tunic 21k",
        "WTS Fungi Tunic 22k",
        "WTS Fungi Tunic 23k",
        "WTS Fungi Tunic 500k",
    )
    proposal = suggest("Fungi Tunic", history=history)
    assert proposal.price == 22000
    assert proposal.samples == 5


def test_min_samples_can_hold_out_for_more_than_one_data_point() -> None:
    history = history_with("WTS Manastone 40k")
    proposal = suggest("Manastone", history=history, averages={"manastone": 99000}, min_samples=3)
    assert proposal.source is PriceSource.PIGPARSE


def test_lookup_is_case_insensitive() -> None:
    history = history_with("WTS Manastone 40k")
    assert suggest("  MANASTONE ", history=history).price == 40000
    assert suggest("MANASTONE", history=PriceHistory(), averages={"manastone": 5}).price == 5


def test_every_source_has_a_human_label() -> None:
    # The UI shows these; an unattributed number is one taken on faith.
    assert all(source.label for source in PriceSource)


# --- markup and rounding ---------------------------------------------------


def test_the_markup_goes_on_before_the_rounding() -> None:
    """The order is the feature. Rounding first would defeat both settings."""
    assert adjust(1000, markup_percent=15, rounding=Rounding.HUNDRED) == 1200
    # What rounding-first would have produced, spelled out so a refactor that
    # swaps the two steps fails here rather than in someone's auction.
    assert adjust(1000, markup_percent=15, rounding=Rounding.NONE) == 1150
    assert adjust(1000, markup_percent=0, rounding=Rounding.HUNDRED) == 1000


def test_each_setting_works_without_the_other() -> None:
    assert adjust(1000, markup_percent=20) == 1200
    assert adjust(1040, rounding=Rounding.HUNDRED) == 1000
    assert adjust(1000) == 1000


def test_rounding_goes_to_the_nearest_step_in_both_directions() -> None:
    assert adjust(1049, rounding=Rounding.HUNDRED) == 1000
    assert adjust(1050, rounding=Rounding.HUNDRED) == 1100
    assert adjust(1250, rounding=Rounding.FIVE_HUNDRED) == 1500
    assert adjust(1499, rounding=Rounding.THOUSAND) == 1000


def test_rounding_never_prices_a_real_item_at_nothing() -> None:
    """A 40pp item rounded to the nearest 100 is 100pp, not free."""
    assert adjust(40, rounding=Rounding.HUNDRED) == 100
    assert adjust(4, rounding=Rounding.THOUSAND) == 1000


def test_an_unknown_price_stays_unknown_however_it_is_adjusted() -> None:
    assert adjust(0, markup_percent=50, rounding=Rounding.HUNDRED) == 0


def test_the_markup_rounds_half_up_rather_than_truncating() -> None:
    # Platinum is an int everywhere, so +10% of 105 is 115.5 -> 116.
    assert adjust(105, markup_percent=10) == 116


def test_rounding_lands_on_prices_people_actually_write() -> None:
    """Why the scales are what they are — format_price is the reason."""
    assert format_price(adjust(1234, rounding=Rounding.HUNDRED)) == "1.2k"
    assert format_price(adjust(1234, rounding=Rounding.THOUSAND)) == "1k"
    assert format_price(adjust(1234)) == "1234pp"  # unrounded, and it shows


def test_a_marked_up_suggestion_says_so() -> None:
    proposal = suggest(
        "Manastone",
        history=PriceHistory(),
        averages={"manastone": 100000},
        markup_percent=15,
        rounding=Rounding.THOUSAND,
    )
    assert proposal.price == 115000
    assert proposal.adjusted == "+15%, nearest 1000"
    assert proposal.provenance == "PigParse 6mo +15%, nearest 1000"


def test_an_untouched_suggestion_claims_no_adjustment() -> None:
    proposal = suggest("Manastone", history=history_with("WTS Manastone 40k"))
    assert proposal.adjusted == ""
    assert proposal.provenance == PriceSource.OBSERVED.label


def test_a_suggestion_with_no_price_claims_no_adjustment() -> None:
    """Nothing was marked up, so saying "+15%" would be a claim about air."""
    proposal = suggest("Never Auctioned", history=PriceHistory(), markup_percent=15)
    assert proposal.adjusted == ""
    assert not proposal.known


def test_the_markup_applies_to_every_source_a_sale_can_come_from() -> None:
    live = suggest("Manastone", history=history_with("WTS Manastone 40k"), markup_percent=10)
    assert live.price == 44000
    opposite = suggest("Guise", history=history_with("WTB Guise 90k"), markup_percent=10)
    assert opposite.source is PriceSource.OBSERVED_OPPOSITE
    assert opposite.price == 99000


def test_buy_quotes_are_never_marked_up() -> None:
    """Marking up what you offer to pay is an offer to overpay."""
    history = history_with("WTB Manastone 40k")
    proposal = suggest(
        "Manastone",
        history=history,
        side=Side.BUY,
        markup_percent=25,
        rounding=Rounding.THOUSAND,
    )
    assert proposal.price == 40000
    assert proposal.adjusted == ""


def test_a_buy_quote_off_the_selling_side_is_not_marked_up_either() -> None:
    # The opposite-side branch is a second return, and it needed the same gate.
    proposal = suggest(
        "Manastone", history=history_with("WTS Manastone 40k"), side=Side.BUY, markup_percent=25
    )
    assert proposal.source is PriceSource.OBSERVED_OPPOSITE
    assert proposal.price == 40000


def test_a_pigparse_average_is_not_marked_up_for_a_buy_quote() -> None:
    proposal = suggest(
        "Manastone",
        history=PriceHistory(),
        averages={"manastone": 99000},
        side=Side.BUY,
        markup_percent=25,
    )
    assert proposal.price == 99000


def test_every_rounding_scale_has_a_human_label_and_a_step() -> None:
    assert all(scale.label for scale in Rounding)
    assert Rounding.NONE.step == 0
    steps = [scale.step for scale in Rounding if scale is not Rounding.NONE]
    assert steps == [10, 50, 100, 500, 1000]


def test_an_adjustment_that_does_nothing_says_nothing() -> None:
    assert describe_adjustment() == ""
    assert describe_adjustment(markup_percent=15) == "+15%"
    assert describe_adjustment(rounding=Rounding.FIFTY) == "nearest 50"
