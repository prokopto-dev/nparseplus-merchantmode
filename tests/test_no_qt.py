"""Domain modules must import without Qt.

Mirrors the host's own ``tests/test_architecture.py``: it imports every module
in the Qt-free packages with PySide6 poisoned and fails if anything pulls it in.
Without a test like this the rule is aspirational — one stray convenience
import at the top of a domain module and the plugin stops being loadable by
``nparseplus-plugin validate``, by CI, and by these tests.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

import pytest

import merchant_mode

QT_FREE_MODULES = [
    "merchant_mode",
    "merchant_mode.auctions",
    "merchant_mode.catalog",
    "merchant_mode.inventory",
    "merchant_mode.itemlink",
    "merchant_mode.macros",
    "merchant_mode.nicknames",
    "merchant_mode.packing",
    "merchant_mode.socialpack",
]


@pytest.fixture
def poisoned_qt(monkeypatch: pytest.MonkeyPatch):
    """Make any PySide6 import raise, however it is spelled.

    Re-importing ``merchant_mode`` under the guard replaces the modules in
    ``sys.modules`` with a fresh generation, and its enums and classes are then
    *different objects* from the ones tests imported at collection time — an
    ``is`` comparison against ``PriceSource.OBSERVED`` would fail while the
    repr looked identical. So the original modules are put back afterwards.
    """
    real_import = builtins.__import__

    def guard(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "PySide6" or name.startswith("PySide6."):
            raise AssertionError(f"domain code imported Qt: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guard)
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            monkeypatch.delitem(sys.modules, name)

    saved = {
        name: module for name, module in sys.modules.items() if name.startswith("merchant_mode")
    }
    yield
    for name in [name for name in sys.modules if name.startswith("merchant_mode")]:
        del sys.modules[name]
    sys.modules.update(saved)


def _drop_merchant_mode() -> None:
    """Force the next import to build a fresh copy under the guard."""
    for name in list(sys.modules):
        if name.startswith("merchant_mode"):
            del sys.modules[name]


@pytest.mark.parametrize("module_name", QT_FREE_MODULES)
def test_module_imports_without_qt(module_name: str, poisoned_qt) -> None:
    _drop_merchant_mode()
    importlib.import_module(module_name)


def test_the_plugin_activates_without_qt(poisoned_qt) -> None:
    for name in list(sys.modules):
        if name.startswith("merchant_mode"):
            del sys.modules[name]
    from nparseplus_sdk.testing import FakePluginContext

    module = importlib.import_module("merchant_mode")
    plugin = module.create_plugin()
    plugin.activate(FakePluginContext(plugin.meta))
    # The window is registered as a factory, so nothing Qt is built yet.
    assert len(plugin.build().socials) == 0


def test_the_window_module_is_only_imported_inside_factories() -> None:
    """A guard against ``from .window import ...`` drifting to module level.

    That single line would make the package unimportable everywhere Qt is
    absent, so it is worth asserting structurally rather than trusting review.
    """
    source = Path(merchant_mode.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        if "from .window import" in line:
            assert line.startswith(" " * 8), f"window import not inside a factory: {line!r}"
