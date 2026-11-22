"""``[privacy]``: labelled sensitive sentences about people, never PII shapes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world, identity_shapes
from enterprise_sim.producers.sensitive import SensitivePlanter


def test_planter_labels_what_it_plants() -> None:
    planter = SensitivePlanter(1.0, seed=1, department="people_ops")
    people = (("person:ada-cho", "Ada Cho"),)
    out = planter.apply("Body text.", key="art:1", people=people)
    (label,) = planter.labels("art:1")
    assert label["sentence"] in out and label["person"] == "Ada Cho"
    assert label["entity"] == "person:ada-cho" and identity_shapes(out) == []
    quiet = SensitivePlanter(0.0, seed=1, department="people_ops")
    assert quiet.apply("Body text.", key="art:1", people=people) == "Body text."


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_a_people_ops_world_carries_labelled_sensitive_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 3\n"
        '[company]\nname = "Priv Co"\nvertical = "hr"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-02-27\n"
        '[model]\noffline_backend = "synth"\n[privacy]\nsensitive_rate = 0.5\n'
        '[[departments]]\narchetype = "people_ops"\n[[departments]]\narchetype = "engineering"\n',
        encoding="utf-8",
    )
    root = tmp_path / "w"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    labelled = [
        n for n in nodes.values() if n["type"] == "Artifact" and n["props"].get("sensitive")
    ]
    assert labelled
    people_ops = [n for n in labelled if "people-ops" in n["props"]["path"]]
    assert len(people_ops) > len(labelled) - len(people_ops)  # the rate is per department
    sensitive_edges = {(e["src"], e["dst"]) for e in edges if e["type"] == "mentions_sensitive"}
    for node in labelled:
        path = world_dir / node["props"]["path"]
        for label in node["props"]["sensitive"]:
            assert (node["id"], label["entity"]) in sensitive_edges
            assert nodes[label["entity"]]["type"] == "Person"
            if path.suffix == ".md":
                assert label["sentence"] in path.read_text(encoding="utf-8")
    export_world(world_dir, tmp_path / "out")  # passes the identity-shape guard
