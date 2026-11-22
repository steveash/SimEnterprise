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
    "canonical_token",
    "distinctive_token",
    "source_kind",
    "typed_value",
    "core_documents",
    "core_slice_name",
    "REGISTRY_COLUMNS",
    "TOOL_VOCABULARY",
    "OBSERVATION_COLUMNS",
    "TRAIL_COLUMNS",
    "SliceError",
    "materialized_doc_path",
    "project_slices",
    "project_observations",
    "project_registry",
    "project_trail",
    "slice_documents",
    "slice_name",
    "slice_world",
    "vocabulary_conflicts",
]

#: Where ``spindle-datasets materialize`` puts a slice's documents in a test set.
MATERIALIZED_DOCS = "environment/docs/simx"
_TRUE_STANCES = ("current", "update", "hedged")
#: The gym's discovered-knowledge rule: a value is at least 3 characters, no whitespace.
_MIN_TOKEN = 3
#: TS-DISCOVERED's grouping separators: a declared value may hold none of them.
_GROUPING = (",", "_", "'", "\u00a0", "\u202f")
_RENDERED = (".docx", ".pptx", ".eml", ".ics", ".py", ".toml")
_TEXT = (".md", ".json", ".jsonl")
_SLICE_NAME = re.compile(r"[^a-z0-9-]+")


class SliceError(Exception):
    """Raised when a slice cannot be cut (unknown project, existing target)."""


def canonical_token(token: str) -> bool:
    """Whether ``token`` can be a declared value under the gym's discovered-knowledge rule."""
    return (
        len(token) >= _MIN_TOKEN
        and not any(ch.isspace() for ch in token)
        and not any(sep in token for sep in _GROUPING)
    )


def _occurrences(token: str, texts: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Every line of the slice documents that holds ``token`` (case folded), as a leak report."""
    needle = token.casefold()
    found: list[dict[str, Any]] = []
    for path in sorted(texts):
        for number, line in enumerate(texts[path], start=1):
            if needle in line.casefold():
                found.append({"path": path, "line": number, "kind": source_kind(path)})
    return found


#: Common words and short tokens a prompt could hold by chance: a value equal
#: to one is a substring hazard under the gym's leak rule (sets-cc).
_COMMON_TOKENS = frozenset(
    {
        "weekly",
        "daily",
        "monthly",
        "hourly",
        "annually",
        "quarterly",
        "track",
        "risk",
        "blocked",
        "schedule",
        "behind",
        "once",
        "sprint",
        "week",
        "twice",
        "days",
        "every",
        "online",
        "metro",
        "region",
        "northern",
        "coastal",
    }
)


def distinctive_token(token: str) -> bool:
    """Whether ``token`` is unlikely to occur by chance in a prompt (digit, dot, capital)."""
    if token.casefold() in _COMMON_TOKENS:
        return False
    return any(ch.isdigit() for ch in token) or "." in token or any(ch.isupper() for ch in token)


def typed_value(value: str) -> dict[str, Any]:
    """A machine-readable form of a fact value (for graders that compute, not match)."""
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return {"type": "date", "value": text}
    money = re.fullmatch(r"\$(\d+(?:\.\d+)?)([kM])", text)
    if money:
        scale = 1_000 if money.group(2) == "k" else 1_000_000
        return {"type": "usd", "value": round(float(money.group(1)) * scale)}
    time_of_day = re.search(r"(\d{2}):(\d{2}) UTC", text)
    if time_of_day:
        return {"type": "time_utc", "value": f"{time_of_day.group(1)}{time_of_day.group(2)}"}
    number = re.fullmatch(r"([\d,]+)\s*(ms|units|open roles|weekly active accounts)?", text)
    if number:
        return {
            "type": "count",
            "value": int(number.group(1).replace(",", "")),
            "unit": number.group(2) or "",
        }
    percent = re.fullmatch(r"(\d+)%", text)
    if percent:
        return {"type": "percent", "value": int(percent.group(1))}
    version = re.fullmatch(r"v(\d+)\.(\d+)", text)
    if version:
        return {"type": "version", "value": text}
    cadence = {
        "hourly": "hourly",
        "daily": "daily",
        "weekly": "weekly",
        "every two weeks": "biweekly",
        "monthly": "monthly",
        "quarterly": "quarterly",
        "annually": "annually",
        "twice a week": "twice_weekly",
        "once per sprint": "per_sprint",
        "twice daily": "twice_daily",
        "every 15 minutes": "every_15_minutes",
    }
    if text in cadence:
        return {"type": "cadence", "value": cadence[text]}
    return {"type": "text", "value": text}


def source_kind(path: str) -> str:
    """The kind of source a materialized path is: document, chat, code, registry or trail."""
    if "/registry/" in path or path.startswith("registry/"):
        return "registry"
    if "/trails/" in path or path.startswith("trails/"):
        return "trail"
    if path.endswith(".chat.jsonl"):
        return "chat"
    if "/repo/" in path:
        return "code"
    return "document"


def _documents_by_version(
    edges: list[dict[str, Any]], paths: dict[str, str], docs: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each document stating the fact: its date, the version it states, and its stance."""
    rows = [
        {
            "path": paths[e["src"]],
            "date": str(docs.get(e["src"], {}).get("props", {}).get("date", "")),
            "stated_version": e["props"].get("stated_version"),
            "stance": e["props"]["stance"],
            "kind": source_kind(paths[e["src"]]),
        }
        for e in edges
    ]
    return sorted(rows, key=lambda r: (r["date"], r["path"]))


def _flag_shared_values(facts: list[dict[str, Any]]) -> None:
    """Mark facts of one slice whose current canonical values coincide (one value, two roles)."""
    by_token: dict[str, list[str]] = {}
    for fact in facts:
        by_token.setdefault(str(fact["current"]["canonical"]).casefold(), []).append(fact["fact"])
    for fact in facts:
        others = [
            f for f in by_token[str(fact["current"]["canonical"]).casefold()] if f != fact["fact"]
        ]
        fact["shared_with"] = others


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


#: A core slice: at most this many changed facts and documents (casebook size).
CORE_FACTS = 6
CORE_DOCS = 24


def core_slice_name(project: str) -> str:
    """The catalog name of a project's core slice (``<name>-core``, at most 32 characters)."""
    base = slice_name(project)
    return f"{base[:27].rstrip('-')}-core"


def core_documents(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    project: str,
    *,
    until: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """A casebook-size cut of one project: its changed facts and the documents that state them.

    Up to :data:`CORE_FACTS` facts with at least two versions (so every fact has
    a superseded value); for each, the latest document stating each version,
    every document announcing a change, and up to two documents stating a
    superseded or wrong value; at most :data:`CORE_DOCS` documents in all,
    earliest-dated dropped first. Returns ``(fact ids, artifact nodes)``.
    """
    all_docs = {d["id"]: d for d in slice_documents(nodes, edges, [project], until=until)}
    changed = sorted(
        n["id"]
        for n in nodes.values()
        if n["type"] == "Fact"
        and n["props"].get("subject") == project
        and int(n["props"].get("versions", 1)) > 1
    )[:CORE_FACTS]
    by_fact: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        if edge["type"] == "asserts" and edge["dst"] in changed and edge["src"] in all_docs:
            by_fact.setdefault(edge["dst"], []).append(edge)
    chosen: set[str] = set()
    for fact in changed:
        edges_of = sorted(
            by_fact.get(fact, []),
            key=lambda e: (str(all_docs[e["src"]]["props"].get("date", "")), e["src"]),
        )
        latest: dict[object, str] = {}
        misleading: list[str] = []
        for edge in edges_of:
            stance = edge["props"]["stance"]
            if stance in _TRUE_STANCES:
                latest[edge["props"].get("stated_version")] = edge["src"]
            if stance == "update":
                chosen.add(edge["src"])
            if stance in ("stale", "conflicting"):
                misleading.append(edge["src"])
        chosen.update(latest.values())
        chosen.update(misleading[-2:])
    docs = sorted(
        (all_docs[d] for d in chosen),
        key=lambda n: (str(n["props"].get("date", "")), n["id"]),
    )
    return changed, docs[-CORE_DOCS:]


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
        _, core = core_documents(nodes, edges, project)
        core_files = sorted(
            {
                rel
                for d in core
                if (rel := _export_rel(str(d["props"]["path"]))) is not None
                and (world_dir / str(d["props"]["path"])).is_file()
            }
        )
        if core_files:
            core_name = core_slice_name(project)
            if core_name in used:
                raise SliceError(f"slice name {core_name!r} is used twice")
            used.add(core_name)
            slices.append(
                {
                    "name": core_name,
                    "description": (
                        f"Casebook-size cut of {title}: its changed facts and the documents "
                        "stating their current, superseded and wrong values."
                    ),
                    "files": core_files,
                }
            )
    return slices


def slice_world(
    world_dir: Path | str,
    project: str,
    out: Path | str,
    *,
    at: str | None = None,
    with_dependencies: bool = False,
    core: bool = False,
) -> dict[str, Any]:
    """Write ``knowledge.draft.toml`` and ``answers.json`` for ``project`` into ``out``.

    With ``core``, only the project's core slice (:func:`core_documents`): its
    changed facts and the documents stating them.
    """
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
    core_facts: list[str] | None = None
    if core:
        scope = [project]
        core_facts, docs = core_documents(nodes, edges, project, until=cut)
    else:
        docs = slice_documents(nodes, edges, scope, until=cut)
    paths: dict[str, str] = {}
    for doc in docs:
        mapped = materialized_doc_path(str(doc["props"]["path"]))
        if mapped is not None:
            paths[doc["id"]] = mapped
    texts = {path: _doc_lines(world_dir, doc) for doc in docs if (path := paths.get(doc["id"]))}
    docs_by_id = {doc["id"]: doc for doc in docs}
    versions: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        if node["type"] == "FactVersion":
            versions.setdefault(str(node["props"]["fact"]), []).append(node["props"])
    asserts: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        if edge["type"] == "asserts" and edge["src"] in paths:
            asserts.setdefault(edge["dst"], []).append(edge)

    facts = sorted(
        (
            n
            for n in nodes.values()
            if n["type"] == "Fact"
            and n["props"].get("subject") in scope
            and (core_facts is None or n["id"] in core_facts)
        ),
        key=lambda n: n["id"],
    )
    knowledge: list[str] = []
    answers: dict[str, Any] = {
        "world_id": world["world_id"],
        "project": project,
        "slices": [core_slice_name(project)] if core else [slice_name(p) for p in scope],
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
        token = str(current.get("canonical", current["value"]))
        answers["facts"].append(
            {
                "fact": fact["id"],
                "question": fact["props"].get("question"),
                "current": {"value": current["value"], "canonical": token},
                "history": [
                    {"version": v["version"], "value": v["value"], "valid_from": v["valid_from"]}
                    for v in true
                ],
                "documents": {s: sorted(p) for s, p in sorted(by_stance.items())},
                "valid_from": current["valid_from"],
                "value_kind": "person"
                if fact["props"].get("attribute") == "owner"
                else "canonical",
                "stale_values": _stale_values(asserts.get(fact["id"], []), paths, current),
                "occurrences": _occurrences(token, texts),
                "distinctive": distinctive_token(token),
                "typed": typed_value(str(current["value"])),
                "documents_by_version": _documents_by_version(
                    asserts.get(fact["id"], []), paths, docs_by_id
                ),
            }
        )
        if not sources:
            undiscoverable.append({"fact": fact["id"], "reason": "no slice document states it"})
            continue
        if not canonical_token(token):
            reason = f"value token {token!r} not canonical"
            undiscoverable.append({"fact": fact["id"], "reason": reason})
            continue
        if not distinctive_token(token):
            reason = f"value token {token!r} is a common word (leak hazard)"
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
    _flag_shared_values(answers["facts"])
    observed = project_observations(world_dir, project)
    for fact in answers["facts"]:
        graded = observation_answers(observed, fact)
        if graded is not None:
            fact["observations"] = graded
    answers["undiscoverable"] = undiscoverable
    scope_names = [str(nodes[p]["props"].get("name", p)) for p in scope]
    scope_names += [
        str(n["props"].get("name", ""))
        for n in nodes.values()
        if n["type"] in ("Team", "Department")
    ]
    scope_names += [str(f["props"].get("attribute", "")) for f in facts]
    answers["vocabulary_conflicts"] = vocabulary_conflicts([n for n in scope_names if n])
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


def _doc_lines(world_dir: Path, doc: dict[str, Any]) -> list[str]:
    """A slice document's text as the export renders it, line by line."""
    from enterprise_sim.exporters.text_export import render_docx, render_pptx

    path = world_dir / str(doc["props"]["path"])
    if not path.is_file():
        return []
    if path.suffix == ".docx":
        return render_docx(path).splitlines()
    if path.suffix == ".pptx":
        return render_pptx(path).splitlines()
    if path.suffix in (".py", ".toml"):
        from enterprise_sim.exporters.text_export import render_code

        return render_code(path).splitlines()
    if path.suffix in (".eml", ".ics"):
        from enterprise_sim.exporters.text_export import render_eml, render_ics

        return (render_eml(path) if path.suffix == ".eml" else render_ics(path)).splitlines()
    return path.read_text(encoding="utf-8").splitlines()


def _stale_values(
    edges: list[dict[str, Any]], paths: dict[str, str], current: dict[str, Any]
) -> list[dict[str, Any]]:
    """Each superseded or wrong value the slice states, with who states it.

    ``clean`` is true when no document that states the current value also states
    this one, so a test author can use it as a misleading value without an
    ordinary reader of a current document tripping over it.
    """
    stated: dict[str, set[str]] = {}
    for edge in edges:
        stated.setdefault(str(edge["props"]["value"]), set()).add(paths[edge["src"]])
    current_docs = stated.get(str(current["value"]), set())
    out: list[dict[str, Any]] = []
    for edge in edges:
        if edge["props"]["stance"] not in ("stale", "conflicting"):
            continue
        value = str(edge["props"]["value"])
        if value == current["value"] or any(o["value"] == value for o in out):
            continue
        docs = sorted(stated[value])
        out.append(
            {
                "value": value,
                "stance": edge["props"]["stance"],
                "documents": docs,
                "clean": not (set(docs) & current_docs),
            }
        )
    return sorted(out, key=lambda o: o["value"])


def _cmd_world_slice(args: Any) -> int:
    import sys

    try:
        summary = slice_world(
            args.world_dir,
            args.project,
            args.out,
            at=args.at,
            with_dependencies=args.with_dependencies,
            core=args.core,
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
    parser.add_argument(
        "--core", action="store_true", help="the casebook-size core slice (changed facts only)"
    )
    parser.set_defaults(func=_cmd_world_slice)


#: Columns of a project's trail table (``trails/<slice>.csv``).
TRAIL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("logged_at", "timestamp"),
    ("author", "text"),
    ("kind", "text"),
    ("document", "text"),
    ("summary", "text"),
)


def project_trail(world_dir: Path | str, project: str) -> list[dict[str, str]]:
    """The dated trail of one project: each fact statement of its documents, one row each.

    A structured log beside the prose (the shape a decision or change log has):
    when it was written, by whom, in which kind of document (its export path), and
    the statement exactly as the document makes it, commas replaced by
    semicolons so no cell needs quoting. Stale and wrong statements appear as
    the documents make them: the trail carries no labels (those stay gold).
    """
    world_dir = Path(world_dir)
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    events = {e["id"]: e for e in _jsonl(world_dir / "kg" / "events.jsonl")}
    authors: dict[str, list[str]] = {}
    for edge in edges:
        if edge["type"] == "authored":
            name = str(nodes.get(edge["src"], {}).get("props", {}).get("name", edge["src"]))
            authors.setdefault(edge["dst"], []).append(name)
    rows: list[dict[str, str]] = []
    for doc in slice_documents(nodes, edges, [project]):
        event = events.get(str(doc["props"].get("event", "")))
        rel = _export_rel(str(doc["props"]["path"]))
        if event is None or rel is None or "duplicate_of" in doc["props"]:
            continue
        for fact in event.get("payload", {}).get("facts") or []:
            rows.append(
                {
                    "logged_at": f"{event['timestamp'][:19]}Z",
                    "author": "; ".join(sorted(authors.get(doc["id"], []))),
                    "kind": str(doc["props"].get("kind", "")),
                    "document": rel,
                    "summary": str(fact["statement"]).replace(",", ";"),
                }
            )
    return sorted(rows, key=lambda r: (r["logged_at"], r["document"], r["summary"]))


#: Columns of a project's registry table (``registry/<slice>.csv``).
REGISTRY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("attribute", "text"),
    ("version", "integer"),
    ("value", "text"),
    ("canonical", "text"),
    ("valid_from", "timestamp"),
)


def project_registry(world_dir: Path | str, project: str) -> list[dict[str, str]]:
    """The authoritative change log of one project's facts: every version, one row each.

    The system of record beside the documents (a config or ownership registry): a
    test set can let ``sql`` read the current value here while a ``text_search``
    reader meets stale statements in the documents. Rows carry the attribute
    name, never the gold stance labels.
    """
    world_dir = Path(world_dir)
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    facts = {
        i: n
        for i, n in nodes.items()
        if n["type"] == "Fact" and n["props"].get("subject") == project
    }
    rows: list[dict[str, str]] = []
    for node in nodes.values():
        if node["type"] != "FactVersion" or node["props"]["fact"] not in facts:
            continue
        props = node["props"]
        rows.append(
            {
                "attribute": str(facts[props["fact"]]["props"]["attribute"]),
                "version": str(props["version"]),
                "value": str(props["value"]).replace(",", ";"),
                "canonical": str(props.get("canonical", props["value"])),
                "valid_from": f"{str(props['valid_from'])[:19]}Z",
            }
        )
    return sorted(rows, key=lambda r: (r["attribute"], int(r["version"])))


#: Words of the gym's tool and prompt vocabulary; names holding one as a whole
#: token get rewritten by test-set generators (sets-cc: ``search`` once rewrote
#: ``text_search``).
TOOL_VOCABULARY: frozenset[str] = frozenset(
    {"search", "files", "shell", "publish", "sql", "python", "note", "session"}
)


def vocabulary_conflicts(names: list[str]) -> list[dict[str, str]]:
    """Each name holding a tool-vocabulary word as a whole token (case folded)."""
    found: list[dict[str, str]] = []
    for name in sorted(set(names)):
        tokens = {t for t in re.split(r"[^a-z0-9]+", name.casefold()) if t}
        tokens |= {t.rstrip("s") for t in tokens}
        hits = sorted(tokens & TOOL_VOCABULARY)
        if hits:
            found.append({"name": name, "words": ", ".join(hits)})
    return found


#: Columns of a project's observations table (``observations/<slice>.csv``).
OBSERVATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("date", "date"),
    ("attribute", "text"),
    ("observed", "decimal"),
)
#: Threshold-like facts and how an observation is compared to the value in force.
_OBSERVED = ("latency_target", "reorder_point", "margin_target", "budget", "open_roles")


def _numeric(value: str) -> float | None:
    typed = typed_value(value)
    if typed["type"] in ("count", "usd", "percent"):
        return float(typed["value"])
    return None


def project_observations(world_dir: Path | str, project: str) -> list[dict[str, str]]:
    """Daily measurements graded against a threshold fact (p99 latency, stock, margin, spend).

    One row per weekday of the window per threshold fact of the project that
    changed: an observed value drawn (seeded by fact and day) across the span of
    the fact's values, so "days over the target" depends on which version of the
    target one applies: a stale target gives a different count, not only a
    different token (sets2-cc).
    """
    from datetime import date as _date
    from datetime import timedelta

    world_dir = Path(world_dir)
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    world = json.loads((world_dir / ".world").read_text(encoding="utf-8"))
    start = _date.fromisoformat(str(world["window"]["start"]))
    end = _date.fromisoformat(str(world["window"]["end"]))
    versions: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        if node["type"] == "FactVersion":
            versions.setdefault(str(node["props"]["fact"]), []).append(node["props"])
    rows: list[dict[str, str]] = []
    for fact in sorted(
        (n for n in nodes.values() if n["type"] == "Fact" and n["props"].get("subject") == project),
        key=lambda n: n["id"],
    ):
        attribute = str(fact["props"]["attribute"])
        values = [_numeric(str(v["value"])) for v in versions.get(fact["id"], [])]
        numbers = [v for v in values if v is not None]
        if attribute not in _OBSERVED or len(numbers) < 2:
            continue
        low, high = min(numbers), max(numbers)
        span = high - low
        day = start
        while day <= end:
            if day.weekday() < 5:
                digest = hashlib.sha256(f"obs\x00{fact['id']}\x00{day}".encode()).hexdigest()
                u = int(digest[:8], 16) / 0xFFFFFFFF
                observed = low - 0.2 * span + u * 1.4 * span
                rows.append(
                    {
                        "date": day.isoformat(),
                        "attribute": attribute,
                        "observed": f"{observed:.2f}".rstrip("0").rstrip("."),
                    }
                )
            day += timedelta(days=1)
    return rows


def observation_answers(rows: list[dict[str, str]], fact: dict[str, Any]) -> dict[str, Any] | None:
    """Days over each version's value for one fact's observations (the gold for graders)."""
    attribute = str(fact["fact"]).rsplit(":", 1)[-1]
    mine = [float(r["observed"]) for r in rows if r["attribute"] == attribute]
    if not mine:
        return None
    out: dict[str, int] = {}
    for version in fact["history"]:
        threshold = _numeric(str(version["value"]))
        if threshold is not None:
            out[str(version["value"])] = sum(1 for o in mine if o > threshold)
    return {"rows": len(mine), "days_over": out}
