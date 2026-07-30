"""merchant_mode.pricing — suggesting a price, with its provenance."""

from __future__ import annotations

from datetime import datetime

from merchant_mode.auctions import PriceHistory
from merchant_mode.pricing import PriceSource, Side, Suggestion, format_price, suggest

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
