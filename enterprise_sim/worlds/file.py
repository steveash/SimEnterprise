"""The ``.world`` file: one world directory, one state file (docs/EXPLORER.md §2).

A **world** is one simulated company on disk — its unstructured corpus + gold KG
*and* its structured data products — under a single directory whose marker is a
``.world`` file at its root. Everything that used to be scattered over
``manifest.json`` / ``config.snapshot.json`` / ``lineage.json`` in the run and
``job.json`` / ``config.json`` / ``state.json`` in a sibling jobs dir lives in
that one JSON document, so a world folder can be zipped, moved, unzipped and
every consumer (the CLI, the explorer, pause/resume) keeps working.

Layout of a world directory::

    <world>/
      .world                 this file (the single source of truth)
      .cache/llm/            the response cache (D31) — relocatable with the world
      .jobs/control.json     cooperative-pause request (transient)
      .jobs/progress.jsonl   append-only progress log (a log, not state)
      organization/ artifacts/ kg/ validation/ evals/   the corpus + gold KG
      data/<scenario>/       structured data products (parquet, views, spec, …)

Writes go through :func:`update_world` — a read-modify-write under a process-wide
lock, persisted with an atomic ``tmp + replace`` — because two in-process writers
touch the same file: the pipeline (build fields at the end of a run) and the job
progress sink (the ``job`` ledger, on every artifact event).

Determinism is *structural*, not byte-identical (D10): :func:`structural_view`
strips the fields that legitimately vary between two builds of the same config
(timestamps, the job ledger) so reproducibility can be asserted on the rest.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from enterprise_sim.jobs.state import JobState

__all__ = [
    "CACHE_DIR",
    "CONTROL_FILE",
    "JOBS_DIR",
    "PROGRESS_LOG",
    "SCHEMA_VERSION",
    "VOLATILE_FIELDS",
    "WORLD_FILE",
    "WorldFile",
    "cache_dir",
    "control_path",
    "discover_worlds",
    "is_world_dir",
    "progress_log_path",
    "read_job_state",
    "read_world",
    "slugify",
    "structural_view",
    "update_world",
    "world_path",
    "write_job_state",
    "write_world",
]

#: The marker + state file at the root of every world directory.
WORLD_FILE = ".world"
#: The response cache lives *inside* the world so pause/resume survives a move.
CACHE_DIR = ".cache/llm"
#: Transient job plumbing (pause request, progress log); never the state of record.
JOBS_DIR = ".jobs"
CONTROL_FILE = "control.json"
PROGRESS_LOG = "progress.jsonl"

# Bumped when the shape changes incompatibly. 2.0 is the first ``.world`` — it
# supersedes ``manifest.json`` 1.2 (whose fields it carries) and absorbs the
# config snapshot, lineage, and the job ledger.
SCHEMA_VERSION = "2.0"

# Fields that legitimately differ between two builds of the same config: wall
# clocks, the job ledger (pids, timings, cost), the history's timestamps, and the
# data products' own stamps. Everything else is a pure function of the config.
VOLATILE_FIELDS: tuple[str, ...] = ("created_at", "updated_at", "history", "job", "data")

_LOCK = threading.RLock()


def slugify(name: str) -> str:
    """Return a filesystem-safe, lowercase slug of ``name`` (empty → ``world``)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "world"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class WorldFile:
    """The contents of ``<world>/.world``.

    The first block (``world_id`` … ``outputs``) is what ``manifest.json`` used to
    carry: identity, the config digest, the render backend, KG counts, the
    validation summary and the relative paths of the outputs. ``config`` is the
    *current effective* config (what ``config.snapshot.json`` was); ``history``
    records every create / extend / data pass that shaped the world; ``lineage``
    points at the parent world when this one was forked from another; ``job`` is
    the run ledger (status / progress / cost / segments) that a UI lists worlds
    from; ``data`` summarises each structured-data scenario under ``data/``.
    """

    world_id: str
    company: dict[str, Any]
    config: dict[str, Any]
    config_digest: str
    seed: int
    window: dict[str, str]
    schema_version: str = SCHEMA_VERSION
    tool_version: str = ""
    render_backend: str = "fake"
    live: bool = False
    created_at: str = ""
    updated_at: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    lineage: dict[str, Any] | None = None
    job: JobState = field(default_factory=JobState)
    data: dict[str, dict[str, Any]] = field(default_factory=dict)
    evals: dict[str, Any] = field(default_factory=dict)

    # -- construction ---------------------------------------------------------

    @classmethod
    def new(
        cls,
        *,
        world_id: str,
        company: Mapping[str, Any],
        config: Mapping[str, Any],
        config_digest: str,
        seed: int,
        window: Mapping[str, str],
        tool_version: str,
        render_backend: str = "fake",
        live: bool = False,
        at: str | None = None,
        lineage: Mapping[str, Any] | None = None,
    ) -> WorldFile:
        """A freshly created world: one ``create`` history entry, job ``created``."""
        stamp = at if at is not None else _now()
        return cls(
            world_id=world_id,
            company=dict(company),
            config=dict(config),
            config_digest=config_digest,
            seed=seed,
            window=dict(window),
            tool_version=tool_version,
            render_backend=render_backend,
            live=live,
            created_at=stamp,
            updated_at=stamp,
            history=[{"kind": "create", "at": stamp, "config_digest": config_digest}],
            lineage=dict(lineage) if lineage is not None else None,
        )

    # -- mutation helpers -----------------------------------------------------

    def record_extend(
        self, *, config: Mapping[str, Any], config_digest: str, changes: Mapping[str, Any], at: str
    ) -> None:
        """Adopt an extended config in place and log what changed (§3.5)."""
        self.config = dict(config)
        self.config_digest = config_digest
        self.history.append(
            {"kind": "extend", "at": at, "config_digest": config_digest, "changes": dict(changes)}
        )
        self.updated_at = at

    # -- serialization --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping this file serializes to."""
        return {
            "schema_version": self.schema_version,
            "world_id": self.world_id,
            "tool_version": self.tool_version,
            "company": dict(self.company),
            "seed": self.seed,
            "window": dict(self.window),
            "config": dict(self.config),
            "config_digest": self.config_digest,
            "render_backend": self.render_backend,
            "live": self.live,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "counts": dict(self.counts),
            "validation": dict(self.validation),
            "outputs": dict(self.outputs),
            "history": [dict(h) for h in self.history],
            "lineage": dict(self.lineage) if self.lineage is not None else None,
            "job": self.job.to_dict(),
            "data": {k: dict(v) for k, v in self.data.items()},
            "evals": dict(self.evals),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorldFile:
        """Reconstruct a :class:`WorldFile` from :meth:`to_dict` output."""
        lineage = data.get("lineage")
        return cls(
            schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
            world_id=data["world_id"],
            tool_version=str(data.get("tool_version", "")),
            company=dict(data.get("company", {})),
            seed=int(data.get("seed", 0)),
            window=dict(data.get("window", {})),
            config=dict(data.get("config", {})),
            config_digest=str(data.get("config_digest", "")),
            render_backend=str(data.get("render_backend", "fake")),
            live=bool(data.get("live", False)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            counts=dict(data.get("counts", {})),
            validation=dict(data.get("validation", {})),
            outputs=dict(data.get("outputs", {})),
            history=[dict(h) for h in data.get("history", [])],
            lineage=dict(lineage) if isinstance(lineage, Mapping) else None,
            job=JobState.from_dict(data.get("job", {})),
            data={k: dict(v) for k, v in data.get("data", {}).items()},
            evals=dict(data.get("evals", {})),
        )


def structural_view(world: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a ``.world`` mapping with the volatile fields removed.

    The canonical form for the reproducibility guarantee: two builds of the same
    config produce identical structural views.
    """
    return {k: v for k, v in world.items() if k not in VOLATILE_FIELDS}


# -- paths --------------------------------------------------------------------


def world_path(world_dir: Path | str) -> Path:
    """The ``.world`` file of ``world_dir``."""
    return Path(world_dir) / WORLD_FILE


def cache_dir(world_dir: Path | str) -> Path:
    """The world-local response cache directory."""
    return Path(world_dir) / CACHE_DIR


def control_path(world_dir: Path | str) -> Path:
    """The cooperative-pause request file."""
    return Path(world_dir) / JOBS_DIR / CONTROL_FILE


def progress_log_path(world_dir: Path | str) -> Path:
    """The append-only progress log a UI replays to rebuild a live view."""
    return Path(world_dir) / JOBS_DIR / PROGRESS_LOG


def is_world_dir(path: Path | str) -> bool:
    """``True`` iff ``path`` holds a ``.world`` file — the one and only world test."""
    return world_path(path).is_file()


# -- I/O ----------------------------------------------------------------------


def read_world(world_dir: Path | str) -> WorldFile | None:
    """Read ``<world_dir>/.world``, or ``None`` if there is none."""
    path = world_path(world_dir)
    if not path.is_file():
        return None
    return WorldFile.from_dict(json.loads(path.read_text(encoding="utf-8")))


def write_world(world_dir: Path | str, world: WorldFile, *, at: str | None = None) -> None:
    """Atomically write ``.world`` (tmp + replace), stamping ``updated_at``.

    ``at`` pins the stamp (tests use it for byte-stable output); by default it is
    the wall clock.
    """
    world.updated_at = at if at is not None else _now()
    path = world_path(world_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{WORLD_FILE}.tmp")
    with _LOCK:
        tmp.write_text(
            json.dumps(world.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)


def update_world(
    world_dir: Path | str,
    mutate: Callable[[WorldFile], None],
    *,
    create: Callable[[], WorldFile] | None = None,
    at: str | None = None,
) -> WorldFile:
    """Read-modify-write ``.world`` under the process-wide lock.

    ``mutate`` receives the current file (or ``create()``'s fresh one when none
    exists yet) and edits it in place. Raises ``FileNotFoundError`` when there is
    no ``.world`` and no ``create`` factory — a caller that only *updates* must
    not silently mint a world.
    """
    with _LOCK:
        world = read_world(world_dir)
        if world is None:
            if create is None:
                raise FileNotFoundError(f"no {WORLD_FILE} in {world_dir}")
            world = create()
        mutate(world)
        write_world(world_dir, world, at=at)
        return world


# -- the job ledger -------------------------------------------------------------


def read_job_state(world_dir: Path | str) -> JobState | None:
    """The world's job ledger (``.world`` → ``job``), or ``None`` without a ``.world``."""
    world = read_world(world_dir)
    return world.job if world is not None else None


def write_job_state(world_dir: Path | str, state: JobState) -> None:
    """Persist ``state`` as the world's job ledger (atomic read-modify-write).

    Only the ``job`` block is replaced; every other field keeps whatever the
    pipeline last wrote, so the progress sink and the build never clobber each
    other. Stamps ``state.updated_at`` like the old ``state.json`` writer did.
    """
    state.updated_at = _now()

    def mutate(world: WorldFile) -> None:
        world.job = state

    update_world(world_dir, mutate)


# -- discovery ----------------------------------------------------------------


def discover_worlds(root: Path | str, *, max_depth: int = 3) -> Iterator[Path]:
    """Yield every world directory under ``root`` (sorted, depth-first, ≤ ``max_depth``).

    A directory holding a ``.world`` is yielded and **not** descended into (a
    world's ``data/`` or ``evals/`` never contains another world). Dot-directories
    are skipped, so a world's own ``.cache`` / ``.jobs`` are never walked.
    """
    root = Path(root)

    def walk(dir_path: Path, depth: int) -> Iterator[Path]:
        if is_world_dir(dir_path):
            yield dir_path
            return
        if depth >= max_depth:
            return
        try:
            entries = sorted(p for p in dir_path.iterdir() if p.is_dir())
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            yield from walk(entry, depth + 1)

    if root.is_dir():
        yield from walk(root, 0)
