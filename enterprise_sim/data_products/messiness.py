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
  ``<col>_v2`` from the window's midpoint on (written as a second part file).

Every change is recorded in ``quirks.json`` beside ``spec.json`` (gold): per
table, each injected cell or row with its key, column, original and new value.
Draws are seeded by ``(seed, scenario, table)``.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime
from pathlib import Path
from typing import Any

from enterprise_sim.core.config.diversity import DataMessinessConfig

__all__ = ["DataMessinessConfig", "apply_data_messiness"]


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    import pyarrow as pa

    keys = _key_columns(table)
    entity = next((c["name"] for c in table["columns"] if c["source"] == "entity_id"), None)
    day = next((c["name"] for c in table["columns"] if c["source"] == "date"), None)
    types = {f.name: f.type for f in schema}
    quirks: list[dict[str, Any]] = []

    def where(row: dict[str, Any]) -> dict[str, Any]:
        out = {}
        if entity:
            out[entity] = row.get(entity)
        if day:
            out[day] = _json(row.get(day))
        return out

    out_rows: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        for name, value in list(row.items()):
            if name in keys or value is None:
                continue
            kind = types[name]
            if cfg.null_rate and rng.random() < cfg.null_rate:
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
                new = value * 100 if rng.random() < 0.7 else -value
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
    report: dict[str, Any] = {"config": cfg.model_dump(), "tables": {}}
    window_mid = None
    if spec.get("start") and spec.get("end"):
        start, end = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])
        window_mid = start + (end - start) / 2
    for table in spec.get("tables", []):
        directory = data_dir / "tables" / table["name"]
        parts = sorted(directory.glob("*.parquet"))
        if not parts:
            continue
        arrow = pa.concat_tables([pq.read_table(p) for p in parts])
        rng = random.Random(f"{seed}\x00{spec.get('name')}\x00{table['name']}")
        rows, quirks, drift = _dirty_table(arrow.to_pylist(), table, cfg, rng, arrow.schema)
        for part in parts:
            part.unlink()
        day = next((c["name"] for c in table["columns"] if c["source"] == "date"), None)
        early, late = rows, []
        if drift is not None and day is not None and window_mid is not None:
            early = [r for r in rows if _as_date(r[day]) < window_mid]
            late = [r for r in rows if _as_date(r[day]) >= window_mid]
            for r in late:
                r[f"{drift}_v2"] = r.pop(drift)
            quirks.append(
                {
                    "kind": "schema_drift",
                    "row": {day: window_mid.isoformat()},
                    "column": drift,
                    "original": drift,
                    "surface": f"{drift}_v2",
                }
            )
        pq.write_table(
            pa.Table.from_pylist(early, schema=arrow.schema), directory / "part-00000.parquet"
        )
        if late:
            fields = [
                pa.field(f"{f.name}_v2", f.type) if f.name == drift else f for f in arrow.schema
            ]
            pq.write_table(
                pa.Table.from_pylist(late, schema=pa.schema(fields)),
                directory / "part-00001-drift.parquet",
            )
        report["tables"][table["name"]] = quirks
        for quirk in quirks:
            counts[quirk["kind"]] = counts.get(quirk["kind"], 0) + 1
    report["counts"] = counts
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
