"""Per-document reader lists (decision record 0130): who may read a restricted document.

:func:`access_lists` is the one place that decides it, from an export's gold graph
and config. The export's ``access.json`` (``world export --with-access``) and the
knowledge layer's carrier ``readers`` both read it. The lists are controller-only:
they are gold, never in a file a persona reads.

A document is restricted when one of these holds; its readers are the intersection
of what each rule allows, plus its authors (so a rule never widens another and the
list is never empty):

* it mentions a person's sensitive category (a ``mentions_sensitive`` edge):
  readable by its authors, its reviewers and the people it is about; the reason is
  the category;
* it expresses a walled project (decision record 0036): readable by that project's
  staff; the reason is ``walled``;
* ``[access] team_restricted_rate`` picks it (a hash of the artifact id below the
  rate, among documents no other rule restricts): readable by the members of its
  authors' teams; the reason is ``team``.

:func:`check_access` fails closed: a restricted document with no readers, a reader
outside the roster, an author who is not a reader, or a walled document readable by
anyone off its project's staff is a finding.
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
]

FORMAT = "simenterprise-access/1"
#: Where an export carries its reader lists (``world export --with-access``), as gold.
ACCESS_FILE = "access.json"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _picked(artifact_id: str, rate: float) -> bool:
    """Whether the hash of ``artifact_id`` falls below ``rate`` (order-free, seedless)."""
    digest = hashlib.sha256(f"access\x00{artifact_id}".encode()).hexdigest()
    return int(digest[:12], 16) / float(16**12) < rate


def access_lists(
    nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Artifact id -> ``{readers, teams, reasons}`` for every restricted document."""
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

    lists: dict[str, dict[str, Any]] = {}
    for aid in sorted(i for i, n in nodes.items() if n["type"] == "Artifact"):
        own = authors.get(aid, set())
        allowed: list[set[str]] = []
        reasons: list[str] = []
        teams: set[str] = set()
        if aid in subjects:
            allowed.append(own | reviewers.get(aid, set()) | subjects[aid])
            reasons.extend(sorted(categories[aid]))
        project = project_of.get(aid)
        if project in walled:
            allowed.append(staff.get(project, set()))
            reasons.append("walled")
        if not allowed and rate > 0 and _picked(aid, rate):
            teams = set().union(*(teams_of.get(a, set()) for a in own)) if own else set()
            allowed.append(set().union(*(members.get(t, set()) for t in teams)) | own)
            reasons.append("team")
        if not allowed:
            continue
        readers = set.intersection(*allowed) | own
        lists[aid] = {
            "readers": sorted(readers),
            "teams": sorted(teams),
            "reasons": reasons,
        }
    return lists


def access_document(
    world_id: str,
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    config: dict[str, Any],
    export_of: dict[str, str],
) -> dict[str, Any]:
    """The ``access.json`` body: one row per restricted document, by export path."""
    rows = []
    for aid, entry in access_lists(nodes, edges, config).items():
        path = export_of.get(str(nodes[aid]["props"].get("path", "")))
        if path is None:
            continue
        rows.append({"path": path, "artifact": aid, **entry})
    return {
        "format": FORMAT,
        "world_id": world_id,
        "documents": sorted(rows, key=lambda r: str(r["path"])),
    }


def check_access(export_dir: Path) -> list[str]:
    """Findings of an export's reader lists (empty: they hold). Fails closed."""
    manifest = json.loads((export_dir / "export.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in _jsonl(export_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(export_dir / "kg" / "edges.jsonl")
    config = json.loads((export_dir / "config.json").read_text(encoding="utf-8"))
    export_of = {
        str(f.get("rendered_from") or f.get("source_path") or f["path"]): str(f["path"])
        for f in manifest["files"]
    }
    findings: list[str] = []
    path = export_dir / ACCESS_FILE
    if not path.is_file():
        return [f"{ACCESS_FILE} is missing"]
    listed = json.loads(path.read_text(encoding="utf-8"))
    expected = access_document(manifest["world_id"], nodes, edges, config, export_of)
    if listed != expected:
        findings.append(f"{ACCESS_FILE} is not the reader lists of this export's graph and config")
    roster = {i for i, n in nodes.items() if n["type"] == "Person"}
    authors: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "authored":
            authors.setdefault(e["dst"], set()).add(e["src"])
    files = {str(f["path"]) for f in manifest["files"]}
    for row in listed.get("documents", ()):
        where = row.get("path", "?")
        readers = set(row.get("readers") or ())
        if not readers:
            findings.append(f"{where}: restricted with no readers")
        if row.get("path") not in files:
            findings.append(f"{where}: not a file of the export")
        if not readers <= roster:
            findings.append(f"{where}: readers outside the roster: {sorted(readers - roster)[:3]}")
        own = authors.get(str(row.get("artifact")), set())
        if not own <= readers:
            findings.append(f"{where}: an author is not a reader")
        if "walled" in row.get("reasons", ()):
            project = next(
                (
                    e["dst"]
                    for e in edges
                    if e["type"] == "expresses" and e["src"] == row.get("artifact")
                ),
                None,
            )
            project_staff = {
                e["src"] for e in edges if e["type"] == "member_of" and e["dst"] == project
            }
            if not readers <= project_staff:
                findings.append(f"{where}: a walled document has readers off its project's staff")
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
        print(json.dumps({"world_id": listed["world_id"], "restricted": len(listed["documents"])}))
    return 1 if findings else 0


def add_access_parser(world_subparsers: Any) -> None:
    """Register ``world access-check EXPORT_DIR``."""
    parser = world_subparsers.add_parser(
        "access-check",
        help="check an export's per-document reader lists (access.json, decision record 0130)",
    )
    parser.add_argument("export_dir", type=Path, metavar="EXPORT_DIR", help="a world export")
    parser.set_defaults(func=_cmd_world_access_check)
