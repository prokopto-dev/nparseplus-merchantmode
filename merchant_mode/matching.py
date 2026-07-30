"""Matching what people type in ``/auction`` to what's in your bags.

Qt-free, stdlib only.

The channel does not write item names the way the game does. Your dump says
``Fungus Covered Scale Tunic``; the seller types ``fungi``, ``Fungus Tunic``,
``FCST``, or ``fungus covered scale tunic`` with a stray double space. Matching
those by exact case-folded equality — which is what this plugin did — means the
observed-price path essentially never fires for real inventory, and "Fill
prices" looks broken even when the channel has been talking about your item all
evening.

So resolution is a ladder, cheapest and safest first:

1. **Exact**, after normalizing case, whitespace, and P99's punctuation habits
   (``Teir\\`Dal``, ``Journeyman's``).
2. **Alias** — the user's own nickname table, read backwards, plus acronyms
   derived from the canonical name. Both are *only* accepted when they resolve
   to exactly one item.
3. **Fuzzy**, via :mod:`difflib`, against the canonical set only.

The ladder stops at the first rung that answers, and every rung refuses
ambiguity rather than picking a winner. A wrong match here doesn't look wrong
on screen — it just prices the wrong item — so the failure mode to design
against is a confident mistake, not a miss.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from functools import lru_cache

from .nicknames import NicknameTable

__all__ = ["NameMatcher", "acronym", "normalize"]

FUZZY_CUTOFF = 0.86
"""Deliberately stricter than the search box's. A search offers you a list to
pick from; this decides on its own which item a price attaches to."""

MIN_FUZZY_LEN = 5
"""Below this, fuzzy matching is noise — three- and four-letter names differ by
one edit from far too many others."""

# P99 names are full of ``Teir`Dal`` and ``Journeyman's``; the apostrophe forms
# vary by who is typing. Dropping them entirely beats trying to normalize them.
_APOSTROPHES = re.compile(r"[`'’]")
_NOT_WORD = re.compile(r"[^a-z0-9]+")

_NOISE_WORDS = frozenset({"a", "an", "the", "of", "and"})
"""Skipped when building acronyms — ``Cloak of Flames`` is ``CoF``, not
``COF``-with-an-O. Not stripped from names themselves, where they're load
bearing (``Staff of the Wheel`` vs ``Staff of Wheel``)."""


def normalize(name: str) -> str:
    """Aggressive comparison key: case, whitespace and punctuation removed.

    ``Journeyman's Boots`` and ``journeymans  boots`` both become
    ``journeymans boots``.
    """
    lowered = _APOSTROPHES.sub("", str(name).casefold())
    return " ".join(_NOT_WORD.sub(" ", lowered).split())


def acronym(name: str) -> str:
    """Initials of the significant words: ``Cloak of Flames`` -> ``cof``.

    Includes the noise words' initials only when skipping them would leave
    fewer than two letters, so ``Staff of the Wheel`` stays ``sw`` but ``Of
    Ages`` doesn't collapse to ``a``.
    """
    words = normalize(name).split()
    if not words:
        return ""
    significant = [word for word in words if word not in _NOISE_WORDS]
    if len(significant) < 2:
        significant = words
    return "".join(word[0] for word in significant)


class NameMatcher:
    """Resolves free text to one of a fixed set of canonical item names.

    Build one per set of canonical names — the items you own plus the ones you
    want. It is immutable and safe to cache; rebuild it when that set changes.
    """

    def __init__(
        self,
        canonical: Iterable[str],
        *,
        nicknames: NicknameTable | None = None,
    ) -> None:
        self._canonical: dict[str, str] = {}
        for name in canonical:
            key = normalize(name)
            if key:
                self._canonical.setdefault(key, " ".join(str(name).split()))

        # Aliases map to None once two different items claim them. Keeping the
        # collision rather than dropping the entry is what makes ambiguity
        # visible to :meth:`resolve` instead of order-dependent.
        self._aliases: dict[str, str | None] = {}
        for key, display in self._canonical.items():
            self._claim(acronym(display), key)
        if nicknames is not None:
            for name, nickname in nicknames.to_dict().items():
                key = normalize(name)
                if key in self._canonical:
                    self._claim(normalize(nickname), key)

        self._resolve_cached = lru_cache(maxsize=512)(self._resolve)

    def _claim(self, alias: str, key: str) -> None:
        if not alias or alias in self._canonical:
            return  # never let an alias shadow a real item name
        if alias in self._aliases and self._aliases[alias] != key:
            self._aliases[alias] = None
            return
        self._aliases[alias] = key

    def resolve(self, text: str) -> str | None:
        """The canonical name ``text`` refers to, or ``None`` if unclear."""
        return self._resolve_cached(normalize(text))

    def _resolve(self, key: str) -> str | None:
        if not key:
            return None
        if key in self._canonical:
            return self._canonical[key]
        alias = self._aliases.get(key)
        if alias is not None:
            return self._canonical[alias]
        if len(key) < MIN_FUZZY_LEN:
            return None
        close = difflib.get_close_matches(key, self._canonical, n=2, cutoff=FUZZY_CUTOFF)
        if len(close) == 1:
            return self._canonical[close[0]]
        # Two candidates are only ambiguous if they're genuinely comparable;
        # a clear winner over a distant runner-up is still a match.
        if len(close) == 2:
            scores = [difflib.SequenceMatcher(None, key, name).ratio() for name in close]
            if scores[0] - scores[1] >= 0.05:
                return self._canonical[close[0]]
        return None

    def same(self, left: str, right: str) -> bool:
        """Whether two spellings name the same item.

        Falls back to plain normalized equality when neither side resolves, so
        two identical unknown names still match each other.
        """
        left_key, right_key = normalize(left), normalize(right)
        if left_key == right_key:
            return True
        resolved_left = self.resolve(left_key)
        resolved_right = self.resolve(right_key)
        if resolved_left is None or resolved_right is None:
            return False
        return resolved_left == resolved_right

    def canonical_names(self) -> list[str]:
        return list(self._canonical.values())

    def __len__(self) -> int:
        return len(self._canonical)
