"""Packing item links into EQ social lines (Qt-free, stdlib only).

The social line buffer is a hard **255 raw bytes**, measured twice in-game: a
plain ruler cut at exactly 255, and a six-link probe whose 6th link leaked
``002D`` — the opener plus the first four body characters — at bytes 251-255.

That leak is the whole reason this module exists. A link split mid-body does
not degrade gracefully: it sprays raw hex into the channel, which in
``/auction`` reads as a broken third-party tool. So packing here is
**link-atomic** — a link that does not fit whole is moved to the next line,
never truncated. There is deliberately no code path anywhere in this package
that slices a rendered line to length.

Capacity, for orientation: a link costs 47 raw bytes plus its display text, so
after an ``/auc WTS `` prefix a line holds about 3 full-name or 4 abbreviated
items, and a social holds 5 lines — fewer once throttling takes its share, see
:func:`content_line_budget`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .itemlink import raw_len

LINE_LIMIT = 255
"""Hard cap on one social line, in raw bytes. Measured, not assumed."""

MAX_SOCIAL_LINES = 5
"""Lines one social button holds, including any ``/pause`` lines."""

SEPARATOR = " "
"""Placed between entries on the same line."""


@dataclass(frozen=True)
class Entry:
    """One sellable thing: its link, plus whatever trails it (a price)."""

    link: str
    suffix: str = ""
    label: str = ""
    """Human-readable name, for reporting overflow and oversized entries."""

    @property
    def text(self) -> str:
        return f"{self.link}{self.suffix}"

    @property
    def size(self) -> int:
        return raw_len(self.text)


@dataclass(frozen=True)
class PackResult:
    """Lines that fit, plus everything that didn't and why."""

    lines: list[str] = field(default_factory=list)
    overflow: list[Entry] = field(default_factory=list)
    """Fit a line, but ran out of lines. Offer these as a second social."""
    oversized: list[Entry] = field(default_factory=list)
    """Too big for an empty line. Never truncated — surface these to the user."""

    @property
    def ok(self) -> bool:
        return not self.overflow and not self.oversized


def content_line_budget(max_lines: int = MAX_SOCIAL_LINES, *, paused: bool = True) -> int:
    """How many *content* lines fit once pause lines take their share.

    Pauses go between content lines and never trail, so ``c`` content lines
    need ``2c - 1`` slots. Against the default five that leaves **three** —
    throttling costs 40% of a social's capacity, which is why the packer is
    told the content budget up front instead of discovering it after the fact.
    """
    if max_lines < 1:
        return 0
    if not paused:
        return max_lines
    return (max_lines + 1) // 2


def group_entries(
    prefix: str,
    entries: list[Entry],
    *,
    limit: int = LINE_LIMIT,
    separator: str = SEPARATOR,
) -> tuple[list[list[Entry]], list[Entry]]:
    """Greedily group ``entries`` into lines that each fit ``limit``.

    Unbounded in line count — :func:`pack_lines` applies the budget. Returns
    ``(groups, oversized)``; an entry that cannot fit even on a line of its own
    is never split, it goes to ``oversized``.

    ``separator`` is settable mostly so the in-game six-link probe can be
    reproduced byte-for-byte in the tests, where the links were concatenated
    with nothing between them.
    """
    prefix_len = raw_len(prefix)
    separator_len = raw_len(separator)
    groups: list[list[Entry]] = []
    oversized: list[Entry] = []
    current: list[Entry] = []
    current_len = prefix_len

    for entry in entries:
        size = entry.size
        if prefix_len + size > limit:
            # Hopeless on any line. Reporting beats emitting a corrupt line.
            oversized.append(entry)
            continue
        cost = size if not current else separator_len + size
        if current and current_len + cost > limit:
            groups.append(current)
            current = []
            current_len = prefix_len
            cost = size
        current.append(entry)
        current_len += cost

    if current:
        groups.append(current)
    return groups, oversized


def render_line(prefix: str, group: list[Entry], *, separator: str = SEPARATOR) -> str:
    """Render one group as a finished social line."""
    return prefix + separator.join(entry.text for entry in group)


def pack_lines(
    prefix: str,
    entries: list[Entry],
    *,
    limit: int = LINE_LIMIT,
    max_lines: int = MAX_SOCIAL_LINES,
    separator: str = SEPARATOR,
) -> PackResult:
    """Pack ``entries`` into at most ``max_lines`` lines, link-atomically.

    ``max_lines`` should come from :func:`content_line_budget` so that pause
    lines are accounted for before packing rather than after.
    """
    groups, oversized = group_entries(prefix, entries, limit=limit, separator=separator)
    kept, dropped = groups[:max_lines], groups[max_lines:]
    return PackResult(
        lines=[render_line(prefix, group, separator=separator) for group in kept],
        overflow=[entry for group in dropped for entry in group],
        oversized=oversized,
    )
