"""``enterprise-sim job ...`` CLI JSON round-trips (``docs/EXPLORER_RUNS.md`` §3.4, §6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from enterprise_sim.cli import main


def _write_config(path: Path, *, output_dir: Path, seed: int = 7) -> Path:
    path.write_text(
        f"seed = {seed}\n"
        f'output_dir = "{output_dir.as_posix()}"\n'
        '[company]\nname = "Acme"\nvertical = "software"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-01\nperiod_end = 2026-01-31\n",
        encoding="utf-8",
    )
    return path


def test_job_catalog_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["job", "catalog"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert {a["name"] for a in payload["archetypes"]} >= {"engineering", "retail"}
    for archetype in payload["archetypes"]:
        assert {"name", "charter", "typical_goals", "team_shapes", "playbooks"} <= set(archetype)
    for playbook in payload["playbooks"]:
        assert {"name", "vertical", "deliverables"} <= set(playbook)
    assert isinstance(payload["producers"], list)
    assert set(payload["backends"]) == {"anthropic_api", "bedrock", "claude_cli", "fake"}
    assert "claude-sonnet-4-6" in payload["models"]
    for pricing in payload["models"].values():
        assert {"input_per_mtok", "cached_input_per_mtok", "output_per_mtok"} <= set(pricing)
    assert set(payload["company_sizes"]) == {"startup", "small", "medium", "large", "enterprise"}
    assert "properties" in payload["schema"]  # a RunConfig JSON schema.


def test_job_estimate_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _write_config(tmp_path / "cfg.toml", output_dir=tmp_path / "out")
    assert main(["job", "estimate", "-c", str(config_path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["num_artifacts"] > 0
    assert payload["estimated_cost_usd"] >= 0
    assert payload["model"]
    assert payload["world"]["nodes"] > 0
    assert payload["world"]["edges"] > 0
    assert payload["world"]["departments"] == 1
    assert payload["world"]["scenarios"] == 1
    assert payload["events"] > 0


def test_job_create_run_status_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _write_config(tmp_path / "cfg.toml", output_dir=tmp_path / "out")
    jobs_root = tmp_path / "jobs"

    assert main(["job", "create", "--jobs-root", str(jobs_root), "-c", str(config_path)]) == 0
    created = json.loads(capsys.readouterr().out)
    job_id, job_dir = created["job_id"], Path(created["job_dir"])
    assert job_dir.is_dir()
    assert (job_dir / "job.json").is_file()
    assert (job_dir / "config.json").is_file()

    job_payload = json.loads((job_dir / "job.json").read_text())
    assert job_payload["kind"] == "new"
    assert job_payload["live"] is False
    config_payload = json.loads((job_dir / "config.json").read_text())
    assert config_payload["scale"]["cache_dir"] == str(job_dir / "llm-cache")

    assert main(["job", "status", str(job_dir)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "created"

    assert main(["job", "run", str(job_dir)]) == 0
    run_output = capsys.readouterr()
    progress_lines = [json.loads(line) for line in run_output.out.splitlines() if line]
    assert progress_lines  # streamed progress JSONL on stdout.
    assert progress_lines[-1]["kind"] in {"done", "phase"}
    assert "enterprise-sim job run" in run_output.err  # human note on stderr.

    progress_file = job_dir / "progress.jsonl"
    assert progress_file.is_file()
    file_lines = [json.loads(line) for line in progress_file.read_text().splitlines() if line]
    assert file_lines == progress_lines

    assert main(["job", "status", str(job_dir)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "done"
    assert status["run_dir"] is not None
    assert Path(status["run_dir"]).is_dir()

    assert main(["job", "list", "--jobs-root", str(jobs_root)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert [j["job_id"] for j in listing["jobs"]] == [job_id]
    assert listing["jobs"][0]["job"]["kind"] == "new"
    assert listing["jobs"][0]["state"]["status"] == "done"


def test_job_pause_writes_control_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _write_config(tmp_path / "cfg.toml", output_dir=tmp_path / "out")
    jobs_root = tmp_path / "jobs"
    assert main(["job", "create", "--jobs-root", str(jobs_root), "-c", str(config_path)]) == 0
    job_dir = Path(json.loads(capsys.readouterr().out)["job_dir"])

    assert main(["job", "pause", str(job_dir)]) == 0
    capsys.readouterr()
    control = json.loads((job_dir / "control.json").read_text())
    assert control == {"pause": True}


def test_job_list_on_an_empty_jobs_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["job", "list", "--jobs-root", str(tmp_path / "nope")]) == 0
    assert json.loads(capsys.readouterr().out) == {"jobs": []}


def test_job_status_of_an_unknown_job(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["job", "status", str(tmp_path / "no-such-job")]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload


def test_job_create_extend_writes_lineage_and_grows_the_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path / "cfg.toml", output_dir=tmp_path / "out")
    jobs_root = tmp_path / "jobs"

    assert main(["job", "create", "--jobs-root", str(jobs_root), "-c", str(config_path)]) == 0
    parent_job_dir = Path(json.loads(capsys.readouterr().out)["job_dir"])
    assert main(["job", "run", str(parent_job_dir)]) == 0
    capsys.readouterr()
    parent_run_dir = json.loads((parent_job_dir / "state.json").read_text())["run_dir"]

    assert (
        main(
            [
                "job",
                "create",
                "--jobs-root",
                str(jobs_root),
                "-c",
                str(config_path),
                "--extend",
                parent_run_dir,
                "--period-end",
                "2026-03-31",
                "--add-project",
                json.dumps({"name": "Extra Widget"}),
            ]
        )
        == 0
    )
    child_job_dir = Path(json.loads(capsys.readouterr().out)["job_dir"])
    child_job_payload = json.loads((child_job_dir / "job.json").read_text())
    assert child_job_payload["kind"] == "extend"
    assert child_job_payload["parent_run_dir"] == parent_run_dir
    assert child_job_payload["changes"]["added_projects"] == ["Extra Widget"]
    assert child_job_payload["config"]["simulation"]["period_end"] == "2026-03-31"
    assert child_job_payload["config"]["projects"][-1]["name"] == "Extra Widget"

    assert main(["job", "run", str(child_job_dir)]) == 0
    capsys.readouterr()
    child_state = json.loads((child_job_dir / "state.json").read_text())
    assert child_state["status"] == "done"
    lineage = json.loads((Path(child_state["run_dir"]) / "lineage.json").read_text())
    assert lineage["parent_run_dir"] == parent_run_dir


def test_job_create_extend_rejects_an_earlier_period_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path / "cfg.toml", output_dir=tmp_path / "out")
    jobs_root = tmp_path / "jobs"
    assert main(["job", "create", "--jobs-root", str(jobs_root), "-c", str(config_path)]) == 0
    parent_job_dir = Path(json.loads(capsys.readouterr().out)["job_dir"])
    assert main(["job", "run", str(parent_job_dir)]) == 0
    capsys.readouterr()
    parent_run_dir = json.loads((parent_job_dir / "state.json").read_text())["run_dir"]

    exit_code = main(
        [
            "job",
            "create",
            "--jobs-root",
            str(jobs_root),
            "-c",
            str(config_path),
            "--extend",
            parent_run_dir,
            "--period-end",
            "2025-01-01",
        ]
    )
    assert exit_code == 2


def test_job_run_of_a_missing_job_dir(tmp_path: Path) -> None:
    assert main(["job", "run", str(tmp_path / "no-such-job")]) == 2


def test_job_estimate_reports_a_cost_ceiling_breach(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "cfg.toml"
    config_path.write_text(
        "seed = 7\n"
        f'output_dir = "{(tmp_path / "out").as_posix()}"\n'
        '[company]\nname = "Acme"\nvertical = "software"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-01\nperiod_end = 2026-01-31\n"
        "[scale]\ncost_ceiling_usd = 0.0\n",
        encoding="utf-8",
    )
    assert main(["job", "estimate", "-c", str(config_path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "CostCeilingExceeded"
