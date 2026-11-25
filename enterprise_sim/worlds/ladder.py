"""``world ladder-check``: one seed at several sizes, with the facts a test set needs held constant.

A size ladder lets a sweep measure the size effect with nothing else
changing. Its rungs are worlds built from one seed that differ only by extra
departments (each with its own people, projects, documents and facts) and the data
scale factor. The base departments keep the same people, and their projects keep the
same facts, versions and statements at every rung, because the squad count is equal
and ``cross_team`` is 0 (see ``examples/worlds/ladder/``).

:func:`ladder_check` reads the rungs' exports and writes the ladder's gold record:

* per rung: the world id, the export digest, and its people, documents and table rows
  (each must be larger than the rung before);
* the base departments, and the **invariant facts**: every fact version of a base
  project, present with the same value and valid time at every rung;
* the **invariant assertions**: every (document, fact, stance, value) of a base
  project, the same at every rung, so a test set's evidence is where it expects it;
* the **invariant knowledge**: the base departments' part of each rung's knowledge
  layer (:mod:`enterprise_sim.worlds.knowledge`: their items with values, valid
  times, holders and carriers, their supersessions and misleading documents), which
  must be identical at every rung, so the K items a test set mints are the same at
  every size; the record gives their counts and one digest;
* the **statements in the text**: of the base assertions that state a value (stance
  current, hedged or update), how many carry the value's token in the exported text.
  Which ones do must be the same at every rung. The prose around a fact may differ
  between rungs (a document's wording is drawn from its whole prompt, and the
  company profile in it grows), so the check is on the stated values, not on bytes.

The check refuses a ladder whose rungs do not grow, whose base facts, base assertions,
base knowledge or statements in the text differ between rungs, or whose exports carry a
``ladder`` label (``[ladder]``) other than this ladder id and the rung's size.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

__all__ = ["LadderError", "add_ladder_parser", "ladder_check"]

_STATING = frozenset({"current", "hedged", "update"})


class LadderError(ValueError):
    """The exports do not form a ladder."""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _rung(export_dir: Path, base: Sequence[str]) -> dict[str, Any]:
    manifest = json.loads((export_dir / "export.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in _jsonl(export_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(export_dir / "kg" / "edges.jsonl")
    base_projects = {
        i
        for i, n in nodes.items()
        if n["type"] == "Project" and n["props"].get("department") in base
    }
    facts = sorted(
        {
            (
                str(n["props"]["fact"]),
                int(n["props"]["version"]),
                str(n["props"]["value"]),
                str(n["props"]["valid_from"]),
            )
            for n in nodes.values()
            if n["type"] == "FactVersion"
            and nodes.get(str(n["props"]["fact"]), {}).get("props", {}).get("subject")
            in base_projects
        }
    )
    project_of = {e["src"]: e["dst"] for e in edges if e["type"] == "expresses"}
    stated = _stated_in_text(export_dir, manifest, nodes, edges, project_of, base_projects)
    assertions = sorted(
        {
            (
                str(nodes[e["src"]]["props"]["path"]),
                e["dst"],
                e["props"]["stance"],
                str(e["props"]["value"]),
            )
            for e in edges
            if e["type"] == "asserts" and project_of.get(e["src"]) in base_projects
        }
    )
    rows = 0
    for table in manifest["tables"]:
        if table["kind"] == "table" and str(table["path"]).startswith("data/"):
            text = (export_dir / table["path"]).read_text(encoding="utf-8")
            rows += max(0, len(text.splitlines()) - 1)
    return {
        "world_id": manifest["world_id"],
        "export_digest": hashlib.sha256((export_dir / "export.json").read_bytes()).hexdigest(),
        "people": sum(1 for n in nodes.values() if n["type"] == "Person"),
        "documents": sum(1 for f in manifest["files"] if f["path"].startswith("artifacts/")),
        "rows": rows,
        "_label": manifest.get("ladder"),
        "_facts": facts,
        "_assertions": assertions,
        "_stated": stated,
        "_knowledge": _base_knowledge(export_dir, base_projects),
    }


def _base_knowledge(export_dir: Path, base_projects: set[str]) -> dict[str, Any]:
    """The base departments' part of the rung's knowledge layer (``worlds.knowledge``).

    Every item of a base project (value, valid time, retraction, supersession links,
    holders and carriers), every supersession of their facts and every misleading
    document about them. A size ladder holds this part identical at every rung, so a
    test set minted from one rung's layer finds the same K items at every size.
    """
    from enterprise_sim.worlds.knowledge import build_layer

    layer = build_layer(export_dir)
    items = [i for i in layer["items"] if i["subject"]["id"] in base_projects]
    facts = {i["fact"] for i in items}
    paths = {c["path"] for i in items for c in i["carriers"]}
    return {
        "items": items,
        "supersessions": [s for s in layer["supersessions"] if s["fact"] in facts],
        "misleading": [
            m
            for m in layer["misleading"]
            if m.get("fact") in facts
            or (m["kind"] == "near_duplicate" and m["duplicate_of"] in paths)
        ],
    }


def _stated_in_text(
    export_dir: Path,
    manifest: dict[str, Any],
    nodes: dict[str, Any],
    edges: list[dict[str, Any]],
    project_of: dict[str, str],
    base_projects: set[str],
) -> dict[tuple[str, str, str], bool]:
    """(document, fact, value) -> whether the value's token is in the exported text."""
    from enterprise_sim.facts.catalog import value_token

    export_of = {
        str(f.get("rendered_from") or f.get("source_path") or f["path"]): str(f["path"])
        for f in manifest["files"]
    }
    stated: dict[tuple[str, str, str], bool] = {}
    for e in edges:
        if e["type"] != "asserts" or project_of.get(e["src"]) not in base_projects:
            continue
        if e["props"].get("stance") not in _STATING:
            continue
        world_path = str(nodes[e["src"]]["props"]["path"])
        value = str(e["props"]["value"])
        token = value_token(value)
        path = export_of.get(world_path)
        text = (export_dir / path).read_text(encoding="utf-8") if path else ""
        stated[(world_path, e["dst"], value)] = bool(token) and token in text
    return stated


def ladder_check(
    ladder_id: str, rungs: Sequence[tuple[str, Path]], base: Sequence[str]
) -> dict[str, Any]:
    """The ladder record for ``rungs`` ((size label, export dir), smallest first)."""
    if len(rungs) < 2:
        raise LadderError("a ladder needs at least two rungs")
    found = [(size, _rung(path, base)) for size, path in rungs]
    first = found[0][1]
    if not first["_facts"]:
        raise LadderError(f"no fact of the base departments {list(base)} in the smallest rung")
    for (size_a, a), (size_b, b) in zip(found, found[1:], strict=False):
        for measure in ("people", "documents", "rows"):
            if not b[measure] > a[measure]:
                raise LadderError(f"{measure} does not grow from {size_a} to {size_b}")
    for size, rung in found:
        label = rung["_label"]
        if label is not None and (
            label.get("id") != ladder_id or label.get("rung") != size.lower()
        ):
            raise LadderError(
                f"rung {size}: its export is labelled {label}, not {ladder_id}/{size.lower()}"
            )
    for size, rung in found[1:]:
        if rung["_facts"] != first["_facts"]:
            raise LadderError(f"rung {size}: the base facts differ from the smallest rung")
        if rung["_assertions"] != first["_assertions"]:
            raise LadderError(f"rung {size}: the base assertions differ from the smallest rung")
        if rung["_stated"] != first["_stated"]:
            changed = sorted(
                k for k in first["_stated"] if rung["_stated"].get(k) != first["_stated"][k]
            )
            raise LadderError(
                f"rung {size}: a base value is stated in the text at one rung and not the other "
                f"({len(changed)} statements, first {changed[:1]})"
            )
    for size, rung in found[1:]:
        if rung["_knowledge"] != first["_knowledge"]:
            differing = [
                key
                for key in ("items", "supersessions", "misleading")
                if rung["_knowledge"][key] != first["_knowledge"][key]
            ]
            raise LadderError(
                f"rung {size}: the base departments' knowledge layer differs from the "
                f"smallest rung ({', '.join(differing)})"
            )
    knowledge = first["_knowledge"]
    return {
        "format": "simenterprise-ladder/1",
        "ladder_id": ladder_id,
        "base_departments": list(base),
        "rungs": [
            {"size": size, **{k: v for k, v in rung.items() if not k.startswith("_")}}
            for size, rung in found
        ],
        "invariant_facts": [
            {"fact": f, "version": v, "value": value, "valid_from": at}
            for f, v, value, at in first["_facts"]
        ],
        "invariant_assertions": len(first["_assertions"]),
        "invariant_knowledge": {
            "items": len(knowledge["items"]),
            "supersessions": len(knowledge["supersessions"]),
            "misleading": len(knowledge["misleading"]),
            "sha256": hashlib.sha256(
                json.dumps(knowledge, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
        },
        "statements_in_text": {
            "stating": len(first["_stated"]),
            "value_in_text": sum(first["_stated"].values()),
        },
    }


def _cmd_world_ladder_check(args: argparse.Namespace) -> int:
    rungs = []
    for spec in args.rungs:
        size, _, path = spec.partition("=")
        if not path:
            print(
                f"enterprise-sim world ladder-check: {spec!r} is not SIZE=EXPORT_DIR",
                file=sys.stderr,
            )
            return 2
        rungs.append((size, Path(path)))
    try:
        record = ladder_check(args.ladder_id, rungs, tuple(args.base))
    except (LadderError, OSError, KeyError, ValueError) as exc:
        print(f"enterprise-sim world ladder-check: {exc}", file=sys.stderr)
        return 1
    body = json.dumps(record, indent=1, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.write_text(body, encoding="utf-8")
    print(body.strip())
    return 0


def add_ladder_parser(world_subparsers: Any) -> None:
    """Register ``world ladder-check --ladder-id ID --base DEPT ... SIZE=EXPORT ...``."""
    parser = world_subparsers.add_parser(
        "ladder-check",
        help="check that exports form a size ladder with constant base facts; write its record",
    )
    parser.add_argument("rungs", nargs="+", metavar="SIZE=EXPORT_DIR", help="smallest first")
    parser.add_argument("--ladder-id", required=True)
    parser.add_argument(
        "--base",
        action="append",
        required=True,
        metavar="DEPT",
        help="a base department (repeat for each)",
    )
    parser.add_argument("--out", type=Path, help="write the ladder record here")
    parser.set_defaults(func=_cmd_world_ladder_check)
