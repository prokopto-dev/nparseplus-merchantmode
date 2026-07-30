"""The item filter list.

The rules are simple enough that the tests are mostly about the two decisions
that aren't: KEEP beating HIDE regardless of order, and a disabled rule staying
in the list rather than being deleted.
"""

from __future__ import annotations

from merchant_mode.filters import SUGGESTED_RULES, Action, FilterRule, ItemFilters, Match


def test_a_contains_rule_hides_everything_it_names() -> None:
    rules = ItemFilters([FilterRule("bag")])
    assert rules.hidden("Large Bag")
    assert rules.hidden("Bag of the Tinkerers")
    assert not rules.hidden("Cloak of Flames")


def test_keep_beats_hide_whichever_order_they_are_in() -> None:
    """The case the whole feature exists for: some bags are merchandise.

    Order-dependence would make a filter list something you debug rather than
    something you write, so the exception wins from wherever it sits.
    """
    hide = FilterRule("bag")
    keep = FilterRule("Bag of the Tinkerers", Match.EXACT, Action.KEEP)

    for rules in (ItemFilters([hide, keep]), ItemFilters([keep, hide])):
        assert rules.hidden("Large Bag")
        assert not rules.hidden("Bag of the Tinkerers")


def test_a_disabled_rule_stays_in_the_list_and_stops_applying() -> None:
    """"Is this the rule hiding my Fungi?" is answered by a toggle, not a retype."""
    rules = ItemFilters([FilterRule("fungus", enabled=False)])
    assert not rules.hidden("Fungus Covered Scale Tunic")
    assert len(rules) == 1


def test_match_kinds_do_what_they_say() -> None:
    assert ItemFilters([FilterRule("rusty", Match.PREFIX)]).hidden("Rusty Long Sword")
    assert not ItemFilters([FilterRule("rusty", Match.PREFIX)]).hidden("A Rusty Sword")
    assert ItemFilters([FilterRule("chips", Match.SUFFIX)]).hidden("Bone Chips")
    assert ItemFilters([FilterRule("Bone Chips", Match.EXACT)]).hidden("bone  chips")
    assert not ItemFilters([FilterRule("Bone Chips", Match.EXACT)]).hidden("Bone Chips Pouch")


def test_patterns_are_compared_the_way_item_names_are_written() -> None:
    """P99 punctuation is not something anyone should have to reproduce."""
    rules = ItemFilters([FilterRule("teirdal", Match.CONTAINS)])
    assert rules.hidden("Teir`Dal Chain Coif")
    assert ItemFilters([FilterRule("journeyman's boots", Match.EXACT)]).hidden("Journeymans Boots")


def test_the_reason_names_the_rule_that_did_it() -> None:
    """A filtered row has to be able to say which rule caught it."""
    rules = ItemFilters([FilterRule("cheap"), FilterRule("bag")])
    reason = rules.reason("Large Bag")
    assert reason is not None and reason.pattern == "bag"
    assert rules.reason("Cloak of Flames") is None


def test_an_exception_means_there_is_no_reason_to_report() -> None:
    rules = ItemFilters([FilterRule("bag"), FilterRule("tinkerers", action=Action.KEEP)])
    assert rules.reason("Bag of the Tinkerers") is None


def test_blank_and_duplicate_rules_are_refused() -> None:
    rules = ItemFilters()
    assert rules.add(FilterRule("bag")) is True
    assert rules.add(FilterRule("bag")) is False
    assert rules.add(FilterRule("  ")) is False
    # Same pattern, different match: a different rule, and a legitimate one.
    assert rules.add(FilterRule("bag", Match.EXACT)) is True
    assert len(rules) == 2


def test_removing_rules_by_position() -> None:
    rules = ItemFilters([FilterRule("a"), FilterRule("b"), FilterRule("c")])
    assert rules.remove([0, 2]) == 2
    assert [rule.pattern for rule in rules.rules()] == ["b"]
    assert rules.remove([99]) == 0


def test_apply_returns_what_survives() -> None:
    rules = ItemFilters([FilterRule("bone chips", Match.EXACT)])
    assert rules.apply(["Bone Chips", "Manastone"]) == ["Manastone"]


def test_rules_round_trip_through_storage() -> None:
    rules = ItemFilters(
        [FilterRule("bag"), FilterRule("Fungi", Match.EXACT, Action.KEEP, enabled=False)]
    )
    restored = ItemFilters.from_list(rules.to_list())
    assert [rule.identity for rule in restored.rules()] == [
        rule.identity for rule in rules.rules()
    ]
    assert restored.rules()[1].enabled is False


def test_malformed_storage_is_skipped_rather_than_fatal() -> None:
    restored = ItemFilters.from_list(
        [
            {"pattern": "bag", "match": "nonsense", "action": "nonsense"},
            {"pattern": "   "},
            "not a rule",
            42,
        ]
    )
    assert len(restored) == 1
    rule = restored.rules()[0]
    assert (rule.match, rule.action) == (Match.CONTAINS, Action.HIDE)


def test_the_suggested_rules_are_a_starting_point_not_a_default() -> None:
    """They must be inert until somebody adds them, and must actually catch
    the junk they claim to."""
    assert not ItemFilters().rules()
    rules = ItemFilters(list(SUGGESTED_RULES))
    assert rules.hidden("Bone Chips")
    assert rules.hidden("Rusty Long Sword")
    assert rules.hidden("Large Bag")
    assert not rules.hidden("Cloak of Flames")
    assert not rules.hidden("Bag of the Tinkerers")


def test_a_rule_describes_itself_in_words() -> None:
    described = FilterRule("bag", Match.CONTAINS, Action.HIDE).describe()
    assert "Hide" in described and "contains" in described and "bag" in described
