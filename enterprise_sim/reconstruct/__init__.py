"""Reconstruct→Reason: rebuild a KG from the corpus, then answer over it (epic esim-nc6).

This package is the inverse of the simulator. Where the sim projects a gold
knowledge graph *into* a grounded artifact corpus, the reconstruct pipeline reads
that corpus back *out* into a :class:`ReconstructedKG`, then measures how
faithfully the reconstruction recovers the gold graph and how well an agent can
reason over it.

This module (esim-nc6.1) is the foundation the pipeline builds on: the schema for
every artifact that flows through it (:class:`Chunk`, :class:`MentionSpan`,
:class:`CandidateTriple`, :class:`Provenance`) and the terminal
:class:`ReconstructedKG`, which writes ``nodes.jsonl`` / ``edges.jsonl`` in the
exact same schema as the gold KG — so the benchmark's graph engines load a
reconstruction unchanged. Later beads add hierarchical chunking (esim-nc6.2),
extraction, the graph-fidelity scorer (esim-nc6.6), and the ``enterprise-sim
reconstruct`` subcommands.
"""

from __future__ import annotations

from enterprise_sim.reconstruct.attribution import (
    ORACLE_NAME,
    RAG_NAME,
    RECONSTRUCTED_NAME,
    Attribution,
    FidelityContext,
    Gap,
    build_attribution,
    render_markdown,
)
from enterprise_sim.reconstruct.baseline import (
    BASELINES_DIR,
    CELL_SPECS,
    FAKE_CELLS,
    FIDELITY_METRIC_KEYS,
    KEYED_CELLS,
    BaselineCell,
    CellSpec,
    CompareResult,
    MetricDrift,
    build_cell,
    cell_path,
    compare,
    metrics_from_summary,
    regenerate_fake_metrics,
)
from enterprise_sim.reconstruct.build import (
    BuildConfig,
    PipelineExtraction,
    aggregate_relations,
    build_kg,
    extract_once,
    project_with_groundings,
    run_pipeline,
)
from enterprise_sim.reconstruct.chunk import (
    chunk_jira,
    chunk_markdown,
    chunk_run,
    iter_corpus_files,
)
from enterprise_sim.reconstruct.e2e import (
    E2EResult,
    run_e2e,
)
from enterprise_sim.reconstruct.extract import (
    EXTRACTION_SCHEMA,
    HAIKU_MODEL,
    Extraction,
    build_extraction_prompt,
    extract_chunk,
    extract_chunks,
    merge_envelopes,
    parse_extraction,
)
from enterprise_sim.reconstruct.fidelity import (
    PRF,
    EdgeFidelity,
    EntityResolution,
    FidelityReport,
    NodeFidelity,
    ProvenanceFidelity,
    align_reconstructed_ids,
    score_fidelity,
)
from enterprise_sim.reconstruct.model_sweep import (
    AnswerScorer,
    ModelPoint,
    ModelSweepReport,
    sweep_models,
)
from enterprise_sim.reconstruct.ontology import (
    NODE_GLOSSES,
    NODE_TYPES,
    RELATION_GLOSSES,
    RELATION_TYPES,
    describe_ontology,
)
from enterprise_sim.reconstruct.resolve import (
    ADJUDICATION_SCHEMA,
    CanonicalEntity,
    Resolution,
    ResolutionConfig,
    adjudicate_pair,
    build_adjudication_prompt,
    resolve_entities,
)
from enterprise_sim.reconstruct.scale import (
    MATRIX_RUNS,
    MATRIX_SEEDS,
    Aggregate,
    AggregateFidelity,
    RunFidelity,
    RunSpec,
    build_aggregate,
    default_run_specs,
    matrix_metrics,
    matrix_run_specs,
    reconstruct_and_score,
    run_scale,
)
from enterprise_sim.reconstruct.schema import (
    CandidateTriple,
    Chunk,
    MentionSpan,
    Provenance,
    ReconstructedKG,
)
from enterprise_sim.reconstruct.structural import structural_envelope
from enterprise_sim.reconstruct.sweep import (
    SweepPoint,
    SweepReport,
    sweep_thresholds,
)

__all__ = [
    "ADJUDICATION_SCHEMA",
    "BASELINES_DIR",
    "CELL_SPECS",
    "EXTRACTION_SCHEMA",
    "FAKE_CELLS",
    "FIDELITY_METRIC_KEYS",
    "HAIKU_MODEL",
    "KEYED_CELLS",
    "MATRIX_RUNS",
    "MATRIX_SEEDS",
    "NODE_GLOSSES",
    "NODE_TYPES",
    "ORACLE_NAME",
    "PRF",
    "RAG_NAME",
    "RECONSTRUCTED_NAME",
    "RELATION_GLOSSES",
    "RELATION_TYPES",
    "Aggregate",
    "AggregateFidelity",
    "AnswerScorer",
    "Attribution",
    "BaselineCell",
    "BuildConfig",
    "CandidateTriple",
    "CanonicalEntity",
    "CellSpec",
    "Chunk",
    "CompareResult",
    "E2EResult",
    "EdgeFidelity",
    "EntityResolution",
    "Extraction",
    "FidelityContext",
    "FidelityReport",
    "Gap",
    "MetricDrift",
    "MentionSpan",
    "ModelPoint",
    "ModelSweepReport",
    "NodeFidelity",
    "PipelineExtraction",
    "Provenance",
    "ProvenanceFidelity",
    "ReconstructedKG",
    "Resolution",
    "ResolutionConfig",
    "RunFidelity",
    "RunSpec",
    "SweepPoint",
    "SweepReport",
    "adjudicate_pair",
    "aggregate_relations",
    "align_reconstructed_ids",
    "build_adjudication_prompt",
    "build_aggregate",
    "build_attribution",
    "build_cell",
    "build_extraction_prompt",
    "build_kg",
    "cell_path",
    "compare",
    "chunk_jira",
    "chunk_markdown",
    "chunk_run",
    "describe_ontology",
    "extract_chunk",
    "extract_chunks",
    "default_run_specs",
    "extract_once",
    "iter_corpus_files",
    "matrix_metrics",
    "matrix_run_specs",
    "merge_envelopes",
    "metrics_from_summary",
    "parse_extraction",
    "project_with_groundings",
    "reconstruct_and_score",
    "regenerate_fake_metrics",
    "render_markdown",
    "resolve_entities",
    "run_e2e",
    "run_pipeline",
    "run_scale",
    "score_fidelity",
    "structural_envelope",
    "sweep_models",
    "sweep_thresholds",
]
