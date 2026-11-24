"""``sre_ops``: an SRE team's operational records, with the traps ops test sets turn on.

Twenty services, each an instance of every story, so a test-set family can draw a
different one per seed. Each table names its rows by a stable id; dates are
``YYYY-MM-DD`` and times UTC; no cell holds a comma. Each deciding fact is stated in
exactly one table row, and the record a careless reader trusts (the published rota,
the tracker) disagrees with it until it is joined with the table that changed it.

* **On-call rota** (``oncall_rota`` + ``rota_changes``): the published rota names a
  primary, a secondary and a backup for each service-week. For one maintenance week
  per service, an agreed swap hands the pager to another engineer; on some services
  that engineer then goes on leave, and the rota rule hands the shift to the backup.
  A second swap was requested and cancelled: its engineer is the misleading answer.
* **Postmortem actions** (``postmortem_actions`` + ``action_reviews``): the tracker
  lists each service's action items as it was last edited. The quarterly review
  merged two duplicates, reassigned one item with a later due day, and reopened one
  the tracker shows closed because its verification failed.
* **Change freeze** (``change_freeze``, ``changes`` + ``freeze_exceptions``): a freeze
  around a launch, extended by a day after it was published. Standard changes inside
  the freeze are deferred; one service's change got an exception that was later
  revoked, another's an exception that stands.
* **Context** (``incidents``, ``deploys``): incidents with their commanders, and
  deploys with outcome and rollback.

The gold ``truth.json`` gives, per service, the true primary of the maintenance week
(with the chain of changes and the misleading engineer), the open and overdue action
items at the review day after the review decisions (and what the tracker alone gives),
and the deferred changes and standing exceptions.
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

SERVICES = 20
_NAMES = (
    "checkout-api",
    "orders-db",
    "payments-gateway",
    "search-index",
    "identity-auth",
    "media-cdn",
    "billing-worker",
    "notifications",
    "inventory-sync",
    "pricing-engine",
    "events-bus",
    "reports-batch",
    "ledger-db",
    "session-cache",
    "partner-api",
    "storefront-web",
    "metrics-pipeline",
    "export-service",
    "fraud-scoring",
    "edge-proxy",
)
_TEAMS = ("platform-infra", "data-platform", "payments-infra", "core-services", "edge-networking")
_ACTIONS = (
    "Add a canary stage before the full rollout",
    "Alert on queue depth before saturation",
    "Document the manual failover steps",
    "Cap retries on the client library",
    "Rotate the shared service credential",
    "Add a dashboard for replica lag",
    "Load-test the new connection pool",
    "Remove the legacy health check",
)


def _people(world: World) -> list[str]:
    """The SRE department's people (or every person, if the world has none)."""
    sre = sorted(
        str(n.props["name"])
        for n in world.nodes_by_type("Person")
        if n.props.get("department") == "site_reliability"
        and n.props.get("name")
        and not n.props.get("left_at")
    )
    return sre or sorted(str(n.props["name"]) for n in world.nodes_by_type("Person"))


def _utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute))


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _open_and_overdue(
    actions: list[dict[str, Any]],
    review_day: date,
    decisions: dict[str, str] | None,
    new_due: date,
) -> dict[str, list[str]]:
    """The open and overdue items at ``review_day``: as tracked, or after ``decisions``."""
    open_ids, overdue = [], []
    for a in actions:
        aid = a["action_id"]
        is_open = a["status"] == "open"
        due: date = a["due_on"]
        if decisions is not None:
            if aid == decisions["merged"]:
                is_open = False
            elif aid == decisions["reopened"]:
                is_open = True
            if aid == decisions["reassigned"]:
                due = new_due
        if is_open:
            open_ids.append(aid)
            if due < review_day:
                overdue.append(aid)
    return {"open": sorted(open_ids), "overdue": sorted(overdue)}


class SreOps:
    name = "sre_ops"
    title = "SRE Operations: Rota, Postmortem Actions, Change Freeze"
    brief = (
        "Twenty services' on-call rota with swaps, leave and a cancelled swap; their "
        "postmortem action items with a quarterly review that merged, reassigned and "
        "reopened items; a change freeze with exceptions granted and revoked; incidents "
        "and deploys for context."
    )

    def build_data(self, *, world: World, seed: int, start: date, end: date) -> ScriptedData:
        rng = random.Random(f"{seed}\x00sre_ops")
        people = _people(world)
        if len(people) < 6:
            raise ValueError("sre_ops needs at least six people")
        weeks = []
        cursor = _monday(start) + (timedelta(days=7) if start.weekday() else timedelta(0))
        while cursor + timedelta(days=6) <= end:
            weeks.append(cursor)
            cursor += timedelta(days=7)
        if len(weeks) < 6:
            raise ValueError("sre_ops needs a window of six weeks or more")
        review_day = end - timedelta(days=7)
        launch = start + (end - start) * 2 // 3
        freeze_start, freeze_end = launch - timedelta(days=2), launch + timedelta(days=2)
        freeze_extended = freeze_end + timedelta(days=1)

        services: list[dict[str, Any]] = []
        rota: list[dict[str, Any]] = []
        rota_changes: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        reviews: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        exceptions: list[dict[str, Any]] = []
        incidents: list[dict[str, Any]] = []
        deploys: list[dict[str, Any]] = []
        truth: dict[str, Any] = {
            "review_day": review_day,
            "freeze": {"starts_on": freeze_start, "ends_on": freeze_extended},
            "services": {},
        }
        n_change = n_ch = n_action = n_review = n_exception = n_incident = n_deploy = 0
        for index in range(SERVICES):
            sid = f"SVC-{index + 1:02d}"
            name = _NAMES[index]
            services.append(
                {
                    "service_id": sid,
                    "name": name,
                    "tier": 1 + index % 3,
                    "owner_team": _TEAMS[index % len(_TEAMS)],
                }
            )
            gold: dict[str, Any] = {"name": name}

            # -- rota: a primary, secondary and backup per week; one maintenance week
            crew = rng.sample(people, 6)
            for w, monday in enumerate(weeks):
                trio = [crew[(w + k) % len(crew)] for k in (0, 2, 4)]
                rota.append(
                    {
                        "shift_id": f"{sid}-W{w + 1:02d}",
                        "service_id": sid,
                        "week_start": monday,
                        "primary": trio[0],
                        "secondary": trio[1],
                        "backup": trio[2],
                    }
                )
            target = rng.randrange(2, len(weeks) - 1)
            shift = rota[-len(weeks) + target]
            others = [
                p for p in crew if p not in (shift["primary"], shift["secondary"], shift["backup"])
            ]
            swap_to, cancelled_to = others[0], others[1]
            requested = shift["week_start"] - timedelta(days=rng.randint(6, 12))
            chain: list[str] = []
            n_change += 1
            rota_changes.append(
                {
                    "change_id": f"RC-{n_change:04d}",
                    "shift_id": shift["shift_id"],
                    "requested_on": requested,
                    "kind": "swap",
                    "from_person": shift["primary"],
                    "to_person": swap_to,
                    "status": "agreed",
                }
            )
            chain.append(f"RC-{n_change:04d}")
            primary = swap_to
            on_leave = index % 2 == 0  # half the services: the swapped-in engineer goes on leave
            if on_leave:
                n_change += 1
                rota_changes.append(
                    {
                        "change_id": f"RC-{n_change:04d}",
                        "shift_id": shift["shift_id"],
                        "requested_on": requested + timedelta(days=3),
                        "kind": "leave",
                        "from_person": swap_to,
                        "to_person": shift["backup"],
                        "status": "agreed",
                    }
                )
                chain.append(f"RC-{n_change:04d}")
                primary = shift["backup"]
            n_change += 1
            rota_changes.append(
                {
                    "change_id": f"RC-{n_change:04d}",
                    "shift_id": shift["shift_id"],
                    "requested_on": requested + timedelta(days=1),
                    "kind": "swap",
                    "from_person": shift["primary"],
                    "to_person": cancelled_to,
                    "status": "cancelled",
                }
            )
            gold["maintenance"] = {
                "shift_id": shift["shift_id"],
                "week_start": shift["week_start"],
                "published_primary": shift["primary"],
                "primary": primary,
                "escalation": shift["secondary"],
                "changes": chain,
                "cancelled": f"RC-{n_change:04d}",
                "misleading_primary": cancelled_to,
            }

            # -- postmortem actions: the tracker, then the review's decisions
            pm = f"PM-{index + 1:02d}"
            ids = []
            for k in range(6):
                n_action += 1
                aid = f"AI-{n_action:04d}"
                ids.append(aid)
                due = review_day + timedelta(days=rng.randint(-25, 25))
                actions.append(
                    {
                        "action_id": aid,
                        "postmortem_id": pm,
                        "service_id": sid,
                        "title": _ACTIONS[(index + k) % len(_ACTIONS)],
                        "owner": crew[k % len(crew)],
                        "due_on": due,
                        "status": "open",
                        "closed_on": None,
                    }
                )
            by_id = {a["action_id"]: a for a in actions[-6:]}
            dup, kept, moved, reopened = ids[1], ids[0], ids[2], ids[3]
            by_id[dup]["title"] = by_id[kept]["title"]  # the duplicate repeats its original
            by_id[moved]["due_on"] = review_day - timedelta(
                days=rng.randint(3, 10)
            )  # overdue as tracked
            by_id[reopened]["status"] = "closed"
            by_id[reopened]["closed_on"] = review_day - timedelta(days=rng.randint(8, 20))
            new_due = review_day + timedelta(days=rng.randint(10, 30))
            new_owner = next(p for p in crew if p != by_id[moved]["owner"])
            decided = review_day - timedelta(days=rng.randint(1, 4))
            for decision, aid, extra in (
                ("merged", dup, {"merged_into": kept}),
                ("reassigned", moved, {"new_owner": new_owner, "new_due_on": new_due}),
                ("reopened", reopened, {}),
            ):
                n_review += 1
                reviews.append(
                    {
                        "review_id": f"RV-{n_review:04d}",
                        "action_id": aid,
                        "reviewed_on": decided,
                        "decision": decision,
                        "merged_into": extra.get("merged_into"),
                        "new_owner": extra.get("new_owner"),
                        "new_due_on": extra.get("new_due_on"),
                    }
                )

            mine = list(by_id.values())
            decisions = {"merged": dup, "reassigned": moved, "reopened": reopened}
            gold["actions_tracker"] = _open_and_overdue(mine, review_day, None, new_due)
            gold["actions_after_review"] = _open_and_overdue(mine, review_day, decisions, new_due)
            gold["action_decisions"] = decisions

            # -- changes around the freeze
            mine_changes: list[dict[str, Any]] = []
            for k in range(3):
                n_ch += 1
                cid = f"CH-{n_ch:04d}"
                # inside the published freeze; after the freeze; on the day it was extended by
                planned = (
                    freeze_start + timedelta(days=rng.randint(0, 3)),
                    freeze_extended + timedelta(days=rng.randint(1, 3)),
                    freeze_extended,
                )[k]
                kind = "emergency" if k == 2 and index % 5 == 0 else "standard"
                changes.append(
                    {
                        "change_id": cid,
                        "service_id": sid,
                        "requested_on": planned - timedelta(days=rng.randint(5, 15)),
                        "planned_on": planned,
                        "kind": kind,
                        "status": "scheduled",
                    }
                )
                mine_changes.append(changes[-1])
            standing = revoked = None
            if index % 4 == 1:
                n_exception += 1
                standing = mine_changes[0]["change_id"]
                exceptions.append(
                    {
                        "exception_id": f"FX-{n_exception:03d}",
                        "change_id": standing,
                        "granted_on": freeze_start - timedelta(days=4),
                        "revoked_on": None,
                        "decided_by": crew[0],
                    }
                )
            if index % 4 == 3:
                n_exception += 1
                revoked = mine_changes[0]["change_id"]
                exceptions.append(
                    {
                        "exception_id": f"FX-{n_exception:03d}",
                        "change_id": revoked,
                        "granted_on": freeze_start - timedelta(days=6),
                        "revoked_on": freeze_start - timedelta(days=2),
                        "decided_by": crew[0],
                    }
                )
            gold["freeze"] = {
                "deferred": sorted(
                    c["change_id"]
                    for c in mine_changes
                    if c["kind"] == "standard"
                    and freeze_start <= c["planned_on"] <= freeze_extended
                    and c["change_id"] != standing
                ),
                "standing_exception": standing,
                "revoked_exception": revoked,
            }

            # -- context: incidents and deploys
            for _ in range(rng.randint(1, 3)):
                n_incident += 1
                opened = _utc(
                    start + timedelta(days=rng.randint(0, (end - start).days - 2)),
                    rng.randint(0, 22),
                )
                incidents.append(
                    {
                        "incident_id": f"INC-{n_incident:04d}",
                        "service_id": sid,
                        "severity": f"SEV{rng.randint(1, 3)}",
                        "opened_at": opened,
                        "resolved_at": opened + timedelta(minutes=rng.randint(20, 600)),
                        "commander": rng.choice(crew),
                        "postmortem_id": pm,
                    }
                )
            for _ in range(rng.randint(3, 6)):
                n_deploy += 1
                rolled = rng.random() < 0.15
                deploys.append(
                    {
                        "deploy_id": f"DP-{n_deploy:05d}",
                        "service_id": sid,
                        "deployed_at": _utc(
                            start + timedelta(days=rng.randint(0, (end - start).days)),
                            rng.randint(9, 16),
                            rng.choice((0, 15, 30, 45)),
                        ),
                        "author": rng.choice(crew),
                        "outcome": "rolled_back" if rolled else "succeeded",
                    }
                )
            truth["services"][sid] = gold

        def table(
            name: str,
            description: str,
            columns: tuple[tuple[str, str], ...],
            rows: list[dict[str, Any]],
            key: str,
        ) -> ScriptedTable:
            return ScriptedTable(name, description, columns, tuple(rows), key)

        tables = (
            table(
                "services",
                "The services the SRE team runs: tier and owning team.",
                (
                    ("service_id", "string"),
                    ("name", "string"),
                    ("tier", "int64"),
                    ("owner_team", "string"),
                ),
                services,
                "service_id",
            ),
            table(
                "oncall_rota",
                "The published on-call rota: primary, secondary and backup per service-week.",
                (
                    ("shift_id", "string"),
                    ("service_id", "string"),
                    ("week_start", "date"),
                    ("primary", "string"),
                    ("secondary", "string"),
                    ("backup", "string"),
                ),
                rota,
                "shift_id",
            ),
            table(
                "rota_changes",
                "Changes to the rota after it was published: swaps (agreed or cancelled) "
                "and leave handed to the backup.",
                (
                    ("change_id", "string"),
                    ("shift_id", "string"),
                    ("requested_on", "date"),
                    ("kind", "string"),
                    ("from_person", "string"),
                    ("to_person", "string"),
                    ("status", "string"),
                ),
                rota_changes,
                "change_id",
            ),
            table(
                "postmortem_actions",
                "The action-item tracker as last edited.",
                (
                    ("action_id", "string"),
                    ("postmortem_id", "string"),
                    ("service_id", "string"),
                    ("title", "string"),
                    ("owner", "string"),
                    ("due_on", "date"),
                    ("status", "string"),
                    ("closed_on", "date"),
                ),
                actions,
                "action_id",
            ),
            table(
                "action_reviews",
                "Decisions of the quarterly action review: merged, reassigned or reopened.",
                (
                    ("review_id", "string"),
                    ("action_id", "string"),
                    ("reviewed_on", "date"),
                    ("decision", "string"),
                    ("merged_into", "string"),
                    ("new_owner", "string"),
                    ("new_due_on", "date"),
                ),
                reviews,
                "review_id",
            ),
            table(
                "change_freeze",
                "The change freeze around the launch, as published and as extended.",
                (
                    ("freeze_id", "string"),
                    ("starts_on", "date"),
                    ("ends_on", "date"),
                    ("extended_to", "date"),
                ),
                [
                    {
                        "freeze_id": "FZ-1",
                        "starts_on": freeze_start,
                        "ends_on": freeze_end,
                        "extended_to": freeze_extended,
                    }
                ],
                "freeze_id",
            ),
            table(
                "changes",
                "Production changes and their planned day.",
                (
                    ("change_id", "string"),
                    ("service_id", "string"),
                    ("requested_on", "date"),
                    ("planned_on", "date"),
                    ("kind", "string"),
                    ("status", "string"),
                ),
                changes,
                "change_id",
            ),
            table(
                "freeze_exceptions",
                "Freeze exceptions granted (and revoked) by the change board.",
                (
                    ("exception_id", "string"),
                    ("change_id", "string"),
                    ("granted_on", "date"),
                    ("revoked_on", "date"),
                    ("decided_by", "string"),
                ),
                exceptions,
                "exception_id",
            ),
            table(
                "incidents",
                "Production incidents with their commander and postmortem.",
                (
                    ("incident_id", "string"),
                    ("service_id", "string"),
                    ("severity", "string"),
                    ("opened_at", "timestamp"),
                    ("resolved_at", "timestamp"),
                    ("commander", "string"),
                    ("postmortem_id", "string"),
                ),
                incidents,
                "incident_id",
            ),
            table(
                "deploys",
                "Deploys with their outcome.",
                (
                    ("deploy_id", "string"),
                    ("service_id", "string"),
                    ("deployed_at", "timestamp"),
                    ("author", "string"),
                    ("outcome", "string"),
                ),
                deploys,
                "deploy_id",
            ),
        )
        questions = (
            ScriptedQuestion(
                "sre.published_primary",
                "rota",
                "Who does the published rota name as primary for each service's maintenance week?",
                """
                SELECT r.service_id, r.shift_id, r."primary"
                FROM oncall_rota r
                JOIN (SELECT DISTINCT shift_id FROM rota_changes) c USING (shift_id)
                ORDER BY r.service_id
                """,
            ),
            ScriptedQuestion(
                "sre.open_actions_tracker",
                "actions",
                "How many action items does the tracker alone show open, per service?",
                """
                SELECT service_id, count(*) AS open_items FROM postmortem_actions
                WHERE status = 'open' GROUP BY service_id ORDER BY service_id
                """,
            ),
            ScriptedQuestion(
                "sre.deferred_changes",
                "freeze",
                "Which standard changes fall inside the extended freeze "
                "without a standing exception?",
                """
                SELECT c.change_id FROM changes c, change_freeze f
                WHERE c.kind = 'standard' AND c.planned_on BETWEEN f.starts_on AND f.extended_to
                  AND c.change_id NOT IN (
                    SELECT change_id FROM freeze_exceptions WHERE revoked_on IS NULL)
                ORDER BY c.change_id
                """,
            ),
        )
        return ScriptedData(tables=tables, questions=questions, truth=truth)


SCRIPTED_SCENARIOS.register(SreOps())
