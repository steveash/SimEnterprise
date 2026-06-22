"""The ``retail`` department archetype (ARCHITECTURE.md §4, Registry 1).

A merchandising / store-operations org unit: it keeps product on shelves, so it
leans on the ``sell_merchandise`` reference playbook (low-stock cascade, supplier
negotiation, purchase orders — see
:func:`enterprise_sim.authoring.patterns.sell_merchandise`). Importing this
module registers the archetype as a side effect.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec, TeamShape
from enterprise_sim.core.registry import ARCHETYPES

#: The retail department archetype.
RETAIL = ARCHETYPES.register(
    DepartmentArchetypeSpec(
        name="retail",
        charter="Keep merchandise in stock and moving across stores.",
        typical_goals=(
            "Keep {category} in stock above {service_level}.",
            "Grow {region} same-store sales by {target}.",
            "Cut stockouts on {sku} during {season}.",
        ),
        team_shapes=(
            TeamShape(
                title="Merchandising / Buying",
                count="2..5",
                skills=("buying", "supplier_negotiation", "category_management"),
            ),
            TeamShape(
                title="Inventory / Replenishment",
                count="2..4",
                skills=("demand_planning", "inventory_control"),
            ),
            TeamShape(
                title="Store Operations",
                count="3..8",
                skills=("store_ops", "merchandising", "customer_service"),
            ),
        ),
        playbooks=("sell_merchandise",),
    )
)
