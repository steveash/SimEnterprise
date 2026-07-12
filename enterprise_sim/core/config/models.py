"""Pydantic v2 schema for a run config (PLAN.md §2 / ARCHITECTURE.md §6 step 1).

A user describes a company at a high level; the simulator invents everything
else. This module defines the *input* a user supplies: the company profile, the
projects/initiatives to seed, the simulation window, the LLM backend, the root
seed, and the output directory. Everything is validated up front so a bad config
fails before any LLM cost is incurred, and the validated object is snapshotted
into the run's ``manifest.json`` for reproducibility.

All models are frozen (immutable) and forbid unknown keys, so typos in a config
file surface as errors rather than being silently ignored.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    """Selectable LLM backend (ARCHITECTURE.md §7).

    Mirrors the names accepted by ``core.llm.build_backend`` and the CLI
    ``--backend`` flags; ``fake`` is the deterministic, network-free default (D31).
    """

    FAKE = "fake"
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


class RunConfig(_Frozen):
    """Top-level run configuration: the complete validated input to a simulation."""

    company: CompanyConfig
    simulation: SimulationConfig
    seed: int = Field(
        default=0,
        ge=0,
        description="Root seed; threads through the run to derive all sub-seeds.",
    )
    output_dir: Path = Field(
        default=Path("runs"),
        description="Directory the run materializes into (corpus + manifest + gold KG).",
    )
    projects: tuple[ProjectConfig, ...] = Field(
        default=(),
        description="Optional anchor projects; empty lets Layer A invent them.",
    )
    model: ModelConfig = Field(default_factory=ModelConfig)
    scale: ScaleConfig = Field(default_factory=ScaleConfig)
