"""``[concurrency]``: days when two teams work the same hours and one finds a fact the other needs.

See :mod:`enterprise_sim.facts.concurrency`. At its default (0 days) the section is not
even a key of the stored config, so no world id moves.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
