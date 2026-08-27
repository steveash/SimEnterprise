"""Materialize SQL views over sampled parquet tables (DuckDB).

DuckDB is the scenario's query engine: every sampled table is registered as a
view over its parquet partition glob, each :class:`ViewSpec` is executed in
declaration order (so later views can build on earlier ones) and COPY'd to
``views/<name>/part-00000.parquet``, and the same connection layout is used by
the question loop to evaluate answerability. Nothing here mutates the spec —
views are pure derivations, the "materialized views" half of the data product.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from enterprise_sim.data_products.spec import (
    ColumnSource,
    ScenarioSpec,
    TableSpec,
    VariableKind,
)

__all__ = [
    "MaterializeError",
    "ViewResult",
    "connect_scenario",
    "materialize_views",
    "validate_view_sql",
]


class MaterializeError(Exception):
    """Raised when a view's SQL fails to plan or execute."""


def _register_parquet_view(conn: duckdb.DuckDBPyConnection, name: str, directory: Path) -> None:
    # CREATE VIEW cannot be a prepared statement, so the glob is inlined with
    # SQL single-quote escaping.
    glob = (directory / "*.parquet").as_posix().replace("'", "''")
    conn.execute(f"CREATE VIEW \"{name}\" AS SELECT * FROM read_parquet('{glob}')")


@dataclass(frozen=True, slots=True)
class ViewResult:
    """One materialized view: where it landed and how many rows it holds."""

    name: str
    path: Path
    rows: int


def connect_scenario(
    spec: ScenarioSpec, data_dir: Path, *, include_views: bool = True
) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB connection with every table (and view) registered.

    Tables resolve to their parquet partition globs under
    ``data_dir/tables/<name>/``; materialized views (when ``include_views``)
    resolve to ``data_dir/views/<name>/``. The caller owns closing the
    connection.
    """
    conn = duckdb.connect(":memory:")
    for table in spec.tables:
        _register_parquet_view(conn, table.name, data_dir / "tables" / table.name)
    if include_views:
        for view in spec.views:
            view_dir = data_dir / "views" / view.name
            if view_dir.is_dir():
                _register_parquet_view(conn, view.name, view_dir)
    return conn


def materialize_views(spec: ScenarioSpec, data_dir: Path) -> list[ViewResult]:
    """Execute every view's SQL and write it to ``data_dir/views/<name>/``.

    Views run in declaration order against a connection where all tables and
    previously-materialized views are visible, so aggregation views can stack
    (daily → weekly → monthly). A failing view raises :class:`MaterializeError`
    naming the view — an LLM-authored view with bad SQL should fail loudly at
    materialize time, not silently produce nothing.
    """
    results: list[ViewResult] = []
    conn = connect_scenario(spec, data_dir, include_views=False)
    try:
        for view in spec.views:
            view_dir = data_dir / "views" / view.name
            view_dir.mkdir(parents=True, exist_ok=True)
            out_path = view_dir / "part-00000.parquet"
            # DuckDB's parallel aggregation emits rows in nondeterministic
            # order; a total ORDER BY ALL canonicalizes the materialized file
            # so identical runs stay byte-identical.
            escaped = out_path.as_posix().replace("'", "''")
            try:
                conn.execute(
                    f"COPY (SELECT * FROM ({_view_body(view.sql)}) __v ORDER BY ALL) "
                    f"TO '{escaped}' (FORMAT PARQUET)"
                )
            except duckdb.Error as exc:
                raise MaterializeError(f"view {view.name!r} failed: {exc}") from exc
            rows_row = conn.execute(
                "SELECT COUNT(*) FROM read_parquet(?)", [out_path.as_posix()]
            ).fetchone()
            rows = int(rows_row[0]) if rows_row is not None else 0
            # Make the view queryable by the ones declared after it.
            _register_parquet_view(conn, view.name, view_dir)
            results.append(ViewResult(name=view.name, path=out_path, rows=rows))
    finally:
        conn.close()
    return results


def _view_body(sql: str) -> str:
    """A view's SQL normalized for subquery wrapping (no trailing semicolons)."""
    return sql.strip().rstrip(";").strip()


def _empty_column_type(table: TableSpec, kind_of: dict[str, VariableKind]) -> dict[str, str]:
    types: dict[str, str] = {}
    for col in table.columns:
        if col.source in (ColumnSource.ENTITY_ID, ColumnSource.KG_ID, ColumnSource.KG_NAME):
            types[col.name] = "VARCHAR"
        elif col.source is ColumnSource.DATE:
            types[col.name] = "DATE"
        elif col.source is ColumnSource.FACTOR:
            types[col.name] = "DOUBLE"
        else:
            kind = kind_of.get(col.ref)
            if kind is VariableKind.CATEGORICAL:
                types[col.name] = "VARCHAR"
            elif kind is VariableKind.COUNT:
                types[col.name] = "BIGINT"
            elif kind is VariableKind.BINARY:
                types[col.name] = "BOOLEAN"
            else:
                types[col.name] = "DOUBLE"
    return types


def validate_view_sql(spec: ScenarioSpec) -> list[str]:
    """Dry-run every view's SQL against empty schema-shaped tables.

    Catches what the static lint cannot — parse errors, unknown columns/tables,
    type mismatches — *before* any sampling cost, which is what makes
    LLM-authored views safe to accept. Views are created in declaration order
    so later views may reference earlier ones. Returns one message per failing
    view (empty list = all valid).
    """
    errors: list[str] = []
    conn = duckdb.connect(":memory:")
    try:
        for table in spec.tables:
            kind_of = {
                var.name: var.kind
                for pop in spec.populations
                if pop.name == table.population
                for var in (*pop.attributes, *pop.panel)
            }
            columns = ", ".join(
                f'"{name}" {dtype}' for name, dtype in _empty_column_type(table, kind_of).items()
            )
            conn.execute(f'CREATE TABLE "{table.name}" ({columns})')
        for view in spec.views:
            body = _view_body(view.sql)
            try:
                conn.execute(f"EXPLAIN SELECT * FROM ({body}) __v")
                conn.execute(f'CREATE VIEW "{view.name}" AS {body}')
            except duckdb.Error as exc:
                errors.append(f"view {view.name!r}: {exc}")
    finally:
        conn.close()
    return errors
