"""Layer A generators: build the world (company → people → projects) + markdown.

:func:`build_world` invents a deterministic company from a
:class:`~enterprise_sim.core.config.RunConfig` and writes it into the KG;
:func:`write_organization` renders that world to ``organization/`` markdown
reference data. See :mod:`enterprise_sim.world_builders.builder`.
"""

from __future__ import annotations

from enterprise_sim.world_builders.builder import build_world
from enterprise_sim.world_builders.markdown import render_organization, write_organization

__all__ = ["build_world", "render_organization", "write_organization"]
