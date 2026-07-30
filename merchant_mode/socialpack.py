"""Building ``nparseplus-socials`` macro packs (Qt-free, stdlib only).

The envelope is nParse+'s own shareable macro format
(``nparseplus.core.socials_exchange``), emitted here as a plain dict so this
module stays importable with the SDK alone::

    {"format": "nparseplus-socials", "version": 1,
     "exported_at": "2026-07-30T12:00:00", "label": "Xantik (P1999Green)",
     "socials": [{"page": 1, "button": 1, "name": "WTS 1",
                  "color": 13, "lines": [...]}]}

Exporting a pack rather than writing character inis is deliberate. The host's
Macro Editor already owns the dangerous part — it backs the ini up before the
first write, merges key-by-key so unrelated sections survive, and warns when EQ
is running (the client rewrites these files on camp and would discard the
edits). Reimplementing that here would mean reimplementing its bugs too.

**Throttling.** A social fires every line at once, so five ``/auc`` lines
back-to-back is precisely the channel spam to avoid — and EQ's own chat
throttle will start dropping them. Lines are interleaved with ``/pause``, which
costs line slots: see :func:`.packing.content_line_budget`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

EXPORT_FORMAT = "nparseplus-socials"
EXPORT_VERSION = 1

MAX_LINES = 5
"""Lines one social holds. Matches the host's ``socials.MAX_LINES``."""

DEFAULT_COLOR = 13
"""The host's ``socials.DEFAULT_COLOR``."""

MAX_COLOR = 255

DEFAULT_PAUSE_TENTHS = 30
"""Default gap between content lines, in tenths of a second (≈3.0 s).

EQ's ``/pause`` takes tenths, so 30 is three seconds — courteous in a shared
channel and clear of the client's auction throttle. Set 0 to disable.
"""

MAX_PAUSE_TENTHS = 999


def pause_line(tenths: int) -> str:
    """The ``/pause`` command for ``tenths`` tenths of a second."""
    return f"/pause {tenths}"


def interleave_pauses(lines: list[str], pause_tenths: int = DEFAULT_PAUSE_TENTHS) -> list[str]:
    """Put a pause between consecutive content lines.

    Between only — a trailing pause would delay nothing. ``pause_tenths <= 0``
    returns the lines untouched.
    """
    if pause_tenths <= 0 or len(lines) < 2:
        return list(lines)
    pause = pause_line(min(pause_tenths, MAX_PAUSE_TENTHS))
    woven: list[str] = []
    for index, line in enumerate(lines):
        if index:
            woven.append(pause)
        woven.append(line)
    return woven


def build_social(
    *,
    page: int,
    button: int,
    name: str,
    lines: list[str],
    color: int = DEFAULT_COLOR,
    pause_tenths: int = DEFAULT_PAUSE_TENTHS,
) -> dict:
    """One social button, with pauses woven in and the line cap enforced."""
    woven = interleave_pauses(lines, pause_tenths)[:MAX_LINES]
    return {
        "page": int(page),
        "button": int(button),
        "name": name.strip()[:64],
        "color": max(0, min(MAX_COLOR, int(color))),
        "lines": woven,
    }


def build_pack(
    socials: list[dict], *, label: str = "", exported_at: datetime | None = None
) -> dict:
    """Wrap ``socials`` in the export envelope.

    ``exported_at`` is a naive local datetime — the project-wide convention,
    and what the host asserts on import.
    """
    stamp = (exported_at or datetime.now()).replace(microsecond=0)
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": stamp.isoformat(),
        "label": label,
        "socials": list(socials),
    }


def write_pack(pack: dict, path: Path | str) -> Path:
    """Write ``pack`` as JSON, mirroring the Macro Editor's own exporter."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(pack, indent=2), encoding="utf-8")
    return target
