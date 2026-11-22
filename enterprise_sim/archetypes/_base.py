"""Shared shape for DepartmentArchetype plugins (ARCHITECTURE.md §4, Registry 1).

A department archetype is a *kind* of org unit — it biases Layer A toward a
believable department by declaring its charter, the goals it typically pursues,
the team shapes it staffs, and which **playbooks** it tends to run. The four
plugin protocols in :mod:`enterprise_sim.core.registry.plugins` are structural,
so an archetype only has to expose ``name`` + ``playbooks`` to register; the
extra fields here are the §4 "typical goals / typical team shapes" metadata Layer
A reads to generate something plausible.

This module is ``_``-prefixed so plugin discovery skips it (it ships no
registrations of its own — see :func:`enterprise_sim.core.registry.discover`);
the concrete archetype modules import :class:`DepartmentArchetypeSpec` from here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(slots=True)
class TeamShape:
    """A team this archetype typically staffs: a title and a head-count range.

    ``count`` is a ``"min..max"`` range string (the same convention selectors and
    :class:`~enterprise_sim.authoring.sdk.Spread` use) so Layer A can pick a
    concrete size within believable bounds.
    """

    title: str
    #: head-count range, e.g. ``"3..6"``.
    count: str
    #: representative skills the team carries (for staffing/affinity biasing).
    skills: tuple[str, ...] = ()


@dataclass(slots=True)
class DepartmentArchetypeSpec:
    """A registrable :class:`DepartmentArchetype` with Layer-A biasing metadata.

    Satisfies the structural :class:`DepartmentArchetype` protocol via ``name`` +
    ``playbooks``; ``charter``, ``typical_goals``, and ``team_shapes`` carry the
    "typical goals / typical team shapes" the architecture asks an archetype to
    declare so generation produces a believable org unit.
    """

    name: str
    #: one-line statement of the department's function.
    charter: str
    #: goals this department typically pursues (goal templates for Layer A).
    typical_goals: tuple[str, ...]
    #: teams this department typically staffs.
    team_shapes: tuple[TeamShape, ...]
    #: names of playbooks this archetype typically runs (the protocol field).
    playbooks: Sequence[str]
    #: the ``role`` prop given to the department head, which the archetype's
    #: playbooks select their lead on (``None``: the builder's built-in table).
    lead_role: str | None = None
    #: company verticals this archetype is the primary department for.
    verticals: tuple[str, ...] = ()
    #: individual-contributor title noun for this department's people
    #: (``None``: derived from the team title, default ``Specialist``).
    ic_noun: str | None = None
    #: team-shape title -> ``role`` given to that team's lead, for playbooks
    #: that select a lead other than the department head (retail's sales lead
    #: and demand planner).
    team_lead_roles: dict[str, str] = field(default_factory=dict)
