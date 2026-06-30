"""Command-line entrypoint: ``enterprise-sim {run, lint, eval}``.

This is an M1 scaffold stub. Subcommands parse arguments and report that they
are not yet implemented; later milestones wire them to the engine, the quality
stack (§13), and the assembly layer.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from enterprise_sim import __version__

if TYPE_CHECKING:
    from enterprise_sim.authoring.sdk import Playbook
    from enterprise_sim.core.config import RunConfig


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
            estimate = estimate_run(config)
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
        result = execute_run(config)
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


def _add_bench_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the ``bench`` command group; later beads append its subcommands.

    The nested subparser object is stashed on the parser's defaults as
    ``bench_subparsers`` so subsequent milestones (the generator, runners, the
    grader, the report) can register ``generate``/``score``/``report`` without
    re-deriving the group here.
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
        metavar="{generate,score,report}",
    )
    bench_parser.set_defaults(func=_cmd_bench, bench_parser=bench_parser)
    # Exposed for later beads to attach subcommands to the same group.
    bench_parser.set_defaults(bench_subparsers=bench_subparsers)


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
        choices=["fake", "anthropic_api", "bedrock", "claude_cli"],
        help="LLM backend for --judge (default: fake, deterministic)",
    )
    eval_parser.set_defaults(func=_cmd_eval)

    _add_bench_parser(subparsers)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch to the selected subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
