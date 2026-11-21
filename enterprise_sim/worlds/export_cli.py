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
        result = export_world(args.world_dir, args.out)
    except ExportError as exc:
        print(f"enterprise-sim world export: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"export_dir": str(result.directory), "digest": result.digest, "files": result.files}
        )
    )
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
    parser.set_defaults(func=_cmd_world_export)
