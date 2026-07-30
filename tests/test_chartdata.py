"""Preparing price data for drawing, without drawing any of it.

The chart's job is to carry doubt as well as numbers, so most of what's tested
here is the doubt: that a two-sale average is visibly thinner than a
two-hundred-sale one, that a split market doesn't hide behind its median, and
that "no data" is a state the caller is told about rather than one it infers
from an empty axis.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from merchant_mode.auctions import Observation
from merchant_mode.chartdata import (
    THIN_CONFIDENCE,
    axis_top,
    build_chart,
    confidence,
    spread,
)
from merchant_mode.market import MarketRecord

T0 = datetime(2026, 7, 30, 21, 0, 0)


def _record(**counts: int) -> MarketRecord:
    """A record whose windows all average 5000, with the given sample counts."""
    return MarketRecord(
        name="Cloak of Flames",
        averages={key: 5000 for key in ("30d", "90d", "6mo", "all")},
        counts=dict(counts),
        fetched_at=T0,
    )


def _seen(price: int, *, minutes: int = 0, wanted: bool = False) -> Observation:
    return Observation(
        timestamp=T0 - timedelta(minutes=minutes),
        name="Cloak of Flames",
        price=price,
        sender="Someone",
        wanted=wanted,
    )


# --- confidence ------------------------------------------------------------ #
def test_no_sales_is_no_confidence() -> None:
    assert confidence(0) == 0.0


def test_a_thin_window_keeps_a_floor_rather_than_vanishing() -> None:
    """The number exists; hiding it would be its own kind of lying."""
    assert confidence(1) == THIN_CONFIDENCE


def test_confidence_rises_with_sample_count() -> None:
    assert confidence(2) < confidence(10) < confidence(50)


def test_confidence_is_capped_at_one() -> None:
    assert confidence(5000) == 1.0
    assert confidence(50) == 1.0


def test_confidence_is_log_scaled_not_linear() -> None:
    """Two-to-ten has to matter more than forty-to-fifty, or the drawing lies."""
    assert (confidence(10) - confidence(2)) > (confidence(50) - confidence(40))


# --- spread ---------------------------------------------------------------- #
def test_spread_reports_low_median_and_high() -> None:
    result = spread([5000, 1000, 3000])
    assert (result.low, result.median, result.high, result.count) == (1000, 3000, 5000, 3)


def test_spread_of_nothing_is_none() -> None:
    assert spread([]) is None
    assert spread([0, -5]) is None


def test_a_split_market_is_flagged_wide() -> None:
    """Median alone hides two very different asks for the same item."""
    assert spread([1000, 1200, 9000]).wide


def test_a_tight_market_is_not_flagged_wide() -> None:
    assert not spread([5000, 5200, 5400]).wide


def test_two_observations_are_never_called_a_split_market() -> None:
    """A claim the data can't carry. The min/max still show."""
    result = spread([1000, 9000])
    assert not result.wide
    assert (result.low, result.high) == (1000, 9000)


# --- axis ------------------------------------------------------------------ #
def test_axis_top_rounds_to_something_worth_labelling() -> None:
    assert axis_top(4300) == 5000
    assert axis_top(5000) == 5000
    assert axis_top(12847) == 20000
    assert axis_top(1) == 1
    assert axis_top(0) == 0


# --- the assembled chart --------------------------------------------------- #
def test_an_item_with_nothing_known_is_explicitly_empty() -> None:
    """Handled first, not last: most items look exactly like this."""
    chart = build_chart("Manastone")
    assert chart.empty
    assert not chart.has_windows
    assert not chart.has_observations
    assert chart.top == 0
    assert chart.span is None


def test_every_window_gets_a_bar_even_when_pigparse_knows_nothing() -> None:
    """Four bars always, so a missing window reads as absent, not as narrow."""
    chart = build_chart("Manastone")
    assert [bar.key for bar in chart.windows] == ["30d", "90d", "6mo", "all"]
    assert not any(bar.known for bar in chart.windows)


def test_sample_counts_reach_the_bars_with_their_confidence() -> None:
    chart = build_chart("Cloak of Flames", record=_record(**{"30d": 2, "all": 200}))
    bars = {bar.key: bar for bar in chart.windows}
    assert bars["30d"].count_text == "2 sales"
    assert bars["all"].count_text == "200 sales"
    assert bars["30d"].confidence < bars["all"].confidence
    assert bars["90d"].count_text == "no sales"


def test_one_sale_reads_as_singular() -> None:
    chart = build_chart("Cloak of Flames", record=_record(**{"30d": 1}))
    assert chart.windows[0].count_text == "1 sale"
    assert not chart.windows[0].well_sampled


def test_the_baseline_is_the_same_number_the_price_box_would_use() -> None:
    """A chart that disagreed with Fill prices would be worse than no chart."""
    record = _record(**{"30d": 1, "90d": 8})
    chart = build_chart("Cloak of Flames", record=record)
    assert chart.baseline == record.headline
    assert chart.baseline_label == "PigParse 90 days"


def test_observations_are_ordered_oldest_first_for_plotting() -> None:
    """The tables read newest-first; a time axis cannot."""
    chart = build_chart(
        "Cloak of Flames",
        observations=[_seen(5000, minutes=1), _seen(4000, minutes=60), _seen(6000, minutes=30)],
    )
    assert [item.price for item in chart.observations] == [4000, 6000, 5000]
    assert chart.span == (T0 - timedelta(minutes=60), T0 - timedelta(minutes=1))


def test_the_two_sides_of_the_trade_get_their_own_spread() -> None:
    """A WTB ask and a WTS ask are not samples of the same number."""
    chart = build_chart(
        "Cloak of Flames",
        observations=[
            _seen(5000),
            _seen(5400, minutes=10),
            _seen(3000, minutes=20, wanted=True),
        ],
    )
    assert (chart.sell.low, chart.sell.high) == (5000, 5400)
    assert chart.buy.median == 3000
    assert chart.sell.count == 2


def test_the_axis_clears_the_tallest_thing_on_the_chart() -> None:
    chart = build_chart(
        "Cloak of Flames",
        record=_record(**{"all": 40}),
        observations=[_seen(9100)],
    )
    assert chart.ceiling == 9100
    assert chart.top == 10000


def test_observations_alone_are_enough_to_draw() -> None:
    """PigParse silence is common; it must not blank the live picture."""
    chart = build_chart("Cloak of Flames", observations=[_seen(5000)])
    assert not chart.empty
    assert chart.has_observations
    assert chart.baseline == 0
    assert chart.baseline_label == ""


def test_pigparse_alone_is_enough_to_draw() -> None:
    chart = build_chart("Cloak of Flames", record=_record(**{"6mo": 12}))
    assert not chart.empty
    assert chart.has_windows
    assert not chart.has_observations


def test_a_single_sighting_collapses_the_span_without_dividing_by_zero() -> None:
    """The caller has to handle an instant; it must not be handed a surprise."""
    chart = build_chart("Cloak of Flames", observations=[_seen(5000), _seen(6000)])
    start, end = chart.span
    assert start == end
