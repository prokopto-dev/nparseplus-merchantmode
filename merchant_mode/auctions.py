"""Reading prices out of live auction chat (Qt-free, stdlib only).

nParse+ already parses every chat line and republishes it as a typed
``CommsEvent``, so this module never touches a log file — it takes
``event.content`` for ``CommsChannel.AUCTION`` lines and pulls out what is
being offered and for how much.

The splitting and tag-stripping follow the approach proven in the host's
``examples/plugins/merchant_prices/pricing.py``; the difference is that this
one *keeps* the price it strips instead of discarding it, because a local
history of what things actually sold for is the point.

Auction text is free-form and sellers are inventive, so parsing is deliberately
forgiving and every price is optional. A missed price is a blank cell; a
mis-parsed one would be a lie.
"""

from __future__ import annotations

import re
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime

MAX_HISTORY = 400
"""Observations kept in memory and persisted. Roughly a busy evening."""

MIN_NAME_LEN = 3

_SEPARATORS = re.compile(r"\s*(?:\||,|/|;)\s*")

# "5k", "1.5k", "150pp", "20 p", "2kpp". Captured rather than discarded.
_PRICE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(kpp|k|pp|p)(?![\w])",
    re.IGNORECASE,
)
_QUANTITY = re.compile(r"\bx\s*\d+\b", re.IGNORECASE)
_EACH = re.compile(r"\bea\.?\b", re.IGNORECASE)


@dataclass(frozen=True)
class Offer:
    """One item mentioned in an auction line."""

    name: str
    price: int | None = None
    """Platinum, or ``None`` when the seller didn't say."""


@dataclass(frozen=True)
class Observation:
    """A priced offer seen in the channel, for the local history."""

    timestamp: datetime
    name: str
    price: int
    sender: str = ""
    wanted: bool = False
    """True for WTB, false for WTS."""


def parse_price(text: str) -> int | None:
    """Platinum value of the first price tag in ``text``, if any.

    ``5k`` -> 5000, ``1.5k`` -> 1500, ``150pp`` -> 150.
    """
    match = _PRICE.search(text)
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2).lower()
    if unit in ("k", "kpp"):
        amount *= 1000
    return round(amount)


def _clean_name(chunk: str) -> str:
    chunk = _PRICE.sub(" ", chunk)
    chunk = _QUANTITY.sub(" ", chunk)
    chunk = _EACH.sub(" ", chunk)
    chunk = re.sub(r"\s{2,}", " ", chunk)
    return chunk.strip(" .!?'\"-:")


def _offers(body: str) -> list[Offer]:
    offers: list[Offer] = []
    for chunk in _SEPARATORS.split(body):
        if not chunk.strip():
            continue
        name = _clean_name(chunk)
        if len(name) < MIN_NAME_LEN or name.isdigit():
            continue
        offers.append(Offer(name=name, price=parse_price(chunk)))
    return offers


def parse_auction(content: str) -> tuple[list[Offer], list[Offer]]:
    """Split an auction line into ``(selling, buying)``.

    Sellers routinely put both halves on one line — ``WTS foo 5k | WTB bar`` —
    so the markers, not the line, decide which side an item belongs to.
    """
    upper = content.upper()
    wts_at, wtb_at = upper.find("WTS"), upper.find("WTB")
    if wts_at == -1 and wtb_at == -1:
        return [], []

    selling_text = buying_text = ""
    if wts_at != -1 and wtb_at != -1:
        if wts_at < wtb_at:
            selling_text, buying_text = content[wts_at + 3 : wtb_at], content[wtb_at + 3 :]
        else:
            buying_text, selling_text = content[wtb_at + 3 : wts_at], content[wts_at + 3 :]
    elif wts_at != -1:
        selling_text = content[wts_at + 3 :]
    else:
        buying_text = content[wtb_at + 3 :]

    return _offers(selling_text), _offers(buying_text)


class PriceHistory:
    """Recent priced offers seen in the auction channel.

    Bounded on purpose: this is a feel for the going rate, not a market
    database, and it rides along in the plugin's JSON storage.
    """

    def __init__(self, observations: list[Observation] | None = None, *, limit: int = MAX_HISTORY):
        self._limit = limit
        self._items: deque[Observation] = deque(observations or (), maxlen=limit)

    def record(
        self,
        content: str,
        *,
        timestamp: datetime,
        sender: str = "",
    ) -> list[Observation]:
        """Parse one auction line and keep whatever carried a price."""
        selling, buying = parse_auction(content)
        added: list[Observation] = []
        for offers, wanted in ((selling, False), (buying, True)):
            for offer in offers:
                if offer.price is None:
                    continue
                observation = Observation(
                    timestamp=timestamp,
                    name=offer.name,
                    price=offer.price,
                    sender=sender,
                    wanted=wanted,
                )
                self._items.append(observation)
                added.append(observation)
        return added

    def recent(self, name: str | None = None, *, limit: int = 50) -> list[Observation]:
        """Newest first, optionally filtered to one item name."""
        if name is None:
            selected = list(self._items)
        else:
            key = name.strip().casefold()
            selected = [item for item in self._items if item.name.casefold() == key]
        return list(reversed(selected))[:limit]

    def names(self) -> list[str]:
        """Distinct item names seen, newest first, case-insensitively deduped."""
        seen: dict[str, str] = {}
        for item in reversed(self._items):
            seen.setdefault(item.name.casefold(), item.name)
        return list(seen.values())

    def prices_for(self, name: str, *, wanted: bool = False) -> list[int]:
        """Every observed price for ``name`` on one side of the trade."""
        key = name.strip().casefold()
        return [
            item.price
            for item in self._items
            if item.name.casefold() == key and item.wanted == wanted
        ]

    def average(self, name: str, *, wanted: bool = False) -> int | None:
        """Mean observed price for ``name``, or ``None`` if never seen."""
        prices = self.prices_for(name, wanted=wanted)
        if not prices:
            return None
        return round(sum(prices) / len(prices))

    def median(self, name: str, *, wanted: bool = False) -> int | None:
        """Median observed price, or ``None`` if never seen.

        Preferred over the mean for suggesting a price: one optimist asking
        10x the going rate drags a mean somewhere useless, and in a channel
        where people routinely fish for a bite that is not a rare event.
        """
        prices = self.prices_for(name, wanted=wanted)
        if not prices:
            return None
        return round(statistics.median(prices))

    def to_list(self) -> list[dict]:
        return [
            {
                "timestamp": item.timestamp.isoformat(),
                "name": item.name,
                "price": item.price,
                "sender": item.sender,
                "wanted": item.wanted,
            }
            for item in self._items
        ]

    @classmethod
    def from_list(cls, data: object, *, limit: int = MAX_HISTORY) -> PriceHistory:
        """Rebuild from storage, skipping anything malformed."""
        observations: list[Observation] = []
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                try:
                    observations.append(
                        Observation(
                            timestamp=datetime.fromisoformat(str(row["timestamp"])),
                            name=str(row["name"]),
                            price=int(row["price"]),
                            sender=str(row.get("sender", "")),
                            wanted=bool(row.get("wanted", False)),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        return cls(observations, limit=limit)

    def __len__(self) -> int:
        return len(self._items)
