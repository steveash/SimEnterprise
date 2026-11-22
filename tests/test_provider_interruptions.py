"""A provider interruption stops the build at every catch site; it is never 'repaired'.

Invariant: a tripped call gate, the cost ceiling, or a rate limit whose retries
ran out (``is_interruption``) propagates out of every best-effort or repair
path, so no world is finished with fallback content in place of live content.
The table lists every such site; a new one must join it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.core.llm import LLMClient, LLMConfig, Prompt
from enterprise_sim.core.llm.types import (
    Completion,
    CostCeilingExceeded,
    LLMError,
    RateLimitTripped,
    TransientLLMError,
    is_interruption,
)

_START, _END = date(2026, 1, 5), date(2026, 2, 27)


class _Raises:
    """A backend whose every call raises ``error``."""

    name = "raises"

    def __init__(self, error: LLMError) -> None:
        self._error = error
        self.attempts = 0

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:
        self.attempts += 1
        raise self._error

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        self.attempts += 1
        raise self._error


def _client(backend: _Raises) -> LLMClient:
    return LLMClient(
        backend,
        config=LLMConfig(
            backend="raises", cache_enabled=False, max_retries=0, rate_limit_retries=0
        ),
        sleep=lambda _: None,
    )


def _spec() -> Any:
    from enterprise_sim.data_products.scenarios import discover_scenarios

    return discover_scenarios().get("support_ops").build_spec(start=_START, end=_END)


def _author_spec(client: LLMClient) -> object:
    from enterprise_sim.data_products import authoring
    from enterprise_sim.data_products.scenarios import discover_scenarios

    scenario = discover_scenarios().get("support_ops")
    return authoring.author_spec(
        scenario, start=_START, end=_END, mode="llm", client=client, world=None
    )


def _generate_questions(client: LLMClient) -> object:
    from enterprise_sim.data_products import authoring

    return authoring.generate_questions(_spec(), client=client, existing_ids=[])


def _propose_gap_fixes(client: LLMClient) -> object:
    from enterprise_sim.data_products import authoring

    spec = _spec()
    return authoring.propose_gap_fixes(spec, [(spec.questions[0], "no such column")], client=client)


def _llm_critique(client: LLMClient) -> object:
    from enterprise_sim.benchmark.schema import QAPair
    from enterprise_sim.core.world import World
    from enterprise_sim.evals.critic import llm_critique

    pair = QAPair(
        id="q1",
        question="Who leads Checkout?",
        qtype="who",
        reasoning_type="direct_relation",
        expected_ids=("person:a",),
    )
    return llm_critique([pair], World(), client)


SITES: dict[str, Callable[[LLMClient], object]] = {
    "data authoring: author_spec": _author_spec,
    "data authoring: generate_questions": _generate_questions,
    "data authoring: propose_gap_fixes": _propose_gap_fixes,
    "evals critic: llm_critique": _llm_critique,
}

INTERRUPTIONS: dict[str, LLMError] = {
    "gate tripped": RateLimitTripped("call gate stopped"),
    "cost ceiling": CostCeilingExceeded(2.0, 1.0),
    "rate limited": TransientLLMError("429", rate_limited=True),
}


@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize("kind", sorted(INTERRUPTIONS))
def test_every_site_propagates_an_interruption_after_one_attempt(site: str, kind: str) -> None:
    backend = _Raises(INTERRUPTIONS[kind])
    with pytest.raises(type(INTERRUPTIONS[kind])):
        SITES[site](_client(backend))
    assert backend.attempts == 1  # no repair prompt, no immediate retry


@pytest.mark.parametrize("site", sorted(SITES))
def test_a_bad_sample_is_still_repaired_or_skipped(site: str) -> None:
    backend = _Raises(LLMError("unusable sample"))
    SITES[site](_client(backend))  # does not raise
    assert backend.attempts >= 1


def test_what_counts_as_an_interruption() -> None:
    assert all(is_interruption(e) for e in INTERRUPTIONS.values())
    assert not is_interruption(TransientLLMError("5xx"))
    assert not is_interruption(LLMError("bad json"))
    assert not is_interruption(ValueError("x"))


def test_an_interrupted_data_phase_fails_the_job_resumably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import enterprise_sim.data_products as data_products
    from enterprise_sim.cli import main
    from enterprise_sim.worlds.file import read_world

    def stopped(*_: Any, **__: Any) -> Any:
        raise RateLimitTripped("call gate stopped: test")

    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 5\n"
        '[company]\nname = "Data Co"\nvertical = "analytics"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-01-16\n"
        '[data]\nscenarios = ["support_ops"]\nscale_factor = 0.05\niterations = 1\n',
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    monkeypatch.setattr(data_products, "execute_data_run", stopped)
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) != 0
    (world_dir,) = list(root.iterdir())
    world = read_world(world_dir)
    assert world is not None
    assert world.job.status == "failed"
    assert world.job.error is not None and world.job.error["type"] == "RateLimitTripped"
    monkeypatch.undo()
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    resumed = read_world(world_dir)
    assert resumed is not None and resumed.job.status == "done"
    assert (world_dir / "data" / "support_ops" / "spec.json").exists()
