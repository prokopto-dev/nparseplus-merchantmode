"""Rules for the items you never sell (Qt-free, stdlib only).

Most of what an ``/outputfile inventory`` dump contains is not merchandise. A
main comes back from a night out holding food, drink, bone chips, the four
starting daggers every toon rolls with, and a rack of 4-slot merchant bags —
and every one of those rows sits in the Sell tab between the two items you
actually want to advertise. Deleting them is not the answer either: they come
straight back the next time that character dumps.

So the answer is a rule, kept once and applied to every dump forever. The whole
model is three fields — what to look for, how to look for it, and whether the
hit is hidden or spared:

* :class:`Match` is how the pattern is compared. ``CONTAINS`` is the workhorse;
  ``EXACT`` is what a "filter out exactly this" gesture produces.
* :class:`Action` is ``HIDE`` or ``KEEP``, **and KEEP always wins**.

That last rule is the one that makes this usable for the case that motivated
it. *Hide anything containing "bag", keep "Bag of the Tinkerers"* is two rules
and needs no thought about which order they sit in — which matters, because a
filter list that silently depends on ordering is one you have to debug rather
than write.

Nothing here ever deletes anything. A hidden item is still in the vault, still
in the dump, and still findable by turning the filter off; the count of what is
currently hidden is reported everywhere a filtered list is shown, because a
list quietly missing rows is worse than clutter.

Comparison goes through :func:`~merchant_mode.matching.normalize`, so a rule
written ``teir'dal`` matches ``Teir\\`Dal`` and nobody has to guess which
apostrophe the game used.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .matching import normalize

__all__ = ["SUGGESTED_RULES", "Action", "FilterRule", "ItemFilters", "Match"]


class Match(StrEnum):
    """How a pattern is compared against an item name."""

    CONTAINS = "contains"
    EXACT = "exact"
    PREFIX = "prefix"
    SUFFIX = "suffix"

    @property
    def label(self) -> str:
        return {
            Match.CONTAINS: "contains",
            Match.EXACT: "is exactly",
            Match.PREFIX: "starts with",
            Match.SUFFIX: "ends with",
        }[self]

    def hits(self, name_key: str, pattern_key: str) -> bool:
        """Whether ``name_key`` answers ``pattern_key``. Both normalized."""
        if not pattern_key or not name_key:
            return False
        if self is Match.EXACT:
            return name_key == pattern_key
        if self is Match.PREFIX:
            return name_key.startswith(pattern_key)
        if self is Match.SUFFIX:
            return name_key.endswith(pattern_key)
        return pattern_key in name_key


class Action(StrEnum):
    """What a matching rule does."""

    HIDE = "hide"
    KEEP = "keep"
    """An exception. Beats every ``HIDE`` rule, whatever the order."""

    @property
    def label(self) -> str:
        return "Hide" if self is Action.HIDE else "Keep"


def _as_enum(enum_type, value, fallback):
    try:
        return enum_type(str(value).strip().casefold())
    except ValueError:
        return fallback


@dataclass(frozen=True)
class FilterRule:
    """One line of the filter list."""

    pattern: str
    match: Match = Match.CONTAINS
    action: Action = Action.HIDE
    enabled: bool = True
    """Off keeps the rule in the list without applying it — the honest way to
    answer "is this rule the one hiding my Fungi?" without retyping it."""

    @property
    def key(self) -> str:
        """Normalized pattern, or ``""`` for a rule that can never match."""
        return normalize(self.pattern)

    @property
    def identity(self) -> tuple[str, str, str]:
        """What makes two rules the same rule, for de-duplication."""
        return (self.key, str(self.match), str(self.action))

    def hits(self, name_key: str) -> bool:
        """Whether this rule applies to an already-normalized name."""
        return self.enabled and self.match.hits(name_key, self.key)

    def describe(self) -> str:
        return f"{self.action.label} items where the name {self.match.label} “{self.pattern}”"

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "match": str(self.match),
            "action": str(self.action),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: object) -> FilterRule | None:
        """Rebuild from storage; ``None`` for anything that can't match."""
        if not isinstance(data, dict):
            return None
        pattern = str(data.get("pattern", "")).strip()
        if not pattern or not normalize(pattern):
            return None
        return cls(
            pattern=pattern,
            match=_as_enum(Match, data.get("match"), Match.CONTAINS),
            action=_as_enum(Action, data.get("action"), Action.HIDE),
            enabled=bool(data.get("enabled", True)),
        )


SUGGESTED_RULES: tuple[FilterRule, ...] = (
    FilterRule("Bone Chips", Match.EXACT),
    FilterRule("Rusty", Match.PREFIX),
    FilterRule("Cloth Cap", Match.EXACT),
    FilterRule("Backpack", Match.EXACT),
    FilterRule("Small Bag", Match.EXACT),
    FilterRule("Large Bag", Match.EXACT),
    FilterRule("Water Flask", Match.EXACT),
    FilterRule("Bread Cake", Match.EXACT),
    FilterRule("Fish Scales", Match.EXACT),
)
"""A starting point, offered by a button and never applied on its own.

Merchant-bought bags, the newbie-armour-quest leavings, and the food and drink
every character carries — the things that are junk on every server and in every
bag. Deliberately short and deliberately all removable: a filter list somebody
else wrote is one you can't trust, so this is a head start rather than a
default.
"""


class ItemFilters:
    """An ordered, user-built list of rules, applied to item names.

    Account-wide rather than per-server, unlike almost everything else in this
    plugin. Prices, inventories and listings are split by server because a Blue
    item cannot be sold to a Green buyer — but "Bone Chips are not merchandise"
    is a fact about the item, not about a market, and making the user rebuild
    the same junk list on every server would be busywork with nothing behind it.
    """

    def __init__(self, rules: list[FilterRule] | None = None) -> None:
        self._rules: list[FilterRule] = [rule for rule in (rules or ()) if rule.key]

    def rules(self) -> list[FilterRule]:
        return list(self._rules)

    def add(self, rule: FilterRule) -> bool:
        """Append a rule. ``False`` when it's blank or already in the list."""
        if not rule.key:
            return False
        if any(existing.identity == rule.identity for existing in self._rules):
            return False
        self._rules.append(rule)
        return True

    def extend(self, rules: list[FilterRule]) -> int:
        """Add several, skipping blanks and duplicates. Returns how many landed."""
        return sum(1 for rule in rules if self.add(rule))

    def remove(self, indices: list[int] | set[int]) -> int:
        """Drop rules by position. Returns how many went."""
        drop = {index for index in indices if 0 <= index < len(self._rules)}
        if not drop:
            return 0
        self._rules = [rule for index, rule in enumerate(self._rules) if index not in drop]
        return len(drop)

    def replace(self, rules: list[FilterRule]) -> None:
        self._rules = [rule for rule in rules if rule.key]

    def matching(self, name: str) -> list[FilterRule]:
        """Every enabled rule that applies to ``name``, in list order."""
        key = normalize(name)
        if not key:
            return []
        return [rule for rule in self._rules if rule.hits(key)]

    def reason(self, name: str) -> FilterRule | None:
        """The rule hiding ``name``, or ``None`` when nothing hides it.

        A ``KEEP`` match ends the question — the caller is being told *why*
        something is hidden, and "it isn't" is the answer whenever an exception
        applies, regardless of how many ``HIDE`` rules also matched.
        """
        hidden: FilterRule | None = None
        for rule in self.matching(name):
            if rule.action is Action.KEEP:
                return None
            if hidden is None:
                hidden = rule
        return hidden

    def hidden(self, name: str) -> bool:
        return self.reason(name) is not None

    def apply(self, names: list[str]) -> list[str]:
        return [name for name in names if not self.hidden(name)]

    def to_list(self) -> list[dict]:
        return [rule.to_dict() for rule in self._rules]

    @classmethod
    def from_list(cls, data: object) -> ItemFilters:
        """Rebuild from storage, skipping anything malformed."""
        rules: list[FilterRule] = []
        if isinstance(data, list):
            for row in data:
                rule = FilterRule.from_dict(row)
                if rule is not None:
                    rules.append(rule)
        return cls(rules)

    def __len__(self) -> int:
        return len(self._rules)

    def __bool__(self) -> bool:
        return bool(self._rules)
