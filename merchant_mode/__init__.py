"""Merchant Mode — inventory into linkable WTS auction macros, for nParse+.

Load an ``/outputfile inventory`` dump — or let nParse+ hand over the one it
noticed you writing — pick what you're selling, set prices, and export a macro
pack the host's Macro Editor imports. The item ids come out of the dump itself,
so the links are forged from the game's own answer rather than from a lookup
table that might be stale.

**The plugin never sends anything.** It builds macros; the human presses the
button. There is no keystroke simulation anywhere in this package — P99 bans
it, and the feature isn't worth the account.

**Everything that touches trade is keyed by server.** Items cannot move between
P99 servers, so an item on Blue can never be sold to a buyer on Green: the
inventories you can list, the prices you can quote, the auctions that count as
evidence, the WTB list and the ticked listings are all per server, and there is
no combined view of any of them anywhere in the plugin. Two things are
deliberately *not* split, because they are facts about items rather than about
markets: item ids (:mod:`merchant_mode.catalog`), which the game assigns
globally, and the user's filter list (:mod:`merchant_mode.filters`), because
junk is junk everywhere.

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
from .filters import FilterRule, ItemFilters
from .finding import HoldingMatch, find_holdings
from .inventory import (
    ORIGIN_HOST,
    ORIGIN_MANUAL,
    SNAPSHOT_INVENTORY,
    STALE_AFTER,
    CharacterInventory,
    Holding,
    InventoryItem,
    InventoryVault,
    character_from_filename,
    inventory_key,
    parse_inventory_file,
    parse_snapshot_file,
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

SCHEMA_VERSION = 5
"""Storage layout. v1 kept no inventories; v2 retains one dump per character;
v3 keeps PigParse's whole stats block per item instead of a bare average, and
remembers which server you last dumped on; v4 remembers the file each dump came
from, so a stale one can be reloaded without hunting for it again; v5 files
prices, listings, the WTB list and every auction sighting under a server key,
and adds the item filter list.

Every version is read by the one after it. A v3 store simply has no source
paths, and its dumps reload through the file dialog the way they always did. A
v4 store's flat prices, listings and WTB names are filed under the server it
remembered — the plugin only ever tracked one at a time before v5, so that
server is the right answer rather than a guess."""

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


def _watch_dumps(ctx: PluginContext, plugin: MerchantModePlugin) -> None:
    """Take inventory dumps in as the host files them, with nobody asking.

    nParse+ 2.1.0 watches the EQ directory and stores a snapshot of every
    ``/outputfile`` dump it sees, announcing each one on the bus. A merchant
    who dumps six mules in a row should not then have to find six files in a
    dialog, so the plugin listens and reads them.

    The events carry *identity* — character, kind, digest, and the path of the
    snapshot — rather than items, because the library itself is host-internal;
    :meth:`MerchantModePlugin.ingest_dump_snapshot` does the reading. Both
    events are the same news to a merchant (a dump exists and this is what it
    says), so both go to one handler; the diff on the Updated one is about
    what *changed*, which is a question the Dumps tab answers with ages.

    These classes arrived in 2.1.0, which is why ``min_app_version`` names it:
    below that this import raises and the caller's ``except ImportError`` puts
    the whole subscription block — auctions included — out of action.
    """
    from nparseplus_sdk.events import CharacterDumpImportedEvent, CharacterDumpUpdatedEvent

    def on_dump(event: Any) -> None:
        plugin.ingest_dump_snapshot(
            event.path,
            character=event.character,
            server=event.server,
            kind=event.kind,
            captured_at=event.captured_at,
            digest=event.digest,
            source_file=event.source_file,
        )

    ctx.subscribe(CharacterDumpImportedEvent, on_dump)
    ctx.subscribe(CharacterDumpUpdatedEvent, on_dump)


class MerchantModePlugin(NParsePlugin):
    meta = PluginMeta(
        id="merchant-mode",
        name="Merchant Mode",
        version="0.4.1",
        description=(
            "Turn your inventory into linkable WTS auction macros, find which "
            "mule is holding what, look up what anything is worth, and see how "
            "its price is actually moving — one server at a time, since that is "
            "the only place any of it can be sold."
        ),
        author="prokopto-dev",
        homepage="https://github.com/prokopto-dev/nparseplus-merchantmode",
        requires_sdk=">=1.0,<2",
        # The version this was actually built and verified against, and now
        # also a hard floor: the Character Dumps library and its
        # ``CharacterDumpImportedEvent`` / ``CharacterDumpUpdatedEvent`` landed
        # in 2.1.0, and on anything older the subscription block raises
        # ImportError and takes auction tracking down with it. Claim only what
        # has been tested and let the host block older installs rather than
        # letting them half-work at runtime.
        min_app_version="2.1.0",
    )

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        # The driver thread writes; the GUI thread reads via snapshot().
        self._lock = threading.Lock()
        self._version = 0
        self._vault = InventoryVault()
        # Content digests of the dumps the host has already handed over, per
        # character and server, so an unchanged one arriving again costs
        # nothing. Deliberately not persisted: it is an optimisation, and after
        # a restart the first event simply re-files rows the vault already has.
        self._ingested: dict[str, str] = {}
        # Everything a trade touches is filed by server key ("" is the real,
        # reachable bucket for a dump loaded before any server was chosen).
        # There is no combined view: items don't cross servers, so a pooled
        # list is one the reader has to re-filter in their head every time.
        self._listings: dict[str, list[Listing]] = {}
        self._wanted: dict[str, list[str]] = {}
        self._prices: dict[str, dict[str, MarketRecord]] = {}
        self._nicknames = NicknameTable()
        self._catalog = ItemCatalog()
        self._history = PriceHistory()
        # Account-wide on purpose — see merchant_mode.filters.
        self._filters = ItemFilters()
        self._pause_tenths = DEFAULT_PAUSE_TENTHS
        self._poll_seconds = DEFAULT_POLL_SECONDS
        self._max_socials = DEFAULT_MAX_SOCIALS
        self._stale_days = DEFAULT_STALE_DAYS
        self._abbreviate = True
        self._show_ids = False
        """Whether the Sell tab keeps its ID column. Off by default: for a
        dumped inventory it reads "owned" on every row, and the number itself
        is one nobody types. The disagreement that actually matters is marked
        on the item's own name instead."""
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
        # Rebuilt lazily; all are pure functions of state that rarely changes.
        # One matcher per server: an acronym two items could both claim is only
        # ambiguous when both are on the same server, and the pricing matcher
        # refuses ambiguity rather than guessing.
        self._matchers: dict[str, NameMatcher] = {}
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
            _watch_dumps(ctx, self)
        except ImportError:
            ctx.logger.warning(
                "host events unavailable (standalone run); price tracking and "
                "automatic dump ingest are inert"
            )

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

    def ingest_dump_snapshot(
        self,
        path: Path | str,
        *,
        character: str = "",
        server: str = "",
        kind: str = SNAPSHOT_INVENTORY,
        captured_at: datetime | None = None,
        digest: str = "",
        source_file: str = "",
    ) -> int:
        """Take in a dump the host's library just stored. Returns the item count.

        The other end of :func:`_watch_dumps`: everything here comes off a
        ``CharacterDumpImportedEvent`` or ``CharacterDumpUpdatedEvent``, which
        name a snapshot rather than carrying one. Zero means nothing was taken
        — a spellbook, an unchanged dump arriving again, or a snapshot with
        nothing sellable in it — and is not an error.

        Runs on the driver thread, like the auction subscription. All the state
        it touches goes through :meth:`_store_dump`'s lock.
        """
        # A spellbook is the other kind of dump and holds nothing to sell. An
        # event with no kind at all is a shape the host does not send; let it
        # through to the items check rather than inventing a rule for it.
        if kind.strip().casefold() not in ("", SNAPSHOT_INVENTORY):
            return 0

        # Same fallback chain as load_dump, and for the same reason — except
        # that here the event names the character, so the other two are only
        # ever reached if the host sent a blank one.
        who = (
            character.strip()
            or self._active_character()
            or character_from_filename(source_file or path)
        )
        # SERVER IS THE TRAP. P99 writes ``<Character>-Inventory.txt`` and the
        # host only reads a server out of a ``Name_Server-Kind.txt`` spelling,
        # so ``event.server`` is empty in practice — every auto-ingested dump
        # would land in the unfiled bucket if this took it at its word.
        where = normalize_key(server) or self._active_server() or self._server

        # The digest is over the dump's *contents*, so re-running /outputfile
        # out of habit produces the one already filed. Skipping it keeps the
        # version counter still — the window would otherwise redraw on every
        # scan — and leaves any rows the seller cropped off this dump cropped.
        seen = inventory_key(who, where)
        if digest:
            with self._lock:
                if self._ingested.get(seen) == digest:
                    return 0

        items = sellable(parse_snapshot_file(path))
        if not items:
            return 0
        self._store_dump(
            who,
            where,
            items,
            captured_at=captured_at or _dump_mtime(source_file or path),
            # The game's own file, not the snapshot: Reload on the Dumps tab
            # re-reads a tab-separated dump, and that file is also the one the
            # next /outputfile rewrites. The snapshot is the host's copy of it.
            source_path=str(source_file or path),
            origin=ORIGIN_HOST,
        )
        if digest:
            with self._lock:
                self._ingested[seen] = digest
        if self._ctx is not None:
            # Worth a line: a merchant juggling mules should be able to see the
            # dump they just typed arrive, rather than wonder whether it did.
            self._ctx.logger.info(
                "took in %s's inventory dump from the host — %d sellable item(s) on %s",
                who or "an unnamed character",
                len(items),
                label_for(where) or "no server",
            )
        return len(items)

    def _store_dump(
        self,
        character: str,
        server: str,
        items: list[InventoryItem],
        *,
        captured_at: datetime,
        source_path: str,
        origin: str = ORIGIN_MANUAL,
    ) -> None:
        """File one character's items under one server, and remember the file.

        ``origin`` is how this copy of the dump got here. The Dumps tab says it
        on every row: one that appeared without being asked for is one the
        reader never chose, and an unexplained row is one they can't weigh.
        """
        with self._lock:
            self._vault.put(
                character,
                server,
                items,
                captured_at=captured_at,
                source_path=source_path,
                origin=origin,
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
            # Whatever brought the first copy in, *these* rows were asked for:
            # the Source column answers "where did this dump come from", not
            # "who introduced this character".
            origin=ORIGIN_MANUAL,
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
            where = self._server_key(server)
            self._vault.drop(character, where)
            self._listings[where] = [
                listing
                for listing in self._listings_on(where)
                if listing.character.casefold() != character.strip().casefold()
            ]
            self._version += 1
            self._invalidate_matcher()
        self._persist()

    def remove_items(self, targets: list[tuple[str, str, str, int]]) -> int:
        """Delete individual rows from stored dumps. Returns how many went.

        Each target is ``(character, server, location, item_id)`` — everything
        needed to name one row of one dump, which is what the Sell tab's
        selection gives it. Any listing whose item is no longer held on that
        server goes too, because a macro advertising an item you have just
        declared you don't have is the one failure mode worth preventing here.

        This edits the stored dump, not the file: reloading that character
        brings the rows back. That is deliberate — a dump is a photograph and
        this crops the copy. :meth:`set_filters` is how you make it stick.
        """
        by_dump: dict[tuple[str, str], set[tuple[str, int]]] = {}
        for character, server, location, item_id in targets:
            key = (character, normalize_key(server))
            by_dump.setdefault(key, set()).add((location.replace("-", "").casefold(), int(item_id)))
        if not by_dump:
            return 0

        removed = 0
        touched: set[str] = set()
        with self._lock:
            for (character, server), rows in by_dump.items():
                gone = self._vault.remove_items(character, server, rows)
                if gone:
                    removed += gone
                    touched.add(server)
            for server in touched:
                # Per server, not pooled: the same item name still held on Blue
                # says nothing about whether Green can still advertise it.
                held = {
                    holding.name.casefold()
                    for holding in self._vault.holdings()
                    if normalize_key(holding.server) == server
                }
                self._listings[server] = [
                    listing
                    for listing in self._listings_on(server)
                    if listing.name.casefold() in held
                ]
            if removed:
                self._version += 1
                self._invalidate_matcher()
        if removed:
            self._persist()
        return removed

    def holdings(
        self,
        *,
        server: str | None = None,
        character: str = "",
        include_filtered: bool = False,
    ) -> list[Holding]:
        """Sellable items on one server, optionally narrowed to one character.

        ``server=None`` means the server currently in play — there is
        deliberately no "every server" option, because items on different
        servers can't be sold to the same buyer and a pooled list is one you'd
        have to re-filter in your head on every read. ``server=""`` means
        specifically the dumps that have no server recorded, which is a real and
        reachable state: a dump loaded with EQ closed and nothing chosen yet
        lands there, and it must stay visible rather than silently vanishing.

        Items matched by the filter list are left out unless
        ``include_filtered``. Callers that show the result are expected to say
        how many were hidden — see :meth:`hidden_count`.
        """
        wanted_character = character.strip().casefold()
        with self._lock:
            key = self._server_key(server)
            found = [
                holding
                for holding in self._vault.holdings()
                if normalize_key(holding.server) == key
            ]
            rules = self._filters
        if wanted_character:
            found = [
                holding for holding in found if holding.character.casefold() == wanted_character
            ]
        if not include_filtered:
            found = [holding for holding in found if not rules.hidden(holding.name)]
        return found

    def all_holdings(self) -> list[Holding]:
        """Every held item on every server, filters ignored.

        The one deliberately unscoped read, and not a trading view: it exists so
        the item-name index can learn every spelling you have ever dumped, which
        is a fact about the game rather than about a market. Nothing that
        prices, lists or advertises an item may use it.
        """
        with self._lock:
            return self._vault.holdings()

    def hidden_count(self, *, server: str | None = None, character: str = "") -> int:
        """How many held items the filter list is currently hiding.

        Reported wherever a filtered list is shown. A list quietly missing rows
        is worse than a cluttered one, so the number is never allowed to be
        invisible.
        """
        shown = len(self.holdings(server=server, character=character))
        every = len(self.holdings(server=server, character=character, include_filtered=True))
        return every - shown

    def inventories(self, *, server: str | None = None) -> list[CharacterInventory]:
        """Loaded dumps, newest first. ``server=None`` means every server.

        The exception to the plugin's server rule, and the only one in the UI:
        the Dumps tab manages the dumps themselves rather than trading with
        them, and a Blue dump you can't see is a Blue dump you can't reload or
        forget. Every row names its server.
        """
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
        """The server everything is currently scoped to."""
        with self._lock:
            return self._server_key(None)

    def set_server(self, server: str) -> None:
        key = normalize_key(server)
        with self._lock:
            if key == self._server:
                return
            self._server = key
            self._version += 1
        self._persist()

    def _server_key(self, server: object = None) -> str:
        """``None`` means the server in play; anything else is normalized.

        Takes no lock and touches only fields whose reads are atomic, so it is
        safe to call with the lock already held — which most callers do.
        """
        if server is None:
            return self._server or self._active_server()
        return normalize_key(server)

    def _listings_on(self, key: str) -> list[Listing]:
        """Caller holds the lock."""
        return self._listings.get(key, [])

    def _wanted_on(self, key: str) -> list[str]:
        """Caller holds the lock."""
        return self._wanted.get(key, [])

    def _prices_on(self, key: str) -> dict[str, MarketRecord]:
        """Caller holds the lock."""
        return self._prices.get(key, {})

    def locate(self, name: str, *, server: str | None = None) -> list[Holding]:
        """Which characters hold ``name`` on one server, and where."""
        key = self._server_key(server)
        with self._lock:
            return [
                holding
                for holding in self._vault.locate(name)
                if normalize_key(holding.server) == key
            ]

    def find_holdings(
        self, query: str, *, server: str | None = None, limit: int = 100
    ) -> list[HoldingMatch]:
        """Held items on one server matching ``query``.

        Scoped like everything else, and it used to be the exception. The
        argument for searching every server was that "is it anywhere on the
        account" is a different question from "can I sell it to you" — true,
        but not the question this tab is asked. A buyer asks whether you have a
        Fungi *while standing in front of you*, on their server, and a row
        naming a mule they can never trade with is an answer that wastes the
        few seconds the tab exists to save.

        Filtered items are searched anyway: you may not advertise your Bone
        Chips, but if you went looking for them you want them found.
        """
        key = self._server_key(server)
        matcher = self.matcher(server=key)
        with self._lock:
            holdings = [
                holding
                for holding in self._vault.holdings()
                if normalize_key(holding.server) == key
            ]
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
        """Flat item list for the server in play (kept for convenience)."""
        return [holding.item for holding in self.holdings()]

    # --- item filters ------------------------------------------------------
    def filters(self) -> ItemFilters:
        """The account-wide filter list. Read-only; use the setters to change."""
        with self._lock:
            return self._filters

    def filter_rules(self) -> list[FilterRule]:
        with self._lock:
            return self._filters.rules()

    def set_filters(self, rules: list[FilterRule]) -> None:
        with self._lock:
            self._filters = ItemFilters(list(rules))
            self._version += 1
        self._persist()

    def add_filters(self, rules: list[FilterRule]) -> int:
        """Append rules, skipping blanks and duplicates. Returns how many landed."""
        with self._lock:
            added = self._filters.extend(list(rules))
            if added:
                self._version += 1
        if added:
            self._persist()
        return added

    def remove_filters(self, indices: list[int]) -> int:
        with self._lock:
            removed = self._filters.remove(indices)
            if removed:
                self._version += 1
        if removed:
            self._persist()
        return removed

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
        """Pack listings into socials using the current settings.

        Without an explicit list this builds the current server's, which is the
        only pack that could ever be posted: one ``/auc`` line reaches one
        server's channel.
        """
        with self._lock:
            chosen = list(
                listings if listings is not None else self._listings_on(self._server_key(None))
            )
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
    def observe_auction(
        self,
        content: str,
        *,
        timestamp: datetime,
        sender: str = "",
        server: str | None = None,
    ) -> None:
        """Record what the channel said, stamped with which channel it was.

        The live session's server wins over the chosen one here, where it loses
        everywhere else: this is a thing that was *heard*, and it was heard on
        whichever server the log driver is reading. Choosing Blue in the picker
        while parked on Green must not file Green's auctions under Blue.
        """
        with self._lock:
            where = normalize_key(server) if server is not None else (
                self._active_server() or self._server
            )
            added = self._history.record(
                content, timestamp=timestamp, sender=sender, server=where
            )
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
        """Names worth pricing on the current server. Caller holds the lock.

        Anything with a record newer than :data:`~merchant_mode.market.STALE_AFTER`
        is skipped, which is what lets successive polls advance through a big
        inventory. The previous version re-sent the same first forty names on
        every poll, so item forty-one was never priced at all.

        Filtered items are skipped entirely. A price for something you have
        already declared you will never sell is a request PigParse answered for
        nothing, and on a main carrying forty rows of food and bone chips it is
        most of the poll.
        """
        stamp = now or datetime.now()
        where = self._server_key(None)
        known = self._prices_on(where)
        seen: dict[str, str] = {}

        def offer(name: str) -> None:
            key = name.strip().casefold()
            if not key or key in seen or self._filters.hidden(name):
                return
            record = known.get(key)
            if record is not None and not record.is_stale(stamp):
                return
            seen[key] = name.strip()

        for name in self._wanted_on(where):
            offer(name)
        for listing in self._listings_on(where):
            offer(listing.name)
        for holding in self._vault.holdings():
            if normalize_key(holding.server) == where:
                offer(holding.name)
        for name in self._history.names(server=where):
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
        where = normalize_key(server)
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            bucket = self._prices.setdefault(where, {})
            for record in records:
                bucket[record.name.casefold()] = record
                # Ids are the exception to the server rule: the game assigns
                # them globally, so an id learned on Blue is an id on Green.
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
    def _averages_view(self, server: str) -> dict[str, int]:
        """The headline number per item on one server, in ``suggest``'s shape.

        Caller holds the lock. Derived rather than stored so there is one
        source of truth: :meth:`MarketRecord.best` decides which averaging
        window to trust, and it decides it in exactly one place.
        """
        return {
            key: record.headline
            for key, record in self._prices_on(server).items()
            if record.headline
        }

    def matcher(self, *, server: str | None = None) -> NameMatcher:
        """Name matcher over everything the plugin considers a real item *here*.

        One per server. The matcher refuses ambiguity rather than guessing, so
        the set it is built from decides what counts as ambiguous — and two
        items sharing an acronym across servers is not a real collision, since
        only one of them can be the thing the channel is talking about.

        Cached against the version counter: building it walks every holding,
        and the window asks for a suggestion once per visible row per refresh.
        """
        with self._lock:
            key = self._server_key(server)
            if self._matcher_version == self._version and key in self._matchers:
                return self._matchers[key]
            if self._matcher_version != self._version:
                self._matchers.clear()
                self._matcher_version = self._version
            names = [
                holding.name
                for holding in self._vault.holdings()
                if normalize_key(holding.server) == key
            ]
            names.extend(self._wanted_on(key))
            names.extend(listing.name for listing in self._listings_on(key))
            names.extend(record.name for record in self._prices_on(key).values())
            built = NameMatcher(names, nicknames=self._nicknames)
            self._matchers[key] = built
            return built

    def _invalidate_matcher(self) -> None:
        """Caller holds the lock."""
        self._matchers.clear()
        self._matcher_version = -1

    def suggest_price(
        self, name: str, *, side: Side = Side.SELL, server: str | None = None
    ) -> Suggestion:
        """Best known price for ``name`` on one server, and where it came from."""
        key = self._server_key(server)
        matcher = self.matcher(server=key)
        with self._lock:
            return suggest(
                name,
                history=self._history,
                averages=self._averages_view(key),
                side=side,
                matcher=matcher,
                server=key,
            )

    def suggest_prices(
        self, names: list[str], *, side: Side = Side.SELL, server: str | None = None
    ) -> dict[str, Suggestion]:
        key = self._server_key(server)
        matcher = self.matcher(server=key)
        with self._lock:
            averages = self._averages_view(key)
            return {
                name: suggest(
                    name,
                    history=self._history,
                    averages=averages,
                    side=side,
                    matcher=matcher,
                    server=key,
                )
                for name in names
            }

    def unpriced_listings(self, *, server: str | None = None) -> list[str]:
        """Ticked items with a blank price box and nothing known to fill it.

        What the Fill button should go and *ask* about, rather than reporting
        that it found nothing.
        """
        key = self._server_key(server)
        matcher = self.matcher(server=key)
        with self._lock:
            averages = self._averages_view(key)
            return [
                listing.name
                for listing in self._listings_on(key)
                if not listing.price.strip()
                and not suggest(
                    listing.name,
                    history=self._history,
                    averages=averages,
                    matcher=matcher,
                    server=key,
                ).known
            ]

    def fill_prices(self, *, overwrite: bool = False, server: str | None = None) -> int:
        """Push known prices onto one server's listings. Returns how many changed.

        Only fills blanks unless ``overwrite``: a price the seller typed is a
        decision, and a lookup shouldn't quietly overrule it.
        """
        key = self._server_key(server)
        matcher = self.matcher(server=key)
        with self._lock:
            averages = self._averages_view(key)
            filled: list[Listing] = []
            changed = 0
            for listing in self._listings_on(key):
                if listing.price.strip() and not overwrite:
                    filled.append(listing)
                    continue
                proposal = suggest(
                    listing.name,
                    history=self._history,
                    averages=averages,
                    side=Side.SELL,
                    matcher=matcher,
                    server=key,
                )
                if not proposal.known or proposal.text == listing.price:
                    filled.append(listing)
                    continue
                filled.append(replace(listing, price=proposal.text))
                changed += 1
            if changed:
                self._listings[key] = filled
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

        Names are the one thing pooled across servers, and the reason is the
        same one that splits everything else: an item's *existence* is a fact
        about the game, while its price and location are facts about a market.
        The lookup this box feeds is per server; the spelling it saves you
        typing is not.
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
            for names in self._wanted.values():
                index.add(names)
            for bucket in self._prices.values():
                index.add(record.name for record in bucket.values())
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

    def market_for(self, name: str, *, server: str | None = None) -> MarketRecord | None:
        """PigParse's stats for ``name`` on one server, if they've been fetched.

        Per server because PigParse's own answers are: the same item has a
        different history on Blue and on Green, and showing one under the
        other's heading would be wrong in a way nothing on screen reveals.
        """
        where = self._server_key(server)
        key = name.strip().casefold()
        with self._lock:
            record = self._prices_on(where).get(key)
        if record is not None:
            return record
        resolved = self.matcher(server=where).resolve(name)
        if resolved is None:
            return None
        with self._lock:
            return self._prices_on(where).get(resolved.casefold())

    def observations_for(self, name: str, *, limit: int = 50, server: str | None = None):
        """Local ``/auc`` sightings of ``name`` on one server, newest first."""
        where = self._server_key(server)
        matcher = self.matcher(server=where)
        with self._lock:
            return self._history.recent(name, limit=limit, matcher=matcher, server=where)

    def chart_for(self, name: str, *, limit: int = 200, server: str | None = None) -> PriceChart:
        """Everything the price panel draws for ``name`` on one server.

        Assembled here rather than in the window so the window's paint code
        gets a finished answer: what it draws, and whether there is anything to
        draw at all, are decisions with reasons behind them and both belong on
        this side of the Qt line.
        """
        where = self._server_key(server)
        return build_chart(
            name,
            record=self.market_for(name, server=where),
            observations=self.observations_for(name, limit=limit, server=where),
        )

    # --- snapshot for the window (GUI thread) ------------------------------
    def snapshot(self) -> dict:
        """A consistent read of everything the window draws.

        Scoped to the server in play, which is why it is one call rather than a
        parameter: every tab that reads it is asking about the same server at
        the same moment, and letting them disagree is exactly the bug this
        release exists to remove. ``inventories`` is the documented exception —
        the Dumps tab manages dumps, not trades.
        """
        with self._lock:
            where = self._server_key(None)
            holdings = [
                holding
                for holding in self._vault.holdings()
                if normalize_key(holding.server) == where
            ]
            return {
                "version": self._version,
                "holdings": holdings,
                "items": [holding.item for holding in holdings],
                "inventories": self._vault.characters(),
                "listings": list(self._listings_on(where)),
                "wanted": list(self._wanted_on(where)),
                "averages": self._averages_view(where),
                "prices": dict(self._prices_on(where)),
                "history": self._history.recent(limit=100, server=where),
                "conflicts": self._catalog.conflicts(),
                "filters": self._filters.rules(),
                "pause_tenths": self._pause_tenths,
                "abbreviate": self._abbreviate,
                "server": where,
                "status": self._status,
                "busy": self._in_flight > 0,
                "stale_days": self._stale_days,
            }

    def resolve_id(self, name: str):
        with self._lock:
            return self._catalog.resolve(name)

    def set_listings(self, listings: list[Listing], *, server: str | None = None) -> None:
        with self._lock:
            self._listings[self._server_key(server)] = list(listings)
            self._version += 1
            self._invalidate_matcher()
        self._persist()

    def set_wanted(self, names: list[str], *, server: str | None = None) -> None:
        with self._lock:
            self._wanted[self._server_key(server)] = [
                name.strip() for name in names if name.strip()
            ]
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
            self._show_ids = bool(stored.get("show_ids", False))
            self._prefix = str(stored.get("prefix") or DEFAULT_PREFIX)
            nicknames = stored.get("nicknames")
            if isinstance(nicknames, dict):
                self._nicknames = NicknameTable({str(k): str(v) for k, v in nicknames.items()})
            self._catalog = ItemCatalog.from_dict(stored.get("catalog"))
            self._filters = ItemFilters.from_list(stored.get("filters"))
            self._server = normalize_key(stored.get("server"))
            # Pre-v5 rows carry no server. The plugin only ever tracked one at a
            # time back then, so the one it remembered is where they belong —
            # leaving them at "" would empty an upgrading merchant's history the
            # moment they picked their own server back out of the box.
            self._history = PriceHistory.from_list(
                stored.get("history"), default_server=self._server
            )
            nested = _as_int(stored.get("schema_version"), 1) >= 5
            self._wanted = _by_server(
                stored.get("wanted"),
                self._server,
                lambda rows: [str(name) for name in rows if str(name).strip()],
            )
            self._listings = _by_server(stored.get("listings"), self._server, _read_listings)
            self._prices = _read_prices(stored.get("prices"), self._server, nested=nested)
            # v2 stored a bare 6-month average per name. Carry them over as
            # single-window records so an upgrading user's Fill button keeps
            # working before the first fetch; the counts fill in on the next one.
            legacy = stored.get("averages")
            if isinstance(legacy, dict) and not self._prices:
                bucket = self._prices.setdefault(self._server, {})
                for name, value in legacy.items():
                    average = _as_int(value, 0)
                    if average > 0:
                        bucket[str(name).casefold()] = MarketRecord(
                            name=str(name),
                            averages={"6mo": average},
                            counts={},
                            server=self._server,
                        )
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
                "show_ids": self._show_ids,
                "prefix": self._prefix,
                "nicknames": self._nicknames.to_dict(),
                "catalog": self._catalog.to_dict(),
                "filters": self._filters.to_list(),
                "history": self._history.to_list(),
                "server": self._server,
                "inventories": self._vault.to_dict(),
                # Server-keyed from v5 on. The key is a server key, and ""
                # is a real one — the bucket a dump loaded with EQ closed and
                # nothing chosen yet belongs to.
                "wanted": {server: list(names) for server, names in self._wanted.items()},
                "prices": {
                    server: {key: record.to_dict() for key, record in bucket.items()}
                    for server, bucket in self._prices.items()
                },
                "listings": {
                    server: [
                        {
                            "item_id": item.item_id,
                            "name": item.name,
                            "price": item.price,
                            "character": item.character,
                        }
                        for item in rows
                    ]
                    for server, rows in self._listings.items()
                },
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
                "show_ids": self._show_ids,
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
            if "show_ids" in values:
                self._show_ids = bool(values["show_ids"])
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


def _by_server(stored: object, legacy_server: str, read) -> dict[str, list]:
    """Read a v5 ``{server: [rows]}`` map, or file a v4 flat list under one key.

    The two shapes are told apart structurally rather than by schema version,
    because a store written by a newer build and opened by an older one is a
    real thing that happens to anyone who rolls a release back.
    """
    if isinstance(stored, dict):
        return {
            normalize_key(server): read(rows)
            for server, rows in stored.items()
            if isinstance(rows, list)
        }
    if isinstance(stored, list):
        return {legacy_server: read(stored)}
    return {}


def _read_listings(rows: object) -> list[Listing]:
    if not isinstance(rows, list):
        return []
    return [
        Listing(
            item_id=int(row["item_id"]),
            name=str(row["name"]),
            price=str(row.get("price", "")),
            character=str(row.get("character", "")),
        )
        for row in rows
        if isinstance(row, dict) and _as_int(row.get("item_id"), 0) > 0 and row.get("name")
    ]


def _read_prices(
    stored: object, legacy_server: str, *, nested: bool
) -> dict[str, dict[str, MarketRecord]]:
    """Rebuild the per-server price cache from either storage shape.

    ``nested`` comes from the stored schema version rather than from sniffing
    the data: both shapes are ``{str: dict}`` at the top level, and a v4 item
    named after a server would be enough to make a structural guess wrong.

    A v4 record carries the server it was fetched for in its own body, so the
    flat map re-files itself accurately rather than being dumped wholesale into
    whichever server happens to be current.
    """
    prices: dict[str, dict[str, MarketRecord]] = {}
    if not isinstance(stored, dict):
        return prices
    if nested:
        for server, rows in stored.items():
            if not isinstance(rows, dict):
                continue
            bucket = prices.setdefault(normalize_key(server), {})
            for name, row in rows.items():
                record = MarketRecord.from_dict(row)
                if record is not None:
                    bucket[str(name).casefold()] = record
        return prices
    for key, raw in stored.items():
        record = MarketRecord.from_dict(raw)
        if record is not None:
            where = normalize_key(record.server) or legacy_server
            prices.setdefault(where, {})[str(key).casefold()] = record
    return prices


def create_plugin() -> MerchantModePlugin:
    return MerchantModePlugin()
