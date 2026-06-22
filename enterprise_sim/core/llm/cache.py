"""On-disk response cache keyed by ``(prompt_hash, model)`` (ARCHITECTURE.md §16.4, D31).

A run that re-executes with the same config + seed issues the *same* calls in the
same order (determinism is structural, §7). Caching each response on disk means
only *changed* artifacts regenerate — re-runs are cheap and reproducible. The key
folds the prompt hash, model, generation mode, and the schema/candidate set so a
structured call and a prose call over the same prompt never collide.

The cache is a plain directory of JSON files (one per key); it is safe to delete,
share, or check the size of, and needs no server. Concurrent writers race only to
write *identical* content, so a last-writer-wins overwrite is correct.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from enterprise_sim.core.llm.prompt import Prompt
from enterprise_sim.core.llm.types import Completion


def request_key(
    *,
    prompt: Prompt,
    model: str,
    mode: str,
    schema: Mapping[str, Any] | None = None,
    candidates: tuple[str, ...] = (),
    temperature: float = 0.0,
) -> str:
    """Compute the cache key for one request.

    Per D31 the key is anchored on ``(prompt_hash, model)`` but also folds the
    generation ``mode`` (``"structured"`` / ``"content"``), the JSON schema, the
    candidate reference set, and temperature — anything that changes the response
    for an otherwise-identical prompt. Returns a hex digest used as the filename.
    """
    h = hashlib.sha256()
    h.update(prompt.hash().encode())
    h.update(b"\x00")
    h.update(model.encode())
    h.update(b"\x00")
    h.update(mode.encode())
    h.update(b"\x00")
    # ``sort_keys`` makes the schema serialization order-independent.
    h.update(json.dumps(schema or {}, sort_keys=True).encode())
    h.update(b"\x00")
    h.update("\x1f".join(candidates).encode())
    h.update(b"\x00")
    h.update(f"{temperature:.4f}".encode())
    return h.hexdigest()


class ResponseCache:
    """A directory-backed cache of :class:`Completion` objects.

    Pass ``enabled=False`` (or ``dir=None``) for a no-op cache — every lookup
    misses and nothing is written, which is what the fast unit tests that *don't*
    exercise caching want.
    """

    def __init__(self, dir: str | Path | None, *, enabled: bool = True) -> None:
        self._dir = Path(dir) if dir is not None else None
        self._enabled = enabled and self._dir is not None
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        """Whether this cache reads/writes the disk."""
        return self._enabled

    def _path(self, key: str) -> Path:
        assert self._dir is not None  # guarded by ``_enabled``
        return self._dir / f"{key}.json"

    def get(self, key: str) -> Completion | None:
        """Return the cached completion for ``key`` (with ``cache_hit=True``) or ``None``."""
        if not self._enabled:
            self.misses += 1
            return None
        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            # A corrupt entry is treated as a miss; it will be overwritten on put.
            self.misses += 1
            return None
        self.hits += 1
        completion = Completion.from_dict(data)
        # Mark the value as cache-sourced regardless of what was serialized.
        return Completion(
            text=completion.text,
            usage=completion.usage,
            model=completion.model,
            structured=completion.structured,
            references_used=completion.references_used,
            cache_hit=True,
        )

    def put(self, key: str, completion: Completion) -> None:
        """Store ``completion`` under ``key`` (no-op when disabled)."""
        if not self._enabled:
            return
        assert self._dir is not None
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(completion.to_dict(), sort_keys=True, indent=2))
        tmp.replace(path)  # atomic on POSIX so a reader never sees a half-written file
