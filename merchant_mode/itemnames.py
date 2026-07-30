"""The universe of item names you can search (Qt-free, stdlib only).

PigParse has no search endpoint — ``api/item/postmultiple`` takes a list of
*exact* names and gives back stats for the ones it recognises. So searching for
an item is a local problem: find the name first, then ask about that name.

Three sources feed the index, best-effort and in order, because no one of them
is enough on its own:

1. **The host's master item list.** ``nparseplus/data/items/master_item_list.txt``
   is ~25k P99 item names. Read in place when the host is importable.
2. **A vendored copy** of the same file, under ``merchant_mode/data/``. Nothing
   in the host actually reads its copy, so it is not a contract and could move;
   the vendored copy is what makes reaching for the host's version safe.
3. **Names the plugin already knows** — your dumps, anything seen in ``/auc``,
   anything PigParse has answered about. These are the only source that covers
   items added after this list was cut.

Free text always wins over all of it: if you type a name the index has never
heard of, it still goes to PigParse. The index is there to save typing and
catch typos, not to gatekeep.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["ItemNameIndex", "load_master_names", "normalize"]

MASTER_FILENAME = "master_item_list.txt"

FUZZY_CUTOFF = 0.72
"""How close a fuzzy match has to be. Tuned to catch a transposed letter or a
missing space without offering unrelated items — a wrong suggestion the user
accepts is worse than no suggestion, because it silently prices another item."""

FUZZY_CANDIDATES = 4000
"""Ceiling on names fed to difflib in one search. ``SequenceMatcher`` is far
too slow to run over 25k names per keystroke, so fuzzy matching only ever sees
a prefiltered slice — and only when substring matching found nothing at all."""


def normalize(name: str) -> str:
    """Comparison key: trimmed, case-folded, inner whitespace collapsed."""
    return " ".join(str(name).split()).casefold()


def _split_names(text: str) -> list[str]:
    """The file is one enormous comma-separated line; be tolerant anyway."""
    return [
        cleaned
        for line in text.splitlines()
        for chunk in line.split(",")
        if (cleaned := chunk.strip())
    ]


def _read_names(path: Path) -> list[str]:
    try:
        return _split_names(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def _host_master_path() -> Path | None:
    """The host's own copy, located the way the host locates its data files.

    Mirrors ``nparseplus.core.zones``' ``resources.files("nparseplus") /
    "data" / ...``. Returns ``None`` whenever the host isn't installed, which
    is the normal case for CI and the validator.
    """
    try:
        from importlib import resources

        path = Path(str(resources.files("nparseplus") / "data" / "items" / MASTER_FILENAME))
    except (ImportError, ModuleNotFoundError, TypeError, OSError):
        return None
    return path if path.is_file() else None


def _vendored_master_path() -> Path | None:
    path = Path(__file__).parent / "data" / MASTER_FILENAME
    return path if path.is_file() else None


def load_master_names() -> tuple[list[str], str]:
    """Every known P99 item name, and a label for where they came from.

    Falls through host -> vendored -> nothing. The label is shown in the UI:
    a search that quietly covers 200 names instead of 25,000 should say so.
    """
    for path, source in (
        (_host_master_path(), "host item list"),
        (_vendored_master_path(), "bundled item list"),
    ):
        if path is None:
            continue
        names = _read_names(path)
        if names:
            return names, source
        logger.warning("item list at %s was unreadable or empty", path)
    return [], "no item list"


class ItemNameIndex:
    """Searchable set of item names, deduped case-insensitively.

    The first spelling of a name wins, so a name learned from your own dump
    keeps the game's capitalisation even if PigParse later reports it
    differently.
    """

    def __init__(self, names: Iterable[str] = (), *, source: str = "") -> None:
        self._by_key: dict[str, str] = {}
        self.source = source
        self.add(names)

    @classmethod
    def from_master(cls) -> ItemNameIndex:
        names, source = load_master_names()
        return cls(names, source=source)

    def add(self, names: Iterable[str]) -> int:
        """Merge names in. Returns how many were new."""
        added = 0
        for name in names:
            cleaned = " ".join(str(name).split())
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key not in self._by_key:
                self._by_key[key] = cleaned
                added += 1
        return added

    def canonical(self, name: str) -> str | None:
        """The indexed spelling of ``name``, if it's known."""
        return self._by_key.get(normalize(name))

    def search(self, query: str, *, limit: int = 50) -> list[str]:
        """Names matching ``query``, best first.

        Ranked exact, then prefix, then word-start, then anywhere in the name;
        ties break alphabetically so the order doesn't jitter between
        keystrokes. Fuzzy matching is a last resort — it only runs when nothing
        matched literally, so a real substring hit is never pushed down the
        list by a closer-looking typo correction.
        """
        key = normalize(query)
        if not key:
            return []

        ranked: list[tuple[int, str, str]] = []
        for stored, display in self._by_key.items():
            if stored == key:
                rank = 0
            elif stored.startswith(key):
                rank = 1
            elif f" {key}" in stored:
                rank = 2
            elif key in stored:
                rank = 3
            else:
                continue
            ranked.append((rank, stored, display))

        if ranked:
            ranked.sort(key=lambda row: (row[0], len(row[1]), row[1]))
            return [display for _rank, _stored, display in ranked[:limit]]
        return self._fuzzy(key, limit=limit)

    def _fuzzy(self, key: str, *, limit: int) -> list[str]:
        """Typo tolerance, over a prefiltered slice for the sake of speed."""
        span = range(max(1, len(key) - 4), len(key) + 6)
        candidates = [
            stored
            for stored in self._by_key
            if len(stored) in span and stored[:1] == key[:1]
        ]
        if not candidates:
            candidates = [stored for stored in self._by_key if len(stored) in span]
        matches = difflib.get_close_matches(
            key, candidates[:FUZZY_CANDIDATES], n=limit, cutoff=FUZZY_CUTOFF
        )
        return [self._by_key[stored] for stored in matches]

    def names(self) -> list[str]:
        return list(self._by_key.values())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and normalize(name) in self._by_key

    def __len__(self) -> int:
        return len(self._by_key)
