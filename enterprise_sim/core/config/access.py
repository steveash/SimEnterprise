"""``[access]``: which of a world's documents only named people may read (decision record 0130).

The per-document reader lists are export metadata, computed by
:func:`enterprise_sim.exporters.access.access_lists` from the gold graph; they change
no document, so the section is not part of the config digest (like ``[data]`` and
``[ladder]``), and at its defaults it is not even a key of the stored config.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AccessConfig"]


class AccessConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    team_restricted_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Share of the document families no other rule restricts that only their authors' "
            "teams may read (picked by a hash of the family's event id)."
        ),
    )
    restricted_projects: int = Field(
        default=0,
        ge=0,
        description=(
            "How many unwalled projects only their own staff may read, every document of them "
            "(picked by a hash of the project id)."
        ),
    )
