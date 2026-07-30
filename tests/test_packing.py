"""merchant_mode.packing — the 255-byte link-atomic packer.

The regression this file exists for: a link split mid-body sprays raw hex into
the channel. The in-game probe that proved the limit is reproduced below
byte-for-byte, and :func:`assert_atomic` is the assertion that would actually
have caught the leak — a length check alone would not.
"""

from __future__ import annotations

from merchant_mode.itemlink import BODY_LEN, DELIM, make_link, raw_len
from merchant_mode.packing import (
    LINE_LIMIT,
    MAX_SOCIAL_LINES,
    Entry,
    content_line_budget,
    group_entries,
    pack_lines,
)

CLOAK_OF_FLAMES = 11621


def link(display: str) -> str:
    return make_link(CLOAK_OF_FLAMES, display)


def entries(display: str, count: int) -> list[Entry]:
    return [Entry(link=link(display), label=f"item {n}") for n in range(count)]


def assert_atomic(line: str) -> None:
    """No link on ``line`` is truncated.

    Delimiters must pair up, and the text between each pair must start with a
    complete 45-character body. This is what fails loudly on a mid-body split;
    checking only ``len(line) <= 255`` would let the corrupt line through.
    """
    assert line.count(DELIM) % 2 == 0, f"unbalanced delimiters: {line!r}"
    for chunk in line.split(DELIM)[1::2]:
        assert len(chunk) >= BODY_LEN, f"truncated link body: {chunk!r}"
        int(chunk[:BODY_LEN], 16)  # raises ValueError if it isn't clean hex


def assert_within_limit(lines: list[str], limit: int = LINE_LIMIT) -> None:
    for line in lines:
        assert raw_len(line) <= limit, f"{raw_len(line)} bytes: {line!r}"


# --- the in-game probe -----------------------------------------------------


def test_six_link_probe_spills_the_sixth_link_whole() -> None:
    """Reproduces the measured probe exactly.

    Six links with three-character display text, concatenated with nothing
    between them: each is 50 raw bytes, five fill 250, and the sixth would put
    its opener at byte 251 — which is where the real client leaked ``002D``,
    the opener plus the first four characters of the body.
    """
    result = pack_lines("", entries("CoF", 6), max_lines=5, separator="")

    assert len(result.lines) == 2
    assert raw_len(result.lines[0]) == 250
    assert result.lines[0].count(DELIM) // 2 == 5
    assert result.lines[1].count(DELIM) // 2 == 1
    assert not result.overflow and not result.oversized


def test_six_link_probe_never_leaks_body_hex() -> None:
    result = pack_lines("", entries("CoF", 6), max_lines=5, separator="")
    assert_within_limit(result.lines)
    for line in result.lines:
        assert_atomic(line)
    # The specific leak: line one must not end partway into a body.
    assert not result.lines[0].endswith("002D")
    assert result.lines[0].endswith(DELIM)


# --- the exact-fit boundary ------------------------------------------------


def test_five_links_totalling_exactly_255_stay_on_one_line() -> None:
    # 4-char display -> 51 bytes each -> 5 x 51 == 255 exactly. The off-by-one
    # most likely to bite: this must fill the line, not spill.
    result = pack_lines("", entries("CoFx", 5), max_lines=5, separator="")
    assert len(result.lines) == 1
    assert raw_len(result.lines[0]) == LINE_LIMIT == 255
    assert_atomic(result.lines[0])


def test_five_links_totalling_260_spill_the_last() -> None:
    # 5-char display -> 52 bytes each -> 260 > 255, so only four fit.
    result = pack_lines("", entries("CoFxy", 5), max_lines=5, separator="")
    assert len(result.lines) == 2
    assert result.lines[0].count(DELIM) // 2 == 4
    assert result.lines[1].count(DELIM) // 2 == 1
    assert_within_limit(result.lines)


# --- general packing behaviour ---------------------------------------------


def test_never_exceeds_the_limit_across_a_range_of_display_widths() -> None:
    for width in range(0, 40):
        result = pack_lines("/auc WTS ", entries("x" * width, 12), max_lines=99)
        assert_within_limit(result.lines)
        for line in result.lines:
            assert_atomic(line)


def test_prefix_counts_against_the_budget() -> None:
    bare = pack_lines("", entries("CoF", 20), max_lines=99, separator="")
    prefixed = pack_lines("/auc WTS ", entries("CoF", 20), max_lines=99, separator="")
    assert len(prefixed.lines) >= len(bare.lines)
    for line in prefixed.lines:
        assert line.startswith("/auc WTS ")
    assert_within_limit(prefixed.lines)


def test_suffix_counts_against_the_budget() -> None:
    priced = [Entry(link=link("CoF"), suffix=" 5000pp") for _ in range(6)]
    result = pack_lines("/auc WTS ", priced, max_lines=99)
    assert_within_limit(result.lines)
    for line in result.lines:
        assert_atomic(line)


def test_entries_beyond_the_line_budget_become_overflow() -> None:
    result = pack_lines("", entries("CoF", 12), max_lines=2, separator="")
    assert len(result.lines) == 2
    assert len(result.overflow) == 2  # 12 items, 5 per line, 2 lines kept
    assert not result.ok


def test_an_entry_too_long_for_an_empty_line_is_reported_not_truncated() -> None:
    huge = Entry(link=link("N" * 250), label="Absurdly Named Thing")
    result = pack_lines("/auc WTS ", [huge, *entries("CoF", 2)], max_lines=5)
    assert result.oversized == [huge]
    assert result.oversized[0].label == "Absurdly Named Thing"
    # The good entries still pack, and nothing got cut down to size.
    assert_within_limit(result.lines)
    for line in result.lines:
        assert_atomic(line)
        assert "N" * 250 not in line


def test_empty_input_produces_no_lines() -> None:
    result = pack_lines("/auc WTS ", [], max_lines=5)
    assert result.lines == []
    assert result.ok


def test_group_entries_is_unbounded_in_line_count() -> None:
    groups, oversized = group_entries("", entries("CoF", 23), separator="")
    assert len(groups) == 5
    assert sum(len(group) for group in groups) == 23
    assert oversized == []


# --- the throttling budget -------------------------------------------------


def test_pauses_cut_the_content_budget_from_five_lines_to_three() -> None:
    assert content_line_budget(MAX_SOCIAL_LINES, paused=True) == 3
    assert content_line_budget(MAX_SOCIAL_LINES, paused=False) == 5


def test_content_budget_leaves_room_for_the_pauses_it_implies() -> None:
    for max_lines in range(1, 12):
        content = content_line_budget(max_lines, paused=True)
        # c content lines need c-1 pauses between them.
        assert content + max(0, content - 1) <= max_lines
