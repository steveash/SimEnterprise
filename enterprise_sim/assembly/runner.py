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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path

from enterprise_sim import __version__
from enterprise_sim.assembly.corpus import CorpusResult, RenderEstimate, build_corpus
from enterprise_sim.assembly.manifest import SCHEMA_VERSION, Manifest
from enterprise_sim.assembly.validation import summarize_issue_rows, validate_consistency
from enterprise_sim.core.config import RunConfig
from enterprise_sim.core.llm import LLMClient, LLMConfig, build_client
from enterprise_sim.core.sim.scheduler import ValidationIssue
from enterprise_sim.core.world import World
from enterprise_sim.producers.artifact import (
    issue_records,
    mention_records,
    provenance_records,
)
from enterprise_sim.world_builders import build_world, write_organization

# Subdirectories every run lays down. ``organization/`` holds the Layer-A
# markdown reference data and ``kg/`` the structural KG export (nodes/edges);
# ``validation/`` holds the consistency validator's ``issues.jsonl`` (§11.4/D17).
_ORGANIZATION_DIR = "organization"
_KG_DIR = "kg"
_VALIDATION_DIR = "validation"
_ARTIFACTS_DIR = "artifacts"
_KG_NODES = "nodes.jsonl"
_KG_EDGES = "edges.jsonl"
_KG_EVENTS = "events.jsonl"
_KG_MENTIONS = "mentions.jsonl"
_KG_PROVENANCE = "provenance.jsonl"
_VALIDATION_ISSUES = "issues.jsonl"
_CONFIG_SNAPSHOT = "config.snapshot.json"
_MANIFEST = "manifest.json"


@dataclass(frozen=True, slots=True)
class RunResult:
    """The outcome of :func:`execute_run`: where the run landed and its manifest.

    ``world`` is the knowledge graph after every layer ran (Layer A structure plus
    the Layer B events/calendars and Layer C artifact nodes/edges), so a caller can
    introspect or re-query the run without re-reading it from disk. ``corpus`` is
    the rendered markdown corpus + combined event journal.
    """

    run_id: str
    run_dir: Path
    manifest: Manifest
    world: World
    corpus: CorpusResult


# The default render backend. A run is network-free and reproducible out of the
# box; selecting a real provider (api/bedrock/cli) is an explicit caller concern
# (pass a configured ``client=`` to :func:`execute_run`).
_DEFAULT_BACKEND = "fake"


def llm_config_for(config: RunConfig, *, backend: str = _DEFAULT_BACKEND) -> LLMConfig:
    """Project a :class:`RunConfig` onto the :class:`LLMConfig` the client needs.

    Carries the run's model and ``scale`` controls (concurrency, cost ceiling, and
    the on-disk response-cache settings) onto the client so the dry-run gate and
    bounded-concurrency render honor what the config asked for. The backend
    defaults to the deterministic ``fake`` so a default run never touches the
    network (ARCHITECTURE.md §7/§16.4).
    """
    scale = config.scale
    return LLMConfig(
        backend=backend,
        model=config.model.name,
        max_concurrency=scale.max_concurrency,
        cost_ceiling_usd=scale.cost_ceiling_usd,
        cache_dir=scale.cache_dir,
        cache_enabled=scale.cache_enabled,
    )


def _client_for(config: RunConfig, client: LLMClient | None) -> LLMClient:
    """Return ``client`` if given, else a ``fake`` client wired from ``config``."""
    if client is not None:
        return client
    return build_client(llm_config_for(config))


def _slugify(name: str) -> str:
    """Return a filesystem-safe, lowercase slug of ``name`` (empty → ``run``)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "run"


def _canonical_config(config: RunConfig) -> dict[str, object]:
    """Canonical JSON-mode dump of the config used for the snapshot."""
    return config.model_dump(mode="json")


def compute_config_digest(config: RunConfig) -> str:
    """Return a stable ``sha256`` hex digest identifying the config's *content*.

    Operational knobs that control *where/how fast* a run is produced — not *what*
    it is — are excluded so they never change a run's identity:

    * ``output_dir`` — where the run lands.
    * ``scale`` — concurrency, cost ceiling, and cache settings. The corpus is
      identical regardless of these (concurrency is bounded but deterministic,
      D26), so two runs that differ only in scale share an id and manifest.
    """
    payload = _canonical_config(config)
    payload.pop("output_dir", None)
    payload.pop("scale", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_run_id(config: RunConfig, *, digest: str | None = None) -> str:
    """Return the deterministic run id ``<company-slug>-<digest12>`` for ``config``."""
    digest = digest if digest is not None else compute_config_digest(config)
    return f"{_slugify(config.company.name)}-{digest[:12]}"


def build_manifest(
    config: RunConfig,
    *,
    counts: dict[str, int] | None = None,
    validation: dict[str, object] | None = None,
    generated_at: str | None = None,
) -> Manifest:
    """Build the :class:`Manifest` for ``config`` (no filesystem side effects).

    ``counts`` records the size of the built knowledge graph (nodes/edges/events).
    It defaults to zeros so the manifest can be built before a world exists;
    :func:`execute_run` passes the real Layer-A counts. ``validation`` is the
    consistency-validator summary of ``validation/issues.jsonl`` (total + per-kind
    tally); it defaults to an empty/clean summary.
    """
    digest = compute_config_digest(config)
    stamp = generated_at if generated_at is not None else datetime.now(UTC).isoformat()
    counts = counts if counts is not None else {"nodes": 0, "edges": 0, "events": 0}
    validation = validation if validation is not None else {"total": 0, "by_kind": {}}
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
        counts=dict(counts),
        validation=dict(validation),
        outputs={
            "config_snapshot": _CONFIG_SNAPSHOT,
            "organization": f"{_ORGANIZATION_DIR}/",
            "artifacts": f"{_ARTIFACTS_DIR}/",
            "kg": f"{_KG_DIR}/",
            "events": f"{_KG_DIR}/{_KG_EVENTS}",
            "mentions": f"{_KG_DIR}/{_KG_MENTIONS}",
            "provenance": f"{_KG_DIR}/{_KG_PROVENANCE}",
            "validation": f"{_VALIDATION_DIR}/",
        },
        generated_at=stamp,
    )


def execute_run(
    config: RunConfig,
    *,
    generated_at: str | None = None,
    client: LLMClient | None = None,
) -> RunResult:
    """Materialize the full run directory for ``config`` and return its result.

    Runs every layer end to end: Layer A (the deterministic world), Layer B (the
    scheduler — scenario events + per-person calendars), Layer C (the producers —
    the grounded markdown corpus), and Layer D (assembly). It creates
    ``<config.output_dir>/<run-id>/`` containing ``manifest.json``, the config
    snapshot, the ``organization/`` reference data, the ``artifacts/`` markdown
    corpus, and the ``kg/`` export (structural ``nodes``/``edges`` plus the
    ``events``/``mentions``/``provenance`` side files), with any soft findings under
    ``validation/``. The operation is idempotent: re-running the same config with a
    deterministic backend rewrites the same directory in place.

    Args:
        config: The validated run configuration.
        generated_at: Optional ISO-8601 override for the manifest's volatile
            wall-clock stamp; used by tests for byte-stable output.
        client: LLM client the producers render against; defaults to a
            deterministic, network-free ``fake`` client wired from ``config``'s
            ``scale`` controls (concurrency, cost ceiling, cache) so a run is
            reproducible and free out of the box.
    """
    client = _client_for(config, client)

    world = build_world(config)
    corpus = build_corpus(world, config, client)

    # The consistency validator (D17): soft cross-checks over the built run. Its
    # findings join the scheduler's and producers' issues in one issues.jsonl,
    # and the combined tally is summarised into the manifest. Report-and-continue
    # — a finding never fails the run.
    consistency = validate_consistency(world, corpus.journal, window=_simulation_window(config))
    issue_rows = [
        *_scheduler_issue_records(corpus.issues),
        *issue_records(corpus.artifacts),
        *(issue.to_dict() for issue in consistency),
    ]

    counts = {
        "nodes": world.node_count,
        "edges": world.edge_count,
        "events": len(corpus.journal),
    }
    manifest = build_manifest(
        config,
        counts=counts,
        validation=summarize_issue_rows(issue_rows),
        generated_at=generated_at,
    )
    run_dir = config.output_dir / manifest.run_id

    for name in (_ORGANIZATION_DIR, _KG_DIR, _VALIDATION_DIR, _ARTIFACTS_DIR):
        (run_dir / name).mkdir(parents=True, exist_ok=True)

    snapshot = json.dumps(_canonical_config(config), sort_keys=True, indent=2)
    (run_dir / _CONFIG_SNAPSHOT).write_text(snapshot + "\n", encoding="utf-8")

    write_organization(world, run_dir / _ORGANIZATION_DIR)
    _write_artifacts(run_dir, corpus)
    _write_kg(world, run_dir / _KG_DIR)
    _write_corpus_side_files(run_dir, corpus)
    _write_jsonl(run_dir / _VALIDATION_DIR / _VALIDATION_ISSUES, issue_rows)

    manifest_json = json.dumps(manifest.to_dict(), sort_keys=True, indent=2)
    (run_dir / _MANIFEST).write_text(manifest_json + "\n", encoding="utf-8")

    return RunResult(
        run_id=manifest.run_id,
        run_dir=run_dir,
        manifest=manifest,
        world=world,
        corpus=corpus,
    )


def estimate_run(
    config: RunConfig,
    *,
    client: LLMClient | None = None,
) -> RenderEstimate:
    """Dry-run a config: build the world, schedule, and price the render (D13).

    Runs Layer A + Layer B (both cheap and network-free) to learn how many
    artifacts the render would produce, then returns the priced dry-run estimate
    *without* making a single model call or writing anything to disk. If the
    config sets a cost ceiling the estimate breaches, this raises
    :class:`~enterprise_sim.core.llm.CostCeilingExceeded` — the same up-front gate
    a full :func:`execute_run` applies before it renders.
    """
    client = _client_for(config, client)
    world = build_world(config)
    corpus = build_corpus(world, config, client, dry_run=True)
    assert corpus.estimate is not None  # build_corpus always estimates
    return corpus.estimate


def _write_artifacts(run_dir: Path, corpus: CorpusResult) -> None:
    """Write every rendered file to its run-relative, scenario-clustered path.

    Binary producers (e.g. ``pptx``) carry their bytes in ``binary_body``; those are
    written verbatim. Text producers (e.g. ``markdown``) carry their UTF-8 body.
    """
    for artifact in corpus.artifacts:
        path = run_dir / artifact.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if artifact.binary_body is not None:
            path.write_bytes(artifact.binary_body)
        else:
            path.write_text(artifact.body, encoding="utf-8")


def _simulation_window(config: RunConfig) -> tuple[datetime, datetime]:
    """The inclusive ``(start, end)`` sim-time window the validator checks against.

    Whole-day inclusive of ``[period_start, period_end]`` so a stamp is judged
    out-of-window by its *date*, never by working-hour boundaries within a day.
    """
    start = datetime.combine(config.simulation.period_start, time.min)
    end = datetime.combine(config.simulation.period_end, time.max)
    return (start, end)


def _write_corpus_side_files(run_dir: Path, corpus: CorpusResult) -> None:
    """Write the Layer B/C KG side files: events, mentions, and provenance (§11.4).

    The ``validation/issues.jsonl`` index is written by :func:`execute_run`, which
    merges these scheduler/producer findings with the consistency validator's.
    """
    kg_dir = run_dir / _KG_DIR
    (kg_dir / _KG_EVENTS).write_text(corpus.journal.dumps(), encoding="utf-8")
    _write_jsonl(kg_dir / _KG_MENTIONS, mention_records(corpus.artifacts))
    _write_jsonl(kg_dir / _KG_PROVENANCE, provenance_records(corpus.artifacts))


def _scheduler_issue_records(
    issues: Sequence[ValidationIssue],
) -> list[dict[str, object]]:
    """Serialize scheduler validation issues into uniform ``issues.jsonl`` rows."""
    rows: list[dict[str, object]] = []
    for issue in issues:
        details: dict[str, object] = {}
        if issue.at is not None:
            details["at"] = issue.at.isoformat()
        rows.append(
            {
                "kind": issue.code,
                "message": issue.message,
                "where": issue.subject or "",
                "details": details,
            }
        )
    return rows


def _write_kg(world: World, kg_dir: Path) -> None:
    """Export the KG as deterministic JSONL (one node/edge per line, sorted by id)."""
    payload = world.to_dict()
    _write_jsonl(kg_dir / _KG_NODES, payload["nodes"])
    _write_jsonl(kg_dir / _KG_EDGES, payload["edges"])


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    lines = [json.dumps(record, sort_keys=True, separators=(",", ":")) for record in records]
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
