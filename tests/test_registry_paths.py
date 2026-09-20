"""Tests for external plugin discovery: ``discover_paths`` / ``plugin_paths``.

Covers EXPLORER_TEMPLATES.md §2.1's discovery contract (synthetic
``enterprise_sim_templates.<slug>`` package, ``<dir>/<slug>/plugin.py`` and bare
``<dir>/<name>.py``, idempotent via ``sys.modules``, ``_``-prefixed skipped,
existing dirs only, env var merge) plus the ``RunConfig.plugins`` field and its
exclusion from the config digest (``compute_config_digest``, like ``scale``).

Every fixture uses a globally-unique slug (via ``uuid4``) because the four
plugin registries and ``sys.modules`` are process-wide singletons shared by the
whole test session — reusing a slug across tests would either raise a
duplicate-registration error or (worse) silently short-circuit on the
``sys.modules`` cache from an earlier test's fixture.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
from enterprise_sim.core.config import CompanyConfig, CompanySize, RunConfig, SimulationConfig
from enterprise_sim.core.registry import ARCHETYPES, discover_paths, plugin_paths


def _slug(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _write_archetype_plugin(root: Path, slug: str) -> Path:
    """A minimal ``<root>/<slug>/plugin.py`` registering a DepartmentArchetypeSpec."""
    directory = root / slug
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text(
        "from enterprise_sim.archetypes._base import DepartmentArchetypeSpec, TeamShape\n"
        "from enterprise_sim.core.registry import ARCHETYPES\n\n"
        "ARCHETYPES.register(\n"
        "    DepartmentArchetypeSpec(\n"
        f"        name={slug!r},\n"
        "        charter='test',\n"
        "        typical_goals=(),\n"
        "        team_shapes=(TeamShape(title='Core', count='1..2', skills=()),),\n"
        "        playbooks=(),\n"
        "    )\n"
        ")\n",
        encoding="utf-8",
    )
    return directory


# -- discover_paths ----------------------------------------------------------


def test_discover_paths_loads_a_fixture_template(tmp_path: Path) -> None:
    slug = _slug("dp_load")
    _write_archetype_plugin(tmp_path, slug)

    imported = discover_paths([tmp_path])

    assert f"enterprise_sim_templates.{slug}" in imported
    assert slug in ARCHETYPES
    assert ARCHETYPES.get(slug).name == slug


def test_discover_paths_registers_only_once(tmp_path: Path) -> None:
    slug = _slug("dp_once")
    _write_archetype_plugin(tmp_path, slug)

    first = discover_paths([tmp_path])
    second = discover_paths([tmp_path])  # idempotent: no DuplicateRegistrationError

    assert f"enterprise_sim_templates.{slug}" in first
    assert second == []  # already cached in sys.modules; nothing new imported
    assert slug in ARCHETYPES


def test_discover_paths_loads_bare_py_files(tmp_path: Path) -> None:
    slug = _slug("dp_bare")
    (tmp_path / f"{slug}.py").write_text(
        "from enterprise_sim.archetypes._base import DepartmentArchetypeSpec, TeamShape\n"
        "from enterprise_sim.core.registry import ARCHETYPES\n\n"
        "ARCHETYPES.register(\n"
        "    DepartmentArchetypeSpec(\n"
        f"        name={slug!r}, charter='t', typical_goals=(),\n"
        "        team_shapes=(TeamShape(title='C', count='1..1', skills=()),), playbooks=(),\n"
        "    )\n"
        ")\n",
        encoding="utf-8",
    )

    imported = discover_paths([tmp_path])

    assert f"enterprise_sim_templates.{slug}" in imported
    assert slug in ARCHETYPES


def test_discover_paths_skips_underscore_prefixed(tmp_path: Path) -> None:
    slug = _slug("dp_skip")
    directory = tmp_path / f"_{slug}"
    directory.mkdir()
    (directory / "plugin.py").write_text(
        "raise RuntimeError('must not import')\n", encoding="utf-8"
    )
    (tmp_path / f"_{slug}.py").write_text(
        "raise RuntimeError('must not import')\n", encoding="utf-8"
    )

    imported = discover_paths([tmp_path])  # would raise if either file were imported

    assert imported == []


def test_discover_paths_skips_dir_without_plugin_py(tmp_path: Path) -> None:
    slug = _slug("dp_noplugin")
    (tmp_path / slug).mkdir()
    (tmp_path / slug / "template.json").write_text("{}", encoding="utf-8")

    assert discover_paths([tmp_path]) == []


def test_discover_paths_skips_missing_directory(tmp_path: Path) -> None:
    assert discover_paths([tmp_path / "does-not-exist"]) == []


def test_discover_paths_failed_import_is_not_cached(tmp_path: Path) -> None:
    slug = _slug("dp_broken")
    directory = tmp_path / slug
    directory.mkdir()
    (directory / "plugin.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="boom"):
        discover_paths([tmp_path])

    # A retry (e.g. after the author fixes the bug) must not be a silent no-op
    # from a half-cached sys.modules entry.
    (directory / "plugin.py").write_text(
        "from enterprise_sim.core.registry import PROCESSES\n"
        "from dataclasses import dataclass\n\n"
        "@dataclass(slots=True)\n"
        "class _P:\n"
        "    name: str\n"
        "    emits: tuple[str, ...] = ()\n"
        "    requests: tuple[str, ...] = ()\n\n"
        f"PROCESSES.register(_P({slug!r}))\n",
        encoding="utf-8",
    )
    imported = discover_paths([tmp_path])
    assert f"enterprise_sim_templates.{slug}" in imported


# -- plugin_paths --------------------------------------------------------------


def test_plugin_paths_filters_to_existing_dirs(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    missing = tmp_path / "missing"

    result = plugin_paths([real, missing])

    assert result == [real.resolve()]


def test_plugin_paths_merges_config_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("ENTERPRISE_SIM_PLUGIN_PATH", str(b))

    result = plugin_paths([a])

    assert result == [a.resolve(), b.resolve()]


def test_plugin_paths_dedupes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = tmp_path / "a"
    a.mkdir()
    monkeypatch.setenv("ENTERPRISE_SIM_PLUGIN_PATH", str(a))

    result = plugin_paths([a, a])

    assert result == [a.resolve()]


def test_plugin_paths_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENTERPRISE_SIM_PLUGIN_PATH", raising=False)
    assert plugin_paths() == []


# -- config digest excludes plugins (like scale/output_dir) --------------------


def _run_config(output_dir: Path, plugins: tuple[str, ...] = ()) -> RunConfig:
    return RunConfig(
        company=CompanyConfig(name="PluginsCo", vertical="software", size=CompanySize.STARTUP),
        simulation=SimulationConfig(period_start=date(2026, 1, 5), period_end=date(2026, 1, 9)),
        output_dir=output_dir,
        plugins=plugins,
    )


def test_plugins_field_excluded_from_config_digest(tmp_path: Path) -> None:
    from enterprise_sim.assembly import compute_config_digest, compute_run_id

    without = _run_config(tmp_path / "a")
    with_plugins = _run_config(tmp_path / "b", plugins=(str(tmp_path / "templates"),))

    assert compute_config_digest(without) == compute_config_digest(with_plugins)
    assert compute_run_id(without) == compute_run_id(with_plugins)


def test_plugins_field_defaults_to_empty_tuple() -> None:
    config = _run_config(Path("runs"))
    assert config.plugins == ()
