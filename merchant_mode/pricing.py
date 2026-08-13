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

On top of the market number sits the seller's own policy — a markup and a
rounding scale, both off until asked for. Marking up *before* rounding is what
makes the result a number a person would type; and the adjustment is always
named beside the price, because a markup read back later as an observation is
how a merchant talks themselves into a price nobody is paying.

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


DEFAULT_MARKUP_PERCENT = 0
MAX_MARKUP_PERCENT = 100
"""Bound on the markup over market. Doubling the market is already past what
anyone pays, so the ceiling costs no real merchant anything — it is there so a
slipped keystroke (150 for 15) can't quote fifteen times the going rate and
look deliberate while doing it.

Lives here rather than in the plugin for the same reason ``MAX_PAUSE_TENTHS``
lives in :mod:`merchant_mode.socialpack`: the settings page and the setting's
owner both need it, and neither should be importing the package root."""


class Rounding(StrEnum):
    """How far a suggested price is rounded before it's offered.

    The value *is* the step in platinum, so a stored setting reads as the thing
    it does and the arithmetic needs no lookup table beside it.

    The scales stop at a thousand and skip 250 on purpose: :func:`format_price`
    renders a multiple of 100 as ``1.5k`` but ``1250`` as ``1250pp``, so a step
    that isn't a divisor of 1000 would round the number *away* from how a
    seller writes it — the opposite of what rounding is for here.
    """

    NONE = "none"
    TEN = "10"
    FIFTY = "50"
    HUNDRED = "100"
    FIVE_HUNDRED = "500"
    THOUSAND = "1000"

    @property
    def step(self) -> int:
        """Platinum to round to, or ``0`` for no rounding at all."""
        return 0 if self is Rounding.NONE else int(self.value)

    @property
    def label(self) -> str:
        return "no rounding" if self is Rounding.NONE else f"nearest {self.step}"


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
    adjusted: str = ""
    """What your own settings did to the market number on its way here — e.g.
    ``"+15%, nearest 100"`` — or ``""`` when the number is the market's own.

    A marked-up price that presents itself as an observation is a confident
    mistake, the failure this plugin is built to avoid: the seller would read
    their own markup back as evidence and mark it up again."""

    @property
    def known(self) -> bool:
        return self.price is not None and self.price > 0

    @property
    def text(self) -> str:
        """The price as a seller would type it, or ``""`` when unknown."""
        return format_price(self.price) if self.known else ""

    @property
    def provenance(self) -> str:
        """Where the number came from *and* what was done to it, for the UI."""
        return f"{self.source.label} {self.adjusted}" if self.adjusted else self.source.label


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


def adjust(price: int, *, markup_percent: int = 0, rounding: Rounding = Rounding.NONE) -> int:
    """Mark up first, then round — so the round number is the one you ask for.

    The order is the whole point. 1000 plat at +15% is 1150, which rounds to
    1200. Rounding first would give 1000 -> 1000 -> 1150, defeating both
    settings at once: the rounding would have done nothing and the markup would
    have left a number nobody writes in ``/auction``.

    Platinum is an int everywhere in this plugin, so the markup rounds
    half-up rather than truncating — +10% on 105pp is 116pp, not 115pp.
    """
    if price <= 0:
        return price
    marked = (price * (100 + markup_percent) + 50) // 100
    step = rounding.step
    if step <= 0:
        return marked
    rounded = ((marked + step // 2) // step) * step
    # Never round a real price away to nothing. A 40pp item with rounding set
    # to 100 is still worth something, and a 0 in the price box reads as free.
    return rounded or step


def describe_adjustment(*, markup_percent: int = 0, rounding: Rounding = Rounding.NONE) -> str:
    """How to say what :func:`adjust` did, or ``""`` if it would do nothing.

    Shown beside the price rather than folded into it, because the seller has
    to be able to tell their own policy apart from the market's answer.
    """
    parts = []
    if markup_percent:
        parts.append(f"{markup_percent:+d}%")
    if rounding is not Rounding.NONE:
        parts.append(rounding.label)
    return ", ".join(parts)


def suggest(
    name: str,
    *,
    history=None,
    averages: dict[str, int] | None = None,
    side: Side = Side.SELL,
    min_samples: int = 1,
    matcher=None,
    server: str | None = None,
    markup_percent: int = 0,
    rounding: Rounding = Rounding.NONE,
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

    ``markup_percent`` and ``rounding`` are the seller's own pricing policy,
    passed in the way ``averages`` and ``matcher`` are so this stays a function
    of its arguments. They apply to **asks only**: the same call quotes the WTB
    list, and marking up what you offer to pay is an offer to overpay.
    """
    if side is not Side.SELL:
        markup_percent, rounding = 0, Rounding.NONE
    note = describe_adjustment(markup_percent=markup_percent, rounding=rounding)

    def offer(price: int | None, source: PriceSource, samples: int = 0) -> Suggestion:
        return Suggestion(
            price=adjust(price, markup_percent=markup_percent, rounding=rounding)
            if price
            else price,
            source=source,
            samples=samples,
            # Only claim an adjustment when there was a number to adjust.
            adjusted=note if price else "",
        )

    if history is not None:
        matching = history.prices_for(name, wanted=side.wanted, matcher=matcher, server=server)
        if len(matching) >= min_samples:
            return offer(
                history.median(name, wanted=side.wanted, matcher=matcher, server=server),
                PriceSource.OBSERVED,
                len(matching),
            )
        opposite = history.prices_for(name, wanted=not side.wanted, matcher=matcher, server=server)
        if len(opposite) >= min_samples:
            return offer(
                history.median(name, wanted=not side.wanted, matcher=matcher, server=server),
                PriceSource.OBSERVED_OPPOSITE,
                len(opposite),
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
            return offer(int(average), PriceSource.PIGPARSE)

    return Suggestion()
