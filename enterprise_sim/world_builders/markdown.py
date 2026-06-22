"""Render the Layer-A world to ``organization/`` markdown reference data.

The bead's second deliverable (esim-65bf4594) is human-readable **reference
data**: a markdown mirror of the generated company so a person can read the org
the same way the KG encodes it. This module is a *pure function of a*
:class:`~enterprise_sim.core.world.World` — it only queries the graph (by type
and by traversing reified edges), which doubles as a demonstration that the KG is
queryable (the acceptance criterion). It writes nothing that is not already in
the graph.

Layout (under ``<run>/organization/``)::

    README.md                index: company, counts, department list
    company.md               company profile + goal tree (D14 nesting)
    people.md                full roster table
    departments/<name>.md    per-department: teams, people, initiatives, projects

Output is deterministic: every listing sorts by node id (as the store's typed
queries already do), so the same world always renders byte-identical markdown.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from enterprise_sim.core.world import Node, World
from enterprise_sim.world_builders.builder import (
    E_ADVANCES_GOAL,
    E_LEADS,
    E_MEMBER_OF,
    E_PART_OF,
    E_SUBGOAL_OF,
    E_SUBINITIATIVE_OF,
    E_UNDER,
    N_COMPANY,
    N_DEPARTMENT,
    N_GOAL,
    N_INITIATIVE,
    N_PERSON,
)

__all__ = ["render_organization", "write_organization"]


def write_organization(world: World, dest: Path) -> list[Path]:
    """Render ``world`` to markdown under ``dest`` and return the files written.

    ``dest`` is the ``organization/`` directory; it (and a ``departments/``
    subdirectory) is created if absent. Returns the written paths in a stable
    order.
    """
    written: list[Path] = []
    for rel, text in render_organization(world).items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def render_organization(world: World) -> dict[str, str]:
    """Return ``relative-path → markdown`` for the whole org (no filesystem I/O)."""
    pages: dict[str, str] = {
        "README.md": _render_index(world),
        "company.md": _render_company(world),
        "people.md": _render_people(world),
    }
    for dept in world.nodes_by_type(N_DEPARTMENT):
        pages[f"departments/{dept.props.get('archetype', _tail(dept.id))}.md"] = _render_department(
            world, dept
        )
    return pages


# -- pages ------------------------------------------------------------------#


def _render_index(world: World) -> str:
    company = _first(world, N_COMPANY)
    lines = ["# Organization Reference", ""]
    if company is not None:
        lines += [
            f"**{company.props.get('name', _tail(company.id))}** — "
            f"{company.props.get('vertical', '?')}, {company.props.get('size', '?')}.",
            "",
        ]
    lines += [
        "## Contents",
        "",
        "- [Company & goals](company.md)",
        "- [People](people.md)",
    ]
    for dept in world.nodes_by_type(N_DEPARTMENT):
        name = dept.props.get("name", _tail(dept.id))
        archetype = dept.props.get("archetype", _tail(dept.id))
        lines.append(f"- [{name}](departments/{archetype}.md)")
    lines += ["", "## Knowledge-graph counts", ""]
    lines += _counts_table(world)
    return _join(lines)


def _render_company(world: World) -> str:
    company = _first(world, N_COMPANY)
    lines = ["# Company", ""]
    if company is not None:
        lines += [
            f"- **Name:** {company.props.get('name', '')}",
            f"- **Vertical:** {company.props.get('vertical', '')}",
            f"- **Size:** {company.props.get('size', '')}",
        ]
        description = company.props.get("description")
        if description:
            lines += ["", str(description)]
    lines += ["", "## Goals", ""]
    top_goals = [g for g in world.nodes_by_type(N_GOAL) if not world.out_edges(g.id, E_SUBGOAL_OF)]
    if not top_goals:
        lines.append("_No goals generated._")
    for goal in top_goals:
        lines.append(f"- **{goal.props.get('statement', _tail(goal.id))}**")
        for sub in world.neighbors(goal.id, E_SUBGOAL_OF, direction="in"):
            lines.append(f"    - {sub.props.get('statement', _tail(sub.id))}")
    return _join(lines)


def _render_people(world: World) -> str:
    lines = ["# People", "", f"{len(world.nodes_by_type(N_PERSON))} people.", ""]
    lines += _people_table(world.nodes_by_type(N_PERSON))
    return _join(lines)


def _render_department(world: World, dept: Node) -> str:
    name = dept.props.get("name", _tail(dept.id))
    lines = [f"# {name}", "", f"_{dept.props.get('charter', '')}_", ""]

    advanced = world.neighbors(dept.id, E_ADVANCES_GOAL, direction="out")
    if advanced:
        lines += ["## Advances goals", ""]
        for goal in advanced:
            lines.append(f"- {goal.props.get('statement', _tail(goal.id))}")
        lines.append("")

    lines += ["## Teams", ""]
    for team in world.neighbors(dept.id, E_PART_OF, direction="in"):
        members = world.neighbors(team.id, E_MEMBER_OF, direction="in")
        leads = {n.id for n in world.neighbors(team.id, E_LEADS, direction="in")}
        skills = ", ".join(team.props.get("skills", []) or [])
        lines += [f"### {team.props.get('name', _tail(team.id))}", ""]
        if skills:
            lines += [f"_Skills: {skills}_", ""]
        lines += _people_table(members, leads=leads)
        lines.append("")

    lines += _render_initiatives(world, dept)
    return _join(lines)


def _render_initiatives(world: World, dept: Node) -> list[str]:
    archetype = dept.props.get("archetype")
    programs = [
        n
        for n in world.nodes_by_type(N_INITIATIVE)
        if n.props.get("type") == "program" and n.props.get("department") == archetype
    ]
    lines = ["## Initiatives & projects", ""]
    if not programs:
        lines.append("_No initiatives generated._")
        return lines
    for program in programs:
        lines.append(f"- **{program.props.get('name', _tail(program.id))}** (program)")
        for scenario in world.neighbors(program.id, E_SUBINITIATIVE_OF, direction="in"):
            playbook = scenario.props.get("playbook", "?")
            lines.append(
                f"    - {scenario.props.get('name', _tail(scenario.id))} "
                f"(scenario · playbook `{playbook}`)"
            )
            for project in world.neighbors(scenario.id, E_UNDER, direction="in"):
                lines.append(f"        - 📦 {project.props.get('name', _tail(project.id))}")
                for person, role in _project_members(world, project.id):
                    who = person.props.get("name", _tail(person.id))
                    lines.append(f"            - {role}: {who}")
    return lines


# -- shared table/format helpers --------------------------------------------#


def _people_table(people: Iterable[Node], *, leads: set[str] | None = None) -> list[str]:
    leads = leads or set()
    people = list(people)
    if not people:
        return ["_No people._"]
    rows = ["| Name | Title | Seniority | Expertise |", "| --- | --- | --- | --- |"]
    for person in people:
        marker = " ⭐" if person.id in leads else ""
        expertise = ", ".join(person.props.get("expertise", []) or [])
        rows.append(
            f"| {person.props.get('name', _tail(person.id))}{marker} "
            f"| {person.props.get('title', '')} "
            f"| {person.props.get('seniority', '')} "
            f"| {expertise} |"
        )
    return rows


def _counts_table(world: World) -> list[str]:
    rows = ["| Kind | Type | Count |", "| --- | --- | --- |"]
    for node_type in world.node_types():
        rows.append(f"| node | {node_type} | {len(world.nodes_by_type(node_type))} |")
    for edge_type in world.edge_types():
        rows.append(f"| edge | {edge_type} | {len(world.edges_by_type(edge_type))} |")
    return rows


def _project_members(world: World, project_id: str) -> list[tuple[Node, str]]:
    """Return ``(person, role)`` for a project, ordered lead → contributor → reviewer."""
    order = {"lead": 0, "contributor": 1, "reviewer": 2}
    pairs: list[tuple[Node, str]] = []
    for edge in world.in_edges(project_id, E_MEMBER_OF):
        person = world.get_node(edge.src)
        if person is not None:
            pairs.append((person, str(edge.props.get("role", "member"))))
    pairs.sort(key=lambda pair: (order.get(pair[1], 9), pair[0].id))
    return pairs


def _first(world: World, node_type: str) -> Node | None:
    nodes = world.nodes_by_type(node_type)
    return nodes[0] if nodes else None


def _tail(node_id: str) -> str:
    return node_id.split(":", 1)[-1]


def _join(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"
