"""The must-not-cross map of a walled world, and the export check (decision record 0036).

Invariant: in a world built with walls, every exported data file is assigned to
exactly one project or to the shared set; no file of a walled project names staff of
a project across a wall; and no file other than a walled project's own states one of
its decisive values. The export declares the map in the gold file ``isolation.json``.
:func:`isolation_check` is the one place that verifies it: ``world export`` calls it
over the files it has just written and refuses the export on any finding, so a world
whose oracle would be wrong is never published.

Everything is read from the gold KG (``kg/nodes.jsonl``, ``kg/edges.jsonl``), where
:func:`enterprise_sim.world_builders.walls.assign_walls`'s plan was written.

* **Projects** are keyed by project id. Each lists every slice that holds its files
  (the full slice and its ``-core`` cut).
* **Shared** files are the organization pages (``organization/``) and the structured
  data tables (``data/``): company-wide, no project's.
* **Staff** of a walled project: its ``member_of`` people, by full name. Walled staff
  are named apart from everyone (``walls.unique_staff_names``). A person matches only
  by full name, as a whole token, case-sensitive; a first name alone never matches.
* **Decisive values** of a walled project (``{"attribute", "value"}``; the key is not
  ``token``, which privacy checks read as a secret name): the whitespace-free token
  (:func:`~enterprise_sim.facts.catalog.value_token`) of each of its fact values that is
  distinctive (:func:`~enterprise_sim.worlds.slice.distinctive_token`), at least three
  characters, not a date nor a number under four digits, not a person's name (people
  are staff), and held by no other fact of the world in any attribute. Declared token
  sets are therefore pairwise disjoint across projects and attributes. A token matches
  as a whole token (not inside a longer word, number or version). A value a bank could
  not keep apart is shared, so it is not declared; every walled project still has its
  engagement code (:mod:`enterprise_sim.facts.engagement`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "ISOLATION_FILE",
    "SHARED_PREFIXES",
    "isolation_check",
    "isolation_map",
    "matches",
    "wall_markers",
]

ISOLATION_FILE = "isolation.json"
SHARED_PREFIXES = ("organization/", "data/")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _decisive(token: str, value: str, distinctive: Any, typed: Any) -> bool:
    if len(token) < 3 or not distinctive(token) or typed(value).get("type") == "date":
        return False
    return not (token.isdigit() and len(token) < 4)  # "200" occurs in text by chance


def isolation_map(
    world_dir: Path, slice_names: Sequence[str], data_files: Sequence[str], tables: Sequence[str]
) -> dict[str, Any] | None:
    """The map of a walled world (``None`` without walls).

    ``slice_names`` are the export's slices, ``data_files`` its data-role paths and
    ``tables`` its data-table names.
    """
    from enterprise_sim.facts.catalog import value_token
    from enterprise_sim.worlds.slice import (
        core_slice_name,
        distinctive_token,
        slice_name,
        typed_value,
    )

    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    project_ids = sorted(i for i, n in nodes.items() if n["type"] == "Project")
    walled = [p for p in project_ids if nodes[p]["props"].get("walled")]
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
    facts_of: dict[str, set[str]] = {}  # token -> every fact holding it, any attribute
    candidates: dict[str, set[tuple[str, str]]] = {p: set() for p in walled}
    for node in nodes.values():
        if node["type"] != "FactVersion":
            continue
        fact_id = str(node["props"].get("fact"))
        fact = nodes.get(fact_id)
        value = str(node["props"].get("value", ""))
        token = value_token(value)
        if fact is None or not token:
            continue
        facts_of.setdefault(token, set()).add(fact_id)
        subject = str(fact["props"].get("subject"))
        if value in people or subject not in staff:
            continue
        if _decisive(token, value, distinctive_token, typed_value):
            candidates[subject].add((str(fact["props"].get("attribute")), token))
    existing = set(slice_names)
    projects = []
    for project in project_ids:
        own = [s for s in (slice_name(project), core_slice_name(project)) if s in existing]
        entry: dict[str, Any] = {"project": project, "walled": project in staff, "slices": own}
        if project in staff:
            entry["staff"] = sorted(staff[project])
            entry["values"] = [
                {"attribute": a, "value": t}
                for a, t in sorted(candidates[project])
                if len(facts_of[t]) == 1
            ]
        projects.append(entry)
    walls = sorted(sorted((e["src"], e["dst"])) for e in edges if e["type"] == "walled_from")
    shared_files = sorted(p for p in data_files if p.startswith(SHARED_PREFIXES[0]))
    return {
        "walls": walls,
        "projects": projects,
        "shared": {"files": shared_files, "tables": sorted(tables)},
    }


def _whole(text: str) -> re.Pattern[str]:
    """``text`` as a whole token: not inside a longer word, number or version."""
    needle = re.escape(" ".join(text.split())).replace(r"\ ", " ")
    return re.compile(rf"(?<![\w.]){needle}(?![\w]|\.\w)")


def _normal(text: str) -> str:
    return " ".join(text.split())


def matches(needle: str, text: str) -> bool:
    """The one matcher the export check and graders share (decision record 0036).

    Case-sensitive; runs of whitespace (line breaks, double spaces) count as one
    space in both; ``needle`` must stand as a whole token: not inside a longer word,
    number or version (``v3.1`` is not in ``v3.10``), while a trailing sentence
    period or ``'s`` is a boundary (``v3.1.`` and ``Omar Novak's`` match).
    """
    return _whole(needle).search(_normal(text)) is not None


def wall_markers(world_dir: Path) -> dict[str, list[str]]:
    """What names a walled project besides its staff and values: its display name,
    its project id and its initiative ids (cited artifacts carry them in their ids)."""
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    out: dict[str, list[str]] = {}
    for project, node in nodes.items():
        if node["type"] != "Project" or not node["props"].get("walled"):
            continue
        markers = {project, str(node["props"].get("name", ""))}
        markers |= {e["dst"] for e in edges if e["type"] == "under" and e["src"] == project}
        out[project] = sorted(m for m in markers if m)
    return out


def isolation_check(
    mapping: Mapping[str, Any],
    slice_files: Mapping[str, Sequence[str]],
    data_files: Sequence[str],
    read: Callable[[str], str],
    *,
    shared_paths: Sequence[str] = (),
    markers: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """Every finding against the map in the exported data files (empty when none).

    ``slice_files`` maps a slice name to its export paths, ``data_files`` lists every
    data-role path, and ``read(path)`` returns a file's exported text. Findings:

    1. a data file in no project's slices and not shared (unassigned), in the slices of
       two projects, or both shared and in a project's slices; ``shared_paths`` are the
       declared shared files and the data tables' paths;
    2. a file of a walled project naming, by full name, staff of a project across a
       wall, or naming such a project by its display name, project id or initiative id
       (``markers``, from :func:`wall_markers`: a cited artifact's id carries them);
    3. any file but a walled project's own stating one of its decisive values,
       whichever project or shared set the file belongs to.
    """
    projects = {p["project"]: p for p in mapping["projects"]}
    across: dict[str, set[str]] = {p: set() for p in projects}
    for a, b in mapping["walls"]:
        across[a].add(b)
        across[b].add(a)
    owner: dict[str, set[str]] = {}
    for project in projects.values():
        for name in project["slices"]:
            for path in slice_files.get(name, ()):
                owner.setdefault(path, set()).add(project["project"])
    shared = set(shared_paths)
    problems: list[str] = []
    for path in data_files:
        owners = owner.get(path, set())
        if len(owners) > 1:
            problems.append(f"{path}: in the slices of {sorted(owners)}")
        elif owners and path in shared:
            problems.append(f"{path}: both shared and in the slices of {sorted(owners)}")
        elif not owners and path not in shared:
            problems.append(f"{path}: assigned to no project and not shared")
    names = {
        p: [(f"staff {person}", _whole(person)) for person in projects[p].get("staff", ())]
        + [(f"the project as {m}", _whole(m)) for m in (markers or {}).get(p, ())]
        for p in projects
    }
    tokens = {p: [(v, _whole(v["value"])) for v in projects[p].get("values", ())] for p in projects}
    for path in data_files:
        text = _normal(read(path))
        owners = owner.get(path, set())
        mine = next(iter(owners)) if len(owners) == 1 else None
        for other in sorted(across.get(mine, set()) if mine else ()):
            for what, pattern in names[other]:
                if pattern.search(text):
                    problems.append(f"{path} ({mine}) names {what} of {other}")
        for project, values in tokens.items():
            if project == mine:
                continue
            for value, pattern in values:
                if pattern.search(text):
                    problems.append(
                        f"{path} ({mine or 'shared'}) states {value['value']} "
                        f"({value['attribute']}) of {project}"
                    )
    return problems
