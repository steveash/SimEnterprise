"""Rebuild a world from its export: ``config.json`` plus the recorded model responses.

A live world is not reproducible by calling the model again, but it is by
replaying what the model said. :func:`replay_export` mints a world from the
export's ``config.json`` (its recorded render backend, so it gets the same world
id), seeds the world's response cache from ``replay/llm-cache.jsonl`` and builds
it: every model request is a cache hit, so no call is made and the documents come
out as recorded. A request missing from the replay file would call the backend;
:func:`replay_export` refuses to start if the backend cannot be reached that way
(it sets a backend that fails on any call).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["ReplayError", "replay_export"]


class ReplayError(Exception):
    """Raised when an export cannot be replayed."""


def replay_export(export_dir: Path | str, worlds_root: Path | str) -> Path:
    """Rebuild ``export_dir``'s world under ``worlds_root``; return its directory."""
    from enterprise_sim.assembly.runner import build_world_file, compute_world_id, execute_run
    from enterprise_sim.core.config import WorldConfig
    from enterprise_sim.core.llm import LLMClient, LLMConfig
    from enterprise_sim.core.llm.cache import ResponseCache
    from enterprise_sim.exporters.text_export import REPLAY_KEY_BACK, REPLAY_USAGE_KEYS
    from enterprise_sim.worlds.file import WorldFile, update_world, write_world

    export_dir = Path(export_dir)
    manifest = json.loads((export_dir / "export.json").read_text(encoding="utf-8"))
    config_data = json.loads((export_dir / "config.json").read_text(encoding="utf-8"))
    replay = export_dir / "replay" / "llm-cache.jsonl"
    backend = str(manifest.get("render_backend", "fake"))
    if backend not in ("fake", "synth") and not replay.is_file():
        raise ReplayError(f"{export_dir}: a {backend} world needs replay/llm-cache.jsonl")
    config = WorldConfig.model_validate({**config_data, "worlds_root": str(worlds_root)})
    world_id = compute_world_id(config, backend=backend)
    if world_id != manifest["world_id"]:
        raise ReplayError(
            f"config.json gives world id {world_id}, export says {manifest['world_id']}"
        )
    world_dir = Path(worlds_root) / world_id
    if world_dir.exists():
        raise ReplayError(f"{world_dir}: already exists")
    cache_dir = world_dir / ".cache" / "llm"
    cache_dir.mkdir(parents=True)
    if replay.is_file():
        for line in replay.read_text(encoding="utf-8").splitlines():
            if line:
                entry = json.loads(line)
                completion = dict(entry["completion"])
                back = {v: k for k, v in REPLAY_USAGE_KEYS.items()}
                completion["usage"] = {
                    back.get(k, k): v for k, v in (completion.get("usage") or {}).items()
                }
                key = str(entry["key"]).translate(REPLAY_KEY_BACK)
                (cache_dir / f"{key}.json").write_text(
                    json.dumps(completion, sort_keys=True, indent=2), encoding="utf-8"
                )
    live = backend not in ("fake", "synth")
    write_world(world_dir, build_world_file(config, backend=backend, live=live))
    llm = LLMConfig(
        backend=backend,  # the request keys of the cache fold the backend name in
        model=config.model.name,
        max_concurrency=config.scale.max_concurrency,
        cache_dir=str(cache_dir),
    )
    client = (
        LLMClient.from_config(llm)
        if not live
        else LLMClient(
            _NoCalls(backend),
            config=llm,
            cache=ResponseCache(str(cache_dir), enabled=True),
        )
    )
    if config.data.scenarios and config.data.authoring.value == "llm":
        raise ReplayError("llm-authored data specs are not recorded; replay supports template data")
    execute_run(config, client=client, live=live, world_dir=world_dir)
    # The rest of a build, as the job worker runs it: data products (template
    # authoring makes no model call) and the eval questions.
    from enterprise_sim.jobs.progress import NullSink
    from enterprise_sim.jobs.worker import _build_data, _finalize

    if config.data.scenarios:
        _build_data(world_dir, config, live=False, progress=NullSink())
    _finalize(world_dir, NullSink())

    def done(world: WorldFile) -> None:
        world.job.status = "done"
        world.job.phase = "done"

    update_world(world_dir, done)
    return world_dir


class _NoCalls:
    """A backend that refuses every call: a replay must be served from the cache."""

    def __init__(self, name: str) -> None:
        self.name = name

    def generate_structured(self, *args: Any, **kwargs: Any) -> Any:
        raise ReplayError("a request is missing from the replay file (structured)")

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        raise ReplayError("a request is missing from the replay file (content)")
