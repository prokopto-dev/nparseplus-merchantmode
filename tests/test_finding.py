"""Searching held items by whatever the buyer actually typed.

The whole point of this module is that ``locate()``'s exact-string comparison
answers a question nobody asks. These tests are mostly about the rungs below
exact, and about the ordering between them: a search that finds the right item
but buries it under six substring hits has not answered anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from merchant_mode.finding import MatchKind, find_holdings
from merchant_mode.inventory import CharacterInventory, InventoryItem, InventoryVault
from merchant_mode.matching import NameMatcher
from merchant_mode.nicknames import NicknameTable

T0 = datetime(2026, 7, 30, 21, 0, 0)


def _vault() -> InventoryVault:
    vault = InventoryVault()
    vault.put(
        "Xantik",
        "green",
        [
            InventoryItem("Chest", "Fungus Covered Scale Tunic", 2735, 1, 0),
            InventoryItem("Back", "Cloak of Flames", 11621, 1, 0),
            InventoryItem("General1-Slot1", "Fine Steel Long Sword", 5350, 1, 0),
            InventoryItem("General1-Slot2", "Bone Chips", 13073, 14, 0),
        ],
        captured_at=T0 - timedelta(hours=2),
    )
    vault.put(
        "Mulebank",
        "blue",
        [
            InventoryItem("General1-Slot1", "Rubicite Breastplate", 9876, 1, 0),
            InventoryItem("General1-Slot2", "Cloak of Flames", 11621, 1, 0),
        ],
        captured_at=T0 - timedelta(days=31),
    )
    return vault


def _matcher(vault: InventoryVault, **nicknames: str) -> NameMatcher:
    table = NicknameTable({name: nick for name, nick in nicknames.items()})
    return NameMatcher([holding.name for holding in vault.holdings()], nicknames=table)


def test_exact_name_matches_regardless_of_case_and_punctuation() -> None:
    vault = InventoryVault()
    vault.put(
        "Xantik",
        "green",
        [InventoryItem("Feet", "Journeyman's Boots", 4576, 1, 0)],
        captured_at=T0,
    )
    found = find_holdings("journeymans  boots", vault.holdings())
    assert [match.name for match in found] == ["Journeyman's Boots"]
    assert found[0].kind is MatchKind.EXACT


def test_a_nickname_resolves_through_the_matcher() -> None:
    vault = _vault()
    matcher = _matcher(vault, **{"Fungus Covered Scale Tunic": "fungi"})
    found = find_holdings("fungi", vault.holdings(), matcher=matcher)
    assert [match.name for match in found] == ["Fungus Covered Scale Tunic"]
    assert found[0].kind is MatchKind.RESOLVED


def test_an_acronym_resolves_through_the_matcher() -> None:
    """``cf`` is derived from the canonical name, not from a nickname table.

    Noise words are skipped when the acronym is built, so ``Cloak of Flames``
    is ``cf`` and not ``cof`` — see :func:`merchant_mode.matching.acronym`.
    """
    vault = _vault()
    found = find_holdings("cf", vault.holdings(), matcher=_matcher(vault))
    assert {match.name for match in found} == {"Cloak of Flames"}
    assert all(match.kind is MatchKind.RESOLVED for match in found)


def test_a_partial_name_matches_as_a_substring() -> None:
    found = find_holdings("scale tun", _vault().holdings())
    assert [match.name for match in found] == ["Fungus Covered Scale Tunic"]
    assert found[0].kind is MatchKind.WORD


def test_a_leading_fragment_ranks_as_a_prefix() -> None:
    found = find_holdings("bone", _vault().holdings())
    assert found[0].kind is MatchKind.PREFIX


def test_an_exact_hit_never_sits_below_a_substring_hit() -> None:
    """Ranking is the whole job — completeness without it is a scroll bar."""
    vault = InventoryVault()
    vault.put(
        "Xantik",
        "green",
        [
            InventoryItem("General1-Slot1", "Rusty Long Sword", 5019, 1, 0),
            InventoryItem("General1-Slot2", "Long Sword", 5023, 1, 0),
            InventoryItem("General1-Slot3", "Fine Steel Long Sword", 5350, 1, 0),
        ],
        captured_at=T0,
    )
    found = find_holdings("long sword", vault.holdings())
    assert found[0].name == "Long Sword"
    assert found[0].kind is MatchKind.EXACT
    assert {match.name for match in found[1:]} == {"Rusty Long Sword", "Fine Steel Long Sword"}


def test_every_holding_of_a_matching_name_is_returned() -> None:
    """Two mules holding the same item is the answer, not a duplicate."""
    found = find_holdings("Cloak of Flames", _vault().holdings())
    assert {match.character for match in found} == {"Xantik", "Mulebank"}
    assert {match.server for match in found} == {"green", "blue"}


def test_results_cross_servers_because_the_question_does() -> None:
    """The Sell tab is scoped to one server; "is it anywhere" is not."""
    found = find_holdings("rubicite", _vault().holdings())
    assert [match.server for match in found] == ["blue"]


def test_a_match_carries_its_location_count_and_staleness() -> None:
    found = find_holdings("bone chips", _vault().holdings())
    assert found[0].count == 14
    assert found[0].where() == "Xantik · General1-Slot2"
    assert not found[0].is_stale(T0)

    old = find_holdings("rubicite", _vault().holdings())[0]
    assert old.is_stale(T0)
    assert old.age_text(T0) == "4w"
    assert "4w old" in old.where(T0)


def test_no_match_is_an_empty_list_not_a_guess() -> None:
    assert find_holdings("manastone", _vault().holdings()) == []
    assert find_holdings("", _vault().holdings()) == []
    assert find_holdings("cloak", []) == []


def test_a_one_character_query_does_not_match_everything() -> None:
    """Below the substring floor, only exact and resolved rungs run."""
    assert find_holdings("o", _vault().holdings()) == []


def test_it_still_works_without_a_matcher() -> None:
    """No matcher means no nickname rung, not no search."""
    found = find_holdings("flames", _vault().holdings(), matcher=None)
    assert {match.name for match in found} == {"Cloak of Flames"}


def test_the_limit_is_honoured() -> None:
    vault = InventoryVault()
    vault.put(
        "Xantik",
        "green",
        [
            InventoryItem(f"General1-Slot{n}", f"Bag of Holding {n}", 100 + n, 1, 0)
            for n in range(9)
        ],
        captured_at=T0,
    )
    assert len(find_holdings("bag", vault.holdings(), limit=3)) == 3


def test_matches_expose_the_underlying_holding() -> None:
    match = find_holdings("cloak of flames", _vault().holdings())[0]
    assert match.item_id == 11621
    assert isinstance(match.holding.captured_at, datetime)


def test_the_vault_records_where_a_dump_came_from() -> None:
    """Reloading a stale dump needs the path; a v3 store simply has none."""
    vault = InventoryVault()
    vault.put("Xantik", "green", [], captured_at=T0, source_path="/tmp/Xantik-Inventory.txt")
    assert vault.get("Xantik", "green").source_path == "/tmp/Xantik-Inventory.txt"
    assert InventoryVault.from_dict(vault.to_dict()).get("Xantik", "green").source_path
    assert CharacterInventory("Xantik", "green", T0, []).source_path == ""
