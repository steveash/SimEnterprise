"""The fact ledger, the brief contract and the ``synth`` backend."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.assembly import compute_config_digest
from enterprise_sim.cli import main
from enterprise_sim.core.config import WorldConfig
from enterprise_sim.core.config.diversity import FactsConfig
from enterprise_sim.core.events import Deliverable, Event
from enterprise_sim.core.llm import assemble_prompt
from enterprise_sim.core.llm.brief import FACTS_HEADER, facts_block, parse_brief
from enterprise_sim.core.llm.synth import SynthBackend
from enterprise_sim.facts import assign_assertions, build_ledger
from enterprise_sim.world_builders import build_world

_BASE = {
    "seed": 4,
    "company": {"name": "Facts Co", "vertical": "software", "size": "medium"},
    "simulation": {"period_start": "2026-01-05", "period_end": "2026-03-27"},
}


def _config(**sections: Any) -> WorldConfig:
    return WorldConfig.model_validate({**_BASE, **sections})


# -- digest stability ---------------------------------------------------------


def test_default_diversity_sections_do_not_change_the_digest() -> None:
    plain = WorldConfig.model_validate(_BASE)
    explicit = _config(facts={}, messiness={}, org={}, model={"offline_backend": "fake"})
    assert compute_config_digest(plain) == compute_config_digest(explicit)


def test_a_non_default_section_or_backend_changes_the_digest() -> None:
    plain = compute_config_digest(WorldConfig.model_validate(_BASE))
    assert compute_config_digest(_config(facts={"per_project": 4})) != plain
    assert compute_config_digest(_config(messiness={"typo_rate": 0.01})) != plain
    assert compute_config_digest(_config(org={"turnover_rate": 0.1})) != plain
    assert compute_config_digest(WorldConfig.model_validate(_BASE), backend="synth") != plain


# -- brief contract -----------------------------------------------------------


def test_parse_brief_reads_back_what_producers_write() -> None:
    text = "\n".join(
        [
            "Task: write the prose body of a status report.",
            "Title: Status Report.",
            "Date: 2026-02-13.",
            "Topic: report weekly progress.",
            "Authored by: Ada Cho, Ben Diaz.",
            "Reviewed by: Cleo Ueda.",
            "",
            facts_block(["The launch date for Checkout is 2026-05-14.", "Checkout is blocked."]),
            "",
            "ROSTER — refer ONLY to the following, by exactly these names:",
            "- Ada Cho",
        ]
    )
    brief = parse_brief(text)
    assert brief.kind == "status report"
    assert brief.title == "Status Report"
    assert brief.date == "2026-02-13"
    assert brief.authors == ("Ada Cho", "Ben Diaz")
    assert brief.reviewers == ("Cleo Ueda",)
    assert brief.facts == ("The launch date for Checkout is 2026-05-14.", "Checkout is blocked.")
    assert not brief.is_comment


def test_parse_brief_reads_a_review_comment_opener() -> None:
    brief = parse_brief(
        "Write Ada Cho's review comment on the design doc titled 'Checkout API'.\n"
        "This comment replies to an earlier comment in the thread."
    )
    assert brief.commenter == "Ada Cho" and brief.kind == "design doc"
    assert brief.title == "Checkout API" and brief.replying


def test_facts_block_is_empty_without_facts() -> None:
    assert facts_block([]) == ""
    assert facts_block(["x."]).startswith(FACTS_HEADER)


# -- synth backend ------------------------------------------------------------


def _synth(brief: str, candidates: tuple[str, ...] = ()) -> Any:
    prompt = assemble_prompt(system="sys", brief=brief)
    return SynthBackend().generate_content(
        prompt, candidate_references=candidates, model="m", temperature=0.0
    )


def test_synth_states_every_fact_verbatim_and_is_deterministic() -> None:
    facts = ["The launch date for Checkout is 2026-05-14.", "Vera Ueda owns Checkout day to day."]
    brief = "Task: write the prose body of a status report.\nTitle: Status.\n\n" + facts_block(
        facts
    )
    first = _synth(brief, ("a1", "a2", "a3"))
    second = _synth(brief, ("a1", "a2", "a3"))
    assert first.text == second.text
    for fact in facts:
        assert fact in first.text
    assert "[fake" not in first.text and "ROSTER" not in first.text
    assert first.references_used == ("a2", "a3")


def test_synth_prose_has_no_out_of_roster_names() -> None:
    from enterprise_sim.producers.grounding import Roster, detect_unresolved_names

    brief = "Task: write the prose body of a design doc.\nTitle: Checkout.\nTopic: checkout."
    for seed in range(20):
        text = _synth(brief + f"\nDate: 2026-01-{seed + 1:02d}.").text
        assert detect_unresolved_names(text, Roster(entries=())) == []


# -- the ledger ---------------------------------------------------------------


def _ledger_world(**facts: Any) -> tuple[Any, Any, FactsConfig]:
    config = _config(facts={"per_project": 6, **facts})
    world = build_world(config)
    ledger = build_ledger(
        world,
        config.facts,
        seed=config.seed,
        start=config.simulation.period_start,
        end=config.simulation.period_end,
    )
    return world, ledger, config.facts


def test_ledger_versions_tile_the_window() -> None:
    world, ledger, _ = _ledger_world(change_rate=0.9, max_versions=4)
    assert ledger.facts
    for record in ledger.facts.values():
        assert record.versions[0].valid_from == datetime(2026, 1, 5, 9, 0)
        for a, b in zip(record.versions, record.versions[1:], strict=False):
            assert a.valid_to == b.valid_from and a.value != b.value
            assert b.number == a.number + 1
        assert record.versions[-1].valid_to is None
        assert len(record.versions) <= 4
    assert any(len(r.versions) > 1 for r in ledger.facts.values())


def test_ledger_is_off_by_default() -> None:
    config = WorldConfig.model_validate(_BASE)
    world = build_world(config)
    ledger = build_ledger(
        world, config.facts, seed=4, start=date(2026, 1, 5), end=date(2026, 3, 27)
    )
    assert not ledger


def _events(project: str, n: int) -> list[Event]:
    return [
        Event(
            id=f"evt:{i:03d}",
            type="StatusReported",
            timestamp=datetime(2026, 1, 5 + (i % 20), 9, 0) if i < 20 else datetime(2026, 3, 20, 9),
            project=project,
            deliverable=Deliverable("status_report", "document"),
        )
        for i in range(n)
    ]


def test_assertions_follow_the_stance_rules() -> None:
    world, ledger, cfg = _ledger_world(
        change_rate=0.9, stale_rate=0.4, conflict_rate=0.3, hedge_rate=0.3, per_artifact="3..5"
    )
    project = sorted(ledger.by_project)[0]
    events = sorted(_events(project, 60), key=lambda e: (e.timestamp, e.id))
    out = assign_assertions(ledger, events, cfg, seed=4)
    stances: Counter[str] = Counter()
    announced: set[tuple[str, int]] = set()
    for event in events:
        for a in out.get(event.id, []):
            record = ledger.facts[a.fact]
            true = record.version_at(event.timestamp)
            assert a.true_version == true.number
            assert a.value in a.statement
            stances[a.stance] += 1
            if a.stance in ("current", "hedged", "update"):
                assert a.value == true.value
            if a.stance == "update":
                assert (a.fact, true.number) not in announced  # announced once
                announced.add((a.fact, true.number))
                assert record.versions[true.number - 2].value in a.statement
            if a.stance == "stale":
                assert a.stated_version is not None and a.stated_version < true.number
                assert a.value == record.versions[a.stated_version - 1].value
            if a.stance == "conflicting":
                assert a.stated_version is None
                assert all(a.value != v.value for v in record.versions)
    assert {"current", "update", "stale", "conflicting", "hedged"} <= set(stances)


def test_assignment_is_deterministic() -> None:
    def run() -> list[dict[str, Any]]:
        _, ledger, cfg = _ledger_world(change_rate=0.6)
        project = sorted(ledger.by_project)[0]
        out = assign_assertions(ledger, _events(project, 30), cfg, seed=4)
        return [a.to_payload() for k in sorted(out) for a in out[k]]

    assert run() == run()


# -- end to end ---------------------------------------------------------------


def _build(tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str) -> Path:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 4\n"
        '[company]\nname = "Facts Co"\nvertical = "software"\nsize = "medium"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-02-27\n" + body,
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    return world_dir


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_a_synth_world_states_its_facts_and_labels_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(
        tmp_path,
        capsys,
        '[model]\noffline_backend = "synth"\n[facts]\nper_project = 5\nchange_rate = 0.6\n',
    )
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    asserts = [e for e in edges if e["type"] == "asserts"]
    assert asserts
    assert Counter(n["type"] for n in nodes.values())["Fact"] > 0
    texts = {
        n["props"]["path"]: (world_dir / n["props"]["path"]).read_text(encoding="utf-8")
        for n in nodes.values()
        if n["type"] == "Artifact" and str(n["props"].get("path", "")).endswith(".md")
    }
    checked = 0
    for edge in asserts:
        artifact = nodes[edge["src"]]
        assert nodes[edge["dst"]]["type"] == "Fact"
        path = artifact["props"]["path"]
        if path in texts:
            assert edge["props"]["value"] in texts[path], (path, edge["props"])
            checked += 1
    assert checked > 0
    assert not any("[fake:" in text for text in texts.values())
    issues = _jsonl(world_dir / "validation" / "issues.jsonl")
    assert not [i for i in issues if i.get("kind") == "unresolved_mention"]


def test_a_fake_world_without_facts_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(tmp_path, capsys, "")
    nodes = _jsonl(world_dir / "kg" / "nodes.jsonl")
    assert not [n for n in nodes if n["type"] in ("Fact", "FactVersion")]
    events = _jsonl(world_dir / "kg" / "events.jsonl")
    assert not [e for e in events if "facts" in e.get("payload", {})]


def test_ledger_questions_join_the_world_benchmark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(
        tmp_path,
        capsys,
        '[model]\noffline_backend = "synth"\n[facts]\nper_project = 5\nchange_rate = 0.8\n'
        "stale_rate = 0.4\n",
    )
    from enterprise_sim.benchmark.generate import generate

    pairs = list(generate(world_dir).pairs)
    temporal = [p for p in pairs if p.reasoning_type == "temporal"]
    conflict = [p for p in pairs if p.reasoning_type == "conflict"]
    assert {p.qtype for p in temporal} >= {"current", "as_of", "count"}
    assert conflict
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    for pair in temporal:
        if pair.qtype in ("current", "as_of"):
            (vid,) = pair.expected_ids
            assert nodes[vid]["props"]["value"] == pair.expected_label
    for pair in conflict:
        assert all(nodes[a]["type"] == "Artifact" for a in pair.expected_ids)


def test_extending_the_window_never_rewrites_a_past_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Facts, turnover, noise and conflicts are drawn over a fixed horizon, not the window."""
    world_dir = _build(
        tmp_path,
        capsys,
        '[model]\noffline_backend = "synth"\n'
        "[facts]\nper_project = 8\nchange_rate = 0.7\nmax_versions = 5\nstale_rate = 0.3\n"
        "conflict_rate = 0.3\ncross_team = 1.0\n"
        "[messiness]\ntypo_rate = 0.02\nalias_rate = 0.3\ndate_format_mix = 0.4\n"
        "duplicate_rate = 0.2\nfiller_rate = 0.2\n"
        "[org]\nturnover_rate = 0.3\ntransfer_rate = 0.2\n",
    )
    before = {
        p.relative_to(world_dir): p.read_bytes()
        for p in (world_dir / "artifacts").rglob("*")
        if p.is_file()
    }
    assert main(["world", "extend", str(world_dir), "--period-end", "2026-04-24"]) == 0
    capsys.readouterr()
    after = {
        p.relative_to(world_dir): p.read_bytes()
        for p in (world_dir / "artifacts").rglob("*")
        if p.is_file()
    }
    assert len(after) > len(before)
    changed = [str(path) for path, data in before.items() if after.get(path) != data]
    assert changed == []


@pytest.mark.parametrize(
    ("value", "token"),
    [
        ("daily at 02:00 UTC", "02:00"),
        ("$250k", "$250k"),
        ("2026-05-14", "2026-05-14"),
        ("PostgreSQL", "PostgreSQL"),
        ("analytics.fct_orders", "analytics.fct_orders"),
        ("150 ms", "150"),
        ("the nightly crm extract", "nightly"),
    ],
)
def test_value_tokens_are_whitespace_free_substrings(value: str, token: str) -> None:
    from enterprise_sim.facts.catalog import value_token

    assert value_token(value) == token
    assert token in value and " " not in token


def test_money_values_have_no_grouping_separator() -> None:
    import random

    from enterprise_sim.facts.catalog import _money, _money_mutate, _money_value

    assert _money(250_000) == "$250k" and _money(1_200_000) == "$1.2M"
    assert _money_value("$1.2M") == 1_200_000 and _money_value("$250k") == 250_000
    rng = random.Random(1)
    value = "$900k"
    for _ in range(20):
        value = _money_mutate(rng, value, None)  # type: ignore[arg-type]
        assert "," not in value


def test_an_owner_who_leaves_hands_the_fact_to_their_manager() -> None:
    handed = 0
    for seed in range(1, 16):
        config = WorldConfig.model_validate(
            {
                **_BASE,
                "seed": seed,
                "simulation": {"period_start": "2026-01-05", "period_end": "2026-06-26"},
                "facts": {"per_project": 12, "change_rate": 0.0},
                "org": {"turnover_rate": 0.8},
            }
        )
        world = build_world(config)
        ledger = build_ledger(
            world,
            config.facts,
            seed=seed,
            start=config.simulation.period_start,
            end=config.simulation.period_end,
        )
        people = {str(n.props["name"]): n for n in world.nodes_by_type("Person")}
        for record in ledger.facts.values():
            if record.attribute.name != "owner":
                continue
            numbers = [v.number for v in record.versions]
            assert numbers == list(range(1, len(numbers) + 1))
            for a, b in zip(record.versions, record.versions[1:], strict=False):
                assert a.valid_to == b.valid_from
                leaver = people[a.value]
                left = leaver.props.get("left_at")
                if left is not None and b.valid_from == datetime.fromisoformat(str(left)):
                    (edge,) = world.out_edges(leaver.id, "reports_to")
                    assert people[b.value].id == edge.dst
                    handed += 1
    assert handed > 0


def test_entity_ids_name_the_project_in_every_statement() -> None:
    _, ledger, cfg = _ledger_world(change_rate=0.6)
    cfg = cfg.model_copy(update={"entity_ids": True})
    project = sorted(ledger.by_project)[0]
    out = assign_assertions(ledger, _events(project, 30), cfg, seed=4)
    assertions = [a for found in out.values() for a in found]
    assert assertions
    for a in assertions:  # each names its own fact's project (cited facts name theirs)
        slug = ledger.facts[a.fact].subject.split(":", 1)[-1]
        assert f"[{slug}]" in a.statement, a.statement
    del project


def test_retracted_versions_are_believed_until_corrected_then_announced() -> None:
    _, ledger, cfg = _ledger_world(change_rate=0.9, max_versions=4, retraction_rate=1.0)
    retracted = [
        (r, v) for r in ledger.facts.values() for v in r.versions if v.retracted_at is not None
    ]
    assert retracted
    for record, version in retracted:
        assert version.retracted_at is not None
        assert version.number > 1 and version.valid_from < version.retracted_at
        assert record.version_at(version.valid_from).number == version.number
        believed = record.version_at(version.retracted_at)
        assert believed.number != version.number
    project = next(
        p for p, ids in ledger.by_project.items() if any(r.id in ids for r, _ in retracted)
    )
    events = sorted(_events(project, 60), key=lambda e: (e.timestamp, e.id))
    out = assign_assertions(ledger, events, cfg, seed=4)
    corrections = [a for found in out.values() for a in found if a.stance == "retraction"]
    assert corrections
    for a in corrections:
        assert a.statement.startswith("Correction:") and a.value in a.statement


def test_retraction_off_changes_nothing() -> None:
    _, off, _ = _ledger_world(change_rate=0.9, max_versions=4)
    _, zero, _ = _ledger_world(change_rate=0.9, max_versions=4, retraction_rate=0.0)
    assert [[(v.value, v.valid_from) for v in r.versions] for r in off.facts.values()] == [
        [(v.value, v.valid_from) for v in r.versions] for r in zero.facts.values()
    ]
