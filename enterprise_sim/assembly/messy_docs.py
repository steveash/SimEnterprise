"""Document-level messiness: dropped headers and near-duplicate copies.

Prose-level noise (typos, aliases, date formats, filler) is applied by every
producer through :meth:`~enterprise_sim.producers.markdown.ProducerContext.messy`.
This module handles what only exists once a document is rendered, for Markdown
documents (the text formats):

* ``drop_header_rate`` — the templated ``**Type:** … · **Date:** …`` line is
  removed, so the date and authors survive only in the prose (if at all);
* ``duplicate_rate`` — a near-duplicate copy is written beside the original
  (``-v2``/``-final``/``-copy`` path suffix), its last paragraph dropped, with
  its own ``Artifact`` node carrying ``duplicate_of`` and a ``duplicate_of`` edge.

Every change is a quirk on the artifact's gold node (``props["quirks"]``), as
for prose noise. Draws are seeded by ``(world seed, artifact id)``.
"""

from __future__ import annotations

import copy
import hashlib
import random

from enterprise_sim.core.world import Edge, Node
from enterprise_sim.producers.artifact import ProducedArtifact
from enterprise_sim.producers.noise import ProseNoise

__all__ = ["apply_document_messiness"]

E_DUPLICATE_OF = "duplicate_of"
_SUFFIXES = ("-v2", "-final", "-copy", "-v2-final", "-old")


def _rng(seed: int, key: str) -> random.Random:
    digest = hashlib.sha256(f"docnoise\x00{seed}\x00{key}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def apply_document_messiness(
    produced: list[ProducedArtifact], noise: ProseNoise
) -> list[ProducedArtifact]:
    """Label prose quirks, drop headers, and return any duplicate copies to add."""
    cfg = noise.config
    extra: list[ProducedArtifact] = []
    for artifact in produced:
        quirks = list(noise.quirks(artifact.artifact_id))
        if artifact.fmt == "markdown" and not artifact.is_binary:
            rng = _rng(noise.seed, artifact.artifact_id)
            if cfg.drop_header_rate > 0 and rng.random() < cfg.drop_header_rate:
                lines = artifact.body.split("\n")
                kept = [line for line in lines if not line.startswith("**Type:**")]
                if len(kept) != len(lines):
                    artifact.body = "\n".join(kept)
                    quirks.append({"kind": "dropped_header", "original": "", "surface": ""})
            if cfg.duplicate_rate > 0 and rng.random() < cfg.duplicate_rate:
                extra.append(_duplicate(artifact, rng))
                quirks.append({"kind": "has_duplicate", "original": "", "surface": ""})
        if quirks:
            artifact.node.props["quirks"] = quirks
    return extra


def _duplicate(original: ProducedArtifact, rng: random.Random) -> ProducedArtifact:
    suffix = rng.choice(_SUFFIXES)
    stem, dot, ext = original.path.rpartition(".")
    path = f"{stem}{suffix}.{ext}" if dot else f"{original.path}{suffix}"
    artifact_id = f"{original.artifact_id}{suffix}"
    paragraphs = original.body.rstrip("\n").split("\n\n")
    body = "\n\n".join(paragraphs[:-1] if len(paragraphs) > 3 else paragraphs) + "\n"
    props = copy.deepcopy(original.node.props)
    props.update(
        {
            "path": path,
            "duplicate_of": original.artifact_id,
            "quirks": [{"kind": "duplicate", "original": original.path, "surface": path}],
        }
    )
    node = Node(artifact_id, original.node.type, original.node.created_at, props=props, aliases=[])
    edges = [
        Edge(
            f"edge:{e.type}:{e.src if e.src != original.artifact_id else artifact_id}->"
            f"{e.dst if e.dst != original.artifact_id else artifact_id}",
            e.type,
            artifact_id if e.src == original.artifact_id else e.src,
            artifact_id if e.dst == original.artifact_id else e.dst,
            e.created_at,
            props=copy.deepcopy(e.props),
        )
        for e in original.edges
        if e.type in ("authored", "expresses")
    ]
    edges.append(
        Edge(
            f"edge:{E_DUPLICATE_OF}:{artifact_id}->{original.artifact_id}",
            E_DUPLICATE_OF,
            artifact_id,
            original.artifact_id,
            original.node.created_at,
        )
    )
    return ProducedArtifact(
        artifact_id=artifact_id,
        path=path,
        fmt=original.fmt,
        body=body,
        node=node,
        edges=edges,
        metadata={**original.metadata, "path": path, "duplicate_of": original.artifact_id},
        cache_hit=True,
    )
