"""``world adopt``: turn a pre-``.world`` run directory into a world (one-off migration).

Before the world model, a build landed as ``<runs>/<run-id>/`` with its state
spread over ``manifest.json`` + ``config.snapshot.json`` (+ ``lineage.json``),
and each structured-data scenario went to a *separate* ``<runs>-data/<scenario>-<digest>/``
folder with its own ``manifest.json``. :func:`adopt_run_dir` folds all of that
into one world: it writes the ``.world`` from the old files (the ``run_id``
becomes the ``world_id``, so nothing pinned changes), moves the folder under a
worlds root if asked, copies the old external response cache into
``<world>/.cache/llm`` so extends stay cheap, and moves each old data-run
folder to ``<world>/data/<scenario>/``. The old state files are removed once
the ``.world`` carries their contents.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from enterprise_sim.jobs.state import JobState

__all__ = ["AdoptError", "adopt_run_dir"]

_MANIFEST = "manifest.json"
_SNAPSHOT = "config.snapshot.json"
_LINEAGE = "lineage.json"
#: Where a world keeps its structured data (mirrors ``data_products.runner.DATA_DIR``
#: without importing that module, which needs the optional data extra).
_DATA_DIR = "data"


class AdoptError(ValueError):
    """The directory is not an old-style run (or the destination is taken)."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdoptError(
            f"{path.parent} is not an old-style run directory: no {path.name}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AdoptError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdoptError(f"{path} does not hold a JSON object")
    return payload


def adopt_run_dir(
    old_dir: Path | str,
    *,
    worlds_root: Path | str | None = None,
    data_dirs: Sequence[Path | str] = (),
) -> Path:
    """Convert ``old_dir`` into a world; returns the world directory.

    ``worlds_root`` moves the folder to ``<worlds_root>/<world-id>/`` (the
    destination must not exist yet); without it the folder becomes a world where
    it is. Each of ``data_dirs`` is an old data-run folder to fold into the
    world's ``data/``. Raises :class:`AdoptError` for anything that is not an
    old-style run, and refuses a directory that already holds a ``.world``.
    """
    from enterprise_sim.assembly import WORLD_OUTPUTS, canonical_config
    from enterprise_sim.core.config import WorldConfig
    from enterprise_sim.jobs.extend import copy_cache
    from enterprise_sim.worlds.file import WorldFile, cache_dir, is_world_dir, write_world

    old_dir = Path(old_dir)
    if is_world_dir(old_dir):
        raise AdoptError(f"{old_dir} already is a world")
    manifest = _read_json(old_dir / _MANIFEST)
    snapshot = _read_json(old_dir / _SNAPSHOT)
    if "run_id" not in manifest or "config_digest" not in manifest:
        raise AdoptError(f"{old_dir / _MANIFEST} is not a run manifest (no run_id)")
    world_id = str(manifest["run_id"])
    generated_at = str(manifest.get("generated_at", ""))
    backend = str(manifest.get("render_backend", "fake"))
    # The old snapshot carried its destination under the old field name; a
    # world never records where it lives.
    snapshot.pop("output_dir", None)
    snapshot.pop("worlds_root", None)
    config = WorldConfig.model_validate(snapshot)

    dest = old_dir
    if worlds_root is not None:
        dest = Path(worlds_root) / world_id
        if dest.exists():
            raise AdoptError(f"{dest} already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_dir), str(dest))

    # The old external response cache (D31) becomes world-local, so a later
    # extend still reuses every rendered artifact; the recorded config then
    # stops pointing outside the folder.
    old_cache = config.scale.cache_dir
    if old_cache:
        copy_cache(Path(old_cache), cache_dir(dest))
        config = config.model_copy(
            update={"scale": config.scale.model_copy(update={"cache_dir": None})}
        )

    company = dict(manifest.get("company", {}))
    company.setdefault("description", config.company.description)
    world = WorldFile.new(
        world_id=world_id,
        company=company,
        config=canonical_config(config),
        config_digest=str(manifest["config_digest"]),
        seed=int(manifest.get("seed", config.seed)),
        window=dict(manifest.get("window", {})),
        tool_version=str(manifest.get("tool_version", "")),
        render_backend=backend,
        live=backend != "fake",
        at=generated_at or None,
    )
    world.history[0]["adopted_from"] = str(old_dir)
    world.counts = dict(manifest.get("counts", {}))
    world.validation = dict(manifest.get("validation", {}))
    world.outputs = dict(WORLD_OUTPUTS)

    lineage_path = dest / _LINEAGE
    if lineage_path.is_file():
        lineage = _read_json(lineage_path)
        world.lineage = {
            "parent_world_id": lineage.get("parent_run_id"),
            "parent_world_dir": lineage.get("parent_run_dir"),
            "changes": dict(lineage.get("changes") or {}),
        }

    artifacts = sum(1 for p in (dest / "artifacts").rglob("*") if p.is_file())
    world.job = JobState(
        status="done",
        phase="done",
        artifacts_total=artifacts,
        artifacts_done=artifacts,
        started_at=world.created_at,
        finished_at=world.created_at,
    )
    questions = dest / "evals" / "questions.jsonl"
    if questions.is_file():
        world.evals = {"questions": sum(1 for line in questions.read_text().splitlines() if line)}

    for data_dir in data_dirs:
        _adopt_data_dir(Path(data_dir), dest, world)

    write_world(dest, world, at=world.created_at or None)
    for stale in (_MANIFEST, _SNAPSHOT, _LINEAGE):
        (dest / stale).unlink(missing_ok=True)
    return dest


def _adopt_data_dir(old: Path, dest: Path, world: Any) -> None:
    """Move an old data-run folder to ``<dest>/data/<scenario>/`` and record it."""
    manifest = _read_json(old / _MANIFEST)
    scenario = manifest.get("scenario")
    if not isinstance(scenario, str) or not scenario:
        raise AdoptError(f"{old / _MANIFEST} is not a data-run manifest (no scenario)")
    target = dest / _DATA_DIR / scenario
    if target.exists():
        raise AdoptError(f"{target} already exists")
    target.mkdir(parents=True)
    # Old layout: <old>/data/{tables,views} + spec*.json, lineage.json, questions.json.
    for name in ("tables", "views"):
        src = old / _DATA_DIR / name
        if src.is_dir():
            shutil.move(str(src), str(target / name))
    for src in sorted(old.iterdir()):
        if src.is_file() and src.name != _MANIFEST:
            shutil.move(str(src), str(target / src.name))
    shutil.rmtree(old, ignore_errors=True)

    iterations = list(manifest.get("iterations") or [])
    summary = {k: v for k, v in manifest.items() if k != "config"}
    summary["dir"] = f"{_DATA_DIR}/{scenario}/"
    summary["rows_by_table"] = dict(iterations[-1].get("rows_by_table", {})) if iterations else {}
    summary["adopted_from"] = str(old)
    world.data[scenario] = summary
    world.history.append(
        {
            "kind": "data",
            "at": manifest.get("created_at"),
            "scenarios": [scenario],
            "config": manifest.get("config"),
            "adopted_from": str(old),
        }
    )
