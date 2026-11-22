"""What a document may see: the one place its as-of view is cut (``document_view``).

Every deliverable event's producers render against ``document_view(world, event)``:
``world.projection(at=event.timestamp)`` minus

* everything across the walls of the event's project (decision record 0036,
  :func:`enterprise_sim.world_builders.walls.hidden_from`), and
* every planted homonym twin outside the event's department
  (:func:`enterprise_sim.world_builders.homonyms.homonyms_hidden`), so a shared full
  name resolves to the twin the document's context means.

The event's own actors are never hidden. A world without walls or homonyms sees
exactly ``world.projection(at)``, as before.
"""

from __future__ import annotations

from enterprise_sim.core.events import Event
from enterprise_sim.core.world import World, WorldView
from enterprise_sim.world_builders.homonyms import homonyms_hidden
from enterprise_sim.world_builders.walls import hidden_from

__all__ = ["document_view"]


def document_view(world: World, event: Event) -> WorldView:
    """The view the producers of ``event``'s deliverable render against."""
    hidden = hidden_from(world, event.project) | homonyms_hidden(world, event.project)
    hidden -= {pid for ids in event.actors.values() for pid in ids}
    if not hidden:
        return world.projection(at=event.timestamp)
    keep = [n.id for n in world.nodes() if n.id not in hidden]
    return world.projection(at=event.timestamp, focus=keep)
