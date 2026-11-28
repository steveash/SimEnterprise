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
  * ``project`` (``[access] restricted_projects`` picks that many unwalled projects by a
    hash of the project id): every family of the project, readable by its staff, so the
    project's facts can have a restricted reach;
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

import csv
import hashlib
import io
import json
from collections.abc import Sequence
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
    "record_readers",
    "record_rows",
    "restricted_projects",
]

FORMAT = "simenterprise-access/1"
#: The directories of a project's derived records (one file per project slice).
_RECORD_DIRS = frozenset({"trails", "registry", "observations"})
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


def restricted_projects(nodes: dict[str, dict[str, Any]], config: dict[str, Any]) -> set[str]:
    """The unwalled projects ``[access] restricted_projects`` picks (a hash of the id)."""
    count = int((config.get("access") or {}).get("restricted_projects", 0))
    open_projects = sorted(
        (
            pid
            for pid, n in nodes.items()
            if n["type"] == "Project" and not n["props"].get("walled")
        ),
        key=lambda pid: hashlib.sha256(f"access-project\x00{pid}".encode()).hexdigest(),
    )
    return set(open_projects[:count])


def access_lists(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    config: dict[str, Any],
    files: list[dict[str, Any]],
    slices: Sequence[dict[str, Any]] = (),
) -> dict[str, dict[str, Any]]:
    """Key -> ``{family, paths, readers, teams, reasons}`` for every restricted document.

    A key is an artifact id, or ``record:<path>`` for a project's derived record (its
    trail, registry or observations table) when the project is walled or restricted.
    """
    knobs = config.get("access") or {}
    rate = float(knobs.get("team_restricted_rate", 0.0))
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
    picked = restricted_projects(nodes, config)
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
        if projects & picked:
            allowed.append(set.intersection(*(staff.get(p, set()) for p in projects & picked)))
            reasons.append("project")
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
    # A walled or restricted project's derived records (trails/, registry/, observations/
    # <slice>.csv) state its facts too: they get the project's staff, or the reach of
    # those facts would be everyone (too wide). A slice belongs to a project when its
    # documents are of that project only.
    project_of_path = {
        p: project_of.get(aid)
        for aid, n in nodes.items()
        if n["type"] == "Artifact"
        for p in paths_of.get(str(n["props"].get("path", "")), [])
    }
    for s in slices:
        documents = [f for f in s.get("files", ()) if str(f).startswith("artifacts/")]
        owners = {project_of_path.get(f) for f in documents}
        if len(owners) != 1:
            continue
        (owner,) = owners
        if owner is None:
            continue
        rule = record_readers(nodes, edges, config, owner)
        if rule is None:
            continue
        reason, readers = rule
        for f in s.get("files", ()):
            if str(f).split("/")[0] in _RECORD_DIRS:
                lists[f"record:{f}"] = {
                    "family": f"project:{owner}",
                    "paths": [str(f)],
                    "readers": readers,
                    "teams": [],
                    "reasons": [reason],
                }
    return lists


def record_readers(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    config: dict[str, Any],
    project: str,
) -> tuple[str, list[str]] | None:
    """``(reason, readers)`` of ``project``'s derived records, or ``None`` when they are open.

    A walled or restricted project's records get its staff (the one record rule, shared by
    :func:`access_lists` and the trail writer, decision records 0130 and 0172).
    """
    node = nodes.get(project, {})
    if node.get("type") != "Project":
        return None
    if node["props"].get("walled"):
        reason = "walled"
    elif project in restricted_projects(nodes, config):
        reason = "project"
    else:
        return None
    staff = {e["src"] for e in edges if e["type"] == "member_of" and e["dst"] == project}
    return reason, sorted(staff)


def record_rows(
    rows: Sequence[dict[str, str]],
    project: str | None,
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    config: dict[str, Any],
    files: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """The rows of ``project``'s trail its readers may all read (decision record 0172).

    With R the record's readers (everyone, or :func:`record_readers`) and D a row's
    document's readers (everyone, or its :func:`access_lists` list), a row is kept exactly
    when R is within D. A row whose document is not a file of a known artifact is dropped:
    the direction that must never fail is too wide.
    """
    rule = record_readers(nodes, edges, config, project) if project is not None else None
    record = None if rule is None else set(rule[1])
    lists = access_lists(nodes, edges, config, files)
    paths_of = data_files_of(files)
    readers_of: dict[str, set[str] | None] = {}
    for aid, n in nodes.items():
        if n["type"] != "Artifact":
            continue
        path = str(n["props"].get("path", ""))
        doc = set(lists[aid]["readers"]) if aid in lists else None
        for p in (path, *paths_of.get(path, [])):
            readers_of[p] = doc
    kept: list[dict[str, str]] = []
    for r in rows:
        source = r.get("document")
        if source not in readers_of:
            continue  # unknown: dropped
        doc = readers_of[source]
        if doc is None or (record is not None and record <= doc):
            kept.append(r)
    return kept


def access_document(
    world_id: str,
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    config: dict[str, Any],
    files: list[dict[str, Any]],
    slices: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """The ``access.json`` body: one row per restricted document, sorted by key.

    A derived record's row has ``artifact: null``.
    """
    rows = [
        {"artifact": None if key.startswith("record:") else key, **entry}
        for key, entry in sorted(access_lists(nodes, edges, config, files, slices).items())
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
    slices = manifest.get("slices", [])
    if listed != access_document(manifest["world_id"], nodes, edges, config, files, slices):
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
        project = (
            str(row.get("family", "")).removeprefix("project:")
            if row.get("artifact") is None
            else project_of.get(aid, "")
        )
        if "walled" in row.get("reasons", ()) and not readers <= staff.get(project, set()):
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
    walled_projects = {
        p for p, n in nodes.items() if n["type"] == "Project" and n["props"].get("walled")
    }
    # A walled or restricted project's records are restricted (the too-wide direction).
    # The projects and each slice's owner come from the graph and the file map, not from
    # the rows under check, so a row file that drops a project cannot hide its records.
    restricted = walled_projects | restricted_projects(nodes, config)
    files_of = data_files_of(files)
    project_of_file = {
        p: project_of.get(aid)
        for aid, n in nodes.items()
        if n["type"] == "Artifact"
        for p in files_of.get(str(n["props"].get("path", "")), [])
    }
    for s in slices:
        docs = [f for f in s.get("files", ()) if str(f).startswith("artifacts/")]
        owners = {project_of_file.get(f) for f in docs}
        if len(owners) == 1 and owners & restricted:
            for f in s.get("files", ()):
                if str(f).split("/")[0] in _RECORD_DIRS and f not in covered:
                    findings.append(
                        f"{f}: a walled or restricted project's record that no row covers"
                    )
    # An open trail carries no row of a restricted document (decision record 0172): the
    # rows the export may keep are recomputed, so a hand-edited trail cannot pass.
    for s in slices:
        for f in s.get("files", ()):
            if not str(f).startswith("trails/") or not (export_dir / f).is_file():
                continue
            rows = list(csv.DictReader(io.StringIO((export_dir / f).read_text(encoding="utf-8"))))
            owners = {
                project_of_file.get(d)
                for d in s.get("files", ())
                if str(d).startswith("artifacts/")
            }
            project = next(iter(owners)) if len(owners) == 1 else None
            allowed = record_rows(rows, project, nodes, edges, config, files)
            if len(allowed) != len(rows):
                findings.append(
                    f"{f}: a trail with {len(rows) - len(allowed)} row(s) of a document some "
                    "of its readers may not read, or of an unknown document"
                )
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
