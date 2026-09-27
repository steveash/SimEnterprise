"""``world adopt``: migrating pre-``.world`` run + data-run directories into one world."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from enterprise_sim.assembly import execute_run
from enterprise_sim.cli import main
from enterprise_sim.core.config import WorldConfig, load_config_from_mapping
from enterprise_sim.data_products.config import DataRunConfig
from enterprise_sim.data_products.runner import execute_data_run
from enterprise_sim.worlds import WORLD_FILE, cache_dir, is_world_dir, read_world
from enterprise_sim.worlds.adopt import AdoptError, adopt_run_dir


def _config(worlds_root: Path, *, cache_dir: Path | None = None) -> WorldConfig:
    config = load_config_from_mapping(
        {
            "company": {"name": "Legacy Co", "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": 7,
            "worlds_root": str(worlds_root),
        }
    )
    if cache_dir is not None:
        config = config.model_copy(
            update={"scale": config.scale.model_copy(update={"cache_dir": str(cache_dir)})}
        )
    return config


def _downgrade_to_old_layout(world_dir: Path, old_dir: Path) -> None:
    """Rewrite a fresh world as the pre-.world layout: manifest.json + config.snapshot.json."""
    world = read_world(world_dir)
    assert world is not None
    shutil.move(str(world_dir), str(old_dir))
    (old_dir / WORLD_FILE).unlink()
    shutil.rmtree(old_dir / ".cache", ignore_errors=True)
    shutil.rmtree(old_dir / ".jobs", ignore_errors=True)
    manifest = {
        "schema_version": "1.2",
        "run_id": world.world_id,
        "tool_version": world.tool_version,
        "seed": world.seed,
        "config_digest": world.config_digest,
        "render_backend": world.render_backend,
        "company": {k: v for k, v in world.company.items() if k != "description"},
        "window": world.window,
        "counts": world.counts,
        "validation": world.validation,
        "outputs": {"kg": "kg/", "artifacts": "artifacts/"},
        "generated_at": "2026-05-01T12:00:00+00:00",
    }
    (old_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    snapshot = {**world.config, "output_dir": "runs/legacy"}  # the old field name + an old path
    (old_dir / "config.snapshot.json").write_text(json.dumps(snapshot, indent=2))


def _downgrade_data_to_old_layout(world_dir: Path, scenario: str, old_dir: Path) -> None:
    """Rewrite <world>/data/<scenario> as the old standalone data-run folder."""
    world = read_world(world_dir)
    assert world is not None
    src = world_dir / "data" / scenario
    old_dir.mkdir(parents=True)
    (old_dir / "data").mkdir()
    shutil.move(str(src / "tables"), str(old_dir / "data" / "tables"))
    shutil.move(str(src / "views"), str(old_dir / "data" / "views"))
    for f in list(src.iterdir()):
        shutil.move(str(f), str(old_dir / f.name))
    shutil.rmtree(world_dir / "data")
    summary = world.data[scenario]
    manifest = {
        **{k: v for k, v in summary.items() if k not in ("dir", "rows_by_table")},
        "config": {"scenarios": [scenario], "seed": 7, "output_dir": "runs/legacy-data"},
    }
    (old_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


@pytest.fixture
def legacy(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An old-style run dir (with an external cache) + an old-style data-run dir."""
    external_cache = tmp_path / ".runtime" / "llm-cache" / "legacy"
    result = execute_run(_config(tmp_path / "fresh", cache_dir=external_cache))
    data = execute_data_run(
        DataRunConfig.model_validate(
            {
                "scenarios": ["support_ops"],
                "seed": 7,
                "world": {"world_dir": str(result.world_dir)},
                "window": {"start": "2026-01-05", "end": "2026-01-18"},
                "scale": {"factor": 0.05, "rows_per_partition": 50_000},
                "loop": {"iterations": 1},
            }
        )
    )
    old_data = tmp_path / "runs" / "legacy-data" / "support-ops-abc123"
    _downgrade_data_to_old_layout(data.world_dir, "support_ops", old_data)
    old_run = tmp_path / "runs" / "legacy" / result.world_id
    old_run.parent.mkdir(parents=True)
    _downgrade_to_old_layout(result.world_dir, old_run)
    assert not is_world_dir(old_run)
    assert (old_run / "manifest.json").is_file()
    assert (old_data / "data" / "tables").is_dir()
    return old_run, old_data, external_cache


def test_adopt_in_place_writes_the_world_file_and_removes_the_old_state(
    legacy: tuple[Path, Path, Path],
) -> None:
    old_run, _, external_cache = legacy
    manifest = json.loads((old_run / "manifest.json").read_text())

    world_dir = adopt_run_dir(old_run)

    assert world_dir == old_run  # in place
    assert is_world_dir(world_dir)
    for stale in ("manifest.json", "config.snapshot.json", "lineage.json"):
        assert not (world_dir / stale).exists()
    world = read_world(world_dir)
    assert world is not None
    assert world.world_id == manifest["run_id"]  # pinned ids survive the migration.
    assert world.config_digest == manifest["config_digest"]
    assert world.counts == manifest["counts"]
    assert world.created_at == "2026-05-01T12:00:00+00:00"
    assert world.history[0]["kind"] == "create"
    assert world.history[0]["adopted_from"] == str(old_run)
    assert world.company["name"] == "Legacy Co"
    assert "output_dir" not in world.config and "worlds_root" not in world.config
    assert world.job.status == "done"
    assert world.job.artifacts_total == world.job.artifacts_done > 0
    # The external cache moved inside, and the config no longer points at it.
    assert world.config["scale"]["cache_dir"] is None
    assert {p.name for p in cache_dir(world_dir).iterdir()} == {
        p.name for p in external_cache.iterdir()
    }
    # The adopted world round-trips through the real config model.
    WorldConfig.model_validate(world.config)


def test_adopt_moves_under_a_worlds_root_and_folds_in_the_data_runs(
    legacy: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    old_run, old_data, _ = legacy
    root = tmp_path / "worlds"

    world_dir = adopt_run_dir(old_run, worlds_root=root, data_dirs=[old_data])

    assert world_dir == root / old_run.name
    assert not old_run.exists() and not old_data.exists()
    assert is_world_dir(world_dir)
    scenario_dir = world_dir / "data" / "support_ops"
    assert (scenario_dir / "tables").is_dir()
    assert (scenario_dir / "views").is_dir()
    for name in ("spec.json", "lineage.json", "questions.json"):
        assert (scenario_dir / name).is_file(), name
    assert not (scenario_dir / "manifest.json").exists()
    world = read_world(world_dir)
    assert world is not None
    assert set(world.data) == {"support_ops"}
    assert world.data["support_ops"]["dir"] == "data/support_ops/"
    assert world.data["support_ops"]["questions"]["total"] > 0
    assert world.data["support_ops"]["rows_by_table"]
    assert [h["kind"] for h in world.history] == ["create", "data"]
    assert world.history[1]["adopted_from"] == str(old_data)


def test_adopt_refuses_non_runs_and_existing_worlds(tmp_path: Path) -> None:
    with pytest.raises(AdoptError, match="not an old-style run"):
        adopt_run_dir(tmp_path)
    result = execute_run(_config(tmp_path / "worlds"))
    with pytest.raises(AdoptError, match="already is a world"):
        adopt_run_dir(result.world_dir)


def test_adopt_cli(
    legacy: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old_run, old_data, _ = legacy
    root = tmp_path / "worlds"
    assert (
        main(["world", "adopt", str(old_run), "--data", str(old_data), "--worlds-root", str(root)])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["world_dir"] == str(root / old_run.name)
    assert payload["data"] == ["support_ops"]
    assert is_world_dir(payload["world_dir"])

    assert main(["world", "adopt", str(tmp_path / "nope")]) == 2
