"""Structured-data messiness: dirty raw tables over clean views, every change labelled.

Real warehouses are messy where the data lands and cleaner where it is
reported: dashboards are built on curated models while the raw tables carry
nulls, duplicated loads, inconsistent category labels, unit slips and columns
renamed mid-stream. :func:`apply_data_messiness` reproduces that split. It runs
after a scenario's views and question answers are final, and dirties only the
**tables** (``tables/<name>/part-*.parquet``), never the views, so

* a question answered from a view gets the clean answer (``questions.json``);
* the same question computed from the raw tables differs unless the analyst
  cleans them — which is the skill a data-analysis benchmark wants to measure.

Knobs (:class:`DataMessinessConfig`, all off by default):

* ``null_rate`` — per non-key cell, set to null;
* ``duplicate_rate`` — per row, the row is loaded twice;
* ``label_drift_rate`` — per text category cell: ``SMB``, ``Smb``, ``smb `` for ``smb``;
* ``outlier_rate`` — per numeric cell: x100 (a unit slip) or negated;
* ``schema_drift`` — one numeric column of each daily table is renamed
  ``<col>_v2`` from the window's midpoint on (written as a second part file);
* ``recipe`` — ``fixed`` (the above: midpoint, ``_v2``, x100 or negated) or
  ``varied``: each ``(scenario, table)`` draws its own drift day, renamed
  column pattern, unit factor (x10, x100, x1000) and share of sign flips, so
  sets built on different scenarios do not share one recipe;
* ``null_policy`` — ``any`` non-key cell; ``attributes``: only per-entity
  attribute columns, so day-varying measures stay complete; ``carry_forward``:
  only a cell whose value repeats the same entity's previous dated row, so the
  stated rule "a null repeats the entity's previous day" recovers every null
  exactly (after reverting outliers, which have their own rule).

Raw tables are dirty and views clean **by design** (curated models over raw
landing tables): a set must not ship a messy world's views as evidence of the
dirty data, since they hold the clean answers.

Every change is recorded in ``quirks.json`` beside ``spec.json`` (gold): per
table, each injected cell or row with its key, column, original and new value;
with a non-default recipe or null policy, also the table's drawn recipe
(``recipes``). Draws are seeded by ``(seed, scenario, table)``.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from enterprise_sim.core.config.diversity import DataMessinessConfig, NullPolicy, QuirkRecipe

__all__ = ["DataMessinessConfig", "apply_data_messiness"]


_DRIFT_NAMES = ("{c}_v2", "{c}_new", "new_{c}", "{c}2", "{c}_rev", "{c}_adj")
_UNIT_FACTORS = (10, 100, 1000)
_SCALE_SHARES = (0.5, 0.7, 0.9, 1.0)


@dataclass(frozen=True)
class _Recipe:
    """The shape of one table's quirks: unit slip, sign-flip share, drift name and day."""

    factor: int
    scale_share: float  # an outlier is a unit slip with this probability, else negated
    drift_name: str  # a format with {c} for the drifted column
    drift_day: date | None

    def renamed(self, column: str) -> str:
        return self.drift_name.format(c=column)

    def stated(self) -> dict[str, Any]:
        return {
            "unit_factor": self.factor,
            "sign_flip_share": round(1 - self.scale_share, 6),
            "drift_name": self.drift_name.replace("{c}", "<col>"),
            "drift_day": self.drift_day.isoformat() if self.drift_day else None,
        }


def _recipe(
    cfg: DataMessinessConfig, seed: int, scenario: Any, table: str, window: tuple[date, date] | None
) -> _Recipe:
    if cfg.recipe is QuirkRecipe.FIXED:
        mid = window[0] + (window[1] - window[0]) / 2 if window else None
        return _Recipe(100, 0.7, "{c}_v2", mid)
    rng = random.Random(f"{seed}\x00{scenario}\x00{table}\x00recipe")
    factor = rng.choice(_UNIT_FACTORS)
    share = rng.choice(_SCALE_SHARES)
    name = rng.choice(_DRIFT_NAMES)
    day = None
    if window:
        span = (window[1] - window[0]).days
        day = window[0] + timedelta(days=round(span * rng.uniform(0.2, 0.8)))
    return _Recipe(factor, share, name, day)


def _eligible_nulls(
    rows: list[dict[str, Any]],
    table: dict[str, Any],
    policy: NullPolicy,
    entity: str | None,
    day: str | None,
) -> set[tuple[int, str]] | None:
    """The ``(row index, column)`` cells a null may fall on; ``None`` = any cell."""
    if policy is NullPolicy.ANY:
        return None
    if policy is NullPolicy.ATTRIBUTES:
        names = {c["name"] for c in table["columns"] if c["source"] == "attribute"}
        return {(i, n) for i, row in enumerate(rows) for n in row if n in names}
    keys = _key_columns(table)
    if entity is None or day is None:
        return set()
    order = sorted(range(len(rows)), key=lambda i: (str(rows[i][entity]), _as_date(rows[i][day])))
    out: set[tuple[int, str]] = set()
    for before, index in zip(order, order[1:], strict=False):
        prev, row = rows[before], rows[index]
        if prev[entity] != row[entity]:
            continue
        for name, value in row.items():
            if name not in keys and value is not None and prev.get(name) == value:
                out.add((index, name))
    return out


_NULL_RULES = {
    NullPolicy.ATTRIBUTES: "nulls fall only in attribute columns; measures are complete",
    NullPolicy.CARRY_FORWARD: (
        "a null repeats the same entity's previous dated value (fill forward per entity "
        "by date, after reverting outliers)"
    ),
}


def _variants(label: str) -> list[str]:
    return [
        v
        for v in (label.upper(), label.title(), f"{label} ", label.replace("_", " "))
        if v != label
    ]


def _key_columns(table: dict[str, Any]) -> set[str]:
    return {c["name"] for c in table["columns"] if c["source"] in ("entity_id", "date", "kg_id")}


def _json(value: Any) -> Any:
    """A JSON-ready quirk value: dates as ISO text, floats rounded to 6 places.

    Full-precision floats print 15-17 digit runs, which text consumers' privacy
    checks read as card numbers; the gold only needs to identify the cell.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 6)
    return value


def _dirty_table(
    rows: list[dict[str, Any]],
    table: dict[str, Any],
    cfg: DataMessinessConfig,
    rng: random.Random,
    schema: Any,
    recipe: _Recipe,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    import pyarrow as pa

    keys = _key_columns(table)
    entity = next((c["name"] for c in table["columns"] if c["source"] == "entity_id"), None)
    day = next((c["name"] for c in table["columns"] if c["source"] == "date"), None)
    types = {f.name: f.type for f in schema}
    quirks: list[dict[str, Any]] = []
    eligible = _eligible_nulls(rows, table, cfg.null_policy, entity, day)

    def where(row: dict[str, Any]) -> dict[str, Any]:
        out = {}
        if entity:
            out[entity] = row.get(entity)
        if day:
            out[day] = _json(row.get(day))
        return out

    out_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        row = dict(row)
        for name, value in list(row.items()):
            if name in keys or value is None:
                continue
            kind = types[name]
            may_null = eligible is None or (index, name) in eligible
            if cfg.null_rate and may_null and rng.random() < cfg.null_rate:
                quirks.append(
                    {
                        "kind": "null",
                        "row": where(row),
                        "column": name,
                        "original": _json(value),
                        "surface": None,
                    }
                )
                row[name] = None
                continue
            numeric = pa.types.is_integer(kind) or pa.types.is_floating(kind)
            if (
                numeric
                and not pa.types.is_boolean(kind)
                and cfg.outlier_rate
                and rng.random() < cfg.outlier_rate
            ):
                new = value * recipe.factor if rng.random() < recipe.scale_share else -value
                quirks.append(
                    {
                        "kind": "outlier",
                        "row": where(row),
                        "column": name,
                        "original": _json(value),
                        "surface": _json(new),
                    }
                )
                row[name] = new
            elif (
                pa.types.is_string(kind)
                and cfg.label_drift_rate
                and rng.random() < cfg.label_drift_rate
            ):
                options = _variants(str(value))
                if (
                    options
                    and name != "kg_name"
                    and not str(value).startswith(("person:", "team:"))
                ):
                    new = rng.choice(options)
                    quirks.append(
                        {
                            "kind": "label_drift",
                            "row": where(row),
                            "column": name,
                            "original": _json(value),
                            "surface": _json(new),
                        }
                    )
                    row[name] = new
        out_rows.append(row)
        if cfg.duplicate_rate and rng.random() < cfg.duplicate_rate:
            quirks.append(
                {
                    "kind": "duplicate_row",
                    "row": where(row),
                    "column": None,
                    "original": None,
                    "surface": None,
                }
            )
            out_rows.append(dict(row))
    drift_col: str | None = None
    if cfg.schema_drift and day is not None:
        numeric_cols = [
            c["name"]
            for c in table["columns"]
            if c["name"] not in keys
            and (pa.types.is_integer(types[c["name"]]) or pa.types.is_floating(types[c["name"]]))
        ]
        if numeric_cols:
            drift_col = rng.choice(sorted(numeric_cols))
    return out_rows, quirks, drift_col


def apply_data_messiness(
    spec: dict[str, Any], data_dir: Path, cfg: DataMessinessConfig, *, seed: int
) -> dict[str, int]:
    """Dirty ``data_dir``'s raw tables per ``cfg``; write ``quirks.json``; return counts."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if cfg.clean:
        return {}
    counts: dict[str, int] = {}
    report: dict[str, Any] = {"config": cfg.stated(), "tables": {}}
    window = None
    if spec.get("start") and spec.get("end"):
        window = (date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"]))
    declare = cfg.recipe is not QuirkRecipe.FIXED or cfg.null_policy is not NullPolicy.ANY
    recipes: dict[str, Any] = {}
    for table in spec.get("tables", []):
        directory = data_dir / "tables" / table["name"]
        parts = sorted(directory.glob("*.parquet"))
        if not parts:
            continue
        arrow = pa.concat_tables([pq.read_table(p) for p in parts])
        rng = random.Random(f"{seed}\x00{spec.get('name')}\x00{table['name']}")
        recipe = _recipe(cfg, seed, spec.get("name"), table["name"], window)
        rows, quirks, drift = _dirty_table(arrow.to_pylist(), table, cfg, rng, arrow.schema, recipe)
        for part in parts:
            part.unlink()
        day = next((c["name"] for c in table["columns"] if c["source"] == "date"), None)
        early, late = rows, []
        drift_day = recipe.drift_day
        if drift is not None and day is not None and drift_day is not None:
            early = [r for r in rows if _as_date(r[day]) < drift_day]
            late = [r for r in rows if _as_date(r[day]) >= drift_day]
            for r in late:
                r[recipe.renamed(drift)] = r.pop(drift)
            quirks.append(
                {
                    "kind": "schema_drift",
                    "row": {day: drift_day.isoformat()},
                    "column": drift,
                    "original": drift,
                    "surface": recipe.renamed(drift),
                }
            )
        pq.write_table(
            pa.Table.from_pylist(early, schema=arrow.schema), directory / "part-00000.parquet"
        )
        if late:
            fields = [
                pa.field(recipe.renamed(f.name), f.type) if f.name == drift else f
                for f in arrow.schema
            ]
            pq.write_table(
                pa.Table.from_pylist(late, schema=pa.schema(fields)),
                directory / "part-00001-drift.parquet",
            )
        report["tables"][table["name"]] = quirks
        if declare:
            stated = recipe.stated()
            if not any(q["kind"] == "schema_drift" for q in quirks):
                stated["drift_day"] = stated["drift_name"] = None  # no drift in this table
            if cfg.null_policy is not NullPolicy.ANY:
                stated["null_rule"] = _NULL_RULES[cfg.null_policy]
                stated["null_columns"] = sorted(
                    {q["column"] for q in quirks if q["kind"] == "null"}
                )
            recipes[table["name"]] = stated
        for quirk in quirks:
            counts[quirk["kind"]] = counts.get(quirk["kind"], 0) + 1
    report["counts"] = counts
    if declare:
        report["recipes"] = recipes
    (data_dir / "quirks.json").write_text(
        json.dumps(report, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return counts


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
