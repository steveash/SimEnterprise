"""Resolve built-in templates' code through the registries (tests only).

The built-in departments, playbooks, processes and data scenarios live in
``templates/`` and load as ``enterprise_sim_templates.<slug>`` — they are not
importable by a fixed package path. Tests find the module a registered name came
from via :meth:`Registry.origin`, exactly as the template catalog does.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from enterprise_sim.core.registry import Registry, load_templates


def template_module(registry: Registry[Any], name: str) -> ModuleType:
    """The loaded module that registered ``name`` into ``registry``."""
    load_templates()
    origin = registry.origin(name)
    assert origin is not None, f"{name!r} is not registered"
    return sys.modules[origin]
