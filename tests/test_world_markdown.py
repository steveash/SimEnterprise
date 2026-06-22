"""Tests for the ``organization/`` markdown reference renderer (esim-65bf4594).

The renderer is a pure function of a :class:`World`, so it must be deterministic
and must surface the org a reader expects: company + goals, a full roster, and a
per-department page with teams, people, and the initiative→project tree.
"""

from __future__ import annotations

from pathlib import Path

from enterprise_sim.core.config import RunConfig, load_config_from_mapping
from enterprise_sim.world_builders import build_world, render_organization, write_organization


def _config(*, size: str = "medium", projects: list[dict[str, str]] | None = None) -> RunConfig:
    return load_config_from_mapping(
        {
            "company": {"name": "Acme Corp", "vertical": "software", "size": size},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-03-31"},
            "seed": 7,
            "projects": projects or [],
        }
    )


def test_renders_expected_pages() -> None:
    pages = render_organization(build_world(_config()))
    assert "README.md" in pages
    assert "company.md" in pages
    assert "people.md" in pages
    assert any(name.startswith("departments/") for name in pages)


def test_rendering_is_deterministic_and_pure() -> None:
    a = render_organization(build_world(_config()))
    b = render_organization(build_world(_config()))
    assert a == b


def test_company_page_lists_company_and_goals() -> None:
    pages = render_organization(build_world(_config()))
    company = pages["company.md"]
    assert "Acme Corp" in company
    assert "## Goals" in company
    # Goals are rendered as bullets; at least one must appear.
    assert company.count("\n- ") >= 1


def test_people_page_tabulates_the_roster() -> None:
    world = build_world(_config())
    people = render_organization(world)["people.md"]
    # Header row + one row per person.
    assert "| Name | Title | Seniority | Expertise |" in people
    for person in world.nodes_by_type("Person"):
        assert person.props["name"] in people


def test_department_page_shows_teams_and_playbook_binding() -> None:
    pages = render_organization(build_world(_config()))
    eng = pages["departments/engineering.md"]
    assert "# Engineering" in eng
    assert "## Teams" in eng
    assert "## Initiatives & projects" in eng
    assert "playbook `build_software`" in eng


def test_config_project_appears_in_markdown() -> None:
    pages = render_organization(build_world(_config(projects=[{"name": "Checkout Revamp"}])))
    eng = pages["departments/engineering.md"]
    assert "Checkout Revamp" in eng


def test_write_organization_materializes_files(tmp_path: Path) -> None:
    world = build_world(_config())
    written = write_organization(world, tmp_path)
    assert (tmp_path / "README.md").is_file()
    assert (tmp_path / "company.md").is_file()
    assert (tmp_path / "people.md").is_file()
    assert (tmp_path / "departments" / "engineering.md").is_file()
    # Every returned path exists and matches the pure-render output.
    rendered = render_organization(world)
    for path in written:
        assert path.is_file()
    for rel, text in rendered.items():
        assert (tmp_path / rel).read_text(encoding="utf-8") == text
