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

from enterprise_sim.reconstruct.chunk import (
    chunk_jira,
    chunk_markdown,
    chunk_run,
    iter_corpus_files,
)
from enterprise_sim.reconstruct.extract import (
    EXTRACTION_SCHEMA,
    HAIKU_MODEL,
    Extraction,
    build_extraction_prompt,
    extract_chunk,
    extract_chunks,
    parse_extraction,
)
from enterprise_sim.reconstruct.fidelity import (
    PRF,
    EdgeFidelity,
    EntityResolution,
    FidelityReport,
    NodeFidelity,
    score_fidelity,
)
from enterprise_sim.reconstruct.ontology import (
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
from enterprise_sim.reconstruct.schema import (
    CandidateTriple,
    Chunk,
    MentionSpan,
    Provenance,
    ReconstructedKG,
)

__all__ = [
    "ADJUDICATION_SCHEMA",
    "EXTRACTION_SCHEMA",
    "HAIKU_MODEL",
    "NODE_TYPES",
    "PRF",
    "RELATION_GLOSSES",
    "RELATION_TYPES",
    "CandidateTriple",
    "CanonicalEntity",
    "Chunk",
    "EdgeFidelity",
    "EntityResolution",
    "Extraction",
    "FidelityReport",
    "MentionSpan",
    "NodeFidelity",
    "Provenance",
    "ReconstructedKG",
    "Resolution",
    "ResolutionConfig",
    "adjudicate_pair",
    "build_adjudication_prompt",
    "build_extraction_prompt",
    "chunk_jira",
    "chunk_markdown",
    "chunk_run",
    "describe_ontology",
    "extract_chunk",
    "extract_chunks",
    "iter_corpus_files",
    "parse_extraction",
    "resolve_entities",
    "score_fidelity",
]
