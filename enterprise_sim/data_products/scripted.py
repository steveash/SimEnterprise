"""Scripted data scenarios: deterministic tables for a story the causal DSL cannot tell.

The DSL (:mod:`enterprise_sim.data_products.spec`) samples entity and entity-day
tables from equations. Some test sets need a specific causal story at a finer grain:
a run history with timestamps, where one opaque failure code keeps failing on reruns
until a credential rotation that only a vault audit log records; or an hourly latency
series that a regressing commit moves while a decoy commit does not. A scripted
scenario writes such tables as code: a pure function of the world, the seed and the
window.

* A plugin registers into :data:`SCRIPTED_SCENARIOS` with a ``name``, ``title`` and
  ``brief``, and ``build_data(world=, seed=, start=, end=) -> ScriptedData``.
* :func:`run_scripted` writes each table as parquet under ``tables/<name>/``, with
  explicit column types (``timestamp`` included). It writes a minimal ``spec.json``
  naming each table's primary key (the export reads it), materializes the views by SQL
  (byte-stable, as for DSL views), answers the questions into ``questions.json``,
  writes the scenario's gold (``truth.json``), and applies ``[data.messiness]`` to the
  tables.

``[data] scenarios`` names resolve in this registry or the DSL one; a name in both is
refused (:func:`enterprise_sim.data_products.runner.execute_data_run`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from enterprise_sim.core.registry import Registry
from enterprise_sim.core.world import World

__all__ = [
    "SCRIPTED_SCENARIOS",
    "ScriptedData",
    "ScriptedQuestion",
    "ScriptedScenario",
    "ScriptedTable",
    "run_scripted",
]

#: Arrow types a scripted column may use.
COLUMN_TYPES = ("string", "int64", "float64", "bool", "date", "timestamp")


@dataclass(frozen=True)
class ScriptedTable:
    """One table: typed columns, rows as dicts, a one-column primary key."""

    name: str
    description: str
    columns: tuple[tuple[str, str], ...]  # (name, one of COLUMN_TYPES)
    rows: tuple[dict[str, Any], ...]
    primary_key: str


@dataclass(frozen=True)
class ScriptedQuestion:
    id: str
    category: str
    question: str
    sql: str


@dataclass(frozen=True)
class ScriptedData:
    """What a scripted scenario produces: tables, views, questions and its gold."""

    tables: tuple[ScriptedTable, ...]
    views: tuple[tuple[str, str], ...] = ()  # (name, sql)
    questions: tuple[ScriptedQuestion, ...] = ()
    truth: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ScriptedScenario(Protocol):
    name: str
    title: str
    brief: str

    def build_data(self, *, world: World, seed: int, start: date, end: date) -> ScriptedData: ...


SCRIPTED_SCENARIOS: Registry[ScriptedScenario] = Registry("scripted data scenario")


def _arrow_type(kind: str) -> Any:
    import pyarrow as pa

    return {
        "string": pa.string(),
        "int64": pa.int64(),
        "float64": pa.float64(),
        "bool": pa.bool_(),
        "date": pa.date32(),
        "timestamp": pa.timestamp("s"),
    }[kind]


def _spec_document(
    name: str, title: str, brief: str, start: date, end: date, data: ScriptedData
) -> dict[str, Any]:
    """The minimal spec.json: each table's columns with the primary key as the entity id."""
    return {
        "name": name,
        "title": title,
        "description": brief,
        "scripted": True,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "tables": [
            {
                "name": table.name,
                "population": table.name,
                "grain": "entity",
                "description": table.description,
                "columns": [
                    {
                        "name": col,
                        "source": "entity_id" if col == table.primary_key else "attribute",
                    }
                    for col, _ in table.columns
                ],
            }
            for table in data.tables
        ],
        "views": [{"name": view, "sql": sql} for view, sql in data.views],
    }


def run_scripted(
    plugin: ScriptedScenario,
    world: World,
    data_dir: Path,
    *,
    seed: int,
    start: date,
    end: date,
    messiness: Any,
) -> tuple[dict[str, int], int, int, dict[str, Any]]:
    """Write the scenario into ``data_dir``; return (rows by table, questions, answerable, spec)."""
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    from enterprise_sim.data_products.messiness import apply_data_messiness
    from enterprise_sim.data_products.views import _register_parquet_view, _view_body

    data = plugin.build_data(world=world, seed=seed, start=start, end=end)
    for table in data.tables:
        if not any(col == table.primary_key for col, _ in table.columns):
            raise ValueError(f"{table.name}: primary key {table.primary_key!r} is not a column")
        keys = [row[table.primary_key] for row in table.rows]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{table.name}: primary key {table.primary_key!r} repeats")
    rows_by_table: dict[str, int] = {}
    for table in data.tables:
        directory = data_dir / "tables" / table.name
        directory.mkdir(parents=True, exist_ok=True)
        schema = pa.schema([(col, _arrow_type(kind)) for col, kind in table.columns])
        arrow = pa.Table.from_pylist(list(table.rows), schema=schema)
        pq.write_table(arrow, directory / "part-00000.parquet")
        rows_by_table[table.name] = len(table.rows)

    conn = duckdb.connect(":memory:")
    try:
        for table in data.tables:
            _register_parquet_view(conn, table.name, data_dir / "tables" / table.name)
        for name, sql in data.views:
            view_dir = data_dir / "views" / name
            view_dir.mkdir(parents=True, exist_ok=True)
            out = (view_dir / "part-00000.parquet").as_posix().replace("'", "''")
            conn.execute(
                f"COPY (SELECT * FROM ({_view_body(sql)}) __v ORDER BY ALL) "
                f"TO '{out}' (FORMAT PARQUET)"
            )
            _register_parquet_view(conn, name, view_dir)
        answered = []
        for q in data.questions:
            result = conn.execute(q.sql)
            columns = [d[0] for d in result.description or ()]
            rows = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
            answered.append(
                {
                    "id": q.id,
                    "category": q.category,
                    "question": q.question,
                    "sql": q.sql.strip(),
                    "answerable": True,
                    "answer": json.loads(json.dumps(rows, default=str)),
                }
            )
    finally:
        conn.close()

    spec = _spec_document(plugin.name, plugin.title, plugin.brief, start, end, data)
    (data_dir / "spec.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", "utf-8")
    payload = {"scenario": plugin.name, "title": plugin.title, "questions": answered}
    (data_dir / "questions.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8"
    )
    (data_dir / "truth.json").write_text(
        json.dumps(data.truth, indent=2, sort_keys=True, default=str) + "\n", "utf-8"
    )
    apply_data_messiness(spec, data_dir, messiness, seed=seed)
    return rows_by_table, len(answered), len(answered), spec


def names() -> Sequence[str]:
    return tuple(SCRIPTED_SCENARIOS.names())


def load_builtin() -> None:
    """Import the built-in scripted scenarios so they register (idempotent)."""
    import importlib

    for module in ("pipeline_runs", "service_latency"):
        importlib.import_module(f"enterprise_sim.data_products.scripted_scenarios.{module}")
