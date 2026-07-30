"""Matching free-form auction text to canonical item names.

The bar is asymmetric on purpose: a miss costs a blank price box, a false
match silently prices the wrong item. So every test that asserts a match has a
sibling asserting that something plausible-but-different does *not* match.
"""

from __future__ import annotations

import pytest

from merchant_mode.matching import NameMatcher, acronym, normalize
from merchant_mode.nicknames import NicknameTable

ITEMS = [
    "Cloak of Flames",
    "Fungus Covered Scale Tunic",
    "Journeyman's Boots",
    "Rubicite Breastplate",
    "Manastone",
    "Fine Steel Long Sword",
    "Rusty Long Sword",
]


@pytest.fixture
def matcher() -> NameMatcher:
    return NameMatcher(ITEMS, nicknames=NicknameTable())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Cloak of Flames", "Cloak of Flames"),
        ("cloak of flames", "Cloak of Flames"),
        ("Cloak  of   Flames", "Cloak of Flames"),  # collapsed whitespace
        ("Cloack of Flames", "Cloak of Flames"),  # transposed letters
        ("CoF", "Cloak of Flames"),  # the user's own nickname table
        ("Fungi", "Fungus Covered Scale Tunic"),
        ("FCST", "Fungus Covered Scale Tunic"),  # derived acronym
        ("JBoots", "Journeyman's Boots"),
        ("journeymans boots", "Journeyman's Boots"),  # apostrophe dropped
        ("Rubi BP", "Rubicite Breastplate"),
        ("fine steel longsword", "Fine Steel Long Sword"),
    ],
)
def test_the_channel_s_spellings_resolve(matcher, text, expected) -> None:
    assert matcher.resolve(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "sword",  # ambiguous between two swords, and too short to fuzz
        "Guise of the Deceiver",  # a real item, just not one in this set
        "Cloak of Frozen Flames",  # a different real cloak
        "wts",
    ],
)
def test_nothing_plausible_but_wrong_resolves(matcher, text) -> None:
    assert matcher.resolve(text) is None


def test_an_ambiguous_acronym_is_refused() -> None:
    """Two items claiming ``rls`` means neither may have it."""
    matcher = NameMatcher(["Rusty Long Sword", "Rune Long Staff"])
    assert acronym("Rusty Long Sword") == acronym("Rune Long Staff") == "rls"
    assert matcher.resolve("rls") is None
    # ...but the full names still resolve, so the collision costs only the alias.
    assert matcher.resolve("Rusty Long Sword") == "Rusty Long Sword"


def test_an_alias_never_shadows_a_real_item_name() -> None:
    matcher = NameMatcher(["Mana", "Manastone"], nicknames=NicknameTable())
    # "mana" is a nickname for Manastone in the default table, but it is also a
    # real item here, and the real item wins.
    assert matcher.resolve("Mana") == "Mana"


def test_same_falls_back_to_equality_for_unknown_names(matcher) -> None:
    assert matcher.same("Widget of Nothing", "widget of nothing")
    assert not matcher.same("Widget of Nothing", "Gadget of Nothing")


def test_normalize_and_acronym() -> None:
    assert normalize("  Teir`Dal   Robe   ") == "teirdal robe"
    assert normalize("Journeyman's Boots") == "journeymans boots"
    assert acronym("Cloak of Flames") == "cf"  # "of" is skipped
    assert acronym("Manastone") == "m"
    assert acronym("") == ""
