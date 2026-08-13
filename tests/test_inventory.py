"""merchant_mode.inventory — the /outputfile inventory dump parser."""

from __future__ import annotations

import json

from merchant_mode.inventory import (
    ORIGIN_HOST,
    ORIGIN_MANUAL,
    InventoryItem,
    origin_label,
    parse_inventory_file,
    parse_inventory_text,
    parse_snapshot_document,
    parse_snapshot_file,
    sellable,
)

# Shaped after the host's own fixture: a worn item, a bagged item, an unknown
# location, a container, and a row with unparseable numbers.
DUMP = "\n".join(
    (
        "Location\tName\tID\tCount\tSlots",
        "Charm\tGuise of the Deceiver\t1234\t1\t0",
        "General1-Slot1\tRusty Sword\t5678\t1\t0",
        "Mystery-Spot\tWeird Thing\t1\t1\t0",
        "General2\tLarge Bag\t17969\t1\t8",
        "Bad\tRow\tx\ty\tz",
    )
)


def test_parses_every_well_formed_row() -> None:
    items = parse_inventory_text(DUMP)
    assert [item.name for item in items] == [
        "Guise of the Deceiver",
        "Rusty Sword",
        "Weird Thing",
        "Large Bag",
    ]


def test_carries_the_item_id_that_makes_links_possible() -> None:
    # The whole point of ingesting a dump: authoritative ids, no lookup.
    by_name = {item.name: item.item_id for item in parse_inventory_text(DUMP)}
    assert by_name["Guise of the Deceiver"] == 1234
    assert by_name["Large Bag"] == 17969


def test_rows_with_unparseable_numbers_are_skipped_not_fatal() -> None:
    assert all(item.name != "Row" for item in parse_inventory_text(DUMP))


def test_bag_locations_are_hyphenated_and_normalize_like_the_host() -> None:
    bagged = next(item for item in parse_inventory_text(DUMP) if item.name == "Rusty Sword")
    assert bagged.location == "General1-Slot1"
    assert bagged.location_key == "general1slot1"
    assert bagged.container == "General1"


def test_worn_slots_have_no_container() -> None:
    charm = next(item for item in parse_inventory_text(DUMP) if item.name.startswith("Guise"))
    assert charm.container == ""
    assert not charm.is_container


def test_slots_column_marks_containers() -> None:
    bag = next(item for item in parse_inventory_text(DUMP) if item.name == "Large Bag")
    assert bag.slots == 8
    assert bag.is_container


def test_count_column_is_the_stack_size() -> None:
    text = "Location\tName\tID\tCount\tSlots\nGeneral1\tBone Chips\t13073\t14\t0"
    assert parse_inventory_text(text)[0].count == 14


def test_rejects_text_that_is_not_a_dump() -> None:
    assert parse_inventory_text("hello\nworld") == []
    assert parse_inventory_text("") == []


def test_rejects_a_header_only_dump() -> None:
    assert parse_inventory_text("Location\tName\tID\tCount\tSlots") == []


def test_rejects_a_dump_with_the_wrong_header() -> None:
    text = "Slot\tItem\tId\tCount\tSlots\nCharm\tThing\t1\t1\t0"
    assert parse_inventory_text(text) == []


def test_sellable_drops_rows_that_cannot_become_a_link() -> None:
    # An id of 0 would encode a valid-looking body pointing at nothing.
    items = [
        InventoryItem(location="Charm", name="Real Thing", item_id=42, count=1, slots=0),
        InventoryItem(location="General1", name="Empty", item_id=0, count=0, slots=0),
        InventoryItem(location="General2", name="", item_id=7, count=1, slots=0),
        InventoryItem(location="General3", name="Zero Id", item_id=0, count=1, slots=0),
    ]
    assert [item.name for item in sellable(items)] == ["Real Thing"]


def test_sellable_keeps_everything_legitimate() -> None:
    items = parse_inventory_text(DUMP)
    assert len(sellable(items)) == len(items)


def test_missing_file_yields_no_items(tmp_path) -> None:
    assert parse_inventory_file(tmp_path / "nope.txt") == []


def test_reads_a_dump_from_disk(tmp_path) -> None:
    path = tmp_path / "Xantik-Inventory.txt"
    path.write_text(DUMP, encoding="utf-8")
    assert len(parse_inventory_file(path)) == 4


def test_tolerates_undecodable_bytes(tmp_path) -> None:
    path = tmp_path / "Xantik-Inventory.txt"
    path.write_bytes(DUMP.encode("utf-8") + b"\nGeneral3\t\xff\xfe\t99\t1\t0")
    items = parse_inventory_file(path)
    assert len(items) == 5


# --- the host's stored snapshots ---------------------------------------------
#
# Shaped after nParse+ 2.1.0's CharacterDump document: the same five fields the
# tab-separated dump has, under the host's own names.

SNAPSHOT = {
    "schema_version": 1,
    "character": "Xantik",
    "server": "",
    "kind": "inventory",
    "captured_at": "2026-07-30T20:00:00",
    "source_file": "/eq/Xantik-Inventory.txt",
    "digest": "abc123",
    "items": [
        {
            "location": 0,
            "location_name": "Charm",
            "name": "Guise of the Deceiver",
            "item_id": 1234,
            "count": 1,
            "slots": 0,
        },
        {
            "location": 22,
            "location_name": "General1-Slot1",
            "name": "Bone Chips",
            "item_id": 13073,
            "count": 14,
            "slots": 0,
        },
    ],
    "spells": [],
}


def test_a_snapshot_reads_into_the_same_rows_a_dump_file_does() -> None:
    items = parse_snapshot_document(SNAPSHOT)
    assert [item.name for item in items] == ["Guise of the Deceiver", "Bone Chips"]
    assert items[0].item_id == 1234
    assert items[1].count == 14


def test_the_snapshot_location_keeps_the_dash_the_ordinal_cannot_spell() -> None:
    # ``location`` is a wire ordinal; ``location_name`` is the file's own label.
    bagged = parse_snapshot_document(SNAPSHOT)[1]
    assert bagged.location == "General1-Slot1"
    assert bagged.container == "General1"


def test_a_snapshot_written_by_a_newer_host_is_refused_rather_than_half_read() -> None:
    # The same field names could mean other things by then, and a bag slot read
    # out of a shape that changed is worse than no row at all.
    assert parse_snapshot_document({**SNAPSHOT, "schema_version": 2}) == []


def test_snapshot_rows_that_do_not_parse_are_skipped_not_fatal() -> None:
    rows = [SNAPSHOT["items"][0], {"name": "No Id"}, "not a row", {"item_id": 5}]
    items = parse_snapshot_document({**SNAPSHOT, "items": rows})
    assert [item.name for item in items] == ["Guise of the Deceiver"]


def test_a_spellbook_snapshot_holds_nothing_to_sell() -> None:
    book = {**SNAPSHOT, "kind": "spellbook", "items": [], "spells": [{"level": 1, "name": "Ward"}]}
    assert parse_snapshot_document(book) == []


def test_anything_that_is_not_a_snapshot_yields_no_items() -> None:
    assert parse_snapshot_document(None) == []
    assert parse_snapshot_document([1, 2, 3]) == []


def test_reads_a_snapshot_from_disk(tmp_path) -> None:
    path = tmp_path / "20260730-200000-abc123.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    assert len(parse_snapshot_file(path)) == 2


def test_a_missing_or_unparseable_snapshot_yields_no_items(tmp_path) -> None:
    assert parse_snapshot_file(tmp_path / "gone.json") == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert parse_snapshot_file(broken) == []


def test_every_origin_has_a_label_and_an_unknown_one_admits_it() -> None:
    # A store written by a later build must not have its dumps rounded down to
    # "by hand" — that would be a claim about provenance nothing supports.
    assert origin_label(ORIGIN_MANUAL) == "By hand"
    assert origin_label(ORIGIN_HOST) == "Automatic"
    assert origin_label("teleported in") == "Unknown"
