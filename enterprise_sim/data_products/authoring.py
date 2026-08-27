"""LLM-driven scenario authoring: brief → validated causal spec (and questions).

Follows the document pipeline's generation contract (§16): all calls go through
the one ``LLMClient`` interface with layered cache-aware prompts and
``generate_structured`` forcing schema-shaped output; every proposal is
validated (Pydantic) and linted before it is trusted, with a bounded repair
loop that feeds the errors back; and the deterministic scenario **template is
the always-available fallback**, so a run completes on any backend — on
``fake`` the LLM path degrades gracefully to the template and the run stays
keyless-green.

Three authoring surfaces:

* :func:`author_spec` — produce the scenario's causal spec (template, or LLM
  elaboration of the plugin's brief + the simulated company's KG context).
* :func:`generate_questions` — LLM-proposed extra business questions with SQL.
* :func:`propose_gap_fixes` — LLM gap analysis for questions the data cannot
  answer yet, returning validated :class:`SpecDelta` patches.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enterprise_sim.core.llm import LLMClient
from enterprise_sim.core.llm.prompt import assemble_prompt
from enterprise_sim.core.llm.types import LLMError
from enterprise_sim.core.world import World
from enterprise_sim.data_products.lint import SpecLintError, require_clean
from enterprise_sim.data_products.scenarios import DataScenario
from enterprise_sim.data_products.spec import (
    GapFix,
    QuestionSpec,
    ScenarioSpec,
)
from enterprise_sim.data_products.views import validate_view_sql

__all__ = [
    "AuthoringReport",
    "author_spec",
    "generate_questions",
    "propose_gap_fixes",
    "world_context",
]

_SYSTEM_SPEC = """\
You are a data architect designing a synthetic-but-realistic analytics dataset
for a simulated enterprise. You output a causal scenario spec: entity
populations with categorical and numerical variables, exogenous distributions,
structural equations (linear predictors with transforms, links, interactions,
and noise) wiring dozens of factors into output metrics, global daily factors
with trend/seasonality/shocks, physical tables, and DuckDB SQL views.
Ground every dimension you can in the provided company context. Return ONLY
data conforming to the provided JSON schema."""

_SYSTEM_QUESTIONS = """\
You are a business analyst. Given a causal scenario spec describing tables and
views (DuckDB SQL over parquet), propose realistic business questions grouped
into the scenario's categories, each with the single DuckDB SQL query that
answers it using only the scenario's tables and views. Prefer questions an
executive or PM would actually ask. Return ONLY data conforming to the schema."""

_SYSTEM_GAPS = """\
You are a data architect closing gaps in an analytics dataset. For each failing
business question, decide how the scenario spec must change so its SQL can be
answered: add attributes, panel variables, KG dimensions, factors, tables,
table columns, or views. Propose the smallest additive change that makes the
question answerable. Return ONLY data conforming to the schema."""


@dataclass(slots=True)
class AuthoringReport:
    """How a spec (or question/gap batch) was produced, for the manifest."""

    mode: str
    attempts: int = 0
    fell_back: bool = False
    errors: list[str] = dataclass_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "attempts": self.attempts,
            "fell_back": self.fell_back,
            "errors": list(self.errors),
        }


def world_context(world: World, *, max_names: int = 12) -> str:
    """A compact textual summary of the simulated company for prompt grounding.

    Stable (sorted) so it forms a cacheable prompt layer shared by every
    authoring call of the run.
    """
    lines: list[str] = ["Simulated company knowledge graph:"]
    for node_type in world.node_types():
        nodes = world.nodes_by_type(node_type)
        names = [str(node.props.get("name", node.id)) for node in nodes[:max_names]]
        suffix = " …" if len(nodes) > max_names else ""
        lines.append(f"- {node_type} ({len(nodes)}): {', '.join(names)}{suffix}")
    return "\n".join(lines)


def _repair_note(errors: Sequence[str]) -> str:
    joined = "\n".join(f"- {error}" for error in errors)
    return (
        "Your previous output failed validation. Fix these problems and return "
        f"the complete corrected object:\n{joined}"
    )


def author_spec(
    scenario: DataScenario,
    *,
    start: date,
    end: date,
    mode: str,
    client: LLMClient | None,
    world: World | None,
    extra_brief: str | None = None,
    max_repair_attempts: int = 2,
    scale: float = 1.0,
) -> tuple[ScenarioSpec, AuthoringReport]:
    """Produce the scenario's validated spec per the authoring mode.

    ``template`` mode (or a missing client) returns the plugin's deterministic
    spec. ``llm`` mode asks the backend to elaborate the plugin's brief into a
    full spec; validation or lint failures are fed back up to
    ``max_repair_attempts`` times, after which the template is the fallback.
    """
    template = scenario.build_spec(start=start, end=end)
    require_clean(template, scale=scale)
    if mode != "llm" or client is None:
        return template, AuthoringReport(mode="template")

    report = AuthoringReport(mode="llm")
    schema = ScenarioSpec.model_json_schema()
    context_blocks = [world_context(world)] if world is not None else []
    task = (
        f"Author the complete causal scenario spec for {scenario.title!r} "
        f"(name must be {scenario.name!r}) over the window {start.isoformat()} "
        f"to {end.isoformat()}.\n\nGuidance:\n{scenario.brief}"
    )
    if extra_brief:
        task += f"\n\nAdditional user guidance:\n{extra_brief}"
    task += (
        "\n\nA valid reference spec for this scenario follows; keep its spirit "
        "but enrich or restructure it per the guidance:\n"
        + template.model_dump_json(exclude_none=True)
    )

    errors: list[str] = []
    for _attempt in range(max_repair_attempts + 1):
        report.attempts += 1
        brief = task if not errors else task + "\n\n" + _repair_note(errors)
        prompt = assemble_prompt(
            system=_SYSTEM_SPEC,
            stable_context=context_blocks,
            brief=brief,
            labels=["company_kg"],
        )
        try:
            result = client.generate_structured(prompt, schema)
            spec = ScenarioSpec.model_validate(result.data)
            require_clean(spec, scale=scale)
            sql_errors = validate_view_sql(spec)
            if sql_errors:
                raise SpecAuthoringError("; ".join(sql_errors))
        except (ValidationError, SpecLintError, SpecAuthoringError, LLMError) as exc:
            errors = [str(exc)[:2000]]
            report.errors.append(errors[0])
            continue
        if spec.start != start or spec.end != end or spec.name != scenario.name:
            errors = [
                f"spec must keep name={scenario.name!r}, start={start.isoformat()}, "
                f"end={end.isoformat()}"
            ]
            report.errors.append(errors[0])
            continue
        return spec, report

    report.fell_back = True
    return template, report


class SpecAuthoringError(Exception):
    """An authored spec failed a dynamic check (e.g. view SQL does not plan)."""


class _QuestionBatch(BaseModel):
    """Schema for LLM-proposed questions."""

    model_config = ConfigDict(extra="forbid")

    questions: tuple[QuestionSpec, ...] = Field(min_length=1)


def generate_questions(
    spec: ScenarioSpec,
    *,
    client: LLMClient,
    existing_ids: Sequence[str],
    max_repair_attempts: int = 2,
) -> tuple[tuple[QuestionSpec, ...], AuthoringReport]:
    """Ask the backend for additional business questions with answering SQL.

    Returns only questions whose ids don't collide with ``existing_ids``.
    Failure to produce a valid batch returns an empty tuple (the template
    question bank already covers the loop) rather than failing the run.
    """
    report = AuthoringReport(mode="llm")
    schema = _QuestionBatch.model_json_schema()
    spec_context = spec.model_dump_json(
        exclude_none=True, include={"name", "title", "description", "question_categories"}
    )
    tables_context = _tables_context(spec)
    task = (
        "Propose 5-15 new business questions for this scenario, spread across "
        "its categories, each with one DuckDB SQL query over the tables/views "
        "described. Use question ids prefixed 'llm.'."
    )
    errors: list[str] = []
    for _attempt in range(max_repair_attempts + 1):
        report.attempts += 1
        brief = task if not errors else task + "\n\n" + _repair_note(errors)
        prompt = assemble_prompt(
            system=_SYSTEM_QUESTIONS,
            stable_context=[spec_context, tables_context],
            brief=brief,
            labels=["scenario", "tables"],
        )
        try:
            result = client.generate_structured(prompt, schema)
            batch = _QuestionBatch.model_validate(result.data)
        except (ValidationError, LLMError) as exc:
            errors = [str(exc)[:2000]]
            report.errors.append(errors[0])
            continue
        taken = set(existing_ids)
        fresh: list[QuestionSpec] = []
        for question in batch.questions:
            # Skip collisions with existing ids AND within this batch — a
            # duplicate id would poison the spec's lint at the next gap patch.
            if question.id not in taken:
                taken.add(question.id)
                fresh.append(question)
        return tuple(fresh), report

    report.fell_back = True
    return (), report


class _GapProposal(BaseModel):
    """One failing question's proposed fix."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    fix: GapFix


class _GapBatch(BaseModel):
    """Schema for LLM gap analysis output."""

    model_config = ConfigDict(extra="forbid")

    proposals: tuple[_GapProposal, ...] = ()


def propose_gap_fixes(
    spec: ScenarioSpec,
    failing: Sequence[tuple[QuestionSpec, str]],
    *,
    client: LLMClient,
    max_repair_attempts: int = 2,
) -> tuple[dict[str, GapFix], AuthoringReport]:
    """LLM gap analysis: how must the spec change to answer each failing question?

    ``failing`` pairs each question with its evaluation error. Returns a map of
    question id → proposed fix; proposals whose deltas don't validate are
    dropped. An empty map on total failure — the loop simply can't close those
    gaps this iteration.
    """
    report = AuthoringReport(mode="llm")
    schema = _GapBatch.model_json_schema()
    failures = "\n".join(
        f"- {question.id}: {question.question}\n  SQL: {question.sql}\n  error: {error}"
        for question, error in failing
    )
    task = (
        "These questions cannot be answered by the current data. For each one "
        "you can fix, propose the smallest additive spec change (deltas) that "
        f"makes its SQL answerable:\n{failures}"
    )
    errors: list[str] = []
    for _attempt in range(max_repair_attempts + 1):
        report.attempts += 1
        brief = task if not errors else task + "\n\n" + _repair_note(errors)
        prompt = assemble_prompt(
            system=_SYSTEM_GAPS,
            stable_context=[spec.model_dump_json(exclude_none=True)],
            brief=brief,
            labels=["spec"],
        )
        try:
            result = client.generate_structured(prompt, schema)
            batch = _GapBatch.model_validate(result.data)
        except (ValidationError, LLMError) as exc:
            errors = [str(exc)[:2000]]
            report.errors.append(errors[0])
            continue
        known = {question.id for question, _ in failing}
        return {p.question_id: p.fix for p in batch.proposals if p.question_id in known}, report

    report.fell_back = True
    return {}, report


def _tables_context(spec: ScenarioSpec) -> str:
    """A stable textual sketch of the physical schema for question prompts."""
    lines = ["Tables:"]
    for table in spec.tables:
        cols = ", ".join(col.name for col in table.columns)
        lines.append(f"- {table.name} ({table.grain}): {cols}")
    if spec.views:
        lines.append("Views:")
        for view in spec.views:
            lines.append(f"- {view.name}: {view.description or view.sql[:120]}")
    return "\n".join(lines)


def dump_spec(spec: ScenarioSpec) -> str:
    """Pretty JSON for writing a spec snapshot to disk."""
    return json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=False)
