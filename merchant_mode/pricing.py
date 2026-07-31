"""Suggesting a price for an item (Qt-free, stdlib only).

Two sources know what something is worth, and they disagree in a useful way.
Live ``/auction`` traffic is what the market is doing *today*; PigParse's
six-month average is steadier but slower to notice that a patch, a new camp, or
a wave of Kunark twinks moved the floor. So live wins when there is live data,
and PigParse fills the silence.

Every suggestion carries its provenance. A number with no source shown is a
number the user has to take on faith, and the whole point of this plugin is
that they shouldn't have to — the same reason item ids carry an
:class:`~merchant_mode.catalog.IdStatus`.

Nothing here decides anything on its own. Suggestions land in an editable
field; the seller always has the last word on their own price.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Side(StrEnum):
    """Which half of the trade a price is for."""

    SELL = "sell"
    BUY = "buy"

    @property
    def wanted(self) -> bool:
        """The ``PriceHistory`` flag for this side."""
        return self is Side.BUY


class PriceSource(StrEnum):
    """Where a suggested price came from."""

    OBSERVED = "observed"
    """Median of matching auctions seen in the channel."""
    OBSERVED_OPPOSITE = "observed-opposite"
    """Median from the *other* side — a WTB ask standing in for a WTS price."""
    PIGPARSE = "pigparse"
    """PigParse six-month WTS average."""
    NONE = "none"

    @property
    def label(self) -> str:
        return {
            PriceSource.OBSERVED: "seen in /auc",
            PriceSource.OBSERVED_OPPOSITE: "other side of /auc",
            PriceSource.PIGPARSE: "PigParse 6mo",
            PriceSource.NONE: "no data",
        }[self]


@dataclass(frozen=True)
class Suggestion:
    """A suggested price and where it came from."""

    price: int | None = None
    source: PriceSource = PriceSource.NONE
    samples: int = 0
    """How many observations backed it; 0 for PigParse or no data."""

    @property
    def known(self) -> bool:
        return self.price is not None and self.price > 0

    @property
    def text(self) -> str:
        """The price as a seller would type it, or ``""`` when unknown."""
        return format_price(self.price) if self.known else ""


def format_price(platinum: int | None) -> str:
    """Render platinum the way people actually write it in ``/auction``.

    ``5000`` -> ``5k``, ``1500`` -> ``1.5k``, ``850`` -> ``850pp``. Anything
    that would need more than one decimal place stays in plat rather than
    inventing false precision — ``1234`` is ``1234pp``, not ``1.2k``.
    """
    if not platinum or platinum <= 0:
        return ""
    if platinum < 1000:
        return f"{platinum}pp"
    if platinum % 1000 == 0:
        return f"{platinum // 1000}k"
    if platinum % 100 == 0:
        return f"{platinum / 1000:g}k"
    return f"{platinum}pp"


def suggest(
    name: str,
    *,
    history=None,
    averages: dict[str, int] | None = None,
    side: Side = Side.SELL,
    min_samples: int = 1,
    matcher=None,
    server: str | None = None,
) -> Suggestion:
    """Best known price for ``name``, with provenance.

    Order: the median of matching-side auctions, then the median of the other
    side, then the PigParse average. ``min_samples`` raises the bar for
    trusting live data — a single optimistic auction is not a market.

    ``matcher`` is a :class:`~merchant_mode.matching.NameMatcher`. Without one,
    both lookups demand the exact spelling, which is why this used to come back
    empty for items the channel had been pricing all evening.

    ``server`` narrows the live half to one server's channel; ``averages`` is
    expected to have been narrowed by the caller already. A price is only ever
    an answer about one server, since that is the only place the item can
    change hands.
    """
    if history is not None:
        matching = history.prices_for(name, wanted=side.wanted, matcher=matcher, server=server)
        if len(matching) >= min_samples:
            return Suggestion(
                price=history.median(name, wanted=side.wanted, matcher=matcher, server=server),
                source=PriceSource.OBSERVED,
                samples=len(matching),
            )
        opposite = history.prices_for(name, wanted=not side.wanted, matcher=matcher, server=server)
        if len(opposite) >= min_samples:
            return Suggestion(
                price=history.median(name, wanted=not side.wanted, matcher=matcher, server=server),
                source=PriceSource.OBSERVED_OPPOSITE,
                samples=len(opposite),
            )

    if averages:
        # PigParse answers under its own spelling, so try the canonical name
        # too — the average was very likely stored under that one.
        average = averages.get(name.strip().casefold())
        if not average and matcher is not None:
            resolved = matcher.resolve(name)
            if resolved is not None:
                average = averages.get(resolved.strip().casefold())
        if average and average > 0:
            return Suggestion(price=int(average), source=PriceSource.PIGPARSE)

    return Suggestion()
