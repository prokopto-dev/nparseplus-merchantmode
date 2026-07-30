"""Finding which character is holding an item (Qt-free, stdlib only).

A buyer asks "do you have a Fungi?" and the answer has to arrive before they
wander off. The data was always there — :meth:`InventoryVault.holdings` knows
every item on every dumped character — but the only way in was
:meth:`InventoryVault.locate`, which compares whole case-folded strings and so
fails every question a human actually asks. Nobody types ``Fungus Covered Scale
Tunic``. They type ``fungi``, ``FCST``, or ``scale tunic``.

So the query walks the same kind of ladder :mod:`merchant_mode.matching` uses
for prices, widened at the bottom because this is a *search* and not a pricing
decision:

1. **Exact**, after normalizing case and punctuation.
2. **Resolved** — the :class:`~merchant_mode.matching.NameMatcher`'s answer,
   which brings nicknames, acronyms and near-misses with it.
3. **Substring**, in three flavours (prefix, word-start, anywhere) for the "I
   only remember part of it" case.

Where the pricing matcher refuses ambiguity, this one welcomes it: a search
offers a list and a human picks from it, so a wrong-but-plausible row costs a
glance. Ranking is what carries the weight here — an exact hit must never sit
below a substring hit — which is why the rung is kept on every match instead of
being thrown away once the list is built.

Deliberately *not* the same question as :meth:`MerchantModePlugin.search_items`,
which searches the 25k-name index of everything that exists. This searches what
you are holding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum

from .inventory import STALE_AFTER, Holding, humanize_age
from .matching import normalize

__all__ = ["HoldingMatch", "MatchKind", "find_holdings"]

MIN_SUBSTRING_LEN = 2
"""Below this, a substring rung matches most of your bags. Exact and resolved
still run on a one-character query — an acronym that short is a real thing."""


class MatchKind(IntEnum):
    """Which rung of the ladder answered. Lower sorts first."""

    EXACT = 0
    """The name, give or take case and punctuation."""
    RESOLVED = 1
    """A nickname, an acronym, or a close-enough spelling."""
    PREFIX = 2
    """The name starts with what was typed."""
    WORD = 3
    """A word inside the name starts with what was typed."""
    SUBSTRING = 4
    """It appears somewhere in the name."""

    @property
    def label(self) -> str:
        return {
            MatchKind.EXACT: "exact",
            MatchKind.RESOLVED: "nickname",
            MatchKind.PREFIX: "starts with",
            MatchKind.WORD: "word",
            MatchKind.SUBSTRING: "contains",
        }[self]


@dataclass(frozen=True)
class HoldingMatch:
    """One held item that answered the query, and how well."""

    holding: Holding
    kind: MatchKind

    @property
    def name(self) -> str:
        return self.holding.name

    @property
    def character(self) -> str:
        return self.holding.character

    @property
    def server(self) -> str:
        return self.holding.server

    @property
    def count(self) -> int:
        return self.holding.count

    @property
    def item_id(self) -> int:
        return self.holding.item_id

    def where(self, now: datetime | None = None) -> str:
        return self.holding.where(now)

    def is_stale(self, now: datetime, *, after: timedelta = STALE_AFTER) -> bool:
        return self.holding.is_stale(now, after=after)

    def age_text(self, now: datetime) -> str:
        return humanize_age(self.holding.age(now))


def find_holdings(
    query: str,
    holdings: list[Holding],
    *,
    matcher=None,
    limit: int = 100,
) -> list[HoldingMatch]:
    """Held items matching ``query``, best rung first.

    ``matcher`` is a :class:`~merchant_mode.matching.NameMatcher`. Without one
    the nickname rung is simply skipped — the other three still work, so a
    caller that has no matcher yet degrades to a plain substring search rather
    than to nothing.

    Every holding of a matching name is returned, not just the first: the same
    item sitting on two mules is the answer to "do you have one", not a
    duplicate to be collapsed. Ties inside a rung break on name length then
    alphabetically, so the list doesn't reshuffle between keystrokes.
    """
    key = normalize(query)
    if not key or not holdings:
        return []

    resolved_key = ""
    if matcher is not None:
        resolved = matcher.resolve(query)
        if resolved is not None:
            resolved_key = normalize(resolved)

    # Rank once per distinct name and normalize once per holding. Both are
    # regex work, this runs on every keystroke, and an account with a few
    # thousand items would otherwise pay for it inside the sort comparator.
    ranked: dict[str, MatchKind | None] = {}
    scored: list[tuple[MatchKind, int, str, str, str, Holding]] = []
    for holding in holdings:
        stored = normalize(holding.name)
        if not stored:
            continue
        if stored not in ranked:
            ranked[stored] = _rank(stored, key, resolved_key)
        kind = ranked[stored]
        if kind is None:
            continue
        scored.append(
            (
                kind,
                len(stored),
                stored,
                holding.character.casefold(),
                holding.item.location.casefold(),
                holding,
            )
        )

    scored.sort(key=lambda row: row[:5])
    return [HoldingMatch(holding=row[5], kind=row[0]) for row in scored[:limit]]


def _rank(stored: str, key: str, resolved_key: str) -> MatchKind | None:
    """Which rung ``stored`` answers ``key`` on, or ``None`` for no match.

    Both arguments are already normalized. ``resolved_key`` is the matcher's
    answer for the query, or ``""`` when it had none.
    """
    if stored == key:
        return MatchKind.EXACT
    if resolved_key and stored == resolved_key:
        return MatchKind.RESOLVED
    if len(key) < MIN_SUBSTRING_LEN:
        return None
    if stored.startswith(key):
        return MatchKind.PREFIX
    if f" {key}" in stored:
        return MatchKind.WORD
    if key in stored:
        return MatchKind.SUBSTRING
    return None
