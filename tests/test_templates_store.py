"""Tests for ``enterprise_sim.templates.store``: template.json CRUD + dir resolution."""

from __future__ import annotations

from pathlib import Path

import pydantic
import pytest
from enterprise_sim.templates.store import (
    TEMPLATES_DIR_ENV_VAR,
    Provides,
    Template,
    TemplateKind,
    ValidationStatus,
    delete_template,
    list_templates,
    now_iso,
    prepare_isolated_import_dir,
    read_template,
    template_dir,
    templates_dir,
    write_template,
)


def _template(slug: str = "widget", kind: TemplateKind = TemplateKind.DEPARTMENT) -> Template:
    stamp = now_iso()
    return Template(slug=slug, name="Widget", kind=kind, created_at=stamp, updated_at=stamp)


# -- dir resolution ------------------------------------------------------------


def test_templates_dir_explicit_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEMPLATES_DIR_ENV_VAR, str(tmp_path / "env"))
    assert templates_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_templates_dir_env_var_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEMPLATES_DIR_ENV_VAR, str(tmp_path / "env"))
    assert templates_dir() == tmp_path / "env"


def test_templates_dir_defaults_to_cwd_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TEMPLATES_DIR_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    assert templates_dir() == tmp_path / "templates"


# -- read/write/delete ----------------------------------------------------------


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    template = _template()
    write_template(tmp_path, template)

    loaded = read_template(tmp_path, "widget")

    assert loaded == template
    assert (tmp_path / "widget" / "template.json").is_file()


def test_read_missing_template_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_template(tmp_path, "missing")


def test_delete_template_removes_directory(tmp_path: Path) -> None:
    write_template(tmp_path, _template())
    (template_dir(tmp_path, "widget") / "plugin.py").write_text("", encoding="utf-8")

    delete_template(tmp_path, "widget")

    assert not template_dir(tmp_path, "widget").exists()


def test_delete_missing_template_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        delete_template(tmp_path, "missing")


# -- Template model --------------------------------------------------------------


def test_template_kind_and_validation_default() -> None:
    template = _template(kind=TemplateKind.BUNDLE)
    assert template.kind is TemplateKind.BUNDLE
    assert template.validation.status is ValidationStatus.UNVALIDATED
    assert template.provides == Provides()


def test_template_forbids_extra_fields() -> None:
    stamp = now_iso()
    with pytest.raises(pydantic.ValidationError):
        Template.model_validate(
            {
                "slug": "x",
                "name": "X",
                "kind": "department",
                "created_at": stamp,
                "updated_at": stamp,
                "surprise": True,
            }
        )


def test_template_json_round_trips() -> None:
    template = _template()
    assert Template.from_json(template.to_json()) == template


# -- list_templates ---------------------------------------------------------------


def test_list_templates_empty_when_dir_absent(tmp_path: Path) -> None:
    assert list_templates(tmp_path / "nope") == []


def test_list_templates_sorted_by_slug(tmp_path: Path) -> None:
    write_template(tmp_path, _template("bravo"))
    write_template(tmp_path, _template("alpha"))

    entries = list_templates(tmp_path)

    assert [e.slug for e in entries] == ["alpha", "bravo"]
    assert all(e.loadable and e.error is None for e in entries)


def test_list_templates_reports_unloadable_json(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "template.json").write_text("not json", encoding="utf-8")

    entries = list_templates(tmp_path)

    assert len(entries) == 1
    assert entries[0].slug == "broken"
    assert entries[0].loadable is False
    assert entries[0].template is None
    assert entries[0].error is not None


def test_list_templates_skips_dir_without_template_json(tmp_path: Path) -> None:
    (tmp_path / "stray").mkdir()
    (tmp_path / "stray" / "plugin.py").write_text("", encoding="utf-8")

    assert list_templates(tmp_path) == []


# -- prepare_isolated_import_dir ---------------------------------------------------


def test_prepare_isolated_import_dir_contains_only_the_one_slug(tmp_path: Path) -> None:
    write_template(tmp_path, _template("a"))
    write_template(tmp_path, _template("b"))
    (template_dir(tmp_path, "a") / "plugin.py").write_text("", encoding="utf-8")
    (template_dir(tmp_path, "b") / "plugin.py").write_text("", encoding="utf-8")

    isolated = prepare_isolated_import_dir(tmp_path, "a")

    assert [p.name for p in isolated.iterdir()] == ["a"]
    assert (isolated / "a" / "plugin.py").is_file()


def test_prepare_isolated_import_dir_missing_slug_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        prepare_isolated_import_dir(tmp_path, "nope")
