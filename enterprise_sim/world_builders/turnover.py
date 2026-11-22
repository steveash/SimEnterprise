"""People turnover over the window (``[org] turnover_rate`` / ``transfer_rate``).

Real teams change while the documents are being written: people leave and a
successor joins, people move to another team. :func:`apply_turnover` writes
that into the Layer-A graph after the org is staffed:

* a **leaver** keeps their node, gains ``left_at``, and their team membership
  edge gains ``until``; a **successor** joins a few days later (``joined_at``,
  node ``created_at`` = the join), takes the leaver's team, manager and project
  roles, and points at them with a ``replaces`` edge;
* a **transfer** moves a person to another team of the same department on a
  date: the old ``member_of``/``reports_to`` edges gain ``until``, new ones start
  then.

The actor resolver skips a person outside ``[joined_at, left_at)`` (see
:meth:`enterprise_sim.core.sim.resolver.Resolver.resolve`), so departed people
stop authoring and successors start. Team leads never turn over (the playbooks
select their leads by role). Pure in ``(world, config, seed)``; with both rates
at zero nothing changes.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from enterprise_sim.core.config import WorldConfig
from enterprise_sim.core.config.seed import SeedContext
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.world_builders.names import FIRST_NAMES, LAST_NAMES, SlugAllocator, pick

__all__ = ["apply_turnover"]

E_MEMBER_OF = "member_of"
E_REPORTS_TO = "reports_to"
E_LEADS = "leads"
E_REPLACES = "replaces"


def _day(start: datetime, rng_offset: int) -> datetime:
    return datetime.combine((start + timedelta(days=rng_offset)).date(), time(9, 0))


def apply_turnover(
    world: World, config: WorldConfig, ctx: SeedContext, slugs: SlugAllocator
) -> None:
    """Add leavers, successors and transfers to ``world`` per ``config.org``."""
    org = config.org
    if org.turnover_rate <= 0 and org.transfer_rate <= 0:
        return
    t0 = datetime.combine(config.simulation.period_start, time(9, 0))
    span = (config.simulation.period_end - config.simulation.period_start).days
    if span < 14:
        return
    # Dates are drawn over a fixed horizon, not the window, so extending a
    # world's window never moves a past departure; later ones are skipped.
    horizon = org.horizon_days
    leads = {e.src for e in world.edges_by_type(E_LEADS)}
    people = [p for p in world.nodes_by_type("Person") if p.id not in leads]
    teams_by_dept: dict[str, list[str]] = {}
    for team in world.nodes_by_type("Team"):
        teams_by_dept.setdefault(str(team.props.get("department", "")), []).append(team.id)
    for person in people:
        rng = ctx.rng("turnover", person.id)
        leaves = rng.random() < org.turnover_rate
        left = _day(t0, rng.randint(10, horizon))
        gap = rng.randint(3, 14)
        if leaves:
            if (left - t0).days <= span - 5:
                _leave(world, person, left, gap, ctx, slugs, t0, span)
            continue
        moves = rng.random() < org.transfer_rate
        moved = _day(t0, rng.randint(7, horizon))
        if moves and (moved - t0).days <= span - 3:
            dept = str(person.props.get("department", ""))
            current = _team_of(world, person.id)
            options = [t for t in teams_by_dept.get(dept, []) if t != current]
            if options and current is not None:
                _transfer(world, person, current, rng.choice(sorted(options)), moved)


def _team_of(world: World, person_id: str) -> str | None:
    for edge in world.out_edges(person_id, E_MEMBER_OF):
        node = world.get_node(edge.dst)
        if node is not None and node.type == "Team":
            return node.id
    return None


def _manager_of(world: World, person_id: str) -> str | None:
    edges = world.out_edges(person_id, E_REPORTS_TO)
    return edges[0].dst if edges else None


def _leave(
    world: World,
    person: Node,
    left: datetime,
    gap_days: int,
    ctx: SeedContext,
    slugs: SlugAllocator,
    t0: datetime,
    span: int,
) -> None:
    person.props["left_at"] = left.isoformat()
    for edge in world.out_edges(person.id, E_MEMBER_OF):
        edge.props["until"] = left.isoformat()
    for edge in world.out_edges(person.id, E_REPORTS_TO):
        edge.props["until"] = left.isoformat()
    joined = datetime.combine((left + timedelta(days=gap_days)).date(), time(9, 0))
    if joined > t0 + timedelta(days=span):
        return  # the seat stays open until after the window
    rng = ctx.rng("successor", person.id)
    name = f"{pick(rng, FIRST_NAMES)} {pick(rng, LAST_NAMES)}"
    successor_id = f"person:{slugs.allocate(name)}"
    props = {
        key: value
        for key, value in person.props.items()
        if key not in ("left_at", "name", "role", "seniority", "title")
    }
    noun = str(person.props.get("title", "Specialist")).split(" ", 1)[-1]
    props.update(
        {
            "name": name,
            "seniority": "junior",
            "title": f"Junior {noun}",
            "joined_at": joined.isoformat(),
            "replaces": person.id,
        }
    )
    world.add_node(Node(successor_id, "Person", joined, props=props, aliases=[name]))
    world.add_edge(
        Edge(
            f"edge:{E_REPLACES}:{successor_id}->{person.id}",
            E_REPLACES,
            successor_id,
            person.id,
            joined,
        )
    )
    for edge in list(world.out_edges(person.id, E_MEMBER_OF)):
        world.add_edge(
            Edge(
                f"edge:{E_MEMBER_OF}:{successor_id}->{edge.dst}",
                E_MEMBER_OF,
                successor_id,
                edge.dst,
                joined,
                props={k: v for k, v in edge.props.items() if k != "until"},
            )
        )
    manager = _manager_of(world, person.id)
    if manager is not None:
        world.add_edge(
            Edge(
                f"edge:{E_REPORTS_TO}:{successor_id}->{manager}",
                E_REPORTS_TO,
                successor_id,
                manager,
                joined,
            )
        )


def _transfer(world: World, person: Node, old: str, new: str, moved: datetime) -> None:
    for edge in world.out_edges(person.id, E_MEMBER_OF):
        if edge.dst == old:
            edge.props["until"] = moved.isoformat()
    for edge in world.out_edges(person.id, E_REPORTS_TO):
        edge.props["until"] = moved.isoformat()
    team = world.get_node(new)
    person.props["transferred_at"] = moved.isoformat()
    person.props["transferred_to"] = new
    if team is not None:
        person.props["unit"] = team.props.get("name", person.props.get("unit"))
    world.add_edge(
        Edge(f"edge:{E_MEMBER_OF}:{person.id}->{new}", E_MEMBER_OF, person.id, new, moved)
    )
    for lead in world.in_edges(new, E_LEADS):
        world.add_edge(
            Edge(
                f"edge:{E_REPORTS_TO}:{person.id}->{lead.src}",
                E_REPORTS_TO,
                person.id,
                lead.src,
                moved,
            )
        )
