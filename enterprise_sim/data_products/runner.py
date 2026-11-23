"""Orchestrate a data-products run into a world: author → lint → sample → views → loop.

The structured-data analog of ``assembly/runner.py``, and the other half of the
same world: the tables land under ``<world>/data/<scenario>/`` next to the
world's document corpus and gold KG, and every scenario is recorded in the
world's ``.world`` (``data[<scenario>]`` + a ``data`` history entry). For each
configured scenario:

1. **World** — resolve the world: an existing world directory (its exported KG
   is loaded for linking), or an inline company, for which a new world is
   built deterministically first (``execute_run`` on the ``fake`` backend) so
   there is one folder per company either way.
2. **Author** — produce the scenario's causal spec (template plugin, or
   LLM-elaborated per the authoring mode) and lint it.
3. **Loop** (up to ``loop.iterations`` times): sample every table to
   partitioned parquet, materialize the SQL views, evaluate every business
   question's SQL against the data, and — when questions fail — apply their
   gap fixes (template) and/or LLM-proposed deltas to the spec, then resample.
   The loop exits early once everything answerable is answered or no fix
   exists.
4. **Assemble** — write ``spec.json`` (the causal ground truth), per-iteration
   spec snapshots, ``lineage.json`` (parquet values ↔ KG entity ids) and
   ``questions.json`` (categories, SQL, answerability history) into the
   scenario's folder, and its summary into the ``.world``.

Re-running the same config into the same world reproduces the same folders,
and the ``fake``-backend path is fully deterministic end to end.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from enterprise_sim import __version__
from enterprise_sim.core.config import WorldConfig
from enterprise_sim.core.config.models import SimulationConfig
from enterprise_sim.core.config.seed import SeedContext
from enterprise_sim.core.llm import LLMClient, LLMConfig, build_client
from enterprise_sim.core.world import World
from enterprise_sim.data_products import authoring
from enterprise_sim.data_products import questions as questions_mod
from enterprise_sim.data_products.config import AuthoringMode, DataRunConfig
from enterprise_sim.data_products.linking import LinkageError, load_world_kg
from enterprise_sim.data_products.lint import projected_rows, require_clean
from enterprise_sim.data_products.messiness import apply_data_messiness
from enterprise_sim.data_products.questions import QuestionState
from enterprise_sim.data_products.sampler import sample_scenario
from enterprise_sim.data_products.scenarios import DATA_SCENARIOS, discover_scenarios
from enterprise_sim.data_products.spec import GapFix, ScenarioSpec, apply_deltas
from enterprise_sim.data_products.views import (
    MaterializeError,
    materialize_views,
    validate_view_sql,
)
from enterprise_sim.worlds import WorldFile, cache_dir, is_world_dir, update_world

__all__ = ["DATA_DIR", "DataRunResult", "ScenarioRunResult", "execute_data_run", "scenario_dir"]

#: The subdirectory of a world its structured data products live in.
DATA_DIR = "data"

# Bumped when the per-scenario summary recorded in ``.world`` changes incompatibly.
DATA_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    """Where one scenario's data product landed and its headline stats."""

    scenario: str
    data_dir: Path
    spec: ScenarioSpec | None  # None for a scripted scenario (data_products.scripted)
    iterations_run: int
    rows_by_table: dict[str, int]
    questions_total: int
    questions_answerable: int


@dataclass(frozen=True, slots=True)
class DataRunResult:
    """The outcome of :func:`execute_data_run` across all scenarios."""

    world_dir: Path
    scenarios: tuple[ScenarioRunResult, ...]
    world_nodes: int


def scenario_dir(world_dir: Path | str, scenario: str) -> Path:
    """``<world>/data/<scenario>/`` — where that scenario's tables, views and spec live."""
    return Path(world_dir) / DATA_DIR / scenario


def _resolve_world_dir(
    config: DataRunConfig, *, world_dir: Path | None, worlds_root: Path | None
) -> Path:
    """The world the data belongs to — an existing one, or one built from the inline company.

    ``world_dir`` (the CLI's ``--world``) wins over the config's ``world.world_dir``.
    With neither, the config's ``world.company`` is built into a fresh world under
    ``worlds_root`` (default ``worlds/``) on the deterministic ``fake`` backend —
    or reused if that world already exists — so a keyless data run still yields
    one complete world folder.
    """
    from enterprise_sim.assembly import compute_world_id, execute_run
    from enterprise_sim.worlds.cli import DEFAULT_WORLDS_ROOT

    target = world_dir if world_dir is not None else config.world.world_dir
    if target is not None:
        target = Path(target)
        if not is_world_dir(target):
            raise LinkageError(f"{target} is not a world directory (no .world file)")
        return target
    assert config.world.company is not None  # the config validator guarantees one of the two.
    run_config = WorldConfig(
        company=config.world.company,
        simulation=SimulationConfig(period_start=config.window.start, period_end=config.window.end),
        seed=config.seed,
        worlds_root=Path(worlds_root) if worlds_root is not None else DEFAULT_WORLDS_ROOT,
    )
    target = run_config.worlds_root / compute_world_id(run_config)
    if not is_world_dir(target):
        execute_run(run_config, world_dir=target)
    return target


def _build_llm_client(config: DataRunConfig, world_dir: Path) -> LLMClient | None:
    """The authoring client (``llm`` mode only), cached in the world like the corpus render."""
    if config.authoring.mode is not AuthoringMode.LLM:
        return None
    backend = config.model.backend
    llm_config = LLMConfig(
        backend=backend.value,
        model=config.model.name,
        cost_ceiling_usd=config.model.cost_ceiling_usd,
        cache_dir=config.model.cache_dir or str(cache_dir(world_dir)),
    )
    return build_client(llm_config)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _clear_tables_and_views(data_dir: Path) -> None:
    """Wipe the sampled output (``tables/`` + ``views/``) but keep the spec snapshots."""
    for name in ("tables", "views"):
        sub = data_dir / name
        if sub.exists():
            shutil.rmtree(sub)
    data_dir.mkdir(parents=True, exist_ok=True)


def _run_scenario(
    name: str,
    config: DataRunConfig,
    world_dir: Path,
    world: World,
    client: LLMClient | None,
) -> tuple[ScenarioRunResult, dict[str, Any]]:
    """Run one scenario into ``<world>/data/<name>/``; returns the result + its summary."""
    plugin = DATA_SCENARIOS.get(name)
    data_dir = scenario_dir(world_dir, name)
    data_dir.mkdir(parents=True, exist_ok=True)
    seeds = SeedContext(config.seed).child("data", name)

    spec, author_report = authoring.author_spec(
        plugin,
        start=config.window.start,
        end=config.window.end,
        mode=config.authoring.mode.value,
        client=client,
        world=world,
        extra_brief=config.briefs.get(name),
        max_repair_attempts=config.authoring.max_repair_attempts,
        scale=config.scale.factor,
    )

    question_reports: list[dict[str, Any]] = [author_report.to_dict()]
    if client is not None:
        extra, question_report = authoring.generate_questions(
            spec,
            client=client,
            existing_ids=[q.id for q in spec.questions],
            max_repair_attempts=config.authoring.max_repair_attempts,
        )
        question_reports.append(question_report.to_dict())
        if extra:
            spec = spec.model_copy(update={"questions": (*spec.questions, *extra)})

    states = [QuestionState(question) for question in spec.questions]
    iteration_stats: list[dict[str, Any]] = []
    max_iterations = config.loop.iterations
    iterations_run = 0
    report = None
    llm_fixes: dict[str, GapFix] = {}

    for iteration in range(1, max_iterations + 1):
        iterations_run = iteration
        _write_json(
            data_dir / f"spec.iteration-{iteration}.json",
            spec.model_dump(mode="json", exclude_none=True),
        )
        _clear_tables_and_views(data_dir)
        report = sample_scenario(
            spec,
            data_dir,
            seeds=seeds,
            world=world,
            scale=config.scale.factor,
            rows_per_partition=config.scale.rows_per_partition,
        )
        view_results = materialize_views(spec, data_dir)
        # Evaluate EVERY question each iteration: the data is wiped and
        # resampled per pass (and a patched spec shifts the panel RNG stream),
        # so a previously-passing answer must be re-proven against the data
        # actually shipped.
        questions_mod.evaluate_questions(
            spec, data_dir, states, iteration=iteration, only_unanswered=False
        )

        unanswered = [state for state in states if not state.answerable]
        iteration_stats.append(
            {
                "iteration": iteration,
                "rows_by_table": dict(report.rows_by_table),
                "view_rows": {result.name: result.rows for result in view_results},
                "questions_unanswered": [state.question.id for state in unanswered],
            }
        )
        if not unanswered or iteration == max_iterations:
            break

        # Gap analysis: template fixes ride on the questions; llm mode patches
        # the questions that have none.
        if client is not None:
            missing_fix = [
                (state.question, state.attempts[-1].error or "unanswerable")
                for state in unanswered
                if state.question.gap is None and state.question.id not in llm_fixes
            ]
            if missing_fix:
                proposed, gap_report = authoring.propose_gap_fixes(
                    spec,
                    missing_fix,
                    client=client,
                    max_repair_attempts=config.authoring.max_repair_attempts,
                )
                question_reports.append(gap_report.to_dict())
                llm_fixes.update(proposed)

        # Template gap fixes are pre-tested with their scenarios: a failure
        # there is a real bug worth stopping on. LLM-proposed fixes are applied
        # one at a time so a single bad proposal (typo'd target, invalid view
        # SQL) is dropped instead of abandoning the whole patch round.
        template_deltas = questions_mod.collect_gap_deltas(states)
        patched = spec
        try:
            if template_deltas:
                patched = apply_deltas(patched, template_deltas)
            require_clean(patched, scale=config.scale.factor)
            _require_valid_views(patched)
        except Exception as exc:
            iteration_stats[-1]["patch_error"] = str(exc)[:2000]
            break
        for state in unanswered:
            fix = llm_fixes.get(state.question.id)
            if fix is None:
                continue
            try:
                candidate = apply_deltas(patched, fix.deltas)
                require_clean(candidate, scale=config.scale.factor)
                _require_valid_views(candidate)
            except Exception as exc:
                iteration_stats[-1].setdefault("dropped_fixes", {})[state.question.id] = str(exc)[
                    :500
                ]
                continue
            patched = candidate
        if patched == spec:
            # No fix changed anything — resampling identical data is pointless.
            break
        spec = patched
        states = _carry_states(states, spec)

    assert report is not None
    _write_json(data_dir / "spec.json", spec.model_dump(mode="json", exclude_none=True))
    _write_json(
        data_dir / "lineage.json",
        {
            "scenario": spec.name,
            "identities": report.identity_lineage,
            "dimensions": report.dimension_lineage,
        },
    )
    _write_json(
        data_dir / "questions.json",
        questions_mod.questions_payload(spec, states, iterations_run=iterations_run),
    )

    quirk_counts = apply_data_messiness(
        spec.model_dump(mode="json"), data_dir, config.messiness, seed=config.seed
    )

    answerable = sum(1 for state in states if state.answerable)
    summary = {
        "schema_version": DATA_SCHEMA_VERSION,
        "generator": {"name": "enterprise-sim", "version": __version__},
        "created_at": datetime.now(UTC).isoformat(),
        "scenario": spec.name,
        "title": spec.title,
        "seed": config.seed,
        "window": {
            "start": config.window.start.isoformat(),
            "end": config.window.end.isoformat(),
        },
        "dir": f"{DATA_DIR}/{name}/",
        "authoring": question_reports,
        "projected_rows": projected_rows(
            spec, scale=config.scale.factor, sizes=report.population_sizes
        ),
        "population_sizes": report.population_sizes,
        "rows_by_table": dict(report.rows_by_table),
        "iterations": iteration_stats,
        "questions": {"total": len(states), "answerable": answerable},
        "quirks": quirk_counts,
        "llm_cost_usd": (round(client.cost.total_cost_usd, 6) if client is not None else 0.0),
    }

    result = ScenarioRunResult(
        scenario=spec.name,
        data_dir=data_dir,
        spec=spec,
        iterations_run=iterations_run,
        rows_by_table=dict(report.rows_by_table),
        questions_total=len(states),
        questions_answerable=answerable,
    )
    return result, summary


def _run_scripted_scenario(
    name: str, config: DataRunConfig, world_dir: Path, world: World
) -> tuple[ScenarioRunResult, dict[str, Any]]:
    """Run one scripted scenario (``data_products.scripted``) into ``<world>/data/<name>/``."""
    from enterprise_sim.data_products.scripted import SCRIPTED_SCENARIOS, run_scripted

    plugin = SCRIPTED_SCENARIOS.get(name)
    data_dir = scenario_dir(world_dir, name)
    data_dir.mkdir(parents=True, exist_ok=True)
    rows, total, answerable, _ = run_scripted(
        plugin,
        world,
        data_dir,
        seed=config.seed,
        start=config.window.start,
        end=config.window.end,
        messiness=config.messiness,
    )
    summary = {
        "schema_version": DATA_SCHEMA_VERSION,
        "generator": {"name": "enterprise-sim", "version": __version__},
        "created_at": datetime.now(UTC).isoformat(),
        "scenario": name,
        "title": plugin.title,
        "scripted": True,
        "seed": config.seed,
        "window": {
            "start": config.window.start.isoformat(),
            "end": config.window.end.isoformat(),
        },
        "dir": f"{DATA_DIR}/{name}/",
        "rows_by_table": dict(rows),
        "questions": {"total": total, "answerable": answerable},
        "llm_cost_usd": 0.0,
    }
    result = ScenarioRunResult(
        scenario=name,
        data_dir=data_dir,
        spec=None,
        iterations_run=1,
        rows_by_table=dict(rows),
        questions_total=total,
        questions_answerable=answerable,
    )
    return result, summary


def _carry_states(states: list[QuestionState], spec: ScenarioSpec) -> list[QuestionState]:
    """Re-anchor question states on the patched spec (ids are stable)."""
    by_id = {question.id: question for question in spec.questions}
    carried: list[QuestionState] = []
    for state in states:
        question = by_id.get(state.question.id, state.question)
        carried.append(QuestionState(question=question, attempts=state.attempts))
    return carried


def _require_valid_views(spec: ScenarioSpec) -> None:
    """Raise if any view's SQL fails to plan against the spec's schema."""
    errors = validate_view_sql(spec)
    if errors:
        raise MaterializeError("; ".join(errors))


def execute_data_run(
    config: DataRunConfig,
    *,
    world_dir: Path | None = None,
    worlds_root: Path | None = None,
    on_scenario: Callable[[str, int, int], None] | None = None,
) -> DataRunResult:
    """Run every configured scenario into the world; returns per-scenario results.

    ``world_dir`` overrides the config's ``world.world_dir``; ``worlds_root`` is
    where a world built from an inline ``world.company`` lands (see
    :func:`_resolve_world_dir`). Each scenario's summary is recorded in the
    world's ``.world`` under ``data``, and the run itself as a ``data`` history
    entry carrying the data config. ``on_scenario(name, index, total)`` is
    called as each scenario starts (a world build reports it as progress).

    Data scenarios authored as templates (``<templates>/<slug>/plugin.py``
    registering into :data:`DATA_SCENARIOS`) are discovered from the plugin
    paths (``ENTERPRISE_SIM_PLUGIN_PATH``) exactly like archetypes/playbooks.
    """
    from enterprise_sim.core.registry import discover_paths, plugin_paths

    discover_paths(plugin_paths())
    registry = discover_scenarios()
    from enterprise_sim.data_products.scripted import SCRIPTED_SCENARIOS, load_builtin

    load_builtin()
    scripted = set(SCRIPTED_SCENARIOS.names())
    both = sorted(scripted & set(registry.names()))
    if both:
        raise ValueError(f"data scenario names registered twice (DSL and scripted): {both}")
    unknown = [name for name in config.scenarios if name not in set(registry.names()) | scripted]
    if unknown:
        # Fail before any world-build or sampling cost, with the registry's
        # clean known-names message.
        registry.get(unknown[0])
    target = _resolve_world_dir(config, world_dir=world_dir, worlds_root=worlds_root)
    world = load_world_kg(target)
    client = _build_llm_client(config, target)

    results: list[ScenarioRunResult] = []
    summaries: dict[str, dict[str, Any]] = {}
    for index, name in enumerate(config.scenarios):
        if on_scenario is not None:
            on_scenario(name, index, len(config.scenarios))
        if name in scripted:
            result, summary = _run_scripted_scenario(name, config, target, world)
        else:
            result, summary = _run_scenario(name, config, target, world, client)
        results.append(result)
        summaries[name] = summary

    now = datetime.now(UTC).isoformat()
    recorded_config = config.model_dump(mode="json")
    recorded_config["world"] = {"world_dir": None, "company": recorded_config["world"]["company"]}

    def record(w: WorldFile) -> None:
        w.data.update(summaries)
        w.history.append(
            {
                "kind": "data",
                "at": now,
                "scenarios": list(config.scenarios),
                "config": recorded_config,
            }
        )
        w.outputs = {**w.outputs, "data": f"{DATA_DIR}/"}

    update_world(target, record)
    return DataRunResult(world_dir=target, scenarios=tuple(results), world_nodes=world.node_count)


# ``json`` is only needed by the small writers above; imported last to keep the
# dependency block above focused on the pipeline.
import json  # noqa: E402
