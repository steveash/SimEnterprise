"""``world heldout``: held-out variants from a secret seed file outside every worktree.

Invariant: neither the seed file nor any output lives inside a git worktree. The
refusal table below covers every path the command takes; a new path argument must
go through ``outside_worktrees`` and join the table.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.worlds.heldout import (
    HeldoutError,
    build_heldout,
    default_families,
    heldout_config,
    outside_worktrees,
    read_secret,
)

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"
_FAMILY = _EXAMPLES / "coding-team-clean.toml"
_COMPARED = ("artifacts", "kg", "evals", "organization", "data")


def _secret(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / "secrets" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tree(world_dir: Path) -> dict[str, bytes]:
    return {
        p.relative_to(world_dir).as_posix(): p.read_bytes()
        for part in _COMPARED
        if (world_dir / part).exists()
        for p in (world_dir / part).rglob("*")
        if p.is_file()
    }


def _facts(world_dir: Path) -> dict[str, str]:
    """FactVersion id -> value, keyed by (fact attribute, version) so seeds can be compared."""
    out = {}
    for line in (world_dir / "kg" / "nodes.jsonl").read_text(encoding="utf-8").splitlines():
        node = json.loads(line)
        if node["type"] == "FactVersion":
            out[node["id"]] = str(node["props"].get("value"))
    return out


def test_the_same_seed_file_renders_byte_identical_worlds(tmp_path: Path) -> None:
    seed = _secret(tmp_path, "a", "first secret value, long enough\n")
    (one,) = build_heldout([_FAMILY], seed, tmp_path / "store-1")
    (two,) = build_heldout([_FAMILY], seed, tmp_path / "store-2")
    assert one["status"] == two["status"] == "done"
    assert one["world_id"] == two["world_id"]
    first, second = _tree(Path(one["world_dir"])), _tree(Path(two["world_dir"]))
    assert first and first == second


def test_a_different_seed_file_changes_the_seed_the_name_and_the_facts(tmp_path: Path) -> None:
    a = _secret(tmp_path, "a", "first secret value, long enough\n")
    b = _secret(tmp_path, "b", "second secret value, also long\n")
    (one,) = build_heldout([_FAMILY], a, tmp_path / "store-a")
    (two,) = build_heldout([_FAMILY], b, tmp_path / "store-b")
    assert one["world_id"] != two["world_id"]
    facts_a, facts_b = _facts(Path(one["world_dir"])), _facts(Path(two["world_dir"]))
    assert facts_a and facts_b
    assert set(facts_a.values()) != set(facts_b.values())
    seeds = []
    for summary in (one, two):
        world = json.loads((Path(summary["world_dir"]) / ".world").read_text(encoding="utf-8"))
        seeds.append(world["config"]["seed"])
    assert seeds[0] != seeds[1]


def test_derivation_differs_per_family_and_never_keeps_the_public_name() -> None:
    from enterprise_sim.core.config import load_config

    config = load_config(_FAMILY)
    secret = b"a secret of more than sixteen bytes"
    one = heldout_config(config, secret, "coding-team-clean", Path("/store"))
    two = heldout_config(config, secret, "coding-team-messy", Path("/store"))
    assert one.seed != two.seed and one.seed != config.seed
    assert one.company.name != config.company.name
    assert one.worlds_root == Path("/store")
    assert heldout_config(config, secret, "coding-team-clean", Path("/store")) == one


def _worktree(tmp_path: Path, kind: str) -> Path:
    repo = tmp_path / f"repo-{kind}"
    repo.mkdir(parents=True)
    if kind == "checkout":
        (repo / ".git").mkdir()
    else:  # a linked worktree has a .git file
        (repo / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    return repo


@pytest.mark.parametrize("kind", ["checkout", "linked"])
@pytest.mark.parametrize("entry", ["seed file", "output directory", "output subdirectory"])
def test_every_path_inside_a_worktree_is_refused(tmp_path: Path, kind: str, entry: str) -> None:
    repo = _worktree(tmp_path, kind)
    seed = _secret(tmp_path, "ok", "a secret of more than sixteen bytes\n")
    out = tmp_path / "store"
    if entry == "seed file":
        seed = _secret(repo, "seed", "a secret of more than sixteen bytes\n")
    elif entry == "output directory":
        out = repo
    else:
        out = repo / "deep" / "store"
    with pytest.raises(HeldoutError, match="inside a git worktree"):
        build_heldout([_FAMILY], seed, out)
    assert not (tmp_path / "store").exists()
    assert not (repo / "deep").exists()


def test_the_choke_point_accepts_a_plain_directory(tmp_path: Path) -> None:
    assert outside_worktrees(tmp_path / "x" / "y", role="out") == (tmp_path / "x" / "y").resolve()


def test_a_short_seed_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HeldoutError, match="under 16 bytes"):
        read_secret(_secret(tmp_path, "short", "tiny\n"))


def test_the_cli_prints_no_secret_and_no_derived_seed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "the cli secret, long enough to use"
    seed = _secret(tmp_path, "cli", text + "\n")
    out = tmp_path / "store"
    code = main(["world", "heldout", str(_FAMILY), "--seed-file", str(seed), "--out", str(out)])
    printed = capsys.readouterr()
    assert code == 0
    summary: dict[str, Any] = json.loads(printed.out)
    (world,) = summary["worlds"]
    derived = json.loads((Path(world["world_dir"]) / ".world").read_text("utf-8"))["config"]["seed"]
    for stream in (printed.out, printed.err):
        assert text not in stream and str(derived) not in stream
    refused = main(
        [
            "world",
            "heldout",
            "--seed-file",
            str(seed),
            "--out",
            str(_worktree(tmp_path, "checkout")),
        ]
    )
    assert refused == 2


def test_the_default_families_are_every_example_world() -> None:
    assert [p.name for p in default_families()] == sorted(p.name for p in _EXAMPLES.glob("*.toml"))
    assert len(default_families()) >= 9
