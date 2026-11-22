"""``enterprise-sim world profile``: what a built world contains, as one JSON object.

A benchmark author choosing worlds needs the distribution, not the files: how
many people, teams and departments; how many documents of which kinds and
formats; how much the facts churn and how often documents state them wrongly;
how noisy the prose and the raw tables are; how much structured data there is.
:func:`profile_world` reads only the world's outputs (``kg/``, ``artifacts/``,
``data/``), so it describes any world, whichever version built it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from enterprise_sim.worlds.file import read_world

__all__ = ["add_profile_parser", "profile_world"]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.is_dir() else 0


def profile_world(world_dir: Path | str) -> dict[str, Any]:
    """Summarize ``world_dir`` (a built world) as a JSON-ready mapping."""
    world_dir = Path(world_dir)
    world = read_world(world_dir)
    nodes = _jsonl(world_dir / "kg" / "nodes.jsonl")
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    by_type = Counter(str(n["type"]) for n in nodes)
    edge_types = Counter(str(e["type"]) for e in edges)
    people = [n for n in nodes if n["type"] == "Person"]
    artifacts = [n for n in nodes if n["type"] == "Artifact"]
    departments = sorted(
        str(n["props"].get("archetype")) for n in nodes if n["type"] == "Department"
    )
    formats = Counter(Path(str(a["props"].get("path", ""))).suffix or "?" for a in artifacts)
    kinds = Counter(str(a["props"].get("kind")) for a in artifacts)
    quirks = Counter(str(q["kind"]) for a in artifacts for q in a["props"].get("quirks", []) or [])
    stances = Counter(str(e["props"].get("stance")) for e in edges if e["type"] == "asserts")
    versions = [int(n["props"].get("versions", 1)) for n in nodes if n["type"] == "Fact"]
    data: dict[str, Any] = {}
    data_root = world_dir / "data"
    if data_root.is_dir():
        for scenario in sorted(p for p in data_root.iterdir() if p.is_dir()):
            tables = sorted(p.name for p in (scenario / "tables").glob("*") if p.is_dir())
            views = sorted(p.name for p in (scenario / "views").glob("*") if p.is_dir())
            entry: dict[str, Any] = {
                "tables": tables,
                "views": len(views),
                "bytes": _bytes(scenario),
            }
            summary = (world.data if world is not None else {}).get(scenario.name, {})
            if "rows_by_table" in summary:
                entry["rows"] = sum(int(v) for v in summary["rows_by_table"].values())
            quirk_file = scenario / "quirks.json"
            if quirk_file.is_file():
                entry["quirks"] = json.loads(quirk_file.read_text(encoding="utf-8"))["counts"]
            data[scenario.name] = entry
    authored = {str(e["dst"]) for e in edges if e["type"] == "authored"}
    words = 0
    for artifact in artifacts:
        path = world_dir / str(artifact["props"].get("path", ""))
        if path.suffix == ".md" and path.is_file():
            words += len(path.read_text(encoding="utf-8").split())
    return {
        "world_id": world.world_id if world is not None else world_dir.name,
        "render_backend": world.render_backend if world is not None else "",
        "window": world.window if world is not None else {},
        "org": {
            "departments": departments,
            "teams": by_type.get("Team", 0),
            "people": len(people),
            "leavers": sum(1 for p in people if "left_at" in p["props"]),
            "joiners": sum(1 for p in people if "joined_at" in p["props"]),
            "transfers": sum(1 for p in people if "transferred_at" in p["props"]),
            "projects": by_type.get("Project", 0),
            "cross_team_dependencies": edge_types.get("depends_on", 0),
        },
        "documents": {
            "artifacts": len(artifacts),
            "formats": dict(sorted(formats.items())),
            "kinds": dict(kinds.most_common()),
            "markdown_words": words,
            "authored_share": round(
                sum(1 for a in artifacts if a["id"] in authored) / len(artifacts), 3
            )
            if artifacts
            else 0.0,
            "references": edge_types.get("references", 0),
            "quirks": dict(sorted(quirks.items())),
            "sensitive": dict(
                sorted(
                    Counter(
                        str(label["category"])
                        for a in artifacts
                        for label in a["props"].get("sensitive", []) or []
                    ).items()
                )
            ),
            "chat_threads": sum(1 for a in artifacts if a["props"].get("kind") == "chat_thread"),
            "code_files": sum(1 for a in artifacts if a["props"].get("medium") == "code"),
        },
        "facts": {
            "facts": len(versions),
            "versions": sum(versions),
            "changed": sum(1 for v in versions if v > 1),
            "retracted": sum(
                1 for n in nodes if n["type"] == "FactVersion" and n["props"].get("retracted_at")
            ),
            "assertions": dict(sorted(stances.items())),
        },
        "data": data,
        "kg": {
            "nodes": len(nodes),
            "edges": len(edges),
            "node_types": dict(sorted(by_type.items())),
        },
        "bytes": {
            "artifacts": _bytes(world_dir / "artifacts"),
            "kg": _bytes(world_dir / "kg"),
            "data": _bytes(data_root),
        },
    }


def _cmd_world_profile(args: Any) -> int:
    print(json.dumps(profile_world(args.world_dir), indent=2, sort_keys=True))
    return 0


def add_profile_parser(world_subparsers: Any) -> None:
    """Register ``world profile WORLD_DIR``."""
    parser = world_subparsers.add_parser(
        "profile", help="print what a built world contains (org, documents, facts, noise, data)"
    )
    parser.add_argument("world_dir", type=Path, metavar="WORLD_DIR", help="a built world")
    parser.set_defaults(func=_cmd_world_profile)
