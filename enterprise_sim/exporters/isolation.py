"""The must-not-cross map of a walled world, and the export check (decision record 0036).

Invariant: in a world built with walls, no exported file of a walled project belongs
to, names staff of, or states a decisive value of a project across a wall, and the
export declares the walls. :func:`isolation_check` is the one place that verifies it;
``world export`` calls it over the files it has just written and refuses the export on
any finding, so a world whose oracle would be wrong is never published.

Everything is read from the gold KG (``kg/nodes.jsonl``, ``kg/edges.jsonl``), where
:func:`enterprise_sim.world_builders.walls.assign_walls`'s plan was written.

* **Staff** of a walled project: its ``member_of`` people, by name.
* **Decisive values** of a walled project: the whitespace-free token
  (:func:`~enterprise_sim.facts.catalog.value_token`) of each of its fact values that is
  distinctive (:func:`~enterprise_sim.worlds.slice.distinctive_token`), at least three
  characters, not a date nor a number under four digits, and held by no other project
  of the world. A value that is a person's name is not a value token: people are
  declared, and checked by full name, as staff. A value another
  project also holds is not declared, so a grader never blames a system for a value
  that is legitimately elsewhere; a short label (``P0``) or a date can occur in any
  document by chance, so neither is declared either.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["isolation_check", "isolation_map"]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def isolation_map(world_dir: Path) -> dict[str, Any] | None:
    """The walls, each walled project's staff and decisive values; ``None`` without walls."""
    from enterprise_sim.facts.catalog import value_token
    from enterprise_sim.worlds.slice import distinctive_token, slice_name, typed_value

    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    walled = sorted(
        i for i, n in nodes.items() if n["type"] == "Project" and n["props"].get("walled")
    )
    if not walled:
        return None
    staff: dict[str, set[str]] = {p: set() for p in walled}
    for edge in edges:
        if edge["type"] == "member_of" and edge["dst"] in staff:
            person = nodes.get(edge["src"])
            if person is not None and person["props"].get("name"):
                staff[edge["dst"]].add(str(person["props"]["name"]))
    people = {
        str(n["props"]["name"])
        for n in nodes.values()
        if n["type"] == "Person" and n["props"].get("name")
    }
    holders: dict[str, set[str]] = {}
    values: dict[str, list[dict[str, str]]] = {p: [] for p in walled}
    for node in nodes.values():
        if node["type"] != "FactVersion":
            continue
        fact = nodes.get(str(node["props"].get("fact")))
        subject = str(fact["props"].get("subject")) if fact else ""
        value = str(node["props"].get("value", ""))
        token = value_token(value)
        if not token:
            continue
        holders.setdefault(token, set()).add(subject)  # every project, walled or not
        if value in people:
            continue  # a person is declared and checked by full name, as staff
        if subject not in staff or not _decisive(token, value, distinctive_token, typed_value):
            continue
        values[subject].append(
            {
                "attribute": str(fact["props"].get("attribute")) if fact else "",
                "fact": str(node["props"].get("fact")),
                "token": token,
            }
        )
    walls = sorted(
        sorted((slice_name(e["src"]), slice_name(e["dst"])))
        for e in edges
        if e["type"] == "walled_from"
    )
    projects = []
    for project in walled:
        own = {
            (v["attribute"], v["token"])
            for v in values[project]
            if holders[v["token"]] == {project}
        }
        projects.append(
            {
                "slice": slice_name(project),
                "staff": sorted(staff[project]),
                "values": [{"attribute": a, "token": t} for a, t in sorted(own)],
            }
        )
    projects.sort(key=lambda p: str(p["slice"]))
    return {"walls": walls, "projects": projects, "shared": {"files": [], "tables": []}}


def declare_shared(
    mapping: dict[str, Any],
    data_files: list[str],
    slice_files: Mapping[str, list[str]],
    tables: list[str],
) -> None:
    """Fill ``mapping["shared"]``: the data files in no project slice, and every table.

    Structured data and the organization pages are company-wide (record 0036): no test
    may treat them as one project's.
    """
    in_a_slice = {path for files in slice_files.values() for path in files}
    mapping["shared"] = {
        "files": sorted(p for p in data_files if p not in in_a_slice),
        "tables": sorted(tables),
    }


def _decisive(token: str, value: str, distinctive: Any, typed: Any) -> bool:
    if len(token) < 3 or not distinctive(token) or typed(value).get("type") == "date":
        return False
    return not (token.isdigit() and len(token) < 4)  # "200" occurs in text by chance


def isolation_check(
    mapping: Mapping[str, Any], slice_files: Mapping[str, list[str]], read: Any
) -> list[str]:
    """Every crossing in the exported files of walled slices (empty when none).

    ``slice_files`` maps a slice name to its export paths; ``read(path)`` returns a
    file's exported text. A file crosses a wall when it is in the slices of two
    projects across a wall, names a person on the staff of a project across a wall,
    or contains a decisive value token of a project across a wall.
    """
    projects = {p["slice"]: p for p in mapping["projects"]}
    across: dict[str, set[str]] = {name: set() for name in projects}
    for a, b in mapping["walls"]:
        across[a].add(b)
        across[b].add(a)
    owners: dict[str, set[str]] = {}
    for name in projects:
        for path in slice_files.get(name, []):
            owners.setdefault(path, set()).add(name)
    problems: list[str] = []
    for path, names in sorted(owners.items()):
        for name in sorted(names):
            if names & across[name]:
                problems.append(f"{path}: in the slices of {sorted(names)}, which are walled")
                break
    patterns = {
        other: [
            (f"staff {person}", re.compile(rf"(?<![\w]){re.escape(person)}(?![\w])"))
            for person in projects[other]["staff"]
        ]
        + [
            (
                f"value {v['token']} ({v['attribute']})",
                re.compile(rf"(?<![\w.]){re.escape(v['token'])}(?![\w])"),
            )
            for v in projects[other]["values"]
        ]
        for other in projects
    }
    for name in sorted(projects):
        for path in slice_files.get(name, []):
            text = read(path)
            for other in sorted(across[name]):
                for what, pattern in patterns[other]:
                    if pattern.search(text):
                        problems.append(f"{path} ({name}) states {what} of {other}")
    return problems
