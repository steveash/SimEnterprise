"""``world knowledge-layer EXPORT_DIR --out FILE``: the knowledge a test set can be minted from.

A test-set engine (the gym's fin and casebook engines) needs, for each piece of
knowledge it plants: what it says and when it holds, which older version it
supersedes, who in the roster holds it, which documents carry it, and which documents
say something else that could mislead. The world's gold graph has all of it in the
fact ledger; this module reads it out of an export into one documented file,
``knowledge.json`` (``simenterprise-knowledge/1``; schema in
``docs/knowledge-layer.md``). It reads the export only, so it works for every world
already exported, live-prose ones included, with no model call.

* **items**: one per fact version (a decision, policy, target or status of a project),
  with its value, valid time, retraction, and the versions before and after it;
* **holders**: the people who wrote it down (``author``; ``announcer`` for the one
  who announced the change) and the owner of the project's initiative (``owner``),
  with the day each left, if they did;
* **carriers**: every document whose gold ``asserts`` edge states the version as
  true (current, an update, or hedged), with whether the value's token is in its
  exported text (``verbatim``; a typo can drop it);
* **supersessions**: each change of a fact, old item, new item, effective time and
  the documents that announced it;
* **misleading**: every document stating a superseded value as current (``stale``),
  a value that was never true (``conflicting``), or a near-copy of another document
  (``near_duplicate``), with the item each misleads about.

:func:`check_layer` checks a layer against its export: every path it names is a file
of the export, every ``verbatim`` flag matches the text, every holder is a person of
the roster, and the supersession chains are consistent. Deterministic: the same
export gives the same bytes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "FORMAT",
    "KNOWLEDGE_FILE",
    "KnowledgeError",
    "add_knowledge_parser",
    "build_layer",
    "check_layer",
    "layer_bytes",
]

FORMAT = "simenterprise-knowledge/1"
#: Where an export carries its own layer (``world export --with-knowledge``), as gold.
KNOWLEDGE_FILE = "knowledge/knowledge.json"
_TRUE = ("current", "update", "hedged")
_MISLEADING = ("stale", "conflicting")


class KnowledgeError(ValueError):
    """The directory is not an export this command can read."""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _item_id(fact: str, version: int) -> str:
    return f"{fact}@v{version}"


class _Export:
    """The parts of an export the layer reads."""

    def __init__(self, export_dir: Path, manifest: dict[str, Any] | None = None) -> None:
        try:
            self.manifest = manifest or json.loads(
                (export_dir / "export.json").read_text(encoding="utf-8")
            )
            self.nodes = {n["id"]: n for n in _jsonl(export_dir / "kg" / "nodes.jsonl")}
            self.edges = _jsonl(export_dir / "kg" / "edges.jsonl")
            config = json.loads((export_dir / "config.json").read_text(encoding="utf-8"))
            self.config: dict[str, Any] = config
        except (OSError, ValueError, KeyError) as exc:
            raise KnowledgeError(f"{export_dir} is not a SimEnterprise export: {exc}") from exc
        self.dir = export_dir
        self.window = {
            "start": str(config["simulation"]["period_start"]),
            "end": str(config["simulation"]["period_end"]),
        }
        self.files = {str(f["path"]) for f in self.manifest["files"]}
        self.export_of = {
            str(f.get("rendered_from") or f.get("source_path") or f["path"]): str(f["path"])
            for f in self.manifest["files"]
        }
        self._text: dict[str, str] = {}

    def path_of(self, artifact_id: str) -> str | None:
        node = self.nodes.get(artifact_id)
        if node is None:
            return None
        return self.export_of.get(str(node["props"].get("path", "")))

    def text(self, path: str) -> str:
        if path not in self._text:
            self._text[path] = (self.dir / path).read_text(encoding="utf-8")
        return self._text[path]

    def by_type(self, edge_type: str) -> list[dict[str, Any]]:
        return [e for e in self.edges if e["type"] == edge_type]


def _verbatim(value: str, text: str) -> bool:
    """Whether ``text`` holds ``value`` byte for byte (a typo or a paraphrase does not)."""
    return bool(value) and value in text


def _person(node: dict[str, Any]) -> dict[str, Any]:
    props = node["props"]
    return {
        "id": node["id"],
        "name": props.get("name"),
        "department": props.get("department"),
        "team": props.get("team"),
        "title": props.get("title"),
        "joined_at": props.get("joined_at"),
        "left_at": props.get("left_at"),
        "replaces": props.get("replaces"),
    }


def _access_rows(export_dir: Path) -> list[dict[str, Any]] | None:
    """The export's access.json rows (record 0130), or ``None`` when it has none."""
    from enterprise_sim.exporters.access import ACCESS_FILE

    path = export_dir / ACCESS_FILE
    if not path.is_file():
        return None
    rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["documents"]
    return rows


def _reach(ex: _Export, token: str, access: list[dict[str, Any]]) -> dict[str, Any]:
    """Who can read a file holding ``token``: any unrestricted such data file means everyone.

    Every exported data file is searched, not only the item's carriers, so a quoting or
    copying document counts too; the match errs only toward wider (record 0130).
    """
    readers_of = {p: r["readers"] for r in access for p in r["paths"]}
    readers: set[str] = set()
    everyone = False
    for f in ex.manifest["files"]:
        if f.get("role") != "data" or not token:
            continue
        path = str(f["path"])
        if token not in ex.text(path):
            continue
        if path in readers_of:
            readers.update(readers_of[path])
        else:
            everyone = True
    return {"everyone": everyone, "readers": [] if everyone else sorted(readers)}


def _stream_doc(
    ex: _Export,
    aid: str | None,
    fact: str,
    stated: dict[tuple[str, str], int | None],
    items: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """A concurrent day's document: its export path, time and the item it states.

    ``verbatim``: whether its exported text holds that item's value byte for byte (the
    carrier rule; the prose noise keeps a planted document's value as written).
    """
    if aid is None:
        return None
    number = stated.get((aid, fact))
    stated_item = None if number is None else _item_id(fact, int(number))
    path = ex.path_of(aid)
    value = str((items.get(stated_item or "") or {}).get("value", ""))
    return {
        "artifact": aid,
        "path": path,
        "at": ex.nodes[aid].get("created_at"),
        "stated_item": stated_item,
        "verbatim": path is not None and _verbatim(value, ex.text(path)),
    }


def _table_holds(ex: _Export, path: str, token: str, until: str | None = None) -> bool:
    """Whether the table at ``path`` states ``token`` as typed evidence (by ``until``).

    A value counts in a text cell only as a whole token (no letter or digit next to it, so
    ``900`` is not in ``$900k``), or as a whole cell of a number or other non-date type;
    never as a fragment of a timestamp, a number or a header (a set engine's integrity
    rule). A date or timestamp cell is never evidence: it dates its row (a daily table's
    ``2026-03-27`` row does not state a target date of 2026-03-27). With ``until`` (an
    ISO time), a row counts only when every date or timestamp cell in it is on or before
    it: a row written later is not evidence at that time. An undated row always counts.
    """
    if not token:
        return False
    whole = re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")
    entry = next((t for t in ex.manifest.get("tables", ()) if t.get("path") == path), None)
    if entry is None:
        return False
    types = [str(c.get("type", "text")) for c in entry.get("columns", ())]
    rows = csv.reader(io.StringIO(ex.text(path)))
    next(rows, None)  # the header is never evidence
    dated = [i for i, kind in enumerate(types) if kind in ("date", "timestamp")]
    for row in rows:
        if until is not None and any(
            i < len(row) and row[i] and row[i][: len(until)] > until[: len(row[i])] for i in dated
        ):
            continue
        for i, cell in enumerate(row):
            kind = types[i] if i < len(types) else "text"
            if kind == "text" and whole.search(cell):
                return True
            if kind not in ("text", "date", "timestamp") and cell == token:
                return True
    return False


def _costly_facts(
    ex: _Export,
    items: dict[str, dict[str, Any]],
    tables: list[str],
    project: str,
    props: dict[str, Any],
    found: str,
) -> list[dict[str, Any]]:
    """Up to two more facts for a concurrent day: the needing project's own items in force at
    the window's end whose value an exported table holds (a costly path for the publisher)."""
    end = str(props["window_end"])
    out: list[dict[str, Any]] = []
    for item in sorted(items.values(), key=lambda i: i["id"]):
        if item["subject"]["id"] != project or item["fact"] == found:
            continue
        if not (
            str(item["valid_from"]) <= end
            and (item["valid_to"] is None or end < str(item["valid_to"]))
        ):
            continue
        token = str(item["canonical"])
        paths = sorted(t for t in tables if _table_holds(ex, t, token, end))
        if paths:
            out.append(
                {"item": item["id"], "value": item["value"], "canonical": token, "tables": paths}
            )
        if len(out) == 2:
            break
    return out


def _workstreams(
    ex: _Export, items: dict[str, dict[str, Any]], access: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """The concurrent days of the world (``facts.concurrency``), one entry each.

    Each names the fact found mid-day and its new item; the discoverer and the owner
    team; the needing team with its morning and afternoon documents and the item each
    states; the window; the answer (the new value); the table files that carry the new
    value (a costly path, or none); and, when the export has ``access.json``, who can
    read the discovery document.
    """
    from enterprise_sim.facts.concurrency import N_CONCURRENT_DAY

    by_event = {
        str(n["props"].get("event")): aid for aid, n in ex.nodes.items() if n["type"] == "Artifact"
    }
    staff: dict[str, list[str]] = {}
    for e in ex.edges:
        if e["type"] == "member_of" and ex.nodes.get(e["dst"], {}).get("type") == "Project":
            staff.setdefault(e["dst"], []).append(e["src"])
    stated: dict[tuple[str, str], int | None] = {}
    for e in ex.edges:
        if e["type"] == "asserts":
            stated[(e["src"], e["dst"])] = e["props"].get("stated_version")
    readers_of = {p: r["readers"] for r in access for p in r["paths"]} if access is not None else {}
    tables = sorted(str(t["path"]) for t in ex.manifest.get("tables", ()) if t.get("path"))
    out: list[dict[str, Any]] = []
    for node in sorted(
        (n for n in ex.nodes.values() if n["type"] == N_CONCURRENT_DAY), key=lambda n: n["id"]
    ):
        p = node["props"]
        fact = str(p["fact"])
        item_id = _item_id(fact, int(p["version"]))
        item = items.get(item_id)
        if item is None:
            continue

        discovery = _stream_doc(
            ex, by_event.get(str(p["events"]["discovery"])), fact, stated, items
        )
        entry: dict[str, Any] = {
            "day": p["day"],
            "fact": fact,
            "item": item_id,
            "discovered_at": p["discovered_at"],
            "discoverer": p["discoverer"],
            "owner_team": {
                "project": p["owner_project"],
                "people": sorted(staff.get(str(p["owner_project"]), [])),
            },
            "discovery": discovery,
            "needed_by": {
                "project": p["needer_project"],
                "people": list(p["needer_people"]),
                "before": _stream_doc(
                    ex, by_event.get(str(p["events"]["before"])), fact, stated, items
                ),
                "after": _stream_doc(
                    ex, by_event.get(str(p["events"]["after"])), fact, stated, items
                ),
            },
            "window": {"start": p["window_start"], "end": p["window_end"]},
            "answer": {"value": item["value"], "canonical": item["canonical"]},
            "carried_in_tables": sorted(
                t for t in tables if _table_holds(ex, t, str(item["canonical"]))
            ),
            "carried_by_window_end": sorted(
                t
                for t in tables
                if _table_holds(ex, t, str(item["canonical"]), str(p["window_end"]))
            ),
            "costly_facts": _costly_facts(ex, items, tables, str(p["needer_project"]), p, fact),
        }
        if access is not None and discovery is not None:
            readers = readers_of.get(str(discovery["path"]))
            entry["discovery"]["readers"] = readers
            entry["discovery"]["everyone"] = readers is None
        out.append(entry)
    return out


def build_layer(export_dir: Path, *, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """The knowledge layer of ``export_dir`` (see the module docstring).

    With ``manifest`` (``world_id`` and ``files``, as the exporter holds them before it
    writes ``export.json``), the layer is built to go inside that export: its
    ``export_digest`` is null, since ``export.json`` lists the layer and so binds it.
    """
    from enterprise_sim.facts.catalog import kind_of, value_token

    ex = _Export(export_dir, manifest)
    # Who may read each restricted document (decision record 0130; controller-only).
    # Only an export that carries access.json gets access fields, so others keep their bytes.
    access = _access_rows(export_dir)
    restricted = {r["artifact"]: r for r in access} if access is not None else {}
    authors: dict[str, list[str]] = {}
    for edge in ex.by_type("authored"):
        authors.setdefault(edge["dst"], []).append(edge["src"])
    initiative_of = {e["src"]: e["dst"] for e in ex.by_type("under")}
    owner_of = {e["dst"]: e["src"] for e in ex.by_type("owns_initiative")}

    versions: dict[str, list[dict[str, Any]]] = {}
    for node in ex.nodes.values():
        if node["type"] == "FactVersion":
            versions.setdefault(str(node["props"]["fact"]), []).append(node["props"])
    statements: dict[str, list[dict[str, Any]]] = {}
    for edge in ex.by_type("asserts"):
        statements.setdefault(edge["dst"], []).append(edge)
    # A planted near-copy of a document repeats its statements; it misleads (listed
    # as a near_duplicate below) and never carries, announces or retracts an item.
    copies = {e["src"] for e in ex.by_type("duplicate_of")}

    items: list[dict[str, Any]] = []
    supersessions: list[dict[str, Any]] = []
    misleading: list[dict[str, Any]] = []
    held_by: set[str] = set()
    for fact_id in sorted(versions):
        fact = ex.nodes.get(fact_id)
        if fact is None:
            continue
        props = fact["props"]
        subject = ex.nodes.get(str(props.get("subject")), {})
        subject_props = subject.get("props", {})
        label = str(props.get("label", props.get("attribute")))
        subject_name = str(subject_props.get("name", props.get("subject")))
        owner = owner_of.get(initiative_of.get(str(props.get("subject")), ""))
        history = sorted(versions[fact_id], key=lambda v: int(v["version"]))
        carriers: dict[int, list[dict[str, Any]]] = {}
        holders: dict[int, dict[str, str]] = {}
        announced: dict[int, list[str]] = {}
        retracted_in: dict[int, list[str]] = {}
        value_of = {int(v["version"]): str(v["value"]) for v in history}
        for edge in sorted(statements.get(fact_id, ()), key=lambda e: str(e["id"])):
            path = ex.path_of(edge["src"])
            if path is None:
                continue
            at = str(ex.nodes[edge["src"]]["props"].get("date", edge.get("created_at", "")))
            sp = edge["props"]
            stance = sp.get("stance")
            value = str(sp.get("value", ""))
            is_copy = edge["src"] in copies
            if stance in _TRUE and sp.get("stated_version") is not None and not is_copy:
                number = int(sp["stated_version"])
                carrier: dict[str, Any] = {
                    "path": path,
                    "stance": stance,
                    "at": at,
                    "verbatim": _verbatim(value_of.get(number, value), ex.text(path)),
                }
                if edge["src"] in restricted:
                    carrier["readers"] = restricted[edge["src"]]["readers"]
                carriers.setdefault(number, []).append(carrier)
                role = "announcer" if stance == "update" else "author"
                for person in authors.get(edge["src"], ()):
                    mine = holders.setdefault(number, {})
                    if mine.get(person) != "announcer":
                        mine[person] = role
                if stance == "update":
                    announced.setdefault(number, []).append(path)
            elif stance in _TRUE or (stance == "retraction" and is_copy):
                continue  # a near-copy's restatement: listed as a near_duplicate only
            elif stance == "retraction" and sp.get("true_version") is not None:
                retracted_in.setdefault(int(sp["true_version"]), []).append(path)
            elif stance in _MISLEADING:
                true_n = sp.get("true_version")
                stated_n = sp.get("stated_version")
                misleading.append(
                    {
                        "kind": stance,
                        "path": path,
                        "at": at,
                        "fact": fact_id,
                        "stated_value": value,
                        "misleads_about": None
                        if true_n is None
                        else _item_id(fact_id, int(true_n)),
                        "stated_item": None
                        if stated_n is None
                        else _item_id(fact_id, int(stated_n)),
                    }
                )
        for index, version in enumerate(history):
            number = int(version["version"])
            people = dict(holders.get(number, {}))
            if owner is not None and owner not in people:
                people[owner] = "owner"
            held_by.update(people)
            items.append(
                {
                    "id": _item_id(fact_id, number),
                    "fact": fact_id,
                    "version": number,
                    "kind": kind_of(str(props.get("attribute"))) or "decision",
                    "attribute": props.get("attribute"),
                    "label": label,
                    "subject": {
                        "id": props.get("subject"),
                        "name": subject_name,
                        "department": subject_props.get("department"),
                    },
                    "question": props.get("question"),
                    "value": version["value"],
                    "canonical": version.get("canonical") or value_token(str(version["value"])),
                    "statement": f"The {label} of {subject_name} is {version['value']}.",
                    "valid_from": version.get("valid_from"),
                    "valid_to": version.get("valid_to"),
                    "retracted_at": version.get("retracted_at"),
                    "retracted_in": sorted(retracted_in.get(number, ())),
                    "supersedes": _item_id(fact_id, int(history[index - 1]["version"]))
                    if index
                    else None,
                    "superseded_by": _item_id(fact_id, int(history[index + 1]["version"]))
                    if index + 1 < len(history)
                    else None,
                    "holders": [{"person": p, "as": people[p]} for p in sorted(people)],
                    "carriers": sorted(
                        carriers.get(number, ()), key=lambda c: (c["at"], c["path"])
                    ),
                }
            )
            if access is not None:
                items[-1]["reach"] = _reach(ex, str(items[-1]["canonical"]), access)
            if index:
                supersessions.append(
                    {
                        "fact": fact_id,
                        "old": _item_id(fact_id, int(history[index - 1]["version"])),
                        "new": _item_id(fact_id, number),
                        "effective": version.get("valid_from"),
                        "announced_in": sorted(announced.get(number, ())),
                    }
                )

    for edge in sorted(ex.by_type("duplicate_of"), key=lambda e: str(e["id"])):
        copy, original = ex.path_of(edge["src"]), ex.path_of(edge["dst"])
        if copy is None or original is None:
            continue
        misleading.append(
            {
                "kind": "near_duplicate",
                "path": copy,
                "at": str(ex.nodes[edge["src"]]["props"].get("date", "")),
                "duplicate_of": original,
            }
        )
    misleading.sort(key=lambda m: (m["at"], m["path"], m["kind"], m.get("fact") or ""))
    roster = [
        _person(ex.nodes[p])
        for p in sorted(held_by)
        if p in ex.nodes and ex.nodes[p]["type"] == "Person"
    ]
    counts = {
        "items": len(items),
        "facts": len({i["fact"] for i in items}),
        "supersessions": len(supersessions),
        "carriers": sum(len(i["carriers"]) for i in items),
        "misleading": len(misleading),
        "holders": len(roster),
    }
    workstreams = _workstreams(ex, {i["id"]: i for i in items}, access)
    from enterprise_sim.worlds.caveat_layer import caveat_entries

    by_id = {i["id"]: i for i in items}
    caveats = caveat_entries(ex, by_id, access)
    for caveat in caveats:  # only an item with a caveat has the key, so others keep bytes
        by_id[caveat["item"]].setdefault("caveats", []).append(caveat["id"])
    if caveats:
        counts["caveats"] = len(caveats)
    layer: dict[str, Any] = {
        "format": FORMAT,
        "world_id": ex.manifest["world_id"],
        "export_digest": None
        if manifest is not None
        else hashlib.sha256((export_dir / "export.json").read_bytes()).hexdigest(),
        "window": ex.window,
        "counts": counts,
        "items": items,
        "supersessions": supersessions,
        "misleading": misleading,
        "people": roster,
    }
    if workstreams:  # only a world with concurrent days has the key, so others keep bytes
        layer["workstreams"] = workstreams
    if caveats:  # likewise: only a world with caveats has the key
        layer["caveats"] = caveats
    return layer


def check_layer(export_dir: Path, layer: dict[str, Any]) -> list[str]:
    """Problems of ``layer`` against ``export_dir`` (empty: it holds)."""
    ex = _Export(export_dir)
    problems: list[str] = []
    copies = {ex.path_of(e["src"]) for e in ex.by_type("duplicate_of")} - {None}
    if layer.get("format") != FORMAT:
        problems.append(f"format is {layer.get('format')!r}, not {FORMAT}")
    if layer.get("world_id") != ex.manifest["world_id"]:
        problems.append("the layer is of another world")
    digest = hashlib.sha256((export_dir / "export.json").read_bytes()).hexdigest()
    if layer.get("export_digest") is None:  # inside the export: export.json lists it
        listed = [f for f in ex.manifest["files"] if f["path"] == KNOWLEDGE_FILE]
        if not listed or listed[0]["sha256"] != hashlib.sha256(layer_bytes(layer)).hexdigest():
            problems.append(f"the layer is not the {KNOWLEDGE_FILE} this export lists")
    elif layer.get("export_digest") != digest:
        problems.append("the layer is of another export of this world")
    people = {n for n, node in ex.nodes.items() if node["type"] == "Person"}
    access = _access_rows(export_dir)
    readers_of = {p: r["readers"] for r in access for p in r["paths"]} if access is not None else {}
    items = {i["id"]: i for i in layer.get("items", ())}
    if len(items) != len(layer.get("items", ())):
        problems.append("an item id repeats")
    for item in items.values():
        for carrier in item["carriers"]:
            if carrier["path"] not in ex.files:
                problems.append(f"{item['id']}: carrier {carrier['path']} is not in the export")
                continue
            if carrier["verbatim"] != _verbatim(str(item["value"]), ex.text(carrier["path"])):
                problems.append(f"{item['id']}: carrier {carrier['path']} verbatim flag is wrong")
            if carrier["path"] in copies:
                problems.append(f"{item['id']}: carrier {carrier['path']} is a near-duplicate")
            if carrier.get("readers") != readers_of.get(carrier["path"]):
                problems.append(f"{item['id']}: carrier {carrier['path']} readers are wrong")
        expected_reach = _reach(ex, str(item["canonical"]), access) if access is not None else None
        if item.get("reach") != expected_reach:
            problems.append(f"{item['id']}: reach is wrong")
        for path in item["retracted_in"]:
            if path not in ex.files:
                problems.append(f"{item['id']}: retraction {path} is not in the export")
        for holder in item["holders"]:
            if holder["person"] not in people:
                problems.append(f"{item['id']}: holder {holder['person']} is not in the roster")
        for link in ("supersedes", "superseded_by"):
            other = item[link]
            if other is not None and other not in items:
                problems.append(f"{item['id']}: {link} {other} is not an item")
        after = items.get(item["superseded_by"] or "")
        if after is not None and after["supersedes"] != item["id"]:
            problems.append(f"{item['id']}: the chain is not symmetric")
    expected_streams = _workstreams(ex, items, access)
    if layer.get("workstreams", []) != expected_streams or (
        "workstreams" in layer and not expected_streams
    ):
        problems.append("the workstreams are not the concurrent days of this export")
    # caveats: recomputed from the export alone (a fresh layer), never from the items
    # under check, so an edited carrier list cannot hide a silent carrier
    fresh = build_layer(export_dir, manifest=ex.manifest)
    if layer.get("caveats") != fresh.get("caveats"):
        problems.append("the caveats are not the caveats of this export")
    fresh_items = {i["id"]: i for i in fresh["items"]}
    for item_id, item in items.items():
        if item.get("caveats") != fresh_items.get(item_id, {}).get("caveats"):
            problems.append(f"{item_id}: its caveats are not this export's")
    for entry in layer.get("supersessions", ()):
        if entry["old"] not in items or entry["new"] not in items:
            problems.append(f"supersession {entry['old']} -> {entry['new']} names a missing item")
        for path in entry["announced_in"]:
            if path not in ex.files:
                problems.append(f"supersession {entry['new']}: {path} is not in the export")
    for entry in layer.get("misleading", ()):
        for key in ("path", "duplicate_of"):
            path = entry.get(key)
            if path is not None and path not in ex.files:
                problems.append(f"misleading {entry['kind']}: {path} is not in the export")
        for key in ("misleads_about", "stated_item"):
            ref = entry.get(key)
            if ref is not None and ref not in items:
                problems.append(f"misleading {entry['path']}: {key} {ref} is not an item")
    return problems


def layer_bytes(layer: dict[str, Any]) -> bytes:
    """The layer as written: sorted keys, one-space indent, a final newline."""
    return (json.dumps(layer, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode()


def _cmd_world_knowledge_layer(args: argparse.Namespace) -> int:
    try:
        if args.check is not None:
            layer = json.loads(args.check.read_text(encoding="utf-8"))
            problems = check_layer(args.export_dir, layer)
        else:
            layer = build_layer(args.export_dir)
            problems = check_layer(args.export_dir, layer)
            if not problems and args.out is not None:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_bytes(layer_bytes(layer))
    except (KnowledgeError, OSError, ValueError) as exc:
        print(f"enterprise-sim world knowledge-layer: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"enterprise-sim world knowledge-layer: {problem}", file=sys.stderr)
    print(
        json.dumps({"world_id": layer.get("world_id"), **layer.get("counts", {})}, sort_keys=True)
    )
    return 1 if problems else 0


def add_knowledge_parser(world_subparsers: Any) -> None:
    """Register ``world knowledge-layer EXPORT_DIR (--out FILE | --check FILE)``."""
    parser = world_subparsers.add_parser(
        "knowledge-layer",
        help="the knowledge layer of an export (items, holders, carriers, misleading documents)",
    )
    parser.add_argument("export_dir", type=Path, metavar="EXPORT_DIR", help="a world export")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--out", type=Path, help="write knowledge.json here (after checking it)")
    group.add_argument("--check", type=Path, help="check an existing knowledge.json instead")
    parser.set_defaults(func=_cmd_world_knowledge_layer)
