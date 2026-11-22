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
  prose backend, SimEnterprise version and commit), every file with its path,
  media type, sha256, byte size and **role**, and ``slices``: one per project,
  ``{name, description, files}`` (see :mod:`enterprise_sim.worlds.slice`).

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
import re
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from enterprise_sim import __version__
from enterprise_sim.worlds.file import read_world

__all__ = [
    "EXPORT_FORMAT",
    "REPOSITORY",
    "ExportError",
    "ExportResult",
    "export_world",
    "canonical_decimal",
    "export_path",
    "identity_shapes",
    "prose_backend_of",
]

EXPORT_FORMAT = "simenterprise-export/1"

#: A stable public identifier of the generator's repository (the project name,
#: never a host path or an account): with ``commit`` it names the code that
#: built an export.
REPOSITORY = "SimEnterprise"

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
    # data/<scenario>/<file>: the causal spec (every iteration), lineage and
    # questions with their SQL answers; only tables/ and views/ are data.
    if parts[0] == "data" and len(parts) == 3:
        return "gold"
    if rel == "config.json" or rel.startswith("replay/"):
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


_ADDRESS = re.compile(r"\s*<[^>]*@[^>]*>|\b[\w.+-]+@[\w.-]+\b")


def _names_only(value: str) -> str:
    """A header value with every mailbox address removed (display names kept)."""
    return " ".join(_ADDRESS.sub("", value).split()).strip(" ,") or "(unnamed)"


def render_eml(path: Path) -> str:
    """A Markdown rendering of an ``.eml`` thread: each message's names, date, subject, body.

    Addresses are dropped (a catalog refuses e-mail-shaped values); display names
    stay, which is what a reader of the thread needs.
    """
    from email import policy
    from email.parser import BytesParser

    raw = path.read_bytes()
    # A thread file holds several RFC 5322 messages, separated by mbox "From " lines.
    chunks = [c for c in re.split(rb"(?m)^From .*\n", raw) if c.strip()] or [raw]
    lines: list[str] = []
    for number, chunk in enumerate(chunks, start=1):
        message = BytesParser(policy=policy.default).parsebytes(chunk)
        subject = str(message.get("Subject", "")).strip()
        if number == 1:
            lines += [f"# {subject or path.stem}", ""]
        lines += [f"## Message {number}", ""]
        for header in ("From", "To", "Cc", "Date"):
            value = message.get(header)
            if value:
                lines.append(f"**{header}:** {_names_only(str(value))}")
        body = message.get_body(preferencelist=("plain",))
        text = body.get_content() if body is not None else ""
        lines += ["", _ADDRESS.sub("", str(text)).strip(), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def render_ics(path: Path) -> str:
    """A Markdown rendering of an ``.ics`` invite: summary, time, organizer, attendees, agenda."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    text = re.sub(r"\n[ \t]", "", text)  # RFC 5545 line unfolding
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        name, _, value = line.partition(":")
        key = name.split(";", 1)[0]
        cn = re.search(r"CN=([^;:]+)", name)
        fields.setdefault(key, []).append(cn.group(1).strip('"') if cn else value)

    def one(key: str) -> str:
        return fields.get(key, [""])[0]

    agenda = one("DESCRIPTION").replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")
    lines = [
        f"# {one('SUMMARY') or path.stem}",
        "",
        f"**When:** {one('DTSTART')} to {one('DTEND')}",
        f"**Organizer:** {_names_only(one('ORGANIZER'))}",
        f"**Attendees:** {', '.join(_names_only(a) for a in fields.get('ATTENDEE', []))}",
        "",
        _ADDRESS.sub("", agenda).strip(),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def render_code(path: Path) -> str:
    """A Markdown rendering of a source or config file: its path, then the file, fenced."""
    language = {".py": "python", ".toml": "toml"}.get(path.suffix, "")
    text = path.read_text(encoding="utf-8").rstrip("\n")
    return f"# {path.name}\n\n```{language}\n{text}\n```\n"


_RENDERERS = {
    ".docx": render_docx,
    ".pptx": render_pptx,
    ".eml": render_eml,
    ".ics": render_ics,
    ".py": render_code,
    ".toml": render_code,
}


# -- tables -------------------------------------------------------------------- #


def _column_type(arrow_type: Any) -> str:
    """Map an arrow type to the export's column types."""
    import pyarrow as pa

    if pa.types.is_boolean(arrow_type):
        return "boolean"
    if pa.types.is_integer(arrow_type):
        return "integer"
    if pa.types.is_floating(arrow_type) or pa.types.is_decimal(arrow_type):
        return "decimal"
    if pa.types.is_date(arrow_type):
        return "date"
    if pa.types.is_timestamp(arrow_type):
        return "timestamp"
    return "text"


#: Decimal cells keep at most this many fraction digits and this many digits in
#: all, so no cell is a long digit run (the consumer's privacy pack reads long
#: runs as card numbers) and the text stays a faithful rounding of the sample.
_MAX_FRACTION_DIGITS = 6
_MAX_DIGITS = 12


def canonical_decimal(value: float | Decimal) -> str:
    """Fixed notation, rounded, no exponent, no trailing zeros, no ``-0``.

    Built from the shortest round-trip ``repr``; rounded half-even to at most
    :data:`_MAX_FRACTION_DIGITS` fraction digits and :data:`_MAX_DIGITS` digits
    in all (``1e-05`` → ``0.00001``, ``3191.068163337267`` → ``3191.068163``).
    """
    number = Decimal(repr(value)) if isinstance(value, float) else value
    int_digits = len(str(abs(int(number)))) if abs(number) >= 1 else 0
    places = max(0, min(_MAX_FRACTION_DIGITS, _MAX_DIGITS - int_digits))
    number = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", "", "-"):
        return "0"
    return text


def _cell(value: Any, column_type: str, where: str) -> str:
    if value is None:
        return ""
    if column_type == "boolean":
        return "true" if value else "false"
    if column_type == "decimal":
        if isinstance(value, float) and (value != value or value in (_INF, -_INF)):
            return ""
        return canonical_decimal(value)
    if column_type == "integer":
        return str(int(value))
    if column_type == "date":
        if isinstance(value, datetime):
            value = value.date()
        return str(value.isoformat())
    if column_type == "timestamp":
        stamp = value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
        return str(stamp.strftime("%Y-%m-%dT%H:%M:%SZ"))
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ExportError(f"{where}: a cell holds a line break")
    return text


_INF = float("inf")


def _csv_line(cells: list[str]) -> str:
    """One RFC 4180 row: quote a cell only when it holds a comma or a quote."""
    out: list[str] = []
    for cell in cells:
        if "," in cell or '"' in cell:
            cell = '"' + cell.replace('"', '""') + '"'
        out.append(cell)
    return ",".join(out)


def _table_csv(directory: Path, where: str) -> tuple[str, list[dict[str, str]]]:
    """Render every parquet part under ``directory`` as one CSV (header row first).

    A timestamp column whose every value is a midnight is a date column (weekly
    and monthly rollups truncate to the day) and is typed and written as one.
    """
    import pyarrow.parquet as pq

    parts = sorted(directory.rglob("*.parquet"))
    if not parts:
        raise ExportError(f"{directory}: no parquet parts")
    tables = [pq.read_table(part) for part in parts]
    # Parts may differ in columns (schema drift): the union, first seen first.
    fields: dict[str, Any] = {}
    for table in tables:
        for f in table.schema:
            fields.setdefault(f.name, f)
    schema = list(fields.values())
    rows = [
        {f.name: row.get(f.name) for f in schema} for table in tables for row in table.to_pylist()
    ]
    types = {f.name: _column_type(f.type) for f in schema}
    for name, kind in types.items():
        if kind == "timestamp":
            stamps = [row[name] for row in rows if row[name] is not None]
            if all(s.time() == time(0, 0) for s in stamps):
                types[name] = "date"
    columns = [{"name": f.name, "type": types[f.name]} for f in schema]
    lines = [_csv_line([f.name for f in schema])]
    for row in rows:
        lines.append(_csv_line([_cell(row[f.name], types[f.name], where) for f in schema]))
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


def _duplicated_tables(scenario_dir: Path) -> set[str]:
    """Raw tables of a scenario that ``[data.messiness]`` gave duplicate rows."""
    path = scenario_dir / "quirks.json"
    if not path.is_file():
        return set()
    report = json.loads(path.read_text(encoding="utf-8"))
    return {
        table
        for table, quirks in report.get("tables", {}).items()
        if any(q.get("kind") == "duplicate_row" for q in quirks)
    }


# -- the export ---------------------------------------------------------------- #


#: Export paths: this charset, file names <= 90 bytes, paths <= 160 bytes (the
#: consumer's limit is 200 including its own prefix; ustar names fit).
_PATH_CHARS = re.compile(r"[^A-Za-z0-9._/-]")
_MAX_NAME = 90
_MAX_PATH = 160


def export_path(rel: str) -> str:
    """A short, stable export path for world path ``rel`` (``rel`` itself if it fits).

    Over-long or out-of-charset names keep a readable prefix and gain a hash of
    the full world path, so two long names never collide and the mapping is a
    pure function of the path.
    """
    if not _PATH_CHARS.search(rel) and len(rel) <= _MAX_PATH:
        if all(len(part) <= _MAX_NAME for part in rel.split("/")):
            return rel
    tag = _sha256(rel.encode("utf-8"))[:10]
    *dirs, name = rel.split("/")
    short_dirs = [_PATH_CHARS.sub("-", d)[:40] for d in dirs]
    dot = name.find(".", 1)
    stem, suffix = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    suffix = _PATH_CHARS.sub("-", suffix)[:20]
    stem = _PATH_CHARS.sub("-", stem)[:40].rstrip("-.")
    return "/".join([*short_dirs, f"{stem}-{tag}{suffix}"])


#: Identity shapes no exported file may carry (the consumer's privacy pack
#: refuses them too; failing here names the world file that holds one).
_IDENTITY_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "email address",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    ),
    ("phone number", re.compile(r"(?<![\w-])\+?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-])")),
    ("SSN", re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")),
)
#: Card shapes, as the consumer's privacy pack reads them: one maximal digit run
#: of 15 or 16 digits (whatever surrounds it, a decimal point included), or a
#: card grouping with one consistent separator; either must pass Luhn.
_CARD_RUN = re.compile(r"\d+")
_CARD_GROUPED = re.compile(
    r"(?<!\d)(?:\d{4}([ -])\d{4}\1\d{4}\1\d{1,4}"
    r"|\d{4}([ -])\d{4}\2\d{4}\2\d{4}\2\d{1,3}"
    r"|\d{4}([ -])\d{6}\3\d{4,5})(?!\d)"
)


#: JSON keys and column names that read as secret assignments to a privacy scan
#: (``"token": "..."``): a name whose last word is one of these, or one of the
#: compound names. Plain ``key`` (a Jira issue key, ``primary_key``) is not one.
_SECRET_WORDS = frozenset(
    {"token", "tokens", "secret", "secrets", "password", "passwd", "credential", "credentials"}
)
_SECRET_NAMES = frozenset(
    {"apikey", "api_key", "access_key", "auth_token", "private_key", "secret_key", "auth"}
)


def _secret_name(name: str) -> bool:
    lowered = name.casefold()
    words = [w for w in re.split(r"[^a-z0-9]+", lowered) if w]
    return lowered in _SECRET_NAMES or (bool(words) and words[-1] in _SECRET_WORDS)


def secret_names(text: str, media: str) -> list[str]:
    """Secret-sounding JSON keys (json/jsonl) or CSV header names in ``text``."""
    if media == "text/csv":
        header = text.split("\n", 1)[0]
        return sorted({c for c in header.split(",") if _secret_name(c)})
    if media in ("application/json", "application/x-ndjson"):
        keys = re.findall(r'"((?:[^"\\]|\\.)*)"\s*:', text)
        return sorted({k for k in keys if _secret_name(k)})
    return []


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def identity_shapes(text: str) -> list[str]:
    """Names of the identity shapes found in ``text`` (empty when clean)."""
    found = [name for name, pattern in _IDENTITY_SHAPES if pattern.search(text)]
    runs = (m.group(0) for m in _CARD_RUN.finditer(text) if len(m.group(0)) in (15, 16))
    grouped = (re.sub(r"\D", "", m.group(0)) for m in _CARD_GROUPED.finditer(text))
    if any(_luhn(digits) for digits in (*runs, *grouped)):
        found.append("card number")
    return found


class _Writer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self.paths: set[str] = set()

    def write(self, rel: str, data: bytes, media: str, **extra: Any) -> str:
        if b"\r" in data:
            raise ExportError(f"{rel}: carriage return in exported text")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExportError(f"{rel}: not UTF-8 text") from exc
        names = secret_names(data.decode("utf-8"), media)
        if names:
            raise ExportError(f"{rel}: secret-like key or column names {names[:3]}")
        shapes = identity_shapes(data.decode("utf-8"))
        if shapes:
            raise ExportError(f"{rel}: holds a value shaped like {', '.join(shapes)}")
        source = rel
        rel = export_path(rel)
        if rel != source:
            extra.setdefault("source_path", source)
        if rel in self.paths:
            raise ExportError(f"{source}: export path {rel} is taken")
        self.paths.add(rel)
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entry: dict[str, Any] = {
            "path": rel,
            "media_type": media,
            "sha256": _sha256(data),
            "bytes": len(data),
            "role": _role(source),
        }
        entry.update(extra)
        self.files.append(entry)
        return rel


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


#: Usage keys of a cached response, renamed in the replay file so no key reads
#: as a secret ("input_tokens") to a privacy scan; ``worlds.replay`` maps back.
REPLAY_USAGE_KEYS = {
    "input_tokens": "in",
    "cached_input_tokens": "cached_in",
    "output_tokens": "out",
}


def replay_record(path: Path) -> dict[str, Any]:
    """One cached response as a replay-file record (usage keys renamed)."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    usage = data.get("usage") or {}
    data["usage"] = {REPLAY_USAGE_KEYS.get(k, k): v for k, v in usage.items()}
    return data


def export_world(
    world_dir: Path | str, out_root: Path | str, *, with_replay: bool | None = None
) -> ExportResult:
    """Export ``world_dir`` as ``<out_root>/<world id>/`` (``simenterprise-export/1``).

    Refuses a directory that is not a world, a world whose build is not done, an
    existing non-empty target, and any file it does not know how to render as text.

    ``with_replay`` (default: on for live worlds) adds ``replay/llm-cache.jsonl``
    (gold): every recorded model response, keyed by request, so
    ``enterprise-sim world replay`` rebuilds the world with no model call.
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
            text = _RENDERERS[suffix](path).replace("\r\n", "\n").replace("\r", "\n")
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
        rel = f"data/{scenario}/{kind}/{name}.csv"
        csv_text, columns = _table_csv(directory, rel)
        rel = writer.write(rel, csv_text.encode("utf-8"), "text/csv")
        primary, foreign = _table_keys(spec, name) if kind == "tables" else ([], [])
        duplicated = _duplicated_tables(world_dir / "data" / scenario)
        if kind == "tables" and name in duplicated:
            # [data.messiness] loaded some rows twice: the raw table has no key.
            primary = []
        # a reference into a table without a key is no foreign key either.
        foreign = [fk for fk in foreign if fk["table"] not in duplicated]
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

    if with_replay is None:
        with_replay = prose_backend_of(world.render_backend) == "live"
    if with_replay:
        cache = world_dir / ".cache" / "llm"
        lines = [
            json.dumps(
                {"key": path.stem, "completion": replay_record(path)},
                sort_keys=True,
                ensure_ascii=False,
            )
            for path in sorted(cache.glob("*.json"))
        ]
        if lines:
            writer.write(
                "replay/llm-cache.jsonl",
                ("\n".join(lines) + "\n").encode("utf-8"),
                "application/x-ndjson",
            )

    from enterprise_sim.worlds.slice import (
        OBSERVATION_COLUMNS,
        REGISTRY_COLUMNS,
        TRAIL_COLUMNS,
        core_slice_name,
        project_observations,
        project_registry,
        project_slices,
        project_trail,
        slice_name,
    )

    slices = project_slices(world_dir)
    projects = {
        n["id"]
        for n in (
            json.loads(line)
            for line in (world_dir / "kg" / "nodes.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        )
        if n["type"] == "Project"
    }
    by_name = {s["name"]: s for s in slices}
    for project in sorted(projects):
        rows = project_trail(world_dir, project)
        entry = by_name.get(slice_name(project))
        if not rows or entry is None:
            continue
        names = [c for c, _ in TRAIL_COLUMNS]
        body = "\n".join([_csv_line(names), *(_csv_line([r[c] for c in names]) for r in rows)])
        rel = writer.write(f"trails/{entry['name']}.csv", (body + "\n").encode("utf-8"), "text/csv")
        writer.tables.append(
            {
                # table names map to identifiers ([a-z_][a-z0-9_]*) downstream.
                "name": f"trail.{entry['name'].replace('-', '_')}",
                "path": rel,
                "kind": "table",
                "columns": [{"name": c, "type": t} for c, t in TRAIL_COLUMNS],
                "primary_key": [],
                "foreign_keys": [],
            }
        )
        entry["files"] = sorted([*entry["files"], rel])
    for project in sorted(projects):
        registry = project_registry(world_dir, project)
        entry = by_name.get(slice_name(project))
        if not registry or entry is None:
            continue
        names = [c for c, _ in REGISTRY_COLUMNS]
        body = "\n".join([_csv_line(names), *(_csv_line([r[c] for c in names]) for r in registry)])
        rel = writer.write(
            f"registry/{entry['name']}.csv", (body + "\n").encode("utf-8"), "text/csv"
        )
        writer.tables.append(
            {
                "name": f"registry.{entry['name'].replace('-', '_')}",
                "path": rel,
                "kind": "table",
                "columns": [{"name": c, "type": t} for c, t in REGISTRY_COLUMNS],
                "primary_key": ["attribute", "version"],
                "foreign_keys": [],
            }
        )
        entry["files"] = sorted([*entry["files"], rel])
        core = by_name.get(core_slice_name(project))
        if core is not None:
            core["files"] = sorted([*core["files"], rel])
        observations = project_observations(world_dir, project)
        if observations:
            names = [c for c, _ in OBSERVATION_COLUMNS]
            body = "\n".join(
                [_csv_line(names), *(_csv_line([r[c] for c in names]) for r in observations)]
            )
            obs_rel = writer.write(
                f"observations/{entry['name']}.csv", (body + "\n").encode("utf-8"), "text/csv"
            )
            writer.tables.append(
                {
                    "name": f"observations.{entry['name'].replace('-', '_')}",
                    "path": obs_rel,
                    "kind": "table",
                    "columns": [{"name": c, "type": t} for c, t in OBSERVATION_COLUMNS],
                    "primary_key": ["date", "attribute"],
                    "foreign_keys": [],
                }
            )
            for holder in (entry, core):
                if holder is not None:
                    holder["files"] = sorted([*holder["files"], obs_rel])
    for entry in slices:
        missing = [f for f in entry["files"] if f not in writer.paths]
        if missing:
            raise ExportError(f"slice {entry['name']}: files not in the export: {missing[:3]}")

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
        "simenterprise": {
            "version": __version__,
            "commit": commit,
            "dirty": dirty,
            "repository": REPOSITORY,
        },
        "counts": world.counts,
        "files": sorted(writer.files, key=lambda f: f["path"]),
        "tables": sorted(writer.tables, key=lambda t: t["name"]),
        "slices": slices,
    }
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    (target / "export.json").write_bytes(body.encode("utf-8"))
    return ExportResult(target, _sha256(body.encode("utf-8")), len(writer.files))
