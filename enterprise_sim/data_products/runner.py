"""Orchestrate a data-products run: author → lint → sample → views → loop.

The structured-data analog of ``assembly/runner.py``. For each configured
scenario:

1. **World** — load the linked enterprise KG (a completed run's export, or an
   inline company built deterministically by Layer A).
2. **Author** — produce the scenario's causal spec (template plugin, or
   LLM-elaborated per the authoring mode) and lint it.
3. **Loop** (up to ``loop.iterations`` times): sample every table to
   partitioned parquet, materialize the SQL views, evaluate every business
   question's SQL against the data, and — when questions fail — apply their
   gap fixes (template) and/or LLM-proposed deltas to the spec, then resample.
   The loop exits early once everything answerable is answered or no fix
   exists.
4. **Assemble** — write ``spec.json`` (the causal ground truth), per-iteration
   spec snapshots, ``lineage.json`` (parquet values ↔ KG entity ids),
   ``questions.json`` (categories, SQL, answerability history), and
   ``manifest.json``.

The run id is a content digest of ``(config, scenario)`` — re-running the same
config reproduces the same directory, and the ``fake``-backend path is fully
deterministic end to end.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from enterprise_sim import __version__
from enterprise_sim.core.config import RunConfig
from enterprise_sim.core.config.models import SimulationConfig
from enterprise_sim.core.config.seed import SeedContext
from enterprise_sim.core.llm import LLMClient, LLMConfig, build_client
from enterprise_sim.core.world import World
from enterprise_sim.data_products import authoring
from enterprise_sim.data_products import questions as questions_mod
from enterprise_sim.data_products.config import AuthoringMode, DataRunConfig
from enterprise_sim.data_products.linking import load_world_from_run
from enterprise_sim.data_products.lint import projected_rows, require_clean
from enterprise_sim.data_products.questions import QuestionState
from enterprise_sim.data_products.sampler import sample_scenario
from enterprise_sim.data_products.scenarios import DATA_SCENARIOS, discover_scenarios
from enterprise_sim.data_products.spec import GapFix, ScenarioSpec, apply_deltas
from enterprise_sim.data_products.views import materialize_views
from enterprise_sim.world_builders import build_world

__all__ = ["DataRunResult", "ScenarioRunResult", "execute_data_run"]

MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    """Where one scenario's data product landed and its headline stats."""

    scenario: str
    run_dir: Path
    spec: ScenarioSpec
    iterations_run: int
    rows_by_table: dict[str, int]
    questions_total: int
    questions_answerable: int


@dataclass(frozen=True, slots=True)
class DataRunResult:
    """The outcome of :func:`execute_data_run` across all scenarios."""

    scenarios: tuple[ScenarioRunResult, ...]
    world_nodes: int


def _run_id(config: DataRunConfig, scenario: str) -> str:
    payload = config.model_dump(mode="json", exclude={"output_dir"})
    digest = hashlib.blake2b(
        json.dumps({"config": payload, "scenario": scenario}, sort_keys=True).encode("utf-8"),
        digest_size=6,
    ).hexdigest()
    return f"{scenario.replace('_', '-')}-{digest}"


def _resolve_world(config: DataRunConfig) -> World:
    if config.world.run_dir is not None:
        return load_world_from_run(config.world.run_dir)
    assert config.world.company is not None
    run_config = RunConfig(
        company=config.world.company,
        simulation=SimulationConfig(period_start=config.window.start, period_end=config.window.end),
        seed=config.seed,
    )
    return build_world(run_config)


def _build_llm_client(config: DataRunConfig) -> LLMClient | None:
    if config.authoring.mode is not AuthoringMode.LLM:
        return None
    backend = config.model.backend
    llm_config = LLMConfig(
        backend=backend.value,
        model=config.model.name,
        cost_ceiling_usd=config.model.cost_ceiling_usd,
        cache_dir=config.model.cache_dir,
    )
    return build_client(llm_config)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _clear_data_dir(data_dir: Path) -> None:
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)


def _run_scenario(
    name: str,
    config: DataRunConfig,
    world: World,
    client: LLMClient | None,
) -> ScenarioRunResult:
    plugin = DATA_SCENARIOS.get(name)
    run_dir = Path(config.output_dir) / _run_id(config, name)
    run_dir.mkdir(parents=True, exist_ok=True)
    data_dir = run_dir / "data"
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
            run_dir / f"spec.iteration-{iteration}.json",
            spec.model_dump(mode="json", exclude_none=True),
        )
        _clear_data_dir(data_dir)
        report = sample_scenario(
            spec,
            data_dir,
            seeds=seeds,
            world=world,
            scale=config.scale.factor,
            rows_per_partition=config.scale.rows_per_partition,
        )
        view_results = materialize_views(spec, data_dir)
        questions_mod.evaluate_questions(spec, data_dir, states, iteration=iteration)

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

        deltas = list(questions_mod.collect_gap_deltas(states))
        for state in unanswered:
            fix = llm_fixes.get(state.question.id)
            if fix is not None:
                deltas.extend(fix.deltas)
        if not deltas:
            break
        try:
            patched = apply_deltas(spec, tuple(deltas))
            require_clean(patched, scale=config.scale.factor)
        except Exception as exc:  # lint or validation failure: keep the old spec
            iteration_stats[-1]["patch_error"] = str(exc)[:2000]
            break
        spec = patched
        states = _carry_states(states, spec)

    assert report is not None
    _write_json(run_dir / "spec.json", spec.model_dump(mode="json", exclude_none=True))
    _write_json(
        run_dir / "lineage.json",
        {
            "scenario": spec.name,
            "identities": report.identity_lineage,
            "dimensions": report.dimension_lineage,
        },
    )
    _write_json(
        run_dir / "questions.json",
        questions_mod.questions_payload(spec, states, iterations_run=iterations_run),
    )

    answerable = sum(1 for state in states if state.answerable)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator": {"name": "enterprise-sim", "version": __version__},
        "created_at": datetime.now(UTC).isoformat(),
        "scenario": spec.name,
        "title": spec.title,
        "seed": config.seed,
        "window": {
            "start": config.window.start.isoformat(),
            "end": config.window.end.isoformat(),
        },
        "config": config.model_dump(mode="json"),
        "authoring": question_reports,
        "projected_rows": projected_rows(spec, scale=config.scale.factor),
        "population_sizes": report.population_sizes,
        "iterations": iteration_stats,
        "questions": {"total": len(states), "answerable": answerable},
        "llm_cost_usd": (round(client.cost.total_cost_usd, 6) if client is not None else 0.0),
    }
    _write_json(run_dir / "manifest.json", manifest)

    return ScenarioRunResult(
        scenario=spec.name,
        run_dir=run_dir,
        spec=spec,
        iterations_run=iterations_run,
        rows_by_table=dict(report.rows_by_table),
        questions_total=len(states),
        questions_answerable=answerable,
    )


def _carry_states(states: list[QuestionState], spec: ScenarioSpec) -> list[QuestionState]:
    """Re-anchor question states on the patched spec (ids are stable)."""
    by_id = {question.id: question for question in spec.questions}
    carried: list[QuestionState] = []
    for state in states:
        question = by_id.get(state.question.id, state.question)
        carried.append(QuestionState(question=question, attempts=state.attempts))
    return carried


def execute_data_run(config: DataRunConfig) -> DataRunResult:
    """Run every configured scenario; returns per-scenario results."""
    discover_scenarios()
    world = _resolve_world(config)
    client = _build_llm_client(config)
    results = tuple(_run_scenario(name, config, world, client) for name in config.scenarios)
    return DataRunResult(scenarios=results, world_nodes=world.node_count)
