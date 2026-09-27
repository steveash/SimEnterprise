"""End-to-end data-products run tests: templates, the question loop, and CLI.

The acceptance surface: every built-in scenario template lints clean and its
gap fixes apply cleanly; a real (tiny, keyless) run drives every question —
including the deliberately-gapped ones — to answerable within the loop budget
and lays down the full output contract *inside the world*
(``<world>/data/<scenario>/`` with spec/lineage/questions + parquet tables and
views, and the scenario's summary in the world's ``.world``).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.core.llm import build_client
from enterprise_sim.data_products.authoring import author_spec, world_context
from enterprise_sim.data_products.config import DataRunConfig
from enterprise_sim.data_products.lint import require_clean
from enterprise_sim.data_products.questions import QuestionState, collect_gap_deltas
from enterprise_sim.data_products.runner import DataRunResult, execute_data_run, scenario_dir
from enterprise_sim.data_products.scenarios import DATA_SCENARIOS, discover_scenarios
from enterprise_sim.data_products.spec import apply_deltas
from enterprise_sim.data_products.views import MaterializeError, materialize_views
from enterprise_sim.worlds import is_world_dir, read_world

START = date(2026, 1, 5)
END = date(2026, 1, 25)

EXPECTED_SCENARIOS = (
    "product_growth",
    "sales_pipeline",
    "support_ops",
    "subscription_finance",
    "marketing_attribution",
)

# Parametrize over the live registry so a newly-registered scenario is picked
# up by the template conformance tests automatically.
ALL_SCENARIOS = tuple(discover_scenarios().names())


def run_config(scenarios: tuple[str, ...]) -> DataRunConfig:
    return DataRunConfig.model_validate(
        {
            "scenarios": list(scenarios),
            "seed": 11,
            "world": {
                "company": {
                    "name": "Test Co",
                    "vertical": "software",
                    "size": "small",
                }
            },
            "window": {"start": START.isoformat(), "end": END.isoformat()},
            "scale": {"factor": 0.02, "rows_per_partition": 50_000},
            "loop": {"iterations": 3},
        }
    )


@pytest.fixture(scope="module")
def growth_run(tmp_path_factory: pytest.TempPathFactory) -> DataRunResult:
    tmp = tmp_path_factory.mktemp("data-run")
    return execute_data_run(
        run_config(("product_growth", "sales_pipeline")), worlds_root=tmp / "worlds"
    )


class TestTemplates:
    def test_all_scenarios_registered(self) -> None:
        registry = discover_scenarios()
        assert set(EXPECTED_SCENARIOS) <= set(registry.names())

    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_template_lints_clean(self, name: str) -> None:
        discover_scenarios()
        spec = DATA_SCENARIOS.get(name).build_spec(start=START, end=END)
        require_clean(spec)
        ids = [question.id for question in spec.questions]
        assert len(ids) == len(set(ids))
        assert spec.questions, "template must ship a question bank"
        assert any(question.gap is not None for question in spec.questions), (
            "template must ship gap questions to exercise the loop"
        )

    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_gap_fixes_apply_cleanly(self, name: str) -> None:
        discover_scenarios()
        spec = DATA_SCENARIOS.get(name).build_spec(start=START, end=END)
        states = [QuestionState(question) for question in spec.questions]
        deltas = collect_gap_deltas(states)  # nothing evaluated -> all gaps pending
        assert deltas
        require_clean(apply_deltas(spec, deltas))


class TestEndToEnd:
    def test_every_question_becomes_answerable(self, growth_run: DataRunResult) -> None:
        for scenario in growth_run.scenarios:
            assert scenario.questions_answerable == scenario.questions_total, scenario.scenario
            assert 2 <= scenario.iterations_run <= 3

    def test_gap_questions_needed_the_loop(self, growth_run: DataRunResult) -> None:
        payload = _questions_payload(growth_run, "product_growth")
        by_id = {question["id"]: question for question in payload["questions"]}
        assert by_id["pg.nps_weekly"]["first_answerable_iteration"] == 2
        assert by_id["pg.nps_weekly"]["attempts"][0]["ok"] is False
        assert by_id["pg.dau_trend"]["first_answerable_iteration"] == 1

    def test_data_lives_inside_the_world(self, growth_run: DataRunResult) -> None:
        # One folder per company: the inline-company config built a world, and
        # both scenarios landed under its data/ next to the corpus + KG.
        world_dir = growth_run.world_dir
        assert is_world_dir(world_dir)
        assert (world_dir / "kg" / "nodes.jsonl").is_file()
        assert (world_dir / "artifacts").is_dir()
        for scenario in growth_run.scenarios:
            assert scenario.data_dir == scenario_dir(world_dir, scenario.scenario)
            assert scenario.data_dir == world_dir / "data" / scenario.scenario
        # No per-scenario manifest: the summary is in the .world.
        assert not (growth_run.scenarios[0].data_dir / "manifest.json").exists()

    def test_output_contract(self, growth_run: DataRunResult) -> None:
        scenario = growth_run.scenarios[0]
        data_dir = scenario.data_dir
        for name in ("spec.json", "questions.json", "lineage.json", "spec.iteration-1.json"):
            assert (data_dir / name).is_file(), name
        for table_name in scenario.rows_by_table:
            parts = list((data_dir / "tables" / table_name).glob("*.parquet"))
            assert parts, table_name
        spec_doc = json.loads((data_dir / "spec.json").read_text())
        for view in spec_doc["views"]:
            assert (data_dir / "views" / view["name"] / "part-00000.parquet").is_file()

    def test_lineage_points_at_kg(self, growth_run: DataRunResult) -> None:
        payload = json.loads((growth_run.scenarios[0].data_dir / "lineage.json").read_text())
        product_map = payload["dimensions"]["user"]["product_line"]
        assert product_map, "product_line dimension must be KG-linked"
        assert all(kg_id.startswith("proj") for kg_id in product_map.values())

    def test_world_file_records_each_scenario(self, growth_run: DataRunResult) -> None:
        world = read_world(growth_run.world_dir)
        assert world is not None
        assert set(world.data) == {"product_growth", "sales_pipeline"}
        entry = world.data["product_growth"]
        assert entry["questions"]["answerable"] == entry["questions"]["total"]
        assert entry["iterations"][0]["questions_unanswered"]
        assert entry["llm_cost_usd"] == 0.0
        assert entry["dir"] == "data/product_growth/"
        assert entry["rows_by_table"] == growth_run.scenarios[0].rows_by_table
        assert world.outputs["data"] == "data/"
        data_entries = [h for h in world.history if h["kind"] == "data"]
        assert len(data_entries) == 1
        assert data_entries[0]["scenarios"] == ["product_growth", "sales_pipeline"]
        assert data_entries[0]["config"]["seed"] == 11
        assert data_entries[0]["config"]["world"]["company"]["name"] == "Test Co"

    def test_rerun_into_the_same_world_is_idempotent(
        self, growth_run: DataRunResult, tmp_path: Path
    ) -> None:
        # The inline company resolves to the same world (same id); the scenario
        # folder is rewritten in place and the .world gains one more data entry.
        again = execute_data_run(
            run_config(("product_growth",)), worlds_root=growth_run.world_dir.parent
        )
        assert again.world_dir == growth_run.world_dir
        world = read_world(again.world_dir)
        assert world is not None
        assert set(world.data) == {"product_growth", "sales_pipeline"}
        assert len([h for h in world.history if h["kind"] == "data"]) == 2
        assert [p.name for p in again.world_dir.parent.iterdir()] == [again.world_dir.name]


class TestAuthoring:
    def test_llm_mode_on_fake_backend_falls_back_to_template(self) -> None:
        discover_scenarios()
        plugin = DATA_SCENARIOS.get("product_growth")
        spec, report = author_spec(
            plugin,
            start=START,
            end=END,
            mode="llm",
            client=build_client(),  # the deterministic fake backend
            world=None,
            max_repair_attempts=1,
        )
        assert report.fell_back is True
        assert report.attempts == 2
        require_clean(spec)
        assert spec.name == "product_growth"

    def test_world_context_is_stable(self, growth_run: DataRunResult) -> None:
        from enterprise_sim.data_products.linking import load_world_kg

        world = load_world_kg(growth_run.world_dir)
        assert world_context(world) == world_context(world)
        assert "Person" in world_context(world)


class TestChurnStory:
    def test_subscription_churn_accumulates(self, tmp_path: Path) -> None:
        """The near-absorbing hazard must actually accumulate churn over time."""
        import duckdb

        config = run_config(("subscription_finance",))
        config = config.model_copy(
            update={"loop": config.loop.model_copy(update={"iterations": 1})}
        )
        result = execute_data_run(config, worlds_root=tmp_path / "worlds")
        data_dir = result.scenarios[0].data_dir
        conn = duckdb.connect(":memory:")
        try:
            row = conn.execute(
                "SELECT avg(CASE WHEN active THEN 1.0 ELSE 0.0 END) "
                "FILTER (WHERE date < DATE '2026-01-08'), "
                "avg(CASE WHEN active THEN 1.0 ELSE 0.0 END) "
                "FILTER (WHERE date > DATE '2026-01-22') "
                "FROM read_parquet('"
                + (data_dir / "tables" / "fact_account_day" / "*.parquet").as_posix()
                + "')"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        early, late = float(row[0]), float(row[1])
        assert early > 0.95, "the book must start active (lag_init)"
        assert late < early, "churn must accumulate (near-absorbing hazard)"


class TestViews:
    def test_validate_view_sql_catches_bad_views(self, growth_run: DataRunResult) -> None:
        from enterprise_sim.data_products.views import validate_view_sql

        spec = growth_run.scenarios[0].spec
        assert validate_view_sql(spec) == []
        broken = spec.model_copy(
            update={
                "views": (
                    *spec.views,
                    spec.views[0].model_copy(
                        update={"name": "broken", "sql": "SELECT nope FROM fact_user_day;"}
                    ),
                )
            }
        )
        errors = validate_view_sql(broken)
        assert len(errors) == 1 and "broken" in errors[0]

    def test_bad_view_sql_raises(self, growth_run: DataRunResult) -> None:
        scenario = growth_run.scenarios[0]
        spec = scenario.spec.model_copy(
            update={
                "views": (
                    *scenario.spec.views,
                    scenario.spec.views[0].model_copy(
                        update={"name": "broken", "sql": "SELECT nope FROM missing_table"}
                    ),
                )
            }
        )
        with pytest.raises(MaterializeError, match="broken"):
            materialize_views(spec, scenario.data_dir)


class TestCli:
    def test_data_scenarios_lists_plugins(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["data", "scenarios"]) == 0
        out = capsys.readouterr().out
        for name in EXPECTED_SCENARIOS:
            assert name in out

    def test_world_data_cli_builds_a_world_from_an_inline_company(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = {
            "scenarios": ["marketing_attribution"],
            "seed": 3,
            "world": {"company": {"name": "Cli Co", "vertical": "software", "size": "startup"}},
            "window": {"start": "2026-01-05", "end": "2026-01-18"},
            "scale": {"factor": 0.1, "rows_per_partition": 50_000},
            "loop": {"iterations": 2},
        }
        config_path = tmp_path / "data.json"
        config_path.write_text(json.dumps(config))
        root = tmp_path / "worlds"
        assert main(["world", "data", str(config_path), "--worlds-root", str(root)]) == 0
        out = capsys.readouterr().out
        assert "marketing_attribution" in out
        [world_dir] = list(root.iterdir())
        assert world_dir.name.startswith("cli-co-")
        assert (world_dir / "data" / "marketing_attribution" / "spec.json").is_file()

    def test_world_data_cli_attaches_to_an_existing_world(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = tmp_path / "world.toml"
        cfg.write_text(
            "seed = 3\n"
            '[company]\nname = "Attach Co"\nvertical = "software"\nsize = "startup"\n'
            "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-01-18\n",
            encoding="utf-8",
        )
        root = tmp_path / "worlds"
        assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
        [world_dir] = list(root.iterdir())
        capsys.readouterr()

        config = {
            "scenarios": ["support_ops"],
            "seed": 3,
            "world": {"world_dir": str(world_dir)},
            "window": {"start": "2026-01-05", "end": "2026-01-18"},
            "scale": {"factor": 0.1, "rows_per_partition": 50_000},
            "loop": {"iterations": 1},
        }
        config_path = tmp_path / "data.json"
        config_path.write_text(json.dumps(config))
        assert main(["world", "data", str(config_path)]) == 0
        assert "support_ops" in capsys.readouterr().out
        assert (world_dir / "data" / "support_ops" / "questions.json").is_file()
        world = read_world(world_dir)
        assert world is not None
        assert "support_ops" in world.data
        assert [h["kind"] for h in world.history] == ["create", "data"]

        # `--world` overrides the config's world_dir; a non-world dir is refused.
        assert main(["world", "data", str(config_path), "--world", str(tmp_path / "nope")]) == 1

    def test_world_data_rejects_bad_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = tmp_path / "bad.json"
        config_path.write_text(json.dumps({"scenarios": []}))
        assert main(["world", "data", str(config_path)]) == 2


def _questions_payload(result: DataRunResult, scenario: str) -> dict[str, Any]:
    for entry in result.scenarios:
        if entry.scenario == scenario:
            payload: dict[str, Any] = json.loads((entry.data_dir / "questions.json").read_text())
            return payload
    raise AssertionError(f"no scenario {scenario!r} in result")
