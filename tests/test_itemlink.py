"""merchant_mode.itemlink — the 45-character link body encoder."""

from __future__ import annotations

import pytest

from merchant_mode.itemlink import (
    BODY_LEN,
    DELIM,
    LINK_OVERHEAD,
    MAX_ITEM_ID,
    decode_body,
    encode_body,
    link_size,
    make_link,
    raw_len,
)

CLOAK_OF_FLAMES = 11621
"""Confirmed in-game on P99 Green, and agreed by three independent databases."""

# The known-good body, captured from a line that rendered and clicked through.
KNOWN_GOOD_BODY = "002D65000000000000000000000000000000000000000"

# The full line as sent, 74 raw bytes.
KNOWN_GOOD_LINE = f"/say WTS {DELIM}{KNOWN_GOOD_BODY}Cloak of Flames{DELIM} 5k"


def test_body_is_exactly_45_characters() -> None:
    assert len(encode_body(CLOAK_OF_FLAMES)) == BODY_LEN == 45


def test_encodes_the_known_good_body() -> None:
    assert encode_body(CLOAK_OF_FLAMES) == KNOWN_GOOD_BODY


def test_known_good_line_is_74_raw_bytes() -> None:
    assert raw_len(KNOWN_GOOD_LINE) == 74


def test_decode_recovers_every_field() -> None:
    fields = decode_body(KNOWN_GOOD_BODY)
    assert fields["item_id"] == CLOAK_OF_FLAMES
    assert fields["action_id"] == 0
    assert fields["link_hash"] == 0
    assert all(fields[f"augment_{n}"] == 0 for n in range(1, 6))
    assert fields["is_evolving"] == fields["evolve_group"] == fields["evolve_level"] == 0


def test_round_trips_through_encode_and_decode() -> None:
    body = encode_body(0xABCDE, action_id=3, evolve_group=0x1234, link_hash=0xDEADBEEF)
    fields = decode_body(body)
    assert fields["item_id"] == 0xABCDE
    assert fields["action_id"] == 3
    assert fields["evolve_group"] == 0x1234
    assert fields["link_hash"] == 0xDEADBEEF


def test_make_link_wraps_body_in_delimiters() -> None:
    link = make_link(CLOAK_OF_FLAMES, "CoF")
    assert link == f"{DELIM}{KNOWN_GOOD_BODY}CoF{DELIM}"
    assert link.count(DELIM) == 2


def test_display_text_is_cosmetic_but_the_id_still_drives_the_click() -> None:
    # Same item, different label: only the display text differs.
    short = make_link(CLOAK_OF_FLAMES, "CoF")
    long = make_link(CLOAK_OF_FLAMES, "Cloak of Flames")
    assert decode_body(short[1 : 1 + BODY_LEN]) == decode_body(long[1 : 1 + BODY_LEN])


def test_link_size_counts_both_delimiters_and_the_body() -> None:
    assert LINK_OVERHEAD == 47
    assert link_size("CoF") == 50
    assert link_size("") == 47
    assert link_size("CoF") == raw_len(make_link(CLOAK_OF_FLAMES, "CoF"))


@pytest.mark.parametrize("item_id", [-1, MAX_ITEM_ID + 1])
def test_out_of_range_item_id_raises(item_id: int) -> None:
    # An over-wide field would shift every field after it and silently encode a
    # different item, so this must never be allowed to pass through.
    with pytest.raises(ValueError, match="item_id"):
        encode_body(item_id)


def test_delimiter_in_display_text_raises() -> None:
    # It would close the link early and spray the rest of the line as plain text.
    with pytest.raises(ValueError, match="delimiter"):
        make_link(CLOAK_OF_FLAMES, f"CoF{DELIM}")


def test_too_many_augments_raises() -> None:
    with pytest.raises(ValueError, match="augments"):
        encode_body(CLOAK_OF_FLAMES, augments=(0, 0, 0, 0, 0, 0))


def test_decode_rejects_a_wrong_width_body() -> None:
    with pytest.raises(ValueError, match="45"):
        decode_body(KNOWN_GOOD_BODY[:-1])


def test_raw_len_charges_one_byte_per_character() -> None:
    # The EQ client's charset is single-byte; measuring in UTF-8 would
    # over-count and needlessly shrink the usable line.
    assert raw_len("Cloak") == 5
    assert raw_len("Naïve") == 5
    assert len("Naïve".encode()) == 6
