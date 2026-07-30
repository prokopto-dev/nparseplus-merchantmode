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
from datetime import datetime, timedelta
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
from .chartdata import PriceChart, build_chart
from .finding import HoldingMatch, find_holdings
from .inventory import (
    STALE_AFTER,
    CharacterInventory,
    Holding,
    InventoryItem,
    InventoryVault,
    character_from_filename,
    parse_inventory_file,
    sellable,
)
from .itemnames import ItemNameIndex
from .macros import DEFAULT_MAX_SOCIALS, DEFAULT_PREFIX, BuildResult, Listing, build_wts_socials
from .market import MarketRecord
from .matching import NameMatcher
from .nicknames import NicknameTable
from .pricing import Side, Suggestion, suggest
from .servers import host_drift, label_for, normalize_key, wire_for
from .socialpack import DEFAULT_PAUSE_TENTHS, MAX_PAUSE_TENTHS, build_pack, write_pack

__all__ = ["MerchantModePlugin", "create_plugin"]

SCHEMA_VERSION = 4
"""Storage layout. v1 kept no inventories; v2 retains one dump per character;
v3 keeps PigParse's whole stats block per item instead of a bare average, and
remembers which server you last dumped on; v4 remembers the file each dump came
from, so a stale one can be reloaded without hunting for it again.

Every version is read by the one after it. A v3 store simply has no source
paths, and its dumps reload through the file dialog the way they always did."""

DEFAULT_POLL_SECONDS = 600
MIN_POLL_SECONDS = 60
DEFAULT_STALE_DAYS = int(STALE_AFTER.total_seconds() // 86400)
MIN_STALE_DAYS = 1
MAX_STALE_DAYS = 90
"""Bounds on the staleness threshold. Seven days is right for a mule that never
moves and far too generous for a main, so the user gets to say — but a
threshold of zero would mark every dump stale the moment it loaded, which is
the same as having no warning at all."""

MAX_PRICED_NAMES = 40
"""Cap on names sent to PigParse in one call — cadence courtesy.

The cap is per *request*, not per session: :meth:`_pending_price_names` skips
names it already has fresh records for, so successive polls walk through a
large inventory instead of re-asking about the same forty forever.
"""


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
        version="0.3.0",
        description=(
            "Turn your inventory into linkable WTS auction macros, find which "
            "mule is holding what, look up what anything is worth, and see how "
            "its price is actually moving."
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
        self._prices: dict[str, MarketRecord] = {}
        self._wanted: list[str] = []
        self._pause_tenths = DEFAULT_PAUSE_TENTHS
        self._poll_seconds = DEFAULT_POLL_SECONDS
        self._max_socials = DEFAULT_MAX_SOCIALS
        self._stale_days = DEFAULT_STALE_DAYS
        self._abbreviate = True
        self._prefix = DEFAULT_PREFIX
        self._last_poll: datetime | None = None
        # Which server prices are being asked about. Set explicitly when a dump
        # is loaded, so the plugin still knows the answer with EQ closed —
        # which is exactly when dumps get loaded.
        self._server = ""
        self._status = ""
        """Last thing the price fetcher did, for the window to show. The old
        code had no way to say 'I never called PigParse because I don't know
        your server', so the button just looked broken."""
        self._in_flight = 0
        # Rebuilt lazily; both are pure functions of state that rarely changes.
        self._matcher: NameMatcher | None = None
        self._matcher_version = -1
        self._index: ItemNameIndex | None = None

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
                # item names — the location column carries a character name too
                # — and tall enough for the Market tab, which is now the one
                # that sets the floor: a price chart, its figures, and the two
                # feeds underneath don't compress below about 675.
                default_geometry=(220, 220, 700, 700),
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

        for problem in host_drift():
            ctx.logger.warning("server table disagrees with the host — %s", problem)

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
        # A dump with no server can never be priced — PigParse keys on it — and
        # it can't be filed under a server in the Sell tab either. Fall back
        # through the live session to whatever was chosen last, so the common
        # case (same account, same server, EQ closed) needs no input at all.
        where = normalize_key(server) or self._active_server() or self._server
        stamp = captured_at or _dump_mtime(path)
        self._store_dump(who, where, items, captured_at=stamp, source_path=str(path))
        return len(items)

    def _store_dump(
        self,
        character: str,
        server: str,
        items: list[InventoryItem],
        *,
        captured_at: datetime,
        source_path: str,
    ) -> None:
        """File one character's items under one server, and remember the file."""
        with self._lock:
            self._vault.put(
                character, server, items, captured_at=captured_at, source_path=source_path
            )
            for item in items:
                self._catalog.learn_owned(item.name, item.item_id)
            if server:
                self._server = server
            self._version += 1
            self._invalidate_matcher()
        self._persist()

    def reload_dump(self, character: str, server: str) -> int:
        """Re-read one character's dump from the file it came from.

        Returns the sellable count, or 0 when there is no remembered path, the
        file is gone, or it no longer parses — the caller is expected to fall
        back to the file dialog rather than report a reload that didn't happen.
        Reloading is the natural response to seeing a stale row, and making the
        user re-find the file every time is the reason they wouldn't.

        Deliberately does not go through :meth:`load_dump`, which *decides*
        which server a dump belongs to. That decision has already been made and
        re-making it would refile an unfiled dump under whatever is current,
        leaving the original row stranded under a key nothing points at.
        """
        with self._lock:
            record = self._vault.get(character, normalize_key(server))
        if record is None or not record.source_path:
            return 0
        items = sellable(parse_inventory_file(record.source_path))
        if not items:
            return 0
        self._store_dump(
            record.character,
            record.server,
            items,
            captured_at=_dump_mtime(record.source_path),
            source_path=record.source_path,
        )
        return len(items)

    def forget_character(self, character: str, server: str | None = None) -> None:
        """Drop one character's dump and any listings that referenced it.

        ``server=None`` means "whichever one is in play"; ``""`` specifically
        means the unfiled bucket, which is a real place a dump can live and
        would otherwise be unreachable — passing it would have been read as
        "unspecified" and dropped a different character's dump instead.
        """
        with self._lock:
            if server is None:
                where = self._server or self._active_server()
            else:
                where = normalize_key(server)
            self._vault.drop(character, where)
            self._listings = [
                listing
                for listing in self._listings
                if listing.character.casefold() != character.strip().casefold()
            ]
            self._version += 1
            self._invalidate_matcher()
        self._persist()

    def holdings(self, *, server: str | None = None, character: str = "") -> list[Holding]:
        """Sellable items, optionally narrowed to one server and character.

        ``server=None`` means every server; ``server=""`` means specifically the
        dumps that have no server recorded, which is a real and reachable state
        — a dump loaded with EQ closed and nothing chosen yet lands there, and
        it must stay visible rather than silently vanishing from the list.

        The UI only ever offers "every character on *this* server", never every
        character everywhere: items on different servers can't be sold to the
        same buyer, so a cross-server list is one you'd have to mentally
        re-filter on every read.
        """
        wanted_character = character.strip().casefold()
        with self._lock:
            found = self._vault.holdings()
        if server is not None:
            key = normalize_key(server)
            found = [holding for holding in found if normalize_key(holding.server) == key]
        if wanted_character:
            found = [
                holding for holding in found if holding.character.casefold() == wanted_character
            ]
        return found

    def inventories(self, *, server: str | None = None) -> list[CharacterInventory]:
        with self._lock:
            records = self._vault.characters()
        if server is not None:
            key = normalize_key(server)
            records = [record for record in records if normalize_key(record.server) == key]
        return records

    def dumped_servers(self) -> list[str]:
        """Server keys that have at least one dump, most recent first.

        Includes ``""`` when some dump has no server recorded — the caller is
        expected to label that bucket rather than hide it.
        """
        seen: list[str] = []
        for record in self.inventories():
            key = normalize_key(record.server)
            if key not in seen:
                seen.append(key)
        return seen

    def characters_on(self, server: str | None) -> list[str]:
        """Character names dumped on ``server``, most recently dumped first."""
        names: list[str] = []
        for record in self.inventories(server=server):
            if record.character not in names:
                names.append(record.character)
        return names

    def server(self) -> str:
        """The server prices are being asked about."""
        with self._lock:
            return self._server or self._active_server()

    def set_server(self, server: str) -> None:
        key = normalize_key(server)
        with self._lock:
            if key == self._server:
                return
            self._server = key
            self._version += 1
        self._persist()

    def locate(self, name: str) -> list[Holding]:
        """Which characters hold ``name``, and where."""
        with self._lock:
            return self._vault.locate(name)

    def find_holdings(self, query: str, *, limit: int = 100) -> list[HoldingMatch]:
        """Held items matching ``query``, across every server.

        Deliberately unscoped where :meth:`holdings` is scoped: the Sell tab's
        one-server rule exists because you can't sell across servers, but the
        question this answers is "is it anywhere on the account at all", and an
        answer that hid the Blue mule would be a wrong answer. Every result
        carries its server so the follow-up question stays askable.
        """
        matcher = self.matcher()
        with self._lock:
            holdings = self._vault.holdings()
        return find_holdings(query, holdings, matcher=matcher, limit=limit)

    def stale_after(self) -> timedelta:
        """How old a dump has to be before its locations get a warning."""
        with self._lock:
            return timedelta(days=self._stale_days)

    def stale_dumps(self, now: datetime | None = None) -> list[CharacterInventory]:
        """Loaded dumps past the threshold, most recently captured first."""
        stamp = now or datetime.now()
        after = self.stale_after()
        return [record for record in self.inventories() if record.is_stale(stamp, after=after)]

    def items(self) -> list[InventoryItem]:
        """Flat item list across all characters (kept for convenience)."""
        return [holding.item for holding in self.holdings()]

    def _active_character(self) -> str:
        player = getattr(self._ctx, "player", None)
        return (getattr(player, "name", "") or "").strip()

    def _active_server(self) -> str:
        """The logged-in character's server as a key, or ``""``.

        Reads ``server`` rather than ``server_key`` and normalizes: the host's
        ``ActivePlayer.server`` is a ``Server`` IntEnum and ``server_key`` its
        lowercase name, and :func:`~merchant_mode.servers.normalize_key`
        swallows either. Only ever a *default* — the chosen server wins, so
        loading a Blue mule's dump while parked on Green doesn't misfile it.
        """
        player = getattr(self._ctx, "player", None)
        if player is None:
            return ""
        return normalize_key(getattr(player, "server", None)) or normalize_key(
            getattr(player, "server_key", None)
        )

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

    def export_pack(
        self,
        listings: list[Listing] | None = None,
        *,
        label: str = "",
        path: Path | str | None = None,
    ):
        """Build and write a macro pack. Returns ``(path, result)``.

        ``path`` is where the user said to put it. Without one this falls back
        to the plugin's private data directory, which is fine for a programmatic
        call and useless for a human — a pack you have to go spelunking in
        ``~/Library/Application Support`` for is a pack you won't import. The
        window always passes a path, from a save dialog, the same way the
        host's own Macro Editor exports.
        """
        assert self._ctx is not None
        result = self.build(listings)
        pack = build_pack(result.socials, label=label or self.suggested_pack_label())
        if path is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self._ctx.storage.data_dir / "packs" / f"wts-{stamp}.json"
        written = write_pack(pack, path)
        self._ctx.logger.info("wrote macro pack %s (%d socials)", written, len(result.socials))
        return written, result

    def suggested_pack_label(self) -> str:
        """``Xantik (Green)`` — the host's own label convention for a pack."""
        who = self._active_character()
        where = self.server()
        label = label_for(where)
        if who and label:
            return f"{who} ({label})"
        return who or label

    def suggested_pack_filename(self) -> str:
        """A filename worth defaulting a save dialog to."""
        who = self._active_character() or "merchant"
        stem = "".join(char for char in who if char.isalnum() or char in "-_") or "merchant"
        return f"{stem}-wts-macros.json"

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
        with self._lock:
            due = self._last_poll is None or (
                (now - self._last_poll).total_seconds() >= self._poll_seconds
            )
            names = self._pending_price_names(now) if due else []
        if not names:
            return
        if self.request_prices(names, quiet=True):
            self._last_poll = now

    def request_prices(self, names: list[str], *, quiet: bool = False) -> bool:
        """Ask PigParse about ``names``. Returns whether a call went out.

        Safe from any thread: ``ctx.submit`` only enqueues, and the reply is
        applied back on the driver thread. That is what lets the window's Fill
        button fetch directly instead of waiting for a poll that may never come.

        ``quiet`` suppresses the status line, for the background poll — the
        user didn't ask, so a failure isn't news.
        """
        ctx = self._ctx
        if ctx is None:
            return False
        wanted = [name.strip() for name in names if name.strip()][:MAX_PRICED_NAMES]
        if not wanted:
            return False

        server_wire = wire_for(self.server())
        if server_wire is None:
            # The old code returned here too, silently and forever, because it
            # read the live session's server and a dump is usually loaded with
            # EQ closed. Now it says so, and the Sell tab offers a picker.
            if not quiet:
                self._set_status(
                    "No server chosen — pick one on the Sell tab before fetching prices."
                )
            return False

        api = getattr(ctx, "pigparse", None)
        if api is None:
            if not quiet:
                self._set_status("PigParse is unavailable in this session.")
            return False

        server_key = self.server()
        with self._lock:
            self._in_flight += 1
            self._version += 1
        if not quiet:
            self._set_status(f"Asking PigParse about {len(wanted)} item(s)…")

        def fetch() -> Any:
            return api.item_prices(server_wire, wanted)

        def apply(prices: Any) -> None:
            self._apply_prices(prices, server=server_key, asked=wanted, quiet=quiet)

        ctx.submit(fetch, apply)
        return True

    def _pending_price_names(self, now: datetime | None = None) -> list[str]:
        """Names worth pricing, freshest need first. Caller holds the lock.

        Anything with a record newer than :data:`~merchant_mode.market.STALE_AFTER`
        is skipped, which is what lets successive polls advance through a big
        inventory. The previous version re-sent the same first forty names on
        every poll, so item forty-one was never priced at all.
        """
        stamp = now or datetime.now()
        seen: dict[str, str] = {}

        def offer(name: str) -> None:
            key = name.strip().casefold()
            if not key or key in seen:
                return
            record = self._prices.get(key)
            if record is not None and not record.is_stale(stamp):
                return
            seen[key] = name.strip()

        for name in self._wanted:
            offer(name)
        for listing in self._listings:
            offer(listing.name)
        for holding in self._vault.holdings():
            offer(holding.name)
        for name in self._history.names():
            offer(name)
        return list(seen.values())[:MAX_PRICED_NAMES]

    def _apply_prices(
        self,
        prices: Any,
        *,
        server: str = "",
        asked: list[str] | None = None,
        quiet: bool = False,
    ) -> None:
        """Runs back on the driver thread with whatever PigParse returned."""
        stamp = datetime.now().replace(microsecond=0)
        records = [
            built
            for record in (prices or ())
            if (built := MarketRecord.from_pigparse(record, server=server, fetched_at=stamp))
        ]
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            for record in records:
                self._prices[record.name.casefold()] = record
                self._catalog.learn_remote(record.name, record.item_id)
            self._version += 1
        if not quiet:
            requested = len(asked or ())
            if not records:
                self._set_status(
                    f"PigParse had nothing for {requested} item(s)."
                    if requested
                    else "PigParse returned nothing."
                )
            elif requested and len(records) < requested:
                self._set_status(f"PigParse priced {len(records)} of {requested} item(s).")
            else:
                self._set_status(f"PigParse priced {len(records)} item(s).")
        self._persist()

    def _set_status(self, text: str) -> None:
        with self._lock:
            self._status = text
            self._version += 1

    def status(self) -> str:
        with self._lock:
            return self._status

    def busy(self) -> bool:
        with self._lock:
            return self._in_flight > 0

    # --- price suggestions -------------------------------------------------
    def _averages_view(self) -> dict[str, int]:
        """The headline number per item, in the shape ``suggest`` wants.

        Caller holds the lock. Derived rather than stored so there is one
        source of truth: :meth:`MarketRecord.best` decides which averaging
        window to trust, and it decides it in exactly one place.
        """
        return {key: record.headline for key, record in self._prices.items() if record.headline}

    def matcher(self) -> NameMatcher:
        """Name matcher over everything the plugin considers a real item.

        Cached against the version counter: building it walks every holding,
        and the window asks for a suggestion once per visible row per refresh.
        """
        with self._lock:
            if self._matcher is not None and self._matcher_version == self._version:
                return self._matcher
            names = [holding.name for holding in self._vault.holdings()]
            names.extend(self._wanted)
            names.extend(listing.name for listing in self._listings)
            names.extend(record.name for record in self._prices.values())
            built = NameMatcher(names, nicknames=self._nicknames)
            self._matcher, self._matcher_version = built, self._version
            return built

    def _invalidate_matcher(self) -> None:
        """Caller holds the lock."""
        self._matcher = None
        self._matcher_version = -1

    def suggest_price(self, name: str, *, side: Side = Side.SELL) -> Suggestion:
        """Best known price for ``name``, with the source it came from."""
        matcher = self.matcher()
        with self._lock:
            return suggest(
                name,
                history=self._history,
                averages=self._averages_view(),
                side=side,
                matcher=matcher,
            )

    def suggest_prices(self, names: list[str], *, side: Side = Side.SELL) -> dict[str, Suggestion]:
        matcher = self.matcher()
        with self._lock:
            averages = self._averages_view()
            return {
                name: suggest(
                    name,
                    history=self._history,
                    averages=averages,
                    side=side,
                    matcher=matcher,
                )
                for name in names
            }

    def unpriced_listings(self) -> list[str]:
        """Ticked items with a blank price box and nothing known to fill it.

        What the Fill button should go and *ask* about, rather than reporting
        that it found nothing.
        """
        matcher = self.matcher()
        with self._lock:
            averages = self._averages_view()
            return [
                listing.name
                for listing in self._listings
                if not listing.price.strip()
                and not suggest(
                    listing.name,
                    history=self._history,
                    averages=averages,
                    matcher=matcher,
                ).known
            ]

    def fill_prices(self, *, overwrite: bool = False) -> int:
        """Push known prices onto the current listings. Returns how many changed.

        Only fills blanks unless ``overwrite``: a price the seller typed is a
        decision, and a lookup shouldn't quietly overrule it.
        """
        matcher = self.matcher()
        with self._lock:
            averages = self._averages_view()
            filled: list[Listing] = []
            changed = 0
            for listing in self._listings:
                if listing.price.strip() and not overwrite:
                    filled.append(listing)
                    continue
                proposal = suggest(
                    listing.name,
                    history=self._history,
                    averages=averages,
                    side=Side.SELL,
                    matcher=matcher,
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

    # --- item lookup (for the Market tab's search box) ----------------------
    def search_items(self, query: str, *, limit: int = 50) -> list[str]:
        """Item names matching ``query``, best first.

        The index is the bundled/host master list unioned with everything the
        plugin has learned, so items added since that list was cut are still
        findable once you've dumped or heard about them.
        """
        index = self._item_index()
        return index.search(query, limit=limit)

    def _item_index(self) -> ItemNameIndex:
        """Built on first search — 25k names is a quarter-megabyte of strings
        and most sessions never open the search box."""
        with self._lock:
            index = self._index
        if index is None:
            index = ItemNameIndex.from_master()
            with self._lock:
                self._index = index
        with self._lock:
            index.add(holding.name for holding in self._vault.holdings())
            index.add(self._wanted)
            index.add(record.name for record in self._prices.values())
            index.add(self._history.names())
        return index

    def index_source(self) -> str:
        """Where the name index came from, or ``""`` if it isn't built yet.

        Deliberately does *not* force the load: this is called to caption an
        idle panel, and doing 25k names of work to label a box nobody has
        typed into would make the laziness above pointless.
        """
        with self._lock:
            return self._index.source if self._index is not None else ""

    def market_for(self, name: str) -> MarketRecord | None:
        """PigParse's stats for ``name``, if they've been fetched."""
        key = name.strip().casefold()
        with self._lock:
            record = self._prices.get(key)
        if record is not None:
            return record
        resolved = self.matcher().resolve(name)
        if resolved is None:
            return None
        with self._lock:
            return self._prices.get(resolved.casefold())

    def observations_for(self, name: str, *, limit: int = 50):
        """Local ``/auc`` sightings of ``name``, newest first."""
        matcher = self.matcher()
        with self._lock:
            return self._history.recent(name, limit=limit, matcher=matcher)

    def chart_for(self, name: str, *, limit: int = 200) -> PriceChart:
        """Everything the price panel draws for ``name``.

        Assembled here rather than in the window so the window's paint code
        gets a finished answer: what it draws, and whether there is anything to
        draw at all, are decisions with reasons behind them and both belong on
        this side of the Qt line.
        """
        return build_chart(
            name,
            record=self.market_for(name),
            observations=self.observations_for(name, limit=limit),
        )

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
                "averages": self._averages_view(),
                "prices": dict(self._prices),
                "history": self._history.recent(limit=100),
                "conflicts": self._catalog.conflicts(),
                "pause_tenths": self._pause_tenths,
                "abbreviate": self._abbreviate,
                "server": self._server or self._active_server(),
                "status": self._status,
                "busy": self._in_flight > 0,
                "stale_days": self._stale_days,
            }

    def resolve_id(self, name: str):
        with self._lock:
            return self._catalog.resolve(name)

    def set_listings(self, listings: list[Listing]) -> None:
        with self._lock:
            self._listings = list(listings)
            self._version += 1
            self._invalidate_matcher()
        self._persist()

    def set_wanted(self, names: list[str]) -> None:
        with self._lock:
            self._wanted = [name.strip() for name in names if name.strip()]
            self._version += 1
            self._invalidate_matcher()
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
            self._stale_days = self._clamp_stale(stored.get("stale_days", DEFAULT_STALE_DAYS))
            self._abbreviate = bool(stored.get("abbreviate", True))
            self._prefix = str(stored.get("prefix") or DEFAULT_PREFIX)
            self._wanted = [str(name) for name in stored.get("wanted", []) if str(name).strip()]
            nicknames = stored.get("nicknames")
            if isinstance(nicknames, dict):
                self._nicknames = NicknameTable({str(k): str(v) for k, v in nicknames.items()})
            self._catalog = ItemCatalog.from_dict(stored.get("catalog"))
            self._history = PriceHistory.from_list(stored.get("history"))
            self._server = normalize_key(stored.get("server"))
            prices = stored.get("prices")
            if isinstance(prices, dict):
                self._prices = {
                    str(key).casefold(): record
                    for key, raw in prices.items()
                    if (record := MarketRecord.from_dict(raw)) is not None
                }
            # v2 stored a bare 6-month average per name. Carry them over as
            # single-window records so an upgrading user's Fill button keeps
            # working before the first fetch; the counts fill in on the next one.
            legacy = stored.get("averages")
            if isinstance(legacy, dict) and not self._prices:
                for name, value in legacy.items():
                    average = _as_int(value, 0)
                    if average > 0:
                        self._prices[str(name).casefold()] = MarketRecord(
                            name=str(name),
                            averages={"6mo": average},
                            counts={},
                        )
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
                "stale_days": self._stale_days,
                "abbreviate": self._abbreviate,
                "prefix": self._prefix,
                "wanted": list(self._wanted),
                "nicknames": self._nicknames.to_dict(),
                "catalog": self._catalog.to_dict(),
                "history": self._history.to_list(),
                "server": self._server,
                "prices": {key: record.to_dict() for key, record in self._prices.items()},
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

    @staticmethod
    def _clamp_stale(value: object) -> int:
        return max(MIN_STALE_DAYS, min(MAX_STALE_DAYS, _as_int(value, DEFAULT_STALE_DAYS)))

    # --- settings ----------------------------------------------------------
    def settings(self) -> dict:
        with self._lock:
            return {
                "pause_tenths": self._pause_tenths,
                "poll_seconds": self._poll_seconds,
                "max_socials": self._max_socials,
                "stale_days": self._stale_days,
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
            if "stale_days" in values:
                self._stale_days = self._clamp_stale(values["stale_days"])
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
