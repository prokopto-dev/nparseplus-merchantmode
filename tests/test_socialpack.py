"""merchant_mode.socialpack — the nparseplus-socials envelope and throttling."""

from __future__ import annotations

import json
from datetime import datetime

from merchant_mode.socialpack import (
    DEFAULT_COLOR,
    DEFAULT_PAUSE_TENTHS,
    EXPORT_FORMAT,
    EXPORT_VERSION,
    MAX_LINES,
    MAX_PAUSE_TENTHS,
    build_pack,
    build_social,
    interleave_pauses,
    pause_line,
    write_pack,
)

LINES = ["/auc WTS one", "/auc WTS two", "/auc WTS three"]


# --- throttling ------------------------------------------------------------


def test_pause_goes_between_lines_and_never_trails() -> None:
    # A trailing pause would delay nothing.
    woven = interleave_pauses(["a", "b", "c"], 30)
    assert woven == ["a", "/pause 30", "b", "/pause 30", "c"]


def test_a_single_line_needs_no_pause() -> None:
    assert interleave_pauses(["only"], 30) == ["only"]


def test_zero_disables_throttling() -> None:
    assert interleave_pauses(LINES, 0) == LINES


def test_negative_pause_is_treated_as_disabled() -> None:
    assert interleave_pauses(LINES, -5) == LINES


def test_pause_is_clamped_to_what_the_client_accepts() -> None:
    woven = interleave_pauses(["a", "b"], MAX_PAUSE_TENTHS + 1000)
    assert woven[1] == pause_line(MAX_PAUSE_TENTHS)


def test_default_pause_is_three_seconds_in_tenths() -> None:
    assert DEFAULT_PAUSE_TENTHS == 30
    assert pause_line(DEFAULT_PAUSE_TENTHS) == "/pause 30"


# --- socials ---------------------------------------------------------------


def test_throttled_social_holds_three_content_lines_within_the_five_line_cap() -> None:
    social = build_social(page=1, button=1, name="WTS 1", lines=LINES)
    assert len(social["lines"]) == 5 == MAX_LINES
    assert social["lines"].count("/pause 30") == 2
    assert [line for line in social["lines"] if not line.startswith("/pause")] == LINES


def test_unthrottled_social_holds_five_content_lines() -> None:
    five = [f"/auc WTS {n}" for n in range(5)]
    social = build_social(page=1, button=1, name="WTS 1", lines=five, pause_tenths=0)
    assert social["lines"] == five


def test_social_never_exceeds_the_line_cap() -> None:
    social = build_social(page=1, button=1, name="big", lines=[f"line {n}" for n in range(20)])
    assert len(social["lines"]) <= MAX_LINES


def test_social_carries_the_grid_slot_and_default_colour() -> None:
    social = build_social(page=2, button=7, name="  WTS  ", lines=["/auc hi"])
    assert social["page"] == 2
    assert social["button"] == 7
    assert social["name"] == "WTS"
    assert social["color"] == DEFAULT_COLOR


def test_colour_is_clamped_to_what_the_client_accepts() -> None:
    assert build_social(page=1, button=1, name="x", lines=["y"], color=9999)["color"] == 255
    assert build_social(page=1, button=1, name="x", lines=["y"], color=-4)["color"] == 0


# --- the envelope ----------------------------------------------------------


def test_envelope_declares_the_format_the_macro_editor_expects() -> None:
    pack = build_pack([], label="Xantik (P1999Green)")
    assert pack["format"] == EXPORT_FORMAT == "nparseplus-socials"
    assert pack["version"] == EXPORT_VERSION == 1
    assert pack["label"] == "Xantik (P1999Green)"
    assert pack["socials"] == []


def test_exported_at_is_naive_local_time() -> None:
    # The project-wide invariant: the whole pipeline compares naive datetimes.
    pack = build_pack([])
    assert datetime.fromisoformat(pack["exported_at"]).tzinfo is None


def test_exported_at_drops_microseconds() -> None:
    stamp = datetime(2026, 7, 30, 12, 0, 0, 123456)
    assert build_pack([], exported_at=stamp)["exported_at"] == "2026-07-30T12:00:00"


def test_pack_round_trips_through_json() -> None:
    social = build_social(page=1, button=1, name="WTS 1", lines=LINES)
    pack = build_pack([social], label="Xantik (P1999Green)")
    restored = json.loads(json.dumps(pack))
    assert restored == pack


def test_write_pack_creates_parent_directories(tmp_path) -> None:
    target = tmp_path / "packs" / "wts-1.json"
    written = write_pack(build_pack([]), target)
    assert written == target
    assert json.loads(target.read_text(encoding="utf-8"))["format"] == EXPORT_FORMAT
