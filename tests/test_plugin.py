"""The plugin surface, exercised against the SDK's FakePluginContext.

No app, no Qt, no network — the same environment CI and
``nparseplus-plugin validate`` run in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from nparseplus_sdk.testing import FakePluginContext

from merchant_mode import MerchantModePlugin, create_plugin
from merchant_mode.catalog import IdStatus
from merchant_mode.macros import Listing

T0 = datetime(2026, 7, 30, 20, 0, 0)

DUMP = "\n".join(
    (
        "Location\tName\tID\tCount\tSlots",
        "Charm\tGuise of the Deceiver\t1234\t1\t0",
        "Back\tCloak of Flames\t11621\t1\t0",
        "General1\tLarge Bag\t17969\t1\t8",
    )
)


def _host_events_available() -> bool:
    """True when nParse+ itself is installed, not just the SDK.

    ``nparseplus_sdk.events`` re-exports the host's event classes lazily, so it
    only resolves inside the app. CI installs the SDK alone — the plugin has to
    register its window either way.
    """
    try:
        from nparseplus_sdk.events import CommsEvent  # noqa: F401
    except ImportError:
        return False
    return True


def _activated() -> tuple[MerchantModePlugin, FakePluginContext]:
    ctx = FakePluginContext(MerchantModePlugin.meta)
    plugin = create_plugin()
    plugin.activate(ctx)
    return plugin, ctx


# --- metadata and registration ---------------------------------------------


def test_metadata() -> None:
    meta = MerchantModePlugin.meta
    assert meta.id == "merchant-mode"
    assert meta.requires_sdk == ">=1.0,<2"
    assert meta.version


def test_metadata_is_registry_ready() -> None:
    """Every field the registry entry is built from must be populated.

    ``release.yml`` composes ``registry-entry.json`` straight out of these, and
    a blank description or homepage would only be noticed in review — after the
    tag is cut and the sha256 is already pinned.
    """
    import re

    meta = MerchantModePlugin.meta
    assert re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", meta.id)
    assert meta.name and meta.description and meta.author
    assert meta.homepage.startswith("https://")
    # Claim only what has been verified, so older hosts are blocked at install
    # rather than failing somewhere in the middle of a session.
    assert meta.min_app_version


def test_registers_its_window_even_without_the_host() -> None:
    # The window needs nothing from the app, so it must always register.
    _, ctx = _activated()
    assert [spec.key for spec in ctx.windows] == ["merchant"]


def test_registers_a_settings_page_and_a_tick() -> None:
    _, ctx = _activated()
    assert len(ctx.settings_pages) == 1
    assert len(ctx.ticks) == 1


def test_subscribes_to_comms_and_character_changes_when_the_host_is_available() -> None:
    _, ctx = _activated()
    if not _host_events_available():
        assert ctx.subscriptions == []
        return
    subscribed = {event_type.__name__ for event_type, _fn in ctx.subscriptions}
    assert subscribed == {"CommsEvent", "AfterPlayerChangedEvent"}


def test_registers_no_line_parsers() -> None:
    # Chat is already parsed by the host and republished as CommsEvent.
    _, ctx = _activated()
    assert ctx.parsers == []


# --- inventory -------------------------------------------------------------


def test_loading_a_dump_populates_items_and_learns_their_ids(tmp_path) -> None:
    plugin, _ = _activated()
    path = tmp_path / "Xantik-Inventory.txt"
    path.write_text(DUMP, encoding="utf-8")

    assert plugin.load_dump(path) == 3
    assert len(plugin.items()) == 3
    resolved = plugin.resolve_id("Cloak of Flames")
    assert resolved.item_id == 11621
    assert resolved.status is IdStatus.OWNED


def test_loading_a_non_dump_yields_nothing(tmp_path) -> None:
    plugin, _ = _activated()
    path = tmp_path / "notes.txt"
    path.write_text("just some notes", encoding="utf-8")
    assert plugin.load_dump(path) == 0


# --- across characters -----------------------------------------------------

MULE_DUMP = "\n".join(
    (
        "Location\tName\tID\tCount\tSlots",
        "General1-Slot1\tManastone\t4567\t1\t0",
        "General1-Slot2\tRubicite Breastplate\t1234\t1\t0",
    )
)


def _dump(tmp_path, name: str, text: str):
    path = tmp_path / f"{name}-Inventory.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_dumps_from_several_characters_pool_into_one_sellable_list(tmp_path) -> None:
    # A merchant advertises for the whole account, not just who is logged in.
    plugin, _ = _activated()
    plugin.load_dump(_dump(tmp_path, "Xantik", DUMP))
    plugin.load_dump(_dump(tmp_path, "Mulebank", MULE_DUMP))

    names = {holding.name for holding in plugin.holdings()}
    assert "Cloak of Flames" in names
    assert "Manastone" in names
    assert len(plugin.inventories()) == 2


def test_the_character_is_taken_from_the_filename_when_nobody_is_logged_in(tmp_path) -> None:
    plugin, _ = _activated()
    plugin.load_dump(_dump(tmp_path, "Mulebank", MULE_DUMP))
    assert plugin.locate("Manastone")[0].character == "Mulebank"


def test_an_explicit_character_wins_over_the_filename(tmp_path) -> None:
    plugin, _ = _activated()
    plugin.load_dump(_dump(tmp_path, "Xantik", MULE_DUMP), character="Bankalt")
    assert plugin.locate("Manastone")[0].character == "Bankalt"


def test_locate_answers_which_alt_is_holding_it(tmp_path) -> None:
    plugin, _ = _activated()
    plugin.load_dump(_dump(tmp_path, "Mulebank", MULE_DUMP))
    holding = plugin.locate("Rubicite Breastplate")[0]
    assert holding.character == "Mulebank"
    assert holding.item.location == "General1-Slot2"


def test_the_capture_time_comes_from_the_dump_file_not_the_load(tmp_path) -> None:
    # A dump loaded today may have been written last week; the location is only
    # as fresh as the write.
    import os

    plugin, _ = _activated()
    path = _dump(tmp_path, "Mulebank", MULE_DUMP)
    old = (T0 - timedelta(days=30)).timestamp()
    os.utime(path, (old, old))
    plugin.load_dump(path)
    captured = plugin.inventories()[0].captured_at
    assert abs((captured - (T0 - timedelta(days=30))).total_seconds()) < 2


def test_forgetting_a_character_drops_their_items_and_listings(tmp_path) -> None:
    plugin, _ = _activated()
    plugin.load_dump(_dump(tmp_path, "Xantik", DUMP))
    plugin.load_dump(_dump(tmp_path, "Mulebank", MULE_DUMP))
    plugin.set_listings(
        [
            Listing(11621, "Cloak of Flames", "5k", character="Xantik"),
            Listing(4567, "Manastone", "40k", character="Mulebank"),
        ]
    )

    plugin.forget_character("Mulebank")
    assert [listing.name for listing in plugin.snapshot()["listings"]] == ["Cloak of Flames"]
    assert plugin.locate("Manastone") == []


def test_listings_remember_which_character_holds_the_item() -> None:
    plugin, ctx = _activated()
    plugin.set_listings([Listing(11621, "Cloak of Flames", "5k", character="Xantik")])
    plugin.deactivate()

    restored = create_plugin()
    restored.activate(FakePluginContext(MerchantModePlugin.meta, storage=ctx.storage))
    assert restored.snapshot()["listings"][0].character == "Xantik"


# --- price push ------------------------------------------------------------


def test_fill_prices_pushes_observed_prices_onto_blank_listings() -> None:
    plugin, _ = _activated()
    plugin.observe_auction("WTS Cloak of Flames 5k", timestamp=T0)
    plugin.set_listings([Listing(11621, "Cloak of Flames", "")])

    assert plugin.fill_prices() == 1
    assert plugin.snapshot()["listings"][0].price == "5k"


def test_fill_prices_leaves_a_price_you_typed_alone() -> None:
    plugin, _ = _activated()
    plugin.observe_auction("WTS Cloak of Flames 5k", timestamp=T0)
    plugin.set_listings([Listing(11621, "Cloak of Flames", "9k")])

    assert plugin.fill_prices() == 0
    assert plugin.snapshot()["listings"][0].price == "9k"


def test_fill_prices_can_be_told_to_overwrite() -> None:
    plugin, _ = _activated()
    plugin.observe_auction("WTS Cloak of Flames 5k", timestamp=T0)
    plugin.set_listings([Listing(11621, "Cloak of Flames", "9k")])

    assert plugin.fill_prices(overwrite=True) == 1
    assert plugin.snapshot()["listings"][0].price == "5k"


def test_fill_prices_falls_back_to_the_pigparse_average() -> None:
    @dataclass
    class FakePrice:
        item_name: str
        eq_item_id: int
        total_wts_last_6_months_average: int

    plugin, _ = _activated()
    plugin._apply_prices([FakePrice("Manastone", 4567, 42000)])
    plugin.set_listings([Listing(4567, "Manastone", "")])

    assert plugin.fill_prices() == 1
    assert plugin.snapshot()["listings"][0].price == "42k"


def test_fill_prices_leaves_unknown_items_blank() -> None:
    plugin, _ = _activated()
    plugin.set_listings([Listing(11621, "Never Auctioned", "")])
    assert plugin.fill_prices() == 0
    assert plugin.snapshot()["listings"][0].price == ""


def test_suggest_price_reports_its_source() -> None:
    from merchant_mode.pricing import PriceSource

    plugin, _ = _activated()
    plugin.observe_auction("WTS Cloak of Flames 5k", timestamp=T0)
    proposal = plugin.suggest_price("Cloak of Flames")
    assert proposal.text == "5k"
    assert proposal.source is PriceSource.OBSERVED


# --- macro export ----------------------------------------------------------


def test_exporting_writes_a_pack_into_the_plugin_data_dir() -> None:
    plugin, ctx = _activated()
    path, result = plugin.export_pack([Listing(11621, "Cloak of Flames", "5k")])

    assert path.exists()
    assert path.parent == ctx.storage.data_dir / "packs"
    assert len(result.socials) == 1
    assert result.ok


def test_exported_pack_declares_the_socials_format() -> None:
    import json

    plugin, _ = _activated()
    path, _result = plugin.export_pack([Listing(11621, "Cloak of Flames", "5k")])
    pack = json.loads(path.read_text(encoding="utf-8"))
    assert pack["format"] == "nparseplus-socials"
    assert pack["version"] == 1


def test_export_honours_the_configured_pause() -> None:
    plugin, _ = _activated()
    plugin.apply_settings({"pause_tenths": 50})
    listings = [Listing(11621, f"Item Number {n}", "5k") for n in range(8)]
    _path, result = plugin.export_pack(listings)
    assert any("/pause 50" in line for social in result.socials for line in social["lines"])


# --- price tracking --------------------------------------------------------


def test_observing_an_auction_records_priced_offers() -> None:
    plugin, _ = _activated()
    plugin.observe_auction("WTS Cloak of Flames 5k", timestamp=T0, sender="Someone")
    history = plugin.snapshot()["history"]
    assert [obs.name for obs in history] == ["Cloak of Flames"]


def test_the_tick_does_no_network_io_of_its_own() -> None:
    plugin, ctx = _activated()
    plugin.set_wanted(["Manastone"])
    plugin._tick(T0)
    # No player/server on a fake context, so nothing should have been submitted.
    assert ctx.submitted == []


def test_the_tick_submits_a_price_lookup_once_a_server_is_known() -> None:
    @dataclass
    class FakePlayer:
        server: int = 0

    ctx = FakePluginContext(MerchantModePlugin.meta, player=FakePlayer())
    plugin = create_plugin()
    plugin.activate(ctx)
    plugin.set_wanted(["Manastone"])

    plugin._tick(T0)
    assert len(ctx.submitted) == 1  # recorded, not executed — no I/O in the tick

    ctx.run_submitted()
    assert ctx.pigparse.calls == [("item_prices", (0, ["Manastone"]), {})]


def test_the_tick_respects_the_poll_interval() -> None:
    @dataclass
    class FakePlayer:
        server: int = 0

    ctx = FakePluginContext(MerchantModePlugin.meta, player=FakePlayer())
    plugin = create_plugin()
    plugin.activate(ctx)
    plugin.apply_settings({"poll_seconds": 600})
    plugin.set_wanted(["Manastone"])

    plugin._tick(T0)
    plugin._tick(T0 + timedelta(seconds=60))
    assert len(ctx.submitted) == 1

    plugin._tick(T0 + timedelta(seconds=601))
    assert len(ctx.submitted) == 2


def test_applying_prices_records_averages_and_learns_remote_ids() -> None:
    @dataclass
    class FakePrice:
        item_name: str
        eq_item_id: int
        total_wts_last_6_months_average: int

    plugin, _ = _activated()
    plugin._apply_prices([FakePrice("Manastone", 4567, 40000)])

    assert plugin.snapshot()["averages"]["manastone"] == 40000
    resolved = plugin.resolve_id("Manastone")
    assert resolved.item_id == 4567
    assert resolved.status is IdStatus.UNVERIFIED


def test_a_pigparse_id_agreeing_with_the_dump_is_confirmed(tmp_path) -> None:
    @dataclass
    class FakePrice:
        item_name: str
        eq_item_id: int
        total_wts_last_6_months_average: int

    plugin, _ = _activated()
    path = tmp_path / "Xantik-Inventory.txt"
    path.write_text(DUMP, encoding="utf-8")
    plugin.load_dump(path)
    plugin._apply_prices([FakePrice("Cloak of Flames", 11621, 5000)])

    assert plugin.resolve_id("Cloak of Flames").status is IdStatus.CONFIRMED


def test_empty_price_results_are_harmless() -> None:
    plugin, _ = _activated()
    plugin._apply_prices([])
    plugin._apply_prices(None)


# --- settings and persistence ----------------------------------------------


def test_settings_round_trip() -> None:
    plugin, _ = _activated()
    plugin.apply_settings({"pause_tenths": 45, "abbreviate": False, "max_socials": 2})
    settings = plugin.settings()
    assert settings["pause_tenths"] == 45
    assert settings["abbreviate"] is False
    assert settings["max_socials"] == 2


def test_pause_is_clamped_to_the_range_the_client_accepts() -> None:
    plugin, _ = _activated()
    plugin.apply_settings({"pause_tenths": 99999})
    assert plugin.settings()["pause_tenths"] == 999
    plugin.apply_settings({"pause_tenths": -10})
    assert plugin.settings()["pause_tenths"] == 0


def test_nonsense_settings_fall_back_rather_than_raising() -> None:
    plugin, _ = _activated()
    plugin.apply_settings({"pause_tenths": "not a number", "poll_seconds": None})
    assert plugin.settings()["pause_tenths"] == 30


def test_state_survives_a_restart() -> None:
    plugin, ctx = _activated()
    plugin.apply_settings({"pause_tenths": 45})
    plugin.set_wanted(["Manastone"])
    plugin.set_listings([Listing(11621, "Cloak of Flames", "5k")])
    plugin.observe_auction("WTS Guise 100k", timestamp=T0)
    plugin.deactivate()

    restored = create_plugin()
    restored.activate(FakePluginContext(MerchantModePlugin.meta, storage=ctx.storage))
    state = restored.snapshot()
    assert restored.settings()["pause_tenths"] == 45
    assert state["wanted"] == ["Manastone"]
    assert [listing.name for listing in state["listings"]] == ["Cloak of Flames"]
    assert [obs.name for obs in state["history"]] == ["Guise"]


def test_a_v1_store_upgrades_without_losing_anything_it_had() -> None:
    """v1 kept no inventories — dumps were in-memory only.

    Everything it *did* hold must survive; the missing inventories are a
    re-dump away, which beats anything a migration could invent.
    """
    ctx = FakePluginContext(MerchantModePlugin.meta)
    ctx.storage.data = {
        # No schema_version key at all — that is what v1 looks like.
        "pause_tenths": 45,
        "wanted": ["Manastone"],
        "nicknames": {"cloak of flames": "CoF"},
        "catalog": {"owned": {"cloak of flames": 11621}, "remote": {}, "names": {}},
        "history": [
            {"timestamp": T0.isoformat(), "name": "Manastone", "price": 40000, "wanted": False}
        ],
        "averages": {"manastone": 41000},
        "listings": [{"item_id": 11621, "name": "Cloak of Flames", "price": "5k"}],
    }
    plugin = create_plugin()
    plugin.activate(ctx)

    state = plugin.snapshot()
    assert plugin.settings()["pause_tenths"] == 45
    assert state["wanted"] == ["Manastone"]
    assert [listing.name for listing in state["listings"]] == ["Cloak of Flames"]
    assert [obs.name for obs in state["history"]] == ["Manastone"]
    assert plugin.resolve_id("Cloak of Flames").item_id == 11621
    # A v1 listing has no holder yet; it gains one when that character re-dumps.
    assert state["listings"][0].character == ""
    assert state["inventories"] == []


def test_saving_stamps_the_current_schema_version() -> None:
    from merchant_mode import SCHEMA_VERSION

    plugin, ctx = _activated()
    plugin.set_wanted(["Manastone"])
    assert ctx.storage.data["schema_version"] == SCHEMA_VERSION == 3


def test_inventories_survive_a_restart(tmp_path) -> None:
    plugin, ctx = _activated()
    plugin.load_dump(_dump(tmp_path, "Mulebank", MULE_DUMP))
    plugin.deactivate()

    restored = create_plugin()
    restored.activate(FakePluginContext(MerchantModePlugin.meta, storage=ctx.storage))
    assert [record.character for record in restored.inventories()] == ["Mulebank"]
    assert restored.locate("Manastone")[0].item.location == "General1-Slot1"


def test_corrupt_storage_does_not_prevent_activation() -> None:
    ctx = FakePluginContext(MerchantModePlugin.meta)
    ctx.storage.data = {
        "pause_tenths": "nonsense",
        "listings": "not a list",
        "catalog": 42,
        "history": {"nope": True},
        "nicknames": ["wrong shape"],
    }
    plugin = create_plugin()
    plugin.activate(ctx)
    assert plugin.settings()["pause_tenths"] == 30
    assert plugin.snapshot()["listings"] == []


def test_snapshot_version_advances_when_state_changes() -> None:
    plugin, _ = _activated()
    before = plugin.snapshot()["version"]
    plugin.set_wanted(["Manastone"])
    assert plugin.snapshot()["version"] > before


def test_deactivate_persists_without_a_context() -> None:
    # deactivate() may run before activate() ever did; it must not explode.
    create_plugin().deactivate()


# --- price fetching --------------------------------------------------------
#
# The regression these cover: fetching used to be gated on ctx.player.server,
# which is unset whenever EQ isn't running — i.e. whenever you actually load a
# dump. No call ever went out, both caches stayed empty, and "Fill prices"
# reported that there was nothing to fill. It looked like a broken button and
# was really a fetch that never happened.


class _FakeItemPrice:
    """Shaped like the host's PigParse ``ItemPrice``, for the fields read."""

    def __init__(self, name: str, item_id: int, average: int, *, samples: int = 0) -> None:
        self.item_name = name
        self.eq_item_id = item_id
        self.total_wts_last_30_days_average = 0
        self.total_wts_last_30_days_count = 0
        self.total_wts_last_90_days_average = 0
        self.total_wts_last_90_days_count = 0
        self.total_wts_last_6_months_average = average
        self.total_wts_last_6_months_count = samples
        self.total_wts_auction_average = average
        self.total_wts_auction_count = samples
        self.last_wts_seen = None


def _seeded(tmp_path, *, server: str = "green"):
    plugin, ctx = _activated()
    plugin.load_dump(_dump(tmp_path, "Xantik", DUMP), server=server)
    plugin.set_listings([Listing(11621, "Cloak of Flames"), Listing(1234, "Guise of the Deceiver")])
    return plugin, ctx


def test_prices_are_fetched_with_no_live_session(tmp_path) -> None:
    plugin, ctx = _seeded(tmp_path)
    assert ctx.player is None  # EQ closed: the case that used to fetch nothing

    assert plugin.request_prices(["Cloak of Flames"]) is True
    ctx.pigparse.item_prices = lambda server, names: [
        _FakeItemPrice("Cloak of Flames", 11621, 5000, samples=9)
    ]
    ctx.run_submitted()
    assert plugin.market_for("Cloak of Flames").headline == 5000


def test_fetching_declines_and_says_why_without_a_server(tmp_path) -> None:
    plugin, _ctx = _seeded(tmp_path, server="")
    assert plugin.server() == ""
    assert plugin.request_prices(["Cloak of Flames"]) is False
    assert "server" in plugin.status().casefold()


def test_the_dump_server_reaches_pigparse_as_its_wire_int(tmp_path) -> None:
    plugin, ctx = _seeded(tmp_path, server="blue")
    seen: list = []
    ctx.pigparse.item_prices = lambda server, names: seen.append(server) or []
    plugin.request_prices(["Cloak of Flames"])
    ctx.run_submitted()
    assert seen == [1]  # Server.BLUE — not the string, and not Green's 0


def test_fill_prices_uses_a_fetched_average(tmp_path) -> None:
    plugin, ctx = _seeded(tmp_path)
    ctx.pigparse.item_prices = lambda server, names: [
        _FakeItemPrice("Cloak of Flames", 11621, 5000, samples=9)
    ]
    plugin.request_prices(["Cloak of Flames"])
    ctx.run_submitted()

    assert plugin.fill_prices() == 1
    priced = {listing.name: listing.price for listing in plugin.snapshot()["listings"]}
    assert priced["Cloak of Flames"] == "5k"


def test_unpriced_listings_is_what_the_fill_button_should_go_and_ask_about(tmp_path) -> None:
    plugin, _ctx = _seeded(tmp_path)
    assert set(plugin.unpriced_listings()) == {"Cloak of Flames", "Guise of the Deceiver"}


def test_polling_advances_instead_of_re_asking_for_the_same_names(tmp_path) -> None:
    """The 40-name cap used to be applied to the same list every poll, so an
    inventory larger than the cap never got past its first forty items."""
    from merchant_mode import MAX_PRICED_NAMES

    plugin, ctx = _activated()
    plugin.set_server("green")
    plugin.set_wanted([f"Item Number {n}" for n in range(MAX_PRICED_NAMES + 5)])

    first = plugin._pending_price_names()
    assert len(first) == MAX_PRICED_NAMES

    ctx.pigparse.item_prices = lambda server, names: [
        _FakeItemPrice(name, 1, 100, samples=3) for name in names
    ]
    plugin.request_prices(first)
    ctx.run_submitted()

    second = plugin._pending_price_names()
    assert set(second).isdisjoint(first)
    assert len(second) == 5


# --- name matching ---------------------------------------------------------


def test_auction_nicknames_attach_to_the_item_you_own(tmp_path) -> None:
    """``WTS Fungi 27k`` is a price for Fungus Covered Scale Tunic.

    Matching was exact case-folded equality, so the channel's own shorthand —
    which is what people actually type — never matched anything in a dump.
    """
    from merchant_mode.pricing import PriceSource

    plugin, _ctx = _activated()
    plugin.load_dump(_dump(tmp_path, "Xantik", DUMP), server="green")
    plugin.observe_auction("WTS Guise 88k", timestamp=T0, sender="Someone")

    proposal = plugin.suggest_price("Guise of the Deceiver")
    assert proposal.text == "88k"
    assert proposal.source is PriceSource.OBSERVED


def test_a_typo_in_the_channel_still_matches(tmp_path) -> None:
    plugin, _ctx = _activated()
    plugin.load_dump(_dump(tmp_path, "Xantik", DUMP), server="green")
    plugin.observe_auction("WTS Cloack of Flames 6k", timestamp=T0)
    assert plugin.suggest_price("Cloak of Flames").text == "6k"


def test_an_unrelated_item_does_not_match(tmp_path) -> None:
    plugin, _ctx = _activated()
    plugin.load_dump(_dump(tmp_path, "Xantik", DUMP), server="green")
    plugin.observe_auction("WTS Rusty Long Sword 5pp", timestamp=T0)
    assert not plugin.suggest_price("Cloak of Flames").known


# --- server scoping --------------------------------------------------------


def test_holdings_scope_to_one_server(tmp_path) -> None:
    plugin, _ctx = _activated()
    plugin.load_dump(_dump(tmp_path, "Xantik", DUMP), server="green")
    plugin.load_dump(_dump(tmp_path, "Mulebank", MULE_DUMP), server="blue")

    assert plugin.dumped_servers() == ["blue", "green"] or plugin.dumped_servers() == [
        "green",
        "blue",
    ]
    assert plugin.characters_on("green") == ["Xantik"]
    assert plugin.characters_on("blue") == ["Mulebank"]
    assert {holding.name for holding in plugin.holdings(server="blue")} == {
        "Manastone",
        "Rubicite Breastplate",
    }
    # No filter still means everything — the UI just never asks for that.
    assert len(plugin.holdings()) == 5


def test_a_dump_with_no_server_stays_visible_in_its_own_bucket(tmp_path) -> None:
    """Unfiled dumps must be reachable, not silently absent.

    A dump loaded before any server was chosen has ``server == ""``. Filtering
    it out would make a successful load look like a failed one.
    """
    plugin, _ctx = _activated()
    plugin.load_dump(_dump(tmp_path, "Xantik", DUMP), server="")
    assert plugin.dumped_servers() == [""]
    assert len(plugin.holdings(server="")) == 3


# --- export ----------------------------------------------------------------


def test_exporting_writes_where_it_is_told(tmp_path) -> None:
    plugin, _ctx = _activated()
    target = tmp_path / "somewhere" / "my-macros.json"
    path, _result = plugin.export_pack([Listing(11621, "Cloak of Flames", "5k")], path=target)
    assert path == target
    assert target.exists()


def test_v2_averages_survive_the_upgrade_to_v3() -> None:
    """An upgrading user's Fill button must work before the first fetch.

    v2 stored a bare 6-month average per name; v3 stores the whole block. The
    old numbers are still the best thing known until a fetch replaces them.
    """
    ctx = FakePluginContext(MerchantModePlugin.meta)
    ctx.storage.data = {
        "schema_version": 2,
        "averages": {"cloak of flames": 5000},
        "listings": [{"item_id": 11621, "name": "Cloak of Flames", "price": ""}],
    }
    plugin = create_plugin()
    plugin.activate(ctx)

    assert plugin.market_for("Cloak of Flames").headline == 5000
    assert plugin.fill_prices() == 1
    assert plugin.snapshot()["listings"][0].price == "5k"
