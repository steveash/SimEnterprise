"""Per-document reader lists (decision record 0130): computed once, gold, checked closed."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.access import ACCESS_FILE, access_lists, check_access
from enterprise_sim.exporters.text_export import ExportError, export_world

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"
_RATE = 0.3


def _world(root: Path, extra: str) -> Path:
    root.mkdir(parents=True)
    text = (_EXAMPLES / "walled-small-legal.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^\[data\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    (root / "cfg.toml").write_text(text + extra, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    return world


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _world(
        tmp_path_factory.mktemp("access") / "a",
        f"\n[access]\nteam_restricted_rate = {_RATE}\nrestricted_projects = 1\n",
    )


@pytest.fixture(scope="module")
def export(world: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("access-export")
    return export_world(world, out, with_access=True, with_knowledge=True).directory


def _graph(
    export: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    nodes = {
        n["id"]: n
        for n in map(json.loads, (export / "kg" / "nodes.jsonl").read_text().splitlines())
    }
    edges = [json.loads(line) for line in (export / "kg" / "edges.jsonl").read_text().splitlines()]
    config = json.loads((export / "config.json").read_text(encoding="utf-8"))
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    files = [f for f in manifest["files"] if f["path"] != ACCESS_FILE]
    return nodes, edges, config, files


def test_the_lists_are_gold_and_hold(export: Path) -> None:
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    (entry,) = [f for f in manifest["files"] if f["path"] == ACCESS_FILE]
    assert entry["role"] == "gold"
    assert all(ACCESS_FILE not in s["files"] for s in manifest["slices"])  # controller-only
    assert check_access(export) == []
    listed = json.loads((export / ACCESS_FILE).read_text(encoding="utf-8"))
    reasons = {r for row in listed["documents"] for r in row["reasons"]}
    assert {"walled", "team"} <= reasons
    for row in listed["documents"]:
        assert row["readers"] and row["readers"] == sorted(set(row["readers"]))


def test_every_rule_keeps_its_readers(export: Path) -> None:
    nodes, edges, config, files = _graph(export)
    lists = access_lists(nodes, edges, config, files)
    authors: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "authored":
            authors.setdefault(e["dst"], set()).add(e["src"])
    project_of = {e["src"]: e["dst"] for e in edges if e["type"] == "expresses"}
    staff: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "member_of" and nodes[e["dst"]]["type"] == "Project":
            staff.setdefault(e["dst"], set()).add(e["src"])
    artifacts = [i for i, n in nodes.items() if n["type"] == "Artifact"]
    walled = {i for i, n in nodes.items() if n["type"] == "Project" and n["props"].get("walled")}
    for aid in artifacts:
        project = project_of.get(aid)
        if project in walled:  # every walled document is restricted to its staff
            assert set(lists[aid]["readers"]) <= staff[project], aid
        if aid in lists:
            assert authors[aid] <= set(lists[aid]["readers"])
    families: dict[str, set[str]] = {}
    for aid in artifacts:
        families.setdefault(nodes[aid]["props"]["event"], set()).add(aid)
    for members in families.values():  # a family is restricted whole, with one list
        restricted = [lists.get(a, {}).get("readers") for a in members]
        assert all(r == restricted[0] for r in restricted)
    # the team rule applies only where no other rule does: unwalled, non-sensitive families
    sensitive = {e["src"] for e in edges if e["type"] == "mentions_sensitive"}
    candidates = [
        f
        for f, ms in families.items()
        if not {project_of.get(a) for a in ms} & walled
        and not ms & sensitive
        and "project" not in lists.get(next(iter(ms)), {}).get("reasons", ())
    ]
    teamed = [
        f for f in candidates if "team" in lists.get(next(iter(families[f])), {}).get("reasons", ())
    ]
    assert sensitive and candidates
    assert 0.1 < len(teamed) / len(candidates) < 0.5  # about the configured share of families
    exported: dict[str, list[str]] = {}
    for f in files:
        if f["role"] == "data":
            exported.setdefault(
                str(f.get("rendered_from") or f.get("source_path") or f["path"]), []
            ).append(f["path"])
    for aid, entry in lists.items():  # every exported file of a restricted artifact is listed
        assert entry["paths"] == sorted(exported.get(nodes[aid]["props"]["path"], []))


def test_the_knowledge_layer_carries_the_same_readers(export: Path) -> None:
    from enterprise_sim.worlds.knowledge import KNOWLEDGE_FILE, check_layer

    layer = json.loads((export / KNOWLEDGE_FILE).read_text(encoding="utf-8"))
    assert check_layer(export, layer) == []
    listed = {
        p: r["readers"]
        for r in json.loads((export / ACCESS_FILE).read_text(encoding="utf-8"))["documents"]
        for p in r["paths"]
    }
    carriers = [c for i in layer["items"] for c in i["carriers"]]
    assert any("readers" in c for c in carriers)
    for c in carriers:
        assert c.get("readers") == listed.get(c["path"])
    for item in layer["items"]:  # reach: everyone as soon as any unrestricted carrier holds it
        reach = item["reach"]
        public = any("readers" not in c and c["verbatim"] for c in item["carriers"])
        if public:
            assert reach["everyone"] and reach["readers"] == []
        if not reach["everyone"]:
            assert reach["readers"] and all("readers" in c for c in item["carriers"])


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["documents"][0].update(readers=[]),
        lambda d: d["documents"][0]["readers"].append("person:nobody"),
        lambda d: d["documents"][0].update(readers=d["documents"][0]["readers"][1:]),
        lambda d: d["documents"][0].update(paths=["artifacts/not-exported.md"]),
        lambda d: d["documents"].pop(0),
    ],
)
def test_the_check_fails_closed(export: Path, tmp_path: Path, edit: Any) -> None:
    edited = tmp_path / "e"
    shutil.copytree(export, edited)
    body = json.loads((edited / ACCESS_FILE).read_text(encoding="utf-8"))
    edit(body)
    (edited / ACCESS_FILE).write_text(json.dumps(body), encoding="utf-8")
    assert check_access(edited)
    assert main(["world", "access-check", str(edited)]) == 1


def test_an_export_refuses_a_list_that_does_not_hold(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enterprise_sim.exporters import access

    real = access.access_document

    def emptied(*args: Any) -> dict[str, Any]:
        body = real(*args)
        body["documents"][0]["readers"] = []
        return body

    monkeypatch.setattr(access, "access_document", emptied)
    with pytest.raises(ExportError, match="reader lists do not hold"):
        export_world(world, tmp_path / "out", with_access=True)


def test_without_the_flag_and_at_rate_zero_nothing_changes(tmp_path: Path) -> None:
    from enterprise_sim.assembly import canonical_config, compute_config_digest
    from enterprise_sim.core.config import WorldConfig, load_config

    plain = load_config(_EXAMPLES / "walled-small-legal.toml")
    rated = WorldConfig.model_validate(
        {**plain.model_dump(mode="json"), "access": {"team_restricted_rate": 0.4}}
    )
    # in the world id when set: an export with reader lists projects its trails (record 0172)
    assert compute_config_digest(rated) != compute_config_digest(plain)
    assert "access" not in canonical_config(plain)  # at its default: not even a key
    world = _world(tmp_path / "p", "")
    out = export_world(world, tmp_path / "out").directory
    assert not (out / ACCESS_FILE).exists()


def test_a_layer_without_access_json_has_no_access_fields(world: Path, tmp_path: Path) -> None:
    from enterprise_sim.worlds.knowledge import KNOWLEDGE_FILE, check_layer

    plain = export_world(world, tmp_path / "out", with_knowledge=True).directory
    layer = json.loads((plain / KNOWLEDGE_FILE).read_text(encoding="utf-8"))
    assert all("reach" not in i for i in layer["items"])
    assert all("readers" not in c for i in layer["items"] for c in i["carriers"])
    assert check_layer(plain, layer) == []


def test_a_walled_or_restricted_projects_records_are_restricted_to_its_staff(export: Path) -> None:
    nodes, edges, _, _ = _graph(export)
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    staff: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "member_of" and nodes[e["dst"]]["type"] == "Project":
            staff.setdefault(e["dst"], set()).add(e["src"])
    rows = json.loads((export / ACCESS_FILE).read_text(encoding="utf-8"))["documents"]
    records = [r for r in rows if r["artifact"] is None]
    reasons = {x for r in records for x in r["reasons"]}
    assert {"walled", "project"} <= reasons  # both kinds of project restrict their records
    record_dirs = ("trails/", "registry/", "observations/")
    for r in records:
        project = r["family"].removeprefix("project:")
        assert r["readers"] == sorted(staff[project])
        assert all(p.startswith(record_dirs) for p in r["paths"])
    covered = {p for r in rows for p in r["paths"]}
    for s in manifest["slices"]:  # every record of a restricted project's slice is covered
        docs = [f for f in s["files"] if f.startswith("artifacts/")]
        if docs and all(f in covered for f in docs):
            assert all(f in covered for f in s["files"] if f.startswith(record_dirs))


def test_a_restricted_project_gives_its_facts_a_restricted_reach(export: Path) -> None:
    from enterprise_sim.worlds.knowledge import KNOWLEDGE_FILE

    layer = json.loads((export / KNOWLEDGE_FILE).read_text(encoding="utf-8"))
    narrow = [i for i in layer["items"] if not i["reach"]["everyone"]]
    assert narrow
    for item in narrow:
        assert item["reach"]["readers"]
        assert all("readers" in c for c in item["carriers"])


@pytest.mark.parametrize("reason", ["walled", "project"])
def test_an_uncovered_record_of_a_walled_or_restricted_project_is_refused(
    export: Path, tmp_path: Path, reason: str
) -> None:
    edited = tmp_path / "e"
    shutil.copytree(export, edited)
    body = json.loads((edited / ACCESS_FILE).read_text(encoding="utf-8"))
    (dropped,) = [r for r in body["documents"] if r["artifact"] is None and reason in r["reasons"]][
        :1
    ]
    body["documents"].remove(dropped)
    (edited / ACCESS_FILE).write_text(json.dumps(body), encoding="utf-8")
    findings = check_access(edited)
    assert any(dropped["paths"][0] in f and "record that no row covers" in f for f in findings), (
        findings
    )
