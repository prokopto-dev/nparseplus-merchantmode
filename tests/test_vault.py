"""merchant_mode.inventory — dumps kept across characters.

A merchant advertises for a whole account, so the sellable pool is the union of
every character's dump. The question that matters the moment a buyer says yes
is *which alt is holding this, and how stale is that answer* — these tests pin
both.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from merchant_mode.inventory import (
    ORIGIN_HOST,
    ORIGIN_MANUAL,
    STALE_AFTER,
    CharacterInventory,
    InventoryItem,
    InventoryVault,
    character_from_filename,
    inventory_key,
)

T0 = datetime(2026, 7, 30, 20, 0, 0)


def item(name: str, item_id: int, location: str = "General1") -> InventoryItem:
    return InventoryItem(location=location, name=name, item_id=item_id, count=1, slots=0)


def vault_with_two_characters() -> InventoryVault:
    vault = InventoryVault()
    vault.put(
        "Xantik",
        "green",
        [item("Cloak of Flames", 11621, "Back"), item("Bone Chips", 13073, "General1-Slot2")],
        captured_at=T0,
    )
    vault.put(
        "Mulebank",
        "green",
        [item("Manastone", 4567, "General2-Slot1")],
        captured_at=T0 - timedelta(days=30),
    )
    return vault


# --- pooling across characters ---------------------------------------------


def test_dumps_accumulate_rather_than_replace() -> None:
    vault = vault_with_two_characters()
    assert len(vault) == 2
    assert {holding.name for holding in vault.holdings()} == {
        "Cloak of Flames",
        "Bone Chips",
        "Manastone",
    }


def test_reloading_a_character_replaces_only_that_character() -> None:
    vault = vault_with_two_characters()
    vault.put("Xantik", "green", [item("Fungi Tunic", 2735)], captured_at=T0)
    assert len(vault) == 2
    names = {holding.name for holding in vault.holdings()}
    assert names == {"Fungi Tunic", "Manastone"}


def test_character_identity_is_case_insensitive() -> None:
    vault = InventoryVault()
    vault.put("Xantik", "green", [item("A Thing", 1)], captured_at=T0)
    vault.put("xantik", "GREEN", [item("Another Thing", 2)], captured_at=T0)
    assert len(vault) == 1
    assert inventory_key("Xantik", "green") == inventory_key(" XANTIK ", "Green")


def test_the_same_item_on_two_alts_is_reported_twice() -> None:
    vault = InventoryVault()
    vault.put("Xantik", "green", [item("Bone Chips", 13073)], captured_at=T0)
    vault.put("Mulebank", "green", [item("Bone Chips", 13073)], captured_at=T0)
    found = vault.locate("bone chips")
    assert {holding.character for holding in found} == {"Xantik", "Mulebank"}


def test_locating_an_unheld_item_finds_nothing() -> None:
    assert vault_with_two_characters().locate("Rubicite Breastplate") == []


def test_dropping_a_character_removes_their_items() -> None:
    vault = vault_with_two_characters()
    vault.drop("Mulebank", "green")
    assert len(vault) == 1
    assert vault.locate("Manastone") == []


# --- where is it, and how stale ---------------------------------------------


def test_where_names_the_character_and_the_slot() -> None:
    vault = vault_with_two_characters()
    holding = vault.locate("Cloak of Flames")[0]
    assert holding.where() == "Xantik · Back"


def test_a_stale_dump_says_how_old_it_is() -> None:
    vault = vault_with_two_characters()
    holding = vault.locate("Manastone")[0]
    where = holding.where(T0)
    assert where.startswith("Mulebank · General2-Slot1")
    assert "4w old" in where


def test_a_fresh_dump_carries_no_age_warning() -> None:
    vault = vault_with_two_characters()
    holding = vault.locate("Cloak of Flames")[0]
    assert holding.where(T0) == "Xantik · Back"


def test_staleness_is_measured_against_the_capture_time() -> None:
    record = CharacterInventory("Xantik", "green", T0 - STALE_AFTER - timedelta(hours=1), [])
    assert record.is_stale(T0)
    fresh = CharacterInventory("Xantik", "green", T0 - timedelta(days=1), [])
    assert not fresh.is_stale(T0)


def test_characters_are_listed_most_recently_dumped_first() -> None:
    vault = vault_with_two_characters()
    assert [record.character for record in vault.characters()] == ["Xantik", "Mulebank"]


# --- persistence -------------------------------------------------------------


def test_vault_round_trips_through_storage() -> None:
    vault = vault_with_two_characters()
    restored = InventoryVault.from_dict(vault.to_dict())
    assert len(restored) == 2
    holding = restored.locate("Cloak of Flames")[0]
    assert holding.character == "Xantik"
    assert holding.item.location == "Back"
    assert holding.captured_at == T0


def test_how_a_dump_arrived_survives_storage() -> None:
    vault = InventoryVault()
    vault.put("Xantik", "green", [item("Fungi Tunic", 2735)], captured_at=T0, origin=ORIGIN_HOST)
    restored = InventoryVault.from_dict(vault.to_dict())
    assert restored.get("Xantik", "green").origin == ORIGIN_HOST


def test_a_stored_dump_with_no_origin_was_loaded_by_hand() -> None:
    # Storage written before the host had a dump library. There is no guesswork
    # in it: the file dialog was the only way a dump could get in.
    restored = InventoryVault.from_dict(
        {
            "xantik@green": {
                "character": "Xantik",
                "server": "green",
                "captured_at": T0.isoformat(),
                "items": [],
            }
        }
    )
    assert restored.get("Xantik", "green").origin == ORIGIN_MANUAL


def test_malformed_storage_is_skipped_not_fatal() -> None:
    restored = InventoryVault.from_dict(
        {
            "broken@green": {"character": "Broken", "captured_at": "nonsense", "items": []},
            "nolist@green": "not a dict",
            "ok@green": {
                "character": "Ok",
                "server": "green",
                "captured_at": T0.isoformat(),
                "items": [
                    {"name": "Good", "item_id": 1, "location": "Back", "count": 1, "slots": 0},
                    {"name": "Bad", "item_id": "not an int"},
                    "not a dict",
                ],
            },
        }
    )
    assert len(restored) == 1
    assert [holding.name for holding in restored.holdings()] == ["Good"]


def test_non_dict_storage_yields_an_empty_vault() -> None:
    assert len(InventoryVault.from_dict(None)) == 0
    assert len(InventoryVault.from_dict("nope")) == 0


# --- filename fallback -------------------------------------------------------


def test_character_name_can_be_read_off_the_dump_filename() -> None:
    # The dump is often loaded with EQ closed, or while parked on a mule.
    assert character_from_filename("Xantik-Inventory.txt") == "Xantik"
    assert character_from_filename("/tmp/Mulebank-Inventory.txt") == "Mulebank"


def test_an_unexpected_filename_falls_back_to_the_stem() -> None:
    assert character_from_filename("whatever.txt") == "whatever"
