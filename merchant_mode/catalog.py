"""Item-id provenance (Qt-free, stdlib only).

For anything you own the id is settled — it came out of your own inventory
dump, written by the game. For a WTB list it isn't: those items are somewhere
else, and the id has to come from PigParse's ``eq_item_id``.

That gap matters more than it looks, because **a wrong id fails silently**. The
link still displays whatever text you typed; it only misbehaves when somebody
clicks it. The classic-era databases and P99 genuinely disagree on some names
— id 2735 is "Fungus Covered Scale Tunic" live but "Fungus Covered Scale Shirt"
in the alkabor dump — so an id that looks right can be wrong in a way nothing
on screen reveals.

So ids carry provenance rather than being bare ints, and the UI is expected to
show it. An id is only :attr:`IdStatus.CONFIRMED` when two independent sources
agree; everything thinner is flagged. The one certain check is still manual:
forge the link and click it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IdStatus(StrEnum):
    """How much an item id can be trusted."""

    OWNED = "owned"
    """Straight from your inventory dump. Authoritative."""
    CONFIRMED = "confirmed"
    """Dump and PigParse agree."""
    UNVERIFIED = "unverified"
    """PigParse only. Probably right; badge it anyway."""
    CONFLICT = "conflict"
    """Sources disagree. Trust neither without clicking."""

    @property
    def trusted(self) -> bool:
        return self in (IdStatus.OWNED, IdStatus.CONFIRMED)


@dataclass(frozen=True)
class ItemId:
    """An item id and where it came from."""

    name: str
    item_id: int
    status: IdStatus
    alternate_id: int | None = None
    """The other candidate, when sources disagree."""

    @property
    def trusted(self) -> bool:
        return self.status.trusted


def _key(name: str) -> str:
    return name.strip().casefold()


class ItemCatalog:
    """Name -> id, tracking which source each id came from.

    Owned ids always win a disagreement: the dump is the game's own answer for
    an item held in the character's hands, whereas PigParse aggregates what
    other players typed. The disagreement is still recorded so the UI can show
    it rather than quietly resolving it.
    """

    def __init__(self) -> None:
        self._owned: dict[str, int] = {}
        self._remote: dict[str, int] = {}
        self._names: dict[str, str] = {}

    def learn_owned(self, name: str, item_id: int) -> None:
        """Record an id taken from an inventory dump."""
        if item_id <= 0:
            return
        key = _key(name)
        if not key:
            return
        self._owned[key] = item_id
        self._names.setdefault(key, name.strip())

    def learn_remote(self, name: str, item_id: int | None) -> None:
        """Record an id reported by PigParse."""
        if not item_id or item_id <= 0:
            return
        key = _key(name)
        if not key:
            return
        self._remote[key] = item_id
        self._names.setdefault(key, name.strip())

    def resolve(self, name: str) -> ItemId | None:
        """Best id for ``name``, with its provenance, or ``None`` if unknown."""
        key = _key(name)
        display = self._names.get(key, name.strip())
        owned, remote = self._owned.get(key), self._remote.get(key)
        if owned is not None and remote is not None:
            if owned == remote:
                return ItemId(display, owned, IdStatus.CONFIRMED)
            return ItemId(display, owned, IdStatus.CONFLICT, alternate_id=remote)
        if owned is not None:
            return ItemId(display, owned, IdStatus.OWNED)
        if remote is not None:
            return ItemId(display, remote, IdStatus.UNVERIFIED)
        return None

    def unresolved(self, names: list[str]) -> list[str]:
        """Names with no id yet — what to ask PigParse about."""
        return [name for name in names if self.resolve(name) is None]

    def conflicts(self) -> list[ItemId]:
        """Every name whose sources disagree. Worth surfacing prominently."""
        found = [self.resolve(name) for name in self._names.values()]
        return [item for item in found if item is not None and item.status is IdStatus.CONFLICT]

    def to_dict(self) -> dict:
        return {
            "owned": dict(self._owned),
            "remote": dict(self._remote),
            "names": dict(self._names),
        }

    @classmethod
    def from_dict(cls, data: object) -> ItemCatalog:
        """Rebuild from storage, ignoring anything that isn't well-formed."""
        catalog = cls()
        if not isinstance(data, dict):
            return catalog
        for bucket, target in (("owned", catalog._owned), ("remote", catalog._remote)):
            raw = data.get(bucket)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    try:
                        item_id = int(value)
                    except (TypeError, ValueError):
                        continue
                    if item_id > 0:
                        target[str(key)] = item_id
        names = data.get("names")
        if isinstance(names, dict):
            catalog._names.update({str(k): str(v) for k, v in names.items()})
        return catalog

    def __len__(self) -> int:
        return len(set(self._owned) | set(self._remote))
