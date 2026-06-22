"""The four plugin protocols (ARCHITECTURE.md §4).

Each is a *structural* contract: a registrable plugin satisfies it by exposing
the named attributes/methods, with no inheritance required. In M1 these capture
only what registration, discovery, and the deliverable→producer binding need —
a stable ``name`` plus a little declarative metadata. Later milestones extend
them with the behavioural methods sketched in ARCHITECTURE.md §5 (``processes``,
``run``, ``produce``, …) without the core having to change.

Keeping these protocols string-typed (no ``Event``/``WorldView`` imports) is what
lets ``core/registry`` stay free of the format and engine modules it indexes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class DepartmentArchetype(Protocol):
    """A kind of department — biases Layer A toward a believable org unit.

    Declares which playbooks it tends to run (by name); Layer A uses that to
    pick work for a generated department.
    """

    name: str
    #: names of playbooks this archetype typically runs.
    playbooks: Sequence[str]


@runtime_checkable
class Playbook(Protocol):
    """A kind of work, bound to a scenario (e.g. ``build_software``).

    Declares the abstract deliverable kinds it is expected to produce; the
    binding map routes each of those kinds to a producer.
    """

    name: str
    #: business vertical this playbook belongs to (``technology``, ``retail``…).
    vertical: str
    #: abstract deliverable kinds this playbook expects to emit.
    deliverables: Sequence[str]


@runtime_checkable
class Process(Protocol):
    """An atomic event-emitter (e.g. ``weekly_status``, ``design_review``).

    Declares the event types it emits and the abstract deliverable kinds it
    requests — never a file format (the extensibility invariant, §4).
    """

    name: str
    #: event types this process can emit.
    emits: Sequence[str]
    #: abstract deliverable kinds this process requests.
    requests: Sequence[str]


@runtime_checkable
class Producer(Protocol):
    """Renders events/deliverables into concrete artifacts (e.g. ``markdown``).

    Declares the deliverable kinds it can handle and the format(s) it emits; the
    binding map (``deliverable.kind → producer``) is built from these or from
    explicit config.
    """

    name: str
    #: concrete formats this producer emits (``markdown``, ``docx``, ``jira``…).
    formats: Sequence[str]
    #: abstract deliverable kinds this producer can render.
    handles: Sequence[str]
