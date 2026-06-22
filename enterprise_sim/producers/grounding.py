"""Grounding: keep prose consistent with the KG (ARCHITECTURE.md §16.2/§11.3, D30/D20).

Three independent mechanisms, used together by the markdown producer:

1. **Constrained input** (D30.1) — :class:`Roster` projects a :class:`WorldView`
   into the small, closed set of entities a producer may name, and renders the
   ``"refer only to these people, by these names"`` prompt block. The model never
   sees an out-of-scope or future entity.
2. **The mention tagger** (D20, §11.3) — :func:`tag_mentions` scans rendered text
   for the *known surface forms* of in-scope entities. Because the candidate set
   is small and known this is **constrained, high-precision alias matching**, not
   open-domain NER: longest surface form wins, matches respect word boundaries and
   never overlap, and output is ordered by position for byte-stable
   ``mentions.jsonl``.
3. **Detect + one repair** (D30.3) — :func:`detect_unresolved_names` finds
   name-like spans in *generated prose* that resolve to no in-scope entity. The
   producer uses that to fire a single repair re-prompt; whatever survives becomes
   a ``validation/issues.jsonl`` entry (the artifact is always kept, §16.2/D17).

Everything here is a pure function of its inputs and deterministic, so the same
``(WorldView, text)`` always yields the same roster, mentions, and findings.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from enterprise_sim.core.world import WorldView
from enterprise_sim.producers.artifact import Locator, Mention, aliases_for

__all__ = [
    "DEFAULT_NAMED_TYPES",
    "Roster",
    "RosterEntry",
    "detect_unresolved_names",
    "tag_mentions",
]

# Entity types whose surface forms the producer may use and the tagger resolves.
# People dominate prose references; teams/projects/initiatives are named subjects
# that also legitimately appear, so they are in-scope for tagging and must not be
# flagged as unresolved names.
DEFAULT_NAMED_TYPES: tuple[str, ...] = (
    "Person",
    "Team",
    "Department",
    "Initiative",
    "Project",
    "Company",
)

# The shape of a written *person* name: two-or-more consecutive Title-Case words,
# each a capital followed by lowercase letters ("Grace Hopper", "Ada King Lovelace").
# Deliberately strict — it excludes acronyms (``API``, ``KPI``), alphanumerics
# (``Q3``), all-caps emphasis (``MUST``), and lone capitalized words — so the
# repair pass fires on plausible hallucinated *people*, not ordinary prose nouns.
_NAME_WORD = r"[A-Z][a-z]+(?:[-'’][A-Z][a-z]+)?"
# Words are joined by spaces/tabs only — a written name never wraps across a line.
_NAME_RUN = re.compile(rf"{_NAME_WORD}(?:[ \t]+{_NAME_WORD})+")

# Title-Case words that commonly *open* a name-shaped run without naming a person
# ("Next Quarter", "Our Team", "This Sprint"); a run starting with one is ignored.
_SENTENCE_STOPWORDS = frozenset(
    {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "We",
        "Our",
        "Their",
        "Its",
        "They",
        "In",
        "On",
        "At",
        "As",
        "For",
        "And",
        "But",
        "Or",
        "If",
        "When",
        "While",
        "After",
        "Before",
        "During",
        "By",
        "With",
        "From",
        "All",
        "Each",
        "No",
        "Next",
        "Last",
        "First",
        "Today",
        "Now",
        "Per",
        "Both",
        "Some",
        "Many",
    }
)


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One in-scope entity the producer may name (a row of the roster).

    ``surfaces`` is the entity's known surface forms (canonical name first, then
    aliases), all of which the tagger resolves back to ``entity_id``.
    """

    entity_id: str
    entity_type: str
    canonical: str
    surfaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Roster:
    """The closed set of entities a producer may reference (D30 layer 1).

    Built from a timestamped :class:`WorldView` so it cannot contain a future or
    out-of-scope entity. Carries both the prompt-facing view (:meth:`roster_block`)
    and the matcher-facing index (:attr:`surface_index`) the tagger consumes.
    """

    entries: tuple[RosterEntry, ...]
    #: surface form → entity id, longest-surface-first iteration order.
    surface_index: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @classmethod
    def from_worldview(
        cls,
        view: WorldView,
        *,
        types: Sequence[str] = DEFAULT_NAMED_TYPES,
    ) -> Roster:
        """Project ``view`` into a roster of its in-scope named entities.

        Entities are taken in (type, id) order for determinism; each contributes
        its canonical name + aliases as resolvable surface forms. A surface form
        claimed by two entities (an ambiguous alias) is bound to the **first**
        entity in that stable order — the producer logs the collision separately;
        the tagger must still resolve deterministically.
        """
        entries: list[RosterEntry] = []
        seen_surface: dict[str, str] = {}
        for node_type in types:
            for node in view.nodes_by_type(node_type):
                surfaces = tuple(aliases_for(node))
                if not surfaces:
                    continue
                entries.append(
                    RosterEntry(
                        entity_id=node.id,
                        entity_type=node_type,
                        canonical=surfaces[0],
                        surfaces=surfaces,
                    )
                )
                for surface in surfaces:
                    if surface not in seen_surface:
                        seen_surface[surface] = node.id
        # Longest surface first so "Ada Lovelace" wins over "Ada" at match time.
        index = sorted(seen_surface.items(), key=lambda kv: (-len(kv[0]), kv[0]))
        return cls(entries=tuple(entries), surface_index=tuple(index))

    # -- prompt-facing -----------------------------------------------------

    def is_empty(self) -> bool:
        """True when the roster names no entity (nothing to ground against)."""
        return not self.entries

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Ids of every in-scope entity, in roster order."""
        return tuple(e.entity_id for e in self.entries)

    def allowed_surfaces(self) -> frozenset[str]:
        """Every surface form any in-scope entity may legitimately appear as."""
        return frozenset(surface for surface, _ in self.surface_index)

    def roster_block(self, *, limit: int | None = None) -> str:
        """Render the D30 *constrained input* block for the prompt.

        Lists each person/entity by canonical name (+ aliases) and closes with the
        explicit instruction to use only these names. ``limit`` caps the people
        section for very large scenarios (the cap is disclosed in the text).
        """
        if self.is_empty():
            return "ROSTER: (no in-scope entities — do not name any specific people.)"
        people = [e for e in self.entries if e.entity_type == "Person"]
        others = [e for e in self.entries if e.entity_type != "Person"]
        lines = ["ROSTER — refer ONLY to the following, by exactly these names:"]
        shown = people if limit is None else people[:limit]
        for entry in shown:
            extra = entry.surfaces[1:]
            alias = f" (aka {', '.join(extra)})" if extra else ""
            lines.append(f"- {entry.canonical}{alias}")
        if limit is not None and len(people) > limit:
            lines.append(f"- … and {len(people) - limit} more (use only listed names)")
        if others:
            named = ", ".join(e.canonical for e in others)
            lines.append(f"In-scope teams/projects/initiatives: {named}.")
        lines.append("Do not invent or mention anyone not listed above.")
        return "\n".join(lines)


# -- mention tagging --------------------------------------------------------


def tag_mentions(text: str, roster: Roster, *, artifact_path: str) -> list[Mention]:
    """Tag in-scope entity surface forms in ``text`` (D20, §11.3).

    Constrained, high-precision alias matching: only the roster's known surface
    forms are searched, the longest match wins at any position, matches respect
    word boundaries and never overlap, and every full occurrence is recorded (not
    just the first). Output is ordered by character offset so the resulting
    ``mentions.jsonl`` is byte-stable across runs.
    """
    if roster.is_empty() or not text:
        return []
    line_starts = _line_starts(text)
    claimed: list[tuple[int, int]] = []  # (start, end) spans already taken
    mentions: list[Mention] = []
    # Surfaces are longest-first; a longer name claims its span before a shorter
    # sub-name can, which is what gives "Ada Lovelace" priority over "Ada".
    for surface, entity_id in roster.surface_index:
        for start, end in _iter_word_bounded(text, surface):
            if _overlaps(start, end, claimed):
                continue
            claimed.append((start, end))
            mentions.append(
                Mention(
                    artifact_path=artifact_path,
                    entity_id=entity_id,
                    surface_form=surface,
                    locator=Locator(
                        offset=start,
                        length=end - start,
                        line=_line_of(start, line_starts),
                    ),
                )
            )
    mentions.sort(key=lambda m: (m.locator.offset, m.locator.length))
    return mentions


def detect_unresolved_names(text: str, roster: Roster) -> list[str]:
    """Return name-like spans in ``text`` that resolve to no in-scope entity (D30.3).

    Only ``FirstName LastName``-shaped runs (see :data:`_NAME_RUN`) are considered,
    which keeps precision high — false positives cost a needless repair call. A run
    is reported when it is **not** a known surface form, **not** word-for-word
    covered by in-scope surfaces (two adjacent in-scope names), and does **not**
    open with a Title-Case sentence word ("Next Quarter"). Duplicates collapse;
    order follows first appearance.

    Known limitation: without a gazetteer this cannot tell a hallucinated person
    ("Grace Hopper") from a Title-Case domain bigram ("Machine Learning"), so the
    latter can trigger a spurious repair. That is acceptable by design — repair is
    a best-effort layer that always keeps the artifact (§16.2.3 / D17); the cost of
    a false positive is one extra call and a soft, filterable validation issue.
    """
    allowed = roster.allowed_surfaces()
    findings: list[str] = []
    seen: set[str] = set()
    for match in _NAME_RUN.finditer(text):
        span = match.group(0)
        if span in allowed or span in seen:
            continue
        if _all_words_allowed(span, allowed):
            continue
        if span.split()[0] in _SENTENCE_STOPWORDS:
            continue
        seen.add(span)
        findings.append(span)
    return findings


# -- helpers ----------------------------------------------------------------


def _iter_word_bounded(text: str, needle: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` of every word-boundary occurrence of ``needle``."""
    if not needle:
        return
    start = 0
    n = len(needle)
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            return
        end = idx + n
        if _word_bounded(text, idx, end):
            yield idx, end
        start = idx + 1


def _word_bounded(text: str, start: int, end: int) -> bool:
    """True when ``text[start:end]`` is not glued to an adjacent word character."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not _is_wordish(before) and not _is_wordish(after)


def _is_wordish(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch in "_'’")


def _overlaps(start: int, end: int, claimed: Sequence[tuple[int, int]]) -> bool:
    return any(start < c_end and c_start < end for c_start, c_end in claimed)


def _all_words_allowed(span: str, allowed: frozenset[str]) -> bool:
    """True when every whitespace-separated word of ``span`` is an allowed surface."""
    words = span.split()
    return len(words) > 1 and all(word in allowed for word in words)


def _line_starts(text: str) -> list[int]:
    """Offsets at which each line begins (index 0 → line 1)."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_of(offset: int, line_starts: Sequence[int]) -> int:
    """1-based line number containing ``offset`` (binary search over starts)."""
    lo, hi = 0, len(line_starts) - 1
    line = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if line_starts[mid] <= offset:
            line = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return line + 1
