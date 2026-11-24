"""``enterprise-sim world …``: create, extend, fork, run, pause, status, list, estimate, catalog.

Wires the ``world`` command group (``docs/EXPLORER_RUNS.md`` §3.4). A world is
the one folder a simulated company lives in (``enterprise_sim.worlds``); these
commands mint it, build it (as a resumable, cost-tracked job), grow it in place,
fork a parallel one, and list what exists. Machine-facing commands (``run``'s
streamed progress, ``status``, ``list``, and every ``--no-run`` create) speak
JSON on stdout with human notes on stderr, so a supervising sidecar can spawn
any of them and parse stdout without guessing; the human-facing forms print a
readable summary instead.

Module-level imports here are deliberately light; everything that touches the
simulator proper is imported inside each command function, matching
:mod:`enterprise_sim.cli`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from pydantic import ValidationError

if TYPE_CHECKING:
    from enterprise_sim.core.config import WorldConfig
    from enterprise_sim.core.config.models import ProjectConfig

__all__ = ["DEFAULT_WORLDS_ROOT", "add_world_parser"]

#: Where ``world list`` looks when no ``--worlds-root`` is given.
DEFAULT_WORLDS_ROOT = Path("worlds")


# -- helpers ------------------------------------------------------------------


def _say(args: argparse.Namespace) -> TextIO:
    """Where human notes go: stderr when stdout is reserved for JSON (``--no-run``)."""
    return sys.stderr if getattr(args, "no_run", False) else sys.stdout


def _load_config(path: str | Path, *, command: str) -> WorldConfig | None:
    """Load a config, printing the CLI-style error and returning ``None`` on failure."""
    from enterprise_sim.core.config import ConfigError, load_config

    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"enterprise-sim world {command}: {exc}", file=sys.stderr)
        return None
    except ValidationError as exc:
        print(f"enterprise-sim world {command}: invalid config {path}:\n{exc}", file=sys.stderr)
        return None


def _apply_scale_overrides(config: WorldConfig, args: argparse.Namespace) -> WorldConfig:
    """Apply ``--max-concurrency`` / ``--cost-ceiling`` CLI overrides onto ``config``."""
    updates: dict[str, object] = {}
    if getattr(args, "max_concurrency", None) is not None:
        updates["max_concurrency"] = args.max_concurrency
    if getattr(args, "cost_ceiling", None) is not None:
        updates["cost_ceiling_usd"] = args.cost_ceiling
    if not updates:
        return config
    scale = config.scale.model_copy(update=updates)
    return config.model_copy(update={"scale": scale})


def _parse_projects(raw: list[str], *, command: str) -> tuple[ProjectConfig, ...] | None:
    """Parse repeated ``--add-project JSON`` values; ``None`` (after printing) if any is bad."""
    from enterprise_sim.core.config.models import ProjectConfig

    try:
        return tuple(ProjectConfig.model_validate(json.loads(p)) for p in raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"enterprise-sim world {command}: invalid --add-project: {exc}", file=sys.stderr)
        return None


def _parse_date(raw: str | None) -> date | None:
    return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None


def _refuse_if_running(world_dir: Path, status: str, pid: int | None, *, command: str) -> bool:
    """``True`` (after printing) when a live worker owns this world right now."""
    from enterprise_sim.jobs.state import is_alive

    if status == "running" and is_alive(pid):
        print(
            f"enterprise-sim world {command}: {world_dir} is being built by pid {pid}; "
            "pause it first",
            file=sys.stderr,
        )
        return True
    return False


def _run_inline(world_dir: Path, say: TextIO) -> int:
    """Build ``world_dir`` in this process with a file-only progress log; print a summary."""
    from enterprise_sim.jobs.progress import JsonlSink
    from enterprise_sim.jobs.worker import run_job
    from enterprise_sim.worlds.file import progress_log_path

    sink = JsonlSink(None, also_to=progress_log_path(world_dir))
    try:
        state = run_job(world_dir, progress=sink)
    finally:
        sink.close()
    return _report(world_dir, state, say)


def _report(world_dir: Path, state: Any, say: TextIO) -> int:
    if state.status == "done":
        print(
            f"enterprise-sim world: built {world_dir} "
            f"({state.artifacts_total} artifacts, {state.artifacts_cached} from cache, "
            f"${state.cost_usd_segment:.4f} this run, ${state.cost_usd_total:.4f} total)",
            file=say,
        )
        return 0
    if state.status == "paused":
        print(
            f"enterprise-sim world: {world_dir} paused "
            f"({state.artifacts_done}/{state.artifacts_total} artifacts); "
            f"resume with: enterprise-sim world run {world_dir}",
            file=say,
        )
        return 0
    error = state.error or {}
    print(
        f"enterprise-sim world: {world_dir} failed: {error.get('type')}: {error.get('message')}",
        file=sys.stderr,
    )
    return 1


# -- commands -----------------------------------------------------------------


def _cmd_world(args: argparse.Namespace) -> int:
    args.world_parser.print_help()
    return 2


def _cmd_world_catalog(args: argparse.Namespace) -> int:
    """Print the catalog JSON: archetypes, playbooks, producers, backends, models, …."""
    from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
    from enterprise_sim.core.config.models import CompanySize, LLMBackend
    from enterprise_sim.core.config.models import WorldConfig as _WorldConfig
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
        "data_scenarios": _data_scenarios_catalog(),
        "producers": PRODUCERS.names(),
        "backends": [b.value for b in LLMBackend] + ["fake", "synth"],
        "models": models,
        "company_sizes": [s.value for s in CompanySize],
        "schema": _WorldConfig.model_json_schema(),
    }
    print(json.dumps(catalog, sort_keys=True))
    return 0


def _data_scenarios_catalog() -> list[dict[str, Any]]:
    """Every registered data scenario (all templates), tagged with its template.

    Empty when the ``data`` extra is not installed, so the catalog never fails.
    """
    try:
        from enterprise_sim.data_products.scenarios import discover_scenarios
    except ModuleNotFoundError:
        return []
    from enterprise_sim.core.registry import TEMPLATE_MODULE_PREFIX

    registry = discover_scenarios()
    prefix = f"{TEMPLATE_MODULE_PREFIX}."
    out: list[dict[str, Any]] = []
    for name in registry.names():
        origin = registry.origin(name) or ""
        out.append(
            {
                "name": name,
                "title": registry.get(name).title,
                "brief": registry.get(name).brief,
                "source": f"template:{origin[len(prefix) :]}"
                if origin.startswith(prefix)
                else "builtin",
            }
        )
    return out


def _cmd_world_estimate(args: argparse.Namespace) -> int:
    """Print JSON: the RenderEstimate fields + world counts + event count (dry-run, keyless)."""
    from enterprise_sim.assembly.corpus import build_corpus
    from enterprise_sim.assembly.runner import llm_config_for, render_backend_for
    from enterprise_sim.core.llm import CostCeilingExceeded, build_client
    from enterprise_sim.world_builders import build_world

    config_path = args.config_opt if args.config_opt is not None else args.config
    if config_path is None:
        print("enterprise-sim world estimate: provide a config path (.toml or .json)")
        return 2
    config = _load_config(config_path, command="estimate")
    if config is None:
        return 2
    backend = render_backend_for(config, live=args.live)
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


def _cmd_world_create(args: argparse.Namespace) -> int:
    """Mint ``<worlds-root>/<world-id>/.world`` from a config and (unless ``--no-run``) build it.

    Idempotent for the same config: a world that already exists is resumed /
    rebuilt (every rendered artifact is a cache hit). A world whose ``.world``
    carries a *different* config than the one given is refused — that is what
    ``world extend`` is for.
    """
    config_path = args.config_opt if args.config_opt is not None else args.config
    if config_path is None:
        print("enterprise-sim world create: provide a config path (.toml or .json)")
        return 2
    config = _load_config(config_path, command="create")
    if config is None:
        return 2
    if args.worlds_root is not None:
        config = config.model_copy(update={"worlds_root": args.worlds_root})
    config = _apply_scale_overrides(config, args)
    say = _say(args)

    print(
        f"enterprise-sim world create: validated config for {config.company.name} "
        f"({config.company.vertical}, {config.company.size.value}); "
        f"seed={config.seed}, window={config.simulation.period_start.isoformat()}"
        f"..{config.simulation.period_end.isoformat()}, projects={len(config.projects)}; "
        f"backend={config.model.backend.value if args.live else config.model.offline_backend.value}"
        f"{'' if args.live else ' (deterministic; pass --live for the real provider)'}",
        file=say,
    )

    from enterprise_sim.core.llm import CostCeilingExceeded

    if args.dry_run:
        from enterprise_sim.assembly import estimate_run

        try:
            estimate = estimate_run(config, live=args.live)
        except CostCeilingExceeded as exc:
            print(f"enterprise-sim world create: {exc}", file=say)
            return 1
        ceiling = config.scale.cost_ceiling_usd
        print(
            f"enterprise-sim world create (dry-run): {estimate.num_artifacts} artifacts, "
            f"estimated ${estimate.estimated_cost_usd:.4f} "
            f"({estimate.input_tokens_each}+{estimate.output_tokens_each} tok/artifact, "
            f"model {estimate.model})"
            + (f"; ceiling ${ceiling:.4f}" if ceiling is not None else ""),
            file=say,
        )
        return 0

    from enterprise_sim.assembly import build_world_file, compute_config_digest, compute_world_id
    from enterprise_sim.assembly.runner import render_backend_for
    from enterprise_sim.worlds.file import read_world, write_world

    backend = render_backend_for(config, live=args.live)
    digest = compute_config_digest(config, backend=backend)
    world_id = compute_world_id(config, digest=digest)
    world_dir = Path(config.worlds_root) / world_id
    existing = read_world(world_dir)
    if existing is not None:
        if existing.config_digest != digest:
            print(
                f"enterprise-sim world create: {world_dir} already exists with a different "
                "config; grow it with `enterprise-sim world extend` instead",
                file=sys.stderr,
            )
            return 2
        if _refuse_if_running(world_dir, existing.job.status, existing.job.pid, command="create"):
            return 2
        print(f"enterprise-sim world create: {world_dir} exists; resuming", file=say)
    else:
        world_dir.mkdir(parents=True, exist_ok=True)
        write_world(world_dir, build_world_file(config, backend=backend, live=args.live))

    if args.no_run:
        print(json.dumps({"world_id": world_id, "world_dir": str(world_dir)}))
        return 0
    return _run_inline(world_dir, say)


def _cmd_world_extend(args: argparse.Namespace) -> int:
    """Grow a world in place: later ``period_end`` and/or more projects; then build it."""
    from enterprise_sim.assembly import canonical_config, compute_config_digest
    from enterprise_sim.assembly.runner import render_backend_for
    from enterprise_sim.core.config import WorldConfig
    from enterprise_sim.jobs.extend import ExtendError, extend_config
    from enterprise_sim.worlds.file import WorldFile, read_world, update_world

    world_dir: Path = args.world_dir
    world = read_world(world_dir)
    if world is None:
        print(f"enterprise-sim world extend: no such world: {world_dir}", file=sys.stderr)
        return 2
    if _refuse_if_running(world_dir, world.job.status, world.job.pid, command="extend"):
        return 2

    parent = WorldConfig.model_validate(world.config)
    add_projects = _parse_projects(args.add_project, command="extend")
    if add_projects is None:
        return 2
    try:
        child = extend_config(
            parent, period_end=_parse_date(args.period_end), add_projects=add_projects
        )
    except ExtendError as exc:
        print(f"enterprise-sim world extend: {exc}", file=sys.stderr)
        return 2
    added_data = [n for n in dict.fromkeys(args.add_data) if n not in parent.data.scenarios]
    if added_data:
        plan = child.data.model_copy(update={"scenarios": (*child.data.scenarios, *added_data)})
        child = child.model_copy(update={"data": plan})
    if child is parent:
        print(
            "enterprise-sim world extend: nothing to extend "
            "(give --period-end, --add-project and/or --add-data)",
            file=sys.stderr,
        )
        return 2

    live = True if args.live else world.live
    backend = render_backend_for(child, live=live)
    digest = compute_config_digest(child, backend=backend)
    changes = {
        "period_end": [
            parent.simulation.period_end.isoformat(),
            child.simulation.period_end.isoformat(),
        ],
        "added_projects": [p.name for p in add_projects],
        "added_data": added_data,
    }
    now = datetime.now(UTC).isoformat()

    def adopt(w: WorldFile) -> None:
        w.record_extend(
            config=canonical_config(child), config_digest=digest, changes=changes, at=now
        )
        w.live = live
        w.render_backend = backend
        w.window = {
            "start": child.simulation.period_start.isoformat(),
            "end": child.simulation.period_end.isoformat(),
        }
        w.job.status = "created"
        w.job.phase = None
        w.job.error = None
        w.job.finished_at = None

    update_world(world_dir, adopt)

    if args.no_run:
        print(
            json.dumps(
                {"world_id": world.world_id, "world_dir": str(world_dir), "changes": changes}
            )
        )
        return 0
    return _run_inline(world_dir, _say(args))


def _cmd_world_fork(args: argparse.Namespace) -> int:
    """Create a parallel world from an existing one (with lineage + a seeded cache)."""
    from enterprise_sim.assembly import build_world_file, compute_config_digest, compute_world_id
    from enterprise_sim.assembly.runner import render_backend_for
    from enterprise_sim.core.config import WorldConfig
    from enterprise_sim.jobs.extend import ExtendError, copy_cache, extend_config
    from enterprise_sim.worlds.file import cache_dir, read_world, write_world

    world_dir: Path = args.world_dir
    world = read_world(world_dir)
    if world is None:
        print(f"enterprise-sim world fork: no such world: {world_dir}", file=sys.stderr)
        return 2

    parent = WorldConfig.model_validate(world.config)
    add_projects = _parse_projects(args.add_project, command="fork")
    if add_projects is None:
        return 2
    try:
        child = extend_config(
            parent, period_end=_parse_date(args.period_end), add_projects=add_projects
        )
    except ExtendError as exc:
        print(f"enterprise-sim world fork: {exc}", file=sys.stderr)
        return 2
    if args.seed is not None:
        child = child.model_copy(update={"seed": args.seed})
    added_data = [n for n in dict.fromkeys(args.add_data) if n not in child.data.scenarios]
    if added_data:
        child = child.model_copy(
            update={
                "data": child.data.model_copy(
                    update={"scenarios": (*child.data.scenarios, *added_data)}
                )
            }
        )

    live = True if args.live else world.live
    backend = render_backend_for(child, live=live)
    digest = compute_config_digest(child, backend=backend)
    if digest == world.config_digest:
        print(
            "enterprise-sim world fork: the fork would be identical to its parent "
            "(give --seed, --period-end and/or --add-project)",
            file=sys.stderr,
        )
        return 2

    worlds_root = args.worlds_root if args.worlds_root is not None else world_dir.parent
    child_id = compute_world_id(child, digest=digest)
    child_dir = Path(worlds_root) / child_id
    if child_dir.exists():
        print(f"enterprise-sim world fork: {child_dir} already exists", file=sys.stderr)
        return 2
    changes = {
        "seed": [parent.seed, child.seed],
        "period_end": [
            parent.simulation.period_end.isoformat(),
            child.simulation.period_end.isoformat(),
        ],
        "added_projects": [p.name for p in add_projects],
    }
    child_dir.mkdir(parents=True)
    forked = build_world_file(child, backend=backend, live=live)
    forked.lineage = {
        "parent_world_id": world.world_id,
        "parent_world_dir": str(world_dir),
        "changes": changes,
    }
    write_world(child_dir, forked)
    copy_cache(cache_dir(world_dir), cache_dir(child_dir))

    if args.no_run:
        print(json.dumps({"world_id": child_id, "world_dir": str(child_dir), "changes": changes}))
        return 0
    return _run_inline(child_dir, _say(args))


def _cmd_world_run(args: argparse.Namespace) -> int:
    """Run or resume ``WORLD_DIR``, streaming progress JSONL on stdout (+ the world's log)."""
    from enterprise_sim.jobs.progress import JsonlSink
    from enterprise_sim.jobs.worker import run_job
    from enterprise_sim.worlds.file import is_world_dir, progress_log_path

    world_dir: Path = args.world_dir
    if not is_world_dir(world_dir):
        print(f"enterprise-sim world run: no such world: {world_dir}", file=sys.stderr)
        return 2

    sink = JsonlSink(sys.stdout, also_to=progress_log_path(world_dir))
    try:
        state = run_job(world_dir, progress=sink)
    finally:
        sink.close()
    return _report(world_dir, state, sys.stderr)


def _cmd_world_data(args: argparse.Namespace) -> int:
    """Run structured-data scenarios into a world (``<world>/data/<scenario>/``)."""
    try:
        from enterprise_sim.data_products import execute_data_run, load_data_config
    except ModuleNotFoundError as exc:
        print(
            f"enterprise-sim world data: missing dependency {exc.name!r} — install the data "
            "extra: uv sync --extra data (or pip install 'enterprise-sim[data]')",
            file=sys.stderr,
        )
        return 2
    from enterprise_sim.core.registry import RegistryError
    from enterprise_sim.data_products.config import DataConfigError
    from enterprise_sim.data_products.linking import LinkageError
    from enterprise_sim.data_products.lint import SpecLintError
    from enterprise_sim.data_products.views import MaterializeError

    try:
        config = load_data_config(args.config)
    except DataConfigError as exc:
        print(f"enterprise-sim world data: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"enterprise-sim world data: invalid config {args.config}:\n{exc}", file=sys.stderr)
        return 2

    if args.scale is not None:
        config = config.model_copy(
            update={"scale": config.scale.model_copy(update={"factor": args.scale})}
        )
    if args.iterations is not None:
        config = config.model_copy(
            update={"loop": config.loop.model_copy(update={"iterations": args.iterations})}
        )

    try:
        result = execute_data_run(config, world_dir=args.world, worlds_root=args.worlds_root)
    except (SpecLintError, LinkageError, RegistryError, MaterializeError) as exc:
        print(f"enterprise-sim world data: {exc}", file=sys.stderr)
        return 1

    for scenario in result.scenarios:
        total_rows = sum(scenario.rows_by_table.values())
        print(
            f"enterprise-sim world data: {scenario.scenario} -> {scenario.data_dir} "
            f"({total_rows:,} rows across {len(scenario.rows_by_table)} tables, "
            f"{scenario.questions_answerable}/{scenario.questions_total} questions "
            f"answerable after {scenario.iterations_run} iteration(s))"
        )
    print(f"enterprise-sim world data: recorded in {result.world_dir}")
    return 0


def _cmd_world_adopt(args: argparse.Namespace) -> int:
    """Turn a pre-.world run directory (+ its old data-run dirs) into one world."""
    from enterprise_sim.worlds.adopt import AdoptError, adopt_run_dir
    from enterprise_sim.worlds.file import read_world

    try:
        world_dir = adopt_run_dir(
            args.old_dir, worlds_root=args.worlds_root, data_dirs=list(args.data)
        )
    except AdoptError as exc:
        print(f"enterprise-sim world adopt: {exc}", file=sys.stderr)
        return 2
    world = read_world(world_dir)
    assert world is not None  # adopt_run_dir just wrote it.
    print(
        json.dumps(
            {"world_id": world.world_id, "world_dir": str(world_dir), "data": sorted(world.data)}
        )
    )
    return 0


def _cmd_world_cancel(args: argparse.Namespace) -> int:
    """Mark a world's (paused / interrupted / failed) job cancelled; the folder is kept."""
    from enterprise_sim.worlds.file import WorldFile, read_world, update_world

    world_dir: Path = args.world_dir
    world = read_world(world_dir)
    if world is None:
        print(json.dumps({"error": f"no such world: {world_dir}"}))
        return 2
    if _refuse_if_running(world_dir, world.job.status, world.job.pid, command="cancel"):
        return 2

    def mark(w: WorldFile) -> None:
        if w.job.status != "done":
            w.job.status = "cancelled"
            w.job.finished_at = datetime.now(UTC).isoformat()

    updated = update_world(world_dir, mark)
    print(json.dumps({"world_id": updated.world_id, "status": updated.job.status}))
    return 0


def _cmd_world_pause(args: argparse.Namespace) -> int:
    """Write the pause request; a running worker on this world stops cooperatively."""
    from enterprise_sim.jobs.control import RunControl

    RunControl(args.world_dir).request_pause()
    print(f"enterprise-sim world pause: requested for {args.world_dir}", file=sys.stderr)
    return 0


def _cmd_world_status(args: argparse.Namespace) -> int:
    """Print one world's summary (its ``.world`` plus liveness) as JSON."""
    from enterprise_sim.worlds.summary import summarize_world

    summary = summarize_world(args.world_dir)
    if summary is None:
        print(json.dumps({"error": f"no such world: {args.world_dir}"}))
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


def _cmd_world_list(args: argparse.Namespace) -> int:
    """Print every world under ``--worlds-root`` as ``{"worlds": [...]}``."""
    from enterprise_sim.worlds.file import discover_worlds
    from enterprise_sim.worlds.summary import summarize_world

    root: Path = args.worlds_root if args.worlds_root is not None else DEFAULT_WORLDS_ROOT
    worlds = [s for p in discover_worlds(root) if (s := summarize_world(p)) is not None]
    print(json.dumps({"worlds": worlds, "worlds_root": str(root)}, sort_keys=True))
    return 0


def _cmd_world_configure(args: argparse.Namespace) -> int:
    """Merge a JSON patch into the world's config (e.g. raise the cost ceiling), validated."""
    from enterprise_sim.assembly import canonical_config
    from enterprise_sim.core.config import WorldConfig
    from enterprise_sim.worlds.file import WorldFile, read_world, update_world

    world_dir: Path = args.world_dir
    world = read_world(world_dir)
    if world is None:
        print(f"enterprise-sim world configure: no such world: {world_dir}", file=sys.stderr)
        return 2
    if _refuse_if_running(world_dir, world.job.status, world.job.pid, command="configure"):
        return 2
    try:
        patch = json.loads(args.patch)
    except json.JSONDecodeError as exc:
        print(f"enterprise-sim world configure: invalid --patch: {exc}", file=sys.stderr)
        return 2
    if not isinstance(patch, dict):
        print("enterprise-sim world configure: --patch must be a JSON object", file=sys.stderr)
        return 2
    try:
        config = WorldConfig.model_validate(_deep_merge(dict(world.config), patch))
    except ValidationError as exc:
        print(
            f"enterprise-sim world configure: invalid config after patch:\n{exc}", file=sys.stderr
        )
        return 2

    def apply(w: WorldFile) -> None:
        w.config = canonical_config(config)

    updated = update_world(world_dir, apply)
    print(json.dumps({"world_id": updated.world_id, "config": updated.config}, sort_keys=True))
    return 0


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``patch`` into ``base`` (nested objects merge; anything else replaces)."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(dict(base[key]), value)
        else:
            base[key] = value
    return base


# -- parser -------------------------------------------------------------------


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("config", nargs="?", default=None, help="path to a world config")
    parser.add_argument(
        "-c",
        "--config",
        dest="config_opt",
        default=None,
        metavar="PATH",
        help="path to a world config (alternative to the positional argument)",
    )


def _add_build_args(parser: argparse.ArgumentParser, *, command: str) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "render against the real provider named by the config's [model] backend "
            "(anthropic_api/bedrock/claude_cli); without it the build uses the "
            "deterministic, network-free 'fake' backend. A live build costs money "
            "and is not byte-reproducible"
        ),
    )
    parser.add_argument(
        "--no-run",
        dest="no_run",
        action="store_true",
        help=f"only {command} the world (prints its id + dir as JSON); build it with `world run`",
    )


def _add_extend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--period-end",
        dest="period_end",
        default=None,
        metavar="YYYY-MM-DD",
        help="move the simulation window's end date later",
    )
    parser.add_argument(
        "--add-project",
        dest="add_project",
        action="append",
        default=[],
        metavar="JSON",
        help="a project to append, as a JSON object (repeatable)",
    )
    parser.add_argument(
        "--add-data",
        dest="add_data",
        action="append",
        default=[],
        metavar="SCENARIO",
        help="a structured-data scenario to add to the world's data plan (repeatable)",
    )


def _world_dir_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("world_dir", type=Path, metavar="WORLD_DIR", help="a world directory")


def add_world_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the ``world`` command group."""
    world_parser = subparsers.add_parser(
        "world",
        help="create, extend, fork, build, and list simulated worlds",
        description=(
            "A world is one simulated company on disk: its document corpus + gold KG "
            "and its structured data products, in one folder with a .world file "
            "(docs/EXPLORER_RUNS.md). Create one from a config, grow it in place with "
            "more time or projects, fork a parallel one, build/resume it as a "
            "resumable job with streamed progress, and list what exists."
        ),
    )
    world_subparsers = world_parser.add_subparsers(
        dest="world_command",
        required=True,
        metavar="{create,extend,fork,run,pause,cancel,status,list,data,adopt,estimate,catalog,configure}",
    )
    world_parser.set_defaults(func=_cmd_world, world_parser=world_parser)

    create_parser = world_subparsers.add_parser(
        "create", help="mint a world from a config and build it (deterministic unless --live)"
    )
    _add_config_args(create_parser)
    create_parser.add_argument(
        "--worlds-root",
        dest="worlds_root",
        type=Path,
        default=None,
        metavar="DIR",
        help="override the config's worlds_root (the world lands in DIR/<world-id>/)",
    )
    create_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="estimate artifact count + cost and exit without writing anything (D13)",
    )
    _add_build_args(create_parser, command="create")
    create_parser.add_argument(
        "--max-concurrency",
        dest="max_concurrency",
        type=int,
        default=None,
        metavar="N",
        help="override scale.max_concurrency (parallel scenario renders)",
    )
    create_parser.add_argument(
        "--cost-ceiling",
        dest="cost_ceiling",
        type=float,
        default=None,
        metavar="USD",
        help="override scale.cost_ceiling_usd (abort if the dry-run estimate exceeds it)",
    )
    create_parser.set_defaults(func=_cmd_world_create)

    extend_parser = world_subparsers.add_parser(
        "extend", help="grow a world in place: more time and/or more projects, then build"
    )
    _world_dir_arg(extend_parser)
    _add_extend_args(extend_parser)
    _add_build_args(extend_parser, command="extend")
    extend_parser.set_defaults(func=_cmd_world_extend)

    fork_parser = world_subparsers.add_parser(
        "fork", help="create a parallel world from an existing one (new seed/time/projects)"
    )
    _world_dir_arg(fork_parser)
    fork_parser.add_argument(
        "--worlds-root",
        dest="worlds_root",
        type=Path,
        default=None,
        metavar="DIR",
        help="where the fork lands (default: next to its parent)",
    )
    fork_parser.add_argument("--seed", type=int, default=None, help="a different root seed")
    _add_extend_args(fork_parser)
    _add_build_args(fork_parser, command="fork")
    fork_parser.set_defaults(func=_cmd_world_fork)

    run_parser = world_subparsers.add_parser(
        "run", help="build or resume a world; streams progress JSONL on stdout"
    )
    _world_dir_arg(run_parser)
    run_parser.set_defaults(func=_cmd_world_run)

    pause_parser = world_subparsers.add_parser("pause", help="request a cooperative pause")
    _world_dir_arg(pause_parser)
    pause_parser.set_defaults(func=_cmd_world_pause)

    cancel_parser = world_subparsers.add_parser(
        "cancel", help="mark a paused/interrupted/failed build cancelled (the world is kept)"
    )
    _world_dir_arg(cancel_parser)
    cancel_parser.set_defaults(func=_cmd_world_cancel)

    status_parser = world_subparsers.add_parser("status", help="print one world's summary JSON")
    _world_dir_arg(status_parser)
    status_parser.set_defaults(func=_cmd_world_status)

    list_parser = world_subparsers.add_parser("list", help="list every world under a root")
    list_parser.add_argument(
        "--worlds-root",
        dest="worlds_root",
        type=Path,
        default=None,
        metavar="DIR",
        help=f"where to look (default: {DEFAULT_WORLDS_ROOT}/)",
    )
    list_parser.set_defaults(func=_cmd_world_list)

    estimate_parser = world_subparsers.add_parser(
        "estimate", help="dry-run a config's artifact count + cost estimate as JSON (keyless)"
    )
    _add_config_args(estimate_parser)
    estimate_parser.add_argument(
        "--live", action="store_true", help="price against the config's configured backend"
    )
    estimate_parser.set_defaults(func=_cmd_world_estimate)

    catalog_parser = world_subparsers.add_parser(
        "catalog", help="print the catalog JSON (archetypes/playbooks/producers/backends/models/…)"
    )
    catalog_parser.set_defaults(func=_cmd_world_catalog)

    data_parser = world_subparsers.add_parser(
        "data", help="run structured-data scenarios into a world (<world>/data/<scenario>/)"
    )
    data_parser.add_argument("config", help="path to a data config (.toml or .json)")
    data_parser.add_argument(
        "--world",
        dest="world",
        type=Path,
        default=None,
        metavar="WORLD_DIR",
        help="the world to attach the data to (overrides the config's [world] world_dir)",
    )
    data_parser.add_argument(
        "--worlds-root",
        dest="worlds_root",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "where a world built from the config's [world.company] lands "
            f"(default: {DEFAULT_WORLDS_ROOT}/)"
        ),
    )
    data_parser.add_argument(
        "--scale",
        dest="scale",
        type=float,
        default=None,
        metavar="X",
        help="override scale.factor (multiplies every population's base size)",
    )
    data_parser.add_argument(
        "--iterations",
        dest="iterations",
        type=int,
        default=None,
        metavar="N",
        help="override loop.iterations (question-loop passes, 1-5)",
    )
    data_parser.set_defaults(func=_cmd_world_data)

    adopt_parser = world_subparsers.add_parser(
        "adopt", help="migrate a pre-.world run dir (and its old data-run dirs) into one world"
    )
    adopt_parser.add_argument(
        "old_dir", type=Path, metavar="OLD_RUN_DIR", help="a run dir holding manifest.json"
    )
    adopt_parser.add_argument(
        "--data",
        action="append",
        default=[],
        type=Path,
        metavar="OLD_DATA_RUN_DIR",
        help="an old standalone data-run dir to fold into the world's data/ (repeatable)",
    )
    adopt_parser.add_argument(
        "--worlds-root",
        dest="worlds_root",
        type=Path,
        default=None,
        metavar="DIR",
        help="move the folder to DIR/<world-id>/ (default: adopt it where it is)",
    )
    adopt_parser.set_defaults(func=_cmd_world_adopt)

    configure_parser = world_subparsers.add_parser(
        "configure", help="merge a JSON patch into a world's config (e.g. raise the cost ceiling)"
    )
    _world_dir_arg(configure_parser)
    configure_parser.add_argument(
        "--patch", required=True, metavar="JSON", help='e.g. \'{"scale": {"cost_ceiling_usd": 5}}\''
    )
    configure_parser.set_defaults(func=_cmd_world_configure)

    from enterprise_sim.worlds.export_cli import add_export_parser

    add_export_parser(world_subparsers)

    from enterprise_sim.worlds.profile import add_profile_parser

    add_profile_parser(world_subparsers)

    from enterprise_sim.worlds.slice import add_slice_parser

    add_slice_parser(world_subparsers)

    from enterprise_sim.worlds.heldout import add_heldout_parser

    add_heldout_parser(world_subparsers)

    from enterprise_sim.worlds.retrieval_fixture import add_retrieval_fixture_parser

    add_retrieval_fixture_parser(world_subparsers)

    from enterprise_sim.worlds.ladder import add_ladder_parser

    add_ladder_parser(world_subparsers)

    from enterprise_sim.worlds.knowledge import add_knowledge_parser

    add_knowledge_parser(world_subparsers)
