"""The run skeleton: materialize an (M1: empty) run directory (ARCHITECTURE.md §6).

This is the orchestration glue for a run. M1 wires only the endpoints — Layer A
(world building), Layer B (event simulation) and Layer C (producers) arrive in
later milestones — so :func:`execute_run` currently performs only **Layer D, the
assembly step**: it lays out ``<output_dir>/<run-id>/``, snapshots the validated
config, writes the ``manifest.json`` index, and creates the (empty) reference
directory hierarchy the later layers fill in.

The run id is a pure function of ``(config, seed)`` — a short content digest of
the config (excluding the *destination* ``output_dir``, which is about where the
run lands, not what it is) — so re-running the same config reproduces the same
run id and the same structural manifest (PLAN.md M1 acceptance).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from enterprise_sim import __version__
from enterprise_sim.assembly.manifest import SCHEMA_VERSION, Manifest
from enterprise_sim.core.config import RunConfig

# Subdirectories every run lays down; later layers populate them. M1 leaves them
# empty (organization/ is the deliverable named in the bead; kg/ and validation/
# are reserved for the gold-KG export and consistency validator of later
# milestones — created here so the layout is stable from day one).
_ORGANIZATION_DIR = "organization"
_KG_DIR = "kg"
_VALIDATION_DIR = "validation"
_CONFIG_SNAPSHOT = "config.snapshot.json"
_MANIFEST = "manifest.json"


@dataclass(frozen=True, slots=True)
class RunResult:
    """The outcome of :func:`execute_run`: where the run landed and its manifest."""

    run_id: str
    run_dir: Path
    manifest: Manifest


def _slugify(name: str) -> str:
    """Return a filesystem-safe, lowercase slug of ``name`` (empty → ``run``)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "run"


def _canonical_config(config: RunConfig) -> dict[str, object]:
    """Canonical JSON-mode dump of the config used for the snapshot."""
    return config.model_dump(mode="json")


def compute_config_digest(config: RunConfig) -> str:
    """Return a stable ``sha256`` hex digest identifying the config's *content*.

    The destination ``output_dir`` is excluded: it controls where a run is
    written, not what the run is, so two runs of the same config to different
    directories share an identity (and thus a structural manifest).
    """
    payload = _canonical_config(config)
    payload.pop("output_dir", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_run_id(config: RunConfig, *, digest: str | None = None) -> str:
    """Return the deterministic run id ``<company-slug>-<digest12>`` for ``config``."""
    digest = digest if digest is not None else compute_config_digest(config)
    return f"{_slugify(config.company.name)}-{digest[:12]}"


def build_manifest(config: RunConfig, *, generated_at: str | None = None) -> Manifest:
    """Build the :class:`Manifest` for ``config`` (no filesystem side effects)."""
    digest = compute_config_digest(config)
    stamp = generated_at if generated_at is not None else datetime.now(UTC).isoformat()
    return Manifest(
        schema_version=SCHEMA_VERSION,
        run_id=compute_run_id(config, digest=digest),
        tool_version=__version__,
        seed=config.seed,
        config_digest=digest,
        company={
            "name": config.company.name,
            "vertical": config.company.vertical,
            "size": config.company.size.value,
        },
        window={
            "start": config.simulation.period_start.isoformat(),
            "end": config.simulation.period_end.isoformat(),
        },
        # M1 produces an empty world; later layers raise these counts.
        counts={"nodes": 0, "edges": 0, "events": 0},
        outputs={
            "config_snapshot": _CONFIG_SNAPSHOT,
            "organization": f"{_ORGANIZATION_DIR}/",
            "kg": f"{_KG_DIR}/",
            "validation": f"{_VALIDATION_DIR}/",
        },
        generated_at=stamp,
    )


def execute_run(config: RunConfig, *, generated_at: str | None = None) -> RunResult:
    """Materialize the run directory for ``config`` and return its :class:`RunResult`.

    Creates ``<config.output_dir>/<run-id>/`` containing ``manifest.json``, the
    config snapshot, and the (empty, in M1) reference directory hierarchy. The
    operation is idempotent: re-running the same config rewrites the same
    directory in place.

    Args:
        config: The validated run configuration.
        generated_at: Optional ISO-8601 override for the manifest's volatile
            wall-clock stamp; used by tests for byte-stable output.
    """
    manifest = build_manifest(config, generated_at=generated_at)
    run_dir = config.output_dir / manifest.run_id

    for name in (_ORGANIZATION_DIR, _KG_DIR, _VALIDATION_DIR):
        (run_dir / name).mkdir(parents=True, exist_ok=True)

    snapshot = json.dumps(_canonical_config(config), sort_keys=True, indent=2)
    (run_dir / _CONFIG_SNAPSHOT).write_text(snapshot + "\n", encoding="utf-8")

    manifest_json = json.dumps(manifest.to_dict(), sort_keys=True, indent=2)
    (run_dir / _MANIFEST).write_text(manifest_json + "\n", encoding="utf-8")

    return RunResult(run_id=manifest.run_id, run_dir=run_dir, manifest=manifest)
