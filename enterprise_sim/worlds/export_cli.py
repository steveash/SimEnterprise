"""``enterprise-sim world export``: a world as a text-only export directory.

See :mod:`enterprise_sim.exporters.text_export` for the format.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from enterprise_sim.exporters.text_export import ExportError, export_world

__all__ = ["add_export_parser"]


def _cmd_world_export(args: argparse.Namespace) -> int:
    try:
        result = export_world(args.world_dir, args.out, with_replay=args.with_replay)
    except ExportError as exc:
        print(f"enterprise-sim world export: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"export_dir": str(result.directory), "digest": result.digest, "files": result.files}
        )
    )
    return 0


def _cmd_world_replay(args: argparse.Namespace) -> int:
    from enterprise_sim.worlds.replay import ReplayError, replay_export

    try:
        world_dir = replay_export(args.export_dir, args.worlds_root)
    except ReplayError as exc:
        print(f"enterprise-sim world replay: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"world_dir": str(world_dir)}))
    return 0


def add_export_parser(
    world_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register ``world export WORLD_DIR --out DIR``."""
    parser = world_subparsers.add_parser(
        "export",
        help="write a text-only export of a world (simenterprise-export/1) to DIR/<world-id>/",
    )
    parser.add_argument("world_dir", type=Path, metavar="WORLD_DIR", help="a built world")
    parser.add_argument(
        "--out", required=True, type=Path, metavar="DIR", help="export root directory"
    )
    parser.add_argument(
        "--with-replay",
        dest="with_replay",
        action="store_true",
        default=None,
        help="include replay/llm-cache.jsonl (default: on for live worlds)",
    )
    parser.set_defaults(func=_cmd_world_export)

    replay = world_subparsers.add_parser(
        "replay",
        help="rebuild a world from an export's config.json and recorded responses (no calls)",
    )
    replay.add_argument("export_dir", type=Path, metavar="EXPORT_DIR")
    replay.add_argument("--worlds-root", dest="worlds_root", type=Path, required=True)
    replay.set_defaults(func=_cmd_world_replay)
