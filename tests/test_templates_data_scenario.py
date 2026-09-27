"""``data_scenario`` templates: scaffold -> validate -> catalog -> used by a world build.

Templates can author all three things a world is built from: departments and
playbooks (the organization and the events that become the document corpus +
graph) and data scenarios (structured tables, views and business questions).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from enterprise_sim.cli import main
from enterprise_sim.templates.catalog import build_catalog
from enterprise_sim.templates.scaffold import scaffold
from enterprise_sim.templates.store import TemplateKind, read_template
from enterprise_sim.templates.validate import validate
from enterprise_sim.worlds import read_world


def _slug() -> str:
    # Registries are process-wide singletons: every test needs a fresh name.
    return f"churn_{uuid.uuid4().hex[:8]}"


def test_scaffolded_data_scenario_validates_green(tmp_path: Path) -> None:
    slug = _slug()
    template = scaffold(
        tmp_path, slug=slug, kind="data_scenario", name="Churn Signals", description="Churn."
    )
    assert template.kind is TemplateKind.DATA_SCENARIO
    assert (tmp_path / slug / "plugin.py").is_file()

    report = validate(tmp_path, slug)
    steps = {s["step"]: s for s in report["steps"]}
    assert report["ok"], json.dumps(report, indent=2, default=str)
    assert report["provides"]["data_scenarios"] == [slug]
    assert steps["tests"]["ok"]
    [data] = steps["data_spec"]["detail"]
    assert data["scenario"] == slug
    assert data["rows_by_table"]["fact_account_day"] > 0
    assert data["questions"]["total"] == 2
    assert data["questions"]["with_gap_fix"] == 1
    # The gap question is unanswerable until its fix is applied — that is the
    # question loop's job during a real build, not a validation failure.
    assert data["questions"]["answerable_before_gap_fixes"] == 1
    assert read_template(tmp_path, slug).validation.status == "valid"


def test_a_data_scenario_template_is_in_the_catalogs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    slug = _slug()
    scaffold(tmp_path, slug=slug, kind="data_scenario", name="Churn Signals")
    catalog = build_catalog(tmp_path)
    by_name = {d["name"]: d for d in catalog["data_scenarios"]}
    assert by_name[slug]["source"] == f"template:{slug}"
    assert (
        by_name["support_ops"]["source"] == "template:support_ops"
    )  # a built-in is a template too

    monkeypatch.setenv("ENTERPRISE_SIM_PLUGIN_PATH", str(tmp_path))
    assert main(["world", "catalog"]) == 0
    world_catalog = json.loads(capsys.readouterr().out)
    assert slug in {d["name"] for d in world_catalog["data_scenarios"]}


def test_a_world_build_uses_a_data_scenario_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    templates = tmp_path / "templates"
    slug = _slug()
    scaffold(templates, slug=slug, kind="data_scenario", name="Churn Signals")
    monkeypatch.setenv("ENTERPRISE_SIM_PLUGIN_PATH", str(templates))

    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 5\n"
        '[company]\nname = "Templated Co"\nvertical = "software"\nsize = "startup"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-01-16\n"
        f'[data]\nscenarios = ["{slug}"]\nscale_factor = 0.1\niterations = 2\n'
        "rows_per_partition = 50000\n",
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    [world_dir] = list(root.iterdir())
    world = read_world(world_dir)
    assert world is not None
    assert slug in world.data
    # The loop applied the template's gap fix: both questions end answerable.
    assert world.data[slug]["questions"] == {"total": 2, "answerable": 2}
    assert (world_dir / "data" / slug / "tables" / "fact_account_day").is_dir()
