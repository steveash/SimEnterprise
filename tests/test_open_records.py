"""An open derived record carries no row from a restricted document (decision record 0172).

The direction that must never fail is too wide: an open trail would state a team-only or
sensitive document's summary to everyone. Checked in both directions over every trail of
an export with reader lists: no open trail keeps a restricted document's row, and every
open document keeps its row (exactly the restricted rows are dropped).
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
from pathlib import Path

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.access import ACCESS_FILE, check_access
from enterprise_sim.exporters.text_export import export_world

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("open-records") / "a"
    root.mkdir()
    text = (_EXAMPLES / "walled-small-legal.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^\[data\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    extra = "\n[access]\nteam_restricted_rate = 0.3\nrestricted_projects = 1\n"
    (root / "cfg.toml").write_text(text + extra, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (out,) = list((root / "w").iterdir())
    return out


@pytest.fixture(scope="module")
def exports(world: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """(with reader lists, without): the same world exported both ways."""
    listed = export_world(world, tmp_path_factory.mktemp("listed"), with_access=True).directory
    plain = export_world(world, tmp_path_factory.mktemp("plain")).directory
    return listed, plain


def _rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def _readers(export: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(document path -> its readers, record path -> its readers), restricted ones only."""
    rows = json.loads((export / ACCESS_FILE).read_text(encoding="utf-8"))["documents"]
    nodes = {
        n["id"]: n
        for n in map(json.loads, (export / "kg" / "nodes.jsonl").read_text().splitlines())
    }
    docs: dict[str, set[str]] = {}
    records: dict[str, set[str]] = {}
    for r in rows:
        if r["artifact"] is None:
            records.update({p: set(r["readers"]) for p in r["paths"]})
            continue
        for p in [*r["paths"], str(nodes[r["artifact"]]["props"]["path"])]:
            docs[p] = set(r["readers"])
    return docs, records


def _may_read(row: dict[str, str], record: set[str] | None, docs: dict[str, set[str]]) -> bool:
    """R within D: everyone (None) reads an open record; an unrestricted document is everyone's."""
    doc = docs.get(row["document"])
    return doc is None or (record is not None and record <= doc)


def test_every_trail_keeps_exactly_the_rows_all_its_readers_may_read(
    exports: tuple[Path, Path],
) -> None:
    listed, plain = exports
    docs, records = _readers(listed)
    trails = sorted((listed / "trails").glob("*.csv"))
    assert trails
    dropped = {"open": 0, "restricted": 0}
    for trail in trails:
        rel = f"trails/{trail.name}"
        record = records.get(rel)
        before = _rows(plain / rel)
        # both directions: no kept row breaks R within D, and every row that passes is kept
        assert _rows(trail) == [r for r in before if _may_read(r, record, docs)], rel
        dropped["open" if record is None else "restricted"] += len(before) - len(_rows(trail))
    # the fixture has a team-restricted document in an open project, and a sensitive one
    # (narrower than the staff) in a walled project
    assert dropped["open"] > 0 and dropped["restricted"] > 0, dropped


def test_the_manifest_counts_the_withheld_rows(exports: tuple[Path, Path]) -> None:
    listed, plain = exports
    manifest = json.loads((listed / "export.json").read_text(encoding="utf-8"))
    counted = 0
    for t in manifest["tables"]:
        if not str(t["path"]).startswith("trails/"):
            continue
        gone = len(_rows(plain / t["path"])) - len(_rows(listed / t["path"]))
        assert t.get("withheld_rows", 0) == gone, t["path"]
        counted += gone
    assert counted > 0
    assert check_access(listed) == []


def test_without_reader_lists_nothing_changes(exports: tuple[Path, Path]) -> None:
    _, plain = exports
    manifest = json.loads((plain / "export.json").read_text(encoding="utf-8"))
    assert all("withheld_rows" not in t for t in manifest["tables"])


def test_the_check_refuses_a_dropped_row_put_back(
    exports: tuple[Path, Path], tmp_path: Path
) -> None:
    listed, plain = exports
    docs, records = _readers(listed)
    for kind in ("open", "restricted"):  # in an open trail and in a restricted one
        target = None
        for trail in sorted((plain / "trails").glob("*.csv")):
            rel = f"trails/{trail.name}"
            record = records.get(rel)
            if (record is None) != (kind == "open"):
                continue
            gone = [r for r in _rows(trail) if not _may_read(r, record, docs)]
            if gone:
                line = next(
                    x
                    for x in trail.read_text(encoding="utf-8").splitlines()
                    if gone[0]["document"] in x
                )
                target = (rel, line)
                break
        assert target is not None, kind
        edited = tmp_path / kind
        shutil.copytree(listed, edited)
        rel, line = target
        with (edited / rel).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        findings = check_access(edited)
        assert any(rel in f and "may not read" in f for f in findings), (kind, findings)


def test_no_derived_record_needs_a_quoted_cell(exports: tuple[Path, Path]) -> None:
    """Every record is CSV a plain reader splits on commas: no cell holds a comma or quote."""
    listed, _ = exports
    checked = 0
    for kind in ("trails", "registry", "observations"):
        for path in sorted((listed / kind).glob("*.csv")):
            header, *rows = path.read_text(encoding="utf-8").splitlines()
            width = len(header.split(","))
            for line in rows:
                checked += 1
                assert '"' not in line and len(line.split(",")) == width, (path.name, line)
    assert checked
