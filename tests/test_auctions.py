"""merchant_mode.auctions — reading prices out of live auction chat."""

from __future__ import annotations

from datetime import datetime, timedelta

from merchant_mode.auctions import Observation, PriceHistory, parse_auction, parse_price

T0 = datetime(2026, 7, 30, 20, 0, 0)


# --- prices ----------------------------------------------------------------


def test_parses_the_price_shorthands_sellers_actually_use() -> None:
    assert parse_price("5k") == 5000
    assert parse_price("1.5k") == 1500
    assert parse_price("150pp") == 150
    assert parse_price("20 p") == 20
    assert parse_price("2kpp") == 2000


def test_a_line_without_a_price_yields_none() -> None:
    assert parse_price("Cloak of Flames") is None
    assert parse_price("") is None


# --- offers ----------------------------------------------------------------


def test_splits_a_wts_line_into_priced_offers() -> None:
    selling, buying = parse_auction("WTS Cloak of Flames 5k | Fungi Tunic 12k")
    assert [(offer.name, offer.price) for offer in selling] == [
        ("Cloak of Flames", 5000),
        ("Fungi Tunic", 12000),
    ]
    assert buying == []


def test_separates_the_selling_and_buying_halves_of_one_line() -> None:
    selling, buying = parse_auction("WTS Manastone 40k WTB Guise 100k")
    assert [offer.name for offer in selling] == ["Manastone"]
    assert [offer.name for offer in buying] == ["Guise"]


def test_handles_wtb_appearing_before_wts() -> None:
    selling, buying = parse_auction("WTB Rubicite 50k WTS Jboots 3k")
    assert [offer.name for offer in selling] == ["Jboots"]
    assert [offer.name for offer in buying] == ["Rubicite"]


def test_a_line_with_neither_marker_yields_nothing() -> None:
    assert parse_auction("anyone want to group?") == ([], [])


def test_unpriced_items_still_parse_with_no_price() -> None:
    selling, _ = parse_auction("WTS Cloak of Flames")
    assert selling[0].name == "Cloak of Flames"
    assert selling[0].price is None


def test_quantity_and_each_tags_are_stripped_from_names() -> None:
    selling, _ = parse_auction("WTS Bone Chips x20 ea. 2pp")
    assert selling[0].name == "Bone Chips"
    assert selling[0].price == 2


def test_very_short_chunks_are_ignored() -> None:
    selling, _ = parse_auction("WTS a | ok | Cloak of Flames 5k")
    assert [offer.name for offer in selling] == ["Cloak of Flames"]


# --- history ---------------------------------------------------------------


def test_records_only_priced_offers() -> None:
    history = PriceHistory()
    added = history.record("WTS Cloak of Flames 5k | Mystery Item", timestamp=T0, sender="Xantik")
    assert [obs.name for obs in added] == ["Cloak of Flames"]
    assert len(history) == 1


def test_records_which_side_of_the_trade_an_offer_was() -> None:
    history = PriceHistory()
    history.record("WTS Manastone 40k WTB Guise 100k", timestamp=T0)
    sides = {obs.name: obs.wanted for obs in history.recent()}
    assert sides == {"Manastone": False, "Guise": True}


def test_recent_returns_newest_first() -> None:
    history = PriceHistory()
    history.record("WTS Alpha Item 1k", timestamp=T0)
    history.record("WTS Beta Item 2k", timestamp=T0 + timedelta(minutes=1))
    assert [obs.name for obs in history.recent()] == ["Beta Item", "Alpha Item"]


def test_recent_can_filter_to_one_item_case_insensitively() -> None:
    history = PriceHistory()
    history.record("WTS Cloak of Flames 5k", timestamp=T0)
    history.record("WTS Fungi Tunic 12k", timestamp=T0)
    assert len(history.recent("cloak of flames")) == 1


def test_average_is_per_side_of_the_trade() -> None:
    history = PriceHistory()
    history.record("WTS Manastone 40k", timestamp=T0)
    history.record("WTS Manastone 50k", timestamp=T0)
    history.record("WTB Manastone 10k", timestamp=T0)
    assert history.average("Manastone") == 45000
    assert history.average("Manastone", wanted=True) == 10000


def test_average_of_an_unseen_item_is_none() -> None:
    assert PriceHistory().average("Nothing") is None


def test_history_is_bounded() -> None:
    history = PriceHistory(limit=3)
    for n in range(10):
        history.record(f"WTS Item Number {n} 1k", timestamp=T0)
    assert len(history) == 3


def test_names_are_deduped_case_insensitively_newest_first() -> None:
    history = PriceHistory()
    history.record("WTS Cloak of Flames 5k", timestamp=T0)
    history.record("WTS cloak of flames 6k", timestamp=T0 + timedelta(minutes=1))
    history.record("WTS Fungi Tunic 12k", timestamp=T0 + timedelta(minutes=2))
    assert history.names() == ["Fungi Tunic", "cloak of flames"]


def test_history_round_trips_through_storage() -> None:
    history = PriceHistory()
    history.record("WTS Cloak of Flames 5k", timestamp=T0, sender="Xantik")
    restored = PriceHistory.from_list(history.to_list())
    assert len(restored) == 1
    observation = restored.recent()[0]
    assert observation == Observation(T0, "Cloak of Flames", 5000, "Xantik", False)


def test_malformed_stored_rows_are_skipped_not_fatal() -> None:
    restored = PriceHistory.from_list(
        [
            {"timestamp": "nonsense", "name": "x", "price": 1},
            {"name": "missing timestamp", "price": 1},
            "not a dict",
            {"timestamp": T0.isoformat(), "name": "Good", "price": 5},
        ]
    )
    assert [obs.name for obs in restored.recent()] == ["Good"]


def test_non_list_storage_yields_an_empty_history() -> None:
    assert len(PriceHistory.from_list(None)) == 0
    assert len(PriceHistory.from_list({"nope": 1})) == 0
