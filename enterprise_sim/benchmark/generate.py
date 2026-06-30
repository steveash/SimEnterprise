"""Deterministically derive :class:`QAPair`\\ s from the gold knowledge graph (esim-uzc.2).

The sim emits a gold KG plus its answer key; this module turns that ground truth
into a question/answer benchmark — no LLM, no labelling, fully reproducible. Each
generator walks the gold :class:`~enterprise_sim.core.world.World` (and, for the
provenance family, the grounding mentions) and mints :class:`QAPair`\\ s whose
``expected_ids`` are the exact KG node ids a correct answer must resolve to,
tagged by the kind of reasoning required (:data:`REASONING_TYPES`):

* ``direct_relation`` — one edge: who someone reports to, leads, belongs to;
  what a company owns; what a person authored.
* ``transitive`` — a chain of like edges: a person's full management chain
  (``reports_to+``) and which department they sit in (``member_of`` then
  ``part_of``).
* ``provenance`` — the answer key: which artifacts ground (mention) an entity.
* ``aggregation`` — a count over many nodes: team headcount, direct reports,
  teams per department.
* ``goal_tree`` — a goal/sub-goal walk: what advances a goal (directly or via a
  subgoal) and a goal's subgoals.

Determinism is structural, not incidental: every generator iterates the gold
graph in sorted order, every answer set is sorted, each pair's id is a content
hash of its semantics, and the benchmark is sorted by a stable key — so the same
gold run yields a byte-identical benchmark. :func:`generate` is the entry point;
it runs a fresh golden run by default (so the gold graph and answer key always
agree) or reads an existing run directory when given one.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.core.world import Edge, Node, World

# Separator for content-hashed ids: a NUL byte never appears in an id or a
# question, so distinct groupings of the same characters can never collide
# (mirrors enterprise_sim.core.config.seed).
_SEP = "\x00"


@dataclass(frozen=True)
class _Draft:
    """A reasoning-tagged question/answer before it is assigned a stable id.

    The id is derived from this content (:meth:`_qid`), so two drafts with the
    same semantics always produce the same :class:`QAPair`.
    """

    reasoning_type: str
    qtype: str
    question: str
    expected_ids: tuple[str, ...]
    expected_label: str | None
    difficulty: str

    def sort_key(self) -> tuple[str, ...]:
        """A stable ordering key (reasoning type, then surface form, then answer)."""
        return (self.reasoning_type, self.qtype, self.question, *self.expected_ids)

    def to_pair(self) -> QAPair:
        """Materialize the :class:`QAPair` with its content-derived id."""
        return QAPair(
            id=_qid(self.reasoning_type, self.qtype, self.question, self.expected_ids),
            question=self.question,
            qtype=self.qtype,
            reasoning_type=self.reasoning_type,
            expected_ids=self.expected_ids,
            expected_label=self.expected_label,
            difficulty=self.difficulty,
        )


def _qid(reasoning_type: str, qtype: str, question: str, expected_ids: tuple[str, ...]) -> str:
    """A stable ``qa-<hash>`` id derived from a pair's semantic content."""
    digest = hashlib.sha1(usedforsecurity=False)
    for part in (reasoning_type, qtype, question, *sorted(expected_ids)):
        digest.update(part.encode("utf-8"))
        digest.update(_SEP.encode("utf-8"))
    return f"qa-{digest.hexdigest()[:12]}"


def _label(node: Node) -> str:
    """A human-readable name for ``node``: its canonical alias, else a prop, else id."""
    if node.aliases:
        return node.aliases[0]
    props = node.props
    for key in ("name", "title", "statement"):
        value = props.get(key)
        if value:
            return str(value)
    return node.id


def _label_of(world: World, node_id: str) -> str:
    """:func:`_label` for ``node_id``, falling back to the id when unknown."""
    node = world.get_node(node_id)
    return _label(node) if node is not None else node_id


def _single_label(world: World, expected_ids: tuple[str, ...]) -> str | None:
    """The label of the lone answer, or ``None`` when the answer is not singular."""
    return _label_of(world, expected_ids[0]) if len(expected_ids) == 1 else None


def _group_by_src(edges: Iterable[Edge]) -> dict[str, list[str]]:
    """Map each edge source to its sorted, de-duplicated destination ids."""
    grouped: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        grouped[edge.src].add(edge.dst)
    return {src: sorted(dsts) for src, dsts in grouped.items()}


# -- direct_relation: a single edge -----------------------------------------


def _direct_relations(world: World) -> Iterator[_Draft]:
    """One-hop questions over ``reports_to``/``leads``/``member_of``/``owns``/``authored``."""
    # Each spec is (edge type, qtype, difficulty, question template). The
    # template receives the subject's label; the answer is every destination.
    specs = [
        ("reports_to", "who", "easy", "Who does {subject} report to?"),
        ("leads", "what", "easy", "What team or department does {subject} lead?"),
        ("member_of", "what", "easy", "What teams and projects is {subject} a member of?"),
        ("owns", "what", "easy", "What goals does {subject} own?"),
        ("authored", "what", "medium", "What artifacts did {subject} author?"),
    ]
    for edge_type, qtype, difficulty, template in specs:
        grouped = _group_by_src(world.edges_by_type(edge_type))
        for src in sorted(grouped):
            subject = world.get_node(src)
            if subject is None:
                continue
            expected = tuple(grouped[src])
            yield _Draft(
                reasoning_type="direct_relation",
                qtype=qtype,
                question=template.format(subject=_label(subject)),
                expected_ids=expected,
                expected_label=_single_label(world, expected),
                difficulty=difficulty,
            )


# -- transitive: a chain of like edges --------------------------------------


def _ancestors(world: World, start: str, edge_type: str) -> list[str]:
    """Follow ``edge_type`` out-edges from ``start`` to a fixpoint (cycle-safe)."""
    chain: list[str] = []
    seen = {start}
    frontier = start
    while True:
        nxt = [edge.dst for edge in world.out_edges(frontier, edge_type) if edge.dst not in seen]
        if not nxt:
            return chain
        # Deterministic walk: a well-formed hierarchy is single-parent, but if a
        # node has several parents, take the smallest id and record them all.
        for node_id in sorted(nxt):
            seen.add(node_id)
            chain.append(node_id)
        frontier = sorted(nxt)[0]


def _management_chains(world: World) -> Iterator[_Draft]:
    """Each person's full ``reports_to+`` chain (skip-level managers included)."""
    for person in world.nodes_by_type("Person"):
        chain = sorted(_ancestors(world, person.id, "reports_to"))
        if not chain:
            continue
        expected = tuple(chain)
        yield _Draft(
            reasoning_type="transitive",
            qtype="who",
            question=f"Who is in {_label(person)}'s management chain, all the way up?",
            expected_ids=expected,
            expected_label=_single_label(world, expected),
            difficulty="hard" if len(expected) > 1 else "medium",
        )


def _departments(world: World) -> Iterator[_Draft]:
    """Each person's department via ``member_of`` (a team) then ``part_of``."""
    for person in world.nodes_by_type("Person"):
        depts: set[str] = set()
        for membership in world.out_edges(person.id, "member_of"):
            team = world.get_node(membership.dst)
            if team is None or team.type != "Team":
                continue
            for part in world.out_edges(team.id, "part_of"):
                if (dept := world.get_node(part.dst)) is not None and dept.type == "Department":
                    depts.add(dept.id)
        if not depts:
            continue
        expected = tuple(sorted(depts))
        yield _Draft(
            reasoning_type="transitive",
            qtype="which",
            question=f"Which department is {_label(person)} in?",
            expected_ids=expected,
            expected_label=_single_label(world, expected),
            difficulty="medium",
        )


# -- provenance: the answer key ---------------------------------------------


def _provenance(world: World, groundings: Mapping[str, list[str]]) -> Iterator[_Draft]:
    """Which artifacts ground (mention) each entity, from the grounding map."""
    for entity_id in sorted(groundings):
        entity = world.get_node(entity_id)
        if entity is None:
            continue
        artifacts = tuple(sorted({a for a in groundings[entity_id] if a in world}))
        if not artifacts:
            continue
        yield _Draft(
            reasoning_type="provenance",
            qtype="which",
            question=f"Which artifacts mention or ground {_label(entity)}?",
            expected_ids=artifacts,
            expected_label=None,
            difficulty="medium",
        )


# -- aggregation: a count over many nodes -----------------------------------


def _aggregations(world: World) -> Iterator[_Draft]:
    """Counts whose answer is the full set being counted (count = ``len``)."""

    def count_draft(question: str, members: Iterable[str]) -> _Draft | None:
        expected = tuple(sorted(set(members)))
        if not expected:
            return None
        return _Draft(
            reasoning_type="aggregation",
            qtype="count",
            question=question,
            expected_ids=expected,
            expected_label=str(len(expected)),
            difficulty="medium",
        )

    drafts: list[_Draft | None] = []

    # Headcount per team: the people who are member_of the team.
    for team in world.nodes_by_type("Team"):
        members = [edge.src for edge in world.in_edges(team.id, "member_of")]
        drafts.append(count_draft(f"How many people are on {_label(team)}?", members))

    # Direct reports per manager: the people who report_to them.
    for person in world.nodes_by_type("Person"):
        reports = [edge.src for edge in world.in_edges(person.id, "reports_to")]
        drafts.append(count_draft(f"How many people report directly to {_label(person)}?", reports))

    # Teams per department: the teams that are part_of the department.
    for dept in world.nodes_by_type("Department"):
        teams = [edge.src for edge in world.in_edges(dept.id, "part_of")]
        drafts.append(count_draft(f"How many teams are in {_label(dept)}?", teams))

    yield from (draft for draft in drafts if draft is not None)


# -- goal_tree: a goal/sub-goal decomposition walk --------------------------


def _subgoals(world: World, goal_id: str) -> list[str]:
    """All descendants of ``goal_id`` via incoming ``subgoal_of`` edges (transitive)."""
    found: list[str] = []
    seen = {goal_id}
    frontier = [goal_id]
    while frontier:
        nxt: list[str] = []
        for parent in frontier:
            for edge in world.in_edges(parent, "subgoal_of"):
                if edge.src not in seen:
                    seen.add(edge.src)
                    found.append(edge.src)
                    nxt.append(edge.src)
        frontier = sorted(nxt)
    return found


def _goal_trees(world: World) -> Iterator[_Draft]:
    """What advances each goal (directly or via subgoals), and each goal's subgoals."""
    for goal in world.nodes_by_type("Goal"):
        label = _label(goal)

        # Advancers of the goal itself and of every subgoal beneath it.
        targets = [goal.id, *_subgoals(world, goal.id)]
        advancers = {
            edge.src for target in targets for edge in world.in_edges(target, "advances_goal")
        }
        if advancers:
            expected = tuple(sorted(advancers))
            yield _Draft(
                reasoning_type="goal_tree",
                qtype="what",
                question=f"What advances the goal '{label}', directly or through its subgoals?",
                expected_ids=expected,
                expected_label=_single_label(world, expected),
                difficulty="hard",
            )

        # Direct subgoals of the goal.
        subgoals = sorted(edge.src for edge in world.in_edges(goal.id, "subgoal_of"))
        if subgoals:
            expected = tuple(subgoals)
            yield _Draft(
                reasoning_type="goal_tree",
                qtype="what",
                question=f"What are the subgoals of the goal '{label}'?",
                expected_ids=expected,
                expected_label=_single_label(world, expected),
                difficulty="medium",
            )


# -- assembly ---------------------------------------------------------------


def build_benchmark(world: World, groundings: Mapping[str, list[str]]) -> Benchmark:
    """Build the full benchmark from the gold ``world`` and its grounding map.

    ``groundings`` maps an entity node id to the artifact node ids that mention
    it (see :func:`load_groundings`). The result is deterministic: drafts from
    every reasoning family are collected, sorted by a stable key, and assigned
    content-hashed ids, so the same inputs always yield a byte-identical
    benchmark.
    """
    drafts: list[_Draft] = [
        *_direct_relations(world),
        *_management_chains(world),
        *_departments(world),
        *_provenance(world, groundings),
        *_aggregations(world),
        *_goal_trees(world),
    ]
    drafts.sort(key=_Draft.sort_key)
    return Benchmark.of(draft.to_pair() for draft in drafts)


def load_world_from_run(run_dir: str | Path) -> World:
    """Reconstruct the gold :class:`World` from a run's ``kg/{nodes,edges}.jsonl``."""
    kg = Path(run_dir) / "kg"
    world = World()
    for line in (kg / "nodes.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            world.add_node(Node.from_dict(json.loads(line)))
    for line in (kg / "edges.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            world.add_edge(Edge.from_dict(json.loads(line)))
    return world


def load_groundings(run_dir: str | Path, world: World) -> dict[str, list[str]]:
    """Map each entity id to the artifact node ids that mention it (``kg/mentions.jsonl``).

    The answer key on disk records mentions by artifact *path*; this resolves
    each path to its :class:`~enterprise_sim.core.world.Node` id via the
    ``Artifact`` nodes' ``path`` property, so the provenance answers are real KG
    node ids.
    """
    by_path = {
        node.props["path"]: node.id
        for node in world.nodes_by_type("Artifact")
        if "path" in node.props
    }
    groundings: dict[str, set[str]] = defaultdict(set)
    mentions = Path(run_dir) / "kg" / "mentions.jsonl"
    for line in mentions.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        artifact_id = by_path.get(record["artifact_path"])
        if artifact_id is not None:
            groundings[record["entity_id"]].add(artifact_id)
    return {entity_id: sorted(artifacts) for entity_id, artifacts in groundings.items()}


def generate(run_dir: str | Path | None = None) -> Benchmark:
    """Generate the KG-QA benchmark, deterministically.

    With ``run_dir``, read the gold graph and answer key from that existing run
    directory. Without it, execute a fresh golden run (deterministic ``fake``
    backend, no network) into a throwaway directory so the gold graph and the
    answer key are guaranteed to agree, then discard the directory.
    """
    with contextlib.ExitStack() as stack:
        if run_dir is None:
            from enterprise_sim.benchmark.fixtures import golden_run

            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="esim-bench-gen-"))
            result = golden_run(tmp)
            world = result.world
            run_dir = result.run_dir
        else:
            world = load_world_from_run(run_dir)
        groundings = load_groundings(run_dir, world)
        return build_benchmark(world, groundings)
