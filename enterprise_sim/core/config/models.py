"""Pydantic v2 schema for a run config (PLAN.md §2 / ARCHITECTURE.md §6 step 1).

A user describes a company at a high level; the simulator invents everything
else. This module defines the *input* a user supplies: the company profile, the
projects/initiatives to seed, the simulation window, the LLM backend, the root
seed, and the output directory. Everything is validated up front so a bad config
fails before any LLM cost is incurred, and the validated object is snapshotted
into the world's ``.world`` for reproducibility.

All models are frozen (immutable) and forbid unknown keys, so typos in a config
file surface as errors rather than being silently ignored.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise_sim.core.config.diversity import (
    ChatConfig,
    CodeConfig,
    DataMessinessConfig,
    FactsConfig,
    FormatsConfig,
    MessinessConfig,
    OfflineBackend,
    OrgConfig,
    PrivacyConfig,
)
from enterprise_sim.core.config.isolation import IsolationConfig


class _Frozen(BaseModel):
    """Base for config models: immutable, strict, and typo-proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CompanySize(StrEnum):
    """Coarse company-size band; biases org depth, headcount, and archetypes."""

    STARTUP = "startup"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    ENTERPRISE = "enterprise"


class LLMBackend(StrEnum):
    """Selectable LLM backend (ARCHITECTURE.md §7)."""

    ANTHROPIC_API = "anthropic_api"
    BEDROCK = "bedrock"
    CLAUDE_CLI = "claude_cli"


class CompanyConfig(_Frozen):
    """High-level description of the company to simulate."""

    name: str = Field(min_length=1, description="Company display name.")
    vertical: str = Field(
        min_length=1,
        description="Industry/vertical, e.g. 'software', 'retail'. Drives playbook bias.",
    )
    size: CompanySize = Field(description="Coarse size band biasing org structure.")
    description: str | None = Field(
        default=None,
        description="Optional free-text seed steering world generation.",
    )


class ProjectConfig(_Frozen):
    """A named project/initiative to seed into the world.

    Projects are optional: an empty list lets Layer A invent them from the
    company profile. When supplied, each entry anchors a concrete initiative the
    generated org must include.
    """

    name: str = Field(min_length=1, description="Project name.")
    description: str | None = Field(
        default=None, description="Optional free-text description / goal."
    )
    playbook: str | None = Field(
        default=None,
        description=(
            "Registered playbook this project's scenario binds to. Defaults to "
            "its department archetype's first playbook."
        ),
    )
    department: str | None = Field(
        default=None,
        description=(
            "Archetype name of the department this project is anchored under. "
            "Defaults to the primary department."
        ),
    )


class DepartmentConfig(_Frozen):
    """Explicit archetype selection for one department (§3.6).

    Ordered — the first entry is the *primary* department (the one a project
    attaches to by default, and the vertical/size-driven selection's role when
    ``WorldConfig.departments`` is omitted entirely).
    """

    archetype: str = Field(min_length=1, description="A registered department archetype name.")


class SimulationConfig(_Frozen):
    """Simulation window and clock parameters."""

    period_start: date = Field(description="Inclusive first day of the simulated window.")
    period_end: date = Field(description="Inclusive last day of the simulated window.")

    @model_validator(mode="after")
    def _check_window(self) -> SimulationConfig:
        if self.period_end < self.period_start:
            raise ValueError(
                f"simulation.period_end ({self.period_end.isoformat()}) must not precede "
                f"period_start ({self.period_start.isoformat()})"
            )
        return self


class ModelConfig(_Frozen):
    """LLM backend selection and the realism/cost dial (ARCHITECTURE.md §7)."""

    backend: LLMBackend = Field(
        default=LLMBackend.ANTHROPIC_API, description="Which provider/backend to call."
    )
    name: str = Field(
        default="claude-opus-4-8",
        min_length=1,
        description="Model identifier passed to the backend.",
    )
    realism: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Realism/cost dial in [0,1]; higher trades cost for fidelity.",
    )
    offline_backend: OfflineBackend = Field(
        default=OfflineBackend.FAKE,
        description=(
            "Render backend without --live: fake (prompt echo, structure only) or "
            "synth (deterministic fact-bearing prose)."
        ),
    )


class ScaleConfig(_Frozen):
    """Scale / cost controls for the Layer C render phase (ARCHITECTURE.md §7/§16.4).

    These knobs bound the parallel render and gate spend before a large run: the
    render fans out across scenarios capped at ``max_concurrency`` (§16.1 D26), a
    dry-run estimate (artifact count × per-artifact token estimate) is checked
    against ``cost_ceiling_usd`` *before* any call is made (D13), and the on-disk
    response cache (D31) is configured here so cheap re-runs only regenerate what
    changed. All have safe defaults so a config need not mention scale at all.
    """

    max_concurrency: int = Field(
        default=8,
        ge=1,
        description="Max scenarios rendered in parallel (the Layer C concurrency dial).",
    )
    cost_ceiling_usd: float | None = Field(
        default=None,
        ge=0.0,
        description="Hard USD ceiling; a dry-run estimate over it aborts before any call.",
    )
    est_input_tokens_per_artifact: int = Field(
        default=1200,
        ge=0,
        description="Per-artifact input-token estimate used by the dry-run gate (D13).",
    )
    est_cached_input_tokens_per_artifact: int = Field(
        default=0,
        ge=0,
        description="Per-artifact cached-prefix input tokens assumed by the dry-run gate.",
    )
    est_output_tokens_per_artifact: int = Field(
        default=600,
        ge=0,
        description="Per-artifact output-token estimate used by the dry-run gate (D13).",
    )
    cache_dir: str | None = Field(
        default=None,
        description="On-disk response-cache directory (D31); in-memory only when unset.",
    )
    cache_enabled: bool = Field(
        default=True,
        description="Whether the response cache is consulted/written (D31).",
    )


class DataAuthoring(StrEnum):
    """How each structured-data scenario's causal spec is produced (docs/DATA_PRODUCTS.md §2)."""

    TEMPLATE = "template"
    LLM = "llm"


class DataPlanConfig(_Frozen):
    """The structured-data half of a world: which data scenarios to build with it.

    A world is one company: its document corpus + gold KG (the rest of
    :class:`WorldConfig`) and, when ``scenarios`` is non-empty, structured data
    products — causal tables, views, and business questions — sampled over the
    same window and linked against the same KG, written to
    ``<world>/data/<scenario>/``. The build runs them as a ``data`` phase after
    the corpus. ``llm`` authoring uses the world's ``[model]`` (on a ``--live``
    build; otherwise the deterministic template is used, as in keyless mode).
    """

    scenarios: tuple[str, ...] = Field(
        default=(),
        description="Data-scenario names (built-in or template); empty = no structured data.",
    )
    authoring: DataAuthoring = Field(
        default=DataAuthoring.TEMPLATE,
        description="template (deterministic) or llm (the world's model elaborates each spec).",
    )
    scale_factor: float = Field(
        default=1.0, gt=0.0, description="Multiplier on every data population's base size."
    )
    iterations: int = Field(
        default=3, ge=1, le=5, description="Max question-loop passes per scenario."
    )
    rows_per_partition: int = Field(
        default=2_000_000, ge=10_000, description="Target rows per parquet part file."
    )
    messiness: DataMessinessConfig = Field(
        default_factory=DataMessinessConfig,
        description="Raw-table noise (nulls, duplicate rows, label drift, outliers, schema drift).",
    )


class WorldConfig(_Frozen):
    """Top-level run configuration: the complete validated input to a simulation."""

    company: CompanyConfig
    simulation: SimulationConfig
    seed: int = Field(
        default=0,
        ge=0,
        description="Root seed; threads through the run to derive all sub-seeds.",
    )
    worlds_root: Path = Field(
        default=Path("worlds"),
        description="Directory the world lands in, as <worlds_root>/<world-id>/.",
    )
    projects: tuple[ProjectConfig, ...] = Field(
        default=(),
        description="Optional anchor projects; empty lets Layer A invent them.",
    )
    departments: tuple[DepartmentConfig, ...] = Field(
        default=(),
        description=(
            "Optional explicit department/archetype selection (ordered; first is "
            "primary). Empty applies the vertical/size-driven selection, as before."
        ),
    )
    model: ModelConfig = Field(default_factory=ModelConfig)
    scale: ScaleConfig = Field(default_factory=ScaleConfig)
    data: DataPlanConfig = Field(
        default_factory=DataPlanConfig,
        description=(
            "Structured data products built with the world (data scenarios over the "
            "same window, linked to the same KG). Excluded from the config digest: "
            "it does not change the corpus or graph, so adding data never changes "
            "a world's id."
        ),
    )
    facts: FactsConfig = Field(
        default_factory=FactsConfig,
        description="The fact ledger: changing per-project facts, stale/conflicting assertions.",
    )
    messiness: MessinessConfig = Field(
        default_factory=MessinessConfig,
        description="Surface noise injected into rendered documents, labelled in the gold KG.",
    )
    privacy: PrivacyConfig = Field(
        default_factory=PrivacyConfig,
        description="Labelled sensitive-category sentences about people (no PII shapes).",
    )
    isolation: IsolationConfig = Field(
        default_factory=IsolationConfig,
        description="Walled projects whose knowledge must not cross (decision record 0036).",
    )
    code: CodeConfig = Field(
        default_factory=CodeConfig,
        description="Repository artifacts per engineering project (config, README, source).",
    )
    chat: ChatConfig = Field(
        default_factory=ChatConfig,
        description="Team-channel threads restating documents' facts, one fact per message.",
    )
    formats: FormatsConfig = Field(
        default_factory=FormatsConfig,
        description="Bind kit document kinds to the pptx / e-mail / invite producers.",
    )
    org: OrgConfig = Field(
        default_factory=OrgConfig,
        description="Org-shape overrides: department cap, squads per team shape, turnover.",
    )
    plugins: tuple[str, ...] = Field(
        default=(),
        description=(
            "Extra external plugin directories to discover (EXPLORER_TEMPLATES.md "
            "§2.1), each holding <slug>/plugin.py (and/or bare <name>.py) modules "
            "registering archetypes/playbooks/processes. Operational, like `scale`: "
            "excluded from the config digest so pointing a run at a template dir "
            "never changes its run id. Merged with the "
            "ENTERPRISE_SIM_PLUGIN_PATH env var by core.registry.plugin_paths()."
        ),
    )
