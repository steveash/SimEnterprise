"""``pipeline_runs``: a pipeline run history with opaque failure codes, plus ops logs.

For the test set ops-failure-code-incidental. A few
pipelines, owned by the world's real data-engineering teams, run on schedules over the
window. Most failures are ordinary: a code, an honest message, a rerun that succeeds.
Two incidents are not:

* **Credential incident** (a third of the way in): one pipeline starts failing with the
  opaque code ``E-4127``, whose message blames an upstream schema mismatch. Every rerun
  fails the same way until a credential rotation for the pipeline's warehouse service
  account. Only the vault audit log records that rotation. The message misleads; the
  log holds the cause.
* **Backfill incident** (two thirds in): another pipeline fails with ``E-2201``, whose
  message blames a lock timeout. Reruns fail until a partition backfill, which only
  the backfill log records, repairs the missing partition.

Tables:

* ``dim_pipeline``;
* ``fact_pipeline_run``: run_id, pipeline_id, started_at, finished_at, status, code,
  message, attempt, rerun_of;
* ``fact_credential_event``: the vault audit log;
* ``fact_backfill_log``.

The gold ``truth.json`` states both incidents: code, pipeline, the misleading message,
the true cause, and the fixing event id and time.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from typing import Any

from enterprise_sim.core.world import World
from enterprise_sim.data_products.scripted import (
    SCRIPTED_SCENARIOS,
    ScriptedData,
    ScriptedQuestion,
    ScriptedTable,
)

_ORDINARY = (
    ("E-1009", "upstream file arrived late; retrying"),
    ("E-3302", "worker preempted; task rescheduled"),
    ("E-5120", "row count below threshold for partition"),
)
_DATA_DEPARTMENTS = ("data_engineering", "data_analytics", "engineering")


def _teams(world: World) -> list[tuple[str, list[str]]]:
    """(team name, member names) for the data teams, falling back to any team."""
    members: dict[str, list[str]] = {}
    for edge in world.edges_by_type("member_of"):
        team = world.get_node(edge.dst)
        person = world.get_node(edge.src)
        if team is None or person is None or team.type != "Team":
            continue
        members.setdefault(team.id, []).append(str(person.props.get("name")))
    teams = [
        n for n in sorted(world.nodes_by_type("Team"), key=lambda n: n.id) if members.get(n.id)
    ]
    preferred = [t for t in teams if t.props.get("department") in _DATA_DEPARTMENTS]
    chosen = preferred or teams
    return [(str(t.props.get("name", t.id)), sorted(members[t.id])) for t in chosen]


class PipelineRuns:
    name = "pipeline_runs"
    title = "Pipeline Run History and Ops Logs"
    brief = (
        "A pipeline run history (timestamps, status, opaque failure codes and their "
        "messages, reruns) with a vault audit log of credential events and a partition "
        "backfill log; two incidents whose messages mislead and whose fix only a log records."
    )

    def build_data(self, *, world: World, seed: int, start: date, end: date) -> ScriptedData:
        rng = random.Random(f"{seed}\x00pipeline_runs")
        teams = _teams(world) or [("Data Platform", ["the on-call engineer"])]
        t0 = datetime.combine(start, time(0, 0))
        t1 = datetime.combine(end, time(23, 0))
        span = t1 - t0
        names = (
            "orders_ingest",
            "events_rollup",
            "billing_export",
            "crm_sync",
            "inventory_load",
            "ledger_close",
        )
        pipelines = []
        for index, pname in enumerate(names):
            team, people = teams[index % len(teams)]
            pipelines.append(
                {
                    "pipeline_id": f"pl-{pname.replace('_', '-')}",
                    "name": pname,
                    "owner_team": team,
                    "schedule": "hourly" if index % 2 == 0 else "daily",
                    "_people": people,
                }
            )
        cred_pipe, back_pipe = pipelines[0], pipelines[3]
        cred_start = t0 + span / 3
        cred_fix = cred_start + timedelta(hours=rng.randint(30, 60))
        back_start = t0 + span * 2 / 3
        back_fix = back_start + timedelta(hours=rng.randint(50, 90))  # a daily pipeline: 2-3 days
        rotation_id = f"vault-{rng.randint(100000, 999999)}"
        backfill_id = f"bf-{rng.randint(1000, 9999)}"
        missing_partition = (back_start - timedelta(days=1)).date()

        runs: list[dict[str, Any]] = []
        counter = 0

        def run(
            p: dict[str, Any], at: datetime, status: str, code: str, msg: str, attempt: int, of: str
        ) -> str:
            nonlocal counter
            counter += 1
            rid = f"run-{counter:06d}"
            minutes = rng.randint(3, 40) if status == "success" else rng.randint(1, 6)
            runs.append(
                {
                    "run_id": rid,
                    "pipeline_id": p["pipeline_id"],
                    "started_at": at,
                    "finished_at": at + timedelta(minutes=minutes),
                    "status": status,
                    "code": code,
                    "message": msg,
                    "attempt": attempt,
                    "rerun_of": of,
                }
            )
            return rid

        for p in pipelines:
            step = timedelta(hours=1 if p["schedule"] == "hourly" else 24)
            at = t0 + timedelta(minutes=rng.randint(0, 50))
            while at <= t1:
                if p is cred_pipe and cred_start <= at < cred_fix:
                    msg = "upstream schema mismatch: column set changed for source table"
                    first = run(p, at, "failed", "E-4127", msg, 1, "")
                    for attempt in (2, 3):
                        retry = at + timedelta(minutes=15 * (attempt - 1))
                        if retry < cred_fix:
                            run(p, retry, "failed", "E-4127", msg, attempt, first)
                elif p is back_pipe and back_start <= at < back_fix:
                    msg = "timeout waiting for table lock on target"
                    first = run(p, at, "failed", "E-2201", msg, 1, "")
                    retry = at + timedelta(minutes=30)
                    if retry < back_fix:
                        run(p, retry, "failed", "E-2201", msg, 2, first)
                elif rng.random() < 0.03:
                    code, msg = rng.choice(_ORDINARY)
                    first = run(p, at, "failed", code, msg, 1, "")
                    run(p, at + timedelta(minutes=10), "success", "", "", 2, first)
                else:
                    run(p, at, "success", "", "", 1, "")
                at += step

        events: list[dict[str, Any]] = []
        for index, p in enumerate(pipelines):
            path = f"svc/{p['name']}/warehouse"
            reads = t0 + timedelta(hours=6 + index)
            while reads <= t1:
                events.append(
                    {
                        "event_id": f"vault-{len(events) + 1:06d}",
                        "at": reads,
                        "secret_path": path,
                        "action": "read",
                        "actor": f"svc-{p['name']}",
                    }
                )
                reads += timedelta(days=7)
        events.append(
            {
                "event_id": f"vault-{len(events) + 1:06d}",
                "at": cred_start - timedelta(minutes=5),
                "secret_path": f"svc/{cred_pipe['name']}/warehouse",
                "action": "expire",
                "actor": "vault-ttl",
            }
        )
        fixer = cred_pipe["_people"][0]
        events.append(
            {
                "event_id": rotation_id,
                "at": cred_fix - timedelta(minutes=10),
                "secret_path": f"svc/{cred_pipe['name']}/warehouse",
                "action": "rotate",
                "actor": fixer,
            }
        )
        events.sort(key=lambda e: (e["at"], e["event_id"]))

        backfills = [
            {
                "backfill_id": f"bf-{rng.randint(1000, 9999)}-{n}",
                "pipeline_id": pipelines[(n + 1) % len(pipelines)]["pipeline_id"],
                "partition": (t0 + span * (n + 1) / 6).date(),
                "requested_at": t0 + span * (n + 1) / 6,
                "completed_at": t0 + span * (n + 1) / 6 + timedelta(hours=2),
                "requested_by": pipelines[(n + 1) % len(pipelines)]["_people"][0],
                "status": "completed",
            }
            for n in range(2)
        ]
        backfills.append(
            {
                "backfill_id": backfill_id,
                "pipeline_id": back_pipe["pipeline_id"],
                "partition": missing_partition,
                "requested_at": back_fix - timedelta(hours=3),
                "completed_at": back_fix - timedelta(minutes=20),
                "requested_by": back_pipe["_people"][0],
                "status": "completed",
            }
        )
        backfills.sort(key=lambda b: (b["requested_at"], b["backfill_id"]))

        for p in pipelines:
            p.pop("_people")
        truth = {
            "incidents": [
                {
                    "code": "E-4127",
                    "pipeline_id": cred_pipe["pipeline_id"],
                    "misleading_message": "upstream schema mismatch",
                    "cause": "the pipeline's warehouse service credential expired",
                    "first_failure": cred_start,
                    "fixed_by": {
                        "log": "fact_credential_event",
                        "event_id": rotation_id,
                        "action": "rotate",
                    },
                    "fixed_at": cred_fix,
                },
                {
                    "code": "E-2201",
                    "pipeline_id": back_pipe["pipeline_id"],
                    "misleading_message": "timeout waiting for table lock",
                    "cause": f"partition {missing_partition.isoformat()} was missing upstream",
                    "first_failure": back_start,
                    "fixed_by": {"log": "fact_backfill_log", "event_id": backfill_id},
                    "fixed_at": back_fix,
                },
            ]
        }
        tables = (
            ScriptedTable(
                "dim_pipeline",
                "Pipelines, their owner team (a real team) and schedule.",
                (
                    ("pipeline_id", "string"),
                    ("name", "string"),
                    ("owner_team", "string"),
                    ("schedule", "string"),
                ),
                tuple(pipelines),
                "pipeline_id",
            ),
            ScriptedTable(
                "fact_pipeline_run",
                "Every run: timestamps, status, failure code and message, attempt, "
                "and the run it reruns.",
                (
                    ("run_id", "string"),
                    ("pipeline_id", "string"),
                    ("started_at", "timestamp"),
                    ("finished_at", "timestamp"),
                    ("status", "string"),
                    ("code", "string"),
                    ("message", "string"),
                    ("attempt", "int64"),
                    ("rerun_of", "string"),
                ),
                tuple(runs),
                "run_id",
            ),
            ScriptedTable(
                "fact_credential_event",
                "Vault audit log: reads, expiries and rotations of service credentials.",
                (
                    ("event_id", "string"),
                    ("at", "timestamp"),
                    ("secret_path", "string"),
                    ("action", "string"),
                    ("actor", "string"),
                ),
                tuple(events),
                "event_id",
            ),
            ScriptedTable(
                "fact_backfill_log",
                "Partition backfills: what was requested, by whom, and when it completed.",
                (
                    ("backfill_id", "string"),
                    ("pipeline_id", "string"),
                    ("partition", "date"),
                    ("requested_at", "timestamp"),
                    ("completed_at", "timestamp"),
                    ("requested_by", "string"),
                    ("status", "string"),
                ),
                tuple(backfills),
                "backfill_id",
            ),
        )
        views = (
            (
                "failures_by_code",
                "SELECT code, count(*) AS failed_runs, count(DISTINCT pipeline_id) AS pipelines, "
                "min(started_at) AS first_seen, max(started_at) AS last_seen "
                "FROM fact_pipeline_run WHERE status = 'failed' GROUP BY code",
            ),
        )
        questions = (
            ScriptedQuestion(
                "pr.codes",
                "reliability",
                "Which failure codes occurred, how often, and when were they first and last seen?",
                "SELECT code, failed_runs, first_seen, last_seen FROM failures_by_code "
                "ORDER BY code",
            ),
            ScriptedQuestion(
                "pr.e4127_fix",
                "incident",
                "What event immediately preceded the first successful run after the "
                "E-4127 failures?",
                f"""
                SELECT e.event_id, e.at, e.action, e.secret_path FROM fact_credential_event e
                WHERE e.secret_path = 'svc/{cred_pipe["name"]}/warehouse' AND e.action = 'rotate'
                ORDER BY e.at DESC LIMIT 1
                """,
            ),
            ScriptedQuestion(
                "pr.e2201_fix",
                "incident",
                "Which backfill fixed the E-2201 failures, and for which partition?",
                f"""
                SELECT backfill_id, partition, completed_at FROM fact_backfill_log
                WHERE pipeline_id = '{back_pipe["pipeline_id"]}' ORDER BY completed_at DESC LIMIT 1
                """,
            ),
        )
        return ScriptedData(tables=tables, views=views, questions=questions, truth=truth)


SCRIPTED_SCENARIOS.register(PipelineRuns())
