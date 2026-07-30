"""Forging EverQuest item links (Qt-free, stdlib only).

An item link on the wire is::

    \\x12 + <45-char body> + <display text> + \\x12

The body is the Titanium ("6.2") layout from EQEmu's
``common/patches/titanium.cpp`` (``ServerToTitaniumSayLink``) — fixed-width
hex fields with no delimiters:

===== === ============= ====== =====
off   len field         format value
===== === ============= ====== =====
0     1   action_id     %1X    0
1     5   item_id       %05X   the item
6     25  augment_1..5  5x%05X 0
31    1   is_evolving   %1X    0
32    4   evolve_group  %04X   0
36    1   evolve_level  %1X    0
37    8   hash          %08X   0
===== === ============= ====== =====

Two facts make forging viable, both confirmed in-game on P99 Green:

- **The hash can be zero.** EQEmu never computes it either (its source carries
  a bare ``// TODO: add hash call``), so ``00000000`` renders *and* clicks
  through to the right item.
- **Display text is cosmetic.** ``item_id`` alone drives the click, so
  ``\\x12<body>CoF\\x12`` shows "CoF" and opens Cloak of Flames. That makes the
  display text the only compressible part of a link — see :mod:`.nicknames`.

Sizes are measured in **latin-1 bytes**, not characters and not UTF-8. The EQ
client charges a raw byte per character of its own charset; measuring in UTF-8
would over-count any non-ASCII character and silently shrink the usable line.
"""

from __future__ import annotations

from dataclasses import dataclass

DELIM = "\x12"
"""The link delimiter (ASCII DC2). Opens and closes every link."""

BODY_LEN = 45
"""Total width of the encoded body, in characters."""

LINK_OVERHEAD = 2 + BODY_LEN
"""Raw bytes a link costs before its display text: both delimiters plus body."""

MAX_ITEM_ID = 0xFFFFF
"""Largest id the 5-wide ``%05X`` field can hold without shifting later fields."""

MAX_AUGMENTS = 5

# Field offsets, as (name, start, width). Kept as data so encode and decode
# cannot drift apart — decode_body walks this table rather than re-hardcoding
# the offsets a second time.
_LAYOUT: tuple[tuple[str, int, int], ...] = (
    ("action_id", 0, 1),
    ("item_id", 1, 5),
    ("augment_1", 6, 5),
    ("augment_2", 11, 5),
    ("augment_3", 16, 5),
    ("augment_4", 21, 5),
    ("augment_5", 26, 5),
    ("is_evolving", 31, 1),
    ("evolve_group", 32, 4),
    ("evolve_level", 36, 1),
    ("link_hash", 37, 8),
)


@dataclass(frozen=True)
class ItemLink:
    """An item plus the text it should display in chat."""

    item_id: int
    display: str

    @property
    def text(self) -> str:
        return make_link(self.item_id, self.display)

    @property
    def raw_size(self) -> int:
        return LINK_OVERHEAD + raw_len(self.display)


def raw_len(text: str) -> int:
    """Length of ``text`` in the raw bytes the EQ client charges for it.

    The client's charset is single-byte, so a character outside ASCII still
    costs exactly one byte to it. Encoding as latin-1 with replacement models
    that; ``len(text.encode())`` (UTF-8) would over-count and make the packer
    needlessly conservative.
    """
    return len(text.encode("latin-1", errors="replace"))


def encode_body(
    item_id: int,
    *,
    action_id: int = 0,
    augments: tuple[int, ...] = (0, 0, 0, 0, 0),
    is_evolving: int = 0,
    evolve_group: int = 0,
    evolve_level: int = 0,
    link_hash: int = 0,
) -> str:
    """Encode the 45-character link body for ``item_id``.

    Every field is range-checked rather than allowed to overflow its width. An
    over-wide field would not merely be wrong, it would shift every field after
    it and turn the body into a different — but still plausible-looking — item,
    which is exactly the kind of silent failure this format invites.
    """
    if not 0 <= item_id <= MAX_ITEM_ID:
        raise ValueError(f"item_id {item_id} outside 0..{MAX_ITEM_ID} (the %05X field)")
    if len(augments) > MAX_AUGMENTS:
        raise ValueError(f"at most {MAX_AUGMENTS} augments, got {len(augments)}")
    padded = tuple(augments) + (0,) * (MAX_AUGMENTS - len(augments))
    for index, augment in enumerate(padded, start=1):
        if not 0 <= augment <= MAX_ITEM_ID:
            raise ValueError(f"augment_{index} {augment} outside 0..{MAX_ITEM_ID}")
    if not 0 <= action_id <= 0xF:
        raise ValueError(f"action_id {action_id} outside 0..15")
    if not 0 <= is_evolving <= 0xF:
        raise ValueError(f"is_evolving {is_evolving} outside 0..15")
    if not 0 <= evolve_group <= 0xFFFF:
        raise ValueError(f"evolve_group {evolve_group} outside 0..65535")
    if not 0 <= evolve_level <= 0xF:
        raise ValueError(f"evolve_level {evolve_level} outside 0..15")
    if not 0 <= link_hash <= 0xFFFFFFFF:
        raise ValueError(f"link_hash {link_hash} outside 0..4294967295")

    # One fragment per field, in layout order, so the body reads the same way
    # the table at the top of this module does.
    fields = [
        f"{action_id:1X}",
        f"{item_id:05X}",
        *(f"{augment:05X}" for augment in padded),
        f"{is_evolving:1X}",
        f"{evolve_group:04X}",
        f"{evolve_level:1X}",
        f"{link_hash:08X}",
    ]
    body = "".join(fields)
    # Belt and braces: the range checks above should make this unreachable, but
    # a body of the wrong width corrupts the whole chat line, so never emit one.
    if len(body) != BODY_LEN:
        raise AssertionError(f"encoded body is {len(body)} chars, expected {BODY_LEN}")
    return body


def decode_body(body: str) -> dict[str, int]:
    """Inverse of :func:`encode_body`, for round-trip tests and diagnostics."""
    if len(body) != BODY_LEN:
        raise ValueError(f"body must be {BODY_LEN} chars, got {len(body)}")
    try:
        return {name: int(body[start : start + width], 16) for name, start, width in _LAYOUT}
    except ValueError as exc:
        raise ValueError(f"body is not hex: {body!r}") from exc


def make_link(item_id: int, display: str) -> str:
    """Build a complete, clickable link showing ``display``."""
    if DELIM in display:
        # A delimiter inside the display text would close the link early and
        # spray the remainder — including the next link's body — as plain text.
        raise ValueError("display text may not contain the link delimiter (\\x12)")
    return f"{DELIM}{encode_body(item_id)}{display}{DELIM}"


def link_size(display: str) -> int:
    """Raw bytes a link with ``display`` will occupy."""
    return LINK_OVERHEAD + raw_len(display)
