"""What PigParse knows about an item (Qt-free, stdlib only).

The host's ``ItemPrice`` wire model carries a whole WTS stats block — 30-day,
90-day and 6-month averages, a sample count for each, and when the item was
last seen in an auction. The plugin used to read exactly one field of it
(``total_wts_last_6_months_average``) and then never show it to anyone.

That's the difference between a number and an answer. "5k" tells you nothing
about whether that's one sale from February or forty from last week, and those
two situations call for very different asking prices. So the whole block is
kept, with its counts, and the UI shows them.

Reading is deliberately duck-typed: ``ItemPrice`` lives in the host's
``nparseplus.net.pigparse_models`` and this module must import without it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

__all__ = ["MarketRecord", "Window", "WINDOWS"]

STALE_AFTER = timedelta(hours=12)
"""How long a fetched record stays worth reusing before it's worth re-asking.

PigParse aggregates over months, so nothing meaningful moves in an hour; this
is about not re-requesting the same forty names on every tick, not freshness.
"""


@dataclass(frozen=True)
class Window:
    """One of PigParse's averaging periods."""

    key: str
    label: str
    average_attr: str
    count_attr: str


WINDOWS: tuple[Window, ...] = (
    Window("30d", "30 days", "total_wts_last_30_days_average", "total_wts_last_30_days_count"),
    Window("90d", "90 days", "total_wts_last_90_days_average", "total_wts_last_90_days_count"),
    Window(
        "6mo",
        "6 months",
        "total_wts_last_6_months_average",
        "total_wts_last_6_months_count",
    ),
    Window("all", "all time", "total_wts_auction_average", "total_wts_auction_count"),
)
"""Narrowest first. The UI reads them in this order, and :meth:`MarketRecord.best`
picks the first one with enough samples to mean anything."""

MIN_SAMPLES = 2
"""Below this a "window average" is one person's asking price wearing a suit."""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class MarketRecord:
    """PigParse's WTS stats for one item, as of when it was fetched."""

    name: str
    item_id: int | None = None
    averages: dict[str, int] = None  # type: ignore[assignment]
    counts: dict[str, int] = None  # type: ignore[assignment]
    last_seen: datetime | None = None
    fetched_at: datetime | None = None
    server: str = ""

    def __post_init__(self) -> None:
        # Frozen dataclasses can't assign in __init__, but mutable defaults are
        # a worse trade than one object.__setattr__ each.
        if self.averages is None:
            object.__setattr__(self, "averages", {})
        if self.counts is None:
            object.__setattr__(self, "counts", {})

    @classmethod
    def from_pigparse(
        cls,
        record: Any,
        *,
        server: str = "",
        fetched_at: datetime | None = None,
    ) -> MarketRecord | None:
        """Adapt one host ``ItemPrice``. ``None`` when it carries no name."""
        name = str(getattr(record, "item_name", "") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            item_id=_int(getattr(record, "eq_item_id", None)) or None,
            averages={
                window.key: _int(getattr(record, window.average_attr, 0)) for window in WINDOWS
            },
            counts={window.key: _int(getattr(record, window.count_attr, 0)) for window in WINDOWS},
            last_seen=getattr(record, "last_wts_seen", None),
            fetched_at=fetched_at or datetime.now().replace(microsecond=0),
            server=server,
        )

    def average(self, key: str) -> int:
        return self.averages.get(key, 0)

    def count(self, key: str) -> int:
        return self.counts.get(key, 0)

    def best(self, *, min_samples: int = MIN_SAMPLES) -> tuple[int, Window] | None:
        """Narrowest window with enough samples, and its average.

        Recent beats broad: a 30-day average built on five sales describes
        today's market better than a 6-month one built on forty, because the
        six-month figure is still carrying whatever the item cost before the
        last patch.
        """
        for window in WINDOWS:
            average, count = self.average(window.key), self.count(window.key)
            if average > 0 and count >= min_samples:
                return average, window
        for window in WINDOWS:  # nothing well-sampled; take anything real
            average = self.average(window.key)
            if average > 0:
                return average, window
        return None

    @property
    def headline(self) -> int:
        """The single number to fill a price box with, or 0."""
        best = self.best()
        return best[0] if best is not None else 0

    @property
    def total_sales(self) -> int:
        return self.count("all")

    def is_stale(self, now: datetime, *, after: timedelta = STALE_AFTER) -> bool:
        if self.fetched_at is None:
            return True
        return (now - self.fetched_at) > after

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "item_id": self.item_id,
            "averages": dict(self.averages),
            "counts": dict(self.counts),
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "server": self.server,
        }

    @classmethod
    def from_dict(cls, data: object) -> MarketRecord | None:
        """Rebuild from storage; ``None`` for anything malformed."""
        if not isinstance(data, dict):
            return None
        name = str(data.get("name", "")).strip()
        if not name:
            return None
        averages = data.get("averages")
        counts = data.get("counts")
        return cls(
            name=name,
            item_id=_int(data.get("item_id")) or None,
            averages={str(k): _int(v) for k, v in averages.items()}
            if isinstance(averages, dict)
            else {},
            counts={str(k): _int(v) for k, v in counts.items()} if isinstance(counts, dict) else {},
            last_seen=_parse_stamp(data.get("last_seen")),
            fetched_at=_parse_stamp(data.get("fetched_at")),
            server=str(data.get("server", "")),
        )


def _parse_stamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
