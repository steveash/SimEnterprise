"""The site-reliability department and the ``sre_ops`` scenario tell the ops stories."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "worlds" / "ops-sre.toml"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("sre")
    text = re.sub(r"(?m)^worlds_root = .*$", "", _EXAMPLE.read_text(encoding="utf-8"))
    (root / "cfg.toml").write_text(text, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (built,) = list((root / "w").iterdir())
    return built


def _rows(world: Path, table: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    directory = world / "data" / "sre_ops" / "tables" / table
    return [r for p in sorted(directory.glob("*.parquet")) for r in pq.read_table(p).to_pylist()]


def _truth(world: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((world / "data" / "sre_ops" / "truth.json").read_text())
    return data


def test_every_service_has_a_rota_trap_with_a_distinct_misleading_engineer(world: Path) -> None:
    truth = _truth(world)
    rota = {r["shift_id"]: r for r in _rows(world, "oncall_rota")}
    changes = _rows(world, "rota_changes")
    assert len(truth["services"]) == 20
    for sid, gold in truth["services"].items():
        m = gold["maintenance"]
        shift = rota[m["shift_id"]]
        assert shift["service_id"] == sid and shift["primary"] == m["published_primary"]
        assert m["escalation"] == shift["secondary"]
        # the true primary follows the agreed changes, in order; the cancelled one is ignored
        primary = shift["primary"]
        for change in sorted(
            (c for c in changes if c["shift_id"] == m["shift_id"] and c["status"] == "agreed"),
            key=lambda c: (c["requested_on"], c["change_id"]),
        ):
            assert change["from_person"] == primary
            primary = change["to_person"]
        assert primary == m["primary"] != m["published_primary"]
        assert m["misleading_primary"] not in (m["primary"], m["published_primary"])
        (cancelled,) = [c for c in changes if c["change_id"] == m["cancelled"]]
        assert (
            cancelled["status"] == "cancelled" and cancelled["to_person"] == m["misleading_primary"]
        )


def test_the_review_changes_what_is_open_and_overdue(world: Path) -> None:
    truth = _truth(world)
    actions = _rows(world, "postmortem_actions")
    reviews = {r["action_id"]: r for r in _rows(world, "action_reviews")}
    review_day = date.fromisoformat(truth["review_day"])
    for sid, gold in truth["services"].items():
        mine = [a for a in actions if a["service_id"] == sid]
        open_after, overdue_after = [], []
        for a in mine:
            decision = reviews.get(a["action_id"], {})
            is_open = a["status"] == "open"
            due = a["due_on"]
            if decision.get("decision") == "merged":
                is_open = False
            elif decision.get("decision") == "reopened":
                is_open = True
            elif decision.get("decision") == "reassigned":
                due = decision["new_due_on"]
            if is_open:
                open_after.append(a["action_id"])
                if due < review_day:
                    overdue_after.append(a["action_id"])
        assert gold["actions_after_review"] == {
            "open": sorted(open_after),
            "overdue": sorted(overdue_after),
        }
        assert gold["actions_after_review"] != gold["actions_tracker"], sid


def test_questions_answer_the_gold(world: Path) -> None:
    truth = _truth(world)
    payload = json.loads((world / "data" / "sre_ops" / "questions.json").read_text())
    answers = {q["id"]: q["answer"] for q in payload["questions"]}
    published = {r["service_id"]: r["primary"] for r in answers["sre.published_primary"]}
    tracked = {r["service_id"]: r["open_items"] for r in answers["sre.open_actions_tracker"]}
    for sid, gold in truth["services"].items():
        assert published[sid] == gold["maintenance"]["published_primary"]
        assert tracked[sid] == len(gold["actions_tracker"]["open"])
    deferred = sorted(r["change_id"] for r in answers["sre.deferred_changes"])
    assert deferred == sorted(
        c for g in truth["services"].values() for c in g["freeze"]["deferred"]
    )
    kinds = {
        k
        for g in truth["services"].values()
        for k in ("standing_exception", "revoked_exception")
        if g["freeze"][k]
    }
    assert kinds == {"standing_exception", "revoked_exception"}


def test_cells_hold_no_comma_and_ids_are_stable(world: Path) -> None:
    keys = {
        "services": "service_id",
        "oncall_rota": "shift_id",
        "rota_changes": "change_id",
        "postmortem_actions": "action_id",
        "action_reviews": "review_id",
        "changes": "change_id",
        "freeze_exceptions": "exception_id",
        "incidents": "incident_id",
        "deploys": "deploy_id",
    }
    for table, key in keys.items():
        rows = _rows(world, table)
        assert rows and len({r[key] for r in rows}) == len(rows), table
        for row in rows:
            assert all("," not in v for v in row.values() if isinstance(v, str)), (table, row)


def test_the_department_carries_sre_facts(world: Path) -> None:
    lines = (world / "kg" / "nodes.jsonl").read_text(encoding="utf-8").splitlines()
    nodes = [json.loads(line) for line in lines]
    projects = {
        n["id"]
        for n in nodes
        if n["type"] == "Project" and n["props"].get("department") == "site_reliability"
    }
    attributes = {
        n["props"]["attribute"]
        for n in nodes
        if n["type"] == "Fact" and n["props"].get("subject") in projects
    }
    assert projects and {"oncall_primary", "escalation_owner"} & attributes
    assert attributes <= {
        "oncall_primary",
        "escalation_owner",
        "deploy_window",
        "change_freeze_end",
        "failover_region",
        "alert_threshold",
        "slo_target",
        "log_retention",
        "target_date",
        "budget",
        "owner",
        "status",
        "priority",
        "review_cadence",
    }
