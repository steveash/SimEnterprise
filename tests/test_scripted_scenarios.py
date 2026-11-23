"""Scripted data scenarios: ``pipeline_runs`` and ``service_latency`` tell their stories."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


def _build(root: Path, example: str, scenario: str, extra: str = "") -> Path:
    root.mkdir(parents=True)
    text = (_EXAMPLES / example).read_text(encoding="utf-8")
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    text = re.sub(r"(?ms)^\[data\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?ms)^\[data\.messiness\]\n(?:(?!^\[).)*", "", text)
    text += f'\n[data]\nscenarios = ["{scenario}"]\nscale_factor = 1.0\niterations = 1\n' + extra
    (root / "cfg.toml").write_text(text, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    return world


def _rows(directory: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    return [r for p in sorted(directory.glob("*.parquet")) for r in pq.read_table(p).to_pylist()]


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("pr") / "a", "coding-team-clean.toml", "pipeline_runs")


@pytest.fixture(scope="module")
def latency(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("sl") / "a", "coding-team-clean.toml", "service_latency")


def _truth(world: Path, scenario: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((world / "data" / scenario / "truth.json").read_text("utf-8"))
    return data


def _at(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _parquet(world: Path) -> dict[str, bytes]:
    return {
        p.relative_to(world).as_posix(): p.read_bytes() for p in (world / "data").rglob("*.parquet")
    }


def test_an_opaque_code_fails_on_every_rerun_until_the_logged_fix(runs: Path) -> None:
    tables = runs / "data" / "pipeline_runs" / "tables"
    all_runs = _rows(tables / "fact_pipeline_run")
    logs = {
        "fact_credential_event": {
            r["event_id"]: r for r in _rows(tables / "fact_credential_event")
        },
        "fact_backfill_log": {r["backfill_id"]: r for r in _rows(tables / "fact_backfill_log")},
    }
    for incident in _truth(runs, "pipeline_runs")["incidents"]:
        mine = sorted(
            (r for r in all_runs if r["pipeline_id"] == incident["pipeline_id"]),
            key=lambda r: r["started_at"],
        )
        coded = [r for r in all_runs if r["code"] == incident["code"]]
        assert len(coded) >= 3, incident["code"]
        assert all(r["status"] == "failed" for r in coded)
        assert all(r["pipeline_id"] == incident["pipeline_id"] for r in coded)
        assert all(incident["misleading_message"] in r["message"] for r in coded)
        fixed = _at(incident["fixed_at"])
        assert all(_at(r["started_at"]) < fixed for r in coded)
        after = next(r for r in mine if r["started_at"] >= fixed)
        assert after["status"] == "success"  # the first run after the fix succeeds
        event = logs[incident["fixed_by"]["log"]][incident["fixed_by"]["event_id"]]
        when = _at(event.get("at") or event.get("completed_at"))
        assert _at(incident["first_failure"]) < when <= fixed


def test_reruns_point_at_a_failed_run_of_the_same_pipeline(runs: Path) -> None:
    all_runs = _rows(runs / "data" / "pipeline_runs" / "tables" / "fact_pipeline_run")
    by_id = {r["run_id"]: r for r in all_runs}
    reruns = [r for r in all_runs if r["rerun_of"]]
    assert reruns
    for r in reruns:
        original = by_id[r["rerun_of"]]
        assert original["status"] == "failed" and original["pipeline_id"] == r["pipeline_id"]
        assert r["attempt"] > 1 and r["started_at"] > original["started_at"]


def test_pipelines_are_owned_by_real_teams(runs: Path) -> None:
    nodes = [
        json.loads(line)
        for line in (runs / "kg" / "nodes.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    teams = {n["props"]["name"] for n in nodes if n["type"] == "Team"}
    owners = {
        r["owner_team"] for r in _rows(runs / "data" / "pipeline_runs" / "tables" / "dim_pipeline")
    }
    assert owners <= teams


def test_the_regressing_commit_not_the_decoy_explains_the_latency(latency: Path) -> None:
    tables = latency / "data" / "service_latency" / "tables"
    truth = _truth(latency, "service_latency")
    commits = {c["sha"]: c for c in _rows(tables / "fact_commit")}
    assert commits[truth["regressing_commit"]]["release_id"] == truth["release"]
    assert commits[truth["decoy_commit"]]["release_id"] == truth["release"]  # shipped together
    revert = commits[truth["revert_commit"]]
    assert truth["regressing_commit"] in revert["summary"]
    assert truth["decoy_commit"] not in revert["summary"]
    assert revert["release_id"] == truth["fix_release"]
    series = [r for r in _rows(tables / "fact_latency_hourly") if r["service"] == truth["service"]]
    start, end = _at(truth["deployed_at"]), _at(truth["fixed_at"])
    during = [r["p95_ms"] for r in series if start <= r["hour"] < end]
    before = [r["p95_ms"] for r in series if r["hour"] < start]
    after = [r["p95_ms"] for r in series if r["hour"] >= end]
    assert _mean(during) > 1.3 * _mean(before)
    assert _mean(after) < 1.1 * _mean(before)


def test_questions_answer_from_the_tables(latency: Path) -> None:
    payload = json.loads(
        (latency / "data" / "service_latency" / "questions.json").read_text(encoding="utf-8")
    )
    by_id = {q["id"]: q for q in payload["questions"]}
    truth = _truth(latency, "service_latency")
    assert by_id["sl.regressing_release"]["answer"][0]["release_id"] == truth["release"]
    assert by_id["sl.revert"]["answer"][0]["sha"] == truth["revert_commit"]


def test_scripted_tables_export_with_keys_and_timestamps(runs: Path, tmp_path: Path) -> None:
    manifest = json.loads(
        (export_world(runs, tmp_path / "out").directory / "export.json").read_text("utf-8")
    )
    tables = {t["name"]: t for t in manifest["tables"]}
    run_table = tables["pipeline_runs.fact_pipeline_run"]
    assert run_table["primary_key"] == ["run_id"]
    types = {c["name"]: c["type"] for c in run_table["columns"]}
    assert types["started_at"] == "timestamp" and types["attempt"] == "integer"
    gold = {f["path"]: f["role"] for f in manifest["files"]}
    assert gold["data/pipeline_runs/truth.json"] == "gold"


def test_scripted_scenarios_are_deterministic_and_take_messiness(tmp_path: Path) -> None:
    a = _build(tmp_path / "a", "coding-team-clean.toml", "pipeline_runs")
    b = _build(tmp_path / "b", "coding-team-clean.toml", "pipeline_runs")
    assert _parquet(a) == _parquet(b)
    messy = _build(
        tmp_path / "m",
        "coding-team-clean.toml",
        "pipeline_runs",
        "\n[data.messiness]\nduplicate_rate = 0.05\nlabel_drift_rate = 0.1\n",
    )
    quirks = json.loads((messy / "data" / "pipeline_runs" / "quirks.json").read_text("utf-8"))
    assert quirks["counts"].get("duplicate_row", 0) > 0
