"""merchant_mode.nicknames — the abbreviation table."""

from __future__ import annotations

from merchant_mode.nicknames import DEFAULT_NICKNAMES, NicknameTable


def test_abbreviates_a_known_name() -> None:
    table = NicknameTable({"Cloak of Flames": "CoF"})
    assert table.display_for("Cloak of Flames") == "CoF"


def test_lookup_is_case_and_whitespace_insensitive() -> None:
    table = NicknameTable({"Cloak of Flames": "CoF"})
    assert table.display_for("  cloak OF flames ") == "CoF"


def test_an_unknown_name_passes_through_unchanged() -> None:
    assert NicknameTable({}).display_for("Rusty Sword") == "Rusty Sword"


def test_abbreviation_can_be_turned_off() -> None:
    table = NicknameTable({"Cloak of Flames": "CoF"})
    assert table.display_for("Cloak of Flames", abbreviate=False) == "Cloak of Flames"


def test_nicknames_are_editable() -> None:
    table = NicknameTable({})
    table.set("Manastone", "Mana")
    assert table.display_for("Manastone") == "Mana"
    table.set("Manastone", "MS")
    assert table.display_for("Manastone") == "MS"


def test_setting_a_blank_nickname_removes_it() -> None:
    table = NicknameTable({"Manastone": "Mana"})
    table.set("Manastone", "   ")
    assert table.display_for("Manastone") == "Manastone"


def test_remove_drops_an_entry() -> None:
    table = NicknameTable({"Manastone": "Mana"})
    table.remove("manastone")
    assert "Manastone" not in table


def test_blank_names_are_ignored() -> None:
    table = NicknameTable({})
    table.set("  ", "X")
    assert len(table) == 0


def test_defaults_cover_the_abbreviations_p99_already_uses() -> None:
    table = NicknameTable()
    assert table.display_for("Cloak of Flames") == "CoF"
    assert len(table) == len(DEFAULT_NICKNAMES)


def test_table_round_trips_through_storage() -> None:
    table = NicknameTable({"Cloak of Flames": "CoF"})
    restored = NicknameTable(table.to_dict())
    assert restored.display_for("Cloak of Flames") == "CoF"
