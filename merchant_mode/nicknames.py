"""Abbreviating link display text (Qt-free, stdlib only).

Display text is the only compressible part of a link: the body is a fixed 45
characters and ``item_id`` alone decides what a click opens, so the visible
label is free to be as short as the seller likes. ``\\x12<body>CoF\\x12`` shows
"CoF" and still opens Cloak of Flames.

The payoff is real but bounded — measured against typical P99 item names, a
three-character nickname takes a line from 3 items to 4, so a five-line social
goes from 15 items to 20. Worth having, not worth mangling names for, which is
why nothing here abbreviates automatically: the table is the user's.
"""

from __future__ import annotations

DEFAULT_NICKNAMES: dict[str, str] = {
    "cloak of flames": "CoF",
    "journeyman's boots": "JBoots",
    "fungus covered scale tunic": "Fungi",
    "guise of the deceiver": "Guise",
    "rubicite breastplate": "Rubi BP",
    "cloak of the shrouded temple": "CoTST",
    "manastone": "Mana",
}
"""A small starter table of the abbreviations P99 already uses in chat.

Deliberately short. These are conventions a buyer will recognise on sight;
inventing new ones costs the reader more than it saves the line.
"""


def normalize(name: str) -> str:
    """Key form for nickname lookup: trimmed and case-folded."""
    return name.strip().casefold()


class NicknameTable:
    """User-editable item name -> display text mapping.

    Lookup is case-insensitive; the stored key is normalized but the original
    casing of the *value* is preserved, because that's what shows in chat.
    """

    def __init__(self, entries: dict[str, str] | None = None) -> None:
        self._entries: dict[str, str] = {}
        for name, nickname in (entries if entries is not None else DEFAULT_NICKNAMES).items():
            self.set(name, nickname)

    def set(self, name: str, nickname: str) -> None:
        key = normalize(name)
        if not key:
            return
        value = nickname.strip()
        if value:
            self._entries[key] = value
        else:
            self._entries.pop(key, None)

    def remove(self, name: str) -> None:
        self._entries.pop(normalize(name), None)

    def get(self, name: str) -> str | None:
        return self._entries.get(normalize(name))

    def display_for(self, name: str, *, abbreviate: bool = True) -> str:
        """The text a link for ``name`` should show."""
        if not abbreviate:
            return name
        return self.get(name) or name

    def to_dict(self) -> dict[str, str]:
        return dict(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and normalize(name) in self._entries
