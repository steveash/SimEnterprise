"""Benchmark-diversity knobs of a world config (``[facts]``, ``[messiness]``, ``[org]``).

These sections widen the distribution of worlds SimEnterprise can generate for
retrieval/memory benchmarks:

* :class:`FactsConfig` — a **fact ledger**: per-project facts (target dates,
  owners, budgets, datastores, pipeline schedules, metric definitions, ...) that
  change over the window, asserted by the documents — some of them stale,
  conflicting or hedged — with every assertion labelled in the gold KG.
* :class:`MessinessConfig` — surface noise in the rendered corpus (typos,
  nickname and initial aliases, mixed date formats, near-duplicate copies,
  dropped headers), each injection labelled in the gold KG.
* :class:`OrgConfig` — org-shape overrides: more departments than the size band
  allows, several squads per team shape, and people turnover over the window.

Every section defaults to *off*, and a section left at its default contributes
nothing to the config digest, so existing worlds (notably the pinned golden
world) keep their ids.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OfflineBackend(StrEnum):
    """The render backend a build uses without ``--live``.

    ``fake`` echoes the prompt (structure only, no facts in the prose); ``synth``
    composes deterministic, fact-bearing prose from the brief, at no cost.
    """

    FAKE = "fake"
    SYNTH = "synth"


def _range(text: str) -> tuple[int, int]:
    lo_s, _, hi_s = text.partition("..")
    lo, hi = int(lo_s), int(hi_s or lo_s)
    if lo < 0 or hi < lo:
        raise ValueError(f"bad range {text!r}: expected 'lo..hi' with 0 <= lo <= hi")
    return lo, hi


class FactsConfig(_Frozen):
    """The fact ledger: facts per project, how they change, how documents state them."""

    per_project: int = Field(
        default=0, ge=0, le=40, description="Facts per project; 0 disables the ledger."
    )
    change_rate: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Probability a fact gets a new version at each of up to max_versions-1 draws.",
    )
    max_versions: int = Field(default=3, ge=1, le=8, description="Most versions one fact can have.")
    per_artifact: str = Field(
        default="1..3", description="Facts a document asserts, as 'lo..hi' (0 allowed)."
    )
    stale_rate: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Probability a document states a superseded value of a changed fact.",
    )
    conflict_rate: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Probability a document states a value that was never true.",
    )
    hedge_rate: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Probability a correct statement is hedged ('I believe ...').",
    )
    cross_team: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Probability a project depends on a project of another department, whose "
            "facts its documents then cite (stale more often: cross_team_stale_boost)."
        ),
    )
    cross_team_stale_boost: float = Field(
        default=2.0, ge=1.0, le=10.0, description="Multiplier on stale_rate for cited facts."
    )
    horizon_days: int = Field(
        default=182,
        ge=14,
        le=3650,
        description=(
            "Days over which fact changes are drawn from the window start (fixed, not the "
            "window, so extending a world never rewrites the past). Raise for multi-year worlds."
        ),
    )
    retraction_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Probability a changed fact has one version later found to be an error: "
            "stated as current until a correction, never valid (recorded vs valid time)."
        ),
    )
    entity_ids: bool = Field(
        default=False,
        description=(
            "Name the project by its slug id too in every fact statement "
            "('Checkout [checkout-engineering]'), so each fact line identifies its entity."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> FactsConfig:
        _range(self.per_artifact)
        return self

    @property
    def per_artifact_range(self) -> tuple[int, int]:
        return _range(self.per_artifact)


class MessinessConfig(_Frozen):
    """Surface noise injected into rendered documents (0 everywhere = clean)."""

    typo_rate: float = Field(
        default=0.0, ge=0.0, le=0.2, description="Per-word probability of a typo."
    )
    alias_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Probability a person mention is written as a nickname or initials.",
    )
    date_format_mix: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Probability a date in prose is written in a non-ISO format.",
    )
    duplicate_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Probability a document also exists as a near-duplicate copy ('-v2-final').",
    )
    drop_header_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Probability a document loses its templated author/date header line.",
    )
    filler_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Probability of an off-topic filler paragraph (chatter, boilerplate).",
    )


class OrgConfig(_Frozen):
    """Org-shape overrides over the size band."""

    max_departments: int | None = Field(
        default=None,
        ge=1,
        le=40,
        description="Department cap when [[departments]] is not given (default: the size band).",
    )
    squads_per_team: str = Field(
        default="1..1",
        description="How many squads each team shape staffs, 'lo..hi' (size biases the pick).",
    )
    turnover_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=0.8,
        description="Share of non-lead people who leave during the window (a successor joins).",
    )
    horizon_days: int = Field(
        default=182,
        ge=14,
        le=3650,
        description="Days over which departures and transfers are drawn (raise for multi-year).",
    )
    project_names: bool = Field(
        default=False,
        description="Name default projects from a per-department bank ('Checkout Rewrite').",
    )
    transfer_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=0.5,
        description="Share of non-lead people who move to another team during the window.",
    )

    @model_validator(mode="after")
    def _check(self) -> OrgConfig:
        lo, _ = _range(self.squads_per_team)
        if lo < 1:
            raise ValueError("squads_per_team must start at 1 or more")
        return self

    @property
    def squads_range(self) -> tuple[int, int]:
        return _range(self.squads_per_team)


class DataMessinessConfig(_Frozen):
    """Raw-table noise for structured data products (0 everywhere = clean).

    Applied by :mod:`enterprise_sim.data_products.messiness` to the raw tables
    after views and answers are final.
    """

    null_rate: float = Field(default=0.0, ge=0.0, le=0.5)
    duplicate_rate: float = Field(default=0.0, ge=0.0, le=0.5)
    label_drift_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    outlier_rate: float = Field(default=0.0, ge=0.0, le=0.2)
    schema_drift: bool = False

    @property
    def clean(self) -> bool:
        return not any(self.model_dump().values())


class FormatsConfig(_Frozen):
    """Which kit document kinds render in the other native formats (all off = md/docx/Jira).

    The pptx and Outlook producers exist and are tested; these switches bind kit
    kinds to them: ``decks`` (business reviews, board packs, launch plans → .pptx),
    ``email`` (escalations, incident notices, offer approvals, pricing exceptions,
    purchase requests → .eml threads), ``invites`` (forecast calls, CSAT reviews,
    access reviews → .ics meeting invites with an agenda).
    """

    decks: bool = False
    email: bool = False
    invites: bool = False


class ChatConfig(_Frozen):
    """Team-channel threads beside documents (``producers/chat.py``)."""

    rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Probability a document also gets a chat thread restating its facts.",
    )


class CodeConfig(_Frozen):
    """Repository-like code artifacts for engineering projects (``assembly/code_repo.py``)."""

    enabled: bool = False


class PrivacyConfig(_Frozen):
    """Labelled sensitive-category sentences about people (``producers/sensitive.py``)."""

    sensitive_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Per-document probability in people ops, legal, finance (tenth elsewhere).",
    )


__all__ = [
    "ChatConfig",
    "PrivacyConfig",
    "CodeConfig",
    "DataMessinessConfig",
    "FormatsConfig",
    "FactsConfig",
    "MessinessConfig",
    "OfflineBackend",
    "OrgConfig",
]
