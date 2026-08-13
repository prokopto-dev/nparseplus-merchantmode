"""The host's events, as the host actually publishes them.

Everything else about dump ingest is tested against a snapshot written by hand,
because CI installs the SDK alone and the event classes only resolve inside the
app. That leaves one thing untested there and untestable anywhere else: whether
the fields this plugin reads off an event are the fields nParse+ puts on it. A
rename would pass CI, pass ``nparseplus-plugin validate``, and then do nothing
at all in the app — the failure mode :mod:`tests.test_imports` exists for.

Skipped without the app, like ``tests/test_window.py``. Run locally with:

    uv pip install -e /path/to/nparse-plus
    pytest tests/test_host_events.py
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

pytest.importorskip("nparseplus", reason="host app not installed")
pytest.importorskip(
    "nparseplus.core.dumps", reason="host predates the Character Dumps library (2.1.0)"
)

from nparseplus_sdk.events import CharacterDumpImportedEvent, CharacterDumpUpdatedEvent
from nparseplus_sdk.testing import FakePluginContext

from merchant_mode import MerchantModePlugin, create_plugin
from merchant_mode.inventory import ORIGIN_HOST

T0 = datetime(2026, 7, 30, 20, 0, 0)


def _library_snapshot(tmp_path, character: str = "Xantik", digest: str = "abc123"):
    """A snapshot written by the host's own store, not by this test.

    The point of the exercise: ``CharacterDump.model_dump_json`` decides the
    field names, so building the document any other way would test this repo
    against itself.
    """
    from nparseplus.core.dumps.models import CharacterDump, DumpKind, InventoryEntry
    from nparseplus.core.dumps.store import DumpLibrary

    dump = CharacterDump(
        character=character,
        kind=DumpKind.INVENTORY,
        captured_at=T0,
        source_file=f"/eq/{character}-Inventory.txt",
        digest=digest,
        items=[
            InventoryEntry(
                location=0,
                location_name="Charm",
                name="Guise of the Deceiver",
                item_id=1234,
                count=1,
                slots=0,
            ),
            InventoryEntry(
                location=22,
                location_name="General1-Slot1",
                name="Manastone",
                item_id=4567,
                count=1,
                slots=0,
            ),
        ],
    )
    ref = DumpLibrary(tmp_path).store(dump)
    assert ref is not None
    return dump, ref


def _deliver(ctx, event) -> None:
    """Hand an event to every subscription that asked for its type."""
    handled = 0
    for event_type, fn in ctx.subscriptions:
        if isinstance(event, event_type):
            fn(event)
            handled += 1
    assert handled == 1, f"{type(event).__name__} reached {handled} subscribers"


def _event(kind, dump, ref, **extra):
    return kind(
        character=dump.character,
        kind=str(dump.kind),
        server=dump.server,
        captured_at=dump.captured_at,
        entry_count=dump.entry_count,
        digest=dump.digest,
        path=str(ref.path),
        source_file=dump.source_file,
        **extra,
    )


def _activated():
    ctx = FakePluginContext(MerchantModePlugin.meta)
    plugin = create_plugin()
    plugin.activate(ctx)
    return plugin, ctx


def test_an_imported_dump_event_puts_the_items_on_the_sell_tab(tmp_path) -> None:
    plugin, ctx = _activated()
    dump, ref = _library_snapshot(tmp_path)

    _deliver(ctx, _event(CharacterDumpImportedEvent, dump, ref))

    assert {holding.name for holding in plugin.holdings()} == {
        "Guise of the Deceiver",
        "Manastone",
    }
    record = plugin.inventories()[0]
    assert record.character == "Xantik"
    assert record.origin == ORIGIN_HOST
    assert record.captured_at == T0
    assert record.source_path == "/eq/Xantik-Inventory.txt"


def test_an_updated_dump_event_is_the_same_news(tmp_path) -> None:
    """Both events say "a dump exists and this is what it says".

    ``added``/``removed`` describe what changed, which is a question the Dumps
    tab answers with ages instead.
    """
    plugin, ctx = _activated()
    dump, ref = _library_snapshot(tmp_path)

    _deliver(ctx, _event(CharacterDumpUpdatedEvent, dump, ref, added=("Manastone",), removed=()))
    assert len(plugin.holdings()) == 2


def test_a_spellbook_event_never_reaches_the_sell_tab(tmp_path) -> None:
    from nparseplus.core.dumps.models import CharacterDump, DumpKind, SpellbookEntry
    from nparseplus.core.dumps.store import DumpLibrary

    plugin, ctx = _activated()
    book = CharacterDump(
        character="Xantik",
        kind=DumpKind.SPELLBOOK,
        captured_at=T0,
        digest="book01",
        spells=[SpellbookEntry(level=1, name="Ward")],
    )
    ref = DumpLibrary(tmp_path).store(book)
    _deliver(ctx, _event(CharacterDumpImportedEvent, book, ref))

    assert plugin.inventories() == []


def test_the_stored_snapshot_still_has_the_fields_this_plugin_reads(tmp_path) -> None:
    """The five item fields, spelled the host's way, in the host's document."""
    _dump, ref = _library_snapshot(tmp_path)
    document = json.loads(ref.path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert set(document) >= {"character", "kind", "captured_at", "digest", "items"}
    assert set(document["items"][0]) >= {
        "location_name",
        "name",
        "item_id",
        "count",
        "slots",
    }
