"""``service_latency``: hourly p95 latency per service, deploys with release ids, commits.

For sets3-cc's ops forge fg1-15 (P571048095, ops-verified-vs-guess). Three services,
owned by the world's real engineering teams, each have an hourly p95 latency series, a
commit trail (sha, time, author, summary) and deploys that ship the commits made since
the last one.

One mid-window release of one service ships two commits, a **regressing** one and a
plausible **decoy** (a logging middleware, an obvious suspect). p95 rises by about half
from that deploy. A later release reverts only the regressing commit, and p95 returns.
Guessing the decoy is easy; verifying needs the revert and the series.

Tables:

* ``fact_latency_hourly``: sample_id, service, hour, p95_ms, requests;
* ``fact_commit``: sha, service, committed_at, author, summary, release_id;
* ``fact_deploy``: release_id, service, deployed_at, head_sha, commits.

The gold ``truth.json`` names the regressing commit, the decoy, the release that
shipped them and the release that fixed it.
"""

from __future__ import annotations

import hashlib
import math
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

_SERVICES = (("checkout-api", 180.0), ("search-api", 120.0), ("auth-api", 60.0))
_ROUTINE = (
    "Refactor {svc} request validation",
    "Add unit tests for {svc} handlers",
    "Update dependencies for {svc}",
    "Tidy error messages in {svc}",
    "Document {svc} configuration flags",
)


def _engineers(world: World) -> list[str]:
    names = sorted(
        str(n.props.get("name"))
        for n in world.nodes_by_type("Person")
        if n.props.get("department") in ("engineering", "platform", "security")
        and n.props.get("name")
    )
    return names or sorted(str(n.props.get("name")) for n in world.nodes_by_type("Person"))[:6]


def _sha(seed: int, n: int) -> str:
    return hashlib.sha256(f"{seed}\x00commit\x00{n}".encode()).hexdigest()[:10]


class ServiceLatency:
    name = "service_latency"
    title = "Service Latency, Releases and Commits"
    brief = (
        "Hourly p95 latency per service, deploys with release ids, and a commit trail with "
        "authors; one release ships a regressing commit and a plausible decoy, and a later "
        "release reverts only the regressing one."
    )

    def build_data(self, *, world: World, seed: int, start: date, end: date) -> ScriptedData:
        rng = random.Random(f"{seed}\x00service_latency")
        people = _engineers(world) or ["an engineer"]
        t0 = datetime.combine(start, time(0, 0))
        hours = int((datetime.combine(end, time(23, 0)) - t0).total_seconds() // 3600) + 1
        target = _SERVICES[0][0]
        bad_deploy = t0 + timedelta(hours=int(hours * 0.45))
        fix_deploy = bad_deploy + timedelta(hours=rng.randint(60, 120))

        commits: list[dict[str, Any]] = []
        deploys: list[dict[str, Any]] = []
        n = 0
        regressing = decoy = revert = ""
        bad_release = fix_release = ""
        for svc, _ in _SERVICES:
            pending: list[str] = []
            at = t0 + timedelta(hours=rng.randint(4, 20))
            next_deploy = t0 + timedelta(days=rng.randint(3, 6))
            special_done = fix_done = False
            while at < t0 + timedelta(hours=hours):
                is_target = svc == target
                if is_target and not special_done and at >= bad_deploy - timedelta(hours=30):
                    for summary, role in (
                        ("Switch session cache writes to synchronous mode", "regressing"),
                        ("Add request logging middleware", "decoy"),
                    ):
                        n += 1
                        sha = _sha(seed, n)
                        commits.append(
                            {
                                "sha": sha,
                                "service": svc,
                                "committed_at": bad_deploy - timedelta(hours=rng.randint(2, 20)),
                                "author": rng.choice(people),
                                "summary": summary,
                            }
                        )
                        pending.append(sha)
                        if role == "regressing":
                            regressing = sha
                        else:
                            decoy = sha
                    next_deploy = bad_deploy
                    special_done = True
                elif (
                    is_target
                    and special_done
                    and not fix_done
                    and at >= fix_deploy - timedelta(hours=12)
                ):
                    n += 1
                    revert = _sha(seed, n)
                    commits.append(
                        {
                            "sha": revert,
                            "service": svc,
                            "committed_at": fix_deploy - timedelta(hours=rng.randint(1, 8)),
                            "author": rng.choice(people),
                            "summary": f"Revert {regressing}: restore asynchronous "
                            "session cache writes",
                        }
                    )
                    pending.append(revert)
                    next_deploy = fix_deploy
                    fix_done = True
                else:
                    n += 1
                    sha = _sha(seed, n)
                    commits.append(
                        {
                            "sha": sha,
                            "service": svc,
                            "committed_at": at,
                            "author": rng.choice(people),
                            "summary": rng.choice(_ROUTINE).format(svc=svc),
                        }
                    )
                    pending.append(sha)
                at += timedelta(hours=rng.randint(18, 60))
                if at >= next_deploy and pending:
                    release = f"{svc}-r{next_deploy.strftime('%Y%m%d')}-{len(deploys) + 1}"
                    for c in commits:
                        if c["sha"] in pending:
                            c["release_id"] = release
                    deploys.append(
                        {
                            "release_id": release,
                            "service": svc,
                            "deployed_at": next_deploy,
                            "head_sha": pending[-1],
                            "commits": len(pending),
                        }
                    )
                    if regressing in pending:
                        bad_release = release
                    if revert and revert in pending:
                        fix_release = release
                    pending = []
                    next_deploy = next_deploy + timedelta(days=rng.randint(3, 6))
                    if svc == target and special_done and not fix_done:
                        next_deploy = max(next_deploy, fix_deploy)
        for c in commits:
            c.setdefault("release_id", "")

        samples: list[dict[str, Any]] = []
        for svc, base in _SERVICES:
            for h in range(hours):
                at = t0 + timedelta(hours=h)
                diurnal = 1.0 + 0.25 * math.sin(2 * math.pi * ((at.hour - 8) % 24) / 24)
                regressed = svc == target and bad_deploy <= at < fix_deploy
                p95 = base * diurnal * (1.5 if regressed else 1.0) * rng.uniform(0.92, 1.08)
                samples.append(
                    {
                        "sample_id": f"{svc}-{at.strftime('%Y%m%dT%H')}",
                        "service": svc,
                        "hour": at,
                        "p95_ms": round(p95, 1),
                        "requests": int(4000 * diurnal * rng.uniform(0.85, 1.15)),
                    }
                )
        truth = {
            "service": target,
            "regressing_commit": regressing,
            "decoy_commit": decoy,
            "release": bad_release,
            "deployed_at": bad_deploy,
            "fix_release": fix_release,
            "revert_commit": revert,
            "fixed_at": fix_deploy,
            "effect": "p95 about 1.5x from the release until the revert",
        }
        tables = (
            ScriptedTable(
                "fact_latency_hourly",
                "Hourly p95 latency and request count per service.",
                (
                    ("sample_id", "string"),
                    ("service", "string"),
                    ("hour", "timestamp"),
                    ("p95_ms", "float64"),
                    ("requests", "int64"),
                ),
                tuple(samples),
                "sample_id",
            ),
            ScriptedTable(
                "fact_commit",
                "Commits: sha, service, time, author (a real engineer), summary, and "
                "the release that shipped it.",
                (
                    ("sha", "string"),
                    ("service", "string"),
                    ("committed_at", "timestamp"),
                    ("author", "string"),
                    ("summary", "string"),
                    ("release_id", "string"),
                ),
                tuple(sorted(commits, key=lambda c: (c["committed_at"], c["sha"]))),
                "sha",
            ),
            ScriptedTable(
                "fact_deploy",
                "Deploys: the release id, service, time, head commit and number of "
                "commits shipped.",
                (
                    ("release_id", "string"),
                    ("service", "string"),
                    ("deployed_at", "timestamp"),
                    ("head_sha", "string"),
                    ("commits", "int64"),
                ),
                tuple(sorted(deploys, key=lambda d: (d["deployed_at"], d["release_id"]))),
                "release_id",
            ),
        )
        views = (
            (
                "p95_by_release",
                "SELECT d.release_id, d.service, d.deployed_at, "
                "round(avg(l.p95_ms), 1) AS mean_p95_ms "
                "FROM fact_deploy d JOIN fact_latency_hourly l ON l.service = d.service "
                "AND l.hour >= d.deployed_at AND l.hour < d.deployed_at + INTERVAL 24 HOUR "
                "GROUP BY d.release_id, d.service, d.deployed_at",
            ),
        )
        questions = (
            ScriptedQuestion(
                "sl.release_p95",
                "latency",
                "What was the mean p95 in the 24 hours after each release?",
                "SELECT release_id, service, mean_p95_ms FROM p95_by_release ORDER BY deployed_at",
            ),
            ScriptedQuestion(
                "sl.regressing_release",
                "latency",
                "Which release raised p95 the most compared with the previous release "
                "of the same service?",
                """
                SELECT release_id, service, mean_p95_ms - lag(mean_p95_ms) OVER (
                    PARTITION BY service ORDER BY deployed_at) AS change_ms
                FROM p95_by_release QUALIFY change_ms IS NOT NULL ORDER BY change_ms DESC LIMIT 1
                """,
            ),
            ScriptedQuestion(
                "sl.revert",
                "latency",
                "Which commit was reverted, and in which release?",
                "SELECT sha, summary, release_id FROM fact_commit WHERE summary LIKE 'Revert %'",
            ),
        )
        return ScriptedData(tables=tables, views=views, questions=questions, truth=truth)


SCRIPTED_SCENARIOS.register(ServiceLatency())
