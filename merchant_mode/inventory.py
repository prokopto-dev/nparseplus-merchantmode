"""Parsing ``/outputfile inventory`` dumps (Qt-free, stdlib only).

The in-game command writes a tab-separated file with one header line::

    Location	Name	ID	Count	Slots

This is the feature everything else rests on, because **the dump carries the
item ID**. For anything you own, forging a link needs no wiki, no external
database, and no guessing — the IDs are authoritative and come straight from
the game client. That is what makes the generated macros trustworthy.

nParse+'s own ``nparseplus/core/inventory.py`` is a reference implementation,
not public API, so this is an independent parser. It matches that one's proven
handling (strict-but-partial header check, silent skip of malformed rows,
hyphen-stripped location keys) and adds only what selling needs: keeping the
raw location string for bag grouping, and dropping unsellable rows.

Since nParse+ 2.1.0 the host watches the EQ directory itself and files every
dump away as a JSON snapshot. Those snapshots say exactly what the tab-separated
file said, so :func:`parse_snapshot_file` reads one into the same
:class:`InventoryItem` rows rather than growing a second model — see
:data:`SNAPSHOT_SCHEMA_VERSION` for what "exactly" is pinned against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

HEADER = ("Location", "Name", "ID")
"""First three header columns. ``Count`` and ``Slots`` are positional only —
the reference parser doesn't name-check them either."""

COLUMNS = 5

_UNSELLABLE_NAMES = frozenset({"", "empty"})


@dataclass(frozen=True)
class InventoryItem:
    """One row of a dump."""

    location: str
    """Raw location as written, e.g. ``Charm`` or ``General1-Slot1``."""
    name: str
    item_id: int
    count: int
    slots: int
    """Container capacity; 0 for anything that isn't a bag."""

    @property
    def location_key(self) -> str:
        """Normalized location, matching how the host resolves these."""
        return self.location.replace("-", "").casefold()

    @property
    def container(self) -> str:
        """The bag this sits in (``General1`` for ``General1-Slot1``), or ``""``."""
        head, sep, _ = self.location.partition("-")
        return head if sep else ""

    @property
    def is_container(self) -> bool:
        return self.slots > 0


def parse_inventory_text(text: str) -> list[InventoryItem]:
    """Parse a dump into items. Returns ``[]`` for anything that isn't one.

    Malformed rows are skipped rather than raising: a dump is written by the
    game mid-session and a single odd row should never cost the user the other
    two hundred.
    """
    lines = text.splitlines()
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    if len(header) < COLUMNS or tuple(header[:3]) != HEADER:
        return []

    items: list[InventoryItem] = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < COLUMNS:
            continue
        try:
            item_id, count, slots = int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            continue
        items.append(
            InventoryItem(
                location=parts[0].strip(),
                name=parts[1].strip(),
                item_id=item_id,
                count=count,
                slots=slots,
            )
        )
    return items


def sellable(items: list[InventoryItem]) -> list[InventoryItem]:
    """Drop rows that can't become a link.

    An id of 0 or a placeholder name would encode to a valid-looking body
    pointing at nothing. The EQ client appears to omit empty slots from the
    dump entirely — the reference parser has no such filter and there is no
    ``Empty`` sentinel anywhere in its tests — but the check costs nothing and
    the failure it prevents is silent.
    """
    return [
        item for item in items if item.item_id > 0 and item.name.casefold() not in _UNSELLABLE_NAMES
    ]


def parse_inventory_file(path: Path | str, *, encoding: str = "utf-8") -> list[InventoryItem]:
    """Read and parse a dump. Missing or unreadable file yields ``[]``."""
    try:
        text = Path(path).read_text(encoding=encoding, errors="replace")
    except OSError:
        return []
    return parse_inventory_text(text)


# --------------------------------------------------------------------------- #
# The host's stored snapshots
# --------------------------------------------------------------------------- #
SNAPSHOT_SCHEMA_VERSION = 1
"""The ``CharacterDump`` layout this reader was written against (nParse+ 2.1.0).

A document stamped higher was written by a newer host, where the same field
names may not mean the same things, so it is left alone rather than half-read.
The host's own loader refuses it for the same reason — and the failure this
plugin designs against is a confident mistake, not a miss: a bag slot read out
of a shape that changed is worse than no row at all, and the *Load inventory
dump…* button still reads the game's file directly.
"""

SNAPSHOT_INVENTORY = "inventory"
"""``DumpKind.INVENTORY``'s wire value. The other kind is a spellbook, and
there is nothing in a spellbook to sell."""


def parse_snapshot_document(data: object) -> list[InventoryItem]:
    """Items out of one stored ``/outputfile inventory`` snapshot.

    The host's dump library is not plugin API — ``PluginContext`` exposes no
    dumps — so what a plugin gets is a path on an event and the stdlib to read
    it with. The document is a JSON object carrying ``schema_version``, the
    character's identity and ``items[]`` rows of ``location_name, name,
    item_id, count, slots``: the same five fields the tab-separated dump has,
    which is why this maps onto :class:`InventoryItem` instead of introducing a
    second model of an inventory.

    Malformed rows are skipped rather than raising, for the same reason
    :func:`parse_inventory_text` skips them: one odd row must not cost the user
    the other two hundred.
    """
    if not isinstance(data, dict):
        return []
    try:
        version = int(data.get("schema_version", SNAPSHOT_SCHEMA_VERSION))
    except (TypeError, ValueError):
        return []
    if version > SNAPSHOT_SCHEMA_VERSION:
        return []

    items: list[InventoryItem] = []
    for row in data.get("items") or ():
        if not isinstance(row, dict):
            continue
        try:
            items.append(
                InventoryItem(
                    # ``location_name`` is the dump file's own label, dash and
                    # all (``General1-Slot1``). Its sibling ``location`` is a
                    # wire ordinal that cannot spell one and flattens anything
                    # the client's enum never had to ``Unknown``.
                    location=str(row.get("location_name", "")).strip(),
                    name=str(row["name"]).strip(),
                    item_id=int(row["item_id"]),
                    count=int(row.get("count", 1)),
                    slots=int(row.get("slots", 0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return items


def parse_snapshot_file(path: Path | str, *, encoding: str = "utf-8") -> list[InventoryItem]:
    """Read one stored snapshot. Missing, unreadable or not JSON yields ``[]``."""
    try:
        text = Path(path).read_text(encoding=encoding, errors="replace")
    except OSError:
        return []
    try:
        return parse_snapshot_document(json.loads(text))
    except ValueError:
        return []


def character_from_filename(path: Path | str) -> str:
    """Guess the character name from ``<Character>-Inventory.txt``.

    Only a fallback for when the dump is loaded while a different character is
    logged in (or none is) — the active player is the better answer when it's
    available.
    """
    stem = Path(path).stem
    head, sep, tail = stem.partition("-")
    if sep and tail.casefold().startswith("inventory"):
        return head.strip()
    return stem.strip()


# --------------------------------------------------------------------------- #
# Across characters
# --------------------------------------------------------------------------- #
STALE_AFTER = timedelta(days=7)
"""How old a dump gets before its locations deserve a warning, by default.

Seven days is right for a mule that never moves and far too generous for a
main, so it's a setting — every function here takes an ``after`` override and
this is only the fallback.

Nothing enforces it either way. A dump is a photograph, and the plugin has no
way to know you moved something an hour later. Showing the age is the honest
option; silently presenting week-old bag slots as fact is not.
"""


def humanize_age(age: timedelta) -> str:
    """A rough age a human scans rather than reads: ``4h``, ``3d``, ``6w``.

    Deliberately coarse and deliberately never zero — "0h old" reads like a
    bug, and the question this answers ("can I still trust this bag slot?")
    never turns on the difference between fifty and seventy minutes.
    """
    days = max(0, age.days)
    if days >= 14:
        return f"{days // 7}w"
    if days >= 1:
        return f"{days}d"
    return f"{max(1, age.seconds // 3600)}h"


ORIGIN_MANUAL = "manual"
"""A dump the user went and fetched: the file dialog, or a reload of it."""

ORIGIN_HOST = "host"
"""A dump nParse+ noticed and handed over, with nobody asking.

Worth recording because a row that appeared on its own is a row the reader
never chose, and an unexplained row is one they have no reason to trust. The
Dumps tab names the origin of every dump it lists.
"""

_ORIGIN_LABELS = {ORIGIN_MANUAL: "By hand", ORIGIN_HOST: "Automatic"}


def origin_label(origin: str) -> str:
    """How a dump got here, in the two words the Dumps tab has room for.

    An origin this build has never heard of — a store written by a later
    version and opened by this one — reads as unknown rather than being
    rounded down to "by hand", which would be a claim nothing supports.
    """
    return _ORIGIN_LABELS.get(origin) or "Unknown"


@dataclass(frozen=True)
class CharacterInventory:
    """One character's dump, and when it was taken."""

    character: str
    server: str
    captured_at: datetime
    items: list[InventoryItem] = field(default_factory=list)
    source_path: str = ""
    """The file it was read from, so it can be reloaded without a dialog.

    Empty for anything restored from v3 storage or loaded from text directly.
    Kept as a string rather than a ``Path`` because it round-trips through JSON
    and may well name a file that no longer exists.
    """
    origin: str = ORIGIN_MANUAL
    """Which way it arrived — :data:`ORIGIN_MANUAL` or :data:`ORIGIN_HOST`."""

    @property
    def key(self) -> str:
        return inventory_key(self.character, self.server)

    def age(self, now: datetime) -> timedelta:
        return now - self.captured_at

    def is_stale(self, now: datetime, *, after: timedelta = STALE_AFTER) -> bool:
        return self.age(now) > after

    def age_text(self, now: datetime) -> str:
        return humanize_age(self.age(now))


@dataclass(frozen=True)
class Holding:
    """An item, and which character is holding it where."""

    item: InventoryItem
    character: str
    server: str
    captured_at: datetime

    @property
    def name(self) -> str:
        return self.item.name

    @property
    def item_id(self) -> int:
        return self.item.item_id

    @property
    def count(self) -> int:
        return self.item.count

    def age(self, now: datetime) -> timedelta:
        return now - self.captured_at

    def is_stale(self, now: datetime, *, after: timedelta = STALE_AFTER) -> bool:
        return self.age(now) > after

    def where(self, now: datetime | None = None, *, after: timedelta = STALE_AFTER) -> str:
        """``Xantik · General1-Slot1``, with an age when the dump is stale."""
        base = f"{self.character} · {self.item.location}"
        if now is None:
            return base
        age = self.age(now)
        if age > after:
            return f"{base} ({humanize_age(age)} old)"
        return base


def inventory_key(character: str, server: str) -> str:
    return f"{character.strip().casefold()}@{server.strip().casefold()}"


class InventoryVault:
    """Every character's most recent dump, kept together.

    One merchant usually advertises for a whole account, so the sellable pool
    is the union of every character's dump — not just whoever happens to be
    logged in. Keeping them all is also what lets the UI answer the question
    that actually matters when a buyer says yes: *which character is holding
    this, and how long ago did I check?*
    """

    def __init__(self) -> None:
        self._by_key: dict[str, CharacterInventory] = {}

    def put(
        self,
        character: str,
        server: str,
        items: list[InventoryItem],
        *,
        captured_at: datetime,
        source_path: str = "",
        origin: str = ORIGIN_MANUAL,
    ) -> CharacterInventory:
        """Record (or replace) one character's dump."""
        record = CharacterInventory(
            character=character.strip() or "Unknown",
            server=server.strip(),
            captured_at=captured_at,
            items=list(items),
            source_path=source_path,
            origin=origin,
        )
        self._by_key[record.key] = record
        return record

    def get(self, character: str, server: str) -> CharacterInventory | None:
        return self._by_key.get(inventory_key(character, server))

    def drop(self, character: str, server: str) -> None:
        self._by_key.pop(inventory_key(character, server), None)

    def remove_items(self, character: str, server: str, rows: set[tuple[str, int]]) -> int:
        """Drop individual rows from one character's dump. Returns how many.

        ``rows`` are ``(location_key, item_id)`` pairs — the location is what
        distinguishes two stacks of the same item in different bags, and the id
        is what stops a renamed row being matched by position alone.

        This edits the *record*, not the file. A dump is a photograph and this
        is cropping it: the next reload re-reads the file and the rows come
        back, which is the honest behaviour and the reason
        :mod:`merchant_mode.filters` exists for anything you want gone for good.
        """
        record = self._by_key.get(inventory_key(character, server))
        if record is None or not rows:
            return 0
        kept = [item for item in record.items if (item.location_key, item.item_id) not in rows]
        removed = len(record.items) - len(kept)
        if removed:
            self._by_key[record.key] = CharacterInventory(
                character=record.character,
                server=record.server,
                captured_at=record.captured_at,
                items=kept,
                source_path=record.source_path,
                origin=record.origin,
            )
        return removed

    def characters(self) -> list[CharacterInventory]:
        """Every recorded dump, most recently captured first."""
        return sorted(self._by_key.values(), key=lambda r: r.captured_at, reverse=True)

    def holdings(self) -> list[Holding]:
        """Every sellable item across every character."""
        found: list[Holding] = []
        for record in self.characters():
            for item in record.items:
                found.append(
                    Holding(
                        item=item,
                        character=record.character,
                        server=record.server,
                        captured_at=record.captured_at,
                    )
                )
        return found

    def locate(self, name: str) -> list[Holding]:
        """Everywhere ``name`` is held — the same item can sit on two alts."""
        key = name.strip().casefold()
        return [holding for holding in self.holdings() if holding.name.casefold() == key]

    def to_dict(self) -> dict:
        return {
            key: {
                "character": record.character,
                "server": record.server,
                "captured_at": record.captured_at.isoformat(),
                "source_path": record.source_path,
                "origin": record.origin,
                "items": [
                    {
                        "location": item.location,
                        "name": item.name,
                        "item_id": item.item_id,
                        "count": item.count,
                        "slots": item.slots,
                    }
                    for item in record.items
                ],
            }
            for key, record in self._by_key.items()
        }

    @classmethod
    def from_dict(cls, data: object) -> InventoryVault:
        """Rebuild from storage, skipping anything malformed."""
        vault = cls()
        if not isinstance(data, dict):
            return vault
        for key, raw in data.items():
            if not isinstance(raw, dict):
                continue
            try:
                captured_at = datetime.fromisoformat(str(raw["captured_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            items: list[InventoryItem] = []
            for row in raw.get("items", []) or []:
                if not isinstance(row, dict):
                    continue
                try:
                    items.append(
                        InventoryItem(
                            location=str(row.get("location", "")),
                            name=str(row["name"]),
                            item_id=int(row["item_id"]),
                            count=int(row.get("count", 1)),
                            slots=int(row.get("slots", 0)),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            vault._by_key[str(key)] = CharacterInventory(
                character=str(raw.get("character", "Unknown")),
                server=str(raw.get("server", "")),
                captured_at=captured_at,
                items=items,
                source_path=str(raw.get("source_path", "") or ""),
                # A store written before the host had a dump library has no
                # origin, and there is no guesswork in reading it as by hand:
                # the file dialog was the only way a dump could get in.
                origin=str(raw.get("origin", "") or ORIGIN_MANUAL),
            )
        return vault

    def __len__(self) -> int:
        return len(self._by_key)
