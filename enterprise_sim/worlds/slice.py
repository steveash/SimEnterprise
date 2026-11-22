"""Project slices: which files make up one project, and its test-set knowledge drafts.

A Spindle gym test set's ``environment/`` comes from the catalogued package, never
from a world directory, so the set stays pinned to the shared catalog
(datasets-cc, decision 0022): ``spindle-datasets materialize simx/<co>@<v>#<slice>``
writes the files. Two things here make that work:

* :func:`project_slices` — the slices a world's export declares
  (``export.json`` → ``slices``): per project, the export paths of every
  document that expresses it. The export and :func:`slice_world` both use
  :func:`slice_documents`, so the two can never disagree on membership.
* :func:`slice_world` — writes, for one project, the author/controller files
  that go **beside** the materialized environment, never into it:

  * ``knowledge.draft.toml`` — one derived item per ledger fact of the project:
    ``owner = "environment"``, ``values = [<the FactVersion token>]`` (at least 3
    characters, no whitespace), ``derived_from`` = the materialized paths
    (``environment/docs/simx/<export path>``) of the slice documents stating the
    value true at ``at``;
  * ``answers.json`` — the answer key: per fact the value true at ``at``, its
    history, and which slice documents state it stale, wrongly, hedged, or
    announce a change; items it could not draft (``undiscoverable``, with why);
    the slice's quirk labels.

Both are drafts for the test-set author, who keeps personas, goals, claims and
oracles, and must treat them as gold (controller-only) under the gym's rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from enterprise_sim.exporters.text_export import export_path

__all__ = [
    "MATERIALIZED_DOCS",
    "SliceError",
    "materialized_doc_path",
    "project_slices",
    "slice_documents",
    "slice_name",
    "slice_world",
]

#: Where ``spindle-datasets materialize`` puts a slice's documents in a test set.
MATERIALIZED_DOCS = "environment/docs/simx"
_TRUE_STANCES = ("current", "update", "hedged")
#: The gym's discovered-knowledge rule: a value is at least 3 characters, no whitespace.
_MIN_TOKEN = 3
_RENDERED = (".docx", ".pptx")
_TEXT = (".md", ".json")
_SLICE_NAME = re.compile(r"[^a-z0-9-]+")


class SliceError(Exception):
    """Raised when a slice cannot be cut (unknown project, existing target)."""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _toml_str(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _export_rel(world_rel: str) -> str | None:
    """The export path of a world document (its rendering for binary formats)."""
    suffix = Path(world_rel).suffix.lower()
    if suffix in _RENDERED:
        return export_path(f"{world_rel}.md")
    if suffix in _TEXT:
        return export_path(world_rel)
    return None


def materialized_doc_path(world_rel: str) -> str | None:
    """Where a world document lands in a test set after ``materialize``."""
    rel = _export_rel(world_rel)
    return f"{MATERIALIZED_DOCS}/{rel}" if rel is not None else None


def slice_documents(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    projects: list[str],
    *,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """The Artifact nodes expressing any of ``projects`` (dated no later than ``until``)."""
    members = {e["src"] for e in edges if e["type"] == "expresses" and e["dst"] in projects}
    found = [
        nodes[a]
        for a in members
        if a in nodes
        and nodes[a]["type"] == "Artifact"
        and (until is None or str(nodes[a]["props"].get("date", "")) <= until[:10])
        and _export_rel(str(nodes[a]["props"].get("path", ""))) is not None
    ]
    return sorted(found, key=lambda n: (str(n["props"].get("date", "")), n["id"]))


def slice_name(project: str) -> str:
    """A catalog slice name for ``project``: ``[a-z][a-z0-9-]*``, at most 32 characters."""
    base = _SLICE_NAME.sub("-", project.split(":", 1)[-1].lower()).strip("-") or "project"
    if not base[0].isalpha():
        base = f"p-{base}"
    if len(base) <= 32:
        return base
    tag = hashlib.sha256(project.encode()).hexdigest()[:6]
    return f"{base[:25].rstrip('-')}-{tag}"


def project_slices(world_dir: Path | str) -> list[dict[str, Any]]:
    """One slice per project: name, description and the export paths of its documents."""
    world_dir = Path(world_dir)
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    slices: list[dict[str, Any]] = []
    used: set[str] = set()
    for project in sorted(i for i, n in nodes.items() if n["type"] == "Project"):
        docs = slice_documents(nodes, edges, [project])
        files = sorted(
            {
                rel
                for d in docs
                if (rel := _export_rel(str(d["props"]["path"]))) is not None
                and (world_dir / str(d["props"]["path"])).is_file()
            }
        )
        if not files:
            continue
        name = slice_name(project)
        if name in used:
            raise SliceError(f"slice name {name!r} is used twice")
        used.add(name)
        title = str(nodes[project]["props"].get("name", project))
        slices.append(
            {"name": name, "description": f"Documents of the project {title}.", "files": files}
        )
    return slices


def slice_world(
    world_dir: Path | str,
    project: str,
    out: Path | str,
    *,
    at: str | None = None,
    with_dependencies: bool = False,
) -> dict[str, Any]:
    """Write ``knowledge.draft.toml`` and ``answers.json`` for ``project`` into ``out``."""
    world_dir, out = Path(world_dir), Path(out)
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    if nodes.get(project, {}).get("type") != "Project":
        projects = sorted(i for i, n in nodes.items() if n["type"] == "Project")
        raise SliceError(f"{project!r} is not a project of this world (projects: {projects})")
    if out.exists() and any(out.iterdir()):
        raise SliceError(f"{out}: exists and is not empty")
    world = json.loads((world_dir / ".world").read_text(encoding="utf-8"))
    cut = at or str(world["window"]["end"])
    cut_dt = datetime.fromisoformat(cut if "T" in cut else f"{cut}T23:59:59")

    scope = [project]
    if with_dependencies:
        scope += sorted(
            e["dst"] for e in edges if e["type"] == "depends_on" and e["src"] == project
        )
    docs = slice_documents(nodes, edges, scope, until=cut)
    paths: dict[str, str] = {}
    for doc in docs:
        mapped = materialized_doc_path(str(doc["props"]["path"]))
        if mapped is not None:
            paths[doc["id"]] = mapped
    versions: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        if node["type"] == "FactVersion":
            versions.setdefault(str(node["props"]["fact"]), []).append(node["props"])
    asserts: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        if edge["type"] == "asserts" and edge["src"] in paths:
            asserts.setdefault(edge["dst"], []).append(edge)

    facts = sorted(
        (n for n in nodes.values() if n["type"] == "Fact" and n["props"].get("subject") in scope),
        key=lambda n: n["id"],
    )
    knowledge: list[str] = []
    answers: dict[str, Any] = {
        "world_id": world["world_id"],
        "project": project,
        "slices": [slice_name(p) for p in scope],
        "at": cut,
        "facts": [],
    }
    undiscoverable: list[dict[str, str]] = []
    for index, fact in enumerate(facts, start=1):
        history = sorted(versions.get(fact["id"], []), key=lambda v: int(v["version"]))
        true = [v for v in history if datetime.fromisoformat(str(v["valid_from"])) <= cut_dt]
        if not true:
            continue
        current = true[-1]
        by_stance: dict[str, set[str]] = {}
        for edge in asserts.get(fact["id"], []):
            by_stance.setdefault(str(edge["props"]["stance"]), set()).add(paths[edge["src"]])
        sources = sorted(
            {
                paths[e["src"]]
                for e in asserts.get(fact["id"], [])
                if e["props"]["stance"] in _TRUE_STANCES
                and e["props"].get("stated_version") == current["version"]
            }
        )
        label = str(fact["props"].get("label", fact["props"].get("attribute")))
        subject_node = nodes.get(str(fact["props"]["subject"]), {})
        subject = str(subject_node.get("props", {}).get("name", ""))
        token = str(current.get("token", current["value"]))
        answers["facts"].append(
            {
                "fact": fact["id"],
                "question": fact["props"].get("question"),
                "current": {"value": current["value"], "token": token},
                "history": [
                    {"version": v["version"], "value": v["value"], "valid_from": v["valid_from"]}
                    for v in true
                ],
                "documents": {s: sorted(p) for s, p in sorted(by_stance.items())},
            }
        )
        if not sources:
            undiscoverable.append({"fact": fact["id"], "reason": "no slice document states it"})
            continue
        if len(token) < _MIN_TOKEN or any(ch.isspace() for ch in token):
            reason = f"value token {token!r} not canonical"
            undiscoverable.append({"fact": fact["id"], "reason": reason})
            continue
        item_text = f"As of {cut[:10]}, the {label} of {subject} is {current['value']}."
        knowledge += [
            "[[knowledge]]",
            f'id = "K{index}"',
            'owner = "environment"',
            f"derived_from = [{', '.join(_toml_str(s) for s in sources)}]",
            f"text = {_toml_str(item_text)}",
            f"values = [{_toml_str(token)}]",
            'channels = ["note", "board"]',
            "",
        ]
    answers["undiscoverable"] = undiscoverable
    answers["quirks"] = {
        paths[d["id"]]: d["props"]["quirks"]
        for d in docs
        if d["id"] in paths and d["props"].get("quirks")
    }
    out.mkdir(parents=True, exist_ok=True)
    header = [
        f"# Draft knowledge items for {project} ({world['world_id']}), as of {cut}.",
        "# Generated by enterprise-sim world slice. Paths are where",
        "# `spindle-datasets materialize simx/<company>@<version>#<slice>` writes the documents.",
        "# Gold: controller/author files only; never place them under environment/.",
        "",
    ]
    (out / "knowledge.draft.toml").write_text("\n".join(header + knowledge), encoding="utf-8")
    payload = json.dumps(answers, indent=1, sort_keys=True) + "\n"
    (out / "answers.json").write_text(payload, encoding="utf-8")
    return {
        "out": str(out),
        "documents": len(paths),
        "knowledge": sum(1 for line in knowledge if line == "[[knowledge]]"),
        "undiscoverable": len(undiscoverable),
        "slices": answers["slices"],
    }


def _cmd_world_slice(args: Any) -> int:
    import sys

    try:
        summary = slice_world(
            args.world_dir,
            args.project,
            args.out,
            at=args.at,
            with_dependencies=args.with_dependencies,
        )
    except SliceError as exc:
        print(f"enterprise-sim world slice: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary))
    return 0


def add_slice_parser(world_subparsers: Any) -> None:
    """Register ``world slice WORLD_DIR --project ID --out DIR``."""
    parser = world_subparsers.add_parser(
        "slice", help="draft test-set knowledge and its answer key for one project of a world"
    )
    parser.add_argument("world_dir", type=Path, metavar="WORLD_DIR")
    parser.add_argument("--project", required=True, help="a Project node id (project:...)")
    parser.add_argument("--out", required=True, type=Path, metavar="DIR")
    parser.add_argument("--at", default=None, help="scenario date (YYYY-MM-DD); default window end")
    parser.add_argument("--with-dependencies", action="store_true", dest="with_dependencies")
    parser.set_defaults(func=_cmd_world_slice)
