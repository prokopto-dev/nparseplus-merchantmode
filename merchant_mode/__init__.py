"""Merchant Mode — inventory into linkable WTS auction macros, for nParse+.

Load an ``/outputfile inventory`` dump, pick what you're selling, set prices,
and export a macro pack the host's Macro Editor imports. The item ids come out
of the dump itself, so the links are forged from the game's own answer rather
than from a lookup table that might be stale.

**The plugin never sends anything.** It builds macros; the human presses the
button. There is no keystroke simulation anywhere in this package — P99 bans
it, and the feature isn't worth the account.

Threading, per the SDK contract: :meth:`activate` runs on the GUI thread before
the log driver starts; the ``CommsEvent`` subscription and the tick run on the
driver thread; the window reads a locked snapshot on a QTimer. Network I/O only
ever happens inside ``ctx.submit``.

Qt is imported nowhere at this level — only inside the window factories — so
this module stays importable for ``nparseplus-plugin validate``, for CI, and
for the unit tests, none of which have PySide6 or the host app.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from nparseplus_sdk import (
    NParsePlugin,
    PluginContext,
    PluginMeta,
    PluginSettingsPageSpec,
    PluginWindowSpec,
)

from .auctions import PriceHistory
from .catalog import ItemCatalog
from .inventory import (
    CharacterInventory,
    Holding,
    InventoryItem,
    InventoryVault,
    character_from_filename,
    parse_inventory_file,
    sellable,
)
from .macros import DEFAULT_MAX_SOCIALS, DEFAULT_PREFIX, BuildResult, Listing, build_wts_socials
from .nicknames import NicknameTable
from .pricing import Side, Suggestion, suggest
from .socialpack import DEFAULT_PAUSE_TENTHS, MAX_PAUSE_TENTHS, build_pack, write_pack

__all__ = ["MerchantModePlugin", "create_plugin"]

SCHEMA_VERSION = 2
"""Storage layout. v1 kept no inventories; v2 retains one dump per character."""

DEFAULT_POLL_SECONDS = 600
MIN_POLL_SECONDS = 60
MAX_PRICED_NAMES = 40
"""Cap on names sent to PigParse in one call — cadence courtesy."""


def _dump_mtime(path: Path | str) -> datetime:
    """When the dump was written — the honest answer for 'how old is this?'.

    The file's own mtime beats "now": a dump loaded today may have been written
    last week, and the location it reports is only as fresh as the write.
    """
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).replace(microsecond=0)
    except OSError:
        return datetime.now().replace(microsecond=0)


def _watch_auctions(ctx: PluginContext, plugin: MerchantModePlugin) -> None:
    """Subscribe to the auction channel. Host-only; caller guards ImportError."""
    from nparseplus_sdk.events import CommsChannel, CommsEvent

    def on_comms(event: Any) -> None:
        if event.channel != CommsChannel.AUCTION:
            return
        plugin.observe_auction(
            event.content,
            timestamp=event.timestamp,
            sender=event.sender,
        )

    ctx.subscribe(CommsEvent, on_comms)


def _watch_character(ctx: PluginContext, plugin: MerchantModePlugin) -> None:
    """Notice when the log driver switches to a different character.

    The host publishes these either side of ``ActivePlayer.reset_for``, so
    ``ctx.player`` still names the outgoing character on Before and the
    incoming one on After. Nothing needs saving on the switch — inventories are
    keyed by character and listings carry their holder — but the window should
    redraw, and it is worth a log line when a merchant is juggling mules.
    """
    from nparseplus_sdk.events import AfterPlayerChangedEvent

    def on_after(_event: Any) -> None:
        plugin.note_character_change()

    ctx.subscribe(AfterPlayerChangedEvent, on_after)


class MerchantModePlugin(NParsePlugin):
    meta = PluginMeta(
        id="merchant-mode",
        name="Merchant Mode",
        version="0.1.0",
        description=(
            "Turn your inventory into linkable WTS auction macros, and track "
            "prices on both sides of the trade."
        ),
        author="prokopto-dev",
        homepage="https://github.com/prokopto-dev/nparseplus-merchantmode",
        requires_sdk=">=1.0,<2",
        # The version this was actually built and verified against. The plugin
        # reads ``ctx.player.server_key`` and subscribes to
        # ``AfterPlayerChangedEvent``; rather than guess how far back those go,
        # claim only what has been tested and let the host block older installs
        # instead of letting them fail at runtime.
        min_app_version="1.18.0",
    )

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        # The driver thread writes; the GUI thread reads via snapshot().
        self._lock = threading.Lock()
        self._version = 0
        self._vault = InventoryVault()
        self._listings: list[Listing] = []
        self._nicknames = NicknameTable()
        self._catalog = ItemCatalog()
        self._history = PriceHistory()
        self._averages: dict[str, int] = {}
        self._wanted: list[str] = []
        self._pause_tenths = DEFAULT_PAUSE_TENTHS
        self._poll_seconds = DEFAULT_POLL_SECONDS
        self._max_socials = DEFAULT_MAX_SOCIALS
        self._abbreviate = True
        self._prefix = DEFAULT_PREFIX
        self._last_poll: datetime | None = None

    # --- lifecycle ---------------------------------------------------------
    def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._restore(ctx.storage.load())

        # Register everything that needs nothing from the host FIRST. Anything
        # after the guarded import below is skipped when only the SDK is
        # installed — which is exactly how CI and the validator run.
        ctx.add_window(
            PluginWindowSpec(
                key="merchant",
                title="Merchant Mode",
                factory=self._make_window,
                # Wide enough for the Sell tab's five columns without eliding
                # item names — the location column carries a character name too.
                default_geometry=(220, 220, 660, 420),
            )
        )
        ctx.add_settings_page(
            PluginSettingsPageSpec(
                title="Merchant Mode",
                builder=self._build_settings_page,
                apply=self._apply_settings_page,
            )
        )
        ctx.add_tick(self._tick)

        try:
            _watch_auctions(ctx, self)
            _watch_character(ctx, self)
        except ImportError:
            ctx.logger.warning("host events unavailable (standalone run); price tracking is inert")

    def deactivate(self) -> None:
        self._persist()

    # --- inventory ---------------------------------------------------------
    def load_dump(
        self,
        path,
        *,
        character: str = "",
        server: str = "",
        captured_at: datetime | None = None,
    ) -> int:
        """Record one character's dump. Returns how many sellable items it held.

        Dumps accumulate rather than replace: a merchant advertises for the
        whole account, so every character's inventory stays available. Loading
        the same character again replaces just that character's entry.

        ``character`` defaults to whoever is logged in, falling back to the
        ``<Character>-Inventory.txt`` filename — the dump is often loaded while
        parked on the mule, or with EQ closed entirely.
        """
        items = sellable(parse_inventory_file(path))
        if not items:
            return 0
        who = character.strip() or self._active_character() or character_from_filename(path)
        where = server.strip() or self._active_server()
        stamp = captured_at or _dump_mtime(path)
        with self._lock:
            self._vault.put(who, where, items, captured_at=stamp)
            for item in items:
                self._catalog.learn_owned(item.name, item.item_id)
            self._version += 1
        self._persist()
        return len(items)

    def forget_character(self, character: str, server: str = "") -> None:
        """Drop one character's dump and any listings that referenced it."""
        with self._lock:
            self._vault.drop(character, server or self._active_server())
            self._listings = [
                listing
                for listing in self._listings
                if listing.character.casefold() != character.strip().casefold()
            ]
            self._version += 1
        self._persist()

    def holdings(self) -> list[Holding]:
        """Every sellable item across every character that has been dumped."""
        with self._lock:
            return self._vault.holdings()

    def inventories(self) -> list[CharacterInventory]:
        with self._lock:
            return self._vault.characters()

    def locate(self, name: str) -> list[Holding]:
        """Which characters hold ``name``, and where."""
        with self._lock:
            return self._vault.locate(name)

    def items(self) -> list[InventoryItem]:
        """Flat item list across all characters (kept for convenience)."""
        return [holding.item for holding in self.holdings()]

    def _active_character(self) -> str:
        player = getattr(self._ctx, "player", None)
        return (getattr(player, "name", "") or "").strip()

    def _active_server(self) -> str:
        player = getattr(self._ctx, "player", None)
        return (getattr(player, "server_key", None) or "") if player is not None else ""

    # --- macro building ----------------------------------------------------
    def build(self, listings: list[Listing] | None = None) -> BuildResult:
        """Pack listings into socials using the current settings."""
        with self._lock:
            chosen = list(listings if listings is not None else self._listings)
            table, pause = self._nicknames, self._pause_tenths
            prefix, abbreviate, max_socials = self._prefix, self._abbreviate, self._max_socials
        return build_wts_socials(
            chosen,
            nicknames=table,
            abbreviate=abbreviate,
            prefix=prefix,
            pause_tenths=pause,
            max_socials=max_socials,
        )

    def export_pack(self, listings: list[Listing] | None = None, *, label: str = ""):
        """Build and write a macro pack. Returns ``(path, result)``."""
        assert self._ctx is not None
        result = self.build(listings)
        pack = build_pack(result.socials, label=label)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self._ctx.storage.data_dir / "packs" / f"wts-{stamp}.json"
        write_pack(pack, path)
        self._ctx.logger.info("wrote macro pack %s (%d socials)", path, len(result.socials))
        return path, result

    # --- price tracking (driver thread) ------------------------------------
    def observe_auction(self, content: str, *, timestamp: datetime, sender: str = "") -> None:
        with self._lock:
            added = self._history.record(content, timestamp=timestamp, sender=sender)
            if added:
                self._version += 1

    def _tick(self, now: datetime) -> None:
        """Cheap due-check only — real work goes to ctx.submit.

        The tick budget is 250 ms and two consecutive breaches evict this
        callback for the rest of the session, so nothing expensive belongs here.
        """
        ctx = self._ctx
        if ctx is None:
            return
        with self._lock:
            due = self._last_poll is None or (
                (now - self._last_poll).total_seconds() >= self._poll_seconds
            )
            names = self._pending_price_names()
        if not due or not names:
            return

        player = getattr(ctx, "player", None)
        server = getattr(player, "server", None)
        if server is None:
            return
        self._last_poll = now
        api = ctx.pigparse
        server_int = int(server)
        ctx.submit(lambda: api.item_prices(server_int, names), self._apply_prices)

    def _pending_price_names(self) -> list[str]:
        """Names worth pricing. Caller holds the lock."""
        seen: dict[str, str] = {}
        for name in self._wanted:
            seen.setdefault(name.casefold(), name)
        for listing in self._listings:
            seen.setdefault(listing.name.casefold(), listing.name)
        for name in self._history.names():
            seen.setdefault(name.casefold(), name)
        return list(seen.values())[:MAX_PRICED_NAMES]

    def _apply_prices(self, prices: Any) -> None:
        """Runs back on the driver thread with whatever PigParse returned."""
        if not prices:
            return
        with self._lock:
            for record in prices:
                name = getattr(record, "item_name", "") or ""
                if not name:
                    continue
                average = getattr(record, "total_wts_last_6_months_average", 0) or 0
                if average:
                    self._averages[name.casefold()] = int(average)
                self._catalog.learn_remote(name, getattr(record, "eq_item_id", None))
            self._version += 1
        self._persist()

    # --- price suggestions -------------------------------------------------
    def suggest_price(self, name: str, *, side: Side = Side.SELL) -> Suggestion:
        """Best known price for ``name``, with the source it came from."""
        with self._lock:
            return suggest(name, history=self._history, averages=self._averages, side=side)

    def suggest_prices(self, names: list[str], *, side: Side = Side.SELL) -> dict[str, Suggestion]:
        with self._lock:
            return {
                name: suggest(name, history=self._history, averages=self._averages, side=side)
                for name in names
            }

    def fill_prices(self, *, overwrite: bool = False) -> int:
        """Push known prices onto the current listings. Returns how many changed.

        Only fills blanks unless ``overwrite``: a price the seller typed is a
        decision, and a lookup shouldn't quietly overrule it.
        """
        with self._lock:
            filled: list[Listing] = []
            changed = 0
            for listing in self._listings:
                if listing.price.strip() and not overwrite:
                    filled.append(listing)
                    continue
                proposal = suggest(
                    listing.name, history=self._history, averages=self._averages, side=Side.SELL
                )
                if not proposal.known or proposal.text == listing.price:
                    filled.append(listing)
                    continue
                filled.append(replace(listing, price=proposal.text))
                changed += 1
            if changed:
                self._listings = filled
                self._version += 1
        if changed:
            self._persist()
        return changed

    # --- snapshot for the window (GUI thread) ------------------------------
    def snapshot(self) -> dict:
        """A consistent read of everything the window draws."""
        with self._lock:
            holdings = self._vault.holdings()
            return {
                "version": self._version,
                "holdings": holdings,
                "items": [holding.item for holding in holdings],
                "inventories": self._vault.characters(),
                "listings": list(self._listings),
                "wanted": list(self._wanted),
                "averages": dict(self._averages),
                "history": self._history.recent(limit=100),
                "conflicts": self._catalog.conflicts(),
                "pause_tenths": self._pause_tenths,
                "abbreviate": self._abbreviate,
            }

    def resolve_id(self, name: str):
        with self._lock:
            return self._catalog.resolve(name)

    def set_listings(self, listings: list[Listing]) -> None:
        with self._lock:
            self._listings = list(listings)
            self._version += 1
        self._persist()

    def set_wanted(self, names: list[str]) -> None:
        with self._lock:
            self._wanted = [name.strip() for name in names if name.strip()]
            self._version += 1
        self._persist()

    def nicknames(self) -> NicknameTable:
        with self._lock:
            return self._nicknames

    def note_character_change(self) -> None:
        """The active character changed; nudge the window to redraw."""
        who = self._active_character()
        with self._lock:
            self._version += 1
        if self._ctx is not None and who:
            self._ctx.logger.info("active character is now %s", who)

    def active_character(self) -> str:
        return self._active_character()

    # --- persistence -------------------------------------------------------
    def _restore(self, stored: dict) -> None:
        with self._lock:
            self._pause_tenths = self._clamp_pause(stored.get("pause_tenths", DEFAULT_PAUSE_TENTHS))
            self._poll_seconds = max(
                MIN_POLL_SECONDS, _as_int(stored.get("poll_seconds"), DEFAULT_POLL_SECONDS)
            )
            self._max_socials = max(1, _as_int(stored.get("max_socials"), DEFAULT_MAX_SOCIALS))
            self._abbreviate = bool(stored.get("abbreviate", True))
            self._prefix = str(stored.get("prefix") or DEFAULT_PREFIX)
            self._wanted = [str(name) for name in stored.get("wanted", []) if str(name).strip()]
            nicknames = stored.get("nicknames")
            if isinstance(nicknames, dict):
                self._nicknames = NicknameTable({str(k): str(v) for k, v in nicknames.items()})
            self._catalog = ItemCatalog.from_dict(stored.get("catalog"))
            self._history = PriceHistory.from_list(stored.get("history"))
            averages = stored.get("averages")
            if isinstance(averages, dict):
                self._averages = {
                    str(k).casefold(): value
                    for k, v in averages.items()
                    if (value := _as_int(v, 0))
                }
            self._listings = [
                Listing(
                    item_id=int(row["item_id"]),
                    name=str(row["name"]),
                    price=str(row.get("price", "")),
                    character=str(row.get("character", "")),
                )
                for row in stored.get("listings", [])
                if isinstance(row, dict) and _as_int(row.get("item_id"), 0) > 0 and row.get("name")
            ]
            # v1 stored no inventories at all — dumps were in-memory only. An
            # upgrading user keeps their listings and price history and simply
            # re-dumps; there is nothing to salvage that a re-dump won't beat.
            self._vault = InventoryVault.from_dict(stored.get("inventories"))

    def _persist(self) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        with self._lock:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "pause_tenths": self._pause_tenths,
                "poll_seconds": self._poll_seconds,
                "max_socials": self._max_socials,
                "abbreviate": self._abbreviate,
                "prefix": self._prefix,
                "wanted": list(self._wanted),
                "nicknames": self._nicknames.to_dict(),
                "catalog": self._catalog.to_dict(),
                "history": self._history.to_list(),
                "averages": dict(self._averages),
                "inventories": self._vault.to_dict(),
                "listings": [
                    {
                        "item_id": item.item_id,
                        "name": item.name,
                        "price": item.price,
                        "character": item.character,
                    }
                    for item in self._listings
                ],
            }
        ctx.storage.save(payload)

    @staticmethod
    def _clamp_pause(value: object) -> int:
        return max(0, min(MAX_PAUSE_TENTHS, _as_int(value, DEFAULT_PAUSE_TENTHS)))

    # --- settings ----------------------------------------------------------
    def settings(self) -> dict:
        with self._lock:
            return {
                "pause_tenths": self._pause_tenths,
                "poll_seconds": self._poll_seconds,
                "max_socials": self._max_socials,
                "abbreviate": self._abbreviate,
                "prefix": self._prefix,
            }

    def apply_settings(self, values: dict) -> None:
        with self._lock:
            if "pause_tenths" in values:
                self._pause_tenths = self._clamp_pause(values["pause_tenths"])
            if "poll_seconds" in values:
                self._poll_seconds = max(
                    MIN_POLL_SECONDS, _as_int(values["poll_seconds"], self._poll_seconds)
                )
            if "max_socials" in values:
                self._max_socials = max(1, _as_int(values["max_socials"], self._max_socials))
            if "abbreviate" in values:
                self._abbreviate = bool(values["abbreviate"])
            if "prefix" in values:
                self._prefix = str(values["prefix"]) or DEFAULT_PREFIX
            self._version += 1
        self._persist()

    # --- GUI contributions (Qt imported lazily: it only exists in the app) --
    def _make_window(self, wctx: Any) -> Any:
        from .window import MerchantModeWindow

        return MerchantModeWindow(wctx, self)

    def _build_settings_page(self, parent: Any) -> Any:
        from .window import build_settings_page

        return build_settings_page(parent, self.settings())

    def _apply_settings_page(self, page: Any) -> None:
        from .window import read_settings_page

        self.apply_settings(read_settings_page(page))


def _as_int(value: object, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def create_plugin() -> MerchantModePlugin:
    return MerchantModePlugin()
