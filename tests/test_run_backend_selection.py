"""Backend selection for ``enterprise-sim run`` (ARCHITECTURE.md §7).

A run is network-free and reproducible out of the box, so the CLI renders against
the deterministic ``fake`` backend unless the operator explicitly opts in with
``--live``. These tests pin both halves of that contract:

* **default** — the config's ``[model] backend`` is *not* consulted, so a config
  naming ``anthropic_api`` still renders deterministically and free;
* **``--live``** — the config's backend is honored, which is the only way the
  CLI reaches a real provider.

The gap these cover was a real bug: ``_client_for`` hardcoded ``fake`` with no
opt-in at all, so ``[model] backend`` was silently dead from the CLI and a
"successful" run could quietly emit ``FakeBackend`` prompt echoes as corpus prose.

No network happens here: constructing a backend never calls out, and these tests
only inspect which backend was selected.
"""

from __future__ import annotations

from datetime import date

import pytest
from enterprise_sim.assembly.runner import _client_for
from enterprise_sim.cli import build_parser
from enterprise_sim.core.config.models import (
    CompanyConfig,
    CompanySize,
    LLMBackend,
    ModelConfig,
    RunConfig,
    SimulationConfig,
)


def _config(backend: LLMBackend) -> RunConfig:
    return RunConfig(
        company=CompanyConfig(name="Acme", vertical="software", size=CompanySize.STARTUP),
        simulation=SimulationConfig(period_start=date(2026, 1, 5), period_end=date(2026, 1, 9)),
        seed=7,
        model=ModelConfig(backend=backend, name="claude-haiku-4-5"),
    )


def test_default_run_ignores_the_config_backend_and_stays_deterministic() -> None:
    """Without ``--live`` a run never touches the network, whatever the config says."""
    client = _client_for(_config(LLMBackend.ANTHROPIC_API), None)
    assert client.config.backend == "fake"


@pytest.mark.parametrize(
    "backend",
    [LLMBackend.ANTHROPIC_API, LLMBackend.BEDROCK, LLMBackend.CLAUDE_CLI],
)
def test_live_honors_the_configured_backend(backend: LLMBackend) -> None:
    client = _client_for(_config(backend), None, live=True)
    assert client.config.backend == backend.value


def test_live_still_carries_the_configured_model() -> None:
    client = _client_for(_config(LLMBackend.CLAUDE_CLI), None, live=True)
    assert client.config.model == "claude-haiku-4-5"


def test_an_explicit_client_wins_over_live() -> None:
    """``live`` is ignored when the caller supplies its own client."""
    injected = _client_for(_config(LLMBackend.ANTHROPIC_API), None)  # a fake client
    assert _client_for(_config(LLMBackend.CLAUDE_CLI), injected, live=True) is injected


def test_run_parser_defaults_live_off() -> None:
    args = build_parser().parse_args(["run", "examples/golden.toml"])
    assert args.live is False


def test_run_parser_accepts_live() -> None:
    args = build_parser().parse_args(["run", "examples/golden.toml", "--live"])
    assert args.live is True
