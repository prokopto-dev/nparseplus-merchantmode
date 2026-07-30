"""Which server a dump and a price belong to (Qt-free, stdlib only).

PigParse keys every price by server, so any price question needs one — and the
plugin has to be able to answer it *with EQ closed*, because that is exactly
when an inventory dump gets loaded. So the server travels with the dump rather
than being read off the live session each time it's needed.

The wire ints mirror the host's ``nparseplus.core.enums.Server``, which in turn
mirrors EQToolShared's ``Servers.cs``. They are vendored rather than imported
because this module must work with the SDK alone — the host is not importable
in CI, in ``nparseplus-plugin validate``, or in these tests. :func:`host_drift`
re-checks them against the host when it *is* importable, so a divergence
surfaces as a log line rather than as silently wrong prices.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "SERVERS",
    "Server",
    "by_key",
    "host_drift",
    "keys",
    "label_for",
    "normalize_key",
    "wire_for",
]


@dataclass(frozen=True)
class Server:
    """One P99-family server."""

    key: str
    """The host's ``ActivePlayer.server_key`` convention: lowercase ("green")."""
    label: str
    """How it's written for a human ("Green")."""
    wire: int
    """The int PigParse's ``Server`` field wants. Do not reorder."""


SERVERS: tuple[Server, ...] = (
    Server("green", "Green", 0),
    Server("blue", "Blue", 1),
    Server("red", "Red", 2),
    Server("quarm", "Quarm", 3),
)

_BY_KEY = {server.key: server for server in SERVERS}


def normalize_key(value: object) -> str:
    """Coerce anything server-shaped to a key, or ``""``.

    Accepts a key, a label, the host's ``Server`` enum member, or the bare wire
    int — the value arrives from storage, from a combo box, and from
    ``ctx.player.server`` respectively, and none of them agree on a type.
    """
    if value is None:
        return ""
    if isinstance(value, bool):  # bool is an int; nobody means server False
        return ""
    if isinstance(value, int):
        for server in SERVERS:
            if server.wire == value:
                return server.key
        return ""
    text = str(getattr(value, "name", value) or "").strip().casefold()
    return text if text in _BY_KEY else ""


def by_key(value: object) -> Server | None:
    key = normalize_key(value)
    return _BY_KEY.get(key)


def wire_for(value: object) -> int | None:
    """The PigParse wire int, or ``None`` when the server isn't known.

    ``None`` is the honest answer for "no server chosen yet" and callers are
    expected to decline to fetch rather than guess — a price from the wrong
    server is worse than no price, because nothing about it looks wrong.
    """
    server = by_key(value)
    return server.wire if server is not None else None


def label_for(value: object) -> str:
    server = by_key(value)
    return server.label if server is not None else ""


def keys() -> list[str]:
    return [server.key for server in SERVERS]


def host_drift() -> list[str]:
    """Names where the vendored wire ints disagree with the host's enum.

    Empty when they agree, and empty when the host isn't importable — this is a
    consistency check, not a requirement. Called once at activate() so a future
    server addition shows up in the log instead of in someone's price history.
    """
    try:
        from nparseplus.core.enums import Server as HostServer
    except ImportError:
        return []

    drift: list[str] = []
    for server in SERVERS:
        member = getattr(HostServer, server.key.upper(), None)
        if member is None:
            drift.append(f"{server.key}: absent from host enum")
        elif int(member) != server.wire:
            drift.append(f"{server.key}: host says {int(member)}, vendored says {server.wire}")
    for member in HostServer:
        if member.name.lower() not in _BY_KEY:
            drift.append(f"{member.name.lower()}: host has it, vendored does not")
    return drift
