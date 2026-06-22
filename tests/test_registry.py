"""Tests for the four plugin registries, discovery, and the binding map.

Covers the acceptance criteria for esim-db21ada2: register + lookup +
duplicate-detection, and the ``deliverable.kind → producer`` binding map.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from enterprise_sim.core.registry import (
    ARCHETYPES,
    PLAYBOOKS,
    PROCESSES,
    PRODUCERS,
    BindingError,
    BindingMap,
    DuplicateRegistrationError,
    NamedPlugin,
    Producer,
    Registry,
    UnknownPluginError,
    discover,
    discover_all,
)


@dataclass
class _Plugin:
    """A minimal name-bearing plugin for exercising the generic registry."""

    name: str
    formats: Sequence[str] = field(default_factory=list)
    handles: Sequence[str] = field(default_factory=list)


# --- register + lookup ------------------------------------------------------


def test_register_then_lookup() -> None:
    registry: Registry[_Plugin] = Registry("widget")
    plugin = _Plugin("alpha")
    assert registry.register(plugin) is plugin  # returns the plugin (decorator use)
    assert registry.get("alpha") is plugin
    assert "alpha" in registry
    assert registry.names() == ["alpha"]
    assert len(registry) == 1


def test_register_works_as_decorator() -> None:
    registry: Registry[NamedPlugin] = Registry("widget")

    @registry.register
    class Beta:
        name = "beta"

    assert registry.get("beta").name == "beta"


def test_insertion_order_preserved() -> None:
    registry: Registry[_Plugin] = Registry("widget")
    for name in ("c", "a", "b"):
        registry.register(_Plugin(name))
    assert registry.names() == ["c", "a", "b"]
    assert [p.name for p in registry] == ["c", "a", "b"]


def test_lookup_unknown_raises() -> None:
    registry: Registry[_Plugin] = Registry("widget")
    with pytest.raises(UnknownPluginError):
        registry.get("missing")


# --- duplicate detection ----------------------------------------------------


def test_duplicate_registration_rejected() -> None:
    registry: Registry[_Plugin] = Registry("widget")
    registry.register(_Plugin("dup"))
    with pytest.raises(DuplicateRegistrationError):
        registry.register(_Plugin("dup"))
    # the original survives; the registry is unchanged.
    assert len(registry) == 1


def test_items_and_clear() -> None:
    registry: Registry[_Plugin] = Registry("widget")
    registry.register(_Plugin("x"))
    snapshot = registry.items()
    snapshot.clear()  # type: ignore[attr-defined]
    assert "x" in registry  # snapshot is a copy; registry untouched
    registry.clear()
    assert len(registry) == 0


# --- the four default registries are distinct -------------------------------


def test_default_registries_are_distinct_and_empty_by_default() -> None:
    assert ARCHETYPES.kind == "archetype"
    assert PLAYBOOKS.kind == "playbook"
    assert PROCESSES.kind == "process"
    assert PRODUCERS.kind == "producer"
    # four distinct catalog objects, not aliases of one.
    assert len({id(ARCHETYPES), id(PLAYBOOKS), id(PROCESSES), id(PRODUCERS)}) == 4


# --- binding map: deliverable.kind -> producer ------------------------------


def test_binding_map_default_fallback() -> None:
    bindings = BindingMap(default="markdown")
    # every kind maps to the default when unbound (v1 markdown-only).
    assert bindings.producer_names("status_report") == ["markdown"]
    assert bindings.producer_names("design_doc") == ["markdown"]


def test_binding_map_explicit_overrides_default() -> None:
    bindings = BindingMap(default="markdown")
    bindings.bind("status_report", "word")
    assert bindings.producer_names("status_report") == ["word"]
    assert bindings.producer_names("design_doc") == ["markdown"]  # still default


def test_binding_map_one_to_many() -> None:
    bindings = BindingMap()
    bindings.bind("status_report", "word", "jira", "outlook")
    bindings.bind("status_report", "jira")  # de-duplicated, order preserved
    assert bindings.producer_names("status_report") == ["word", "jira", "outlook"]


def test_binding_map_unbound_without_default_raises() -> None:
    bindings = BindingMap()
    with pytest.raises(BindingError):
        bindings.producer_names("status_report")


def test_binding_map_resolves_against_registry() -> None:
    producers: Registry[Producer] = Registry("producer")
    md = producers.register(_Plugin("markdown", formats=["markdown"]))
    word = producers.register(_Plugin("word", formats=["docx"]))
    bindings = BindingMap(default="markdown")
    bindings.bind("design_doc", "word")
    assert bindings.resolve("design_doc", producers) == [word]
    assert bindings.resolve("status_report", producers) == [md]  # via default


def test_binding_map_resolve_missing_producer_raises() -> None:
    producers: Registry[Producer] = Registry("producer")
    bindings = BindingMap()
    bindings.bind("design_doc", "nonexistent")
    with pytest.raises(UnknownPluginError):
        bindings.resolve("design_doc", producers)


def test_binding_map_from_config() -> None:
    bindings = BindingMap.from_config(
        {"status_report": "word", "design_doc": ["word", "jira"]},
        default="markdown",
    )
    assert bindings.producer_names("status_report") == ["word"]
    assert bindings.producer_names("design_doc") == ["word", "jira"]
    assert bindings.producer_names("kickoff_deck") == ["markdown"]
    assert bindings.default == "markdown"


# --- discovery --------------------------------------------------------------


def test_discover_imports_public_submodules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Discovery imports every public submodule (firing its registration side
    # effects) and skips ``_``-prefixed ones. Use a synthetic package so the
    # assertion stays deterministic regardless of what the real plugin packages
    # ship (they fill up as plugins land).
    pkg = tmp_path / "fake_plugins"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "alpha.py").write_text("IMPORTED = True\n")
    (pkg / "beta.py").write_text("IMPORTED = True\n")
    # If discovery wrongly imported this, the import would raise and fail the test.
    (pkg / "_private.py").write_text("raise RuntimeError('private module imported')\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    found = discover("fake_plugins")

    assert sorted(found) == ["alpha", "beta"]
    assert "_private" not in found
    assert "fake_plugins.alpha" in sys.modules  # the module was really imported


def test_discover_handles_populated_package() -> None:
    # The producers package ships a real module on main (the OOXML spike); the
    # registry must discover populated packages gracefully, not assume emptiness.
    found = discover("enterprise_sim.producers")
    assert isinstance(found, list)
    assert all(isinstance(module_name, str) for module_name in found)


def test_discover_all_covers_four_kinds() -> None:
    found = discover_all()
    assert set(found) == {"archetype", "playbook", "process", "producer"}
    assert all(isinstance(modules, list) for modules in found.values())
