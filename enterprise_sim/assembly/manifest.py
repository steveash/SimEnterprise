"""The run ``manifest.json`` — the self-describing index of a run (ARCHITECTURE.md §6/§11.4).

Every run materializes a ``manifest.json`` at the root of its output directory.
The manifest is the entry point a downstream consumer reads first: it names the
run, records the seed and a content digest of the config (so a run is traceable
back to its exact inputs), summarises the knowledge-graph size, and lists the
relative paths of the other outputs (config snapshot, ``organization/``, …).

Determinism is *structural*, not byte-identical (D10): the manifest carries one
deliberately volatile field, :attr:`Manifest.generated_at` (a wall-clock stamp),
and is otherwise a pure function of ``(config, seed)``. :func:`structural_view`
strips the volatile field so two runs with the same seed can be compared for the
reproducibility guarantee. All recorded paths are *relative to the run directory*
so the manifest is identical regardless of where the run is written.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Bumped when the manifest shape changes incompatibly. Consumers should check it.
SCHEMA_VERSION = "1.0"

# Fields that intentionally vary between otherwise-identical runs (wall clock,
# host, …). Excluded from the structural view used for reproducibility checks.
VOLATILE_FIELDS: tuple[str, ...] = ("generated_at",)


@dataclass(frozen=True, slots=True)
class Manifest:
    """The structured contents of ``manifest.json``.

    Frozen so a built manifest cannot drift before it is written. Round-trips
    through :meth:`to_dict` / :meth:`from_dict`.
    """

    schema_version: str
    run_id: str
    tool_version: str
    seed: int
    config_digest: str
    company: dict[str, Any]
    window: dict[str, str]
    counts: dict[str, int]
    outputs: dict[str, str]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping (the contents of ``manifest.json``)."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "tool_version": self.tool_version,
            "seed": self.seed,
            "config_digest": self.config_digest,
            "company": dict(self.company),
            "window": dict(self.window),
            "counts": dict(self.counts),
            "outputs": dict(self.outputs),
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Manifest:
        """Reconstruct a :class:`Manifest` from :meth:`to_dict` output."""
        return cls(
            schema_version=data["schema_version"],
            run_id=data["run_id"],
            tool_version=data["tool_version"],
            seed=data["seed"],
            config_digest=data["config_digest"],
            company=dict(data["company"]),
            window=dict(data["window"]),
            counts=dict(data["counts"]),
            outputs=dict(data["outputs"]),
            generated_at=data["generated_at"],
        )


def structural_view(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a manifest mapping with the volatile fields removed.

    This is the canonical form for the reproducibility guarantee: two runs with
    the same seed produce identical structural views (PLAN.md M1 acceptance).
    """
    return {k: v for k, v in manifest.items() if k not in VOLATILE_FIELDS}
