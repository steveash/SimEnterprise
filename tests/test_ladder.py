"""``world ladder-check``: one seed at several sizes with the base facts held constant."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world
from enterprise_sim.worlds.ladder import LadderError, ladder_check

_LADDER = Path(__file__).resolve().parents[1] / "examples" / "worlds" / "ladder"
_BASE = ("data_analytics", "marketing", "sales")


def _rung(root: Path, rung: str, *, squads: str | None = None) -> Path:
    root.mkdir(parents=True)
    text = (_LADDER / f"ladder-{rung}.toml").read_text(encoding="utf-8")
    text = re.sub(
        r"(?m)^scale_factor = .*$",
        "scale_factor = " + {"s": "0.02", "m": "0.05", "l": "0.1"}[rung],
        text,
    )
    if squads is not None:
        text += f'\n[org]\nsquads_per_team = "{squads}"\n'
    (root / "cfg.toml").write_text(text, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    return export_world(world, root / "out").directory


@pytest.fixture(scope="module")
def rungs(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, Path]]:
    base = tmp_path_factory.mktemp("ladder")
    return [(size.upper(), _rung(base / size, size)) for size in ("s", "m", "l")]


def test_the_rungs_grow_and_keep_every_base_fact_and_statement(
    rungs: list[tuple[str, Path]],
) -> None:
    record = ladder_check("simx-ladder-corvid", rungs, _BASE)
    sizes = record["rungs"]
    assert [r["size"] for r in sizes] == ["S", "M", "L"]
    for a, b in zip(sizes, sizes[1:], strict=False):
        assert (
            b["people"] > a["people"] and b["documents"] > a["documents"] and b["rows"] > a["rows"]
        )
    assert len({r["world_id"] for r in sizes}) == 3
    assert record["invariant_facts"] and record["invariant_assertions"] > 0
    stated = record["statements_in_text"]
    assert stated["stating"] > 0 and stated["value_in_text"] == stated["stating"]


def test_a_ladder_whose_base_changes_is_refused(
    rungs: list[tuple[str, Path]], tmp_path: Path
) -> None:
    other = _rung(tmp_path / "l2", "l", squads="2..2")  # more squads re-staff the base departments
    with pytest.raises(LadderError, match="base facts differ"):
        ladder_check("simx-ladder-corvid", [rungs[0], rungs[1], ("L", other)], _BASE)


def test_a_ladder_that_does_not_grow_is_refused(rungs: list[tuple[str, Path]]) -> None:
    with pytest.raises(LadderError, match="does not grow"):
        ladder_check("simx-ladder-corvid", [rungs[1], rungs[0]], _BASE)


def test_the_cli_writes_the_record(rungs: list[tuple[str, Path]], tmp_path: Path) -> None:
    out = tmp_path / "ladder.json"
    args = ["world", "ladder-check", "--ladder-id", "simx-ladder-corvid"]
    for dept in _BASE:
        args += ["--base", dept]
    args += [f"{size}={path}" for size, path in rungs] + ["--out", str(out)]
    assert main(args) == 0
    assert out.is_file()


def test_the_ladder_label_is_exported_and_changes_no_world_id(
    rungs: list[tuple[str, Path]],
) -> None:
    import json

    from enterprise_sim.assembly import canonical_config, compute_config_digest
    from enterprise_sim.core.config import WorldConfig, load_config

    for size, export in rungs:
        manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
        assert manifest["ladder"] == {"id": "simx-ladder-corvid", "rung": size.lower()}
    config = load_config(_LADDER / "ladder-m.toml")
    unlabelled = WorldConfig.model_validate(
        {**config.model_dump(mode="json"), "ladder": {"id": None, "rung": None}}
    )
    assert compute_config_digest(unlabelled) == compute_config_digest(config)
    assert "ladder" not in canonical_config(unlabelled)  # unset: not even a key


def test_a_ladder_needs_both_id_and_rung() -> None:
    from enterprise_sim.core.config.ladder import LadderConfig

    with pytest.raises(ValueError):
        LadderConfig.model_validate({"id": "x"})


def test_a_mislabelled_rung_is_refused(rungs: list[tuple[str, Path]]) -> None:
    with pytest.raises(LadderError, match="labelled"):
        ladder_check("another-ladder", rungs, _BASE)


def test_a_value_missing_from_one_rungs_text_is_refused(
    rungs: list[tuple[str, Path]], tmp_path: Path
) -> None:
    import json
    import shutil

    from enterprise_sim.facts.catalog import value_token
    from enterprise_sim.worlds import ladder

    edited = tmp_path / "l"
    shutil.copytree(rungs[2][1], edited)
    stated = ladder._rung(edited, _BASE)["_stated"]
    world_path, _, value = next(k for k, present in sorted(stated.items()) if present)
    manifest = json.loads((edited / "export.json").read_text(encoding="utf-8"))
    (path,) = [
        f["path"]
        for f in manifest["files"]
        if (f.get("rendered_from") or f.get("source_path") or f["path"]) == world_path
    ]
    doc = edited / path
    doc.write_text(doc.read_text(encoding="utf-8").replace(value_token(value), ""), "utf-8")
    with pytest.raises(LadderError, match="stated in the text"):
        ladder_check("simx-ladder-corvid", [rungs[0], rungs[1], ("L", edited)], _BASE)
