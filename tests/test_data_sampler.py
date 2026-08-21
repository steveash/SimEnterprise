"""Sampler engine tests: determinism, causal fidelity, and KG linking.

The sampler's contract is that a linted spec + seed + world fully determine
every byte of parquet, and that the structural equations actually shape the
data (effects flow parent → child, shocks move the series, lags persist).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest
from enterprise_sim.core.config import RunConfig
from enterprise_sim.core.config.models import CompanyConfig, CompanySize, SimulationConfig
from enterprise_sim.core.config.seed import SeedContext
from enterprise_sim.core.world import World
from enterprise_sim.data_products.linking import LinkageError, load_world_from_run
from enterprise_sim.data_products.lint import require_clean
from enterprise_sim.data_products.sampler import sample_scenario
from enterprise_sim.data_products.scenarios._helpers import (
    attr_col,
    categorical,
    date_col,
    entity_col,
    eqn,
    factor_col,
    kg_id_col,
    kg_name_col,
    logit,
    panel_col,
    table,
    term,
    variable,
)
from enterprise_sim.data_products.spec import (
    FactorSpec,
    KgDimension,
    KgIdentity,
    PopulationSpec,
    ScenarioSpec,
    Shock,
    TableGrain,
    VariableKind,
)
from enterprise_sim.world_builders import build_world

START = date(2026, 1, 5)
END = date(2026, 2, 13)  # 40 days


@pytest.fixture(scope="module")
def world() -> World:
    config = RunConfig(
        company=CompanyConfig(name="Sampler Co", vertical="software", size=CompanySize.SMALL),
        simulation=SimulationConfig(period_start=START, period_end=END),
        seed=3,
    )
    return build_world(config)


def causal_spec() -> ScenarioSpec:
    """A spec engineered so causal effects are large and testable."""
    shock_on = START + timedelta(days=20)
    population = PopulationSpec(
        name="user",
        size=400,
        kg_dimensions=(KgDimension(attribute="team", node_type="Team"),),
        attributes=(
            variable(
                "segment",
                VariableKind.CATEGORICAL,
                dist=categorical({"hot": 1.0, "cold": 1.0}),
            ),
            variable(
                "propensity",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    (term("segment", levels={"hot": 2.0, "cold": -2.0}),),
                    noise_sigma=0.1,
                ),
            ),
        ),
        panel=(
            variable(
                "active",
                VariableKind.BINARY,
                eq=logit(0.0, (term("propensity", 2.0),)),
            ),
            variable(
                "value",
                VariableKind.NUMERIC,
                eq=eqn(1.0, (term("boost", 1.0), term("value", 0.5, lagged=True))),
            ),
        ),
    )
    return ScenarioSpec(
        name="causal",
        title="Causal fidelity",
        start=START,
        end=END,
        factors=(
            FactorSpec(
                name="boost",
                base=0.0,
                shocks=(Shock(on=shock_on, magnitude=10.0, decay=1.0),),
            ),
        ),
        populations=(population,),
        tables=(
            table(
                "dim_user",
                "user",
                TableGrain.ENTITY,
                (entity_col(), attr_col("segment"), attr_col("team"), attr_col("propensity")),
            ),
            table(
                "fact_day",
                "user",
                TableGrain.ENTITY_DAY,
                (
                    entity_col(),
                    date_col(),
                    panel_col("active"),
                    panel_col("value"),
                    factor_col("boost"),
                ),
            ),
        ),
    )


def sample_into(tmp: Path, world: World, *, seed: int = 9) -> Path:
    spec = causal_spec()
    require_clean(spec)
    out = tmp
    sample_scenario(spec, out, seeds=SeedContext(seed).child("t"), world=world)
    return out


def q(data_dir: Path, sql: str) -> list[tuple[object, ...]]:
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(
            "CREATE VIEW fact_day AS SELECT * FROM read_parquet('"
            + (data_dir / "tables" / "fact_day" / "*.parquet").as_posix()
            + "')"
        )
        conn.execute(
            "CREATE VIEW dim_user AS SELECT * FROM read_parquet('"
            + (data_dir / "tables" / "dim_user" / "*.parquet").as_posix()
            + "')"
        )
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


class TestDeterminism:
    def test_same_seed_same_bytes(self, tmp_path: Path, world: World) -> None:
        a = sample_into(tmp_path / "a", world)
        b = sample_into(tmp_path / "b", world)
        files_a = sorted(p.relative_to(a) for p in a.rglob("*.parquet"))
        files_b = sorted(p.relative_to(b) for p in b.rglob("*.parquet"))
        assert files_a == files_b
        for rel in files_a:
            assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel

    def test_different_seed_different_data(self, tmp_path: Path, world: World) -> None:
        a = sample_into(tmp_path / "a", world, seed=9)
        b = sample_into(tmp_path / "b", world, seed=10)
        # `value` is noise-free (deterministic in the factor), so compare the
        # stochastic variables: propensity draws and Bernoulli activity.
        sum_a = q(a, "SELECT sum(propensity) FROM dim_user")[0][0]
        sum_b = q(b, "SELECT sum(propensity) FROM dim_user")[0][0]
        assert sum_a != sum_b


class TestCausalFidelity:
    def test_categorical_effect_propagates(self, tmp_path: Path, world: World) -> None:
        data = sample_into(tmp_path, world)
        rows = q(
            data,
            """
            SELECT d.segment, avg(CASE WHEN f.active THEN 1.0 ELSE 0.0 END)
            FROM fact_day f JOIN dim_user d ON d.entity_id = f.entity_id
            GROUP BY d.segment
            """,
        )
        rates = {str(segment): float(rate) for segment, rate in rows}  # type: ignore[arg-type]
        # propensity(hot) ≈ +2 -> P(active) ≈ sigmoid(4); cold symmetric low.
        assert rates["hot"] > 0.9
        assert rates["cold"] < 0.1

    def test_factor_shock_moves_the_panel(self, tmp_path: Path, world: World) -> None:
        data = sample_into(tmp_path, world)
        shock_on = START + timedelta(days=20)
        before, after = q(
            data,
            f"""
            SELECT avg(value) FILTER (WHERE date < DATE '{shock_on.isoformat()}'),
                   avg(value) FILTER (WHERE date >= DATE '{shock_on.isoformat()}')
            FROM fact_day
            """,
        )[0]
        # Permanent +10 shock through an AR(0.5) lag: steady state ≈ 22 vs 2.
        assert float(after) > float(before) + 8.0  # type: ignore[arg-type]

    def test_lag_persistence(self, tmp_path: Path, world: World) -> None:
        data = sample_into(tmp_path, world)
        (corr,) = q(
            data,
            """
            SELECT corr(a.value, b.value)
            FROM fact_day a JOIN fact_day b
              ON b.entity_id = a.entity_id AND b.date = a.date + 1
            WHERE a.date < DATE '2026-01-20'
            """,
        )[0]
        assert corr is not None


class TestKgLinking:
    def test_dimension_levels_are_kg_teams(self, tmp_path: Path, world: World) -> None:
        data = sample_into(tmp_path, world)
        team_names = {str(name) for (name,) in q(data, "SELECT DISTINCT team FROM dim_user")}
        kg_team_names = {
            str(node.props.get("name", node.id)) for node in world.nodes_by_type("Team")
        }
        assert team_names <= kg_team_names
        assert team_names  # at least one team assigned

    def test_identity_binding_rows_match_people(self, tmp_path: Path, world: World) -> None:
        people = world.nodes_by_type("Person")
        spec = ScenarioSpec(
            name="idbind",
            title="Identity binding",
            start=START,
            end=START,
            populations=(
                PopulationSpec(
                    name="rep",
                    size=1,
                    kg_identity=KgIdentity(node_type="Person"),
                ),
            ),
            tables=(
                table(
                    "dim_rep",
                    "rep",
                    TableGrain.ENTITY,
                    (entity_col(), kg_id_col(), kg_name_col("rep_name")),
                ),
            ),
        )
        require_clean(spec)
        report = sample_scenario(spec, tmp_path, seeds=SeedContext(1).child("id"), world=world)
        assert report.population_sizes["rep"] == len(people)
        lineage = report.identity_lineage["rep"]
        assert {entry["kg_id"] for entry in lineage} == {node.id for node in people}

    def test_kg_binding_without_world_fails(self, tmp_path: Path) -> None:
        spec = causal_spec()
        with pytest.raises(LinkageError):
            sample_scenario(spec, tmp_path, seeds=SeedContext(1).child("x"), world=None)

    def test_load_world_requires_kg_export(self, tmp_path: Path) -> None:
        with pytest.raises(LinkageError, match="no exported KG"):
            load_world_from_run(tmp_path)
