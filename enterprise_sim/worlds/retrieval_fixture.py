"""``world retrieval-fixture EXPORT_DIR --out DIR``: a judged retrieval corpus from an export.

For Spindle's retrieval regression suite (decision record 0042): a
committable bundle that loads offline and is pinned by digest.

* ``corpus.jsonl``: one line per document,
  ``{"kind": "doc", "key": "doc:<export path>", "text"}``, for every data file under
  ``artifacts/`` or ``organization/``. Also one line per stated fact version,
  ``{"kind": "fact", "key": "fact:<fact id>@v<n>", "text", "cites", "scope",
  "valid_from", "replaces"}``, where ``cites`` lists the documents whose gold
  ``asserts`` edge states that version.
* ``queries.jsonl``: queries in the suite's ``Query`` shape, judged mechanically from
  the gold ``asserts`` edges with the suite's rubric:
  * grade **2**: a key whose text states the requested value. That is the fact
    version itself, and every document asserting it (current, hedged or update) whose
    exported text contains the value's token;
  * grade **1**: a document about the subject that does not state it: one asserting
    another version, a wrong value, or the value without its token in the text (a
    typo, for example).

  Per fact, one query for the value at the window end; per changed fact, one ``as_of``
  query inside each earlier version's interval. Each query serves the version true in
  its window and lists every other version of the fact's chain as ``not_served``
  (earlier, later and retracted ones alike). Question wording varies over the fact's
  own question and three paraphrases, picked by a hash of the fact id.
* ``manifest.json``: the world id, the export digest (sha256 of its ``export.json``),
  counts, the rubric, and the sha256 of both files.

Keys are unique within a bundle. Deterministic: the same export gives the same bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["add_retrieval_fixture_parser", "build_fixture"]

_STATES = frozenset({"current", "hedged", "update"})
_PARAPHRASES = (
    "{question}",
    "What is the {label} of {subject}?",
    "{subject}: which {label} applies?",
    "Remind me of the {label} for {subject}.",
)


class FixtureError(ValueError):
    """The directory is not an export this command can read."""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _stamp(text: str) -> str:
    return datetime.fromisoformat(text).strftime("%Y-%m-%dT%H:%M:%SZ")


def _midpoint(start: str, end: str) -> str:
    a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    return (a + (b - a) / 2).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lines(rows: Iterable[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows).encode()


def _answers(
    fact_id: str, number: int, stated: dict[int, set[str]], about: set[str]
) -> dict[str, int]:
    """Grades for the value of version ``number``: 2 states it, 1 is about the subject."""
    graded = {k: 1 for k in about}
    for n, keys in stated.items():
        for k in keys:
            graded[k] = 2 if n == number else max(graded.get(k, 0), 1)
    graded[f"fact:{fact_id}@v{number}"] = 2
    return dict(sorted(graded.items()))


def build_fixture(export_dir: Path) -> dict[str, bytes]:
    """The three files of the bundle for ``export_dir``, by name."""
    from enterprise_sim.facts.catalog import value_token

    manifest_path = export_dir / "export.json"
    try:
        export = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FixtureError(f"{export_dir} is not a SimEnterprise export: {exc}") from exc
    nodes = {n["id"]: n for n in _jsonl(export_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(export_dir / "kg" / "edges.jsonl")

    # world path -> export path, for the documents of the corpus
    export_of: dict[str, str] = {}
    docs: list[dict[str, Any]] = []
    for entry in sorted(export["files"], key=lambda f: f["path"]):
        path = entry["path"]
        if entry["role"] != "data" or not path.startswith(("artifacts/", "organization/")):
            continue
        source = entry.get("rendered_from") or entry.get("source_path") or path
        export_of[str(source)] = path
        text = (export_dir / path).read_text(encoding="utf-8")
        docs.append({"kind": "doc", "key": f"doc:{path}", "text": text})
    text_of = {d["key"]: d["text"] for d in docs}

    def doc_key(artifact_id: str) -> str | None:
        node = nodes.get(artifact_id)
        if node is None:
            return None
        path = export_of.get(str(node["props"].get("path", "")))
        return f"doc:{path}" if path else None

    versions: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        if node["type"] == "FactVersion":
            versions.setdefault(str(node["props"]["fact"]), []).append(node["props"])
    asserts = [e for e in edges if e["type"] == "asserts"]

    facts: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    for fact_id in sorted(versions):
        fact = nodes.get(fact_id)
        if fact is None:
            continue
        subject = nodes.get(str(fact["props"].get("subject")), {})
        subject_name = str(subject.get("props", {}).get("name", fact["props"].get("subject")))
        label = str(fact["props"].get("label", fact["props"].get("attribute")))
        history = sorted(versions[fact_id], key=lambda v: int(v["version"]))
        stated: dict[int, set[str]] = {}
        about: set[str] = set()
        for edge in asserts:
            if edge["dst"] != fact_id:
                continue
            key = doc_key(edge["src"])
            if key is None:
                continue
            props = edge["props"]
            number = props.get("stated_version")
            token = value_token(str(props.get("value", "")))
            if (
                props.get("stance") in _STATES
                and number is not None
                and token
                and token in text_of.get(key, "")
            ):
                stated.setdefault(int(number), set()).add(key)
            else:
                about.add(key)
        previous: str | None = None
        for version in history:
            key = f"fact:{fact_id}@v{version['version']}"
            record: dict[str, Any] = {
                "kind": "fact",
                "key": key,
                "text": f"The {label} of {subject_name} is {version['value']}.",
                "cites": sorted(stated.get(int(version["version"]), ())),
                "scope": {"project": str(fact["props"].get("subject"))},
                "valid_from": _stamp(str(version["valid_from"])),
            }
            if previous is not None:
                record["replaces"] = previous
            facts.append(record)
            previous = key
        live = [v for v in history if not v.get("retracted_at")]
        if not live:
            continue
        pick = int(hashlib.sha256(fact_id.encode()).hexdigest()[:8], 16)
        question = _PARAPHRASES[pick % len(_PARAPHRASES)].format(
            question=fact["props"].get("question") or f"What is the {label} of {subject_name}?",
            label=label,
            subject=subject_name,
        )

        current = int(live[-1]["version"])
        chain = [f"fact:{fact_id}@v{v['version']}" for v in history]

        queries.append(
            {
                "id": f"{fact_id}:now",
                "query": question,
                "answers": _answers(fact_id, current, stated, about),
                "served": [f"fact:{fact_id}@v{current}"],
                "not_served": [k for k in chain if k != f"fact:{fact_id}@v{current}"],
            }
        )
        for older, newer in zip(live, live[1:], strict=False):
            n = int(older["version"])
            queries.append(
                {
                    "id": f"{fact_id}:as-of-v{n}",
                    "query": question,
                    "as_of": _midpoint(str(older["valid_from"]), str(newer["valid_from"])),
                    "answers": _answers(fact_id, n, stated, about),
                    "served": [f"fact:{fact_id}@v{n}"],
                    "not_served": [k for k in chain if k != f"fact:{fact_id}@v{n}"],
                }
            )

    corpus = sorted(docs, key=lambda d: d["key"]) + facts
    keys = [r["key"] for r in corpus]
    if len(keys) != len(set(keys)):
        raise FixtureError("a key repeats in the corpus")
    corpus_bytes, query_bytes = _lines(corpus), _lines(queries)
    manifest = {
        "format": "simenterprise-retrieval-fixture/1",
        "world_id": export["world_id"],
        "export_digest": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "judged": "mechanically, from the gold asserts edges",
        "rubric": "2 = the text states the requested value (hedged included); "
        "1 = about the subject without the value",
        "counts": {"docs": len(docs), "facts": len(facts), "queries": len(queries)},
        "files": {
            "corpus.jsonl": hashlib.sha256(corpus_bytes).hexdigest(),
            "queries.jsonl": hashlib.sha256(query_bytes).hexdigest(),
        },
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=1) + "\n").encode()
    return {
        "corpus.jsonl": corpus_bytes,
        "queries.jsonl": query_bytes,
        "manifest.json": manifest_bytes,
    }


def _cmd_world_retrieval_fixture(args: argparse.Namespace) -> int:
    try:
        files = build_fixture(args.export_dir)
    except FixtureError as exc:
        print(f"enterprise-sim world retrieval-fixture: {exc}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (args.out / name).write_bytes(body)
    print(files["manifest.json"].decode().strip())
    return 0


def add_retrieval_fixture_parser(world_subparsers: Any) -> None:
    """Register ``world retrieval-fixture EXPORT_DIR --out DIR``."""
    parser = world_subparsers.add_parser(
        "retrieval-fixture",
        help="a judged retrieval corpus (documents, fact versions, queries) from an export",
    )
    parser.add_argument("export_dir", type=Path, metavar="EXPORT_DIR", help="a world export")
    parser.add_argument("--out", required=True, type=Path, help="directory for the bundle")
    parser.set_defaults(func=_cmd_world_retrieval_fixture)
