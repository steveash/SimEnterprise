"""Per-document reader lists (decision record 0130): who may read a restricted document.

The direction that must never fail is **too wide**: a list must never let anyone read
a fact it says they cannot. Every rule below makes the uncertain case narrower.

:func:`access_lists` is the one place that decides it, from an export's gold graph,
its file list and its config. The export's ``access.json`` (``world export
--with-access``) and the knowledge layer's access fields both read it. The lists are
controller-only gold.

* **The unit is a family:** the artifacts that share an ``event`` (a document with its
  twins, drafts, renderings, near-copies and following chat thread). A family is
  restricted as a whole and every member gets the family's list, so no public twin of
  a restricted document exists.
* **Rules are kinds; across kinds the readers intersect, and the family's authors are
  always readers:**

  * ``sensitive`` (some member mentions a person's sensitive category): the family's
    authors, reviewers and every person it is about; the reasons are the categories;
  * ``walled`` (decision record 0036): the family's project staff;
  * ``team`` (``[access] team_restricted_rate`` picks the family by a hash of its event
    id; only families no other rule restricts and that have an ``authored`` edge):
    everyone ever a member of the authors' teams.

* **Authors** are the ``authored`` edges; a **person of the world** is every Person
  node, leavers included.
* ``paths`` lists every exported data file of an artifact, through ``source_path`` and
  ``rendered_from``.

:func:`check_access` fails closed (see its docstring).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = [
    "ACCESS_FILE",
    "FORMAT",
    "access_document",
    "access_lists",
    "add_access_parser",
    "check_access",
    "data_files_of",
]

FORMAT = "simenterprise-access/1"
#: Where an export carries its reader lists (``world export --with-access``), as gold.
ACCESS_FILE = "access.json"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _picked(key: str, rate: float) -> bool:
    """Whether the hash of ``key`` falls below ``rate`` (order-free, seedless)."""
    digest = hashlib.sha256(f"access\x00{key}".encode()).hexdigest()
    return int(digest[:12], 16) / float(16**12) < rate


def data_files_of(files: list[dict[str, Any]]) -> dict[str, list[str]]:
    """World path -> every exported data file rendered or copied from it."""
    out: dict[str, list[str]] = {}
    for f in files:
        if f.get("role") != "data":
            continue
        source = str(f.get("rendered_from") or f.get("source_path") or f["path"])
        out.setdefault(source, []).append(str(f["path"]))
    return {k: sorted(v) for k, v in out.items()}


def access_lists(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    config: dict[str, Any],
    files: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Artifact id -> ``{family, paths, readers, teams, reasons}`` for every restricted one."""
    rate = float((config.get("access") or {}).get("team_restricted_rate", 0.0))
    authors: dict[str, set[str]] = {}
    reviewers: dict[str, set[str]] = {}
    subjects: dict[str, set[str]] = {}
    categories: dict[str, set[str]] = {}
    project_of: dict[str, str] = {}
    staff: dict[str, set[str]] = {}
    teams_of: dict[str, set[str]] = {}
    members: dict[str, set[str]] = {}
    for e in edges:
        kind, src, dst = e["type"], e["src"], e["dst"]
        if kind == "authored":
            authors.setdefault(dst, set()).add(src)
        elif kind == "reviewed":
            reviewers.setdefault(dst, set()).add(src)
        elif kind == "mentions_sensitive":
            subjects.setdefault(src, set()).add(dst)
            categories.setdefault(src, set()).add(str(e["props"].get("category", "sensitive")))
        elif kind == "expresses":
            project_of[src] = dst
        elif kind == "member_of":
            target = nodes.get(dst, {}).get("type")
            if target == "Project":
                staff.setdefault(dst, set()).add(src)
            elif target == "Team":
                teams_of.setdefault(src, set()).add(dst)
                members.setdefault(dst, set()).add(src)
    walled = {
        pid for pid, n in nodes.items() if n["type"] == "Project" and n["props"].get("walled")
    }
    paths_of = data_files_of(files)

    families: dict[str, list[str]] = {}
    for aid, n in nodes.items():
        if n["type"] == "Artifact":
            families.setdefault(str(n["props"].get("event") or aid), []).append(aid)

    lists: dict[str, dict[str, Any]] = {}
    for family, ids in sorted(families.items()):
        ids = sorted(ids)
        own = set().union(*(authors.get(a, set()) for a in ids))
        allowed: list[set[str]] = []
        reasons: list[str] = []
        teams: set[str] = set()
        mentioned = [a for a in ids if a in subjects]
        if mentioned:
            about = set().union(*(subjects[a] for a in mentioned))
            seen = set().union(*(reviewers.get(a, set()) for a in ids))
            allowed.append(own | seen | about)
            reasons.extend(sorted(set().union(*(categories[a] for a in mentioned))))
        projects = {p for p in (project_of.get(a) for a in ids) if p is not None}
        if projects & walled:
            # one family, one project; a family that spans projects keeps every staff's
            # intersection, which only narrows
            allowed.append(set.intersection(*(staff.get(p, set()) for p in projects & walled)))
            reasons.append("walled")
        if not allowed and own and rate > 0 and _picked(family, rate):
            teams = set().union(*(teams_of.get(a, set()) for a in own))
            allowed.append(set().union(*(members.get(t, set()) for t in teams)) | own)
            reasons.append("team")
        if not allowed:
            continue
        readers = sorted(set.intersection(*allowed) | own)
        for aid in ids:
            lists[aid] = {
                "family": family,
                "paths": paths_of.get(str(nodes[aid]["props"].get("path", "")), []),
                "readers": readers,
                "teams": sorted(teams),
                "reasons": reasons,
            }
    return lists


def access_document(
    world_id: str,
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    config: dict[str, Any],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    """The ``access.json`` body: one row per restricted artifact, sorted by artifact."""
    rows = [
        {"artifact": aid, **entry}
        for aid, entry in sorted(access_lists(nodes, edges, config, files).items())
    ]
    return {"format": FORMAT, "world_id": world_id, "documents": rows}


def check_access(export_dir: Path) -> list[str]:
    """Findings of an export's reader lists (empty: they hold). Fails closed.

    Refused: a list that is not :func:`access_lists`' output; a row with no readers; a
    reader who is not a person of the world; an author who is not a reader; a family
    whose members' lists differ; a walled family read off its project's staff; a row
    path that is not an exported data file; and an exported data file of a restricted
    artifact that no row covers.
    """
    path = export_dir / ACCESS_FILE
    if not path.is_file():
        return [f"{ACCESS_FILE} is missing"]
    manifest = json.loads((export_dir / "export.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in _jsonl(export_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(export_dir / "kg" / "edges.jsonl")
    config = json.loads((export_dir / "config.json").read_text(encoding="utf-8"))
    files = [f for f in manifest["files"] if f["path"] != ACCESS_FILE]
    listed = json.loads(path.read_text(encoding="utf-8"))
    findings: list[str] = []
    if listed != access_document(manifest["world_id"], nodes, edges, config, files):
        findings.append(f"{ACCESS_FILE} is not the reader lists of this export")
    people = {i for i, n in nodes.items() if n["type"] == "Person"}
    data = {str(f["path"]) for f in files if f.get("role") == "data"}
    authors: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "authored":
            authors.setdefault(e["dst"], set()).add(e["src"])
    project_of = {e["src"]: e["dst"] for e in edges if e["type"] == "expresses"}
    staff: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "member_of" and nodes.get(e["dst"], {}).get("type") == "Project":
            staff.setdefault(e["dst"], set()).add(e["src"])
    by_family: dict[str, set[tuple[str, ...]]] = {}
    covered: set[str] = set()
    for row in listed.get("documents", ()):
        aid = str(row.get("artifact"))
        readers = set(row.get("readers") or ())
        if not readers:
            findings.append(f"{aid}: restricted with no readers")
        if not readers <= people:
            findings.append(f"{aid}: readers who are not people of the world")
        if not authors.get(aid, set()) <= readers:
            findings.append(f"{aid}: an author is not a reader")
        by_family.setdefault(str(row.get("family")), set()).add(tuple(sorted(readers)))
        if "walled" in row.get("reasons", ()) and not readers <= staff.get(
            project_of.get(aid, ""), set()
        ):
            findings.append(f"{aid}: a walled document has readers off its project's staff")
        for p in row.get("paths", ()):
            if p not in data:
                findings.append(f"{aid}: {p} is not an exported data file")
            covered.add(p)
    for family, lists in by_family.items():
        if len(lists) > 1:
            findings.append(f"family {family}: its members' lists differ")
    restricted_paths = {
        str(nodes[str(r.get("artifact"))]["props"].get("path", ""))
        for r in listed.get("documents", ())
        if str(r.get("artifact")) in nodes
    }
    for f in files:
        source = str(f.get("rendered_from") or f.get("source_path") or f["path"])
        if f.get("role") == "data" and source in restricted_paths and f["path"] not in covered:
            findings.append(f"{f['path']}: a file of a restricted artifact that no row covers")
    return findings


def _cmd_world_access_check(args: Any) -> int:
    import sys

    try:
        findings = check_access(args.export_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"enterprise-sim world access-check: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"enterprise-sim world access-check: {finding}", file=sys.stderr)
    if not findings:
        listed = json.loads((args.export_dir / ACCESS_FILE).read_text(encoding="utf-8"))
        families = {r["family"] for r in listed["documents"]}
        print(
            json.dumps(
                {
                    "world_id": listed["world_id"],
                    "restricted": len(listed["documents"]),
                    "families": len(families),
                }
            )
        )
    return 1 if findings else 0


def add_access_parser(world_subparsers: Any) -> None:
    """Register ``world access-check EXPORT_DIR``."""
    parser = world_subparsers.add_parser(
        "access-check",
        help="check an export's per-document reader lists (access.json, decision record 0130)",
    )
    parser.add_argument("export_dir", type=Path, metavar="EXPORT_DIR", help="a world export")
    parser.set_defaults(func=_cmd_world_access_check)
