"""The graph-agent runner: a Claude agent answers the benchmark over the KG (esim-uzc.4).

This is the system-under-test that *reasons* over the gold knowledge graph. It
loads the graph into the two embedded engines
(:class:`~enterprise_sim.benchmark.runners.engines.KuzuEngine` for Cypher,
:class:`~enterprise_sim.benchmark.runners.engines.OxigraphEngine` for SPARQL with
the materialized ontology) and, for each
:class:`~enterprise_sim.benchmark.schema.QAPair`, runs a Claude agent that has
three tools — ``cypher_query``, ``sparql_query``, ``search_nodes`` — plus a
``submit_answer`` tool to report the predicted node ids. The result is a
:class:`~enterprise_sim.benchmark.score.Predictions` set the grader scores.

The engine layer (:class:`GraphRunner`) is fully usable **without** an API key —
schema descriptions, node search, and query execution all work offline, which is
how the keyless tests prove the engines. Only :func:`run_benchmark` (the agent
loop) needs ``ANTHROPIC_API_KEY`` and the optional ``claude-agent-sdk`` dependency;
both are imported lazily so importing this module never requires them.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from enterprise_sim.benchmark.runners.engines import KuzuEngine, OxigraphEngine
from enterprise_sim.benchmark.runners.projection import GraphModel
from enterprise_sim.benchmark.runners.reference import REFERENCES
from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.benchmark.score import Prediction, Predictions

if TYPE_CHECKING:
    from enterprise_sim.core.world import World

# Default model for the agent (overridable per call / via the CLI).
DEFAULT_MODEL = "claude-sonnet-4-6"
# Default per-question tool-call budget — enough to search, query, and submit.
DEFAULT_MAX_TURNS = 12
# Cap rows returned to the agent so a broad query cannot blow up the context.
_MAX_ROWS = 50


class GraphRunner:
    """The offline engine layer: both query engines plus node search over one KG.

    Build one from a :class:`GraphModel`; it owns a
    :class:`~enterprise_sim.benchmark.runners.engines.KuzuEngine` and an
    :class:`~enterprise_sim.benchmark.runners.engines.OxigraphEngine`. Everything
    here is deterministic and key-free; the agent loop layered on top
    (:func:`run_benchmark`) is the only part that needs an API key.
    """

    def __init__(self, model: GraphModel) -> None:
        self.model = model
        self.kuzu = KuzuEngine.build(model)
        self.oxigraph = OxigraphEngine.build(model)

    @classmethod
    def from_world(
        cls,
        world: World,
        groundings: dict[str, list[str]] | None = None,
    ) -> GraphRunner:
        """Build a runner from a gold :class:`~enterprise_sim.core.world.World`."""
        return cls(GraphModel.from_world(world, groundings))

    def search_nodes(self, text: str, limit: int = 20) -> list[dict[str, str]]:
        """Case-insensitive substring search over node id, label, aliases, and props.

        Returns up to ``limit`` matches (sorted by id) as ``{id, type, label}`` —
        the agent's way to resolve a question's subject to a node id.
        """
        needle = text.lower()
        out: list[dict[str, str]] = []
        for node in self.model.nodes:
            haystack = [node.id, node.label, *node.aliases]
            haystack.extend(str(v) for v in node.props.values() if isinstance(v, str))
            if any(needle in h.lower() for h in haystack):
                out.append({"id": node.id, "type": node.type, "label": node.label})
            if len(out) >= limit:
                break
        return out

    def schema_prompt(self) -> str:
        """The full schema briefing handed to the agent (both engines + examples)."""
        examples = "\n".join(
            f"  - {ref.description} ({ref.reasoning_type}):\n"
            f"      Cypher: {ref.cypher('<id>')}\n"
            f"      SPARQL: {ref.sparql('<id>')}"
            for ref in REFERENCES
        )
        return (
            "You answer questions over an enterprise knowledge graph using two engines.\n\n"
            "CYPHER (kuzu) — typed property graph, good for multi-hop traversal:\n"
            f"{self.kuzu.describe_schema()}\n\n"
            "SPARQL (oxigraph) — RDF + a materialized ontology of inferred predicates:\n"
            f"{self.oxigraph.describe_schema(self.model)}\n\n"
            "Worked patterns (substitute the subject node id for <id>):\n"
            f"{examples}\n"
        )

    def close(self) -> None:
        """Release the embedded Cypher database."""
        self.kuzu.close()


def _truncate(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Cap rows at :data:`_MAX_ROWS`; return the kept rows and the dropped count."""
    if len(rows) <= _MAX_ROWS:
        return rows, 0
    return rows[:_MAX_ROWS], len(rows) - _MAX_ROWS


def run_benchmark(
    benchmark: Benchmark,
    *,
    run_dir: str | None = None,
    runner: GraphRunner | None = None,
    model: str = DEFAULT_MODEL,
    limit: int | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> Predictions:
    """Run the graph agent over ``benchmark`` and return its :class:`Predictions`.

    The engines are built **once** and reused for every question (build-once /
    answer-many): from ``runner`` when the caller supplies a pre-built
    :class:`GraphRunner` — e.g. one loaded from a reconstructed KG, whose lifecycle
    the caller then owns — otherwise from the gold KG (a fresh golden run, or
    ``run_dir`` when given, so the graph matches the benchmark's gold answers). One
    agent answers each question (the first ``limit`` if set) against that single
    runner. Requires ``ANTHROPIC_API_KEY`` and ``claude-agent-sdk``; raises
    :class:`RuntimeError` if the key is missing.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("graph runner needs ANTHROPIC_API_KEY (the agent calls the Claude API)")

    import asyncio

    # Build the engines once, before the per-question loop. A caller-supplied runner
    # is left open for the caller to close; one we build here we also close.
    owns_runner = runner is None
    active = _build_runner(run_dir) if runner is None else runner
    pairs = list(benchmark)[: limit if limit is not None else len(benchmark)]
    try:
        rows = asyncio.run(_run_all(active, pairs, model=model, max_turns=max_turns))
    finally:
        if owns_runner:
            active.close()
    return Predictions.of(rows)


def _build_runner(run_dir: str | None) -> GraphRunner:
    """Load the gold KG (+ groundings) and build a :class:`GraphRunner`."""
    from enterprise_sim.benchmark.generate import load_groundings, load_world_from_run

    if run_dir is None:
        import tempfile

        from enterprise_sim.benchmark.fixtures import golden_run

        with tempfile.TemporaryDirectory(prefix="esim-bench-graph-") as tmp:
            result = golden_run(tmp)
            groundings = load_groundings(result.run_dir, result.world)
            return GraphRunner.from_world(result.world, groundings)
    world = load_world_from_run(run_dir)
    groundings = load_groundings(run_dir, world)
    return GraphRunner.from_world(world, groundings)


async def _run_all(
    runner: GraphRunner,
    pairs: list[QAPair],
    *,
    model: str,
    max_turns: int,
) -> list[Prediction]:
    """Answer each pair in turn, returning one :class:`Prediction` apiece."""
    out: list[Prediction] = []
    for pair in pairs:
        predicted = await _predict_pair(runner, pair, model=model, max_turns=max_turns)
        out.append(Prediction(qa_id=pair.id, predicted_ids=tuple(predicted)))
    return out


async def _predict_pair(
    runner: GraphRunner,
    pair: QAPair,
    *,
    model: str,
    max_turns: int,
) -> list[str]:
    """Run one agent over one question; return the node ids it submits (sorted)."""
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        create_sdk_mcp_server,
        query,
        tool,
    )

    answer: dict[str, list[str]] = {"ids": []}

    @tool("cypher_query", "Run a Cypher query against the knowledge graph (kuzu).", {"query": str})
    async def cypher_query(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = runner.kuzu.query(args["query"])
        except Exception as exc:  # surface the engine error to the agent, don't crash
            return _text(f"Cypher error: {exc}")
        rows, dropped = _truncate(result.rows)
        return _text(_format_rows(result.columns, rows, dropped))

    @tool(
        "sparql_query", "Run a SPARQL query against the knowledge graph (oxigraph).", {"query": str}
    )
    async def sparql_query(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = runner.oxigraph.query(args["query"])
        except Exception as exc:
            return _text(f"SPARQL error: {exc}")
        if result.kind == "ask":
            return _text(f"ASK -> {result.boolean}")
        rows, dropped = _truncate(result.rows)
        return _text(_format_rows(result.columns, rows, dropped))

    @tool("search_nodes", "Find node ids by a substring of their name/label/id.", {"text": str})
    async def search_nodes(args: dict[str, Any]) -> dict[str, Any]:
        matches = runner.search_nodes(args["text"])
        if not matches:
            return _text("no matching nodes")
        lines = [f"{m['id']}  ({m['type']})  {m['label']}" for m in matches]
        return _text("\n".join(lines))

    @tool(
        "submit_answer",
        "Submit the final answer: the node ids that answer the question.",
        {"node_ids": list},
    )
    async def submit_answer(args: dict[str, Any]) -> dict[str, Any]:
        ids = [str(x) for x in args.get("node_ids", [])]
        answer["ids"] = ids
        return _text(f"recorded {len(ids)} node id(s)")

    server = create_sdk_mcp_server(
        "graph",
        tools=[cypher_query, sparql_query, search_nodes, submit_answer],
    )
    options = ClaudeAgentOptions(
        model=model,
        max_turns=max_turns,
        system_prompt=runner.schema_prompt(),
        mcp_servers={"graph": server},
        allowed_tools=[
            "mcp__graph__cypher_query",
            "mcp__graph__sparql_query",
            "mcp__graph__search_nodes",
            "mcp__graph__submit_answer",
        ],
    )
    prompt = (
        f"Question: {pair.question}\n\n"
        "Find the answer using the cypher_query / sparql_query / search_nodes tools, "
        "then call submit_answer with the list of knowledge-graph node ids that answer "
        "it. Use search_nodes to resolve names to node ids first. The answer is a SET of "
        "node ids; submit an empty list only if nothing matches."
    )
    async for _message in query(prompt=prompt, options=options):
        pass
    return sorted(set(answer["ids"]))


def _text(text: str) -> dict[str, Any]:
    """Wrap plain text as an MCP tool result."""
    return {"content": [{"type": "text", "text": text}]}


def _format_rows(columns: list[str], rows: list[dict[str, Any]], dropped: int) -> str:
    """Render query rows compactly for the agent (header + values + drop notice)."""
    if not rows:
        return "(0 rows)"
    header = " | ".join(columns)
    body = "\n".join(" | ".join(str(row.get(c, "")) for c in columns) for row in rows)
    note = f"\n... ({dropped} more rows omitted)" if dropped else ""
    return f"{header}\n{body}{note}"
