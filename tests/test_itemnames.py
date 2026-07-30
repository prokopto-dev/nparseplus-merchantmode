"""The searchable item-name universe.

PigParse has no search endpoint, so finding an item is entirely a local
problem — which makes this index the only thing standing between the user and
having to type ``Fungus Covered Scale Tunic`` exactly right.
"""

from __future__ import annotations

from merchant_mode.itemnames import ItemNameIndex, load_master_names, normalize


def test_a_master_list_is_bundled_so_search_works_without_the_host() -> None:
    """The vendored copy is what makes reaching for the host's file safe.

    Nothing in nParse+ actually reads its own copy, so that path is not a
    contract and could move; if it does, this must still return names.
    """
    names, source = load_master_names()
    assert len(names) > 10_000
    assert source in {"host item list", "bundled item list"}
    assert "Cloak of Flames" in names


def test_search_ranks_exact_then_prefix_then_substring() -> None:
    index = ItemNameIndex(
        ["Flames", "Cloak of Flames", "Flames of Ro", "Ring of Flames Eternal"]
    )
    assert index.search("flames")[0] == "Flames"
    assert index.search("flames of")[0] == "Flames of Ro"


def test_search_finds_real_items_in_the_bundled_list() -> None:
    index = ItemNameIndex.from_master()
    assert "Cloak of Flames" in index.search("cloak of fl")
    assert "Manastone" in index.search("manastone")


def test_search_tolerates_a_typo_only_when_nothing_matched_literally() -> None:
    index = ItemNameIndex(["Cloak of Flames", "Cloak of Leaves"])
    # A literal substring hit is never pushed down the list by a fuzzy one.
    assert index.search("cloak of l") == ["Cloak of Leaves"]
    assert "Cloak of Flames" in index.search("clok of flames")


def test_a_short_or_empty_query_returns_nothing_rather_than_everything() -> None:
    index = ItemNameIndex.from_master()
    assert index.search("") == []
    assert index.search("   ") == []


def test_names_are_deduped_case_insensitively_first_spelling_wins() -> None:
    index = ItemNameIndex(["Cloak of Flames"])
    assert index.add(["cloak of flames", "CLOAK OF FLAMES"]) == 0
    assert index.names() == ["Cloak of Flames"]
    assert "cloak of flames" in index
    assert index.canonical("CLOAK OF FLAMES") == "Cloak of Flames"


def test_learned_names_extend_the_bundled_list() -> None:
    """Items added to P99 after the list was cut still have to be findable."""
    index = ItemNameIndex.from_master()
    assert index.search("Zzyzx Ceremonial Blade") == []
    index.add(["Zzyzx Ceremonial Blade"])
    assert index.search("zzyzx") == ["Zzyzx Ceremonial Blade"]


def test_normalize_collapses_case_and_whitespace() -> None:
    assert normalize("  Cloak   of  Flames ") == "cloak of flames"
