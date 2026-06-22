"""Layer A — the world & intent builder (ARCHITECTURE.md §2/§6, bead esim-65bf4594).

Layer A is where a run stops being a config and becomes a *company*. Given the
validated :class:`~enterprise_sim.core.config.RunConfig`, :func:`build_world`
invents the whole org top-down and writes it into a :class:`World` (the KG spine,
§11):

    company → goals (may nest, D14) → departments (archetype-biased) →
    teams → people (expertise, reporting lines) → initiatives
    (program ⊃ scenario, scenario binds a playbook) → projects (members w/ roles)

and finally **seeds latent affinities** — ``collaborates_with`` edges (the same
canonical edges the actor resolver reads in §15.3, decision D28) — so go-to
collaborators already exist before Layer B reinforces them.

**Everything is deterministic (D10).** No live LLM is consulted: structure,
staffing, names, and ids are pure functions of ``(config, seed)``, drawn from
seeded sub-streams (:class:`~enterprise_sim.core.config.SeedContext`) in a fixed
order. Two runs with the same seed produce byte-identical KGs. (The architecture
allows an LLM to *parameterize* this step later; the deterministic skeleton is
the contract Layer A must always satisfy, and what the test kit checks.)

The builder is archetype-driven: department shape, team shapes, skills, and which
playbooks a department runs all come from the registered
:class:`DepartmentArchetype` plugins (§4, Registry 1), so the believable-org
surface grows as more archetypes are registered without touching this module.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, time

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec, TeamShape
from enterprise_sim.core.config import RunConfig
from enterprise_sim.core.config.models import CompanySize, ProjectConfig
from enterprise_sim.core.config.seed import SeedContext
from enterprise_sim.core.registry import ARCHETYPES, discover
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.world_builders.names import (
    FIRST_NAMES,
    LAST_NAMES,
    SlugAllocator,
    pick,
    slugify,
)

__all__ = ["build_world"]

# -- node & edge type vocabulary (ARCHITECTURE.md §3) ----------------------- #

N_COMPANY = "Company"
N_GOAL = "Goal"
N_DEPARTMENT = "Department"
N_TEAM = "Team"
N_PERSON = "Person"
N_INITIATIVE = "Initiative"
N_PROJECT = "Project"

E_OWNS = "owns"  # company -> goal
E_SUBGOAL_OF = "subgoal_of"  # goal -> parent goal
E_HAS_DEPARTMENT = "has_department"  # company -> department
E_ADVANCES_GOAL = "advances_goal"  # department/initiative -> goal
E_PART_OF = "part_of"  # team -> department
E_MEMBER_OF = "member_of"  # person -> team / person -> project (role prop)
E_REPORTS_TO = "reports_to"  # person -> manager
E_LEADS = "leads"  # person -> team / person -> department
E_OWNS_INITIATIVE = "owns_initiative"  # person -> initiative
E_SUBINITIATIVE_OF = "subinitiative_of"  # initiative -> parent initiative
E_UNDER = "under"  # project -> initiative
E_COLLABORATES_WITH = "collaborates_with"  # person <-> person (latent affinity)

# Match the actor resolver's canonical affinity-edge id so seeded affinities are
# read back by ``Resolver`` unchanged (resolver.py ``_affinity_edge_id``).
_AFFINITY_EDGE_TYPE = E_COLLABORATES_WITH


# -- sizing -----------------------------------------------------------------#


@dataclass(frozen=True, slots=True)
class _SizeProfile:
    """How a company-size band scales the generated org.

    ``staffing_t`` is a 0..1 dial that biases each team toward the low (0) or
    high (1) end of its archetype head-count range.
    """

    n_goals: int
    max_departments: int
    staffing_t: float


# Bands grow goal count and department breadth; ``max_departments`` is also
# capped by the number of registered archetypes (you cannot staff a department
# you have no template for).
_SIZE_PROFILES: dict[CompanySize, _SizeProfile] = {
    CompanySize.STARTUP: _SizeProfile(n_goals=2, max_departments=1, staffing_t=0.0),
    CompanySize.SMALL: _SizeProfile(n_goals=3, max_departments=1, staffing_t=0.25),
    CompanySize.MEDIUM: _SizeProfile(n_goals=4, max_departments=2, staffing_t=0.5),
    CompanySize.LARGE: _SizeProfile(n_goals=5, max_departments=2, staffing_t=0.75),
    CompanySize.ENTERPRISE: _SizeProfile(n_goals=6, max_departments=2, staffing_t=1.0),
}

# Company verticals that map onto a registered archetype (the primary, core
# department). Anything unknown falls back to the first registered archetype.
_VERTICAL_ALIASES: dict[str, str] = {
    "software": "engineering",
    "technology": "engineering",
    "tech": "engineering",
    "saas": "engineering",
    "engineering": "engineering",
    "retail": "retail",
    "merchandising": "retail",
    "commerce": "retail",
    "ecommerce": "retail",
    "e-commerce": "retail",
}

# A department's lead carries the "role" the reference playbooks select on
# (build_software → eng_lead, sell_merchandise → buyer), so a Layer-A org is
# directly queryable by those playbooks' selectors.
_LEAD_ROLE: dict[str, str] = {
    "engineering": "eng_lead",
    "retail": "buyer",
}

# Company-level objectives Layer A draws from (D14: some are given sub-goals).
_GOAL_BANK: tuple[str, ...] = (
    "Grow annual recurring revenue by 25%.",
    "Expand into two new regional markets.",
    "Achieve SOC 2 Type II compliance.",
    "Improve customer retention to 95%.",
    "Reduce operating costs by 15%.",
    "Launch the next-generation product line.",
    "Raise customer satisfaction above 90%.",
    "Halve time-to-market for new offerings.",
)
_SUBGOAL_BANK: tuple[str, ...] = (
    "Stand up the supporting platform and tooling.",
    "Hire and onboard the delivery team.",
    "Define and instrument success metrics.",
    "Ship a validated pilot to early customers.",
)

_SENIORITY_BANK: tuple[str, ...] = ("senior", "mid", "mid", "junior")

# Maps a team title to a believable individual-contributor noun.
_IC_NOUNS: tuple[tuple[str, str], ...] = (
    ("engineering", "Engineer"),
    ("platform", "Engineer"),
    ("infrastructure", "Engineer"),
    ("quality", "QA Engineer"),
    ("inventory", "Planner"),
    ("replenishment", "Planner"),
    ("store", "Associate"),
    ("merchandising", "Buyer"),
    ("buying", "Buyer"),
)


def _ic_noun(team_title: str) -> str:
    """Return an IC role noun for ``team_title`` (default ``"Specialist"``)."""
    lowered = team_title.lower()
    for needle, noun in _IC_NOUNS:
        if needle in lowered:
            return noun
    return "Specialist"


def _staff_count(shape: TeamShape, t: float, rng: random.Random) -> int:
    """Pick a head-count in ``shape``'s ``lo..hi`` range, biased by size ``t``.

    The base is a linear interpolation toward the high end as ``t`` rises; a
    seeded ±1 jitter keeps teams from all landing on the same size, then the
    result is clamped back into ``[lo, hi]``.
    """
    lo_s, _, hi_s = shape.count.partition("..")
    lo, hi = int(lo_s), int(hi_s or lo_s)
    base = round(lo + t * (hi - lo))
    jittered = base + rng.choice((-1, 0, 1))
    return max(lo, min(hi, jittered))


# -- builder ----------------------------------------------------------------#


class _WorldBuilder:
    """Stateful, single-use builder that materializes one :class:`World`.

    Construct with the run config, call :meth:`build` once. All randomness flows
    from :attr:`_ctx` (the run's :class:`SeedContext`) drawn in a fixed order, so
    the produced graph is a pure function of ``(config, seed)``.
    """

    def __init__(self, config: RunConfig) -> None:
        self._config = config
        self._world = World()
        self._ctx = SeedContext(config.seed)
        self._slugs = SlugAllocator()
        self._t0 = datetime.combine(config.simulation.period_start, time(9, 0))
        self._company_id = ""
        self._goal_ids: list[str] = []
        # Affinity weights keyed by an unordered person-pair (seeded then written
        # once as canonical ``collaborates_with`` edges).
        self._affinity: dict[frozenset[str], float] = {}

    # -- orchestration ------------------------------------------------------

    def build(self) -> World:
        """Run the full top-down pipeline and return the populated KG."""
        archetypes = self._select_archetypes()
        self._build_company()
        self._build_goals()
        for index, spec in enumerate(archetypes):
            self._build_department(index, spec)
        self._write_affinities()
        return self._world

    # -- archetype selection ------------------------------------------------

    def _select_archetypes(self) -> list[DepartmentArchetypeSpec]:
        """Choose departments: the vertical-matched archetype first, then more.

        The primary archetype is the one the company's vertical maps onto (or the
        first registered archetype if the vertical is unknown). Larger size bands
        add further archetypes (name-sorted) up to ``max_departments``, itself
        capped by how many archetypes are registered.
        """
        discover("enterprise_sim.archetypes")  # idempotent; fires registrations.
        registered = {spec.name: spec for spec in ARCHETYPES}
        if not registered:
            raise RuntimeError("no department archetypes registered; cannot build a world")

        vertical = self._config.company.vertical.strip().lower()
        primary = _VERTICAL_ALIASES.get(vertical)
        if primary not in registered:
            primary = sorted(registered)[0]

        ordered = [primary] + [name for name in sorted(registered) if name != primary]
        limit = min(self._profile().max_departments, len(ordered))
        return [_as_spec(registered[name]) for name in ordered[:limit]]

    def _profile(self) -> _SizeProfile:
        return _SIZE_PROFILES[self._config.company.size]

    # -- company ------------------------------------------------------------

    def _build_company(self) -> None:
        company = self._config.company
        self._company_id = f"company:{slugify(company.name)}"
        self._add_node(
            self._company_id,
            N_COMPANY,
            props={
                "name": company.name,
                "vertical": company.vertical,
                "size": company.size.value,
                "description": company.description or "",
            },
            aliases=[company.name],
        )

    # -- goals --------------------------------------------------------------

    def _build_goals(self) -> None:
        """Sample distinct company goals; give every other goal a sub-goal (D14)."""
        rng = self._ctx.rng("goals")
        n = min(self._profile().n_goals, len(_GOAL_BANK))
        chosen = rng.sample(_GOAL_BANK, n)
        sub_pool = list(_SUBGOAL_BANK)
        for i, text in enumerate(chosen, start=1):
            goal_id = f"goal:{i}"
            self._add_node(goal_id, N_GOAL, props={"statement": text}, aliases=[text])
            self._add_edge(E_OWNS, self._company_id, goal_id)
            self._goal_ids.append(goal_id)
            # Nest a sub-goal under every other goal to exercise the recursive
            # goal model without exploding the tree.
            if i % 2 == 1 and sub_pool:
                sub_text = sub_pool[(i - 1) // 2 % len(sub_pool)]
                sub_id = f"{goal_id}.1"
                self._add_node(sub_id, N_GOAL, props={"statement": sub_text}, aliases=[sub_text])
                self._add_edge(E_SUBGOAL_OF, sub_id, goal_id)

    def _goals_for(self, index: int) -> list[str]:
        """Deterministically assign 1–2 top-level goals to department ``index``."""
        if not self._goal_ids:
            return []
        rng = self._ctx.rng("dept_goals", index)
        k = min(len(self._goal_ids), 1 + (index % 2))
        return sorted(rng.sample(self._goal_ids, k))

    # -- departments / teams / people --------------------------------------

    def _build_department(self, index: int, spec: DepartmentArchetypeSpec) -> None:
        dept_id = f"dept:{spec.name}"
        display = f"{spec.name.replace('_', ' ').title()}"
        self._add_node(
            dept_id,
            N_DEPARTMENT,
            props={"name": display, "archetype": spec.name, "charter": spec.charter},
            aliases=[display, f"{display} Department"],
        )
        self._add_edge(E_HAS_DEPARTMENT, self._company_id, dept_id)
        goal_ids = self._goals_for(index)
        for goal_id in goal_ids:
            self._add_edge(E_ADVANCES_GOAL, dept_id, goal_id)

        # Staff each team; the first team's lead becomes the department head.
        t = self._profile().staffing_t
        people_by_team: dict[str, list[str]] = {}
        team_leads: list[str] = []
        dept_head: str | None = None
        for shape in spec.team_shapes:
            team_id, members, lead_id = self._build_team(spec, dept_id, shape, t)
            people_by_team[team_id] = members
            team_leads.append(lead_id)
            if dept_head is None:
                dept_head = lead_id

        assert dept_head is not None  # every archetype declares ≥1 team shape.
        # Reporting + leadership lines: head leads the department; other team
        # leads report to the head.
        self._add_edge(E_LEADS, dept_head, dept_id)
        self._mark_role(dept_head, _LEAD_ROLE.get(spec.name))
        for lead_id in team_leads:
            if lead_id != dept_head:
                self._add_edge(E_REPORTS_TO, lead_id, dept_head)

        self._build_initiatives(index, spec, dept_id, dept_head, goal_ids, people_by_team)
        self._seed_team_affinities(people_by_team)

    def _build_team(
        self, spec: DepartmentArchetypeSpec, dept_id: str, shape: TeamShape, t: float
    ) -> tuple[str, list[str], str]:
        """Create one team and its people; return ``(team_id, members, lead_id)``."""
        team_id = f"team:{spec.name}-{slugify(shape.title)}"
        self._add_node(
            team_id,
            N_TEAM,
            props={
                "name": shape.title,
                "department": spec.name,
                "skills": list(shape.skills),
            },
            aliases=[shape.title],
        )
        self._add_edge(E_PART_OF, team_id, dept_id)

        rng = self._ctx.rng("staff", spec.name, shape.title)
        count = _staff_count(shape, t, rng)
        members: list[str] = []
        lead_id = ""
        for i in range(count):
            is_lead = i == 0
            person_id = self._build_person(spec, dept_id, team_id, shape, is_lead, rng)
            members.append(person_id)
            if is_lead:
                lead_id = person_id
                self._add_edge(E_LEADS, person_id, team_id)
            else:
                self._add_edge(E_REPORTS_TO, person_id, lead_id)
        return team_id, members, lead_id

    def _build_person(
        self,
        spec: DepartmentArchetypeSpec,
        dept_id: str,
        team_id: str,
        shape: TeamShape,
        is_lead: bool,
        rng: random.Random,
    ) -> str:
        first = pick(rng, FIRST_NAMES)
        last = pick(rng, LAST_NAMES)
        name = f"{first} {last}"
        person_id = f"person:{self._slugs.allocate(name)}"

        seniority = "principal" if is_lead else pick(rng, _SENIORITY_BANK)
        if is_lead:
            title = f"{shape.title} Lead"
        else:
            title = f"{seniority.title()} {_ic_noun(shape.title)}"

        expertise = self._expertise(shape, rng)
        self._add_node(
            person_id,
            N_PERSON,
            props={
                "name": name,
                "title": title,
                # ``team`` is the functional area (== department/archetype) the
                # reference playbooks select on; the granular org unit is the
                # member_of Team node plus ``unit``.
                "team": spec.name,
                "unit": shape.title,
                "department": spec.name,
                "seniority": seniority,
                "expertise": expertise,
                "working_hours": {"start": "09:00", "end": "17:00"},
            },
            aliases=[name],
        )
        self._add_edge(E_MEMBER_OF, person_id, team_id)
        return person_id

    def _expertise(self, shape: TeamShape, rng: random.Random) -> list[str]:
        """Draw a non-empty, seeded subset of the team's representative skills."""
        skills = list(shape.skills)
        if not skills:
            return []
        k = rng.randint(1, len(skills))
        return sorted(rng.sample(skills, k))

    def _mark_role(self, person_id: str, role: str | None) -> None:
        """Tag a person with a playbook-selectable ``role`` (e.g. ``eng_lead``)."""
        if role is None:
            return
        node = self._world.get_node(person_id)
        if node is not None:
            node.props["role"] = role

    # -- initiatives / projects --------------------------------------------

    def _build_initiatives(
        self,
        index: int,
        spec: DepartmentArchetypeSpec,
        dept_id: str,
        dept_head: str,
        goal_ids: list[str],
        people_by_team: dict[str, list[str]],
    ) -> None:
        """Build the department's program → scenario(s) → project(s) sub-tree.

        A single ``program`` initiative owns one ``scenario`` per playbook the
        archetype runs; each scenario binds that playbook and anchors a concrete
        project staffed from the department.
        """
        display = spec.name.replace("_", " ").title()
        program_id = f"initiative:{spec.name}-program"
        self._add_node(
            program_id,
            N_INITIATIVE,
            props={"type": "program", "name": f"{display} Program", "department": spec.name},
            aliases=[f"{display} Program"],
        )
        self._add_edge(E_OWNS_INITIATIVE, dept_head, program_id)
        for goal_id in goal_ids:
            self._add_edge(E_ADVANCES_GOAL, program_id, goal_id)

        roster = [pid for members in people_by_team.values() for pid in members]
        for playbook in spec.playbooks:
            scenario_id = f"initiative:{spec.name}-{slugify(playbook)}"
            title = f"{playbook.replace('_', ' ').title()} ({display})"
            self._add_node(
                scenario_id,
                N_INITIATIVE,
                props={
                    "type": "scenario",
                    "name": title,
                    "playbook": playbook,
                    "department": spec.name,
                },
                aliases=[title],
            )
            self._add_edge(E_SUBINITIATIVE_OF, scenario_id, program_id)
            self._add_edge(E_OWNS_INITIATIVE, dept_head, scenario_id)
            for goal_id in goal_ids:
                self._add_edge(E_ADVANCES_GOAL, scenario_id, goal_id)
            self._build_project(spec, scenario_id, playbook, dept_head, roster)

        self._build_config_projects(index, spec, program_id, dept_head, roster)

    def _build_project(
        self,
        spec: DepartmentArchetypeSpec,
        initiative_id: str,
        playbook: str,
        lead_id: str,
        roster: list[str],
        *,
        project_id: str | None = None,
        name: str | None = None,
        description: str = "",
    ) -> None:
        """Create a project under ``initiative_id`` and staff it with roles.

        The lead is ``lead_id``; remaining roster members are split, by a seeded
        shuffle, into contributors and reviewers so the affinity seeder and Layer
        B have realistic project teams to draw on.
        """
        project_id = project_id or f"project:{slugify(playbook)}-{spec.name}"
        display = name or f"{playbook.replace('_', ' ').title()} Delivery"
        self._add_node(
            project_id,
            N_PROJECT,
            props={
                "name": display,
                "description": description,
                "department": spec.name,
                "start": self._config.simulation.period_start.isoformat(),
                "end": self._config.simulation.period_end.isoformat(),
            },
            aliases=[display],
        )
        self._add_edge(E_UNDER, project_id, initiative_id)

        self._add_member(lead_id, project_id, "lead")
        others = [pid for pid in roster if pid != lead_id]
        rng = self._ctx.rng("project", project_id)
        rng.shuffle(others)
        # Roughly two-thirds contribute, the rest review; at least the lead is
        # always present so a tiny team still yields a valid project.
        split = (len(others) * 2) // 3
        for pid in others[:split]:
            self._add_member(pid, project_id, "contributor")
        for pid in others[split:]:
            self._add_member(pid, project_id, "reviewer")

        self._seed_project_affinities(project_id, [lead_id, *others])

    def _build_config_projects(
        self,
        index: int,
        spec: DepartmentArchetypeSpec,
        program_id: str,
        dept_head: str,
        roster: list[str],
    ) -> None:
        """Anchor any user-supplied projects under the primary department (D: config).

        Config projects are only attached to the first (primary) department so
        each anchor appears exactly once; each becomes its own scenario bound to
        the archetype's first playbook plus a concrete project.
        """
        if index != 0 or not self._config.projects:
            return
        playbook = spec.playbooks[0] if spec.playbooks else "build_software"
        for project in self._config.projects:
            self._build_config_project(spec, program_id, playbook, dept_head, roster, project)

    def _build_config_project(
        self,
        spec: DepartmentArchetypeSpec,
        program_id: str,
        playbook: str,
        dept_head: str,
        roster: list[str],
        project: ProjectConfig,
    ) -> None:
        slug = slugify(project.name)
        scenario_id = f"initiative:anchor-{slug}"
        self._add_node(
            scenario_id,
            N_INITIATIVE,
            props={
                "type": "scenario",
                "name": project.name,
                "playbook": playbook,
                "department": spec.name,
                "anchored": True,
            },
            aliases=[project.name],
        )
        self._add_edge(E_SUBINITIATIVE_OF, scenario_id, program_id)
        self._add_edge(E_OWNS_INITIATIVE, dept_head, scenario_id)
        self._build_project(
            spec,
            scenario_id,
            playbook,
            dept_head,
            roster,
            project_id=f"project:{slug}",
            name=project.name,
            description=project.description or "",
        )

    def _add_member(self, person_id: str, project_id: str, role: str) -> None:
        edge_id = f"edge:{E_MEMBER_OF}:{person_id}->{project_id}"
        self._world.add_edge(
            Edge(edge_id, E_MEMBER_OF, person_id, project_id, self._t0, props={"role": role})
        )

    # -- latent affinities (D28 / §15.3) -----------------------------------

    def _seed_team_affinities(self, people_by_team: dict[str, list[str]]) -> None:
        """Add base affinity between every pair of teammates (small teams)."""
        for members in people_by_team.values():
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    self._bump_affinity(a, b, 1.0)

    def _seed_project_affinities(self, project_id: str, members: list[str]) -> None:
        """Strengthen affinity between people who share a project."""
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                self._bump_affinity(a, b, 1.0)

    def _bump_affinity(self, a: str, b: str, weight: float) -> None:
        if a == b:
            return
        self._affinity[frozenset((a, b))] = self._affinity.get(frozenset((a, b)), 0.0) + weight

    def _write_affinities(self) -> None:
        """Materialize accumulated affinity weights as canonical symmetric edges.

        The id format matches :class:`~enterprise_sim.core.sim.resolver.Resolver`
        (``edge:collaborates_with:a<->b`` with a ``weight`` prop) so the resolver
        reads these seeded affinities without translation. Sorted for determinism.
        """
        for pair in sorted(self._affinity, key=lambda p: tuple(sorted(p))):
            src, dst = sorted(pair)
            edge_id = f"edge:{_AFFINITY_EDGE_TYPE}:{src}<->{dst}"
            self._world.add_edge(
                Edge(
                    edge_id,
                    _AFFINITY_EDGE_TYPE,
                    src,
                    dst,
                    self._t0,
                    props={"weight": self._affinity[pair]},
                )
            )

    # -- low-level helpers --------------------------------------------------

    def _add_node(
        self,
        node_id: str,
        node_type: str,
        *,
        props: dict[str, object],
        aliases: list[str],
    ) -> None:
        self._world.add_node(Node(node_id, node_type, self._t0, props=props, aliases=aliases))

    def _add_edge(self, edge_type: str, src: str, dst: str) -> None:
        edge_id = f"edge:{edge_type}:{src}->{dst}"
        self._world.add_edge(Edge(edge_id, edge_type, src, dst, self._t0))


def _as_spec(plugin: object) -> DepartmentArchetypeSpec:
    """Narrow a registered archetype plugin to the concrete builder spec.

    Layer A reads the §4 biasing metadata (team shapes, skills, charter), which
    only :class:`DepartmentArchetypeSpec` carries; the registry is typed to the
    leaner structural protocol, so assert the concrete type here.
    """
    if not isinstance(plugin, DepartmentArchetypeSpec):
        raise TypeError(
            f"Layer A needs a DepartmentArchetypeSpec, got {type(plugin).__name__}; "
            "register archetypes via DepartmentArchetypeSpec."
        )
    return plugin


def build_world(config: RunConfig) -> World:
    """Build the Layer-A knowledge graph for ``config`` (deterministic in seed).

    Returns a populated :class:`World` — company, goals, departments, teams,
    people, initiatives, projects, and seeded ``collaborates_with`` affinities —
    ready for markdown reference output and Layer B simulation.
    """
    return _WorldBuilder(config).build()
