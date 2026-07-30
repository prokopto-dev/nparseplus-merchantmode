"""Server identity, and the wire ints PigParse is keyed on.

These numbers are the difference between a price for your server and a price
for somebody else's, and nothing on screen would reveal the mistake — hence
the drift check against the host's own enum.
"""

from __future__ import annotations

import pytest

from merchant_mode.servers import (
    SERVERS,
    by_key,
    host_drift,
    keys,
    label_for,
    normalize_key,
    wire_for,
)


def test_the_wire_ints_match_eqtoolshared() -> None:
    assert [(server.key, server.wire) for server in SERVERS] == [
        ("green", 0),
        ("blue", 1),
        ("red", 2),
        ("quarm", 3),
    ]


def test_the_vendored_table_agrees_with_the_host() -> None:
    """Skipped without the app — CI installs the SDK alone."""
    pytest.importorskip("nparseplus", reason="host app not installed")
    assert host_drift() == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("green", "green"),
        ("Green", "green"),
        ("  BLUE  ", "blue"),
        (0, "green"),  # the host's Server IntEnum arrives as a bare int
        (3, "quarm"),
        (None, ""),
        ("", ""),
        ("nektulos", ""),
        (99, ""),
        (True, ""),  # bool is an int; nobody means server False
    ],
)
def test_normalize_key_swallows_every_shape_a_server_arrives_in(value, expected) -> None:
    assert normalize_key(value) == expected


def test_wire_for_declines_rather_than_guessing() -> None:
    # A price fetched against a guessed server is worse than no price at all.
    assert wire_for("") is None
    assert wire_for("nektulos") is None
    assert wire_for("blue") == 1


def test_label_and_lookup() -> None:
    assert label_for("quarm") == "Quarm"
    assert label_for("") == ""
    assert by_key("RED").wire == 2
    assert keys() == ["green", "blue", "red", "quarm"]
