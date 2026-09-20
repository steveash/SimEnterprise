"""On-disk template store: ``template.json`` read/write/list/delete (EXPLORER_TEMPLATES.md §1).

A **template** is an external plugin directory (``<templates-dir>/<slug>/``)
holding a ``template.json`` (this module's :class:`Template` model), a
``plugin.py`` that self-registers into the process-wide registries on import
(the same shape as ``enterprise_sim/playbooks/build_software.py`` and
``enterprise_sim/archetypes/engineering.py``), and a ``test_<slug>.py``. This
module owns the metadata file's schema and CRUD; :mod:`enterprise_sim.templates.
scaffold` writes the initial skeleton and :mod:`enterprise_sim.templates.
validate` runs the validation loop and rewrites ``validation``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Env var naming the templates root (EXPLORER.md §4); ``--dir`` wins over it,
#: which in turn wins over the ``<cwd>/templates`` default.
TEMPLATES_DIR_ENV_VAR = "GRAPH_EXPLORER_TEMPLATES_DIR"

_TEMPLATE_FILE = "template.json"


class TemplateKind(StrEnum):
    """What a template registers: an archetype, a playbook, or both."""

    DEPARTMENT = "department"
    PLAYBOOK = "playbook"
    BUNDLE = "bundle"


class ValidationStatus(StrEnum):
    """The last-known outcome of ``templates validate`` for a template."""

    VALID = "valid"
    INVALID = "invalid"
    UNVALIDATED = "unvalidated"


class Provides(BaseModel):
    """The plugin names a template's ``plugin.py`` registers, by kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    archetypes: tuple[str, ...] = ()
    playbooks: tuple[str, ...] = ()
    processes: tuple[str, ...] = ()


class TemplateValidation(BaseModel):
    """The last recorded ``templates validate`` outcome (EXPLORER_TEMPLATES.md §1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ValidationStatus = ValidationStatus.UNVALIDATED
    checked_at: str | None = None
    summary: str = ""


class Template(BaseModel):
    """One ``template.json``: metadata for a user-authored plugin directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: TemplateKind
    description: str = ""
    provides: Provides = Field(default_factory=Provides)
    created_at: str
    updated_at: str
    validation: TemplateValidation = Field(default_factory=TemplateValidation)

    def to_json(self) -> str:
        """Canonical (sorted-key, indented) JSON text for ``template.json``."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Template:
        """Parse a :class:`Template` from ``template.json`` text."""
        return cls.model_validate(json.loads(text))


def now_iso() -> str:
    """The current UTC instant as an ISO-8601 string (``created_at``/``updated_at``)."""
    return datetime.now(UTC).isoformat()


def templates_dir(explicit: str | Path | None = None) -> Path:
    """Resolve the templates root: ``explicit`` (``--dir``) > env var > ``<cwd>/templates``."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(TEMPLATES_DIR_ENV_VAR)
    if env:
        return Path(env)
    return Path.cwd() / "templates"


def template_dir(root: Path, slug: str) -> Path:
    """The directory a template's files live in: ``<root>/<slug>/``."""
    return root / slug


def template_path(root: Path, slug: str) -> Path:
    """The path to a template's ``template.json``."""
    return template_dir(root, slug) / _TEMPLATE_FILE


def read_template(root: Path, slug: str) -> Template:
    """Read and parse ``<root>/<slug>/template.json``; raises if missing/invalid."""
    return Template.from_json(template_path(root, slug).read_text(encoding="utf-8"))


def write_template(root: Path, template: Template) -> None:
    """Write ``template`` to ``<root>/<slug>/template.json`` (creates the directory)."""
    path = template_path(root, template.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.to_json() + "\n", encoding="utf-8")


def delete_template(root: Path, slug: str) -> None:
    """Remove ``<root>/<slug>/`` entirely; raises :class:`FileNotFoundError` if absent."""
    path = template_dir(root, slug)
    if not path.is_dir():
        raise FileNotFoundError(f"no template {slug!r} under {root}")
    shutil.rmtree(path)


@dataclass(frozen=True, slots=True)
class TemplateEntry:
    """One row of ``templates list``: the parsed template plus its loadability.

    ``loadable`` is ``True`` iff ``template.json`` parsed; a template whose
    ``plugin.py`` fails to *import* is still listed (``loadable=True``) with
    whatever ``validation.status`` its last ``templates validate`` recorded —
    only a broken/missing ``template.json`` makes an entry unloadable here.
    """

    slug: str
    template: Template | None
    loadable: bool
    error: str | None


def list_templates(root: Path) -> list[TemplateEntry]:
    """List every template under ``root``, sorted by slug; ``[]`` if ``root`` is absent."""
    if not root.is_dir():
        return []
    entries: list[TemplateEntry] = []
    for sub in sorted(root.iterdir(), key=lambda p: p.name):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        path = sub / _TEMPLATE_FILE
        if not path.is_file():
            continue
        try:
            template = read_template(root, sub.name)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            entries.append(
                TemplateEntry(
                    slug=sub.name,
                    template=None,
                    loadable=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        entries.append(TemplateEntry(slug=sub.name, template=template, loadable=True, error=None))
    return entries


def prepare_isolated_import_dir(root: Path, slug: str) -> Path:
    """A fresh temp dir containing only ``<root>/<slug>`` (symlinked, named ``slug``).

    :func:`~enterprise_sim.core.registry.discover_paths` imports every
    ``<dir>/*/plugin.py`` under a directory it is given; pointing it at ``root``
    directly would import every *other* template living there too. This creates
    a one-entry directory so a single template's import (isolated §2.2 step 1,
    and the real in-process import steps 2-6 reuse it too) can never see, or be
    affected by, a sibling template under the same root.
    """
    source = template_dir(root, slug)
    if not source.is_dir():
        raise FileNotFoundError(f"no template {slug!r} under {root}")
    tmp = Path(tempfile.mkdtemp(prefix="esim-template-import-"))
    (tmp / slug).symlink_to(source.resolve(), target_is_directory=True)
    return tmp


def dump_template(template: Template) -> dict[str, Any]:
    """JSON-mode dict of ``template`` (for CLI output aggregation)."""
    return template.model_dump(mode="json")
