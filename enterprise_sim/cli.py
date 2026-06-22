"""Command-line entrypoint: ``enterprise-sim {run, lint, eval}``.

This is an M1 scaffold stub. Subcommands parse arguments and report that they
are not yet implemented; later milestones wire them to the engine, the quality
stack (§13), and the assembly layer.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from pydantic import ValidationError

from enterprise_sim import __version__


def _cmd_run(args: argparse.Namespace) -> int:
    """Load and validate a run config; the engine itself is not yet wired."""
    if args.config is None:
        print("enterprise-sim run: provide a config path (.toml or .json)")
        return 2

    from enterprise_sim.core.config import ConfigError, load_config

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"enterprise-sim run: {exc}")
        return 2
    except ValidationError as exc:
        print(f"enterprise-sim run: invalid config {args.config}:\n{exc}")
        return 2

    print(
        f"enterprise-sim run: validated config for {config.company.name} "
        f"({config.company.vertical}, {config.company.size.value}); "
        f"seed={config.seed}, window={config.simulation.period_start.isoformat()}"
        f"..{config.simulation.period_end.isoformat()}, projects={len(config.projects)}"
    )
    print("enterprise-sim run: engine not yet implemented")
    return 0


def _cmd_lint(args: argparse.Namespace) -> int:
    """Tier 1 static lint of playbooks/processes (not yet implemented)."""
    print(f"enterprise-sim lint: not yet implemented (target={args.target})")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    """Tier 3 structural + LLM-judge evaluation (not yet implemented)."""
    print(f"enterprise-sim eval: not yet implemented (run={args.run})")
    return 0


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
    run_parser.set_defaults(func=_cmd_run)

    lint_parser = subparsers.add_parser("lint", help="static-lint playbooks/processes")
    lint_parser.add_argument("target", nargs="?", default=None, help="path or plugin to lint")
    lint_parser.set_defaults(func=_cmd_lint)

    eval_parser = subparsers.add_parser("eval", help="evaluate a completed run")
    eval_parser.add_argument("run", nargs="?", default=None, help="path to a run output dir")
    eval_parser.set_defaults(func=_cmd_eval)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch to the selected subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
