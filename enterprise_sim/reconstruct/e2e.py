"""One-command reconstruction eval: build → fidelity → reason → report (spec 0003).

The attribution eval (docs/RECONSTRUCT.md) is a chain of six CLI steps —
``bench generate`` → ``reconstruct build`` → ``reconstruct fidelity`` → three
reason slots (oracle / reconstructed / rag) → ``reconstruct report`` — with four
intermediate files to keep in sync. ``scripts/reconstruct_eval.sh`` wrapped that
chain in bash; this module hoists the same orchestration into a testable,
in-process library function so the chain has a real CLI surface
(``enterprise-sim reconstruct e2e``) and a machine-readable ``summary.json`` the
baseline harness (spec 0003 slice 2) consumes.

Every underlying step is an existing library function driven in-process (no
subprocess fan-out): :func:`~enterprise_sim.benchmark.generate.generate`,
:func:`~enterprise_sim.reconstruct.build.run_pipeline`,
:func:`~enterprise_sim.reconstruct.fidelity.score_fidelity`,
:func:`~enterprise_sim.benchmark.runners.rag.run_rag`, the graph agent
(:func:`~enterprise_sim.benchmark.runners.graph_agent.run_benchmark`), and
:func:`~enterprise_sim.reconstruct.attribution.build_attribution`. FIDELITY and
REPORT are pure/keyless; the three reason slots need a real model/key (or, in
``--keyless-smoke`` mode, the deterministic ``fake`` backend stands them all in).

``--keyless-smoke`` (``keyless_smoke=True``) forces ``backend="fake"`` and
substitutes one keyless RAG prediction for all three reason slots — proving the
plumbing end to end with no key, exactly as ``reconstruct_eval.sh --keyless-smoke``
did. The numbers in that mode are wiring stand-ins, NOT an eval result, and
``summary.json`` records ``"mode": "keyless-smoke"`` with a loud note to match.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enterprise_sim.reconstruct.attribution import Attribution
from enterprise_sim.reconstruct.extract import HAIKU_MODEL
from enterprise_sim.reconstruct.fidelity import FidelityReport

#: The reason-slot model — the graph agent / RAG answer model. Mirrors the
#: ``reconstruct reason`` / ``reconstruct_eval.sh`` default (the build/extraction
#: step keeps its own :data:`HAIKU_MODEL` default, as the shell script did).
DEFAULT_REASON_MODEL = "claude-sonnet-4-6"

# The artifact set written under ``--out`` (mirrors ``reconstruct_eval.sh``), plus
# ``summary.json`` (this module's addition). Named here so the CLI/tests can assert
# the layout without re-deriving it.
BENCH_FILE = "bench.jsonl"
RECON_DIR = "recon"
FIDELITY_FILE = "fidelity.json"
PRED_ORACLE_FILE = "pred.oracle.jsonl"
PRED_RECONSTRUCTED_FILE = "pred.reconstructed.jsonl"
PRED_RAG_FILE = "pred.rag.jsonl"
ATTRIBUTION_FILE = "attribution.md"
SUMMARY_FILE = "summary.json"

# The loud stand-ins note, emitted to stderr and carried in ``summary.json`` so a
# keyless-smoke run can never be mistaken for an eval result.
_SMOKE_NOTE = "--keyless-smoke numbers are wiring stand-ins, NOT an eval."


@dataclass(frozen=True)
class E2EResult:
    """The outcome of :func:`run_e2e`: where it landed plus the summary payload.

    Attributes:
        out_dir: The output dir every artifact (and ``summary.json``) was written to.
        mode: ``"keyless-smoke"`` or ``"eval"`` — the run's provenance flag.
        backend: The LLM backend the reason slots used (``"fake"`` in smoke mode).
        model: The reason-slot model recorded in the summary.
        run_id: The gold run whose corpus was reconstructed (deterministic for the
            fresh golden run, so ``summary.json`` reproduces byte-for-byte).
        fidelity: The reconstruction's :class:`FidelityReport` against the gold KG.
        attribution: The three-system :class:`Attribution` (oracle/reconstructed/rag).
        summary: The exact dict serialized to ``summary.json`` (sorted-keys JSON).
    """

    out_dir: Path
    mode: str
    backend: str
    model: str
    run_id: str
    fidelity: FidelityReport
    attribution: Attribution
    summary: dict[str, Any]


def _say(message: str) -> None:
    """Emit a step banner to stderr (progress only; never part of ``summary.json``)."""
    print(f"\n=== {message} ===", file=sys.stderr)


def _round(value: float) -> float:
    """Round a metric to 6 decimals for the summary (matches the baseline convention).

    The fake-cell metrics are pure functions of a byte-reproducible run and a pure
    scorer, so rounding here only guards ``summary.json`` against last-ulp noise
    from a summation reorder — it never hides real metric movement (spec 0003 §2).
    """
    return round(value, 6)


def _gold_grounding_by_path(run_dir: Path) -> dict[str, list[str]] | None:
    """Gold grounding answer key as ``{entity id → artifact paths}`` for provenance fidelity.

    Mirrors ``enterprise_sim.cli._gold_grounding_by_path`` so ``e2e``'s
    ``fidelity.json`` carries the same provenance numbers ``reconstruct fidelity
    --json`` would: it reuses the benchmark's provenance key
    (:func:`enterprise_sim.benchmark.generate.load_groundings`) and rewrites the
    grounding artifact node ids to the ``path`` prop the reconstruction joins on.
    Returns ``None`` when the run carries no ``kg/mentions.jsonl`` (provenance is
    then simply not scored) rather than failing the whole eval.
    """
    from enterprise_sim.benchmark.generate import load_groundings, load_world_from_run

    if not (run_dir / "kg" / "mentions.jsonl").is_file():
        return None
    gold = load_world_from_run(run_dir)
    path_of = {
        node.id: node.props["path"]
        for node in gold.nodes_by_type("Artifact")
        if "path" in node.props
    }
    grounding: dict[str, list[str]] = {}
    for entity_id, artifact_ids in load_groundings(run_dir, gold).items():
        paths = sorted({path_of[aid] for aid in artifact_ids if aid in path_of})
        if paths:
            grounding[entity_id] = paths
    return grounding


def _build_summary(
    *,
    mode: str,
    backend: str,
    model: str,
    run_id: str,
    fidelity: FidelityReport,
    attribution: Attribution,
) -> dict[str, Any]:
    """Project the eval's reports into the ``summary.json`` payload.

    Sorted-keys, timestamp-free: ``{mode, backend, model, run_id}`` provenance, the
    fidelity headline metrics (the same key set the baseline cells pin), the
    per-system answer F1, and the understanding/reasoning/total gaps. In keyless
    smoke mode it also carries the loud stand-ins ``note``.
    """
    provenance_f1 = fidelity.provenance.overall.f1 if fidelity.provenance is not None else 0.0
    gap = attribution.gap()
    summary: dict[str, Any] = {
        "mode": mode,
        "backend": backend,
        "model": model,
        "run_id": run_id,
        "fidelity": {
            "node_f1": _round(fidelity.nodes.overall.f1),
            "node_precision": _round(fidelity.nodes.overall.precision),
            "node_recall": _round(fidelity.nodes.overall.recall),
            "edge_f1": _round(fidelity.edges.overall.f1),
            "edge_precision": _round(fidelity.edges.overall.precision),
            "edge_recall": _round(fidelity.edges.overall.recall),
            "provenance_f1": _round(provenance_f1),
            "over_merges": fidelity.entity_resolution.over_merges,
            "under_merges": fidelity.entity_resolution.under_merges,
            "reconstructed_nodes": fidelity.reconstructed_node_count,
            "reconstructed_edges": fidelity.reconstructed_edge_count,
        },
        "answer_f1": {
            "oracle": _round(attribution.f1(attribution.oracle)),
            "reconstructed": _round(attribution.f1(attribution.reconstructed)),
            "rag": _round(attribution.f1(attribution.rag)),
        },
        "gaps": {
            "understanding": _round(gap.understanding),
            "reasoning": _round(gap.reasoning),
            "total": _round(gap.total),
        },
    }
    if mode == "keyless-smoke":
        summary["note"] = _SMOKE_NOTE
    return summary


def run_e2e(
    out_dir: str | Path,
    *,
    run_dir: str | Path | None = None,
    backend: str = "anthropic_api",
    model: str = DEFAULT_REASON_MODEL,
    limit: int | None = None,
    keyless_smoke: bool = False,
    use_bedrock: bool = False,
    aws_region: str | None = None,
) -> E2EResult:
    """Run the full attribution eval in-process and write every artifact under ``out_dir``.

    The chain: a fresh golden run (when ``run_dir`` is ``None``) → ``bench generate``
    → ``reconstruct build`` → ``fidelity`` → three reason slots → ``report``, plus a
    machine-readable ``summary.json``. Writes ``bench.jsonl``, ``recon/``,
    ``fidelity.json``, ``pred.{oracle,reconstructed,rag}.jsonl``, ``attribution.md``,
    and ``summary.json`` under ``out_dir``.

    ``keyless_smoke`` forces ``backend="fake"`` and substitutes one keyless RAG
    prediction for all three reason slots (mirroring
    ``reconstruct_eval.sh:81-90``) — the plumbing runs end to end with no key, but
    the numbers are wiring stand-ins, so ``summary.json`` records
    ``"mode": "keyless-smoke"`` and the note lands on stderr too.

    The keyed path (``backend`` ``anthropic_api`` / ``bedrock``) runs the graph
    agent on the gold KG (oracle) and on the reconstructed KG (reconstructed), and
    the RAG baseline (rag); ``use_bedrock`` / ``aws_region`` route the two graph-
    agent slots to Amazon Bedrock (the E1 parity the shell script lacked). The
    reason steps reuse the already-gated runners, which exit cleanly on a missing
    key/creds — surfaced here as the underlying ``RuntimeError``.
    """
    from enterprise_sim.benchmark.generate import generate, load_world_from_run
    from enterprise_sim.benchmark.runners.rag import run_rag
    from enterprise_sim.benchmark.schema import Benchmark
    from enterprise_sim.core.llm import LLMConfig, build_client
    from enterprise_sim.reconstruct.attribution import build_attribution
    from enterprise_sim.reconstruct.build import BuildConfig, run_pipeline
    from enterprise_sim.reconstruct.fidelity import score_fidelity

    if keyless_smoke:
        backend = "fake"
    mode = "keyless-smoke" if keyless_smoke else "eval"

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Step 0: a golden run to reconstruct (fresh, deterministic fake sim) unless the
    # caller supplied one — exactly what reconstruct_eval.sh:60-64 does. Reusing the
    # golden fixture keeps the pin single-sourced (no second pinned config).
    if run_dir is None:
        from enterprise_sim.benchmark.fixtures import golden_run

        _say("golden run (fresh)")
        result = golden_run(out / "runs")
        gold_run_dir = result.run_dir
        run_id = result.run_id
    else:
        gold_run_dir = Path(run_dir)
        run_id = gold_run_dir.name
    print(f"run: {gold_run_dir}", file=sys.stderr)

    # A shared benchmark generated from that run's gold graph.
    _say("bench generate")
    benchmark = generate(gold_run_dir)
    benchmark.write_jsonl(out / BENCH_FILE)

    # 1. BUILD — reconstruct + persist the KG once (gated extract/resolve on backend).
    _say(f"build (backend={backend})")
    client = build_client(LLMConfig(backend=backend, model=HAIKU_MODEL))
    kg = run_pipeline(str(gold_run_dir), client, model=HAIKU_MODEL, config=BuildConfig())
    recon_dir = out / RECON_DIR
    kg.write(recon_dir)

    # 2. FIDELITY — score the reconstruction against the gold graph (keyless).
    _say("fidelity")
    gold = load_world_from_run(gold_run_dir)
    fidelity = score_fidelity(kg, gold, gold_grounding=_gold_grounding_by_path(gold_run_dir))
    (out / FIDELITY_FILE).write_text(fidelity.to_json(), encoding="utf-8")

    # 3. REASON — three prediction files: oracle / reconstructed / rag.
    benchmark = Benchmark.read_jsonl(out / BENCH_FILE)
    if keyless_smoke:
        # Keyless wiring smoke: one keyless RAG prediction stands in for all three
        # slots so REPORT runs without a key. NOT a real eval — proves the plumbing.
        _say("reason (keyless smoke: rag stands in for all three slots)")
        rag_client = build_client(LLMConfig(backend="fake", model=model))
        predictions = run_rag(gold_run_dir, benchmark, rag_client, world=gold)
        predictions.write_jsonl(out / PRED_RAG_FILE)
        predictions.write_jsonl(out / PRED_ORACLE_FILE)
        predictions.write_jsonl(out / PRED_RECONSTRUCTED_FILE)
        print(f"NOTE: {_SMOKE_NOTE}", file=sys.stderr)
        oracle = reconstructed = rag = predictions
    else:
        from enterprise_sim.benchmark.runners.graph_agent import (
            GraphRunner,
            run_benchmark,
        )
        from enterprise_sim.benchmark.runners.projection import GraphModel
        from enterprise_sim.reconstruct.build import project_with_groundings

        _say("reason: oracle (graph agent on gold KG)")
        oracle = run_benchmark(
            benchmark,
            run_dir=str(gold_run_dir),
            model=model,
            limit=limit,
            use_bedrock=use_bedrock,
            aws_region=aws_region,
        )
        oracle.write_jsonl(out / PRED_ORACLE_FILE)

        _say("reason: reconstructed (same agent on reconstructed KG)")
        gold_artifact_ids = {
            node.props["path"]: node.id
            for node in gold.nodes_by_type("Artifact")
            if isinstance(node.props.get("path"), str)
        }
        world, groundings = project_with_groundings(kg, gold_artifact_ids)
        runner = GraphRunner(GraphModel.from_world(world, groundings))
        try:
            reconstructed = run_benchmark(
                benchmark,
                runner=runner,
                model=model,
                limit=limit,
                use_bedrock=use_bedrock,
                aws_region=aws_region,
            )
        finally:
            runner.close()
        reconstructed.write_jsonl(out / PRED_RECONSTRUCTED_FILE)

        _say("reason: rag (corpus baseline)")
        rag_client = build_client(LLMConfig(backend=backend, model=model))
        rag = run_rag(gold_run_dir, benchmark, rag_client, world=gold)
        rag.write_jsonl(out / PRED_RAG_FILE)

    # 4. REPORT — attribute the graph's advantage (understanding vs reasoning).
    _say("report")
    attribution = build_attribution(
        benchmark, oracle=oracle, reconstructed=reconstructed, rag=rag, fidelity=fidelity
    )
    from enterprise_sim.reconstruct.attribution import render_markdown

    (out / ATTRIBUTION_FILE).write_text(render_markdown(attribution), encoding="utf-8")

    summary = _build_summary(
        mode=mode,
        backend=backend,
        model=model,
        run_id=run_id,
        fidelity=fidelity,
        attribution=attribution,
    )
    import json

    (out / SUMMARY_FILE).write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return E2EResult(
        out_dir=out,
        mode=mode,
        backend=backend,
        model=model,
        run_id=run_id,
        fidelity=fidelity,
        attribution=attribution,
        summary=summary,
    )
