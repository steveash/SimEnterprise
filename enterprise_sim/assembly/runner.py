"""The run pipeline: build a world directory end to end (ARCHITECTURE.md §6).

This is the orchestration glue for a build. :func:`execute_run` runs every layer
— Layer A (world building), Layer B (event simulation), Layer C (producers) and
Layer D (assembly) — and materializes the result as a **world directory**: one
folder whose ``.world`` file (:mod:`enterprise_sim.worlds`) carries the identity,
the effective config, the KG counts, the validation summary and the job ledger,
next to ``organization/``, ``artifacts/``, ``kg/`` and ``validation/``.

The world id is a pure function of ``(config, seed)`` — a short content digest
of the config (excluding the *destination* ``worlds_root``, which is about where
the world lands, not what it is) — so building the same config reproduces the
same id and the same structural ``.world`` (PLAN.md M1 acceptance). The id is
minted once: an extend re-renders into the *same* world directory with the new
config and the ``.world`` keeps its ``world_id`` (``docs/EXPLORER_RUNS.md`` §3.5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path

from enterprise_sim import __version__
from enterprise_sim.assembly.corpus import CorpusResult, RenderEstimate, build_corpus
from enterprise_sim.assembly.validation import summarize_issue_rows, validate_consistency
from enterprise_sim.core.config import WorldConfig
from enterprise_sim.core.llm import LLMClient, LLMConfig, build_client
from enterprise_sim.core.sim.scheduler import ValidationIssue
from enterprise_sim.core.world import World
from enterprise_sim.exporters import EXPORTERS, KgBundle, write_jsonl
from enterprise_sim.jobs.control import RunControl
from enterprise_sim.jobs.progress import NullSink, ProgressEvent, ProgressSink
from enterprise_sim.producers.artifact import issue_records
from enterprise_sim.world_builders import build_world, write_organization
from enterprise_sim.worlds import (
    CACHE_DIR,
    WORLD_FILE,
    WorldFile,
    cache_dir,
    is_world_dir,
    slugify,
    update_world,
)

# Subdirectories every world lays down. ``organization/`` holds the Layer-A
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
_KG_ALIASES = "aliases.jsonl"
_KG_SCHEMA = "schema.json"
_KG_GRAPH = "graph.json"
_KG_NEO4J_DIR = "neo4j"
_VALIDATION_ISSUES = "issues.jsonl"

#: The relative paths a built world exposes, recorded in ``.world``'s ``outputs``.
WORLD_OUTPUTS: dict[str, str] = {
    "world": WORLD_FILE,
    "cache": f"{CACHE_DIR}/",
    "organization": f"{_ORGANIZATION_DIR}/",
    "artifacts": f"{_ARTIFACTS_DIR}/",
    "kg": f"{_KG_DIR}/",
    "nodes": f"{_KG_DIR}/{_KG_NODES}",
    "edges": f"{_KG_DIR}/{_KG_EDGES}",
    "events": f"{_KG_DIR}/{_KG_EVENTS}",
    "mentions": f"{_KG_DIR}/{_KG_MENTIONS}",
    "provenance": f"{_KG_DIR}/{_KG_PROVENANCE}",
    "aliases": f"{_KG_DIR}/{_KG_ALIASES}",
    "schema": f"{_KG_DIR}/{_KG_SCHEMA}",
    "graph": f"{_KG_DIR}/{_KG_GRAPH}",
    "neo4j": f"{_KG_DIR}/{_KG_NEO4J_DIR}/",
    "validation": f"{_VALIDATION_DIR}/",
}


@dataclass(frozen=True, slots=True)
class RunResult:
    """The outcome of :func:`execute_run`: where the world landed and its ``.world``.

    ``world`` is the knowledge graph after every layer ran (Layer A structure plus
    the Layer B events/calendars and Layer C artifact nodes/edges), so a caller can
    introspect or re-query the build without re-reading it from disk. ``corpus`` is
    the rendered markdown corpus + combined event journal. ``world_file`` is the
    ``.world`` as written at the end of the build.
    """

    world_id: str
    world_dir: Path
    world_file: WorldFile
    world: World
    corpus: CorpusResult


# The default render backend. A build is network-free and reproducible out of the
# box; reaching a real provider (api/bedrock/cli) always takes an explicit
# opt-in, either ``live=True`` — which honors the config's ``[model] backend``
# and is what ``enterprise-sim world create --live`` passes — or a configured
# ``client=``.
_DEFAULT_BACKEND = "fake"


def llm_config_for(
    config: WorldConfig, *, backend: str = _DEFAULT_BACKEND, world_dir: Path | None = None
) -> LLMConfig:
    """Project a :class:`WorldConfig` onto the :class:`LLMConfig` the client needs.

    Carries the model and ``scale`` controls (concurrency, cost ceiling, and the
    on-disk response-cache settings) onto the client so the dry-run gate and
    bounded-concurrency render honor what the config asked for. The backend
    defaults to the deterministic ``fake`` so a default build never touches the
    network (ARCHITECTURE.md §7/§16.4); the ``live=True`` path in
    :func:`_client_for` is what passes a real one.

    The response cache (D31) lives **inside the world** by default —
    ``<world_dir>/.cache/llm`` — so a paused or finished world can be zipped,
    moved and resumed with every rendered artifact still a free cache hit. A
    config's explicit ``scale.cache_dir`` still wins; with neither (no world dir
    yet, e.g. a dry-run estimate) the cache is in-memory only.
    """
    scale = config.scale
    resolved_cache = scale.cache_dir
    if resolved_cache is None and world_dir is not None:
        resolved_cache = str(cache_dir(world_dir))
    return LLMConfig(
        backend=backend,
        model=config.model.name,
        max_concurrency=scale.max_concurrency,
        cost_ceiling_usd=scale.cost_ceiling_usd,
        cache_dir=resolved_cache,
        cache_enabled=scale.cache_enabled,
    )


def effective_backend(config: WorldConfig, client: LLMClient | None, *, live: bool) -> str:
    """The backend a build will actually render with.

    ``client``'s if one is supplied (it overrides both ``live`` and the config's
    ``[model] backend``), else the config's provider under ``live``, else the
    deterministic default.
    """
    if client is not None:
        return client.config.backend
    return render_backend_for(config, live=live)


def _client_for(
    config: WorldConfig,
    client: LLMClient | None,
    *,
    live: bool = False,
    world_dir: Path | None = None,
) -> LLMClient:
    """Return ``client`` if given, else one wired from ``config`` (see :func:`llm_config_for`)."""
    if client is not None:
        return client
    backend = effective_backend(config, None, live=live)
    return build_client(llm_config_for(config, backend=backend, world_dir=world_dir))


def canonical_config(config: WorldConfig) -> dict[str, object]:
    """Canonical JSON-mode dump of the config recorded in ``.world``.

    ``worlds_root`` is left out: it is the *destination* the world was written to,
    and a ``.world`` must stay valid after the folder is zipped and moved — a
    world knows where it lives by being read from there.
    """
    payload = config.model_dump(mode="json")
    payload.pop("worlds_root", None)
    return payload


def compute_config_digest(config: WorldConfig, *, backend: str | None = None) -> str:
    """Return a stable ``sha256`` hex digest identifying the config's *content*.

    Operational knobs that control *where/how fast* a world is produced — not
    *what* it is — are excluded so they never change a world's identity:

    * ``worlds_root`` — where the world lands.
    * ``scale`` — concurrency, cost ceiling, and cache settings. The corpus is
      identical regardless of these (concurrency is bounded but deterministic,
      D26), so two builds that differ only in scale share an id.
    * ``plugins`` — extra external plugin directories to discover
      (EXPLORER_TEMPLATES.md §2.1). Like ``scale``, this is about *how* the build
      finds its registrations, not what the world is: two configs that resolve to
      the same registered archetype/playbook should share an id whether that
      registration came from the package or a template directory.
    * ``data`` — the structured-data plan. Data products are built *on* the
      corpus + KG and never change them, so adding a scenario to a world must
      not change its id.

    ``backend`` is the *effective* render backend, which is not a config field:
    the same config renders placeholder prose by default and model-authored prose
    under ``--live``. Those are different corpora, so they must not share an id
    and overwrite each other. The default backend contributes nothing to the
    digest, which keeps every existing id (notably the pinned golden world)
    byte-stable; only a real provider adds to it.
    """
    payload = canonical_config(config)
    payload.pop("worlds_root", None)
    payload.pop("scale", None)
    payload.pop("plugins", None)
    payload.pop("data", None)
    _drop_default_sections(payload)
    if backend is not None and backend != _DEFAULT_BACKEND:
        payload["render_backend"] = backend
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


#: Fields added to a diversity section after the section first shipped. Every
#: new field of [facts], [messiness], [org], [data.messiness] or a later section
#: must be listed here, or adding it changes the id of every world that uses the
#: section (tests/test_world_ids.py pins example world ids).
_LATE_FIELDS: dict[str, tuple[str, ...]] = {
    "facts": ("entity_ids", "retraction_rate", "horizon_days"),
    "org": ("project_names", "horizon_days", "homonyms"),
    "code": ("sessions_per_project",),
}


def _drop_default_sections(payload: dict[str, object]) -> None:
    """Remove the diversity knobs left at their defaults from a digest payload.

    ``[facts]``, ``[messiness]``, ``[org]`` and ``[model] offline_backend`` were
    added after worlds were pinned by id; at their defaults they change nothing,
    so they contribute nothing and every existing id stays byte-stable. The
    effective offline backend reaches the digest as ``render_backend``.
    """
    from enterprise_sim.core.config.diversity import (
        ChatConfig,
        CodeConfig,
        FactsConfig,
        FormatsConfig,
        MessinessConfig,
        OrgConfig,
        PrivacyConfig,
    )
    from enterprise_sim.core.config.isolation import IsolationConfig

    defaults = {
        "isolation": IsolationConfig().model_dump(mode="json"),
        "privacy": PrivacyConfig().model_dump(mode="json"),
        "formats": FormatsConfig().model_dump(mode="json"),
        "chat": ChatConfig().model_dump(mode="json"),
        "code": CodeConfig().model_dump(mode="json"),
        "facts": FactsConfig().model_dump(mode="json"),
        "messiness": MessinessConfig().model_dump(mode="json"),
        "org": OrgConfig().model_dump(mode="json"),
    }
    for key, default in defaults.items():
        section = payload.get(key)
        if isinstance(section, dict):
            # A field added to a section after its first release adds nothing
            # at its default, so worlds pinned before it keep their ids.
            for name in _LATE_FIELDS.get(key, ()):
                if name in section and section[name] == default.get(name):
                    section.pop(name)
            original = {k: v for k, v in default.items() if k not in _LATE_FIELDS.get(key, ())}
            if section == original:
                payload.pop(key, None)
    model = payload.get("model")
    if isinstance(model, dict):
        model.pop("offline_backend", None)


def render_backend_for(config: WorldConfig, *, live: bool) -> str:
    """The backend a build renders with: the provider under ``live``, else the offline one."""
    return config.model.backend.value if live else config.model.offline_backend.value


def compute_world_id(
    config: WorldConfig, *, digest: str | None = None, backend: str | None = None
) -> str:
    """Return the deterministic world id ``<company-slug>-<digest12>`` for ``config``.

    ``backend`` is the effective render backend; see :func:`compute_config_digest`
    for why a live render gets an id of its own. This is the id a world is
    *minted* with; later extends keep it (see :func:`execute_run`).
    """
    digest = digest if digest is not None else compute_config_digest(config, backend=backend)
    return f"{slugify(config.company.name)}-{digest[:12]}"


def same_config(stored: Mapping[str, object], config: WorldConfig) -> bool:
    """Whether ``stored`` (a ``.world``/``config.json`` serialization) describes ``config``.

    True when it validates to a config with the same canonical form: a serialization
    written by an older SimEnterprise, which lacks fields added since at their
    defaults, still describes the same world, so it is kept as it was written.
    """
    try:
        loaded = WorldConfig.model_validate({**stored, "worlds_root": str(config.worlds_root)})
    except ValueError:
        return False
    return canonical_config(loaded) == canonical_config(config)


def build_world_file(
    config: WorldConfig,
    *,
    counts: dict[str, int] | None = None,
    validation: dict[str, object] | None = None,
    generated_at: str | None = None,
    backend: str = _DEFAULT_BACKEND,
    live: bool = False,
    stored_config: Mapping[str, object] | None = None,
) -> WorldFile:
    """Build the :class:`WorldFile` for a fresh build of ``config`` (no filesystem side effects).

    ``stored_config`` is the serialization to record instead of today's canonical
    form, for a world whose config comes from an older export (a held-out fork of a
    release keeps its base's ``config.json`` byte for byte but for ``seed`` and
    ``company.name``). It must describe exactly ``config`` (:func:`same_config`).

    ``counts`` records the size of the built knowledge graph (nodes/edges/events).
    It defaults to zeros so the file can be built before a world exists;
    :func:`execute_run` passes the real Layer-A counts. ``validation`` is the
    consistency-validator summary of ``validation/issues.jsonl`` (total + per-kind
    tally); it defaults to an empty/clean summary.
    """
    digest = compute_config_digest(config, backend=backend)
    if stored_config is not None and not same_config(stored_config, config):
        raise ValueError("stored_config does not describe the config being built")
    world = WorldFile.new(
        world_id=compute_world_id(config, digest=digest),
        company={
            "name": config.company.name,
            "vertical": config.company.vertical,
            "size": config.company.size.value,
            "description": config.company.description,
        },
        config=dict(stored_config) if stored_config is not None else canonical_config(config),
        config_digest=digest,
        seed=config.seed,
        window={
            "start": config.simulation.period_start.isoformat(),
            "end": config.simulation.period_end.isoformat(),
        },
        tool_version=__version__,
        render_backend=backend,
        live=live,
        at=generated_at if generated_at is not None else datetime.now(UTC).isoformat(),
    )
    world.counts = dict(counts) if counts is not None else {"nodes": 0, "edges": 0, "events": 0}
    world.validation = dict(validation) if validation is not None else {"total": 0, "by_kind": {}}
    world.outputs = dict(WORLD_OUTPUTS)
    return world


def execute_run(
    config: WorldConfig,
    *,
    generated_at: str | None = None,
    client: LLMClient | None = None,
    live: bool = False,
    progress: ProgressSink | None = None,
    control: RunControl | None = None,
    world_dir: Path | None = None,
) -> RunResult:
    """Build the full world directory for ``config`` and return its result.

    Runs every layer end to end: Layer A (the deterministic world), Layer B (the
    scheduler — scenario events + per-person calendars), Layer C (the producers —
    the grounded markdown corpus), and Layer D (assembly). It fills
    ``<world_dir>/`` with ``.world``, the ``organization/`` reference data, the
    ``artifacts/`` markdown corpus, and the ``kg/`` export (structural
    ``nodes``/``edges`` plus the ``events``/``mentions``/``provenance`` side
    files), with any soft findings under ``validation/``. The operation is
    idempotent: re-running the same config with a deterministic backend rewrites
    the same directory in place.

    Args:
        config: The validated configuration.
        generated_at: Optional ISO-8601 override for ``.world``'s volatile
            wall-clock stamps; used by tests for byte-stable output.
        client: LLM client the producers render against; defaults to a
            deterministic, network-free ``fake`` client wired from ``config``'s
            ``scale`` controls (concurrency, cost ceiling, cache) so a build is
            reproducible and free out of the box.
        live: Opt in to the real provider named by the config's ``[model]
            backend``. Ignored when an explicit ``client`` is passed. A live build
            makes network calls, costs money, and is not byte-reproducible.
        progress: Optional sink for :class:`~enterprise_sim.jobs.progress.ProgressEvent`\\ s
            (``docs/EXPLORER_RUNS.md`` §3.1); defaults to a no-op sink.
        control: Optional cooperative-pause control (§3.2), forwarded to
            :func:`~enterprise_sim.assembly.corpus.build_corpus`. ``None`` (the
            default) never pauses.
        world_dir: The world directory to build into. Defaults to
            ``<config.worlds_root>/<world-id>``. Pass an *existing* world's
            directory to extend it in place: the ``.world`` keeps its
            ``world_id`` and gains a history entry when the config digest changed.
    """
    progress = progress or NullSink()
    # The backend actually used — which is ``client``'s, not the config's, since a
    # caller-supplied client overrides both ``live`` and ``[model] backend``. The
    # world id keys off this so a live render never silently overwrites a
    # placeholder one (and vice versa).
    backend = effective_backend(config, client, live=live)
    digest = compute_config_digest(config, backend=backend)
    if world_dir is None:
        world_dir = config.worlds_root / compute_world_id(config, digest=digest)
    world_dir = Path(world_dir)
    for name in (_ORGANIZATION_DIR, _KG_DIR, _VALIDATION_DIR, _ARTIFACTS_DIR):
        (world_dir / name).mkdir(parents=True, exist_ok=True)
    client = _client_for(config, client, live=live, world_dir=world_dir)

    # Lay the ``.world`` down *before* building so the world is discoverable (and
    # listable with its config) while it renders. A bare build — no job ledger
    # above it — owns the job fields too and closes them out at the end.
    fresh = build_world_file(config, generated_at=generated_at, backend=backend, live=live)
    bare_build = not is_world_dir(world_dir)
    update_world(world_dir, lambda w: None, create=lambda: fresh, at=generated_at)

    progress.emit(ProgressEvent.now("phase", phase="world"))
    world = build_world(config)
    progress.emit(
        ProgressEvent.now(
            "world_built",
            nodes=world.node_count,
            edges=world.edge_count,
            departments=len(world.nodes_by_type("Department")),
            scenarios=len(
                [n for n in world.nodes_by_type("Initiative") if n.props.get("type") == "scenario"]
            ),
        )
    )
    corpus = build_corpus(world, config, client, progress=progress, control=control)

    progress.emit(ProgressEvent.now("phase", phase="assemble"))
    # The consistency validator (D17): soft cross-checks over the built world. Its
    # findings join the scheduler's and producers' issues in one issues.jsonl,
    # and the combined tally is summarised into ``.world``. Report-and-continue
    # — a finding never fails the build.
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
    validation = summarize_issue_rows(issue_rows)

    write_organization(world, world_dir / _ORGANIZATION_DIR)
    _write_artifacts(world_dir, corpus)
    _export_kg(world, corpus, world_dir / _KG_DIR)
    write_jsonl(world_dir / _VALIDATION_DIR / _VALIDATION_ISSUES, issue_rows)

    stamp = generated_at if generated_at is not None else datetime.now(UTC).isoformat()
    config_json = canonical_config(config)

    def finish(w: WorldFile) -> None:
        if w.config_digest != digest:
            # Built with a config the ``.world`` did not yet carry (an extend that
            # bypassed the job layer, or a backend switch): adopt it and say so.
            w.record_extend(
                config=config_json,
                config_digest=digest,
                changes={
                    "config_digest": [w.config_digest, digest],
                    "render_backend": [w.render_backend, backend],
                },
                at=stamp,
            )
        if not same_config(w.config, config):
            w.config = config_json  # an equivalent older serialization is kept verbatim
        w.render_backend = backend
        w.live = live if client.config.backend != _DEFAULT_BACKEND else False
        w.tool_version = __version__
        w.seed = config.seed
        w.window = {
            "start": config.simulation.period_start.isoformat(),
            "end": config.simulation.period_end.isoformat(),
        }
        w.counts = counts
        w.validation = validation
        w.outputs = dict(WORLD_OUTPUTS)
        if bare_build:
            w.job.status = "done"
            w.job.phase = "done"
            w.job.artifacts_total = len(corpus.artifacts)
            w.job.artifacts_done = len(corpus.artifacts)
            w.job.cost_usd_segment = client.cost.total_cost_usd
            w.job.cost_usd_total = client.cost.total_cost_usd
            w.job.usage_total = client.cost.total_usage.to_dict()
            if w.job.started_at is None:
                w.job.started_at = stamp
            w.job.finished_at = stamp

    world_file = update_world(world_dir, finish, at=generated_at)

    # At the pipeline level there is exactly one "segment" (a job's ledger is what
    # distinguishes cost-so-far from cost-across-segments, §3.3/§3.4), so the
    # segment and total costs coincide here.
    progress.emit(
        ProgressEvent.now(
            "done",
            world_id=world_file.world_id,
            world_dir=str(world_dir),
            artifacts=len(corpus.artifacts),
            events=len(corpus.journal),
            cost_usd_segment=client.cost.total_cost_usd,
            cost_usd_total=client.cost.total_cost_usd,
        )
    )
    progress.emit(ProgressEvent.now("phase", phase="done"))

    return RunResult(
        world_id=world_file.world_id,
        world_dir=world_dir,
        world_file=world_file,
        world=world,
        corpus=corpus,
    )


def estimate_run(
    config: WorldConfig,
    *,
    client: LLMClient | None = None,
    live: bool = False,
) -> RenderEstimate:
    """Dry-run a config: build the world, schedule, and price the render (D13).

    Runs Layer A + Layer B (both cheap and network-free) to learn how many
    artifacts the render would produce, then returns the priced dry-run estimate
    *without* making a single model call or writing anything to disk. If the
    config sets a cost ceiling the estimate breaches, this raises
    :class:`~enterprise_sim.core.llm.CostCeilingExceeded` — the same up-front gate
    a full :func:`execute_run` applies before it renders.

    ``live`` mirrors :func:`execute_run` for symmetry, but does **not** change the
    figure: the estimate prices ``[model] name`` against the pricing table, which
    is backend-independent. Passing it only asserts that the configured provider
    is constructible. No model call is made either way.
    """
    client = _client_for(config, client, live=live)
    world = build_world(config)
    corpus = build_corpus(world, config, client, dry_run=True)
    assert corpus.estimate is not None  # build_corpus always estimates
    return corpus.estimate


def _write_artifacts(world_dir: Path, corpus: CorpusResult) -> None:
    """Write every rendered file to its world-relative, scenario-clustered path.

    Binary producers (e.g. ``pptx``) carry their bytes in ``binary_body``; those are
    written verbatim. Text producers (e.g. ``markdown``) carry their UTF-8 body.
    """
    for artifact in corpus.artifacts:
        path = world_dir / artifact.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if artifact.binary_body is not None:
            path.write_bytes(artifact.binary_body)
        else:
            path.write_text(artifact.body, encoding="utf-8")


def _simulation_window(config: WorldConfig) -> tuple[datetime, datetime]:
    """The inclusive ``(start, end)`` sim-time window the validator checks against.

    Whole-day inclusive of ``[period_start, period_end]`` so a stamp is judged
    out-of-window by its *date*, never by working-hour boundaries within a day.
    """
    start = datetime.combine(config.simulation.period_start, time.min)
    end = datetime.combine(config.simulation.period_end, time.max)
    return (start, end)


def _export_kg(world: World, corpus: CorpusResult, kg_dir: Path) -> None:
    """Export the canonical Gold KG + the Neo4j adapter through the exporter registry.

    The canonical JSONL set (nodes/edges/events/provenance/mentions/aliases plus
    ``schema.json`` and ``graph.json``) and the Neo4j/Cypher bundle both derive from
    one :class:`~enterprise_sim.exporters.KgBundle`, so they cannot drift (§11.4-11.5).
    The combined ``validation/issues.jsonl`` index is written by :func:`execute_run`,
    which merges the scheduler/producer findings with the consistency validator's.
    """
    bundle = KgBundle.from_run(world, corpus)
    EXPORTERS.get("jsonl").export(bundle, kg_dir)
    EXPORTERS.get("neo4j").export(bundle, kg_dir / _KG_NEO4J_DIR)


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
