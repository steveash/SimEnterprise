"""``enterprise-sim world …`` CLI round-trips (``docs/EXPLORER_RUNS.md`` §3.4, §6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from enterprise_sim.cli import main
from enterprise_sim.worlds import (
    WORLD_FILE,
    cache_dir,
    control_path,
    is_world_dir,
    progress_log_path,
    read_world,
)


def _write_config(path: Path, *, seed: int = 7, ceiling: float | None = None) -> Path:
    lines = [
        f"seed = {seed}",
        '[company]\nname = "Acme"\nvertical = "software"\nsize = "small"',
        "[simulation]\nperiod_start = 2026-01-01\nperiod_end = 2026-01-31",
    ]
    if ceiling is not None:
        lines.append(f"[scale]\ncost_ceiling_usd = {ceiling}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _mint(tmp_path: Path, capsys: pytest.CaptureFixture[str], **kw: object) -> Path:
    """``world create --no-run`` → the minted world dir."""
    cfg = _write_config(tmp_path / "cfg.toml", **kw)  # type: ignore[arg-type]
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root), "--no-run"]) == 0
    created = json.loads(capsys.readouterr().out)
    return Path(created["world_dir"])


def _world(world_dir: Path):  # type: ignore[no-untyped-def]
    world = read_world(world_dir)
    assert world is not None
    return world


# --- catalog / estimate --------------------------------------------------------


def test_world_catalog_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["world", "catalog"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert {a["name"] for a in payload["archetypes"]} >= {"engineering", "retail"}
    for archetype in payload["archetypes"]:
        assert {"name", "charter", "typical_goals", "team_shapes", "playbooks"} <= set(archetype)
    for playbook in payload["playbooks"]:
        assert {"name", "vertical", "deliverables"} <= set(playbook)
    assert isinstance(payload["producers"], list)
    assert set(payload["backends"]) == {"anthropic_api", "bedrock", "claude_cli", "fake", "synth"}
    assert "claude-sonnet-4-6" in payload["models"]
    for pricing in payload["models"].values():
        assert {"input_per_mtok", "cached_input_per_mtok", "output_per_mtok"} <= set(pricing)
    assert set(payload["company_sizes"]) == {"startup", "small", "medium", "large", "enterprise"}
    assert "properties" in payload["schema"]  # a WorldConfig JSON schema.


def test_world_estimate_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _write_config(tmp_path / "cfg.toml")
    assert main(["world", "estimate", str(cfg)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["num_artifacts"] > 0
    assert payload["estimated_cost_usd"] >= 0
    assert payload["model"]
    assert payload["world"]["nodes"] > 0
    assert payload["world"]["departments"] == 1
    assert payload["world"]["scenarios"] == 1
    assert payload["events"] > 0


def test_world_estimate_reports_a_cost_ceiling_breach(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _write_config(tmp_path / "cfg.toml", ceiling=0.0)
    assert main(["world", "estimate", "-c", str(cfg)]) == 1
    assert json.loads(capsys.readouterr().out)["type"] == "CostCeilingExceeded"


# --- create / run / status / list ---------------------------------------------


def test_world_create_requires_a_config() -> None:
    assert main(["world", "create"]) == 2


def test_create_no_run_then_run_status_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    assert is_world_dir(world_dir)
    # One folder, one state file: none of the old scattered files exist.
    for stale in ("manifest.json", "config.snapshot.json", "job.json", "state.json", "config.json"):
        assert not (world_dir / stale).exists(), stale
    assert _world(world_dir).job.status == "created"
    assert _world(world_dir).world_id == world_dir.name

    assert main(["world", "status", str(world_dir)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "created"
    assert status["has_kg"] is False

    assert main(["world", "run", str(world_dir)]) == 0
    run_output = capsys.readouterr()
    progress_lines = [json.loads(line) for line in run_output.out.splitlines() if line]
    assert progress_lines  # streamed progress JSONL on stdout.
    assert progress_lines[-1]["kind"] in {"done", "phase"}
    assert "enterprise-sim world" in run_output.err  # the human note goes to stderr.

    log = progress_log_path(world_dir)
    assert log == world_dir / ".jobs" / "progress.jsonl"
    file_lines = [json.loads(line) for line in log.read_text().splitlines() if line]
    assert file_lines == progress_lines

    assert main(["world", "status", str(world_dir)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "done"
    assert status["world_dir"] == str(world_dir)
    assert status["has_kg"] is True
    assert status["counts"]["nodes"] > 0
    assert isinstance(status["evals"]["questions"], int)  # the eval set was generated.
    assert (world_dir / "evals" / "questions.jsonl").is_file()
    assert (cache_dir(world_dir)).is_dir()  # the response cache is world-local.

    assert main(["world", "list", "--worlds-root", str(world_dir.parent)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert [w["world_id"] for w in listing["worlds"]] == [world_dir.name]
    assert listing["worlds"][0]["status"] == "done"
    assert listing["worlds"][0]["history"][0]["kind"] == "create"


def test_create_builds_inline_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _write_config(tmp_path / "cfg.toml")
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "validated config for Acme" in out
    assert "built " in out and "acme-" in out
    [world_dir] = list(root.iterdir())
    first = _world(world_dir)
    assert first.job.status == "done"
    assert (world_dir / WORLD_FILE).is_file()

    # Same config again: the existing world is resumed (all cache hits), not duplicated.
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    assert "exists; resuming" in capsys.readouterr().out
    assert [p.name for p in root.iterdir()] == [world_dir.name]
    second = _world(world_dir)
    assert [h["kind"] for h in second.history] == ["create"]
    assert len(second.job.segments) == 2
    assert second.job.artifacts_cached == second.job.artifacts_total > 0


def test_create_refuses_a_different_config_for_an_existing_world(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    assert main(["world", "extend", str(world_dir), "--period-end", "2026-03-31", "--no-run"]) == 0
    capsys.readouterr()
    # The world's config moved on; recreating from the original file must not clobber it.
    cfg = tmp_path / "cfg.toml"
    assert main(["world", "create", str(cfg), "--worlds-root", str(world_dir.parent)]) == 2
    assert "different config" in capsys.readouterr().err


def test_world_pause_writes_the_control_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    assert main(["world", "pause", str(world_dir)]) == 0
    assert control_path(world_dir) == world_dir / ".jobs" / "control.json"
    assert json.loads(control_path(world_dir).read_text()) == {"pause": True}


def test_world_list_on_an_empty_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["world", "list", "--worlds-root", str(tmp_path / "nope")]) == 0
    assert json.loads(capsys.readouterr().out)["worlds"] == []


def test_world_status_of_an_unknown_world(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["world", "status", str(tmp_path / "no-such-world")]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_world_run_of_a_missing_world(tmp_path: Path) -> None:
    assert main(["world", "run", str(tmp_path / "no-such-world")]) == 2


# --- extend (in place) -----------------------------------------------------------


def test_extend_in_place_grows_the_world_and_keeps_its_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    assert main(["world", "run", str(world_dir)]) == 0
    capsys.readouterr()
    parent = _world(world_dir)
    parent_total = parent.job.artifacts_total
    assert parent_total > 0

    assert (
        main(
            [
                "world",
                "extend",
                str(world_dir),
                "--period-end",
                "2026-03-31",
                "--add-project",
                json.dumps({"name": "Extra Widget"}),
                "--no-run",
            ]
        )
        == 0
    )
    extended = json.loads(capsys.readouterr().out)
    assert extended["world_dir"] == str(world_dir)
    assert extended["world_id"] == parent.world_id
    assert extended["changes"]["added_projects"] == ["Extra Widget"]
    assert extended["changes"]["period_end"] == ["2026-01-31", "2026-03-31"]

    adopted = _world(world_dir)
    assert adopted.world_id == parent.world_id  # minted once, never changes.
    assert [h["kind"] for h in adopted.history] == ["create", "extend"]
    assert adopted.config["simulation"]["period_end"] == "2026-03-31"
    assert adopted.config["projects"][-1]["name"] == "Extra Widget"
    assert adopted.window["end"] == "2026-03-31"
    assert adopted.job.status == "created"
    assert adopted.config_digest != parent.config_digest

    assert main(["world", "run", str(world_dir)]) == 0
    capsys.readouterr()
    built = _world(world_dir)
    assert built.job.status == "done"
    assert built.job.artifacts_total > parent_total
    assert len(built.job.segments) == 2  # the ledger spans the world's whole life.
    assert built.config_digest == adopted.config_digest  # execute_run did not re-log an extend.
    assert [h["kind"] for h in built.history] == ["create", "extend"]
    assert [p.name for p in world_dir.parent.iterdir()] == [world_dir.name]  # still one folder.


def test_extend_rejects_an_earlier_period_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    assert main(["world", "extend", str(world_dir), "--period-end", "2025-01-01"]) == 2


def test_extend_with_nothing_to_change_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    assert main(["world", "extend", str(world_dir)]) == 2
    assert "nothing to extend" in capsys.readouterr().err


# --- fork (a parallel world) ---------------------------------------------------


def test_fork_makes_a_parallel_world_with_lineage_and_a_seeded_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    assert main(["world", "run", str(world_dir)]) == 0
    capsys.readouterr()
    parent = _world(world_dir)
    parent_cache = {p.name for p in cache_dir(world_dir).iterdir() if p.is_file()}
    assert parent_cache

    assert main(["world", "fork", str(world_dir), "--seed", "99", "--no-run"]) == 0
    forked = json.loads(capsys.readouterr().out)
    child_dir = Path(forked["world_dir"])
    assert child_dir.parent == world_dir.parent  # lands next to its parent by default.
    assert child_dir != world_dir
    assert is_world_dir(child_dir)
    child = _world(child_dir)
    assert child.world_id != parent.world_id
    assert child.seed == 99
    assert child.lineage == {
        "parent_world_id": parent.world_id,
        "parent_world_dir": str(world_dir),
        "changes": forked["changes"],
    }
    assert child.job.status == "created"
    # Copy-on-create: the fork starts with the parent's cache entries.
    child_cache = {p.name for p in cache_dir(child_dir).iterdir() if p.is_file()}
    assert parent_cache <= child_cache

    assert main(["world", "list", "--worlds-root", str(world_dir.parent)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert {w["world_id"] for w in listing["worlds"]} == {parent.world_id, child.world_id}

    # A fork that changes nothing would just be the same world: refused.
    assert main(["world", "fork", str(world_dir), "--no-run"]) == 2


# --- configure -------------------------------------------------------------------


def test_configure_patches_the_config_in_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    patch = json.dumps({"scale": {"cost_ceiling_usd": 5}})
    assert main(["world", "configure", str(world_dir), "--patch", patch]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["scale"]["cost_ceiling_usd"] == 5
    assert _world(world_dir).config["scale"]["cost_ceiling_usd"] == 5
    assert _world(world_dir).config["company"]["name"] == "Acme"  # nested merge, not replace.

    assert main(["world", "configure", str(world_dir), "--patch", "not json"]) == 2
    assert main(["world", "configure", str(world_dir), "--patch", '{"seed": -1}']) == 2


def test_world_cancel_marks_a_created_or_paused_build_cancelled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    assert main(["world", "cancel", str(world_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    assert _world(world_dir).job.status == "cancelled"
    assert _world(world_dir).job.finished_at is not None
    # A cancelled world is still a world: it can be built later.
    assert main(["world", "run", str(world_dir)]) == 0
    capsys.readouterr()
    assert _world(world_dir).job.status == "done"
    # Cancelling a finished world is a no-op.
    assert main(["world", "cancel", str(world_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "done"
    assert main(["world", "cancel", str(tmp_path / "nope")]) == 2


# --- structured data as part of the world build (docs/DATA_PRODUCTS.md) ------------


def _write_config_with_data(path: Path, scenarios: list[str]) -> Path:
    listed = ", ".join(f'"{s}"' for s in scenarios)
    path.write_text(
        "seed = 7\n"
        '[company]\nname = "Acme"\nvertical = "software"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-01-18\n"
        f"[data]\nscenarios = [{listed}]\nscale_factor = 0.05\niterations = 1\n"
        "rows_per_partition = 50000\n",
        encoding="utf-8",
    )
    return path


def test_create_builds_documents_and_structured_data_in_one_world(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _write_config_with_data(tmp_path / "cfg.toml", ["support_ops"])
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root), "--no-run"]) == 0
    world_dir = Path(json.loads(capsys.readouterr().out)["world_dir"])
    assert main(["world", "run", str(world_dir)]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    phases = [e["data"]["phase"] for e in events if e["kind"] == "phase"]
    assert phases.index("assemble") < phases.index("data") < phases.index("evals")
    assert [e["data"]["scenario"] for e in events if e["kind"] == "data_scenario"] == [
        "support_ops"
    ]
    assert any(e["kind"] == "data_done" and e["data"]["rows"] > 0 for e in events)
    assert not [e for e in events if e["kind"] == "warning" and "data" in e["data"]["message"]]

    world = _world(world_dir)
    assert world.job.status == "done"
    assert (world_dir / "kg" / "nodes.jsonl").is_file()  # the documents half
    assert (world_dir / "data" / "support_ops" / "spec.json").is_file()  # the data half
    assert set(world.data) == {"support_ops"}
    assert world.config["data"]["scenarios"] == ["support_ops"]
    assert [p.name for p in root.iterdir()] == [world_dir.name]  # still one folder


def test_the_data_plan_does_not_change_the_world_id(tmp_path: Path) -> None:
    from enterprise_sim.assembly import compute_world_id
    from enterprise_sim.core.config import load_config

    plain = load_config(_write_config(tmp_path / "a.toml"))
    with_data = load_config(_write_config_with_data(tmp_path / "b.toml", ["support_ops"]))
    plain = plain.model_copy(update={"simulation": with_data.simulation})
    assert compute_world_id(plain) == compute_world_id(with_data)


def test_extend_add_data_attaches_structured_data_to_an_existing_world(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _mint(tmp_path, capsys)
    assert main(["world", "run", str(world_dir)]) == 0
    capsys.readouterr()
    before = _world(world_dir)

    assert main(["world", "extend", str(world_dir), "--add-data", "support_ops", "--no-run"]) == 0
    changes = json.loads(capsys.readouterr().out)["changes"]
    assert changes["added_data"] == ["support_ops"]
    adopted = _world(world_dir)
    assert adopted.world_id == before.world_id
    assert adopted.config_digest == before.config_digest  # data never changes the identity
    assert adopted.config["data"]["scenarios"] == ["support_ops"]

    # Keep the data tiny for the test.
    patch = json.dumps(
        {"data": {"scale_factor": 0.05, "iterations": 1, "rows_per_partition": 50000}}
    )
    assert main(["world", "configure", str(world_dir), "--patch", patch]) == 0
    capsys.readouterr()
    assert main(["world", "run", str(world_dir)]) == 0
    capsys.readouterr()
    built = _world(world_dir)
    assert "support_ops" in built.data
    assert built.job.artifacts_cached == built.job.artifacts_total  # the corpus was all cache hits

    # Adding a scenario that is already there is not an extension.
    assert main(["world", "extend", str(world_dir), "--add-data", "support_ops"]) == 2
