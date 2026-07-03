"""Entity resolution / canonicalization: mentions → canonical typed nodes (esim-nc6.4).

The reconstruct pipeline's hard middle step. Extraction (esim-nc6.3) emits one
:class:`~enterprise_sim.reconstruct.schema.MentionSpan` per surface form it reads
off a chunk — so the same real entity is named many times, in many ways: ``Ben Cho``
in a heading, ``Ben`` in the next sentence, ``Senior Engineer`` as a role-title
gloss. This module clusters those mentions into **canonical nodes**: one typed
:class:`~enterprise_sim.core.world.Node` per real entity, carrying its best label
and every alias it was seen under, plus a ``mention → canonical-id`` map the
relation-aggregation stage (esim-nc6.5) uses to rewrite candidate triples over
canonical ids.

Coreference — aliases and role-titles — is the recognized failure mode of KG
reconstruction (the EDC / LINK-KG line of work): over-merge two distinct people
named ``Ben`` and the graph fuses their neighborhoods; under-merge ``Ben Cho`` /
``Ben`` and it splits one person in two. The fidelity scorer (esim-nc6.6) measures
exactly these two errors, so this stage is where they are won or lost.

The resolution is a three-tier cascade, cheapest first, so the LLM is asked only
what similarity cannot settle:

1. **Block by type.** Only mentions of the same ontology type are ever compared —
   a ``Person`` is never merged with a ``Team``. Within a type-block, mentions
   with an identical *normalized* surface form merge outright (the trivial, common
   case), with no similarity computed.
2. **Hybrid similarity.** For the remaining cross-name pairs, a score combines
   char-n-gram TF-IDF cosine over the two *names* (catches ``Ben Cho`` / ``Ben``,
   ``Platform`` / ``Platform Team``) with TF-IDF cosine over each mention's local
   *context* window (catches two entities that share a name but not a
   neighborhood). Pairs scoring at or above :attr:`~ResolutionConfig.merge_threshold`
   merge without ever calling a model.
3. **LLM adjudication (gated).** Only genuinely ambiguous pairs — scoring in the
   band ``[ambiguous_threshold, merge_threshold)`` — are handed to a cheap model
   (:data:`~enterprise_sim.reconstruct.extract.HAIKU_MODEL`) that answers a single
   same-entity question. This path needs ``ANTHROPIC_API_KEY``; with no client the
   band is left unmerged, so the deterministic tiers stand alone and the whole
   blocking + similarity path is unit-testable without a key.

Everything except tier 3 is a pure function of the mentions and their chunks, so
the same corpus always resolves to the same canonical nodes and ids.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from enterprise_sim.core.llm import LLMClient, Prompt, assemble_prompt
from enterprise_sim.core.world import Node
from enterprise_sim.reconstruct.extract import HAIKU_MODEL
from enterprise_sim.reconstruct.schema import Chunk, MentionSpan

__all__ = [
    "ADJUDICATION_SCHEMA",
    "CanonicalEntity",
    "ResolutionConfig",
    "Resolution",
    "adjudicate_pair",
    "build_adjudication_prompt",
    "resolve_entities",
]


@dataclass(frozen=True)
class ResolutionConfig:
    """Tunable thresholds and weights for the resolution cascade.

    Attributes:
        ngram_size: Character n-gram width for the name TF-IDF vectors.
        context_window: Characters of chunk text taken on each side of a located
            mention to form its context document.
        name_weight: Weight of the name-similarity term in the hybrid score.
        context_weight: Weight of the context-similarity term in the hybrid score.
            The two weights are renormalized, so only their ratio matters.
        merge_threshold: Hybrid score at or above which a cross-name pair merges
            with no LLM call (tier 2). Because the name term carries
            ``name_weight`` of the score, a pair with *no* name overlap caps at
            ``context_weight`` (0.4) however similar its context — so a threshold
            above that can never merge two differently-named entities on context
            coincidence alone; it takes real name similarity *and* a shared
            neighborhood. The default (0.70) merges same-context short forms
            (``Alice Wong`` / ``Alice``) while leaving distinct entities (≤ 0.4)
            far below.
        ambiguous_threshold: Lower bound of the adjudication band; pairs scoring in
            ``[ambiguous_threshold, merge_threshold)`` are sent to the LLM (tier 3)
            when a client is available, and left unmerged otherwise.
        adjudication_min_confidence: Minimum model confidence for an LLM
            ``same_entity`` verdict to actually merge a pair.
    """

    ngram_size: int = 3
    context_window: int = 120
    name_weight: float = 0.6
    context_weight: float = 0.4
    merge_threshold: float = 0.70
    ambiguous_threshold: float = 0.5
    adjudication_min_confidence: float = 0.5


DEFAULT_CONFIG = ResolutionConfig()


@dataclass(frozen=True)
class CanonicalEntity:
    """One resolved real-world entity: a cluster of mentions with a best label.

    Attributes:
        id: Deterministic, content-derived canonical id (e.g. ``person:ben-cho``).
        type: The ontology type shared by every mention in the cluster (empty
            string for untyped mentions).
        label: The chosen canonical name — the most frequently seen surface form,
            longest then lexicographically first on ties.
        aliases: Every distinct surface form the entity was seen under (sorted;
            includes :attr:`label`), the alias set the fidelity scorer matches on.
        mentions: The :class:`MentionSpan`\\ s that resolved to this entity.
    """

    id: str
    type: str
    label: str
    aliases: tuple[str, ...]
    mentions: tuple[MentionSpan, ...]

    def to_node(self, created_at: datetime) -> Node:
        """Build the gold-schema :class:`~enterprise_sim.core.world.Node` for this entity.

        The node carries ``props["name"]`` = :attr:`label` and ``aliases`` =
        :attr:`aliases`, the two surface-form sources the fidelity scorer
        (esim-nc6.6) aligns reconstructed nodes to gold ids by.
        """
        return Node(
            id=self.id,
            type=self.type,
            created_at=created_at,
            props={"name": self.label},
            aliases=list(self.aliases),
        )


@dataclass(frozen=True)
class Resolution:
    """The resolver's result: canonical entities and the mention→canonical map.

    Attributes:
        entities: The canonical entities, in deterministic id order.
        mention_to_entity: Each input mention mapped to its canonical id.
    """

    entities: tuple[CanonicalEntity, ...]
    mention_to_entity: Mapping[MentionSpan, str]

    def nodes(self, created_at: datetime) -> list[Node]:
        """The canonical entities as gold-schema nodes, stamped ``created_at``."""
        return [entity.to_node(created_at) for entity in self.entities]

    def entity_of(self, mention: MentionSpan) -> str | None:
        """The canonical id a ``mention`` resolved to, or ``None`` if unknown."""
        return self.mention_to_entity.get(mention)


# --------------------------------------------------------------------------- #
# LLM adjudication (tier 3) — the only key-gated part.
# --------------------------------------------------------------------------- #

#: Forced-output schema for the same-entity adjudication call.
ADJUDICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "same_entity": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["same_entity"],
}


_ADJUDICATION_SYSTEM = (
    "You resolve entity coreference in an enterprise knowledge graph. Given two "
    "mentions of the same entity type, each with the text surrounding it, decide "
    "whether they refer to the SAME specific real-world entity.\n\n"
    "Rules:\n"
    "- Answer same_entity true only when the two mentions denote one and the same "
    'entity — e.g. a full name and a short form of it ("Ben Cho" / "Ben"), or a '
    "name and a role-title used for that same person in context.\n"
    "- Answer false when the mentions merely share a type or a common name but the "
    'context shows two distinct entities (two different people both called "Ben").\n'
    "- Prefer precision: if the context does not support merging, answer false.\n"
    "- Give a confidence in [0, 1]."
)


def build_adjudication_prompt(
    a: MentionSpan,
    b: MentionSpan,
    context_a: str,
    context_b: str,
) -> Prompt:
    """Assemble the same-entity :class:`~enterprise_sim.core.llm.Prompt` for a pair.

    The rules-bearing system prompt is the stable, cacheable prefix; the volatile
    suffix is just this pair's type, surface forms, and surrounding context.
    """
    type_ = a.entity_type or b.entity_type or "unknown"
    brief = (
        f"Entity type: {type_}\n\n"
        f'Mention A: "{a.surface_form}"\n'
        f"Context A: {context_a}\n\n"
        f'Mention B: "{b.surface_form}"\n'
        f"Context B: {context_b}\n\n"
        f"Do mention A and mention B refer to the same {type_}?"
    )
    return assemble_prompt(system=_ADJUDICATION_SYSTEM, brief=brief)


def adjudicate_pair(
    a: MentionSpan,
    b: MentionSpan,
    client: LLMClient,
    *,
    context_a: str,
    context_b: str,
    model: str | None = HAIKU_MODEL,
    min_confidence: float = 0.5,
) -> bool:
    """Ask ``client`` whether two mentions corefer; return whether to merge them.

    Forces the :data:`ADJUDICATION_SCHEMA` envelope out of the backend and returns
    ``True`` only when the model says ``same_entity`` *and* its confidence clears
    ``min_confidence`` (a missing/invalid confidence is treated as ``1.0``).
    """
    prompt = build_adjudication_prompt(a, b, context_a, context_b)
    result = client.generate_structured(prompt, ADJUDICATION_SCHEMA, model=model)
    if not bool(result.data.get("same_entity")):
        return False
    return _coerce_confidence(result.data.get("confidence")) >= min_confidence


def _coerce_confidence(value: Any) -> float:
    """Coerce a model-supplied confidence to a float in ``[0, 1]`` (default ``1.0``)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 1.0
    return max(0.0, min(1.0, float(value)))


# --------------------------------------------------------------------------- #
# Text features (tiers 1 & 2) — pure, keyless.
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")
_SLUG = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Casefold and collapse whitespace so surface forms compare canonically.

    Matches the fidelity scorer's normalization (esim-nc6.6) so the names this
    stage canonicalizes align the way the scorer will later compare them.
    """
    return _WS.sub(" ", text.strip()).casefold()


def _char_ngrams(text: str, n: int) -> list[str]:
    """Boundary-padded character n-grams of ``text``'s normalized form.

    Padding with spaces marks word edges, so ``Ben`` shares its leading gram with
    ``Ben Cho``. A form shorter than ``n`` (after padding) yields itself as one
    gram, so very short names still vectorize.
    """
    padded = f" {_normalize(text)} "
    if len(padded) <= n:
        return [padded]
    return [padded[i : i + n] for i in range(len(padded) - n + 1)]


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens of ``text``."""
    return _TOKEN.findall(text.casefold())


def _mention_context(mention: MentionSpan, chunk_text: str | None, window: int) -> str:
    """The chunk text around a located mention, or its surface form as a fallback.

    An unlocated mention (``start < 0``) or a missing chunk falls back to the bare
    surface form, so context similarity degrades to a name echo rather than failing.
    """
    if chunk_text is None or mention.start < 0:
        return mention.surface_form
    lo = max(0, mention.start - window)
    hi = min(len(chunk_text), mention.end + window)
    return chunk_text[lo:hi]


def _tfidf_vectors(docs: Sequence[Sequence[str]]) -> list[dict[str, float]]:
    """L2-normalized smoothed TF-IDF vectors for a set of term documents.

    IDF is computed over ``docs`` themselves (the type-block), so a term shared by
    every mention in the block contributes little and a distinctive one dominates.
    """
    n = len(docs)
    doc_freq: Counter[str] = Counter()
    for doc in docs:
        doc_freq.update(set(doc))
    vectors: list[dict[str, float]] = []
    for doc in docs:
        term_freq = Counter(doc)
        vec = {
            term: count * (math.log((n + 1) / (doc_freq[term] + 1)) + 1.0)
            for term, count in term_freq.items()
        }
        vectors.append(_l2_normalize(vec))
    return vectors


def _l2_normalize(vec: dict[str, float]) -> dict[str, float]:
    """Scale ``vec`` to unit length (an empty/zero vector stays empty)."""
    norm = math.sqrt(sum(weight * weight for weight in vec.values()))
    if norm == 0.0:
        return {}
    return {term: weight / norm for term, weight in vec.items()}


def _cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Cosine similarity of two L2-normalized sparse vectors (their dot product)."""
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(term, 0.0) for term, weight in a.items())


# --------------------------------------------------------------------------- #
# Union-find clustering within a type-block.
# --------------------------------------------------------------------------- #


def _cluster_block(
    mentions: Sequence[MentionSpan],
    name_vecs: Sequence[dict[str, float]],
    context_vecs: Sequence[dict[str, float]],
    config: ResolutionConfig,
    adjudicator: Callable[[int, int, float], bool] | None,
) -> list[list[int]]:
    """Cluster one type-block's mention indices via the three-tier cascade.

    Returns the clusters as lists of indices into ``mentions``, each sorted, the
    outer list ordered by smallest member index — a deterministic partition given
    a deterministic (or absent) ``adjudicator``.
    """
    parent = list(range(len(mentions)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    norm_names = [_normalize(m.surface_form) for m in mentions]
    weight_sum = config.name_weight + config.context_weight
    for i in range(len(mentions)):
        for j in range(i + 1, len(mentions)):
            if find(i) == find(j):
                continue
            # Tier 1: identical normalized name → merge, no similarity.
            if norm_names[i] == norm_names[j]:
                union(i, j)
                continue
            # Tier 2: hybrid similarity.
            name_sim = _cosine(name_vecs[i], name_vecs[j])
            context_sim = _cosine(context_vecs[i], context_vecs[j])
            score = config.name_weight * name_sim + config.context_weight * context_sim
            score = score / weight_sum if weight_sum else 0.0
            if score >= config.merge_threshold:
                union(i, j)
            # Tier 3: adjudicate the ambiguous band (only if a client is wired in).
            elif score >= config.ambiguous_threshold and adjudicator is not None:
                if adjudicator(i, j, score):
                    union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for index in range(len(mentions)):
        clusters[find(index)].append(index)
    return sorted((sorted(members) for members in clusters.values()), key=lambda ms: ms[0])


# --------------------------------------------------------------------------- #
# Entity assembly + id assignment.
# --------------------------------------------------------------------------- #


def _best_label(surface_forms: Sequence[str]) -> str:
    """Pick the canonical label: most frequent, then longest, then lexical-first."""
    counts = Counter(surface_forms)
    return min(counts, key=lambda form: (-counts[form], -len(form), form))


def _slug(text: str) -> str:
    """A stable url-ish slug of ``text`` for canonical ids (never empty)."""
    return _SLUG.sub("-", _normalize(text)).strip("-") or "entity"


def _mention_key(mention: MentionSpan) -> tuple[str, int, int, str]:
    """A deterministic ordering key for a mention (chunk, span, surface form)."""
    return (mention.chunk_id, mention.start, mention.end, mention.surface_form)


def _assign_ids(
    clusters: Sequence[tuple[str, tuple[MentionSpan, ...]]],
) -> list[CanonicalEntity]:
    """Build :class:`CanonicalEntity`\\ s with deterministic, de-duplicated ids.

    Each ``(type, mentions)`` cluster becomes an entity; ids are ``type:slug(label)``,
    with a ``-2``/``-3`` suffix when two distinct entities would otherwise collide
    (e.g. two people both named ``Ben`` the cascade kept apart). Clusters are
    ordered by ``(type, label, aliases, first-mention)`` first, so id assignment —
    and thus every id — is independent of input order.
    """
    prepared: list[tuple[str, str, tuple[str, ...], tuple[MentionSpan, ...]]] = []
    for type_, mentions in clusters:
        forms = [m.surface_form for m in mentions]
        label = _best_label(forms)
        aliases = tuple(sorted(set(forms)))
        prepared.append((type_, label, aliases, mentions))

    prepared.sort(key=lambda p: (p[0], p[1], p[2], min(_mention_key(m) for m in p[3])))

    used: Counter[str] = Counter()
    entities: list[CanonicalEntity] = []
    for type_, label, aliases, mentions in prepared:
        base = f"{(type_ or 'entity').lower()}:{_slug(label)}"
        used[base] += 1
        entity_id = base if used[base] == 1 else f"{base}-{used[base]}"
        entities.append(
            CanonicalEntity(
                id=entity_id,
                type=type_,
                label=label,
                aliases=aliases,
                mentions=mentions,
            )
        )
    return entities


def _make_adjudicator(
    mentions: Sequence[MentionSpan],
    chunk_text: Mapping[str, str],
    client: LLMClient | None,
    config: ResolutionConfig,
    model: str | None,
) -> Callable[[int, int, float], bool] | None:
    """Wrap ``client`` into an ``(i, j, score) → merge?`` callable, or ``None``.

    Returns ``None`` when no client is wired in, so the caller leaves the ambiguous
    band unmerged and the whole cascade stays keyless.
    """
    if client is None:
        return None

    def adjudicate(i: int, j: int, _score: float) -> bool:
        a, b = mentions[i], mentions[j]
        return adjudicate_pair(
            a,
            b,
            client,
            context_a=_mention_context(a, chunk_text.get(a.chunk_id), config.context_window),
            context_b=_mention_context(b, chunk_text.get(b.chunk_id), config.context_window),
            model=model or HAIKU_MODEL,
            min_confidence=config.adjudication_min_confidence,
        )

    return adjudicate


def resolve_entities(
    mentions: Sequence[MentionSpan],
    chunks: Iterable[Chunk] = (),
    *,
    config: ResolutionConfig = DEFAULT_CONFIG,
    client: LLMClient | None = None,
    model: str | None = None,
) -> Resolution:
    """Cluster ``mentions`` into canonical typed entities.

    Groups mentions by ontology type, resolves each block through the three-tier
    cascade (identical-name → hybrid similarity → gated LLM adjudication), and
    assembles one :class:`CanonicalEntity` per cluster with a best label, its alias
    set, and a deterministic id. ``chunks`` supply the text the context-similarity
    term reads; a ``client`` (with a key) enables tier-3 adjudication of ambiguous
    pairs — with none, that band is left unmerged and resolution is fully
    deterministic. ``model`` overrides the adjudication model (default
    :data:`~enterprise_sim.reconstruct.extract.HAIKU_MODEL`).
    """
    chunk_text = {chunk.id: chunk.text for chunk in chunks}

    blocks: dict[str, list[MentionSpan]] = defaultdict(list)
    for mention in mentions:
        blocks[mention.entity_type or ""].append(mention)

    clusters: list[tuple[str, tuple[MentionSpan, ...]]] = []
    for type_ in sorted(blocks):
        block = blocks[type_]
        name_vecs = _tfidf_vectors([_char_ngrams(m.surface_form, config.ngram_size) for m in block])
        context_vecs = _tfidf_vectors(
            [
                _tokens(_mention_context(m, chunk_text.get(m.chunk_id), config.context_window))
                for m in block
            ]
        )
        adjudicator = _make_adjudicator(block, chunk_text, client, config, model)
        for members in _cluster_block(block, name_vecs, context_vecs, config, adjudicator):
            clusters.append((type_, tuple(block[i] for i in members)))

    entities = _assign_ids(clusters)
    mention_to_entity = {mention: entity.id for entity in entities for mention in entity.mentions}
    return Resolution(entities=tuple(entities), mention_to_entity=mention_to_entity)
