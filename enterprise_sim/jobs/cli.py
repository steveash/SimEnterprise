"""``enterprise-sim job ...``: catalog, estimate, create, run, pause, status, list.

Wires the ``job`` command group (``docs/EXPLORER_RUNS.md`` §3.4). Every
subcommand speaks JSON on stdout (one document for a one-shot command, JSON
Lines for ``job run``'s streamed progress) and human-readable notes on stderr —
the same convention the rest of the CLI uses for machine-facing output, so a
supervising sidecar can spawn any of these and parse stdout without guessing.

Module-level imports here are deliberately light (``argparse``/``json``/``sys``/
``pathlib``/``datetime`` only); everything that touches the simulator proper is
imported inside each command function, matching the rest of
:mod:`enterprise_sim.cli`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["add_job_parser"]


def _cmd_job(args: argparse.Namespace) -> int:
    """Fallback when ``job`` is invoked without a subcommand (mirrors ``bench``)."""
    args.job_parser.print_help()
    return 2


def _cmd_job_catalog(args: argparse.Namespace) -> int:
    """Print the catalog JSON: archetypes, playbooks, producers, backends, models, …."""
    from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
    from enterprise_sim.core.config.models import CompanySize, LLMBackend
    from enterprise_sim.core.config.models import RunConfig as _RunConfig
    from enterprise_sim.core.llm.pricing import PRICING
    from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, PRODUCERS, discover_all

    discover_all()

    archetypes: list[dict[str, Any]] = []
    for plugin in ARCHETYPES:
        if isinstance(plugin, DepartmentArchetypeSpec):
            archetypes.append(
                {
                    "name": plugin.name,
                    "charter": plugin.charter,
                    "typical_goals": list(plugin.typical_goals),
                    "team_shapes": [
                        {"title": t.title, "count": t.count, "skills": list(t.skills)}
                        for t in plugin.team_shapes
                    ],
                    "playbooks": list(plugin.playbooks),
                }
            )
        else:
            archetypes.append({"name": plugin.name, "playbooks": list(plugin.playbooks)})

    playbooks = [
        {"name": p.name, "vertical": p.vertical, "deliverables": list(p.deliverables)}
        for p in PLAYBOOKS
    ]
    models = {
        name: {
            "input_per_mtok": pricing.input_per_mtok,
            "cached_input_per_mtok": pricing.cached_input_per_mtok,
            "output_per_mtok": pricing.output_per_mtok,
        }
        for name, pricing in PRICING.items()
    }
    catalog = {
        "archetypes": archetypes,
        "playbooks": playbooks,
        "producers": PRODUCERS.names(),
        "backends": [b.value for b in LLMBackend] + ["fake"],
        "models": models,
        "company_sizes": [s.value for s in CompanySize],
        "schema": _RunConfig.model_json_schema(),
    }
    print(json.dumps(catalog, sort_keys=True))
    return 0


def _cmd_job_estimate(args: argparse.Namespace) -> int:
    """Print JSON: the RenderEstimate fields + world counts + event count (dry-run, keyless)."""
    from enterprise_sim.assembly.corpus import build_corpus
    from enterprise_sim.assembly.runner import llm_config_for
    from enterprise_sim.core.config import load_config
    from enterprise_sim.core.llm import CostCeilingExceeded, build_client
    from enterprise_sim.world_builders import build_world

    config = load_config(args.config)
    backend = config.model.backend.value if args.live else "fake"
    client = build_client(llm_config_for(config, backend=backend))
    world = build_world(config)
    try:
        corpus = build_corpus(world, config, client, dry_run=True)
    except CostCeilingExceeded as exc:
        print(json.dumps({"error": str(exc), "type": "CostCeilingExceeded"}))
        return 1
    assert corpus.estimate is not None  # build_corpus always estimates.
    estimate = corpus.estimate
    payload = {
        "num_artifacts": estimate.num_artifacts,
        "estimated_cost_usd": estimate.estimated_cost_usd,
        "input_tokens_each": estimate.input_tokens_each,
        "output_tokens_each": estimate.output_tokens_each,
        "cached_input_tokens_each": estimate.cached_input_tokens_each,
        "model": estimate.model,
        "world": {
            "nodes": world.node_count,
            "edges": world.edge_count,
            "departments": len(world.nodes_by_type("Department")),
            "scenarios": len(
                [n for n in world.nodes_by_type("Initiative") if n.props.get("type") == "scenario"]
            ),
        },
        "events": len(corpus.journal),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cmd_job_create(args: argparse.Namespace) -> int:
    """Create ``<jobs-root>/<job-id>/`` (job/config/state.json); print ``{job_id, job_dir}``."""
    from enterprise_sim.core.config import load_config
    from enterprise_sim.core.config.models import ProjectConfig
    from enterprise_sim.jobs.extend import ExtendError, extend_config
    from enterprise_sim.jobs.state import JobState, generate_job_id, write_state

    config = load_config(args.config)

    kind = "new"
    parent_run_dir: str | None = None
    changes: dict[str, Any] | None = None
    if args.extend is not None:
        kind = "extend"
        parent_run_dir = str(args.extend)
        parent_config = load_config(Path(args.extend) / "config.snapshot.json")
        period_end = (
            datetime.strptime(args.period_end, "%Y-%m-%d").date() if args.period_end else None
        )
        try:
            add_projects = tuple(
                ProjectConfig.model_validate(json.loads(p)) for p in args.add_project
            )
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"enterprise-sim job create: invalid --add-project: {exc}", file=sys.stderr)
            return 2
        try:
            config = extend_config(parent_config, period_end=period_end, add_projects=add_projects)
        except ExtendError as exc:
            print(f"enterprise-sim job create: {exc}", file=sys.stderr)
            return 2
        changes = {
            "period_end": [
                parent_config.simulation.period_end.isoformat(),
                config.simulation.period_end.isoformat(),
            ],
            "added_projects": [p.name for p in add_projects],
        }

    jobs_root = args.jobs_root
    jobs_root.mkdir(parents=True, exist_ok=True)
    job_id = generate_job_id(config.company.name)
    job_dir = jobs_root / job_id
    # The id's one-second resolution can collide when jobs for the same company
    # are created in quick succession (e.g. immediately extending a just-created
    # one); disambiguate with a numeric suffix rather than failing the request.
    suffix = 2
    while job_dir.exists():
        job_id = f"{generate_job_id(config.company.name)}-{suffix}"
        job_dir = jobs_root / job_id
        suffix += 1
    job_dir.mkdir(parents=True)

    if config.scale.cache_dir is None:
        config = config.model_copy(
            update={
                "scale": config.scale.model_copy(update={"cache_dir": str(job_dir / "llm-cache")})
            }
        )

    now = datetime.now(UTC).isoformat()
    job_payload = {
        "kind": kind,
        "live": bool(args.live),
        "config": config.model_dump(mode="json"),
        "parent_run_dir": parent_run_dir,
        "changes": changes,
        "created_at": now,
    }
    (job_dir / "job.json").write_text(
        json.dumps(job_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (job_dir / "config.json").write_text(
        json.dumps(config.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_state(job_dir, JobState(status="created", started_at=now))

    print(json.dumps({"job_id": job_id, "job_dir": str(job_dir)}))
    return 0


def _cmd_job_run(args: argparse.Namespace) -> int:
    """Run or resume ``JOB_DIR``, streaming progress JSONL on stdout (+ ``progress.jsonl``)."""
    from enterprise_sim.jobs.progress import JsonlSink
    from enterprise_sim.jobs.worker import run_job

    job_dir: Path = args.job_dir
    if not (job_dir / "job.json").is_file():
        print(f"enterprise-sim job run: no such job: {job_dir}", file=sys.stderr)
        return 2

    sink = JsonlSink(sys.stdout, also_to=job_dir / "progress.jsonl")
    try:
        state = run_job(job_dir, progress=sink)
    finally:
        sink.close()

    if state.status == "done":
        print(f"enterprise-sim job run: {job_dir} done ({state.run_id})", file=sys.stderr)
        return 0
    if state.status == "paused":
        print(
            f"enterprise-sim job run: {job_dir} paused "
            f"({state.artifacts_done}/{state.artifacts_total} artifacts)",
            file=sys.stderr,
        )
        return 0
    print(f"enterprise-sim job run: {job_dir} failed: {state.error}", file=sys.stderr)
    return 1


def _cmd_job_pause(args: argparse.Namespace) -> int:
    """Write ``control.json``; a running worker on this job dir stops cooperatively."""
    from enterprise_sim.jobs.control import RunControl

    RunControl(args.job_dir).request_pause()
    print(f"enterprise-sim job pause: requested for {args.job_dir}", file=sys.stderr)
    return 0


def _cmd_job_status(args: argparse.Namespace) -> int:
    """Print ``state.json`` for one job."""
    from enterprise_sim.jobs.state import read_state

    state = read_state(args.job_dir)
    if state is None:
        print(json.dumps({"error": f"no such job: {args.job_dir}"}))
        return 2
    print(json.dumps(state.to_dict(), sort_keys=True))
    return 0


def _cmd_job_list(args: argparse.Namespace) -> int:
    """Print every job under ``--jobs-root`` as ``{"jobs": [...]}``."""
    from enterprise_sim.jobs.state import is_alive, read_state

    jobs_root: Path = args.jobs_root
    jobs: list[dict[str, Any]] = []
    if jobs_root.is_dir():
        for job_dir in sorted(jobs_root.iterdir()):
            if not job_dir.is_dir():
                continue
            state = read_state(job_dir)
            if state is None:
                continue
            job_json_path = job_dir / "job.json"
            summary: dict[str, Any] = {}
            if job_json_path.is_file():
                job_data = json.loads(job_json_path.read_text(encoding="utf-8"))
                summary = {
                    "kind": job_data.get("kind"),
                    "live": job_data.get("live"),
                    "created_at": job_data.get("created_at"),
                    "parent_run_dir": job_data.get("parent_run_dir"),
                }
            jobs.append(
                {
                    "job_id": job_dir.name,
                    "job_dir": str(job_dir),
                    "state": state.to_dict(),
                    "job": summary,
                    "alive": is_alive(state.pid) if state.status == "running" else False,
                }
            )
    print(json.dumps({"jobs": jobs}, sort_keys=True))
    return 0


def add_job_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the ``job`` command group: catalog/estimate/create/run/pause/status/list."""
    job_parser = subparsers.add_parser(
        "job",
        help="create, run, pause, and inspect run jobs (progress/cost/pause-resume)",
        description=(
            "Launch and drive a run as a resumable job (docs/EXPLORER_RUNS.md): "
            "catalog what can be configured, dry-run estimate a config's cost, "
            "create a job directory, run/resume it with streamed progress, "
            "request a cooperative pause, and inspect or list job state. Every "
            "subcommand speaks JSON on stdout; human notes go to stderr."
        ),
    )
    job_subparsers = job_parser.add_subparsers(
        dest="job_command",
        required=True,
        metavar="{catalog,estimate,create,run,pause,status,list}",
    )
    job_parser.set_defaults(func=_cmd_job, job_parser=job_parser)

    catalog_parser = job_subparsers.add_parser(
        "catalog", help="print the catalog JSON (archetypes/playbooks/producers/backends/models/…)"
    )
    catalog_parser.set_defaults(func=_cmd_job_catalog)

    estimate_parser = job_subparsers.add_parser(
        "estimate", help="dry-run a config's artifact count + cost estimate (keyless)"
    )
    estimate_parser.add_argument(
        "-c", "--config", required=True, type=Path, metavar="PATH", help="path to a run config"
    )
    estimate_parser.add_argument(
        "--live", action="store_true", help="price against the config's configured backend"
    )
    estimate_parser.set_defaults(func=_cmd_job_estimate)

    create_parser = job_subparsers.add_parser("create", help="create a job directory")
    create_parser.add_argument(
        "--jobs-root", required=True, type=Path, dest="jobs_root", metavar="DIR"
    )
    create_parser.add_argument(
        "-c", "--config", required=True, type=Path, metavar="PATH", help="path to a run config"
    )
    create_parser.add_argument(
        "--live", action="store_true", help="render against the real provider when run"
    )
    create_parser.add_argument(
        "--extend",
        type=Path,
        default=None,
        metavar="RUN_DIR",
        help="extend a finished run instead of starting a new one",
    )
    create_parser.add_argument(
        "--period-end",
        dest="period_end",
        default=None,
        metavar="YYYY-MM-DD",
        help="[--extend] move the simulation window's end date later",
    )
    create_parser.add_argument(
        "--add-project",
        dest="add_project",
        action="append",
        default=[],
        metavar="JSON",
        help="[--extend] a project to append, as a JSON object (repeatable)",
    )
    create_parser.set_defaults(func=_cmd_job_create)

    run_parser = job_subparsers.add_parser("run", help="run or resume a job")
    run_parser.add_argument("job_dir", type=Path, metavar="JOB_DIR")
    run_parser.set_defaults(func=_cmd_job_run)

    pause_parser = job_subparsers.add_parser("pause", help="request a cooperative pause")
    pause_parser.add_argument("job_dir", type=Path, metavar="JOB_DIR")
    pause_parser.set_defaults(func=_cmd_job_pause)

    status_parser = job_subparsers.add_parser("status", help="print state.json")
    status_parser.add_argument("job_dir", type=Path, metavar="JOB_DIR")
    status_parser.set_defaults(func=_cmd_job_status)

    list_parser = job_subparsers.add_parser("list", help="list every job's state")
    list_parser.add_argument(
        "--jobs-root", required=True, type=Path, dest="jobs_root", metavar="DIR"
    )
    list_parser.set_defaults(func=_cmd_job_list)
