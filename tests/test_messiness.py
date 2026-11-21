"""``[messiness]``: prose noise and document-level quirks, each labelled in the gold KG."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.core.config.diversity import MessinessConfig
from enterprise_sim.producers.noise import ProseNoise, is_clean

_PEOPLE = (("person:ada-cho", "Ada Cho"), ("person:ben-diaz", "Ben Diaz"))
_TEXT = (
    "Ada Cho drafted the proposal on 2026-02-13 with Ben Diaz. The migration "
    "timeline depends on approval from finance before 2026-03-01. Ada Cho will "
    "circulate the updated estimate afterwards."
)


def _noise(**rates: float) -> ProseNoise:
    return ProseNoise(MessinessConfig(**rates), seed=3)


def test_clean_config_changes_nothing() -> None:
    assert is_clean(MessinessConfig())
    noise = _noise()
    assert noise.apply(_TEXT, key="a", people=_PEOPLE) == _TEXT
    assert noise.quirks("a") == []


@pytest.mark.parametrize(
    ("rates", "kind"),
    [
        ({"typo_rate": 0.2}, "typo"),
        ({"alias_rate": 1.0}, "alias"),
        ({"date_format_mix": 1.0}, "date_format"),
        ({"filler_rate": 1.0}, "filler"),
    ],
)
def test_each_knob_injects_and_labels_its_quirk(rates: dict[str, float], kind: str) -> None:
    noise = _noise(**rates)
    out = noise.apply(_TEXT, key="a", people=_PEOPLE)
    quirks = noise.quirks("a")
    assert quirks and {q["kind"] for q in quirks} == {kind}
    for quirk in quirks:
        assert quirk["surface"] in out
        if kind in ("alias", "date_format"):
            assert quirk["original"] in _TEXT
        if kind == "alias":
            assert quirk["entity"] in {e for e, _ in _PEOPLE}
    if kind == "date_format":
        assert "2026-02-13" not in out and "2026-03-01" not in out


def test_noise_is_deterministic_and_keyed() -> None:
    a = _noise(typo_rate=0.2, alias_rate=0.5).apply(_TEXT, key="x", people=_PEOPLE)
    b = _noise(typo_rate=0.2, alias_rate=0.5).apply(_TEXT, key="x", people=_PEOPLE)
    c = _noise(typo_rate=0.2, alias_rate=0.5).apply(_TEXT, key="y", people=_PEOPLE)
    assert a == b and a != c


def test_typos_spare_names() -> None:
    noise = _noise(typo_rate=0.2)
    out = noise.apply(_TEXT, key="a", people=_PEOPLE)
    assert "Ada Cho" in out and "Ben Diaz" in out


def test_comment_quirks_roll_up_to_their_artifact() -> None:
    noise = _noise(alias_rate=1.0)
    noise.apply("Ada Cho agrees.", key="art:1#c0", people=_PEOPLE)
    noise.apply("Ben Diaz agrees.", key="art:1#c1", people=_PEOPLE)
    noise.apply("Ada Cho agrees.", key="art:10", people=_PEOPLE)
    assert len(noise.quirks("art:1")) == 2


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _build(tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str) -> Path:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 4\n"
        '[company]\nname = "Messy Co"\nvertical = "software"\nsize = "medium"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-02-27\n"
        '[model]\noffline_backend = "synth"\n[facts]\nper_project = 5\n' + body,
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    return world_dir


def test_a_messy_world_labels_every_quirk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(
        tmp_path,
        capsys,
        "[messiness]\ntypo_rate = 0.03\nalias_rate = 0.5\ndate_format_mix = 0.5\n"
        "duplicate_rate = 0.3\ndrop_header_rate = 0.3\nfiller_rate = 0.3\n",
    )
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    artifacts = [n for n in nodes.values() if n["type"] == "Artifact"]
    kinds = {q["kind"] for a in artifacts for q in a["props"].get("quirks", [])}
    assert {"alias", "typo", "filler", "duplicate", "dropped_header"} <= kinds
    for artifact in artifacts:
        path = world_dir / artifact["props"]["path"]
        assert path.is_file(), path
        if path.suffix != ".md":
            continue
        text = path.read_text(encoding="utf-8")
        for quirk in artifact["props"].get("quirks", []):
            if quirk["kind"] in ("alias", "typo", "date_format", "filler"):
                assert quirk["surface"] in text, (path, quirk)
            if quirk["kind"] == "dropped_header":
                assert "**Type:**" not in text
    duplicates = [a for a in artifacts if "duplicate_of" in a["props"]]
    assert duplicates
    dup_edges = {(e["src"], e["dst"]) for e in edges if e["type"] == "duplicate_of"}
    for dup in duplicates:
        assert (dup["id"], dup["props"]["duplicate_of"]) in dup_edges
        assert nodes[dup["props"]["duplicate_of"]]["type"] == "Artifact"


def test_a_clean_world_has_no_quirks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world_dir = _build(tmp_path, capsys, "")
    nodes = _jsonl(world_dir / "kg" / "nodes.jsonl")
    assert not [n for n in nodes if n["props"].get("quirks")]
    assert not [n for n in nodes if "duplicate_of" in n["props"]]
