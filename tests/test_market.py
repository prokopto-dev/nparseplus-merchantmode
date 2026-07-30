"""PigParse's stats block, and which window to believe.

An average without its sample count is not an answer, which is the whole
reason this type exists instead of a bare int.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from merchant_mode.market import MarketRecord


class _Wire:
    """Shaped like the host's ``ItemPrice``; read duck-typed on purpose."""

    def __init__(self, **fields) -> None:
        self.item_name = "Cloak of Flames"
        self.eq_item_id = 11621
        self.total_wts_last_30_days_average = 0
        self.total_wts_last_30_days_count = 0
        self.total_wts_last_90_days_average = 0
        self.total_wts_last_90_days_count = 0
        self.total_wts_last_6_months_average = 0
        self.total_wts_last_6_months_count = 0
        self.total_wts_auction_average = 0
        self.total_wts_auction_count = 0
        self.last_wts_seen = None
        self.__dict__.update(fields)


def test_the_whole_block_is_kept_not_just_the_six_month_average() -> None:
    record = MarketRecord.from_pigparse(
        _Wire(
            total_wts_last_30_days_average=6000,
            total_wts_last_30_days_count=9,
            total_wts_last_6_months_average=5000,
            total_wts_last_6_months_count=54,
        )
    )
    assert record.average("30d") == 6000
    assert record.count("30d") == 9
    assert record.average("6mo") == 5000
    assert record.count("6mo") == 54
    assert record.item_id == 11621


def test_a_record_with_no_name_is_refused() -> None:
    assert MarketRecord.from_pigparse(_Wire(item_name="  ")) is None


def test_the_narrowest_well_sampled_window_wins() -> None:
    """Recent beats broad: a six-month figure still carries pre-patch prices."""
    record = MarketRecord.from_pigparse(
        _Wire(
            total_wts_last_30_days_average=6000,
            total_wts_last_30_days_count=9,
            total_wts_last_6_months_average=5000,
            total_wts_last_6_months_count=54,
        )
    )
    price, window = record.best()
    assert (price, window.key) == (6000, "30d")


def test_a_thinly_sampled_window_is_skipped_for_a_better_one() -> None:
    record = MarketRecord.from_pigparse(
        _Wire(
            total_wts_last_30_days_average=99000,  # one optimist
            total_wts_last_30_days_count=1,
            total_wts_last_6_months_average=5000,
            total_wts_last_6_months_count=54,
        )
    )
    price, window = record.best()
    assert (price, window.key) == (5000, "6mo")


def test_a_thin_sample_is_still_better_than_nothing() -> None:
    record = MarketRecord.from_pigparse(
        _Wire(total_wts_last_30_days_average=99000, total_wts_last_30_days_count=1)
    )
    assert record.headline == 99000


def test_an_item_pigparse_knows_only_by_name_has_no_headline() -> None:
    record = MarketRecord.from_pigparse(_Wire())
    assert record.best() is None
    assert record.headline == 0


def test_staleness_is_measured_from_the_fetch() -> None:
    now = datetime(2026, 7, 30, 12, 0)
    fresh = MarketRecord(name="x", fetched_at=now - timedelta(hours=1))
    old = MarketRecord(name="x", fetched_at=now - timedelta(days=2))
    assert not fresh.is_stale(now)
    assert old.is_stale(now)
    # Never fetched at all counts as stale, so it gets asked about.
    assert MarketRecord(name="x").is_stale(now)


def test_round_trips_through_storage() -> None:
    original = MarketRecord.from_pigparse(
        _Wire(
            total_wts_last_90_days_average=5100,
            total_wts_last_90_days_count=18,
            last_wts_seen=datetime(2026, 7, 28, 19, 4),
        ),
        server="green",
    )
    restored = MarketRecord.from_dict(original.to_dict())
    assert restored == original


def test_malformed_storage_is_dropped_not_raised() -> None:
    assert MarketRecord.from_dict(None) is None
    assert MarketRecord.from_dict({"name": ""}) is None
    salvaged = MarketRecord.from_dict({"name": "x", "averages": "nope", "counts": 42})
    assert salvaged.averages == {} and salvaged.counts == {}
