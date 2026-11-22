"""Walled projects (decision record 0036): the one producer and the readers of the wall map.

:func:`assign_walls` partitions a department's roster among the walled projects and
names them; the builder writes the plan into the Layer-A graph (project props
``walled``/``wall_side`` and one ``walled_from`` edge per walled pair). Every later
stage (actor resolution, the document view in ``assembly.views``, the fact ledger,
cross-team citing, turnover, the export check) reads the walls only through the
readers below, never from the config.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass

from enterprise_sim.core.world import World

__all__ = [
    "E_WALLED_FROM",
    "WalledProject",
    "across_wall",
    "assign_walls",
    "hidden_from",
    "unique_staff_names",
    "staff_of",
    "walled_pairs",
    "walled_projects",
]

E_WALLED_FROM = "walled_from"
E_MEMBER_OF = "member_of"
_CLIENTS = ("Alder", "Birch", "Cedar", "Dunlin", "Egret")
_MIN_STAFF = 2


@dataclass(frozen=True)
class WalledProject:
    """One walled project as planned: its number, side, display name, lead and staff."""

    number: int
    side: int
    name: str
    lead: str
    staff: tuple[str, ...]
    slug: str  # "<client>-<topic>": a short, readable project id and slice name


def assign_walls(
    count: int,
    sides: list[int],
    roster: Iterable[str],
    topic: str,
    rng: random.Random,
) -> list[WalledProject]:
    """Split ``roster`` into ``count`` disjoint staffs and name the projects.

    Every project gets at least two people; the first of each staff is its lead.
    The names share ``topic`` ("<topic> for Client Alder", ...) so the projects
    look alike on purpose.
    """
    people = sorted(set(roster))
    if len(people) < count * _MIN_STAFF:
        raise ValueError(
            f"{count} walled projects need {count * _MIN_STAFF} people besides the "
            f"department head; the department has {len(people)}"
        )
    rng.shuffle(people)
    staffs: list[list[str]] = [[] for _ in range(count)]
    for index, person in enumerate(people):
        staffs[index % count].append(person)
    return [
        WalledProject(
            number=n + 1,
            side=sides[n],
            name=f"{topic} for Client {_CLIENTS[n]}",
            lead=staff[0],
            staff=tuple(staff),
            slug=f"{_CLIENTS[n].lower()}-{_slug(topic)}",
        )
        for n, staff in enumerate(staffs)
    ]


def _slug(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def walled_projects(world: World) -> dict[str, int]:
    """Project id -> wall side, for every walled project of ``world``."""
    return {
        node.id: int(node.props["wall_side"])
        for node in world.nodes_by_type("Project")
        if node.props.get("walled")
    }


def walled_pairs(world: World) -> set[frozenset[str]]:
    """Every pair of projects that must not cross."""
    return {frozenset((e.src, e.dst)) for e in world.edges_by_type(E_WALLED_FROM)}


def across_wall(world: World, a: str | None, b: str | None) -> bool:
    """Whether projects ``a`` and ``b`` sit on two sides of a wall."""
    if a is None or b is None or a == b:
        return False
    return frozenset((a, b)) in walled_pairs(world)


def staff_of(world: World, project_id: str | None) -> frozenset[str] | None:
    """The staff of a walled project (``None`` for an unwalled one or no project)."""
    if project_id is None:
        return None
    node = world.get_node(project_id)
    if node is None or not node.props.get("walled"):
        return None
    return frozenset(e.src for e in world.in_edges(project_id, E_MEMBER_OF))


def hidden_from(world: World, project_id: str | None) -> set[str]:
    """Every node a walled project's documents must not see: the far side of its walls.

    For each project across a wall: the project, its initiatives, its staff (and
    collaborators), the artifacts that express it, and its facts with their versions.
    Empty for an unwalled project, so its documents see the whole world as before.
    """
    if project_id is None or staff_of(world, project_id) is None:
        return set()
    hidden: set[str] = set()
    for pair in walled_pairs(world):
        if project_id not in pair:
            continue
        (other,) = pair - {project_id}
        hidden.add(other)
        hidden.update(e.dst for e in world.out_edges(other, "under"))
        for edge_type in (E_MEMBER_OF, "collaborates_with", "expresses", "about"):
            hidden.update(e.src for e in world.in_edges(other, edge_type))
    facts = {n for n in hidden if (node := world.get_node(n)) is not None and node.type == "Fact"}
    if facts:
        hidden.update(
            n.id for n in world.nodes_by_type("FactVersion") if str(n.props.get("fact")) in facts
        )
    own = staff_of(world, project_id) or frozenset()
    return hidden - own - {project_id}


def unique_staff_names(world: World, rng: random.Random) -> None:
    """Rename every walled staffer whose full name someone else in the world also has.

    The must-not-cross map names staff by full name, so two people sharing one would
    make a crossing undecidable. Runs after the whole org is staffed, before turnover
    (whose successors avoid taken names in a walled world, ``turnover._leave``).
    """
    from enterprise_sim.world_builders.names import FIRST_NAMES, LAST_NAMES, pick

    staff = {pid for project in walled_projects(world) for pid in staff_of(world, project) or ()}
    people = world.nodes_by_type("Person")
    count: dict[str, int] = {}
    for person in people:
        name = str(person.props.get("name", ""))
        count[name] = count.get(name, 0) + 1
    for person in sorted(people, key=lambda n: n.id):
        name = str(person.props.get("name", ""))
        if person.id not in staff or count.get(name, 0) < 2:
            continue
        fresh = name
        while fresh in count:
            fresh = f"{pick(rng, FIRST_NAMES)} {pick(rng, LAST_NAMES)}"
        count[name] -= 1
        count[fresh] = 1
        person.props["name"] = fresh
        person.aliases[:] = [fresh, *(a for a in person.aliases if a != name)]
