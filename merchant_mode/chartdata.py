"""Turning price records into something drawable (Qt-free, stdlib only).

PigParse hands over four averaging windows per item, each with its own sample
count, and the plugin used to collapse all of it into
:attr:`~merchant_mode.market.MarketRecord.headline` — one number. That is the
right amount of information for filling a price box and the wrong amount for
deciding a price. Thirty days well above all-time means the item is climbing;
forty all-time sales and two in ninety days means the market for it is gone. A
single number hides both.

So this module prepares the *shape*: four bars with a confidence apiece, the
live ``/auc`` sightings as a series, and a min/median/max spread. It does no
drawing — that lives in :mod:`merchant_mode.window`, which is the only place Qt
is allowed — and it hardcodes no colours, no pixels and no fonts. What it does
carry is the skepticism: :data:`~merchant_mode.market.MIN_SAMPLES` exists
because below it a "window average" is one person's asking price wearing a
suit, and :func:`confidence` is how that judgement reaches the paint code
instead of being re-litigated there.

Empty and thin are the normal cases, not the edge ones. Most items have no
observations and one usable window, so :attr:`PriceChart.empty` is a first-class
answer rather than something the caller infers from three lengths.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime

from .auctions import Observation
from .market import MIN_SAMPLES, WINDOWS, MarketRecord

__all__ = [
    "PriceChart",
    "Spread",
    "WindowBar",
    "axis_top",
    "build_chart",
    "confidence",
    "spread",
]

THIN_CONFIDENCE = 0.30
"""Confidence given to a window with a real average but too few sales.

Not zero: the number exists and hiding it would be its own kind of lying. Low
enough that it reads as a whisper next to a well-sampled bar.
"""

FULL_CONFIDENCE_AT = 50
"""Sales at which a window average is as trustworthy as this plugin gets.

There is nothing sacred about fifty. It's the point past which more sales stop
changing how much you'd hedge, and a scale needs a top.
"""

WIDE_SPREAD_RATIO = 2.0
"""Top ask over bottom ask that counts as a genuinely split market.

Median alone hides this, and two very different asks for the same item is a
common P99 pattern rather than a curiosity — one seller who knows what they
have and one who doesn't.
"""

MIN_SPREAD_SAMPLES = 3
"""Below this, "spread" is just the two prices you saw. Reported anyway; only
:attr:`Spread.wide` stays quiet, because calling a market split on two points
is a claim the data can't carry."""


def confidence(count: int, *, min_samples: int = MIN_SAMPLES) -> float:
    """How much a window average deserves to be believed, from 0.0 to 1.0.

    Log-scaled on purpose: the step from two sales to ten changes the answer far
    more than the step from forty to fifty, and a linear scale would draw those
    the other way round.
    """
    if count <= 0:
        return 0.0
    if count < min_samples:
        return THIN_CONFIDENCE
    ratio = math.log10(max(count, 1)) / math.log10(FULL_CONFIDENCE_AT)
    return round(min(1.0, THIN_CONFIDENCE + (1.0 - THIN_CONFIDENCE) * ratio), 3)


@dataclass(frozen=True)
class WindowBar:
    """One PigParse averaging window, ready to draw."""

    key: str
    label: str
    average: int
    count: int
    confidence: float

    @property
    def known(self) -> bool:
        """Whether there's a number here at all."""
        return self.average > 0

    @property
    def well_sampled(self) -> bool:
        return self.count >= MIN_SAMPLES

    @property
    def count_text(self) -> str:
        """``36 sales``, or ``1 sale``, or ``no sales``."""
        if self.count <= 0:
            return "no sales"
        return f"{self.count:,} sale{'' if self.count == 1 else 's'}"


@dataclass(frozen=True)
class Spread:
    """What a set of observed prices actually looked like."""

    low: int
    median: int
    high: int
    count: int

    @property
    def wide(self) -> bool:
        """Whether the top ask is at least double the bottom.

        The signal that a median is hiding two different markets rather than
        describing one.
        """
        return (
            self.count >= MIN_SPREAD_SAMPLES
            and self.low > 0
            and self.high >= self.low * WIDE_SPREAD_RATIO
        )


def spread(prices: list[int]) -> Spread | None:
    """Min, median and max of ``prices``, or ``None`` when there are none."""
    usable = sorted(price for price in prices if price > 0)
    if not usable:
        return None
    return Spread(
        low=usable[0],
        median=round(statistics.median(usable)),
        high=usable[-1],
        count=len(usable),
    )


def axis_top(value: int) -> int:
    """A round number at or just above ``value``, for the top of an axis.

    Steps 1 / 2 / 5 per decade, which is what makes a gridline land on ``10k``
    rather than ``12,847pp``.
    """
    if value <= 0:
        return 0
    decade = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        top = step * decade
        if value <= top:
            return int(top)
    return int(10 * decade)


@dataclass(frozen=True)
class PriceChart:
    """Everything a price panel needs, with nothing Qt-shaped in it."""

    name: str
    windows: tuple[WindowBar, ...] = ()
    observations: tuple[Observation, ...] = ()
    """Oldest first — plotting order, not the newest-first order the tables use."""
    baseline: int = 0
    """The PigParse figure the live prices are being judged against, or 0."""
    baseline_label: str = ""
    sell: Spread | None = None
    buy: Spread | None = None

    @property
    def has_windows(self) -> bool:
        return any(bar.known for bar in self.windows)

    @property
    def has_observations(self) -> bool:
        return bool(self.observations)

    @property
    def empty(self) -> bool:
        """Nothing worth drawing. The caller should say so, not draw an axis."""
        return not self.has_windows and not self.has_observations

    @property
    def ceiling(self) -> int:
        """Highest price anywhere in the chart — what the axis has to reach."""
        return max(
            [bar.average for bar in self.windows]
            + [item.price for item in self.observations]
            + [self.baseline, 0]
        )

    @property
    def top(self) -> int:
        """:attr:`ceiling` rounded up to something worth labelling."""
        return axis_top(self.ceiling)

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        """First and last observation time, or ``None`` when there are none.

        Collapses to a single instant when every sighting shares a timestamp,
        which the caller has to handle rather than divide by.
        """
        if not self.observations:
            return None
        return self.observations[0].timestamp, self.observations[-1].timestamp


def build_chart(
    name: str,
    *,
    record: MarketRecord | None = None,
    observations: list[Observation] | None = None,
    min_samples: int = MIN_SAMPLES,
) -> PriceChart:
    """Assemble the chart for one item.

    ``record`` is PigParse's stats and ``observations`` the local ``/auc``
    sightings; both are optional and either being absent is ordinary. The
    baseline comes from :meth:`MarketRecord.best`, so the reference line the
    live prices are judged against is the same number the Fill button would
    have used — a chart that disagreed with the price box would be worse than
    no chart.
    """
    bars = tuple(
        WindowBar(
            key=window.key,
            label=window.label,
            average=record.average(window.key) if record else 0,
            count=record.count(window.key) if record else 0,
            confidence=confidence(
                record.count(window.key) if record else 0, min_samples=min_samples
            ),
        )
        for window in WINDOWS
    )

    seen = sorted(observations or (), key=lambda item: item.timestamp)

    baseline, baseline_label = 0, ""
    best = record.best(min_samples=min_samples) if record else None
    if best is not None:
        baseline, window = best[0], best[1]
        baseline_label = f"PigParse {window.label}"

    return PriceChart(
        name=name,
        windows=bars,
        observations=tuple(seen),
        baseline=baseline,
        baseline_label=baseline_label,
        sell=spread([item.price for item in seen if not item.wanted]),
        buy=spread([item.price for item in seen if item.wanted]),
    )
