"""``[concurrency]``: days when two teams work the same hours and one finds a fact the other needs.

See :mod:`enterprise_sim.facts.concurrency`. At its default (0 days) the section is not
even a key of the stored config, so no world id moves.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ConcurrencyConfig"]


class ConcurrencyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    days: int = Field(
        default=0,
        ge=0,
        le=40,
        description=(
            "How many concurrent days to plant: on each, a cross-team fact changes mid-day, "
            "its owner team finds it at that time, and the team that cites it works before "
            "and after (stating the old value)."
        ),
    )
    attributes: tuple[str, ...] = Field(
        default=("target_date", "status"),
        min_length=1,
        description=(
            "Which cited attributes a concurrent day's found fact may be (one answer kind per "
            "set, for example only target_date)."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> ConcurrencyConfig:
        unknown = set(self.attributes) - {"target_date", "status"}
        if unknown:
            raise ValueError(f"[concurrency] attributes must be cited ones, not {sorted(unknown)}")
        return self
