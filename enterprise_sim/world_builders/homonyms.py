"""Deliberate homonyms (``[org] homonyms``): pairs of people with one full name.

Real organisations have two people called the same; a memory system has to tell them
apart from context. :func:`plant_homonyms` picks ``count`` pairs of people in
different departments and gives the second the first's full name. Each pair is
labelled in the gold KG with a ``homonym_of`` edge both ways, so a test can ask which
one a document meant.

The disambiguating context is the document's department: a document is rendered
against a view (:func:`enterprise_sim.assembly.views.document_view`) that keeps, of
each pair, only the twin in the document's own department (:func:`homonyms_hidden`)
and hides both from a document of any other department. The prose can therefore only
name the right twin, and the mention tagger binds the shared surface form to that
twin's id, so every gold ``mentions`` edge of a homonym is correct.

Team leads and walled staff (decision record 0036: they are named apart from
everyone) are never planted. With ``count`` 0 nothing changes.
"""

from __future__ import annotations

import random

from enterprise_sim.core.world import Edge, World

__all__ = ["E_HOMONYM_OF", "homonyms_hidden", "plant_homonyms"]

E_HOMONYM_OF = "homonym_of"


def plant_homonyms(world: World, count: int, rng: random.Random) -> list[tuple[str, str]]:
    """Rename ``count`` people so each shares a full name with someone in another department."""
    if count <= 0:
        return []
    from enterprise_sim.world_builders.walls import staff_of, walled_projects

    leads = {e.src for e in world.edges_by_type("leads")}
    walled = {pid for p in walled_projects(world) for pid in staff_of(world, p) or ()}
    people = sorted(
        (
            n
            for n in world.nodes_by_type("Person")
            if n.id not in leads and n.id not in walled and n.props.get("department")
        ),
        key=lambda n: n.id,
    )
    rng.shuffle(people)
    used: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for a in people:
        if len(pairs) >= count:
            break
        if a.id in used:
            continue
        b = next(
            (
                n
                for n in people
                if n.id not in used
                and n.id != a.id
                and n.props.get("department") != a.props.get("department")
            ),
            None,
        )
        if b is None:
            continue
        used.update((a.id, b.id))
        name = str(a.props["name"])
        old = str(b.props.get("name", ""))
        b.props["name"] = name
        b.aliases[:] = [name, *(x for x in b.aliases if x != old)]
        for src, dst in ((a.id, b.id), (b.id, a.id)):
            world.add_edge(
                Edge(f"edge:{E_HOMONYM_OF}:{src}->{dst}", E_HOMONYM_OF, src, dst, a.created_at)
            )
        pairs.append((a.id, b.id))
    return pairs


def homonyms_hidden(world: World, project_id: str | None) -> set[str]:
    """The twins a document of ``project_id`` must not see: every twin outside its department."""
    edges = world.edges_by_type(E_HOMONYM_OF)
    if not edges:
        return set()
    project = world.get_node(project_id) if project_id else None
    department = project.props.get("department") if project is not None else None
    hidden: set[str] = set()
    for edge in edges:
        twin = world.get_node(edge.src)
        if twin is not None and twin.props.get("department") != department:
            hidden.add(twin.id)
    return hidden
