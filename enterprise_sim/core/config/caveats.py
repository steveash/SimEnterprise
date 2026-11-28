"""``[caveats]``: decisions whose value a consumer must adjust before using it.

See :mod:`enterprise_sim.facts.caveats`. At its default (0 caveats) the section is not
even a key of the stored config, so no world id moves.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CaveatsConfig"]


class CaveatsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = Field(
        default=0,
        ge=0,
        le=40,
        description=(
            "How many caveats to plant: on each, one document of the owner team states a "
            "fact's value together with an exception or adjustment that changes the value "
            "a consumer must use (a held-back share of a budget, a later regional launch)."
        ),
    )
