"""``[ladder]``: which size ladder a world is a rung of (P571136233).

A size ladder is one seed built at several sizes, its base facts constant
(:mod:`enterprise_sim.worlds.ladder`). The section only labels the world: ``world
export`` copies it into ``export.json`` as ``ladder: {id, rung}``, so an importer can
check that a catalog row's scale is the export's rung. It changes nothing about the
world, so it is not part of the config digest (like ``[data]``).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["LadderConfig", "LadderRung"]


class LadderRung(StrEnum):
    S = "s"
    M = "m"
    L = "l"


class LadderConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]*$", description="Ladder id.")
    rung: LadderRung | None = Field(default=None, description="This world's rung: s, m or l.")

    @model_validator(mode="after")
    def _check(self) -> LadderConfig:
        if (self.id is None) != (self.rung is None):
            raise ValueError("[ladder] needs both id and rung, or neither")
        return self
