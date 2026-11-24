"""``world knowledge-layer EXPORT_DIR --out FILE``: the knowledge a test set can be minted from.

A test-set engine (the gym's fin and casebook engines) needs, for each piece of
knowledge it plants: what it says and when it holds, which older version it
supersedes, who in the roster holds it, which documents carry it, and which documents
say something else that could mislead. The world's gold graph has all of it in the
fact ledger; this module reads it out of an export into one documented file,
``knowledge.json`` (``simenterprise-knowledge/1``; schema in
``docs/knowledge-layer.md``). It reads the export only, so it works for every world
already exported, live-prose ones included, with no model call.

* **items**: one per fact version (a decision, policy, target or status of a project),
  with its value, valid time, retraction, and the versions before and after it;
* **holders**: the people who wrote it down (``author``; ``announcer`` for the one
  who announced the change) and the owner of the project's initiative (``owner``),
  with the day each left, if they did;
* **carriers**: every document whose gold ``asserts`` edge states the version as
  true (current, an update, or hedged), with whether the value's token is in its
  exported text (``verbatim``; a typo can drop it);
* **supersessions**: each change of a fact, old item, new item, effective time and
  the documents that announced it;
* **misleading**: every document stating a superseded value as current (``stale``),
  a value that was never true (``conflicting``), or a near-copy of another document
  (``near_duplicate``), with the item each misleads about.

:func:`check_layer` checks a layer against its export: every path it names is a file
of the export, every ``verbatim`` flag matches the text, every holder is a person of
the roster, and the supersession chains are consistent. Deterministic: the same
export gives the same bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["FORMAT", "KnowledgeError", "add_knowledge_parser", "build_layer", "check_layer"]

FORMAT = "simenterprise-knowledge/1"
_TRUE = ("current", "update", "hedged")
_MISLEADING = ("stale", "conflicting")


class KnowledgeError(ValueError):
    """The directory is not an export this command can read."""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _item_id(fact: str, version: int) -> str:
    return f"{fact}@v{version}"


class _Export:
    """The parts of an export the layer reads."""

    def __init__(self, export_dir: Path) -> None:
        try:
            self.manifest = json.loads((export_dir / "export.json").read_text(encoding="utf-8"))
            self.nodes = {n["id"]: n for n in _jsonl(export_dir / "kg" / "nodes.jsonl")}
            self.edges = _jsonl(export_dir / "kg" / "edges.jsonl")
            config = json.loads((export_dir / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError) as exc:
            raise KnowledgeError(f"{export_dir} is not a SimEnterprise export: {exc}") from exc
        self.dir = export_dir
        self.window = {
            "start": str(config["simulation"]["period_start"]),
            "end": str(config["simulation"]["period_end"]),
        }
        self.files = {str(f["path"]) for f in self.manifest["files"]}
        self.export_of = {
            str(f.get("rendered_from") or f.get("source_path") or f["path"]): str(f["path"])
            for f in self.manifest["files"]
        }
        self._text: dict[str, str] = {}

    def path_of(self, artifact_id: str) -> str | None:
        node = self.nodes.get(artifact_id)
        if node is None:
            return None
        return self.export_of.get(str(node["props"].get("path", "")))

    def text(self, path: str) -> str:
        if path not in self._text:
            self._text[path] = (self.dir / path).read_text(encoding="utf-8")
        return self._text[path]

    def by_type(self, edge_type: str) -> list[dict[str, Any]]:
        return [e for e in self.edges if e["type"] == edge_type]


def _person(node: dict[str, Any]) -> dict[str, Any]:
    props = node["props"]
    return {
        "id": node["id"],
        "name": props.get("name"),
        "department": props.get("department"),
        "team": props.get("team"),
        "title": props.get("title"),
        "joined_at": props.get("joined_at"),
        "left_at": props.get("left_at"),
        "replaces": props.get("replaces"),
    }


def build_layer(export_dir: Path) -> dict[str, Any]:
    """The knowledge layer of ``export_dir`` (see the module docstring)."""
    from enterprise_sim.facts.catalog import kind_of, value_token

    ex = _Export(export_dir)
    authors: dict[str, list[str]] = {}
    for edge in ex.by_type("authored"):
        authors.setdefault(edge["dst"], []).append(edge["src"])
    initiative_of = {e["src"]: e["dst"] for e in ex.by_type("under")}
    owner_of = {e["dst"]: e["src"] for e in ex.by_type("owns_initiative")}

    versions: dict[str, list[dict[str, Any]]] = {}
    for node in ex.nodes.values():
        if node["type"] == "FactVersion":
            versions.setdefault(str(node["props"]["fact"]), []).append(node["props"])
    statements: dict[str, list[dict[str, Any]]] = {}
    for edge in ex.by_type("asserts"):
        statements.setdefault(edge["dst"], []).append(edge)

    items: list[dict[str, Any]] = []
    supersessions: list[dict[str, Any]] = []
    misleading: list[dict[str, Any]] = []
    held_by: set[str] = set()
    for fact_id in sorted(versions):
        fact = ex.nodes.get(fact_id)
        if fact is None:
            continue
        props = fact["props"]
        subject = ex.nodes.get(str(props.get("subject")), {})
        subject_props = subject.get("props", {})
        label = str(props.get("label", props.get("attribute")))
        subject_name = str(subject_props.get("name", props.get("subject")))
        owner = owner_of.get(initiative_of.get(str(props.get("subject")), ""))
        history = sorted(versions[fact_id], key=lambda v: int(v["version"]))
        carriers: dict[int, list[dict[str, Any]]] = {}
        holders: dict[int, dict[str, str]] = {}
        announced: dict[int, list[str]] = {}
        retracted_in: dict[int, list[str]] = {}
        for edge in sorted(statements.get(fact_id, ()), key=lambda e: str(e["id"])):
            path = ex.path_of(edge["src"])
            if path is None:
                continue
            at = str(ex.nodes[edge["src"]]["props"].get("date", edge.get("created_at", "")))
            sp = edge["props"]
            stance = sp.get("stance")
            value = str(sp.get("value", ""))
            if stance in _TRUE and sp.get("stated_version") is not None:
                number = int(sp["stated_version"])
                token = value_token(value)
                carriers.setdefault(number, []).append(
                    {
                        "path": path,
                        "stance": stance,
                        "at": at,
                        "verbatim": bool(token) and token in ex.text(path),
                    }
                )
                role = "announcer" if stance == "update" else "author"
                for person in authors.get(edge["src"], ()):
                    mine = holders.setdefault(number, {})
                    if mine.get(person) != "announcer":
                        mine[person] = role
                if stance == "update":
                    announced.setdefault(number, []).append(path)
            elif stance == "retraction" and sp.get("true_version") is not None:
                retracted_in.setdefault(int(sp["true_version"]), []).append(path)
            elif stance in _MISLEADING:
                true_n = sp.get("true_version")
                stated_n = sp.get("stated_version")
                misleading.append(
                    {
                        "kind": stance,
                        "path": path,
                        "at": at,
                        "fact": fact_id,
                        "stated_value": value,
                        "misleads_about": None
                        if true_n is None
                        else _item_id(fact_id, int(true_n)),
                        "stated_item": None
                        if stated_n is None
                        else _item_id(fact_id, int(stated_n)),
                    }
                )
        for index, version in enumerate(history):
            number = int(version["version"])
            people = dict(holders.get(number, {}))
            if owner is not None and owner not in people:
                people[owner] = "owner"
            held_by.update(people)
            items.append(
                {
                    "id": _item_id(fact_id, number),
                    "fact": fact_id,
                    "version": number,
                    "kind": kind_of(str(props.get("attribute"))) or "decision",
                    "attribute": props.get("attribute"),
                    "label": label,
                    "subject": {
                        "id": props.get("subject"),
                        "name": subject_name,
                        "department": subject_props.get("department"),
                    },
                    "question": props.get("question"),
                    "value": version["value"],
                    "canonical": version.get("canonical") or value_token(str(version["value"])),
                    "statement": f"The {label} of {subject_name} is {version['value']}.",
                    "valid_from": version.get("valid_from"),
                    "valid_to": version.get("valid_to"),
                    "retracted_at": version.get("retracted_at"),
                    "retracted_in": sorted(retracted_in.get(number, ())),
                    "supersedes": _item_id(fact_id, int(history[index - 1]["version"]))
                    if index
                    else None,
                    "superseded_by": _item_id(fact_id, int(history[index + 1]["version"]))
                    if index + 1 < len(history)
                    else None,
                    "holders": [{"person": p, "as": people[p]} for p in sorted(people)],
                    "carriers": sorted(
                        carriers.get(number, ()), key=lambda c: (c["at"], c["path"])
                    ),
                }
            )
            if index:
                supersessions.append(
                    {
                        "fact": fact_id,
                        "old": _item_id(fact_id, int(history[index - 1]["version"])),
                        "new": _item_id(fact_id, number),
                        "effective": version.get("valid_from"),
                        "announced_in": sorted(announced.get(number, ())),
                    }
                )

    for edge in sorted(ex.by_type("duplicate_of"), key=lambda e: str(e["id"])):
        copy, original = ex.path_of(edge["src"]), ex.path_of(edge["dst"])
        if copy is None or original is None:
            continue
        misleading.append(
            {
                "kind": "near_duplicate",
                "path": copy,
                "at": str(ex.nodes[edge["src"]]["props"].get("date", "")),
                "duplicate_of": original,
            }
        )
    misleading.sort(key=lambda m: (m["at"], m["path"], m["kind"], m.get("fact") or ""))
    roster = [
        _person(ex.nodes[p])
        for p in sorted(held_by)
        if p in ex.nodes and ex.nodes[p]["type"] == "Person"
    ]
    counts = {
        "items": len(items),
        "facts": len({i["fact"] for i in items}),
        "supersessions": len(supersessions),
        "carriers": sum(len(i["carriers"]) for i in items),
        "misleading": len(misleading),
        "holders": len(roster),
    }
    return {
        "format": FORMAT,
        "world_id": ex.manifest["world_id"],
        "export_digest": hashlib.sha256((export_dir / "export.json").read_bytes()).hexdigest(),
        "window": ex.window,
        "counts": counts,
        "items": items,
        "supersessions": supersessions,
        "misleading": misleading,
        "people": roster,
    }


def check_layer(export_dir: Path, layer: dict[str, Any]) -> list[str]:
    """Problems of ``layer`` against ``export_dir`` (empty: it holds)."""
    from enterprise_sim.facts.catalog import value_token

    ex = _Export(export_dir)
    problems: list[str] = []
    if layer.get("format") != FORMAT:
        problems.append(f"format is {layer.get('format')!r}, not {FORMAT}")
    if layer.get("world_id") != ex.manifest["world_id"]:
        problems.append("the layer is of another world")
    digest = hashlib.sha256((export_dir / "export.json").read_bytes()).hexdigest()
    if layer.get("export_digest") != digest:
        problems.append("the layer is of another export of this world")
    people = {n for n, node in ex.nodes.items() if node["type"] == "Person"}
    items = {i["id"]: i for i in layer.get("items", ())}
    if len(items) != len(layer.get("items", ())):
        problems.append("an item id repeats")
    for item in items.values():
        for carrier in item["carriers"]:
            if carrier["path"] not in ex.files:
                problems.append(f"{item['id']}: carrier {carrier['path']} is not in the export")
                continue
            token = value_token(str(item["value"]))
            if carrier["verbatim"] != (bool(token) and token in ex.text(carrier["path"])):
                problems.append(f"{item['id']}: carrier {carrier['path']} verbatim flag is wrong")
        for path in item["retracted_in"]:
            if path not in ex.files:
                problems.append(f"{item['id']}: retraction {path} is not in the export")
        for holder in item["holders"]:
            if holder["person"] not in people:
                problems.append(f"{item['id']}: holder {holder['person']} is not in the roster")
        for link in ("supersedes", "superseded_by"):
            other = item[link]
            if other is not None and other not in items:
                problems.append(f"{item['id']}: {link} {other} is not an item")
        after = items.get(item["superseded_by"] or "")
        if after is not None and after["supersedes"] != item["id"]:
            problems.append(f"{item['id']}: the chain is not symmetric")
    for entry in layer.get("supersessions", ()):
        if entry["old"] not in items or entry["new"] not in items:
            problems.append(f"supersession {entry['old']} -> {entry['new']} names a missing item")
        for path in entry["announced_in"]:
            if path not in ex.files:
                problems.append(f"supersession {entry['new']}: {path} is not in the export")
    for entry in layer.get("misleading", ()):
        for key in ("path", "duplicate_of"):
            path = entry.get(key)
            if path is not None and path not in ex.files:
                problems.append(f"misleading {entry['kind']}: {path} is not in the export")
        for key in ("misleads_about", "stated_item"):
            ref = entry.get(key)
            if ref is not None and ref not in items:
                problems.append(f"misleading {entry['path']}: {key} {ref} is not an item")
    return problems


def _dumps(layer: dict[str, Any]) -> bytes:
    return (json.dumps(layer, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode()


def _cmd_world_knowledge_layer(args: argparse.Namespace) -> int:
    try:
        if args.check is not None:
            layer = json.loads(args.check.read_text(encoding="utf-8"))
            problems = check_layer(args.export_dir, layer)
        else:
            layer = build_layer(args.export_dir)
            problems = check_layer(args.export_dir, layer)
            if not problems and args.out is not None:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_bytes(_dumps(layer))
    except (KnowledgeError, OSError, ValueError) as exc:
        print(f"enterprise-sim world knowledge-layer: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"enterprise-sim world knowledge-layer: {problem}", file=sys.stderr)
    print(
        json.dumps({"world_id": layer.get("world_id"), **layer.get("counts", {})}, sort_keys=True)
    )
    return 1 if problems else 0


def add_knowledge_parser(world_subparsers: Any) -> None:
    """Register ``world knowledge-layer EXPORT_DIR (--out FILE | --check FILE)``."""
    parser = world_subparsers.add_parser(
        "knowledge-layer",
        help="the knowledge layer of an export (items, holders, carriers, misleading documents)",
    )
    parser.add_argument("export_dir", type=Path, metavar="EXPORT_DIR", help="a world export")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--out", type=Path, help="write knowledge.json here (after checking it)")
    group.add_argument("--check", type=Path, help="check an existing knowledge.json instead")
    parser.set_defaults(func=_cmd_world_knowledge_layer)
