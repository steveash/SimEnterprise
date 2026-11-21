"""Text-only world export (format ``simenterprise-export/1``).

A consumer that can only read text (for example a dataset catalog whose privacy
check scans every file as UTF-8) cannot take a world folder as it is: the folder
holds ``.docx``/``.pptx`` documents, parquet tables and DuckDB views, plus
bookkeeping (``.world``, ``.jobs/``, ``.cache/``) that changes on every build.

:func:`export_world` writes one directory per world, ``<out>/<world id>/``, with

* every text file of the world copied as is (``.md``, ``.json``, ``.jsonl``,
  ``.csv``), with CRLF normalized to LF;
* a Markdown rendering beside every binary document (``x.docx`` → ``x.docx.md``,
  ``x.pptx`` → ``x.pptx.md``) carrying the body text and the review comments;
* a CSV rendering of every parquet table and view (``tables/<name>.csv``), with
  its column schema, primary key and foreign keys recorded in ``export.json``;
* ``export.json``: the format tag, the world identity (id, config digest, seed,
  prose backend, SimEnterprise version and commit) and every file with its path,
  media type, sha256, byte size and **role**.

Roles split what an agent may read from the answer key: ``data`` (documents,
organization pages, tables) and ``gold`` (the knowledge graph, eval questions,
validation issues, the effective config and each data scenario's causal spec,
lineage and questions). A consumer keeps ``gold`` files controller-only.

The export is deterministic: the same world yields byte-identical files. The
build timestamps and job ledger in ``.world`` are not exported; ``export.json``
is canonical JSON (sorted keys, compact separators, UTF-8, no trailing newline),
so its sha256 is the export digest.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from enterprise_sim import __version__
from enterprise_sim.worlds.file import read_world

__all__ = [
    "EXPORT_FORMAT",
    "ExportError",
    "ExportResult",
    "export_world",
    "prose_backend_of",
]

EXPORT_FORMAT = "simenterprise-export/1"

#: Text files copied verbatim (after CRLF → LF), keyed by suffix → media type.
_TEXT_MEDIA: dict[str, str] = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".csv": "text/csv",
}

#: Top-level world entries that are bookkeeping, never exported.
_SKIP_TOP: frozenset[str] = frozenset({".world", ".jobs", ".cache", ".control"})

#: Derived interop renderings of the gold KG (rebuildable from ``kg/*.jsonl``).
_SKIP_PREFIXES: tuple[str, ...] = ("kg/neo4j/",)

#: Paths (relative, POSIX) that are the answer key and must stay controller-only.
_GOLD_PREFIXES: tuple[str, ...] = ("kg/", "evals/", "validation/")
_GOLD_DATA_FILES: frozenset[str] = frozenset({"spec.json", "lineage.json", "questions.json"})

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ExportError(Exception):
    """Raised when a world cannot be exported (not a world, or an unknown file)."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Where the export landed and its digest (sha256 of ``export.json``)."""

    directory: Path
    digest: str
    files: int


def prose_backend_of(render_backend: str) -> str:
    """Classify a world's render backend as ``fake``, ``deterministic`` or ``live``.

    ``fake`` echoes prompts (structure only, no facts in the prose); a
    ``deterministic`` backend writes fact-bearing prose reproducibly from the
    seed; anything else called a real model and is ``live``.
    """
    if render_backend == "fake":
        return "fake"
    if render_backend in _DETERMINISTIC_BACKENDS:
        return "deterministic"
    return "live"


#: Render backends that write fact-bearing prose without a model.
_DETERMINISTIC_BACKENDS: frozenset[str] = frozenset({"synth"})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _role(rel: str) -> str:
    if rel.startswith(_GOLD_PREFIXES):
        return "gold"
    parts = rel.split("/")
    if parts[0] == "data" and len(parts) == 3 and parts[2] in _GOLD_DATA_FILES:
        return "gold"
    if rel == "config.json":
        return "gold"
    return "data"


def _git_commit(repo: Path) -> tuple[str, bool]:
    """The SimEnterprise checkout's commit and whether its tree is dirty."""
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown", False
    return head, bool(status)


# -- document renderings ------------------------------------------------------ #


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    return "".join(node.text or "" for node in paragraph.iter(f"{_W}t"))


def render_docx(path: Path) -> str:
    """A Markdown rendering of a ``.docx``: body paragraphs, then its comments."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        body = ElementTree.fromstring(archive.read("word/document.xml"))
        comments = (
            ElementTree.fromstring(archive.read("word/comments.xml"))
            if "word/comments.xml" in names
            else None
        )
    lines: list[str] = []
    for index, paragraph in enumerate(body.iter(f"{_W}p")):
        text = _paragraph_text(paragraph).strip()
        if not text:
            continue
        lines.append(f"# {text}" if index == 0 else text)
        lines.append("")
    if comments is not None:
        entries = list(comments.iter(f"{_W}comment"))
        if entries:
            lines.extend(["## Review comments", ""])
        for comment in entries:
            author = comment.get(f"{_W}author", "")
            date = comment.get(f"{_W}date", "")
            text = " ".join(
                _paragraph_text(p).strip() for p in comment.iter(f"{_W}p") if _paragraph_text(p)
            )
            lines.append(f"- **{author}** ({date}): {text}")
        if entries:
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_pptx(path: Path) -> str:
    """A Markdown rendering of a ``.pptx``: one section per slide, then notes."""
    from pptx import Presentation  # python-pptx: a base dependency

    deck = Presentation(str(path))
    lines: list[str] = []
    for number, slide in enumerate(deck.slides, start=1):
        lines.extend([f"## Slide {number}", ""])
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if text:
                        lines.append(text)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes is not None and notes.text.strip():
                lines.extend(["", f"Notes: {notes.text.strip()}"])
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


_RENDERERS = {".docx": render_docx, ".pptx": render_pptx}


# -- tables -------------------------------------------------------------------- #


def _column_type(arrow_type: Any) -> str:
    """Map an arrow type to the export's column types."""
    import pyarrow as pa

    if pa.types.is_integer(arrow_type):
        return "integer"
    if pa.types.is_floating(arrow_type) or pa.types.is_decimal(arrow_type):
        return "decimal"
    if pa.types.is_date(arrow_type):
        return "date"
    if pa.types.is_timestamp(arrow_type):
        return "timestamp"
    # strings, booleans (written as ``true``/``false``) and anything else.
    return "text"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        return repr(value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _csv_line(cells: list[str]) -> str:
    out: list[str] = []
    for cell in cells:
        if any(ch in cell for ch in (",", '"', "\n", "\r")):
            cell = '"' + cell.replace('"', '""') + '"'
        out.append(cell)
    return ",".join(out)


def _table_csv(directory: Path) -> tuple[str, list[dict[str, str]]]:
    """Render every parquet part under ``directory`` as one CSV (header row first)."""
    import pyarrow.parquet as pq

    parts = sorted(directory.rglob("*.parquet"))
    if not parts:
        raise ExportError(f"{directory}: no parquet parts")
    tables = [pq.read_table(part) for part in parts]
    schema = tables[0].schema
    columns = [{"name": f.name, "type": _column_type(f.type)} for f in schema]
    lines = [_csv_line([f.name for f in schema])]
    for table in tables:
        for row in table.to_pylist():
            lines.append(_csv_line([_cell(row[f.name]) for f in schema]))
    return "\n".join(lines) + "\n", columns


def _table_keys(spec: dict[str, Any], table: str) -> tuple[list[str], list[dict[str, Any]]]:
    """The primary key and foreign keys of a spec table (views have neither).

    An ``entity`` table is keyed by its entity-id column; an ``entity_day`` table
    by (entity id, date), and its entity-id column references the ``entity``
    table of the same population when the scenario has one.
    """
    tables = {t["name"]: t for t in spec.get("tables", [])}
    found = tables.get(table)
    if found is None:
        return [], []

    def col(t: dict[str, Any], source: str) -> str | None:
        for c in t["columns"]:
            if c["source"] == source:
                return str(c["name"])
        return None

    entity_col = col(found, "entity_id")
    if entity_col is None:
        return [], []
    if found["grain"] == "entity":
        return [entity_col], []
    date_col = col(found, "date")
    primary = [entity_col] + ([date_col] if date_col else [])
    foreign: list[dict[str, Any]] = []
    for other in spec.get("tables", []):
        if other["population"] == found["population"] and other["grain"] == "entity":
            target = col(other, "entity_id")
            if target is not None:
                foreign.append(
                    {"columns": [entity_col], "table": other["name"], "references": [target]}
                )
            break
    return primary, foreign


# -- the export ---------------------------------------------------------------- #


class _Writer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []

    def write(self, rel: str, data: bytes, media: str, **extra: Any) -> None:
        if b"\r" in data:
            raise ExportError(f"{rel}: carriage return in exported text")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExportError(f"{rel}: not UTF-8 text") from exc
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entry: dict[str, Any] = {
            "path": rel,
            "media_type": media,
            "sha256": _sha256(data),
            "bytes": len(data),
            "role": _role(rel),
        }
        entry.update(extra)
        self.files.append(entry)


def _world_files(world_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(world_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(world_dir).as_posix()
        if rel.split("/")[0] in _SKIP_TOP or rel.startswith(_SKIP_PREFIXES):
            continue
        files.append(path)
    return files


def export_world(world_dir: Path | str, out_root: Path | str) -> ExportResult:
    """Export ``world_dir`` as ``<out_root>/<world id>/`` (``simenterprise-export/1``).

    Refuses a directory that is not a world, a world whose build is not done, an
    existing non-empty target, and any file it does not know how to render as text.
    """
    world_dir = Path(world_dir)
    world = read_world(world_dir)
    if world is None:
        raise ExportError(f"{world_dir}: not a world (no .world file)")
    if world.job.status not in ("done", ""):
        raise ExportError(f"{world_dir}: build is {world.job.status!r}, not done")
    target = Path(out_root) / world.world_id
    if target.exists() and any(target.iterdir()):
        raise ExportError(f"{target}: already exists and is not empty")
    target.mkdir(parents=True, exist_ok=True)
    writer = _Writer(target)

    config = json.dumps(world.config, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    writer.write("config.json", config.encode("utf-8"), "application/json")

    data_dirs: set[Path] = set()
    for path in _world_files(world_dir):
        rel = path.relative_to(world_dir).as_posix()
        parts = rel.split("/")
        if parts[0] == "data" and len(parts) >= 4 and parts[2] in ("tables", "views"):
            data_dirs.add(world_dir.joinpath(*parts[:4]))
            continue
        suffix = path.suffix.lower()
        if suffix in _TEXT_MEDIA:
            writer.write(rel, _lf(path.read_bytes()), _TEXT_MEDIA[suffix])
        elif suffix in _RENDERERS:
            text = _RENDERERS[suffix](path)
            writer.write(
                f"{rel}.md",
                text.encode("utf-8"),
                "text/markdown",
                rendered_from=rel,
                source_sha256=_sha256(path.read_bytes()),
            )
        elif suffix == ".cypher":
            continue
        else:
            raise ExportError(f"{rel}: no text rendering for {suffix or 'files without a suffix'}")

    for directory in sorted(data_dirs):
        scenario, kind, name = directory.relative_to(world_dir / "data").parts
        spec_path = world_dir / "data" / scenario / "spec.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8")) if spec_path.is_file() else {}
        csv_text, columns = _table_csv(directory)
        rel = f"data/{scenario}/{kind}/{name}.csv"
        writer.write(rel, csv_text.encode("utf-8"), "text/csv")
        primary, foreign = _table_keys(spec, name) if kind == "tables" else ([], [])
        for fk in foreign:
            fk["table"] = f"{scenario}.{fk['table']}"
        writer.tables.append(
            {
                "name": f"{scenario}.{name}",
                "path": rel,
                "kind": "table" if kind == "tables" else "view",
                "columns": columns,
                "primary_key": primary,
                "foreign_keys": foreign,
            }
        )

    commit, dirty = _git_commit(Path(__file__).resolve().parents[2])
    manifest = {
        "format": EXPORT_FORMAT,
        "world_id": world.world_id,
        "company": world.company,
        "config_digest": world.config_digest,
        "seed": world.seed,
        "window": world.window,
        "prose_backend": prose_backend_of(world.render_backend),
        "render_backend": world.render_backend,
        "simenterprise": {"version": __version__, "commit": commit, "dirty": dirty},
        "counts": world.counts,
        "files": sorted(writer.files, key=lambda f: f["path"]),
        "tables": sorted(writer.tables, key=lambda t: t["name"]),
    }
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    (target / "export.json").write_bytes(body.encode("utf-8"))
    return ExportResult(target, _sha256(body.encode("utf-8")), len(writer.files))
