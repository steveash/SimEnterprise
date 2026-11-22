"""``[isolation]``: walled projects whose knowledge must not cross (decision record 0036).

A world with walls has ``walled_projects`` projects in one department that look alike
(one topic stem, one playbook, the same fact attributes) but are staffed apart, carry
values that identify their owner, and never cite each other. The wall map is written
into the Layer-A graph by :func:`enterprise_sim.world_builders.walls.assign_walls`;
every later stage reads it from there, and ``world export`` declares it and refuses a
world whose files cross it (:mod:`enterprise_sim.exporters.isolation`).

Off by default: a section left at its default adds nothing to the config digest.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["IsolationConfig"]


class IsolationConfig(BaseModel):
    """Walled client-engagement projects in one department."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    walled_projects: int = Field(
        default=0,
        ge=0,
        le=5,
        description="Walled projects to build in one department (2-5); 0 disables walls.",
    )
    department: str | None = Field(
        default=None,
        description="Archetype name of the department that holds them (default: the primary).",
    )
    groups: list[list[int]] = Field(
        default_factory=list,
        description=(
            "Sides of the walls as lists of project numbers (1-based). Empty: every project "
            "is its own side, so every pair is walled."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> IsolationConfig:
        if self.walled_projects == 1:
            raise ValueError("walled_projects must be 0 (off) or 2 to 5")
        if self.groups:
            seen = [n for group in self.groups for n in group]
            if sorted(seen) != list(range(1, self.walled_projects + 1)):
                raise ValueError("groups must place each project 1..walled_projects exactly once")
            if len(self.groups) < 2:
                raise ValueError("groups must form at least two sides")
        return self

    @property
    def enabled(self) -> bool:
        return self.walled_projects > 0

    def side_of(self, number: int) -> int:
        """The side (0-based) of project ``number`` (1-based)."""
        if not self.groups:
            return number - 1
        return next(i for i, group in enumerate(self.groups) if number in group)
