"""The ``.world`` file: round-trip, atomic update, structural view, discovery."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from enterprise_sim.worlds import (
    WORLD_FILE,
    WorldFile,
    discover_worlds,
    is_world_dir,
    read_world,
    structural_view,
    update_world,
    write_world,
)


def _world(world_id: str = "acme-abc123") -> WorldFile:
    return WorldFile.new(
        world_id=world_id,
        company={"name": "Acme", "vertical": "software", "size": "small"},
        config={"seed": 7, "company": {"name": "Acme"}},
        config_digest="abc123",
        seed=7,
        window={"start": "2026-01-01", "end": "2026-01-31"},
        tool_version="0.1.0",
        at="2026-06-22T00:00:00+00:00",
    )


def test_new_world_starts_with_a_create_history_entry_and_a_created_job() -> None:
    world = _world()
    assert world.history == [
        {"kind": "create", "at": "2026-06-22T00:00:00+00:00", "config_digest": "abc123"}
    ]
    assert world.job.status == "created"
    assert world.created_at == world.updated_at == "2026-06-22T00:00:00+00:00"


def test_round_trips_through_disk(tmp_path: Path) -> None:
    world = _world()
    world.counts = {"nodes": 3, "edges": 2, "events": 5}
    world.job.status = "done"
    world.data["sales_pipeline"] = {"title": "Sales", "rows_by_table": {"dim_rep": 10}}
    write_world(tmp_path, world, at="2026-06-22T01:00:00+00:00")

    on_disk = json.loads((tmp_path / WORLD_FILE).read_text())
    assert on_disk == world.to_dict()
    assert read_world(tmp_path) == world
    assert is_world_dir(tmp_path)
    assert read_world(tmp_path / "nope") is None
    assert not is_world_dir(tmp_path / "nope")


def test_write_is_atomic_and_leaves_no_tmp(tmp_path: Path) -> None:
    write_world(tmp_path, _world())
    assert sorted(p.name for p in tmp_path.iterdir()) == [WORLD_FILE]


def test_update_world_requires_a_world_or_a_factory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        update_world(tmp_path, lambda w: None)

    created = update_world(tmp_path, lambda w: None, create=_world)
    assert created.world_id == "acme-abc123"
    assert read_world(tmp_path) == created

    def bump(world: WorldFile) -> None:
        world.job.artifacts_done += 1

    updated = update_world(tmp_path, bump)
    assert updated.job.artifacts_done == 1
    assert read_world(tmp_path) is not None
    assert read_world(tmp_path).job.artifacts_done == 1  # type: ignore[union-attr]


def test_concurrent_updates_never_lose_an_increment(tmp_path: Path) -> None:
    # Two in-process writers (the pipeline and the job progress sink) share the
    # file; the read-modify-write lock must serialize them.
    write_world(tmp_path, _world())

    def bump(world: WorldFile) -> None:
        world.job.artifacts_done += 1

    def worker() -> None:
        for _ in range(25):
            update_world(tmp_path, bump)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert read_world(tmp_path).job.artifacts_done == 100  # type: ignore[union-attr]


def test_record_extend_adopts_the_config_and_logs_the_change() -> None:
    world = _world()
    world.record_extend(
        config={"seed": 7, "simulation": {"period_end": "2026-02-28"}},
        config_digest="def456",
        changes={"period_end": ["2026-01-31", "2026-02-28"]},
        at="2026-07-01T00:00:00+00:00",
    )
    assert world.config_digest == "def456"
    assert world.config["simulation"] == {"period_end": "2026-02-28"}
    assert world.history[-1]["kind"] == "extend"
    assert world.history[-1]["changes"] == {"period_end": ["2026-01-31", "2026-02-28"]}
    assert world.world_id == "acme-abc123"  # the identity never changes


def test_structural_view_ignores_the_volatile_fields() -> None:
    a = _world()
    b = _world()
    b.created_at = b.updated_at = "2027-01-01T00:00:00+00:00"
    b.history[0]["at"] = "2027-01-01T00:00:00+00:00"
    b.job.status = "done"
    b.job.pid = 4242
    assert a.to_dict() != b.to_dict()
    assert structural_view(a.to_dict()) == structural_view(b.to_dict())


def test_from_dict_tolerates_a_minimal_document() -> None:
    world = WorldFile.from_dict({"world_id": "w"})
    assert world.world_id == "w"
    assert world.job.status == "created"
    assert world.lineage is None
    assert world.history == []


def test_discover_worlds_finds_nested_worlds_and_skips_their_insides(tmp_path: Path) -> None:
    write_world(tmp_path / "worlds" / "a", _world("a"))
    write_world(tmp_path / "worlds" / "group" / "b", _world("b"))
    # A world's data/evals never hold another world, and dot-dirs are skipped.
    write_world(tmp_path / "worlds" / "a" / "data" / "trap", _world("trap"))
    write_world(tmp_path / "worlds" / ".hidden" / "c", _world("c"))
    (tmp_path / "worlds" / "empty").mkdir()
    # Too deep for the default depth budget.
    write_world(tmp_path / "worlds" / "1" / "2" / "3" / "deep", _world("deep"))

    found = [p.relative_to(tmp_path).as_posix() for p in discover_worlds(tmp_path / "worlds")]
    assert found == ["worlds/a", "worlds/group/b"]
    assert list(discover_worlds(tmp_path / "missing")) == []
    # The root itself may be a world.
    assert list(discover_worlds(tmp_path / "worlds" / "a")) == [tmp_path / "worlds" / "a"]
