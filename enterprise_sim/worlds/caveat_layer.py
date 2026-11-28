"""The knowledge layer's ``caveats`` (``facts.caveats``): read out of an export, checked closed.

One entry per ``Caveat`` gold node: the base item and the value a consumer must use
instead within the caveat's scope, the documents that state the caveat, and the later
documents that state only the base value (silent carriers: a consumer reading only
them computes ``value_before``). :func:`caveat_entries` is the one function the layer
and its check both call, so a layer is checked by recomputing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from enterprise_sim.worlds.knowledge import _Export

__all__ = ["caveat_entries"]


def caveat_entries(
    ex: _Export, items: dict[str, dict[str, Any]], access: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """The caveats of the world, ordered by id (empty for a world without them)."""
    from enterprise_sim.facts.caveats import N_CAVEAT
    from enterprise_sim.worlds.knowledge import _costly_facts, _item_id, _table_holds, _verbatim

    copies = {e["src"] for e in ex.edges if e["type"] == "duplicate_of"}
    family: dict[str, list[str]] = {}
    for aid, n in ex.nodes.items():
        if n["type"] == "Artifact" and aid not in copies:
            family.setdefault(str(n["props"].get("event")), []).append(aid)
    readers_of = {p: r["readers"] for r in access for p in r["paths"]} if access is not None else {}
    tables = sorted(str(t["path"]) for t in ex.manifest.get("tables", ()) if t.get("path"))
    out: list[dict[str, Any]] = []
    for node in sorted(
        (n for n in ex.nodes.values() if n["type"] == N_CAVEAT), key=lambda n: n["id"]
    ):
        p = node["props"]
        item = items.get(_item_id(str(p["fact"]), int(p["version"])))
        if item is None:
            continue
        before, after = str(p["value_before"]), str(p["value_after"])
        carriers: list[dict[str, Any]] = []
        for aid in sorted(family.get(str(p["event"]), [])):
            path = ex.path_of(aid)
            if path is None:
                continue
            text = ex.text(path)
            carrier: dict[str, Any] = {
                "artifact": aid,
                "path": path,
                "at": ex.nodes[aid].get("created_at"),
                "verbatim": _verbatim(before, text) and _verbatim(after, text),
            }
            if access is not None:
                carrier["everyone"] = path not in readers_of
                if path in readers_of:
                    carrier["readers"] = readers_of[path]
            carriers.append(carrier)
        stating = {c["path"] for c in carriers}
        since = str(p["valid_from"])[:10]
        silent = [
            {"path": c["path"], "at": c["at"], "stance": c["stance"]}
            | ({"readers": c["readers"]} if "readers" in c else {})
            for c in item["carriers"]
            if c["path"] not in stating
            and str(c["at"]) >= since
            and not _verbatim(after, ex.text(c["path"]))
        ]
        entry: dict[str, Any] = {
            "id": node["id"],
            "item": item["id"],
            "fact": item["fact"],
            "kind": p["kind"],
            "scope": p["scope"],
            "selector": p["selector"],
            "question": p["question"],
            "statement": f"{item['statement']} {p['sentence']}",
            "value_before": before,
            "value_after": after,
            "canonical_after": p["canonical_after"],
            "typed_after": p["typed_after"],
            "arithmetic": p["arithmetic"],
            "valid_from": p["valid_from"],
            "valid_to": p["valid_to"],
            "author": p["author"],
            "carriers": carriers,
            "silent_carriers": silent,
            "carried_in_tables": [t for t in tables if _table_holds(ex, t, after)],
            # whether anything but the caveat's own documents hints that it exists: every
            # data file (documents and tables) naming its cue, case-insensitive; a loose
            # match, so "no hint" is never claimed while a file hints it
            "hint": _hint(ex, str(p["cue"]), stating),
            # a second fact for a join: up to two more items of the caveat's project in
            # force when it was written, held in a table row dated by then, with holders
            "costly_facts": [
                {**extra, "holders": [h["person"] for h in items[extra["item"]]["holders"]]}
                for extra in _costly_facts(
                    ex,
                    items,
                    tables,
                    str(item["subject"]["id"]),
                    {"window_end": p["valid_from"]},
                    str(p["fact"]),
                )
            ],
        }
        if access is not None:
            public = any(c["everyone"] and c["verbatim"] for c in carriers)
            readers = sorted({r for c in carriers for r in c.get("readers", ())})
            entry["reach"] = {"everyone": public, "readers": [] if public else readers}
        out.append(entry)
    return out


def _hint(ex: _Export, cue: str, stating: set[str]) -> dict[str, Any]:
    """``{cue, files, hinted}``: the data files other than the carriers that name ``cue``."""
    needle = cue.lower()
    files = sorted(
        str(f["path"])
        for f in ex.manifest["files"]
        if f.get("role") == "data"
        and str(f["path"]) not in stating
        and needle in ex.text(str(f["path"])).lower()
    )
    return {"cue": cue, "files": files, "hinted": bool(files)}
