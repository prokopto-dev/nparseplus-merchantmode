"""Turning selected inventory into WTS socials (Qt-free, stdlib only).

This is the composition layer the window drives: listings in, a finished macro
pack out. It owns the one decision none of the lower modules can make alone —
how many socials to spend — because throttling makes overflow the common case
rather than the edge case. With pauses on, a social carries three content lines
of roughly four abbreviated items, so a dozen items is already a second button.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .itemlink import make_link
from .nicknames import NicknameTable
from .packing import (
    LINE_LIMIT,
    MAX_SOCIAL_LINES,
    Entry,
    content_line_budget,
    pack_lines,
)
from .socialpack import DEFAULT_PAUSE_TENTHS, build_social

DEFAULT_PREFIX = "/auc WTS "
DEFAULT_MAX_SOCIALS = 4


@dataclass(frozen=True)
class Listing:
    """One item offered for sale.

    ``character`` records who is actually holding it. A merchant advertises for
    a whole account, so the listing pool spans characters — and the moment a
    buyer says yes, "which of my alts has this?" is the only question that
    matters.
    """

    item_id: int
    name: str
    price: str = ""
    """Free text as it should appear after the link, e.g. ``5k``. The plugin
    never interprets this — the seller's wording is the seller's business."""
    character: str = ""

    def entry(self, nicknames: NicknameTable, *, abbreviate: bool = True) -> Entry:
        display = nicknames.display_for(self.name, abbreviate=abbreviate)
        suffix = f" {self.price.strip()}" if self.price.strip() else ""
        return Entry(
            link=make_link(self.item_id, display),
            suffix=suffix,
            label=self.name,
        )


@dataclass(frozen=True)
class BuildResult:
    """Socials ready to export, plus whatever wouldn't fit."""

    socials: list[dict] = field(default_factory=list)
    unplaced: list[Entry] = field(default_factory=list)
    """Fit a line, but ran out of socials. Raise ``max_socials`` to take them."""
    oversized: list[Entry] = field(default_factory=list)
    """Too big for an empty line even alone — usually a very long name with no
    nickname. Shortening the display text is the only fix."""

    @property
    def ok(self) -> bool:
        return not self.unplaced and not self.oversized

    @property
    def line_count(self) -> int:
        return sum(len(social["lines"]) for social in self.socials)


def build_wts_socials(
    listings: list[Listing],
    *,
    nicknames: NicknameTable | None = None,
    abbreviate: bool = True,
    prefix: str = DEFAULT_PREFIX,
    pause_tenths: int = DEFAULT_PAUSE_TENTHS,
    page: int = 1,
    first_button: int = 1,
    max_socials: int = DEFAULT_MAX_SOCIALS,
    name_template: str = "WTS {n}",
    limit: int = LINE_LIMIT,
    max_lines: int = MAX_SOCIAL_LINES,
) -> BuildResult:
    """Pack ``listings`` into as many socials as it takes, up to ``max_socials``."""
    table = nicknames if nicknames is not None else NicknameTable()
    entries = [listing.entry(table, abbreviate=abbreviate) for listing in listings]

    per_social = content_line_budget(max_lines, paused=pause_tenths > 0)
    if per_social < 1:
        return BuildResult(unplaced=list(entries))

    socials: list[dict] = []
    oversized: list[Entry] = []
    pending = entries
    for index in range(max_socials):
        if not pending:
            break
        result = pack_lines(prefix, pending, limit=limit, max_lines=per_social)
        oversized.extend(result.oversized)
        if not result.lines:
            # Everything left is oversized; another social would not help.
            pending = []
            break
        socials.append(
            build_social(
                page=page,
                button=first_button + index,
                name=name_template.format(n=index + 1),
                lines=result.lines,
                pause_tenths=pause_tenths,
            )
        )
        pending = result.overflow

    return BuildResult(socials=socials, unplaced=list(pending), oversized=oversized)
