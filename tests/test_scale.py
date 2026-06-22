"""Scale / perf tests: concurrency, cache locality, cost ceiling (esim-240e08c3).

Acceptance (ARCHITECTURE.md §7/§16.4, D13/D26/D29):
* a large-company / 1-month run completes within a cost ceiling;
* a dry-run estimate (artifact count × token estimate) is produced before render
  and gates a too-expensive run *before* any model call;
* the render phase is bounded-concurrency and deterministic regardless of the
  concurrency level (cache-locality clustering preserved).

Everything runs on the deterministic, network-free ``fake`` backend, so the suite
is free and fast.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.assembly import (
    RenderEstimate,
    build_corpus,
    estimate_run,
    execute_run,
    llm_config_for,
)
from enterprise_sim.cli import main
from enterprise_sim.core.config import RunConfig, ScaleConfig, load_config_from_mapping
from enterprise_sim.core.llm import (
    Completion,
    CostCeilingExceeded,
    LLMClient,
    LLMConfig,
    Prompt,
    TokenUsage,
    build_client,
)
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.world_builders import build_world


def _config(
    output_dir: Path,
    *,
    size: str = "enterprise",
    vertical: str = "software",
    seed: int = 5,
    end: str = "2026-01-31",
    scale: dict[str, Any] | None = None,
) -> RunConfig:
    body: dict[str, Any] = {
        "company": {"name": "BigCo", "vertical": vertical, "size": size},
        "simulation": {"period_start": "2026-01-01", "period_end": end},
        "seed": seed,
        "output_dir": str(output_dir),
    }
    if scale is not None:
        body["scale"] = scale
    return load_config_from_mapping(body)


# ---------------------------------------------------------------------------
# ScaleConfig schema (defaults + validation)
# ---------------------------------------------------------------------------


def test_scale_config_has_safe_defaults() -> None:
    scale = ScaleConfig()
    assert scale.max_concurrency == 8
    assert scale.cost_ceiling_usd is None
    assert scale.cache_enabled is True
    assert scale.est_input_tokens_per_artifact > 0
    assert scale.est_output_tokens_per_artifact > 0


def test_run_config_defaults_a_scale_block() -> None:
    config = load_config_from_mapping(
        {
            "company": {"name": "C", "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
        }
    )
    assert isinstance(config.scale, ScaleConfig)


@pytest.mark.parametrize("field,value", [("max_concurrency", 0), ("cost_ceiling_usd", -1.0)])
def test_scale_config_rejects_invalid(field: str, value: object) -> None:
    kwargs: dict[str, Any] = {field: value}
    with pytest.raises(ValueError):
        ScaleConfig(**kwargs)


# ---------------------------------------------------------------------------
# RunConfig -> LLMConfig bridge
# ---------------------------------------------------------------------------


def test_llm_config_for_carries_scale_knobs(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        scale={"max_concurrency": 3, "cost_ceiling_usd": 12.5, "cache_enabled": False},
    )
    llm = llm_config_for(config)
    assert llm.backend == "fake"  # network-free default
    assert llm.model == config.model.name
    assert llm.max_concurrency == 3
    assert llm.cost_ceiling_usd == 12.5
    assert llm.cache_enabled is False


def test_llm_config_for_allows_real_backend(tmp_path: Path) -> None:
    llm = llm_config_for(_config(tmp_path), backend="anthropic_api")
    assert llm.backend == "anthropic_api"


# ---------------------------------------------------------------------------
# Dry-run estimate (D13)
# ---------------------------------------------------------------------------


def test_estimate_run_counts_artifacts_and_prices_them(tmp_path: Path) -> None:
    estimate = estimate_run(_config(tmp_path))
    assert isinstance(estimate, RenderEstimate)
    assert estimate.num_artifacts > 0
    assert estimate.estimated_cost_usd > 0
    # Estimate scales as count × per-artifact price, so it is recomputable.
    assert estimate.estimated_cost_usd == pytest.approx(
        estimate.num_artifacts
        * (
            estimate.input_tokens_each * 15.0  # opus input $/Mtok
            + estimate.output_tokens_each * 75.0  # opus output $/Mtok
        )
        / 1_000_000.0
    )


def test_estimate_run_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    estimate_run(_config(out))
    assert not out.exists()  # dry-run never materializes a run directory


def test_estimate_run_makes_no_model_calls(tmp_path: Path) -> None:
    client = build_client(LLMConfig(backend="fake"))
    estimate_run(_config(tmp_path), client=client)
    assert client.cost.calls == 0  # purely a pre-render estimate


def test_dry_run_estimate_matches_actual_count(tmp_path: Path) -> None:
    config = _config(tmp_path)
    estimate = estimate_run(config)
    result = execute_run(config)
    assert estimate.num_artifacts == len(result.corpus.artifacts)


# ---------------------------------------------------------------------------
# Cost ceiling (D13) — gated before any render
# ---------------------------------------------------------------------------


def test_low_ceiling_aborts_before_render(tmp_path: Path) -> None:
    config = _config(tmp_path, scale={"cost_ceiling_usd": 0.0001})
    client = build_client(llm_config_for(config))
    with pytest.raises(CostCeilingExceeded):
        execute_run(config, client=client)
    assert client.cost.calls == 0  # aborted at the gate, nothing rendered


def test_generous_ceiling_completes(tmp_path: Path) -> None:
    config = _config(tmp_path, scale={"cost_ceiling_usd": 1000.0})
    result = execute_run(config)
    assert len(result.corpus.artifacts) > 0
    assert result.corpus.estimate is not None
    assert result.corpus.estimate.estimated_cost_usd <= 1000.0


def test_estimate_surfaced_on_run_result(tmp_path: Path) -> None:
    result = execute_run(_config(tmp_path))
    assert result.corpus.estimate is not None
    assert result.corpus.estimate.num_artifacts == len(result.corpus.artifacts)


# ---------------------------------------------------------------------------
# Large run acceptance: completes within ceiling, bounded concurrency
# ---------------------------------------------------------------------------


def test_large_company_one_month_run_within_ceiling(tmp_path: Path) -> None:
    # Acceptance: a large-company / 1-month run completes within a cost ceiling,
    # under bounded concurrency, and lands a full corpus.
    config = _config(
        tmp_path,
        size="enterprise",
        end="2026-01-31",
        scale={"max_concurrency": 4, "cost_ceiling_usd": 100.0},
    )
    estimate = estimate_run(config)
    assert estimate.estimated_cost_usd <= 100.0  # gate would pass

    result = execute_run(config)
    assert len(result.corpus.artifacts) > 0
    md_files = list((result.run_dir / "artifacts").rglob("*.md"))
    assert len(md_files) == len(result.corpus.artifacts)


# ---------------------------------------------------------------------------
# Bounded concurrency (§16.4) — the render fan-out never exceeds the cap
# ---------------------------------------------------------------------------


class _PeakBackend:
    """A backend that records the peak number of concurrently in-flight calls."""

    name = "peak"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def _enter(self) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def _exit(self) -> None:
        with self._lock:
            self.active -= 1

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:
        self._enter()
        try:
            time.sleep(0.01)  # widen the overlap window so the peak is observable
            return Completion(
                text="{}",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                model=model,
                structured={},
            )
        finally:
            self._exit()

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        raise NotImplementedError


def test_generate_many_never_exceeds_max_concurrency() -> None:
    backend = _PeakBackend()
    client = LLMClient(backend, config=LLMConfig(backend="peak", max_concurrency=3))
    schema = {"type": "object"}
    tasks = [(lambda c, i=i: c.generate_structured(_unique_prompt(i), schema)) for i in range(24)]
    client.generate_many(tasks)
    assert backend.peak <= 3
    assert backend.peak >= 2  # with 24 tasks the pool genuinely overlaps


def test_max_concurrency_one_is_serial() -> None:
    backend = _PeakBackend()
    client = LLMClient(backend, config=LLMConfig(backend="peak", max_concurrency=1))
    schema = {"type": "object"}
    tasks = [(lambda c, i=i: c.generate_structured(_unique_prompt(i), schema)) for i in range(6)]
    client.generate_many(tasks)
    assert backend.peak == 1


def _unique_prompt(i: int) -> Prompt:
    from enterprise_sim.core.llm import assemble_prompt

    return assemble_prompt(system="s", stable_context=[], brief=f"brief {i}")


# ---------------------------------------------------------------------------
# Determinism preserved under concurrency (D26)
# ---------------------------------------------------------------------------


def _corpus_blob(result: object) -> dict[str, str]:
    return {a.path: a.body for a in result.corpus.artifacts}  # type: ignore[attr-defined]


def test_corpus_identical_regardless_of_concurrency(tmp_path: Path) -> None:
    # D26: concurrency changes only how fast it renders, never what is rendered.
    serial = execute_run(_config(tmp_path / "a", scale={"max_concurrency": 1}))
    parallel = execute_run(_config(tmp_path / "b", scale={"max_concurrency": 8}))
    assert serial.run_id == parallel.run_id
    assert _corpus_blob(serial) == _corpus_blob(parallel)


def test_scale_does_not_change_run_identity(tmp_path: Path) -> None:
    # Scale is operational (where/how fast), not content — like output_dir, it is
    # excluded from the config digest, so the run id and manifest stay stable.
    from enterprise_sim.assembly import compute_config_digest, compute_run_id

    fast = _config(tmp_path / "a", scale={"max_concurrency": 8, "cost_ceiling_usd": 9.0})
    slow = _config(tmp_path / "b", scale={"max_concurrency": 1})
    assert compute_config_digest(fast) == compute_config_digest(slow)
    assert compute_run_id(fast) == compute_run_id(slow)


def test_multi_scenario_corpus_is_reproducible(tmp_path: Path) -> None:
    # A multi-scenario enterprise world over a longer window, twice, byte-stable.
    cfg_a = _config(
        tmp_path / "a", size="enterprise", end="2026-02-28", scale={"max_concurrency": 4}
    )
    cfg_b = _config(
        tmp_path / "b", size="enterprise", end="2026-02-28", scale={"max_concurrency": 4}
    )
    a = execute_run(cfg_a)
    b = execute_run(cfg_b)
    assert _corpus_blob(a) == _corpus_blob(b)


def test_build_corpus_dry_run_renders_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    world = build_world(config)
    client = build_client(llm_config_for(config))
    result = build_corpus(world, config, client, dry_run=True)
    assert result.artifacts == ()
    assert result.estimate is not None
    assert result.estimate.num_artifacts > 0
    assert client.cost.calls == 0
    # The journal is still populated — scheduling (Layer B) ran.
    assert len(result.journal) > 0


# ---------------------------------------------------------------------------
# World.copy() — render-isolation primitive
# ---------------------------------------------------------------------------


def _tiny_world() -> World:
    from datetime import datetime

    world = World()
    ts = datetime(2026, 1, 1, 9, 0)
    world.add_node(Node(id="person:ada", type="Person", created_at=ts, props={"name": "Ada"}))
    world.add_node(Node(id="project:x", type="Project", created_at=ts, props={"name": "X"}))
    world.add_edge(
        Edge(
            id="edge:on:ada->x",
            type="works_on",
            src="person:ada",
            dst="project:x",
            created_at=ts,
        )
    )
    return world


def test_world_copy_is_independent() -> None:
    original = _tiny_world()
    clone = original.copy()
    assert clone.to_dict() == original.to_dict()

    from datetime import datetime

    clone.add_node(Node(id="person:bob", type="Person", created_at=datetime(2026, 1, 2, 9, 0)))
    clone_ada = clone.get_node("person:ada")
    assert clone_ada is not None
    clone_ada.props["name"] = "Mutated"

    assert original.get_node("person:bob") is None  # structural isolation
    original_ada = original.get_node("person:ada")
    assert original_ada is not None
    assert original_ada.props["name"] == "Ada"  # deep-copied props


def test_world_copy_preserves_queries() -> None:
    original = _tiny_world()
    clone = original.copy()
    assert [n.id for n in clone.nodes_by_type("Person")] == ["person:ada"]
    assert [e.id for e in clone.out_edges("person:ada")] == ["edge:on:ada->x"]


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def _write_config(path: Path, *, ceiling: float | None = None) -> Path:
    lines = [
        "seed = 5",
        '[company]\nname = "BigCo"\nvertical = "software"\nsize = "enterprise"',
        "[simulation]\nperiod_start = 2026-01-01\nperiod_end = 2026-01-31",
    ]
    if ceiling is not None:
        lines.append(f"[scale]\ncost_ceiling_usd = {ceiling}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_cli_dry_run_writes_nothing(tmp_path: Path, capsys: Any) -> None:
    cfg = _write_config(tmp_path / "demo.toml")
    out = tmp_path / "out"
    assert main(["run", "-c", str(cfg), "-o", str(out), "--dry-run"]) == 0
    assert not out.exists()
    assert "dry-run" in capsys.readouterr().out


def test_cli_dry_run_ceiling_override_fails(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "demo.toml")
    out = tmp_path / "out"
    assert (
        main(["run", "-c", str(cfg), "-o", str(out), "--dry-run", "--cost-ceiling", "0.0001"]) == 1
    )
    assert not out.exists()


def test_cli_cost_ceiling_override_aborts_full_run(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "demo.toml")
    out = tmp_path / "out"
    assert main(["run", "-c", str(cfg), "-o", str(out), "--cost-ceiling", "0.0001"]) == 1
    assert not out.exists()  # aborted before any output written


def test_cli_max_concurrency_override_runs(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "demo.toml")
    out = tmp_path / "out"
    assert main(["run", "-c", str(cfg), "-o", str(out), "--max-concurrency", "2"]) == 0
    assert len(list(out.iterdir())) == 1
