"""Attribution-report tests (esim-nc6.8): understanding vs reasoning over 3 systems.

Exercises the pure, keyless attribution layer end to end — no LLM, no graph
engine:

* **scoring + the gap identity** — the three prediction sets grade correctly and
  the decomposition ``understanding + reasoning == total`` holds overall and per
  reasoning type;
* **fidelity context** — :class:`FidelityContext` projects a live
  :class:`FidelityReport` and round-trips through its JSON;
* **rendering** — the markdown carries the overall table, the per-type F1
  breakdown, the signed attribution table, and the fidelity block (only when
  supplied); rendering is deterministic;
* the ``reconstruct report`` CLI end to end (with and without ``--fidelity``).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.benchmark.score import Predictions
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.reconstruct import (
    FidelityContext,
    ReconstructedKG,
    build_attribution,
    render_markdown,
    score_fidelity,
)
from enterprise_sim.reconstruct.attribution import build_report

_TS = datetime(2026, 1, 2, 3, 4, 5)


def _pair(qid: str, expected: tuple[str, ...], reasoning: str = "direct_relation") -> QAPair:
    return QAPair(
        id=qid,
        question=f"q-{qid}?",
        qtype="who",
        reasoning_type=reasoning,
        expected_ids=expected,
    )


def _preds(mapping: dict[str, list[str]]) -> Predictions:
    return Predictions.from_mapping(mapping)


# A three-question benchmark spanning two reasoning types. The gold answer of each
# question is a single node id equal to its qid, so predictions are easy to read.
def _bench() -> Benchmark:
    return Benchmark.of(
        [
            _pair("q1", ("q1",), reasoning="direct_relation"),
            _pair("q2", ("q2",), reasoning="direct_relation"),
            _pair("q3", ("q3",), reasoning="transitive"),
        ]
    )


# --------------------------------------------------------------------------- #
# Scoring + the attribution identity.
# --------------------------------------------------------------------------- #


def test_perfect_oracle_beats_partial_reconstructed_beats_rag() -> None:
    bench = _bench()
    attribution = build_attribution(
        bench,
        oracle=_preds({"q1": ["q1"], "q2": ["q2"], "q3": ["q3"]}),  # 3/3
        reconstructed=_preds({"q1": ["q1"], "q2": ["q2"], "q3": ["x"]}),  # 2/3
        rag=_preds({"q1": ["q1"], "q2": ["y"], "q3": ["z"]}),  # 1/3
    )
    assert attribution.oracle.overall.macro_f1 == 1.0
    assert attribution.reconstructed.overall.macro_f1 == 2 / 3
    assert attribution.rag.overall.macro_f1 == 1 / 3


def test_gap_decomposition_is_an_identity_overall_and_per_type() -> None:
    bench = _bench()
    attribution = build_attribution(
        bench,
        oracle=_preds({"q1": ["q1"], "q2": ["q2"], "q3": ["q3"]}),
        reconstructed=_preds({"q1": ["q1"], "q2": ["x"], "q3": ["q3"]}),
        rag=_preds({"q1": ["y"], "q2": ["z"], "q3": ["q3"]}),
    )
    for reasoning in (None, "direct_relation", "transitive"):
        gap = attribution.gap(reasoning)
        assert gap.understanding + gap.reasoning == gap.total


def test_understanding_gap_is_oracle_minus_reconstructed() -> None:
    bench = _bench()
    attribution = build_attribution(
        bench,
        oracle=_preds({"q1": ["q1"], "q2": ["q2"], "q3": ["q3"]}),  # 1.0
        reconstructed=_preds({"q1": ["q1"], "q2": ["q2"], "q3": ["x"]}),  # 2/3
        rag=_preds({}),  # 0.0
    )
    gap = attribution.gap()
    assert gap.understanding == 1.0 - 2 / 3
    assert gap.reasoning == 2 / 3
    assert gap.total == 1.0


def test_missing_reasoning_type_scores_zero_not_error() -> None:
    # A benchmark type no system answered still resolves (empty aggregate -> 0.0).
    bench = Benchmark.of([_pair("q1", ("q1",), reasoning="provenance")])
    attribution = build_attribution(
        bench, oracle=_preds({}), reconstructed=_preds({}), rag=_preds({})
    )
    gap = attribution.gap("aggregation")  # a type absent from the benchmark
    assert gap.understanding == 0.0
    assert gap.reasoning == 0.0
    assert gap.total == 0.0


# --------------------------------------------------------------------------- #
# id-alignment of the reconstructed system (esim-d1c.1 / e9z).
# --------------------------------------------------------------------------- #

# The reconstructed agent names an entity by a different id than gold; align it.
_RECON_ID = "recon:node-7"
_GOLD_ID = "person:ada"


def test_alignment_credits_only_the_reconstructed_system() -> None:
    # All three answer the same entity, but reconstructed uses a different id.
    bench = Benchmark.of([_pair("q1", (_GOLD_ID,))])
    oracle = _preds({"q1": [_GOLD_ID]})
    reconstructed = _preds({"q1": [_RECON_ID]})
    rag = _preds({"q1": [_GOLD_ID]})

    raw = build_attribution(bench, oracle=oracle, reconstructed=reconstructed, rag=rag)
    assert raw.reconstructed.overall.macro_f1 == 0.0
    assert raw.aligned is False

    aligned = build_attribution(
        bench,
        oracle=oracle,
        reconstructed=reconstructed,
        rag=rag,
        alignment={_RECON_ID: _GOLD_ID},
    )
    assert aligned.reconstructed.overall.macro_f1 == 1.0  # credited on the gold basis
    assert aligned.oracle.overall.macro_f1 == 1.0  # unchanged (already gold)
    assert aligned.rag.overall.macro_f1 == 1.0  # unchanged (already gold)
    assert aligned.aligned is True


def test_alignment_does_not_touch_oracle_or_rag_ids() -> None:
    # A mapping that would rewrite a gold id is applied to reconstructed only, so
    # oracle/rag keep scoring on their raw (gold) ids.
    bench = Benchmark.of([_pair("q1", (_GOLD_ID,))])
    oracle = _preds({"q1": [_GOLD_ID]})
    rag = _preds({"q1": [_GOLD_ID]})
    aligned = build_attribution(
        bench,
        oracle=oracle,
        reconstructed=_preds({"q1": [_RECON_ID]}),
        rag=rag,
        alignment={_GOLD_ID: "some:other", _RECON_ID: _GOLD_ID},
    )
    assert aligned.oracle.overall.macro_f1 == 1.0
    assert aligned.rag.overall.macro_f1 == 1.0


def test_render_notes_alignment_mode_only_when_aligned() -> None:
    bench = Benchmark.of([_pair("q1", (_GOLD_ID,))])
    oracle = _preds({"q1": [_GOLD_ID]})
    reconstructed = _preds({"q1": [_RECON_ID]})
    rag = _preds({"q1": [_GOLD_ID]})
    aligned = render_markdown(
        build_attribution(
            bench,
            oracle=oracle,
            reconstructed=reconstructed,
            rag=rag,
            alignment={_RECON_ID: _GOLD_ID},
        )
    )
    raw = render_markdown(
        build_attribution(bench, oracle=oracle, reconstructed=reconstructed, rag=rag)
    )
    assert "id-alignment" in aligned
    assert "id-alignment" not in raw


# --------------------------------------------------------------------------- #
# Fidelity context projection + round-trip.
# --------------------------------------------------------------------------- #


def _gold_world() -> World:
    world = World()
    world.add_node(
        Node(id="person:ada", type="Person", created_at=_TS, props={"name": "Ada"}, aliases=["Ada"])
    )
    world.add_node(
        Node(
            id="company:acme",
            type="Company",
            created_at=_TS,
            props={"name": "Acme"},
            aliases=["Acme"],
        )
    )
    world.add_edge(
        Edge(id="e:works_at", type="works_at", src="person:ada", dst="company:acme", created_at=_TS)
    )
    return world


def _kg_from_world(world: World) -> ReconstructedKG:
    kg = ReconstructedKG()
    for node in world.nodes():
        kg.add_node(node)
    for edge in world.edges():
        kg.add_edge(edge)
    return kg


def test_fidelity_context_from_report_matches_gold_vs_gold() -> None:
    gold = _gold_world()
    report = score_fidelity(_kg_from_world(gold), gold)
    context = FidelityContext.from_report(report)
    assert context.node_f1 == 1.0
    assert context.edge_f1 == 1.0
    assert context.over_merges == 0
    assert context.under_merges == 0
    assert context.reconstructed_nodes == context.gold_nodes == 2
    assert context.reconstructed_edges == context.gold_edges == 1


def test_fidelity_context_round_trips_through_json_dict() -> None:
    gold = _gold_world()
    report = score_fidelity(_kg_from_world(gold), gold)
    from_report = FidelityContext.from_report(report)
    from_dict = FidelityContext.from_dict(json.loads(report.to_json()))
    assert from_report == from_dict


# --------------------------------------------------------------------------- #
# Markdown rendering.
# --------------------------------------------------------------------------- #


def _render(bench: Benchmark, fidelity: FidelityContext | None = None) -> str:
    attribution = build_attribution(
        bench,
        oracle=_preds({"q1": ["q1"], "q2": ["q2"], "q3": ["q3"]}),
        reconstructed=_preds({"q1": ["q1"], "q2": ["q2"], "q3": ["x"]}),
        rag=_preds({"q1": ["q1"], "q2": ["y"], "q3": ["z"]}),
        fidelity=fidelity,
    )
    return render_markdown(attribution)


def test_render_has_title_and_all_sections() -> None:
    text = _render(_bench())
    assert text.startswith("# Reconstruct attribution report")
    assert "## Overall" in text
    assert "## By reasoning type" in text
    assert "## Attribution: understanding vs reasoning" in text


def test_render_lists_the_three_systems_and_reasoning_types() -> None:
    text = _render(_bench())
    assert "oracle" in text
    assert "reconstructed" in text
    assert "rag" in text
    assert "direct_relation" in text
    assert "transitive" in text


def test_attribution_table_uses_signed_gaps() -> None:
    text = _render(_bench())
    # overall: oracle 1.0, reconstructed 2/3, rag 1/3 -> understanding +0.333.
    assert "+0.333" in text


def test_fidelity_block_present_only_when_supplied() -> None:
    gold = _gold_world()
    context = FidelityContext.from_report(score_fidelity(_kg_from_world(gold), gold))
    with_ctx = _render(_bench(), fidelity=context)
    without_ctx = _render(_bench())
    assert "## Reconstruction fidelity (context)" in with_ctx
    assert "node F1 **1.000**" in with_ctx
    assert "## Reconstruction fidelity (context)" not in without_ctx


def test_signed_formatter_normalizes_negative_zero() -> None:
    # Identical systems -> every gap is exactly zero and must render as +0.000.
    bench = _bench()
    same = _preds({"q1": ["q1"], "q2": ["q2"], "q3": ["q3"]})
    attribution = build_attribution(bench, oracle=same, reconstructed=same, rag=same)
    text = render_markdown(attribution)
    assert "-0.000" not in text
    assert "+0.000" in text


def test_empty_benchmark_renders_without_type_rows() -> None:
    text = _render(Benchmark())
    assert "_No questions in the benchmark._" in text


def test_rendering_is_deterministic() -> None:
    bench = _bench()
    attribution = build_attribution(
        bench,
        oracle=_preds({"q1": ["q1"]}),
        reconstructed=_preds({"q1": ["q1"]}),
        rag=_preds({}),
    )
    assert render_markdown(attribution) == render_markdown(attribution)


def test_build_report_matches_two_step_render() -> None:
    bench = _bench()
    oracle = _preds({"q1": ["q1"], "q2": ["q2"], "q3": ["q3"]})
    reconstructed = _preds({"q1": ["q1"], "q2": ["q2"], "q3": ["x"]})
    rag = _preds({"q1": ["q1"], "q2": ["y"], "q3": ["z"]})
    one_call = build_report(bench, oracle=oracle, reconstructed=reconstructed, rag=rag)
    two_step = render_markdown(
        build_attribution(bench, oracle=oracle, reconstructed=reconstructed, rag=rag)
    )
    assert one_call == two_step


# --------------------------------------------------------------------------- #
# CLI end to end.
# --------------------------------------------------------------------------- #


def test_report_subcommand_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "reconstruct",
            "report",
            "--bench",
            "b",
            "--oracle",
            "o",
            "--reconstructed",
            "r",
            "--rag",
            "g",
        ]
    )
    assert args.func is not None
    assert args.bench == Path("b")


def _write_inputs(tmp_path: Path) -> dict[str, Path]:
    bench = _bench()
    bench_path = tmp_path / "bench.jsonl"
    bench.write_jsonl(bench_path)

    paths = {"bench": bench_path}
    for name, mapping in (
        ("oracle", {"q1": ["q1"], "q2": ["q2"], "q3": ["q3"]}),
        ("reconstructed", {"q1": ["q1"], "q2": ["q2"], "q3": ["x"]}),
        ("rag", {"q1": ["q1"], "q2": ["y"], "q3": ["z"]}),
    ):
        path = tmp_path / f"{name}.jsonl"
        _preds(mapping).write_jsonl(path)
        paths[name] = path
    return paths


def test_report_cli_writes_markdown_and_summary(tmp_path: Path, capsys: Any) -> None:
    inputs = _write_inputs(tmp_path)
    out = tmp_path / "report.md"

    rc = main(
        [
            "reconstruct",
            "report",
            "--bench",
            str(inputs["bench"]),
            "--oracle",
            str(inputs["oracle"]),
            "--reconstructed",
            str(inputs["reconstructed"]),
            "--rag",
            str(inputs["rag"]),
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "# Reconstruct attribution report" in text
    assert "## Attribution: understanding vs reasoning" in text
    # The one-line summary carries the signed overall gaps.
    err = capsys.readouterr().err
    assert "understanding=+0.333" in err
    assert "total=+0.667" in err


def test_report_cli_includes_fidelity_context(tmp_path: Path, capsys: Any) -> None:
    inputs = _write_inputs(tmp_path)
    gold = _gold_world()
    fidelity_path = tmp_path / "fidelity.json"
    fidelity_path.write_text(score_fidelity(_kg_from_world(gold), gold).to_json(), encoding="utf-8")
    out = tmp_path / "report.md"

    rc = main(
        [
            "reconstruct",
            "report",
            "--bench",
            str(inputs["bench"]),
            "--oracle",
            str(inputs["oracle"]),
            "--reconstructed",
            str(inputs["reconstructed"]),
            "--rag",
            str(inputs["rag"]),
            "--fidelity",
            str(fidelity_path),
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    assert "## Reconstruction fidelity (context)" in out.read_text(encoding="utf-8")


def test_report_cli_to_stdout(tmp_path: Path, capsys: Any) -> None:
    inputs = _write_inputs(tmp_path)
    rc = main(
        [
            "reconstruct",
            "report",
            "--bench",
            str(inputs["bench"]),
            "--oracle",
            str(inputs["oracle"]),
            "--reconstructed",
            str(inputs["reconstructed"]),
            "--rag",
            str(inputs["rag"]),
        ]
    )
    assert rc == 0
    assert "# Reconstruct attribution report" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# CLI --align (esim-d1c.1): the reconstructed system's path-named answers.
# --------------------------------------------------------------------------- #

_ALIGN_PATH = "artifacts/init/groom.md"
_ALIGN_GOLD = "artifact:init:groom"


def _write_align_inputs(tmp_path: Path) -> dict[str, Path]:
    """Benchmark + predictions where reconstructed names the artifact by PATH, plus
    the gold run dir and reconstruction dir the alignment map is built from."""
    artifact = Node(
        id=_ALIGN_GOLD,
        type="Artifact",
        created_at=_TS,
        props={"path": _ALIGN_PATH, "name": "groom"},
        aliases=["groom"],
    )

    bench = Benchmark.of([_pair("q1", (_ALIGN_GOLD,), reasoning="provenance")])
    bench_path = tmp_path / "bench.jsonl"
    bench.write_jsonl(bench_path)

    paths = {"bench": bench_path}
    for name, mapping in (
        ("oracle", {"q1": [_ALIGN_GOLD]}),  # gold namespace
        ("reconstructed", {"q1": [_ALIGN_PATH]}),  # path namespace -> 0 raw
        ("rag", {"q1": [_ALIGN_GOLD]}),
    ):
        path = tmp_path / f"{name}.jsonl"
        _preds(mapping).write_jsonl(path)
        paths[name] = path

    run_dir = tmp_path / "gold"
    (run_dir / "kg").mkdir(parents=True)
    (run_dir / "kg" / "nodes.jsonl").write_text(
        json.dumps(artifact.to_dict()) + "\n", encoding="utf-8"
    )
    (run_dir / "kg" / "edges.jsonl").write_text("", encoding="utf-8")
    paths["run"] = run_dir

    recon_dir = tmp_path / "recon"
    kg = ReconstructedKG()
    kg.add_node(artifact)
    kg.write(recon_dir)
    paths["recon"] = recon_dir
    return paths


def test_report_cli_align_flag_registered() -> None:
    args = build_parser().parse_args(
        [
            "reconstruct",
            "report",
            "--bench",
            "b",
            "--oracle",
            "o",
            "--reconstructed",
            "r",
            "--rag",
            "g",
            "--align",
            "--reconstructed-kg",
            "kg",
        ]
    )
    assert args.align is True
    assert args.reconstructed_kg == Path("kg")


def test_report_cli_align_requires_reconstructed_kg(tmp_path: Path, capsys: Any) -> None:
    inputs = _write_align_inputs(tmp_path)
    rc = main(
        [
            "reconstruct",
            "report",
            "--bench",
            str(inputs["bench"]),
            "--oracle",
            str(inputs["oracle"]),
            "--reconstructed",
            str(inputs["reconstructed"]),
            "--rag",
            str(inputs["rag"]),
            "--align",
        ]
    )
    assert rc == 2
    assert "--align requires --reconstructed-kg" in capsys.readouterr().err


def test_report_cli_align_credits_path_named_reconstructed(tmp_path: Path, capsys: Any) -> None:
    inputs = _write_align_inputs(tmp_path)
    out = tmp_path / "report.md"
    rc = main(
        [
            "reconstruct",
            "report",
            "--bench",
            str(inputs["bench"]),
            "--oracle",
            str(inputs["oracle"]),
            "--reconstructed",
            str(inputs["reconstructed"]),
            "--rag",
            str(inputs["rag"]),
            "--align",
            "--reconstructed-kg",
            str(inputs["recon"]),
            "--run",
            str(inputs["run"]),
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    # The reconstructed row is now credited (path -> canonical), erasing the
    # understanding gap; the report notes the alignment mode.
    assert "id-alignment" in text
    assert "understanding=+0.000" in capsys.readouterr().err
