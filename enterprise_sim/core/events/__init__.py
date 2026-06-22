"""Event types and the append-only event log.

See ARCHITECTURE.md §5 (the ``Event`` contract) and §11.2/§11.4 (the journal).
"""

from __future__ import annotations

from enterprise_sim.core.events.event import Deliverable, Event
from enterprise_sim.core.events.journal import EventJournal

__all__ = ["Deliverable", "Event", "EventJournal"]
