"""Command-line entrypoint: ``enterprise-sim {run, lint, eval}``.

This is an M1 scaffold stub. Subcommands parse arguments and report that they
are not yet implemented; later milestones wire them to the engine, the quality
stack (§13), and the assembly layer.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from enterprise_sim import __version__

if TYPE_CHECKING:
    from enterprise_sim.authoring.sdk import Playbook
    from enterprise_sim.benchmark.schema import Benchmark
    from enterprise_sim.benchmark.score import Predictions
    from enterprise_sim.core.config import RunConfig
    from enterprise_sim.core.llm import LLMClient
    from enterprise_sim.core.world import World

# The single source of truth for every ``--backend`` flag's choices (finding F7).
# These are the ``LLMBackend`` enum values (core.config) in enum order; kept a plain
# literal so the CLI stays lazy about importing config at module load, with
# ``test_backend_enum_matches_backend_factory`` asserting it never drifts from the enum.
_BACKEND_CHOICES: tuple[str, ...] = ("fake", "anthropic_api", "bedrock", "claude_cli")


def _cmd_run(args: argparse.Namespace) -> int:
    """Load a config and materialize an (M1: empty) reproducible run directory."""
    config_path = args.config_opt if args.config_opt is not None else args.config
    if config_path is None:
        print("enterprise-sim run: provide a config path (.toml or .json)")
        return 2

    from enterprise_sim.assembly import execute_run
    from enterprise_sim.core.config import ConfigError, load_config

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"enterprise-sim run: {exc}")
        return 2
    except ValidationError as exc:
        print(f"enterprise-sim run: invalid config {config_path}:\n{exc}")
        return 2

    if args.output_dir is not None:
        config = config.model_copy(update={"output_dir": args.output_dir})

    config = _apply_scale_overrides(config, args)
    client = _resolve_run_client(config, args.backend)

    print(
        f"enterprise-sim run: validated config for {config.company.name} "
        f"({config.company.vertical}, {config.company.size.value}); "
        f"seed={config.seed}, window={config.simulation.period_start.isoformat()}"
        f"..{config.simulation.period_end.isoformat()}, projects={len(config.projects)}"
    )

    from enterprise_sim.core.llm import CostCeilingExceeded

    if args.dry_run:
        from enterprise_sim.assembly import estimate_run

        try:
            estimate = estimate_run(config, client=client)
        except CostCeilingExceeded as exc:
            print(f"enterprise-sim run: {exc}")
            return 1
        ceiling = config.scale.cost_ceiling_usd
        print(
            f"enterprise-sim run (dry-run): {estimate.num_artifacts} artifacts, "
            f"estimated ${estimate.estimated_cost_usd:.4f} "
            f"({estimate.input_tokens_each}+{estimate.output_tokens_each} tok/artifact, "
            f"model {estimate.model})"
            + (f"; ceiling ${ceiling:.4f}" if ceiling is not None else "")
        )
        return 0

    try:
        result = execute_run(config, client=client)
    except CostCeilingExceeded as exc:
        print(f"enterprise-sim run: {exc}")
        return 1
    rendered_estimate = result.corpus.estimate
    estimate_note = (
        f", est ${rendered_estimate.estimated_cost_usd:.4f}"
        if rendered_estimate is not None
        else ""
    )
    print(
        f"enterprise-sim run: wrote {result.run_id} to {result.run_dir} "
        f"({len(result.corpus.journal)} events, {len(result.corpus.artifacts)} artifacts"
        f"{estimate_note})"
    )
    return 0


def _apply_scale_overrides(config: RunConfig, args: argparse.Namespace) -> RunConfig:
    """Apply ``--max-concurrency`` / ``--cost-ceiling`` CLI overrides onto ``config``."""
    updates: dict[str, object] = {}
    if args.max_concurrency is not None:
        updates["max_concurrency"] = args.max_concurrency
    if args.cost_ceiling is not None:
        updates["cost_ceiling_usd"] = args.cost_ceiling
    if not updates:
        return config
    scale = config.scale.model_copy(update=updates)
    return config.model_copy(update={"scale": scale})


def _resolve_run_client(config: RunConfig, backend: str) -> LLMClient | None:
    """Resolve the render backend for ``run``, warning when it overrides the config.

    ``run`` renders with the ``--backend`` flag, whose default is the deterministic,
    network-free ``fake`` backend — a real provider is always an explicit opt-in
    (the determinism invariant; ``_DEFAULT_BACKEND`` in ``assembly/runner``). The
    config's ``[model].backend`` does *not* drive ``run`` implicitly (open question
    in specs/0001-bedrock-first-class.md); so whenever the effective flag disagrees
    with the config's declared backend — a real flag overriding a different backend,
    or the flag left at ``fake`` while the config named a real one — a one-line
    warning goes to stderr so the config stays meaningful. This is a pure value
    comparison: ``ModelConfig.backend`` now defaults to ``fake`` (D31), so a config
    with no ``[model]`` block records ``fake`` and the default run stays silent.

    Returns the LLM client the producers render against, or ``None`` for the default
    ``fake`` backend so that path stays byte-identical to a run with no client wired.
    """
    from enterprise_sim.assembly import llm_config_for
    from enterprise_sim.core.llm import build_client

    config_backend = config.model.backend.value
    if config_backend != backend:
        print(
            f"enterprise-sim run: config [model].backend={config_backend!r} is ignored; "
            f"rendering with --backend {backend!r}",
            file=sys.stderr,
        )
    if backend == "fake":
        return None
    return build_client(llm_config_for(config, backend=backend))


def _cmd_lint(args: argparse.Namespace) -> int:
    """Tier 1 static lint of playbooks (ARCHITECTURE §13).

    Lints one target — a reference-playbook name, a ``"module:callable"`` ref that
    returns a :class:`Playbook`, or a ``.json`` file of a serialized playbook — or
    every reference playbook when no target is given. Returns ``1`` if any lint
    found an error, else ``0``.
    """
    from enterprise_sim.authoring.lint import format_result, lint_playbook
    from enterprise_sim.authoring.patterns import REFERENCE_PLAYBOOKS

    targets: list[tuple[str, Playbook]]
    if args.target is None:
        targets = [(name, factory()) for name, factory in REFERENCE_PLAYBOOKS.items()]
    else:
        try:
            targets = [(args.target, _load_lint_target(args.target))]
        except (ValueError, ImportError, OSError) as exc:
            print(f"enterprise-sim lint: {exc}")
            return 2

    had_error = False
    for name, playbook in targets:
        result = lint_playbook(playbook)
        print(format_result(result, name))
        had_error = had_error or not result.ok
    return 1 if had_error else 0


def _load_lint_target(target: str) -> Playbook:
    """Resolve a lint ``target`` string to a :class:`Playbook`.

    Accepts a reference-playbook name, a ``"pkg.module:callable"`` reference (the
    callable is invoked and must return a ``Playbook``), or a path to a ``.json``
    file holding a serialized playbook (``Playbook.to_dict`` output).
    """
    import importlib
    import json

    from enterprise_sim.authoring.patterns import REFERENCE_PLAYBOOKS
    from enterprise_sim.authoring.sdk import Playbook

    if target in REFERENCE_PLAYBOOKS:
        return REFERENCE_PLAYBOOKS[target]()

    if target.endswith(".json"):
        path = Path(target)
        if not path.is_file():
            raise OSError(f"no such playbook file: {target}")
        return Playbook.from_dict(json.loads(path.read_text(encoding="utf-8")))

    if ":" in target:
        module_path, _, attr = target.partition(":")
        module = importlib.import_module(module_path)
        factory = getattr(module, attr)
        playbook = factory() if callable(factory) else factory
        if not isinstance(playbook, Playbook):
            raise ValueError(f"{target} did not yield a Playbook")
        return playbook

    raise ValueError(
        f"unknown lint target {target!r} "
        f"(known playbooks: {sorted(REFERENCE_PLAYBOOKS)}; or use module:callable / file.json)"
    )


def _cmd_eval(args: argparse.Namespace) -> int:
    """Tier 3 structural + LLM-judge evaluation of a completed run (ARCHITECTURE §13).

    Reads ``<run>/kg/events.jsonl`` (the scheduler's ordered log), computes the
    structural realism metrics, and — with ``--judge`` — samples one artifact and
    runs the LLM-as-judge through the selected backend (``fake`` by default, so it
    stays deterministic and free). Returns ``1`` if any structural metric failed.
    """
    import json

    from enterprise_sim.authoring.eval import evaluate, format_report, judge_sample
    from enterprise_sim.core.events import EventJournal

    if args.run is None:
        print("enterprise-sim eval: provide a path to a run output dir")
        return 2

    run_dir = Path(args.run)
    events_path = run_dir / "kg" / "events.jsonl"
    if not events_path.is_file():
        print(f"enterprise-sim eval: no event log at {events_path}")
        return 2

    with events_path.open(encoding="utf-8") as stream:
        journal = EventJournal.from_jsonl(stream)

    seed = 0
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            seed = int(json.loads(manifest_path.read_text(encoding="utf-8")).get("seed", 0))
        except (ValueError, OSError):
            seed = 0

    report = evaluate(journal)

    if args.judge:
        from enterprise_sim.core.llm import LLMConfig, build_client

        client = build_client(LLMConfig(backend=args.backend))
        verdict = judge_sample(journal, client, root_seed=seed)
        report = type(report)(metrics=report.metrics, judge=verdict)

    print(format_report(report, str(run_dir)))
    return 0 if report.ok else 1


def _cmd_bench(args: argparse.Namespace) -> int:
    """The ``bench`` command group: KG-QA benchmark generation/scoring (epic esim-uzc).

    This scaffold registers the group; its subcommands (``generate``, ``score``,
    ``report``) are added by later beads. Invoked without a subcommand it prints
    the group's usage and exits non-zero.
    """
    args.bench_parser.print_help()
    return 2


def _load_alignment(reconstructed_kg: Path, run: Path | None) -> dict[str, str]:
    """Build the predicted-id → gold-id alignment map for aligned scoring (esim-e9z).

    Reads the reconstruction written by ``ReconstructedKG.write`` and the gold graph
    (from ``run``'s ``kg/``, or a fresh keyless golden run when omitted) and returns
    :func:`~enterprise_sim.reconstruct.align_reconstructed_ids`'s map — reconstructed
    node ids (and gold artifact paths) resolved to the gold namespace. Pure and
    keyless: no LLM, deterministic for the same graphs.
    """
    import contextlib
    import tempfile

    from enterprise_sim.benchmark.generate import load_world_from_run
    from enterprise_sim.reconstruct import ReconstructedKG, align_reconstructed_ids

    kg = ReconstructedKG.read(reconstructed_kg)
    with contextlib.ExitStack() as stack:
        if run is not None:
            gold = load_world_from_run(run)
        else:
            from enterprise_sim.benchmark.fixtures import golden_run

            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="esim-align-"))
            gold = golden_run(tmp).world
        return align_reconstructed_ids(kg, gold)


def _cmd_bench_score(args: argparse.Namespace) -> int:
    """Score a predictions JSONL against a benchmark JSONL (esim-uzc.3).

    Pure and deterministic: reads the gold benchmark and the agent's predictions,
    grades the predicted node-id sets against the expected ones, and prints the
    macro-averaged report (overall and per reasoning type). No LLM involved.

    With ``--align`` the predicted ids are first mapped into the gold namespace
    (esim-e9z) using an alignment map built from ``--reconstructed-kg`` (and the
    gold ``--run``), so an answer that names the right entities under a different id
    namespace — e.g. artifact paths vs. canonical ``artifact:…`` ids — is credited
    instead of scoring 0 on a string mismatch. Raw scoring stays the default (the
    right basis for oracle/self-id runs already in the gold namespace).
    """
    from enterprise_sim.benchmark.schema import Benchmark
    from enterprise_sim.benchmark.score import Predictions, format_report, score

    alignment: dict[str, str] | None = None
    if args.align:
        if args.reconstructed_kg is None:
            print(
                "enterprise-sim bench score: --align requires --reconstructed-kg "
                "(the reconstruction dir whose ids are mapped into the gold namespace)",
                file=sys.stderr,
            )
            return 2
        alignment = _load_alignment(args.reconstructed_kg, args.run)

    benchmark = Benchmark.read_jsonl(args.bench)
    predictions = Predictions.read_jsonl(args.pred)
    report = score(benchmark, predictions, alignment=alignment)
    print(format_report(report, aligned=alignment is not None))
    return 0


def _cmd_bench_run(args: argparse.Namespace) -> int:
    """Run a benchmark runner over the gold KG/corpus and write its predictions.

    Two runners share this command, selected by ``--runner`` (epic esim-uzc):

    * ``rag`` (esim-uzc.5) — the retrieval-augmented baseline: answers each question
      from the RAW artifact corpus and resolves the answer back to KG node ids,
      through the selected LLM ``--backend`` (``--top-k`` chunks per question).
    * ``graph`` (esim-uzc.4) — the graph agent: loads the gold KG into embedded
      Cypher (kuzu) and SPARQL (oxigraph, with the materialized ontology) engines
      and lets a Claude ``--model`` reason over them (``--limit`` caps the subset).

    Both read the gold benchmark, default to a fresh golden run (or ``--run``), and
    write predictions JSONL to ``-o`` (stdout when omitted), with a one-line summary
    on stderr. Both answer steps need a real model/key.
    """
    from enterprise_sim.benchmark.schema import Benchmark

    benchmark = Benchmark.read_jsonl(args.bench)
    run_dir = str(args.run) if args.run is not None else None

    if args.runner == "graph":
        predictions = _run_graph_runner(args, benchmark, run_dir)
        if predictions is None:
            return 2
        detail = f"model {args.model}"
    else:
        predictions = _run_rag_runner(args, benchmark, run_dir)
        detail = f"backend {args.backend}"

    if predictions is None:
        return 2
    if args.output is None:
        print(predictions.to_jsonl(), end="")
    else:
        predictions.write_jsonl(args.output)

    destination = "stdout" if args.output is None else str(args.output)
    print(
        f"enterprise-sim bench run --runner {args.runner}: "
        f"{len(predictions)} predictions over {len(benchmark)} questions "
        f"({detail}) -> {destination}",
        file=sys.stderr,
    )
    return 0


def _run_rag_runner(
    args: argparse.Namespace,
    benchmark: Benchmark,
    run_dir: str | None,
) -> Predictions:
    """The RAG baseline path of ``bench run`` (esim-uzc.5)."""
    import contextlib
    import tempfile

    from enterprise_sim.benchmark.runners.rag import run_rag
    from enterprise_sim.core.llm import LLMConfig, build_client

    client = build_client(LLMConfig(backend=args.backend))
    with contextlib.ExitStack() as stack:
        resolved: str | Path | None = run_dir
        if resolved is None:
            from enterprise_sim.benchmark.fixtures import golden_run

            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="esim-bench-rag-"))
            resolved = golden_run(tmp).run_dir
        return run_rag(resolved, benchmark, client, top_k=args.top_k)


def _run_graph_runner(
    args: argparse.Namespace,
    benchmark: Benchmark,
    run_dir: str | None,
) -> Predictions | None:
    """The graph-agent path of ``bench run`` (esim-uzc.4); ``None`` on missing key."""
    from enterprise_sim.benchmark.runners.graph_agent import run_benchmark

    try:
        return run_benchmark(
            benchmark,
            run_dir=run_dir,
            model=args.model,
            limit=args.limit,
            use_bedrock=args.use_bedrock,
            aws_region=args.aws_region,
        )
    except RuntimeError as exc:
        print(f"enterprise-sim bench run: {exc}", file=sys.stderr)
        return None


def _add_bench_run_parser(
    bench_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``bench run --runner {rag,graph} --bench bench.jsonl -o pred.jsonl`` (esim-uzc.4/5)."""
    run_parser = bench_subparsers.add_parser(
        "run",
        help="run a benchmark runner (RAG baseline or graph agent) over the gold KG/corpus",
        description=(
            "Answer the gold benchmark with a runner and write its predictions "
            "JSONL. The 'rag' runner retrieves from the raw artifact corpus, asks "
            "an LLM to answer, and resolves the answer back to KG node ids; the "
            "'graph' runner reasons over the gold KG via embedded Cypher (kuzu) and "
            "SPARQL (oxigraph) engines with a Claude agent."
        ),
    )
    run_parser.add_argument(
        "--runner",
        default="rag",
        choices=["rag", "graph"],
        help="which runner to use (default: rag, the retrieval-augmented baseline)",
    )
    run_parser.add_argument(
        "--bench",
        required=True,
        type=Path,
        metavar="PATH",
        help="path to the gold benchmark JSONL (one QAPair per line)",
    )
    run_parser.add_argument(
        "--run",
        type=Path,
        default=None,
        metavar="DIR",
        help="read the gold KG/corpus from an existing run dir (default: a fresh golden run)",
    )
    run_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the predictions JSONL to PATH (default: stdout)",
    )
    # RAG-runner options.
    run_parser.add_argument(
        "--backend",
        default="anthropic_api",
        choices=_BACKEND_CHOICES,
        help="[rag] LLM backend for the answer step (default: anthropic_api, needs a key)",
    )
    run_parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        metavar="N",
        help="[rag] number of corpus chunks to retrieve per question (default: 5)",
    )
    # Graph-runner options.
    run_parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        metavar="MODEL",
        help="[graph] the Claude model the agent uses (default: claude-sonnet-4-6)",
    )
    run_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="[graph] answer only the first N questions (default: all)",
    )
    run_parser.add_argument(
        "--use-bedrock",
        action="store_true",
        help="[graph] route the agent SDK to Amazon Bedrock (CLAUDE_CODE_USE_BEDROCK=1, "
        "authenticates from ambient AWS creds instead of ANTHROPIC_API_KEY)",
    )
    run_parser.add_argument(
        "--aws-region",
        default=None,
        metavar="REGION",
        help="[graph] AWS region for --use-bedrock (sets AWS_REGION; default: ambient AWS env)",
    )
    run_parser.set_defaults(func=_cmd_bench_run)


def _add_bench_score_parser(
    bench_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``bench score --bench bench.jsonl --pred pred.jsonl`` (esim-uzc.3)."""
    score_parser = bench_subparsers.add_parser(
        "score",
        help="score a predictions JSONL against a benchmark JSONL",
        description=(
            "Score predicted answer sets against the gold benchmark over node-id "
            "sets: per-item exact-match and precision/recall/F1, macro-averaged "
            "overall and per reasoning type. Pure and deterministic (no LLM)."
        ),
    )
    score_parser.add_argument(
        "--bench",
        required=True,
        type=Path,
        metavar="PATH",
        help="path to the gold benchmark JSONL (one QAPair per line)",
    )
    score_parser.add_argument(
        "--pred",
        required=True,
        type=Path,
        metavar="PATH",
        help="path to the predictions JSONL (one {qa_id, predicted_ids} per line)",
    )
    score_parser.add_argument(
        "--align",
        action="store_true",
        help=(
            "map predicted ids into the gold namespace before scoring (esim-e9z), so "
            "namespace-mismatched-but-correct answers are credited; requires "
            "--reconstructed-kg (recommended for reconstructed runs, not oracle/self-id)"
        ),
    )
    score_parser.add_argument(
        "--reconstructed-kg",
        type=Path,
        default=None,
        metavar="DIR",
        help="[--align] reconstruction dir (ReconstructedKG.write output) whose ids are aligned",
    )
    score_parser.add_argument(
        "--run",
        type=Path,
        default=None,
        metavar="DIR",
        help="[--align] gold run dir (reads DIR/kg/*.jsonl); default: a fresh golden run",
    )
    score_parser.set_defaults(func=_cmd_bench_score)


def _parse_named_pred(value: str) -> tuple[str, Path]:
    """Parse a ``name=path`` ``--pred`` argument into a ``(name, Path)`` pair."""
    name, sep, path = value.partition("=")
    if not sep or not name or not path:
        raise argparse.ArgumentTypeError(
            f"--pred expects NAME=PATH (e.g. graph=pred.graph.jsonl), got {value!r}"
        )
    return name, Path(path)


def _cmd_bench_report(args: argparse.Namespace) -> int:
    """Compare runners side by side as a markdown leaderboard (esim-uzc.6).

    Reads the gold benchmark and one or more named predictions files
    (``--pred NAME=PATH``), auto-adds a trivial most-frequent baseline (unless
    ``--no-baseline``), and renders a markdown report — overall macro-F1 per
    runner plus a per-reasoning-type breakdown — to ``-o`` (stdout when omitted).
    Pure and deterministic: operates only on prediction files, no LLM.
    """
    from enterprise_sim.benchmark.report import build_report
    from enterprise_sim.benchmark.schema import Benchmark
    from enterprise_sim.benchmark.score import Predictions

    benchmark = Benchmark.read_jsonl(args.bench)
    predictions: dict[str, Predictions] = {}
    for name, path in args.pred:
        if name in predictions:
            print(f"enterprise-sim bench report: duplicate runner name {name!r}", file=sys.stderr)
            return 2
        predictions[name] = Predictions.read_jsonl(path)

    markdown = build_report(benchmark, predictions, include_baseline=not args.no_baseline)

    if args.output is None:
        print(markdown, end="")
    else:
        args.output.write_text(markdown, encoding="utf-8")
        print(
            f"enterprise-sim bench report: {len(benchmark)} questions -> {args.output}",
            file=sys.stderr,
        )
    return 0


def _add_bench_report_parser(
    bench_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``bench report --bench bench.jsonl --pred graph=… --pred rag=…`` (esim-uzc.6)."""
    report_parser = bench_subparsers.add_parser(
        "report",
        help="compare runners as a markdown leaderboard",
        description=(
            "Compare two or more runners against the gold benchmark and emit a "
            "markdown leaderboard: overall macro-F1 per runner plus a per-"
            "reasoning-type breakdown, with a trivial most-frequent baseline "
            "column. Pure and deterministic (operates on prediction files, no LLM)."
        ),
    )
    report_parser.add_argument(
        "--bench",
        required=True,
        type=Path,
        metavar="PATH",
        help="path to the gold benchmark JSONL (one QAPair per line)",
    )
    report_parser.add_argument(
        "--pred",
        required=True,
        action="append",
        type=_parse_named_pred,
        metavar="NAME=PATH",
        help="a named runner's predictions JSONL (repeatable, e.g. graph=pred.graph.jsonl)",
    )
    report_parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="omit the auto-added most-frequent baseline runner",
    )
    report_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the markdown report to PATH (default: stdout)",
    )
    report_parser.set_defaults(func=_cmd_bench_report)


def _cmd_bench_generate(args: argparse.Namespace) -> int:
    """Generate a KG-QA benchmark from the gold knowledge graph (esim-uzc.2).

    Derives question/answer pairs deterministically from the gold KG — a fresh
    golden run by default, or the run directory given by ``--run`` — and writes
    them as JSONL to ``-o`` (stdout when omitted). A one-line summary of the pair
    count and reasoning-type spread goes to stderr.
    """
    from enterprise_sim.benchmark.generate import generate

    benchmark = generate(args.run)

    if args.output is None:
        print(benchmark.to_jsonl(), end="")
    else:
        benchmark.write_jsonl(args.output)

    by_type = sorted({pair.reasoning_type for pair in benchmark})
    destination = "stdout" if args.output is None else str(args.output)
    print(
        f"enterprise-sim bench generate: {len(benchmark)} pairs "
        f"across {len(by_type)} reasoning types ({', '.join(by_type)}) -> {destination}",
        file=sys.stderr,
    )
    return 0


def _add_bench_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the ``bench`` command group and its ``generate`` subcommand.

    The nested subparser object is stashed on the parser's defaults as
    ``bench_subparsers`` so subsequent milestones (the runners, the grader, the
    report) can register ``score``/``report`` without re-deriving the group here.
    """
    bench_parser = subparsers.add_parser(
        "bench",
        help="KG-QA benchmark: generate/score/report over the gold KG",
        description=(
            "Generate a question/answer benchmark from the gold knowledge graph, "
            "score agent runners against it, and report results by reasoning type."
        ),
    )
    bench_subparsers = bench_parser.add_subparsers(
        dest="bench_command",
        metavar="{generate,run,score,report}",
    )
    bench_parser.set_defaults(func=_cmd_bench, bench_parser=bench_parser)
    # Exposed for later beads to attach subcommands to the same group.
    bench_parser.set_defaults(bench_subparsers=bench_subparsers)
    _add_bench_run_parser(bench_subparsers)
    _add_bench_score_parser(bench_subparsers)
    _add_bench_report_parser(bench_subparsers)

    generate_parser = bench_subparsers.add_parser(
        "generate",
        help="derive a Q/A benchmark from the gold KG",
        description=(
            "Deterministically derive question/answer pairs from the gold knowledge "
            "graph across reasoning types (direct_relation, transitive, provenance, "
            "aggregation, goal_tree). Defaults to a fresh golden run."
        ),
    )
    generate_parser.add_argument(
        "--run",
        type=Path,
        default=None,
        metavar="DIR",
        help="read the gold KG from an existing run dir (default: a fresh golden run)",
    )
    generate_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the benchmark JSONL to PATH (default: stdout)",
    )
    generate_parser.set_defaults(func=_cmd_bench_generate)


def _cmd_reconstruct(args: argparse.Namespace) -> int:
    """The ``reconstruct`` command group: rebuild a KG from the corpus (epic esim-nc6).

    Registers the group and its subcommands (``build``, ``fidelity``, ``reason``;
    ``report`` is added by a later bead). Invoked without a subcommand it prints the
    group's usage and exits non-zero — mirroring ``bench``.
    """
    args.reconstruct_parser.print_help()
    return 2


def _add_reconstruct_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire the ``reconstruct`` command group (epic esim-nc6), mirroring ``bench``.

    The nested subparser object is stashed on the parser's defaults as
    ``reconstruct_subparsers`` so subsequent beads (chunking, extraction, the
    fidelity scorer, the reasoner) can register ``build``/``fidelity``/``reason``/
    ``report`` without re-deriving the group here.
    """
    reconstruct_parser = subparsers.add_parser(
        "reconstruct",
        help="reconstruct a KG from the corpus, then score/reason over it",
        description=(
            "Read the raw artifact corpus back out into a reconstructed knowledge "
            "graph (in the gold KG's on-disk schema), score its fidelity against "
            "the gold graph, sweep the edge-confidence threshold, and reason over "
            "it. Subcommands: build/fidelity/sweep/reason/report (epic esim-nc6) "
            "and scale (esim-ecr.5)."
        ),
    )
    reconstruct_subparsers = reconstruct_parser.add_subparsers(
        dest="reconstruct_command",
        metavar="{build,fidelity,sweep,reason,report,scale}",
    )
    reconstruct_parser.set_defaults(
        func=_cmd_reconstruct,
        reconstruct_parser=reconstruct_parser,
    )
    # Exposed for later beads to attach subcommands to the same group.
    reconstruct_parser.set_defaults(reconstruct_subparsers=reconstruct_subparsers)
    _add_reconstruct_build_parser(reconstruct_subparsers)
    _add_reconstruct_fidelity_parser(reconstruct_subparsers)
    _add_reconstruct_sweep_parser(reconstruct_subparsers)
    _add_reconstruct_reason_parser(reconstruct_subparsers)
    _add_reconstruct_report_parser(reconstruct_subparsers)
    _add_reconstruct_scale_parser(reconstruct_subparsers)


def _cmd_reconstruct_build(args: argparse.Namespace) -> int:
    """Build + persist the reconstructed KG from a run's corpus (esim-nc6.5).

    Runs chunk → extract → resolve → aggregate end to end over the raw artifact
    corpus (a fresh golden run by default, or ``--run``'s), then writes the
    reconstructed KG (``nodes.jsonl`` / ``edges.jsonl`` in the gold schema, plus
    ``provenance.jsonl``) to ``-o`` — the build-once artifact reused by fidelity /
    reason. The gated LLM steps use ``--backend`` (``fake`` by default, so the
    keyless path still emits a small KG with no key); ``--model`` picks the model.
    """
    import contextlib
    import tempfile

    from enterprise_sim.core.llm import LLMConfig, build_client
    from enterprise_sim.reconstruct import BuildConfig, run_pipeline

    config = BuildConfig(edge_confidence_threshold=args.edge_threshold)
    client = build_client(LLMConfig(backend=args.backend, model=args.model))
    with contextlib.ExitStack() as stack:
        if args.run is not None:
            run_dir = str(args.run)
        else:
            from enterprise_sim.benchmark.fixtures import golden_run

            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="esim-reconstruct-"))
            run_dir = str(golden_run(tmp).run_dir)

        kg = run_pipeline(run_dir, client, model=args.model, config=config)

    kg.write(args.output)
    print(
        f"enterprise-sim reconstruct build: {kg.node_count} nodes, {kg.edge_count} edges "
        f"(edge threshold={args.edge_threshold:.2f}, backend={args.backend}) -> {args.output}",
        file=sys.stderr,
    )
    return 0


def _add_reconstruct_build_parser(
    reconstruct_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``reconstruct build -o DIR [--run DIR] [--model M] [--backend B]`` (esim-nc6.5)."""
    from enterprise_sim.reconstruct import HAIKU_MODEL

    build_parser = reconstruct_subparsers.add_parser(
        "build",
        help="build + persist the reconstructed KG from the corpus (build-once)",
        description=(
            "Reconstruct a knowledge graph from the raw artifact corpus and persist "
            "it once: chunk the corpus, extract typed mentions + candidate relations, "
            "resolve entities, aggregate relations over canonical ids (deduped, with "
            "support counts + provenance, gated by an edge confidence threshold), and "
            "write nodes/edges/provenance.jsonl (gold schema) to the output dir. The "
            "gated LLM steps use the selected backend; the keyless fake backend still "
            "emits a small, loadable KG."
        ),
    )
    build_parser.add_argument(
        "-o",
        "--output",
        required=True,
        type=Path,
        metavar="DIR",
        help="reconstruction output dir (writes nodes/edges/provenance.jsonl)",
    )
    build_parser.add_argument(
        "--run",
        type=Path,
        default=None,
        metavar="DIR",
        help="run dir whose raw corpus is reconstructed; default: a fresh golden run",
    )
    build_parser.add_argument(
        "--backend",
        default="fake",
        choices=_BACKEND_CHOICES,
        help="LLM backend for the gated extract/resolve steps (default: fake, keyless)",
    )
    build_parser.add_argument(
        "--model",
        default=HAIKU_MODEL,
        metavar="MODEL",
        help=f"model for the gated LLM steps (default: {HAIKU_MODEL})",
    )
    build_parser.add_argument(
        "--edge-threshold",
        dest="edge_threshold",
        type=float,
        default=0.0,
        metavar="CONF",
        help="drop aggregated edges below this confidence (precision/recall knob; default: 0.0)",
    )
    build_parser.set_defaults(func=_cmd_reconstruct_build)


def _gold_grounding_by_path(run_dir: Path, gold: World) -> dict[str, list[str]] | None:
    """Gold grounding answer key as ``{entity id → artifact paths}`` for provenance fidelity.

    Reuses the benchmark's own provenance key
    (:func:`enterprise_sim.benchmark.generate.load_groundings`, which reads
    ``kg/mentions.jsonl``) — the exact grounding the provenance reasoning family is
    graded on — and rewrites its artifact node ids to the ``path`` prop the
    reconstruction joins on. Returns ``None`` when the run carries no
    ``mentions.jsonl`` (so provenance fidelity is simply not scored) rather than
    failing the whole fidelity run.
    """
    from enterprise_sim.benchmark.generate import load_groundings

    if not (Path(run_dir) / "kg" / "mentions.jsonl").is_file():
        return None
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


def _cmd_reconstruct_fidelity(args: argparse.Namespace) -> int:
    """Score a reconstructed KG against the gold KG (esim-nc6.6).

    Pure and deterministic (no LLM): loads the reconstruction written by
    :meth:`~enterprise_sim.reconstruct.schema.ReconstructedKG.write` and the gold
    graph (from ``--run``'s ``kg/`` or a fresh golden run), aligns nodes by id then
    type+name, and emits node/edge P/R/F1 plus entity-resolution error counts as a
    markdown report (``--json`` for machine-readable output) to ``-o`` (stdout when
    omitted). Scoring gold against itself yields node + edge F1 = 1.0.
    """
    import contextlib
    import tempfile

    from enterprise_sim.benchmark.generate import load_world_from_run
    from enterprise_sim.reconstruct import ReconstructedKG, score_fidelity

    reconstructed = ReconstructedKG.read(args.reconstructed)
    with contextlib.ExitStack() as stack:
        if args.run is not None:
            gold = load_world_from_run(args.run)
            gold_run_dir = args.run
        else:
            from enterprise_sim.benchmark.fixtures import golden_run

            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="esim-fidelity-"))
            run = golden_run(tmp)
            gold, gold_run_dir = run.world, run.run_dir

        report = score_fidelity(
            reconstructed, gold, gold_grounding=_gold_grounding_by_path(gold_run_dir, gold)
        )

    rendered = report.to_json() if args.json else report.to_markdown()
    if args.output is None:
        print(rendered, end="" if args.json else "\n")
    else:
        args.output.write_text(rendered + ("" if args.json else "\n"), encoding="utf-8")
        provenance = (
            f" provenance F1={report.provenance.overall.f1:.3f}"
            if report.provenance is not None
            else ""
        )
        print(
            f"enterprise-sim reconstruct fidelity: "
            f"node F1={report.nodes.overall.f1:.3f} edge F1={report.edges.overall.f1:.3f}"
            f"{provenance} "
            f"(over-merges={report.entity_resolution.over_merges}, "
            f"under-merges={report.entity_resolution.under_merges}) -> {args.output}",
            file=sys.stderr,
        )
    return 0


def _add_reconstruct_fidelity_parser(
    reconstruct_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``reconstruct fidelity --reconstructed DIR [--run DIR]`` (esim-nc6.6)."""
    fidelity_parser = reconstruct_subparsers.add_parser(
        "fidelity",
        help="score a reconstructed KG against the gold KG",
        description=(
            "Score a reconstructed knowledge graph against the gold graph: node and "
            "edge precision/recall/F1 (overall and per type) after aligning "
            "reconstructed node ids to gold ids, plus entity-resolution over/under-"
            "merge counts. Pure and deterministic (no LLM); gold-vs-gold scores 1.0."
        ),
    )
    fidelity_parser.add_argument(
        "--reconstructed",
        required=True,
        type=Path,
        metavar="DIR",
        help="reconstruction dir with nodes.jsonl/edges.jsonl (ReconstructedKG.write output)",
    )
    fidelity_parser.add_argument(
        "--run",
        type=Path,
        default=None,
        metavar="DIR",
        help="gold run dir (reads DIR/kg/*.jsonl); default: a fresh golden run",
    )
    fidelity_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the report as JSON instead of markdown",
    )
    fidelity_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the report to PATH (default: stdout)",
    )
    fidelity_parser.set_defaults(func=_cmd_reconstruct_fidelity)


def _parse_thresholds(raw: str) -> list[float]:
    """Parse a comma-separated ``--thresholds`` list into floats (argparse type).

    Empty entries are ignored; a non-numeric or empty result raises
    ``argparse.ArgumentTypeError`` so the CLI reports a clean usage error.
    """
    values: list[float] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid threshold {token!r}: not a number") from exc
    if not values:
        raise argparse.ArgumentTypeError("no thresholds given")
    return values


def _parse_models(raw: str) -> list[str]:
    """Parse a comma-separated ``--models`` list into model ids (argparse type).

    Empty entries are ignored (so trailing commas are tolerated); an all-empty
    result raises ``argparse.ArgumentTypeError`` for a clean usage error. Order is
    preserved — the model sweep de-duplicates while keeping first-seen order.
    """
    models = [token for part in raw.split(",") if (token := part.strip())]
    if not models:
        raise argparse.ArgumentTypeError("no models given")
    return models


def _cmd_reconstruct_sweep(args: argparse.Namespace) -> int:
    """Sweep the edge-confidence threshold, or the extraction model (esim-ecr.3/ecr.4).

    Two axes share this command. Without ``--models`` it sweeps the **edge-confidence
    threshold**: extract the corpus once (chunk → extract → resolve, the gated LLM
    prefix) and re-aggregate that single extraction at every ``--thresholds`` value —
    no re-extraction per threshold — scoring each rebuilt KG against the gold graph.
    With ``--models`` it sweeps the **model axis** (:func:`_cmd_reconstruct_model_sweep`):
    reconstruct the corpus once per model and compare their fidelity (and answer-F1
    when ``--bench`` is given). Both emit a comparison table (markdown, or ``--json``)
    to ``-o`` (stdout when omitted); the gated steps use ``--backend`` (``fake`` by
    default, so the keyless path still sweeps a small KG with no key).
    """
    if args.models is not None:
        return _cmd_reconstruct_model_sweep(args)

    import contextlib
    import tempfile

    from enterprise_sim.benchmark.generate import load_world_from_run
    from enterprise_sim.core.llm import LLMConfig, build_client
    from enterprise_sim.reconstruct import extract_once, sweep_thresholds

    client = build_client(LLMConfig(backend=args.backend, model=args.model))
    with contextlib.ExitStack() as stack:
        if args.run is not None:
            run_dir = str(args.run)
            gold = load_world_from_run(args.run)
        else:
            from enterprise_sim.benchmark.fixtures import golden_run

            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="esim-sweep-"))
            run = golden_run(tmp)
            run_dir = str(run.run_dir)
            gold = run.world

        extraction = extract_once(run_dir, client, model=args.model)
        report = sweep_thresholds(extraction, gold, args.thresholds)

    rendered = report.to_json() if args.json else report.to_markdown()
    if args.output is None:
        print(rendered, end="" if args.json else "\n")
    else:
        args.output.write_text(rendered + ("" if args.json else "\n"), encoding="utf-8")
        best = report.best_edge_f1()
        sweet_spot = (
            "no edges"
            if best is None
            else f"best edge F1={best.edges.f1:.3f} @ {best.threshold:.2f}"
        )
        print(
            f"enterprise-sim reconstruct sweep: {len(report.points)} thresholds "
            f"(backend={args.backend}, {sweet_spot}) -> {args.output}",
            file=sys.stderr,
        )
    return 0


def _cmd_reconstruct_model_sweep(args: argparse.Namespace) -> int:
    """Sweep the extraction model axis and compare reconstructions (esim-ecr.4).

    Reconstructs the corpus once per ``--models`` entry (each runs its own gated
    chunk → extract → resolve through the same ``--backend`` client, the per-call
    model override picking the model), builds every KG at ``--edge-threshold``, and
    scores each against the gold graph with the keyless fidelity scorer — a per-model
    node/edge P/R/F1 comparison table. With ``--bench`` each model's KG is also
    reasoned over (by the *same* model, via the graph agent) and graded, adding an
    answer-F1 column; the agent step needs ``ANTHROPIC_API_KEY`` (a keyed crew run),
    while the keyless ``fake`` backend records the label and reports fidelity only.
    """
    import contextlib
    import tempfile

    from enterprise_sim.benchmark.generate import load_world_from_run
    from enterprise_sim.benchmark.schema import Benchmark
    from enterprise_sim.benchmark.score import Report, score
    from enterprise_sim.core.llm import LLMConfig, build_client
    from enterprise_sim.reconstruct import (
        AnswerScorer,
        BuildConfig,
        ReconstructedKG,
        sweep_models,
    )

    client = build_client(LLMConfig(backend=args.backend, model=args.models[0]))
    build_config = BuildConfig(edge_confidence_threshold=args.edge_threshold)

    benchmark = Benchmark.read_jsonl(args.bench) if args.bench is not None else None

    with contextlib.ExitStack() as stack:
        if args.run is not None:
            run_dir = str(args.run)
            gold = load_world_from_run(args.run)
        else:
            from enterprise_sim.benchmark.fixtures import golden_run

            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="esim-model-sweep-"))
            run = golden_run(tmp)
            run_dir = str(run.run_dir)
            gold = run.world

        answer_scorer: AnswerScorer | None = None
        if benchmark is not None:
            from enterprise_sim.benchmark.runners.graph_agent import GraphRunner, run_benchmark
            from enterprise_sim.benchmark.runners.projection import GraphModel
            from enterprise_sim.reconstruct import project_with_groundings

            # The gold Artifact nodes name provenance answers in the benchmark's id
            # coordinate, so every model's reconstruction is projected with its
            # groundings (as `reconstruct reason --run` does) and provenance scores.
            gold_artifact_ids = {
                node.props["path"]: node.id
                for node in gold.nodes_by_type("Artifact")
                if isinstance(node.props.get("path"), str)
            }
            scored_benchmark = benchmark

            def _score_answers(kg: ReconstructedKG, model: str) -> Report:
                world, groundings = project_with_groundings(kg, gold_artifact_ids)
                runner = GraphRunner(GraphModel.from_world(world, groundings))
                try:
                    predictions = run_benchmark(
                        scored_benchmark, runner=runner, model=model, limit=args.limit
                    )
                finally:
                    runner.close()
                return score(scored_benchmark, predictions)

            answer_scorer = _score_answers

        try:
            report = sweep_models(
                run_dir,
                gold,
                args.models,
                client,
                build_config=build_config,
                answer_scorer=answer_scorer,
                backend=args.backend,
            )
        except RuntimeError as exc:
            # The graph-agent answer step raises cleanly on a missing key.
            print(f"enterprise-sim reconstruct sweep --models: {exc}", file=sys.stderr)
            return 2

    rendered = report.to_json() if args.json else report.to_markdown()
    if args.output is None:
        print(rendered, end="" if args.json else "\n")
    else:
        args.output.write_text(rendered + ("" if args.json else "\n"), encoding="utf-8")
        best = report.best_edge_f1()
        leader = "no models" if best is None else f"best edge F1={best.edge_f1:.3f} by {best.model}"
        print(
            f"enterprise-sim reconstruct sweep: {len(report.points)} models "
            f"(backend={args.backend}, {leader}) -> {args.output}",
            file=sys.stderr,
        )
    return 0


def _add_reconstruct_sweep_parser(
    reconstruct_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``reconstruct sweep --thresholds 0,0.25,0.5,0.75 [--run DIR]`` (esim-ecr.3)."""
    from enterprise_sim.reconstruct import HAIKU_MODEL

    sweep_parser = reconstruct_subparsers.add_parser(
        "sweep",
        help="sweep the edge-confidence threshold, or the extraction model (--models)",
        description=(
            "Sweep one of two axes. Default (no --models): find the edge-confidence "
            "precision/recall sweet spot — extract the corpus once (chunk/extract/"
            "resolve) and re-aggregate that single extraction at every --thresholds "
            "value (no re-extraction per threshold), scoring each rebuilt KG against "
            "the gold graph. With --models: sweep the model axis — reconstruct the "
            "corpus once per model and compare their fidelity (and answer-F1 when "
            "--bench is given, which needs a key). Both emit a comparison table "
            "(markdown or --json); the keyless fake backend still sweeps a small KG."
        ),
    )
    sweep_parser.add_argument(
        "--thresholds",
        type=_parse_thresholds,
        default=[0.0, 0.25, 0.5, 0.75],
        metavar="CONF,CONF,...",
        help="threshold axis (no --models): comma-separated edge-confidence "
        "thresholds to sweep (default: 0,0.25,0.5,0.75)",
    )
    sweep_parser.add_argument(
        "--models",
        type=_parse_models,
        default=None,
        metavar="MODEL,MODEL,...",
        help="model axis: comma-separated models to reconstruct with and compare "
        "(e.g. claude-haiku-4-5-20251001,claude-sonnet-4-6); enables the model sweep",
    )
    sweep_parser.add_argument(
        "--edge-threshold",
        type=float,
        default=0.0,
        metavar="CONF",
        help="model axis: single edge-confidence threshold each model's KG is built "
        "at (default: 0.0, keep every edge)",
    )
    sweep_parser.add_argument(
        "--bench",
        type=Path,
        default=None,
        metavar="PATH",
        help="model axis: gold benchmark JSONL — when given, each model's KG is "
        "reasoned over (same model) and answer-F1 is added to the table (needs a key)",
    )
    sweep_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="model axis: with --bench, answer only the first N questions (default: all)",
    )
    sweep_parser.add_argument(
        "--run",
        type=Path,
        default=None,
        metavar="DIR",
        help="run dir whose raw corpus is reconstructed + whose kg/ is the gold graph; "
        "default: a fresh golden run",
    )
    sweep_parser.add_argument(
        "--backend",
        default="fake",
        choices=_BACKEND_CHOICES,
        help="LLM backend for the gated extract/resolve steps (default: fake, keyless)",
    )
    sweep_parser.add_argument(
        "--model",
        default=HAIKU_MODEL,
        metavar="MODEL",
        help=f"model for the gated LLM steps (default: {HAIKU_MODEL})",
    )
    sweep_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the sweep as JSON instead of markdown",
    )
    sweep_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the report to PATH (default: stdout)",
    )
    sweep_parser.set_defaults(func=_cmd_reconstruct_sweep)


def _cmd_reconstruct_reason(args: argparse.Namespace) -> int:
    """Reason over a persisted reconstructed KG via the graph agent (esim-nc6.7).

    The build-once/answer-many payoff: the reconstructed KG written by
    :meth:`~enterprise_sim.reconstruct.schema.ReconstructedKG.write` (nc6.5) is
    loaded **once** into the embedded Cypher (kuzu) + SPARQL (oxigraph, with the
    materialized ontology) engines — the *same* projection the gold KG uses — and
    the existing graph-agent runner answers the whole benchmark over that single
    set of engines (no per-question reconstruction, no per-question rebuild).
    Predictions JSONL goes to ``-o`` (stdout when omitted). The agent step is gated
    (needs ``ANTHROPIC_API_KEY``); on a missing key it reports cleanly and exits 2.
    """
    from enterprise_sim.benchmark.runners.graph_agent import GraphRunner, run_benchmark
    from enterprise_sim.benchmark.runners.projection import GraphModel
    from enterprise_sim.benchmark.schema import Benchmark
    from enterprise_sim.reconstruct import ReconstructedKG, project_with_groundings

    benchmark = Benchmark.read_jsonl(args.bench)
    kg = ReconstructedKG.read(args.reconstructed)
    # Resolve each grounding artifact to the id the benchmark grades provenance in.
    # An artifact's identity is a fixed coordinate of the benchmark's answer space
    # (the same one the oracle and RAG name artifacts in), which the reconstruction
    # only observes by path — so ``--run`` supplies the gold Artifact ``{path → id}``
    # map and provenance answers can match the gold key. Omitted ⇒ path-keyed ids
    # (structurally answerable, but won't match a gold key).
    gold_artifact_ids: dict[str, str] | None = None
    if args.run is not None:
        from enterprise_sim.benchmark.generate import load_world_from_run

        gold = load_world_from_run(args.run)
        gold_artifact_ids = {
            node.props["path"]: node.id
            for node in gold.nodes_by_type("Artifact")
            if isinstance(node.props.get("path"), str)
        }
    # Load the reconstruction into the engines once; the runner owns that build and
    # is reused for every question below (and closed here, not by run_benchmark).
    # The grounding map projects the reconstruction's provenance into ``mentions``
    # edges (as the gold projection does), so the provenance reasoning family is
    # answerable over the reconstructed KG rather than a structural zero.
    world, groundings = project_with_groundings(kg, gold_artifact_ids)
    runner = GraphRunner(GraphModel.from_world(world, groundings))
    try:
        predictions = run_benchmark(
            benchmark,
            runner=runner,
            model=args.model,
            limit=args.limit,
            use_bedrock=args.use_bedrock,
            aws_region=args.aws_region,
        )
    except RuntimeError as exc:
        print(f"enterprise-sim reconstruct reason: {exc}", file=sys.stderr)
        return 2
    finally:
        runner.close()

    if args.output is None:
        print(predictions.to_jsonl(), end="")
    else:
        predictions.write_jsonl(args.output)

    destination = "stdout" if args.output is None else str(args.output)
    print(
        f"enterprise-sim reconstruct reason: {len(predictions)} predictions over "
        f"{len(benchmark)} questions "
        f"(model {args.model}, {kg.node_count} nodes / {kg.edge_count} edges) -> {destination}",
        file=sys.stderr,
    )
    return 0


def _add_reconstruct_reason_parser(
    reconstruct_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``reconstruct reason --reconstructed DIR --bench bench.jsonl -o pred`` (esim-nc6.7)."""
    reason_parser = reconstruct_subparsers.add_parser(
        "reason",
        help="answer the benchmark by reasoning over a reconstructed KG (build-once)",
        description=(
            "Reason over a persisted reconstructed knowledge graph: load it once "
            "into the embedded Cypher (kuzu) and SPARQL (oxigraph, with the "
            "materialized ontology) engines — the same projection the gold KG uses "
            "— and answer the whole KG-QA benchmark with the graph agent, reusing "
            "the engines across every question. Writes a predictions JSONL scorable "
            "by 'bench score'. The agent step needs ANTHROPIC_API_KEY."
        ),
    )
    reason_parser.add_argument(
        "--reconstructed",
        required=True,
        type=Path,
        metavar="DIR",
        help="reconstruction dir with nodes.jsonl/edges.jsonl (ReconstructedKG.write output)",
    )
    reason_parser.add_argument(
        "--bench",
        required=True,
        type=Path,
        metavar="PATH",
        help="path to the gold benchmark JSONL (one QAPair per line)",
    )
    reason_parser.add_argument(
        "--run",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "golden run dir whose gold Artifact nodes name provenance answers in the "
            "benchmark's id coordinate system (path → gold artifact id); omit for "
            "path-keyed artifact ids (provenance answerable but unscorable against a gold key)"
        ),
    )
    reason_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the predictions JSONL to PATH (default: stdout)",
    )
    reason_parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        metavar="MODEL",
        help="the Claude model the agent uses (default: claude-sonnet-4-6)",
    )
    reason_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="answer only the first N questions (default: all)",
    )
    reason_parser.add_argument(
        "--use-bedrock",
        action="store_true",
        help="route the agent SDK to Amazon Bedrock (CLAUDE_CODE_USE_BEDROCK=1, "
        "authenticates from ambient AWS creds instead of ANTHROPIC_API_KEY)",
    )
    reason_parser.add_argument(
        "--aws-region",
        default=None,
        metavar="REGION",
        help="AWS region for --use-bedrock (sets AWS_REGION; default: ambient AWS env)",
    )
    reason_parser.set_defaults(func=_cmd_reconstruct_reason)


def _cmd_reconstruct_report(args: argparse.Namespace) -> int:
    """Attribute the graph's advantage: understanding vs reasoning (esim-nc6.8).

    Closes the reconstruct loop with a three-way comparison on ONE benchmark:
    ``--oracle`` (graph agent on the gold KG, the ceiling), ``--reconstructed``
    (the *same* agent on the reconstructed KG, nc6.7), and ``--rag`` (the corpus
    baseline). Scores all three with the keyless grader and renders a markdown
    report — overall + per-reasoning-type F1 per system plus the decomposition of
    the oracle's advantage into an *understanding* gap (oracle − reconstructed) and
    a *reasoning/structure* gap (reconstructed − rag). With ``--fidelity`` (the
    JSON from ``reconstruct fidelity --json``) it also carries the reconstructed
    KG's fidelity numbers as context. Pure and deterministic (no LLM); writes to
    ``-o`` (stdout when omitted).

    With ``--align`` the reconstructed system's predicted ids are mapped into the
    gold namespace before scoring (esim-e9z), using an alignment map built from
    ``--reconstructed-kg`` (and the gold ``--run``), so an answer that names the
    right entities under a different id namespace is credited rather than scoring 0
    on a string mismatch; the report notes the mode. Oracle and RAG answer in gold
    ids and are always graded raw.
    """
    import json

    from enterprise_sim.benchmark.schema import Benchmark
    from enterprise_sim.benchmark.score import Predictions
    from enterprise_sim.reconstruct import FidelityContext, build_attribution, render_markdown

    alignment: dict[str, str] | None = None
    if args.align:
        if args.reconstructed_kg is None:
            print(
                "enterprise-sim reconstruct report: --align requires --reconstructed-kg "
                "(the reconstruction dir whose ids are mapped into the gold namespace)",
                file=sys.stderr,
            )
            return 2
        alignment = _load_alignment(args.reconstructed_kg, args.run)

    benchmark = Benchmark.read_jsonl(args.bench)
    oracle = Predictions.read_jsonl(args.oracle)
    reconstructed = Predictions.read_jsonl(args.reconstructed)
    rag = Predictions.read_jsonl(args.rag)

    fidelity: FidelityContext | None = None
    if args.fidelity is not None:
        fidelity = FidelityContext.from_dict(json.loads(args.fidelity.read_text(encoding="utf-8")))

    attribution = build_attribution(
        benchmark,
        oracle=oracle,
        reconstructed=reconstructed,
        rag=rag,
        fidelity=fidelity,
        alignment=alignment,
    )
    markdown = render_markdown(attribution)

    if args.output is None:
        print(markdown, end="")
    else:
        args.output.write_text(markdown, encoding="utf-8")
        gap = attribution.gap()
        mode = " id-aligned" if attribution.aligned else ""
        print(
            f"enterprise-sim reconstruct report:{mode} {attribution.benchmark_size} questions "
            f"(understanding={gap.understanding:+.3f}, reasoning={gap.reasoning:+.3f}, "
            f"total={gap.total:+.3f}) -> {args.output}",
            file=sys.stderr,
        )
    return 0


def _add_reconstruct_report_parser(
    reconstruct_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``reconstruct report --bench … --oracle … --reconstructed … --rag …`` (esim-nc6.8)."""
    report_parser = reconstruct_subparsers.add_parser(
        "report",
        help="attribute the graph's advantage: understanding vs reasoning",
        description=(
            "Compare three systems on one benchmark — the oracle (graph agent on "
            "the gold KG), the reconstructed KG's agent, and the RAG baseline — and "
            "emit a markdown attribution report: overall + per-reasoning-type F1 per "
            "system, plus the split of the oracle's advantage over RAG into an "
            "understanding gap (oracle − reconstructed) and a reasoning gap "
            "(reconstructed − rag), with the reconstruction's fidelity numbers as "
            "context. Pure and deterministic (operates on prediction files, no LLM)."
        ),
    )
    report_parser.add_argument(
        "--bench",
        required=True,
        type=Path,
        metavar="PATH",
        help="path to the gold benchmark JSONL (one QAPair per line)",
    )
    report_parser.add_argument(
        "--oracle",
        required=True,
        type=Path,
        metavar="PATH",
        help="predictions JSONL of the graph agent on the GOLD KG (the ceiling)",
    )
    report_parser.add_argument(
        "--reconstructed",
        required=True,
        type=Path,
        metavar="PATH",
        help="predictions JSONL of the graph agent on the RECONSTRUCTED KG (nc6.7)",
    )
    report_parser.add_argument(
        "--rag",
        required=True,
        type=Path,
        metavar="PATH",
        help="predictions JSONL of the RAG corpus baseline",
    )
    report_parser.add_argument(
        "--fidelity",
        type=Path,
        default=None,
        metavar="PATH",
        help="reconstruction fidelity JSON ('reconstruct fidelity --json') for context",
    )
    report_parser.add_argument(
        "--align",
        action="store_true",
        help=(
            "map the reconstructed system's predicted ids into the gold namespace "
            "before scoring (esim-e9z), crediting namespace-mismatched answers; "
            "requires --reconstructed-kg. The report notes the mode."
        ),
    )
    report_parser.add_argument(
        "--reconstructed-kg",
        type=Path,
        default=None,
        metavar="DIR",
        help="[--align] reconstruction dir (ReconstructedKG.write output) whose ids are aligned",
    )
    report_parser.add_argument(
        "--run",
        type=Path,
        default=None,
        metavar="DIR",
        help="[--align] gold run dir (reads DIR/kg/*.jsonl); default: a fresh golden run",
    )
    report_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the markdown report to PATH (default: stdout)",
    )
    report_parser.set_defaults(func=_cmd_reconstruct_report)


def _cmd_reconstruct_scale(args: argparse.Namespace) -> int:
    """Reconstruct + score several varied runs and aggregate the fidelity (esim-ecr.5).

    Generates ``--runs`` deterministic, varied gold runs (different archetype —
    engineering vs retail — and size band), reconstructs and scores each with the
    existing pipeline, and emits an aggregate fidelity report (mean/spread across
    runs). The sim step is always the deterministic ``fake`` backend; only the
    reconstruction's gated LLM steps use ``--backend`` (``fake`` by default, so the
    whole harness runs keyless). Runs land under ``--work-dir`` (a temp dir when
    omitted). ``--json`` emits machine-readable output; ``-o`` writes to a file.
    """
    import contextlib
    import tempfile

    from enterprise_sim.reconstruct import BuildConfig, default_run_specs, run_scale

    build_config = BuildConfig(edge_confidence_threshold=args.edge_threshold)
    specs = default_run_specs(args.runs, seed=args.seed)
    with contextlib.ExitStack() as stack:
        if args.work_dir is not None:
            work_dir: str = str(args.work_dir)
        else:
            work_dir = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="esim-reconstruct-scale-")
            )
        aggregate = run_scale(
            specs,
            work_dir,
            backend=args.backend,
            model=args.model,
            build_config=build_config,
        )

    rendered = aggregate.to_json() if args.json else aggregate.to_markdown()
    if args.output is None:
        print(rendered, end="" if args.json else "\n")
    else:
        args.output.write_text(rendered + ("" if args.json else "\n"), encoding="utf-8")
        node_f1 = aggregate.metrics["node_f1"]
        edge_f1 = aggregate.metrics["edge_f1"]
        print(
            f"enterprise-sim reconstruct scale: {aggregate.run_count} runs "
            f"(node F1 mean={node_f1.mean:.3f}±{node_f1.stdev:.3f}, "
            f"edge F1 mean={edge_f1.mean:.3f}±{edge_f1.stdev:.3f}, "
            f"backend={args.backend}) -> {args.output}",
            file=sys.stderr,
        )
    return 0


def _add_reconstruct_scale_parser(
    reconstruct_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``reconstruct scale [--runs N] [--backend B] -o PATH`` (esim-ecr.5)."""
    from enterprise_sim.reconstruct import HAIKU_MODEL

    scale_parser = reconstruct_subparsers.add_parser(
        "scale",
        help="reconstruct + score several varied runs and aggregate the fidelity",
        description=(
            "Run the reconstruction eval across MORE than the single golden run: "
            "generate several deterministic, varied gold runs (different archetype — "
            "engineering vs retail — and size band), reconstruct and score each, and "
            "emit an aggregate fidelity report (mean/spread across runs). The gold "
            "runs are always the deterministic fake sim; only the reconstruction's "
            "gated LLM steps use the selected backend, so the whole harness runs "
            "keyless with the fake backend."
        ),
    )
    scale_parser.add_argument(
        "--runs",
        type=int,
        default=2,
        metavar="N",
        help="number of varied runs to generate + aggregate (default: 2, the keyless minimum)",
    )
    scale_parser.add_argument(
        "--seed",
        type=int,
        default=7,
        metavar="SEED",
        help="root seed for the varied run configs (each run uses seed+index; default: 7)",
    )
    scale_parser.add_argument(
        "--work-dir",
        dest="work_dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="dir the generated runs land in (default: a temporary dir, removed after)",
    )
    scale_parser.add_argument(
        "--backend",
        default="fake",
        choices=_BACKEND_CHOICES,
        help="LLM backend for the gated reconstruction steps (default: fake, keyless)",
    )
    scale_parser.add_argument(
        "--model",
        default=HAIKU_MODEL,
        metavar="MODEL",
        help=f"model for the gated reconstruction steps (default: {HAIKU_MODEL})",
    )
    scale_parser.add_argument(
        "--edge-threshold",
        dest="edge_threshold",
        type=float,
        default=0.0,
        metavar="CONF",
        help="drop aggregated edges below this confidence (precision/recall knob; default: 0.0)",
    )
    scale_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the aggregate as JSON instead of markdown",
    )
    scale_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the report to PATH (default: stdout)",
    )
    scale_parser.set_defaults(func=_cmd_reconstruct_scale)


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser and its subcommands."""
    parser = argparse.ArgumentParser(
        prog="enterprise-sim",
        description="Extensible enterprise + artifact simulator with a gold knowledge graph.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run a simulation from a config")
    run_parser.add_argument("config", nargs="?", default=None, help="path to a run config")
    run_parser.add_argument(
        "-c",
        "--config",
        dest="config_opt",
        default=None,
        metavar="PATH",
        help="path to a run config (alternative to the positional argument)",
    )
    run_parser.add_argument(
        "-o",
        "--output-dir",
        dest="output_dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="override the config's output_dir (run lands in DIR/<run-id>/)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="estimate artifact count + cost and exit without rendering (D13)",
    )
    run_parser.add_argument(
        "--backend",
        default="fake",
        choices=_BACKEND_CHOICES,
        help="LLM backend to render with (default: fake, the deterministic offline backend)",
    )
    run_parser.add_argument(
        "--max-concurrency",
        dest="max_concurrency",
        type=int,
        default=None,
        metavar="N",
        help="override scale.max_concurrency (parallel scenario renders)",
    )
    run_parser.add_argument(
        "--cost-ceiling",
        dest="cost_ceiling",
        type=float,
        default=None,
        metavar="USD",
        help="override scale.cost_ceiling_usd (abort if the dry-run estimate exceeds it)",
    )
    run_parser.set_defaults(func=_cmd_run)

    lint_parser = subparsers.add_parser("lint", help="static-lint playbooks/processes")
    lint_parser.add_argument("target", nargs="?", default=None, help="path or plugin to lint")
    lint_parser.set_defaults(func=_cmd_lint)

    eval_parser = subparsers.add_parser("eval", help="evaluate a completed run")
    eval_parser.add_argument("run", nargs="?", default=None, help="path to a run output dir")
    eval_parser.add_argument(
        "--judge",
        action="store_true",
        help="also run the LLM-as-judge on a sampled artifact",
    )
    eval_parser.add_argument(
        "--backend",
        default="fake",
        choices=_BACKEND_CHOICES,
        help="LLM backend for --judge (default: fake, deterministic)",
    )
    eval_parser.set_defaults(func=_cmd_eval)

    _add_bench_parser(subparsers)
    _add_reconstruct_parser(subparsers)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch to the selected subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
