"""Config schema + loader for a data-products run.

Mirrors ``core/config``: a small, frozen, typo-proof Pydantic surface loaded
from TOML/JSON, validated up front, and snapshotted into the run's manifest.
A data run names one or more **registered scenarios**, points at the simulated
enterprise it should describe (a completed run's exported KG, or an inline
company built deterministically by Layer A), and dials authoring mode, scale,
and the question-loop depth.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise_sim.core.config.models import CompanyConfig

__all__ = [
    "AuthoringMode",
    "DataConfigError",
    "DataRunConfig",
    "load_data_config",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DataConfigError(Exception):
    """Raised when a data-run config cannot be read or parsed (not validation)."""


class AuthoringMode(StrEnum):
    """How each scenario's spec is produced.

    ``template`` uses the registered scenario plugin's deterministic causal
    graph (keyless, reproducible — the golden path). ``llm`` asks the
    configured backend to author/extend the spec from the scenario's brief;
    on the ``fake`` backend or on validation failure it falls back to the
    template so a run always completes.
    """

    TEMPLATE = "template"
    LLM = "llm"


class DataBackend(StrEnum):
    """LLM backend for a data run; ``fake`` is the deterministic default."""

    FAKE = "fake"
    ANTHROPIC_API = "anthropic_api"
    BEDROCK = "bedrock"
    CLAUDE_CLI = "claude_cli"


class WorldSourceConfig(_Frozen):
    """Where the simulated enterprise (the KG to link against) comes from.

    Exactly one of ``run_dir`` (a completed enterprise-sim run whose
    ``kg/nodes.jsonl`` is loaded) or ``company`` (an inline profile Layer A
    builds deterministically from the data run's seed) must be set.
    """

    run_dir: Path | None = Field(
        default=None, description="A completed run directory with an exported kg/."
    )
    company: CompanyConfig | None = Field(
        default=None, description="Inline company profile to build a world from."
    )

    @model_validator(mode="after")
    def _check(self) -> WorldSourceConfig:
        if (self.run_dir is None) == (self.company is None):
            raise ValueError("world source needs exactly one of run_dir or company")
        return self


class DataWindowConfig(_Frozen):
    """The date window the scenarios' panel data spans."""

    start: date
    end: date

    @model_validator(mode="after")
    def _check(self) -> DataWindowConfig:
        if self.end < self.start:
            raise ValueError(f"window end ({self.end}) must not precede start ({self.start})")
        return self


class AuthoringConfig(_Frozen):
    """Spec-authoring mode and the LLM repair budget."""

    mode: AuthoringMode = AuthoringMode.TEMPLATE
    max_repair_attempts: int = Field(default=2, ge=0, le=5)


class DataScaleConfig(_Frozen):
    """Volume dials: population scaling and parquet partition sizing."""

    factor: float = Field(
        default=1.0,
        gt=0.0,
        description="Multiplier on every population's base size (identity-bound ones excepted).",
    )
    rows_per_partition: int = Field(
        default=2_000_000,
        ge=10_000,
        description="Target rows per parquet part file for entity_day tables.",
    )


class LoopConfig(_Frozen):
    """The analysis loop: propose questions, evaluate, patch gaps, resample."""

    iterations: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Max generate→evaluate→patch passes (loop exits early when no gaps remain).",
    )


class DataModelConfig(_Frozen):
    """LLM backend/model for llm-mode authoring and question generation."""

    backend: DataBackend = DataBackend.FAKE
    name: str = Field(default="claude-opus-4-8", min_length=1)
    cost_ceiling_usd: float | None = Field(default=None, ge=0.0)
    cache_dir: str | None = None


class DataRunConfig(_Frozen):
    """Top-level config for `enterprise-sim data run`."""

    scenarios: tuple[str, ...] = Field(
        min_length=1, description="Registered data-scenario names to run."
    )
    seed: int = Field(default=0, ge=0)
    output_dir: Path = Path("runs/data")
    world: WorldSourceConfig
    window: DataWindowConfig
    briefs: dict[str, str] = Field(
        default_factory=dict,
        description="Optional per-scenario natural-language guidance for llm-mode authoring.",
    )
    authoring: AuthoringConfig = Field(default_factory=AuthoringConfig)
    scale: DataScaleConfig = Field(default_factory=DataScaleConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    model: DataModelConfig = Field(default_factory=DataModelConfig)


def load_data_config(path: str | Path) -> DataRunConfig:
    """Read, parse, and validate a data-run config from ``path`` (.toml/.json)."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise DataConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise DataConfigError(f"could not read config file {path}: {exc}") from exc

    if suffix == ".toml":
        try:
            data: Any = tomllib.loads(raw.decode("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise DataConfigError(f"invalid TOML in {path}: {exc}") from exc
    elif suffix == ".json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DataConfigError(f"invalid JSON in {path}: {exc}") from exc
    else:
        raise DataConfigError(
            f"unsupported config extension {suffix!r} for {path}; use .toml or .json"
        )
    if not isinstance(data, Mapping):
        raise DataConfigError(f"config root must be a table/object in {path}")
    return DataRunConfig.model_validate(data)
