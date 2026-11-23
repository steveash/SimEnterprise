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


@pytest.fixture(scope="module")
def payables(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("ap") / "a", "docs-only-professional.toml", "ap")


def test_payables_are_dated_in_order_and_scoped_by_entity(payables: Path) -> None:
    tables = payables / "data" / "ap" / "tables"
    pos = {r["po_id"]: r for r in _rows(tables / "dim_po")}
    receipts = {r["po_id"]: r for r in _rows(tables / "fact_po_receipt")}
    invoices = _rows(tables / "fact_invoice")
    truth = _truth(payables, "ap")
    assert {r["entity"] for r in pos.values()} == set(truth["entities"])
    for po_id, receipt in receipts.items():
        assert pos[po_id]["ordered_on"] <= receipt["received_on"]
        assert receipt["entity"] == pos[po_id]["entity"]
    for invoice in invoices:
        assert receipts[invoice["po_id"]]["received_on"] <= invoice["received_on"]
        assert (
            round(invoice["amount"] * 100) % 10 != 0
            or invoice["invoice_id"] in truth["traps"]["partial"]["invoices"]
        )
    wrong = truth["traps"]["wrong_entity"]
    (booked,) = [i for i in invoices if i["invoice_id"] == wrong["invoice_id"]]
    assert booked["entity"] == wrong["invoice_entity"] != pos[wrong["po_id"]]["entity"]


def test_month_end_accruals_answer_from_the_tables_and_keep_the_traps(payables: Path) -> None:
    truth = _truth(payables, "ap")
    questions = json.loads((payables / "data" / "ap" / "questions.json").read_text("utf-8"))
    by_id = {q["id"]: q for q in questions["questions"]}
    for month_end, expected in truth["accruals_by_month_end"].items():
        answer = by_id[f"ap.accruals_{month_end}"]["answer"]
        assert [(a["entity"], a["po_id"], a["accrual"]) for a in answer] == [
            (e["entity"], e["po_id"], pytest.approx(e["accrual"])) for e in expected
        ]
    first = truth["accruals_by_month_end"][str(truth["traps"]["cross_month"]["month_end"])]
    open_pos = {a["po_id"] for a in first}
    for trap in ("cross_month", "partial", "wrong_entity"):
        assert truth["traps"][trap]["po_id"] in open_pos, trap  # all three open at that month end


@pytest.fixture(scope="module")
def month_end_payables(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(
        tmp_path_factory.mktemp("apme") / "a", "docs-only-professional.toml", "ap_month_end"
    )


def test_every_month_end_has_every_trap_for_both_entities(month_end_payables: Path) -> None:
    from enterprise_sim.data_products.scripted_scenarios.ap_month_end import KINDS

    truth = _truth(month_end_payables, "ap_month_end")
    tables = month_end_payables / "data" / "ap_month_end" / "tables"
    pos = {r["po_id"]: r for r in _rows(tables / "dim_po")}
    receipts = {r["po_id"]: r for r in _rows(tables / "fact_po_receipt")}
    invoices = {r["invoice_id"]: r for r in _rows(tables / "fact_invoice")}
    month_ends = list(truth["accruals_by_month_end"])
    assert len(month_ends) >= 4
    planted = {(str(t["month_end"]), t["entity"], t["kind"]) for t in truth["traps"]}
    assert planted == {(me, e, k) for me in month_ends for e in truth["entities"] for k in KINDS}
    for receipt in receipts.values():
        assert pos[receipt["po_id"]]["ordered_on"] <= receipt["received_on"]
    for invoice in invoices.values():
        assert receipts[invoice["po_id"]]["received_on"] <= invoice["received_on"]
    for trap in truth["traps"]:
        me, entity, po_id = str(trap["month_end"]), trap["entity"], trap["po_id"]
        accrued = {
            a["po_id"]: a["accrual"]
            for a in truth["accruals_by_month_end"][me]
            if a["entity"] == entity
        }
        over = {o["po_id"]: o["excess"] for o in truth["over_invoiced_by_month_end"][me]}
        booked = [invoices[i] for i in trap["invoices"]]
        if trap["kind"] == "cross_month":
            assert accrued[po_id] == pytest.approx(receipts[po_id]["amount"])
            assert all(str(i["received_on"]) > me for i in booked)
        elif trap["kind"] == "partial":
            assert accrued[po_id] == pytest.approx(trap["accrued"])
        elif trap["kind"] == "over_invoiced":
            assert po_id not in accrued and trap["excess"] > 0
            assert over[po_id] == pytest.approx(trap["excess"])
        else:
            assert accrued[po_id] == pytest.approx(receipts[po_id]["amount"])
            assert [i["entity"] for i in booked] == [trap["invoice_entity"]]
            assert trap["invoice_entity"] != entity and str(booked[0]["received_on"]) <= me


def test_month_end_questions_answer_the_gold(month_end_payables: Path) -> None:
    truth = _truth(month_end_payables, "ap_month_end")
    questions = json.loads(
        (month_end_payables / "data" / "ap_month_end" / "questions.json").read_text("utf-8")
    )
    by_id = {q["id"]: q["answer"] for q in questions["questions"]}
    for me, expected in truth["accruals_by_month_end"].items():
        assert [(a["entity"], a["po_id"], a["accrual"]) for a in by_id[f"ap.accruals_{me}"]] == [
            (e["entity"], e["po_id"], pytest.approx(e["accrual"])) for e in expected
        ]
    for me, expected in truth["over_invoiced_by_month_end"].items():
        answer = by_id[f"ap.over_invoiced_{me}"]
        assert [(a["entity"], a["po_id"], a["excess"]) for a in answer] == [
            (e["entity"], e["po_id"], pytest.approx(e["excess"])) for e in expected
        ]
    wrong = sorted(i for t in truth["traps"] if t["kind"] == "wrong_entity" for i in t["invoices"])
    assert [a["invoice_id"] for a in by_id["ap.wrong_entity"]] == wrong


def test_the_month_end_book_keeps_the_ap_base_rows(
    payables: Path, month_end_payables: Path
) -> None:
    def base(world: Path, scenario: str, moved: set[str]) -> list[dict[str, Any]]:
        rows = _rows(world / "data" / scenario / "tables" / "dim_po")
        return [r for r in rows if r["po_id"] not in moved]

    ap_traps = {t["po_id"] for t in _truth(payables, "ap")["traps"].values()}
    planted = {t["po_id"] for t in _truth(month_end_payables, "ap_month_end")["traps"]}
    kept = base(payables, "ap", ap_traps)
    assert kept and base(month_end_payables, "ap_month_end", planted | ap_traps) == kept
