"""merchant_mode.inventory — the /outputfile inventory dump parser."""

from __future__ import annotations

from merchant_mode.inventory import (
    InventoryItem,
    parse_inventory_file,
    parse_inventory_text,
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
