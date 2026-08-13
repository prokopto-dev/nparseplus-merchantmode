# Merchant Mode — notes for working in this repo

An nParse+ plugin: read an `/outputfile inventory` dump, pick what you're
selling, price it, export a macro pack the host's Macro Editor imports. The
`README.md` explains what it does and why; this file is about how to change it.

## The rule that shapes everything: one server at a time

**Items cannot move between P99 servers.** An item on Blue can never be sold to
a buyer on Green, so anything that touches a trade is keyed by server and there
is no combined view of it anywhere:

| Split by server | Why |
| --- | --- |
| Holdings (`holdings`, `find_holdings`, `locate`) | You can only sell what is on the buyer's server |
| Ticked listings and the macro pack | One `/auc` line reaches one channel |
| The WTB list | It sits next to per-server buy prices |
| PigParse records (`market_for`) | PigParse keys its averages on server |
| `/auc` sightings (`Observation.server`) | A Blue ask is not evidence about a Green price |
| The `NameMatcher` | Two items sharing an acronym across servers is not a real ambiguity |

Three things are deliberately **not** split, and each has a reason that is
about items rather than about markets:

- **Item ids** (`catalog.py`) — the game assigns them globally.
- **The filter list** (`filters.py`) — junk is junk on every server.
- **The item-name index** (`itemnames.py`) — an item's *existence* is a fact
  about the game; only its price and location are facts about a market.

Plus one UI exception: the **Dumps tab** lists every server's dumps, because it
manages dumps rather than trading with them and a Blue dump you can't see is a
Blue dump you can't reload or forget. Every row there names its server.

The convention in the plugin API is `server: str | None = None` meaning *the
server in play*, with `""` a real and reachable bucket (a dump loaded before any
server was chosen). `""` is never a synonym for "unspecified" — passing it means
the unfiled bucket specifically. `_server_key()` resolves the two.

`all_holdings()` is the single unscoped read and exists for the name index.
Nothing that prices, lists or advertises an item may call it.

## Architecture

- **Domain code is Qt-free** and stdlib-only. `window.py` is the only module
  that imports PySide6, and `merchant_mode/__init__.py` imports it exclusively
  *inside* the window factories. `tests/test_no_qt.py` enforces both — add every
  new domain module to its `QT_FREE_MODULES` list.
- **Imports follow the SDK's rules, and `tests/test_imports.py` enforces them**
  — because every one of them fails only in the app, after CI and the validator
  have gone green. Siblings are imported relatively (the host imports this
  package as `nparseplus_user_plugins.merchant_mode`, so `from merchant_mode.x
  import ...` resolves in this checkout and nowhere else); the SDK is used
  through `nparseplus_sdk` itself and its documented `events`/`timers`/`ui`
  re-exports, never `nparseplus_sdk.plugin` and friends; `nparseplus` is
  imported inside the function that needs it, guarded, never at module scope;
  and nothing comes from PyPI, since a frozen build has no pip. That test also
  loads the package through `load_plugin_factory`, the way the app does.
- **The host's dump library is not plugin API.** `PluginContext` exposes no
  dumps, so auto-ingest subscribes to `CharacterDumpImportedEvent` /
  `CharacterDumpUpdatedEvent` (the `nparseplus_sdk.events` re-export, host-only,
  guarded) and reads the snapshot they *name* with plain `json.load` —
  `inventory.parse_snapshot_file`, mapped onto the same `InventoryItem` rows the
  TSV parser produces. Two traps: the event's `server` is effectively always
  `""`, because P99 writes `<Character>-Inventory.txt` and the host only reads a
  server out of a `Name_Server-Kind.txt` spelling, so it is resolved exactly the
  way `load_dump()` resolves it; and the snapshot carries a `schema_version`
  that `SNAPSHOT_SCHEMA_VERSION` pins against, because a bag slot read out of a
  shape that changed is worse than no row at all. Field names are asserted
  against the host's own writer in `tests/test_host_events.py`, which skips
  wherever the app isn't installed — including CI.
- Decisions live below the Qt line. `chartdata.py` decides what a chart should
  say; `PriceChartWidget` only puts it on screen. `finding.py` ranks; the table
  only lists. If a rule can be unit-tested without a display, it belongs there.
- Threading, per the SDK contract: `activate()` on the GUI thread, `CommsEvent`
  and the tick on the driver thread, the window reading a locked `snapshot()` on
  a QTimer. Network I/O only inside `ctx.submit`. The tick's budget is 250 ms —
  it does a due-check and nothing else.
- The window redraws only when `snapshot()["version"]` changes. Any action that
  changes something the counter can't see calls `self._reload()`.

## House style

- **Comments say why, not what.** Almost every non-obvious line here carries the
  reasoning that produced it, usually naming the failure it prevents. Match that
  density; a change that removes a reason should replace it with a better one.
- **Never hide a row silently.** Filtered items are counted on the Sell tab's
  status line and on the Filters tab. A list quietly missing rows is worse than
  a cluttered one.
- **Meet the gesture where it happens.** Every Sell-tab action is on both a
  button and the row's right-click menu (`_items_menu`), because a feature you
  can only find by reading the button bar is one most people never find. New
  row actions belong in both. Keep the menu buildable without a pointer —
  `_items_menu()` returns the `QMenu` and `_on_items_context_menu` only pops
  it, which is what lets tests read and trigger entries. Anything that would
  `exec()` a modal (`QMenu`, `QInputDialog`, `QMessageBox`) has to be patchable
  from a test or reachable through a seam that isn't.
- **Say when you don't know.** A stale dump reports its age, an unattributed
  price is refused, a disputed item id is marked. The failure mode designed
  against throughout is a *confident mistake*, not a miss.
- Storage is versioned (`SCHEMA_VERSION`). Every version must be readable by the
  next one, and migrations go in `_restore` with a comment explaining what the
  old shape meant. Bump the constant and update `test_saving_stamps_the_current_schema_version`.

## Working on it

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/nparseplus-plugin validate merchant_mode
```

CI installs the **SDK only** — no PySide6, no host app — so `tests/test_window.py`,
`tests/test_capture_screenshots.py` and `tests/test_host_events.py` skip there.
Run them locally with the app installed and `QT_QPA_PLATFORM=offscreen`.

Ruff config lives in `pyproject.toml` (line length 100). Run `ruff check` on the
files you touched; the tree is not `ruff format`-clean, so don't reformat the
whole repo in a change about something else.

### Screenshots

The README's images are real offscreen renders, not mockups:

```bash
.venv/bin/python tools/capture_screenshots.py            # all
.venv/bin/python tools/capture_screenshots.py --only window--sell
```

If you change the window, regenerate them, and keep the seed producing the
states the prose claims — `tests/test_capture_screenshots.py` asserts the seed
still yields a CONFLICT badge, a month-stale mule, a split market, and a filter
rule of each kind. Adding a shot means adding it to `SHOTS`, to that test's
recipe list, and to the README.

If the smoke test starts failing on the captured size, the window's *minimum*
grew past `WINDOW_SIZE`; find the widget that did it (usually an unwrapped
`QLabel`, whose minimum width is its whole sentence) rather than nudging the
constant.
