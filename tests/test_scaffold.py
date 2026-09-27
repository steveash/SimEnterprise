"""Placeholder tests confirming the scaffold imports and the CLI dispatches."""

from __future__ import annotations

import enterprise_sim
from enterprise_sim.cli import build_parser, main


def test_version_is_exposed() -> None:
    assert enterprise_sim.__version__ == "0.1.0"


def test_parser_has_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args(["world", "create", "demo.toml"])
    assert args.command == "world"
    assert args.config == "demo.toml"


def test_cli_subcommands_dispatch() -> None:
    # `world create`/`eval` with no argument report usage and exit non-zero; `lint` with no
    # target lints the reference playbooks (all clean → 0).
    assert main(["world", "create"]) == 2
    assert main(["eval"]) == 2
    assert main(["lint"]) == 0
