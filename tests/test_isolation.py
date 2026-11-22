"""Walled projects (decision record 0036): the invariant over every entry point.

Invariant: in a world built with walls, no exported file of a walled project belongs
to, names staff of, or states a decisive value of a project across a wall, and the
export declares the walls. Each row of ENTRY_POINTS checks one path content can take
into a walled project's files on a world built with every producer on; each has a
planted-crossing negative case that ``isolation_check`` must refuse. A new producer
that writes walled documents joins the table.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.assembly import compute_config_digest
from enterprise_sim.cli import main
from enterprise_sim.core.config import WorldConfig, load_config
from enterprise_sim.core.config.isolation import IsolationConfig
from enterprise_sim.exporters.isolation import isolation_check
from enterprise_sim.exporters.text_export import ExportError, export_world

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"

_ENGINEERING = """
[isolation]
walled_projects = 3
[chat]
rate = 0.6
[privacy]
sensitive_rate = 0.5
[code]
enabled = true
[org]
turnover_rate = 0.3
transfer_rate = 0.2
[messiness]
alias_rate = 0.3
duplicate_rate = 0.2
filler_rate = 0.3
"""
_SALES = """
[isolation]
walled_projects = 4
department = "sales"
groups = [[1, 2], [3], [4]]
[formats]
decks = true
email = true
invites = true
[chat]
rate = 0.4
"""
_ANALYTICS = """
[isolation]
walled_projects = 3
department = "data_analytics"
[formats]
decks = true
"""


def _config_text(example: str, extra: str) -> str:
    text = (_EXAMPLES / example).read_text(encoding="utf-8")
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    for section in re.findall(r"(?m)^\[([a-z_.]+)\]$", extra):
        text = re.sub(rf"(?ms)^\[{re.escape(section)}\]\n(?:(?!^\[).)*", "", text)
    return text + extra


def _build(root: Path, example: str, extra: str) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    cfg = root / "cfg.toml"
    cfg.write_text(_config_text(example, extra), encoding="utf-8")
    assert main(["world", "create", str(cfg), "--worlds-root", str(root / "worlds")]) == 0
    (world_dir,) = list((root / "worlds").iterdir())
    result = export_world(world_dir, root / "out")
    return world_dir, result.directory


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[Path, Path]]:
    base = tmp_path_factory.mktemp("isolation")
    return [
        _build(base / "engineering", "coding-team-clean.toml", _ENGINEERING),
        _build(base / "sales", "analytics-structured-clean.toml", _SALES),
        _build(base / "analytics", "analytics-structured-clean.toml", _ANALYTICS),
    ]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _Graph:
    def __init__(self, world_dir: Path) -> None:
        self.nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
        self.edges = _jsonl(world_dir / "kg" / "edges.jsonl")
        self.walled = {
            i: n["props"]["wall_side"]
            for i, n in self.nodes.items()
            if n["type"] == "Project" and n["props"].get("walled")
        }
        self.pairs = {
            frozenset((e["src"], e["dst"])) for e in self.edges if e["type"] == "walled_from"
        }
        self.staff: dict[str, set[str]] = {p: set() for p in self.walled}
        for e in self.edges:
            if e["type"] == "member_of" and e["dst"] in self.staff:
                self.staff[e["dst"]].add(e["src"])
        self.project_of: dict[str, str] = {}
        for e in self.edges:
            if e["type"] == "expresses" and e["dst"] in self.walled:
                self.project_of[e["src"]] = e["dst"]

    def across(self, a: str | None, b: str | None) -> bool:
        return a is not None and b is not None and frozenset((a, b)) in self.pairs

    def artifacts(self, medium: str | None = None) -> list[str]:
        return [
            a
            for a in self.project_of
            if medium is None or self.nodes[a]["props"].get("medium") == medium
        ]


def _people_edges(graph: _Graph, types: tuple[str, ...]) -> list[tuple[str, str]]:
    return [
        (e["src"], e["dst"])
        for e in graph.edges
        if e["type"] in types and e["dst"] in graph.project_of
    ]


def _authors_are_own_staff(graph: _Graph, medium: str | None) -> list[str]:
    bad = []
    for person, artifact in _people_edges(graph, ("authored", "reviewed")):
        project = graph.project_of[artifact]
        node = graph.nodes[artifact]
        if medium is not None and node["props"].get("medium") != medium:
            continue
        if person not in graph.staff[project]:
            bad.append(f"{person} on {artifact}")
    return bad


def _sensitive_about_own_staff(graph: _Graph) -> list[str]:
    return [
        f"{e['src']} -> {e['dst']}"
        for e in graph.edges
        if e["type"] == "mentions_sensitive"
        and e["src"] in graph.project_of
        and e["dst"] not in graph.staff[graph.project_of[e["src"]]]
        and any(e["dst"] in graph.staff[p] for p in graph.walled)
    ]


def _references_stay_inside(graph: _Graph) -> list[str]:
    return [
        f"{e['src']} -> {e['dst']}"
        for e in graph.edges
        if e["type"] in ("references", "duplicate_of")
        and graph.across(graph.project_of.get(e["src"]), graph.project_of.get(e["dst"]))
    ]


def _facts_stay_inside(graph: _Graph) -> list[str]:
    about = {e["src"]: e["dst"] for e in graph.edges if e["type"] == "about"}
    return [
        f"{e['src']} asserts {e['dst']}"
        for e in graph.edges
        if e["type"] == "asserts"
        and graph.across(graph.project_of.get(e["src"]), about.get(e["dst"]))
    ]


def _successors_stay_inside(graph: _Graph) -> list[str]:
    bad = []
    for e in graph.edges:
        if e["type"] != "replaces":
            continue
        for project, staff in graph.staff.items():
            if e["dst"] in staff and any(
                e["src"] in graph.staff[q] for q in graph.walled if graph.across(project, q)
            ):
                bad.append(f"{e['src']} replaces {e['dst']}")
    return bad


def _staff_disjoint(graph: _Graph) -> list[str]:
    return [
        f"{sorted(pair)} share {sorted(graph.staff[a] & graph.staff[b])}"
        for pair in graph.pairs
        for a, b in [sorted(pair)]
        if graph.staff[a] & graph.staff[b]
    ]


def _staff_names_unique(graph: _Graph) -> list[str]:
    names: dict[str, list[str]] = {}
    for node in graph.nodes.values():
        if node["type"] == "Person":
            names.setdefault(str(node["props"].get("name")), []).append(node["id"])
    staff = set().union(*graph.staff.values())
    return [f"{n}: {ids}" for n, ids in names.items() if len(ids) > 1 and staff & set(ids)]


ENTRY_POINTS: dict[str, Callable[[_Graph], list[str]]] = {
    "names: walled staff are named apart from everyone": _staff_names_unique,
    "staffing (resolver binds, project members)": _staff_disjoint,
    "documents md/docx/jira/pptx/eml/ics: authors and reviewers": lambda g: _authors_are_own_staff(
        g, "document"
    ),
    "chat threads: members": lambda g: _authors_are_own_staff(g, "chat"),
    "code repositories: commit authors": lambda g: _authors_are_own_staff(g, "code"),
    "sensitive sentences: about own staff": _sensitive_about_own_staff,
    "citations and near-duplicate copies": _references_stay_inside,
    "fact ledger and cross-team citing": _facts_stay_inside,
    "turnover successors": _successors_stay_inside,
}


@pytest.mark.parametrize("row", sorted(ENTRY_POINTS))
def test_no_entry_point_crosses_a_wall(worlds: list[tuple[Path, Path]], row: str) -> None:
    for world_dir, _ in worlds:
        graph = _Graph(world_dir)
        assert graph.walled and graph.pairs
        assert ENTRY_POINTS[row](graph) == [], (world_dir.name, row)


def test_the_worlds_exercise_every_medium_and_format(worlds: list[tuple[Path, Path]]) -> None:
    media: set[str] = set()
    formats: set[str] = set()
    for world_dir, _ in worlds:
        graph = _Graph(world_dir)
        for artifact in graph.project_of:
            props = graph.nodes[artifact]["props"]
            media.add(str(props.get("medium")))
            formats.add(Path(str(props.get("path", ""))).suffix)
    assert {"document", "chat", "code"} <= media
    assert {".md", ".docx", ".pptx", ".eml", ".ics"} <= formats


def _map(export_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((export_dir / "isolation.json").read_text(encoding="utf-8"))
    return data


def test_the_export_declares_the_map_as_a_gold_file(worlds: list[tuple[Path, Path]]) -> None:
    for world_dir, export_dir in worlds:
        manifest = json.loads((export_dir / "export.json").read_text(encoding="utf-8"))
        assert "isolation" not in manifest  # the map is a gold file, not inline
        (entry,) = [f for f in manifest["files"] if f["path"] == "isolation.json"]
        assert entry["role"] == "gold"
        iso = _map(export_dir)
        assert set(iso) == {"walls", "projects", "shared"}
        graph = _Graph(world_dir)
        slices = {s["name"]: set(s["files"]) for s in manifest["slices"]}
        by_id = {p["project"]: p for p in iso["projects"]}
        assert set(by_id) == {i for i, n in graph.nodes.items() if n["type"] == "Project"}
        assert {frozenset(w) for w in iso["walls"]} == graph.pairs
        people = {n["props"]["name"] for n in graph.nodes.values() if n["type"] == "Person"}
        owned: dict[str, str] = {}
        for project in iso["projects"]:
            assert project["slices"], project["project"]
            assert set(project["slices"]) <= set(slices)
            for name in project["slices"]:
                for path in slices[name]:
                    assert owned.setdefault(path, project["project"]) == project["project"]
            if project["walled"]:
                assert set(project) == {"project", "walled", "slices", "staff", "values"}
                assert project["staff"] and set(project["staff"]) <= people
                assert any(v["attribute"] == "engagement_code" for v in project["values"])
            else:
                assert set(project) == {"project", "walled", "slices"}
        tokens = [v["value"] for p in iso["projects"] for v in p.get("values", [])]
        assert len(tokens) == len(set(tokens))  # pairwise disjoint across attributes
        data = {f["path"] for f in manifest["files"] if f["role"] == "data"}
        assert set(iso["shared"]["files"]) == {p for p in data if p.startswith("organization/")}
        assert all(p in owned or p.startswith(("organization/", "data/")) for p in data)
        assert set(iso["shared"]["tables"]) == {
            t["name"] for t in manifest["tables"] if t["path"].startswith("data/")
        }


def test_walled_projects_neither_cite_nor_are_cited(worlds: list[tuple[Path, Path]]) -> None:
    for world_dir, _ in worlds:
        graph = _Graph(world_dir)
        depends = [e for e in graph.edges if e["type"] == "depends_on"]
        assert not [e for e in depends if e["src"] in graph.walled or e["dst"] in graph.walled]


def test_groups_wall_only_pairs_on_different_sides(worlds: list[tuple[Path, Path]]) -> None:
    graph = _Graph(worlds[1][0])
    assert len(graph.walled) == 4
    assert len(graph.pairs) == 5  # sides {1,2},{3},{4}: 6 pairs minus the one inside a side


# -- the check refuses each kind of crossing ---------------------------------------


def _mapping() -> dict[str, Any]:
    return {
        "walls": [["project:alpha", "project:beta"]],
        "projects": [
            {
                "project": "project:alpha",
                "walled": True,
                "slices": ["alpha", "alpha-core"],
                "staff": ["Ada Engel"],
                "values": [{"attribute": "x", "value": "v2.0"}],
            },
            {
                "project": "project:beta",
                "walled": True,
                "slices": ["beta"],
                "staff": ["Omar Novak"],
                "values": [{"attribute": "y", "value": "KQT-4821"}],
            },
            {"project": "project:gamma", "walled": False, "slices": ["gamma"]},
        ],
        "shared": {"files": ["organization/people.md"], "tables": []},
    }


_SLICES = {"alpha": ["a.md"], "alpha-core": ["a.md"], "beta": ["b.md"], "gamma": ["g.md"]}
_MARKERS = {
    "project:alpha": ["Alpha Rollout for Client Alder", "initiative:walled-alpha", "project:alpha"],
    "project:beta": ["Beta Rollout for Client Birch", "initiative:walled-beta", "project:beta"],
}


@pytest.mark.parametrize(
    ("path", "text", "found"),
    [
        ("a.md", "Omar Novak reviewed the plan.", "names staff Omar Novak"),
        ("a.md", "Filed under KQT-4821 now.", "states KQT-4821"),
        ("g.md", "Gamma also uses KQT-4821.", "states KQT-4821"),  # unwalled project's file
        ("organization/people.md", "Code KQT-4821.", "states KQT-4821"),  # shared file
        ("n.md", "a file the slicer missed", "assigned to no project"),
        ("a.md", "Ada Engel and v2.0 are our own.", None),
        ("a.md", "We serve v2.01, KQT-48211 and Omar Novakov.", None),  # whole tokens only
        ("a.md", "Omar said hello.", None),  # a first name alone never matches
        ("g.md", "Omar Novak helps gamma too.", None),  # names are walled, not banned
        ("a.md", "See Beta Rollout for Client Birch.", "the project as Beta Rollout"),
        ("a.md", "Cites artifact:initiative:walled-beta:status@x.", "initiative:walled-beta"),
        ("a.md", "Omar\nNovak signed off.", "names staff Omar Novak"),  # whitespace runs
        ("a.md", "Moved to KQT-4821.", "states KQT-4821"),  # a sentence period is a boundary
        ("a.md", "omar novak, kqt-4821", None),  # case-sensitive
    ],
)
def test_the_check_refuses_each_kind_of_finding(path: str, text: str, found: str | None) -> None:
    texts = {"a.md": "alpha", "b.md": "beta", "g.md": "gamma", "organization/people.md": "x"}
    texts[path] = text
    problems = isolation_check(
        _mapping(),
        _SLICES,
        sorted(texts),
        texts.__getitem__,
        shared_paths=["organization/people.md"],
        markers=_MARKERS,
    )
    if found is None:
        assert problems == []
    else:
        assert any(found in p for p in problems), problems


def test_a_shared_file_in_a_slice_is_refused() -> None:
    texts = {"a.md": "alpha", "b.md": "beta", "g.md": "gamma"}
    problems = isolation_check(
        _mapping(), _SLICES, sorted(texts), texts.__getitem__, shared_paths=["a.md"]
    )
    assert any("both shared and in the slices" in p for p in problems)


@pytest.mark.parametrize(
    ("needle", "text", "hit"),
    [
        ("v3.1", "now v3.1.", True),
        ("v3.1", "now v3.10", False),
        ("Omar Novak", "Omar Novak's plan", True),
        ("Omar Novak", "Omar  Novak", True),
        ("Omar Novak", "omar novak", False),
        ("KQT-4821", "KQT-48211", False),
    ],
)
def test_the_shared_matcher(needle: str, text: str, hit: bool) -> None:
    from enterprise_sim.exporters.isolation import matches

    assert matches(needle, text) is hit


def test_a_file_in_two_projects_is_refused() -> None:
    slices = {**_SLICES, "gamma": ["g.md", "a.md"]}
    texts = {"a.md": "alpha", "b.md": "beta", "g.md": "gamma"}
    problems = isolation_check(_mapping(), slices, sorted(texts), texts.__getitem__)
    assert any("in the slices of" in p for p in problems)


def test_a_planted_crossing_refuses_the_export(
    worlds: list[tuple[Path, Path]], tmp_path: Path
) -> None:
    world_dir, export_dir = worlds[0]
    walled = [p for p in _map(export_dir)["projects"] if p["walled"]]
    alpha, beta = walled[:2]
    graph = _Graph(world_dir)
    md = next(
        a
        for a in graph.artifacts("document")
        if str(graph.nodes[a]["props"]["path"]).endswith(".md")
        and graph.project_of[a] == alpha["project"]
    )
    path = world_dir / str(graph.nodes[md]["props"]["path"])
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(original + f"\n{beta['staff'][0]} joined this review.\n", encoding="utf-8")
        with pytest.raises(ExportError, match="crossing"):
            export_world(world_dir, tmp_path / "out")
    finally:
        path.write_text(original, encoding="utf-8")


# -- off by default, config rules --------------------------------------------------


def test_isolation_off_changes_no_world_id() -> None:
    config = load_config(_EXAMPLES / "coding-team-clean.toml")
    explicit = WorldConfig.model_validate({**config.model_dump(mode="json"), "isolation": {}})
    assert compute_config_digest(explicit) == compute_config_digest(config)
    walled = WorldConfig.model_validate(
        {**config.model_dump(mode="json"), "isolation": {"walled_projects": 3}}
    )
    assert compute_config_digest(walled) != compute_config_digest(config)


@pytest.mark.parametrize(
    "fields",
    [
        {"walled_projects": 1},
        {"walled_projects": 6},
        {"walled_projects": 3, "groups": [[1, 2, 3]]},
        {"walled_projects": 3, "groups": [[1, 2], [2, 3]]},
    ],
)
def test_bad_isolation_configs_are_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        IsolationConfig.model_validate(fields)


def test_a_world_without_walls_carries_no_isolation_key(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    cfg = root / "cfg.toml"
    cfg.write_text(_config_text("coding-team-clean.toml", ""), encoding="utf-8")
    assert main(["world", "create", str(cfg), "--worlds-root", str(root / "w")]) == 0
    (world_dir,) = list((root / "w").iterdir())
    result = export_world(world_dir, root / "out")
    manifest = json.loads((result.directory / "export.json").read_text(encoding="utf-8"))
    assert "isolation" not in manifest
    assert not (result.directory / "isolation.json").exists()
