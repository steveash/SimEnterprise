"""External plugin templates: department archetypes / playbooks authored outside the package.

A **template** lives in a user directory (``templates/<slug>/``, see
:mod:`enterprise_sim.templates.store`) rather than inside ``enterprise_sim/``,
so the explorer UI (and the ``enterprise-sim templates`` CLI) can create,
validate, and delete plugins without touching the codebase
(``docs/EXPLORER_TEMPLATES.md``). :func:`~enterprise_sim.templates.scaffold.
scaffold` writes a new template's skeleton;
:func:`~enterprise_sim.templates.validate.validate` runs its six-step
validation loop; :mod:`enterprise_sim.templates.catalog` merges built-ins with
valid templates for the run form's dropdowns.
"""

from __future__ import annotations

from enterprise_sim.templates.catalog import build_catalog
from enterprise_sim.templates.scaffold import scaffold
from enterprise_sim.templates.store import (
    TEMPLATES_DIR_ENV_VAR,
    Provides,
    Template,
    TemplateEntry,
    TemplateKind,
    TemplateValidation,
    ValidationStatus,
    delete_template,
    list_templates,
    read_template,
    template_dir,
    templates_dir,
    write_template,
)
from enterprise_sim.templates.validate import validate

__all__ = [
    "TEMPLATES_DIR_ENV_VAR",
    "Provides",
    "Template",
    "TemplateEntry",
    "TemplateKind",
    "TemplateValidation",
    "ValidationStatus",
    "build_catalog",
    "delete_template",
    "list_templates",
    "read_template",
    "scaffold",
    "template_dir",
    "templates_dir",
    "validate",
    "write_template",
]
