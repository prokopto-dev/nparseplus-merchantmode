"""The SDK's import rules, enforced structurally rather than by review.

nParse+ imports a plugin package as ``nparseplus_user_plugins.merchant_mode``
via ``spec_from_file_location`` — never through ``sys.path``. Three
consequences, each of which is a rule this module asserts because each one
fails *only* in the real app, long after the tests and the validator have
gone green:

1. **Own modules are imported relatively.** ``from merchant_mode.market
   import ...`` resolves fine here, where the repo root is on ``sys.path``,
   and raises ``ModuleNotFoundError`` inside the app, where the package does
   not answer to that name.
2. **Only the SDK's published surface.** The package root and the lazy
   re-export modules (``events``, ``timers``, ``ui``) are the contract;
   ``nparseplus_sdk.plugin`` and friends are where it happens to live today.
3. **Nothing from PyPI, and nothing from the host at module scope.** End users
   run a frozen build: there is no pip, no site-packages, and ``nparseplus``
   itself is absent from CI and from ``nparseplus-plugin validate``, both of
   which import this package.

:mod:`tests.test_no_qt` covers the fourth rule — no Qt above the window.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import merchant_mode

PACKAGE_DIR = Path(merchant_mode.__file__).parent
PACKAGE_NAME = merchant_mode.__name__

SOURCES = sorted(PACKAGE_DIR.rglob("*.py"))

SDK_PUBLIC_MODULES = {
    "nparseplus_sdk",  # the package root: the whole versioned contract
    "nparseplus_sdk.events",  # lazy host re-exports, documented as such
    "nparseplus_sdk.timers",
    "nparseplus_sdk.ui",
}

# The only module allowed to import Qt and to touch the host at module scope,
# because it is itself only ever imported from inside a window factory.
QT_MODULE = "window.py"

ALLOWED_THIRD_PARTY = {"PySide6"}
"""Distributions the app is known to ship. Kept to the one this plugin needs:
every addition is a bet that the app will keep bundling it, and the app's
dependencies are its own business rather than part of the SDK contract."""


def _module_scope_imports(tree: ast.AST):
    """Imports that run when the module is imported.

    Function bodies are skipped — that is where a host import belongs, since
    it must not fire until the plugin is running inside the app. A class body
    or a top-level ``if`` runs at import time and is therefore module scope.
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield node
        else:
            yield from _module_scope_imports(node)


def _roots(node: ast.Import | ast.ImportFrom) -> list[str]:
    """The dotted names an import statement reaches for, absolute ones only."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:  # a relative import names nothing absolute
        return []
    return [node.module or ""]


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_own_modules_are_imported_relatively(path: Path) -> None:
    """No sibling is reached by the name only this repo knows it by."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for name in _roots(node):
            assert name.split(".")[0] != PACKAGE_NAME, (
                f"{path.name}:{node.lineno} imports {name!r} absolutely — the host "
                f"imports this package as nparseplus_user_plugins.{PACKAGE_NAME}, "
                "so use a relative import"
            )


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_the_sdk_is_used_through_its_published_surface(path: Path) -> None:
    """``nparseplus_sdk.plugin`` works today and is not the contract.

    The SDK promises semantic versioning for what its package root exports
    plus the three lazy re-export modules. Everything else is layout.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for name in _roots(node):
            if name.split(".")[0] != "nparseplus_sdk":
                continue
            assert name in SDK_PUBLIC_MODULES, (
                f"{path.name}:{node.lineno} imports {name!r} — import from "
                "nparseplus_sdk itself, or from its documented events/timers/ui "
                "modules"
            )


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_the_host_is_never_imported_at_module_scope(path: Path) -> None:
    """The host is absent wherever this package is merely *imported*.

    CI, the validator and the unit tests all import it with nothing but the
    SDK installed, and the lazy re-exports raise ``ImportError`` there. So a
    host import belongs inside the function that needs it, guarded — which is
    what lets the plugin register everything host-free first and degrade to a
    log line instead of failing to load.
    """
    if path.name == QT_MODULE:
        return  # only ever imported from a window factory, i.e. inside the app
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in _module_scope_imports(tree):
        for name in _roots(node):
            root = name.split(".")[0]
            if root == "nparseplus" or name in SDK_PUBLIC_MODULES - {"nparseplus_sdk"}:
                pytest.fail(
                    f"{path.name}:{node.lineno} imports {name!r} at module scope — "
                    "the host is not importable in CI or in nparseplus-plugin "
                    "validate; import it inside the function that needs it"
                )


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_nothing_comes_from_pypi(path: Path) -> None:
    """A frozen build has no pip and no site-packages.

    An import that works in this checkout and nowhere else is the failure
    mode: it passes CI, passes the validator, and breaks for every user who
    installed the zip.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for name in _roots(node):
            root = name.split(".")[0]
            if root in {"nparseplus", "nparseplus_sdk", PACKAGE_NAME, ""}:
                continue
            if root in sys.stdlib_module_names or root in ALLOWED_THIRD_PARTY:
                continue
            pytest.fail(
                f"{path.name}:{node.lineno} imports {root!r}, which the frozen "
                "app has no way to install — vendor it or do without"
            )


def test_the_package_loads_the_way_the_host_loads_it() -> None:
    """The only check here that runs the loader instead of reading the source.

    Everything above is a static scan and would miss, say, a relative import
    of a module that isn't in the zip. This imports the package under the
    private namespace the app uses, from the directory, exactly as
    ``nparseplus-plugin validate`` does.
    """
    from nparseplus_sdk.loading import load_plugin_factory

    saved = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith("nparseplus_user_plugins")
    }
    try:
        plugin = load_plugin_factory(PACKAGE_DIR)()
        assert plugin.meta.id == "merchant-mode"
    finally:
        # A second copy of every module, under other names, would otherwise
        # outlive the test and make an `is` comparison in a later one lie.
        for name in [
            name for name in sys.modules if name.startswith("nparseplus_user_plugins")
        ]:
            del sys.modules[name]
        sys.modules.update(saved)
