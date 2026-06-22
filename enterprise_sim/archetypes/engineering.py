"""The ``engineering`` department archetype (ARCHITECTURE.md §4, Registry 1).

A software-engineering org unit: it ships product, so it leans on the
``build_software`` reference playbook (cadence sprints, design reviews, ship
milestones — see :func:`enterprise_sim.authoring.patterns.build_software`).
Importing this module registers the archetype as a side effect.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec, TeamShape
from enterprise_sim.core.registry import ARCHETYPES

#: The engineering department archetype.
ENGINEERING = ARCHETYPES.register(
    DepartmentArchetypeSpec(
        name="engineering",
        charter="Build, ship, and operate the product.",
        typical_goals=(
            "Ship {project} to general availability.",
            "Reduce {service} p99 latency by {target}.",
            "Pay down tech debt in {area}.",
        ),
        team_shapes=(
            TeamShape(
                title="Product Engineering",
                count="4..8",
                skills=("backend", "frontend", "api_design"),
            ),
            TeamShape(
                title="Platform / Infrastructure",
                count="2..5",
                skills=("infrastructure", "ci_cd", "observability"),
            ),
            TeamShape(
                title="Quality Engineering",
                count="1..3",
                skills=("test_automation", "release_management"),
            ),
        ),
        playbooks=("build_software",),
    )
)
