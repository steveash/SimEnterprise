"""The live-call governor: shared slots, 429 backoff with jitter, the rate-limit trip."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.core.llm import LLMClient, LLMConfig, Prompt, build_client
from enterprise_sim.core.llm.governor import GATE_ENV, CallGovernor, RateLimitTripped
from enterprise_sim.core.llm.types import Completion, TokenUsage, TransientLLMError


def _prompt(n: int = 0) -> Prompt:
    from enterprise_sim.core.llm import assemble_prompt

    return assemble_prompt(system="sys", brief=f"brief {n}")


class _Scripted:
    """Answers per a script of outcomes: 'ok', '429' or '5xx'; then 'ok' forever."""

    name = "scripted"

    def __init__(self, script: Sequence[str] = (), *, hold: float = 0.0) -> None:
        self._script = list(script)
        self._hold = hold
        self._lock = threading.Lock()
        self.attempts = 0
        self.in_flight = 0
        self.peak = 0

    def _next(self, model: str) -> Completion:
        with self._lock:
            self.attempts += 1
            outcome = self._script.pop(0) if self._script else "ok"
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            time.sleep(self._hold)
            if outcome == "429":
                raise TransientLLMError("quota", rate_limited=True)
            if outcome == "5xx":
                raise TransientLLMError("server")
            return Completion(
                text="{}", usage=TokenUsage(input_tokens=1), model=model, structured={}
            )
        finally:
            with self._lock:
                self.in_flight -= 1

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:
        return self._next(model)

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        return self._next(model)


def _client(
    backend: _Scripted, governor: CallGovernor | None, sleeps: list[float], **cfg: Any
) -> LLMClient:
    return LLMClient(
        backend,
        config=LLMConfig(backend="scripted", cache_enabled=False, **cfg),
        sleep=sleeps.append,
        governor=governor,
    )


def test_a_rate_limit_backs_off_exponentially_with_jitter_never_immediately() -> None:
    sleeps: list[float] = []
    backend = _Scripted(["429"] * 6)
    client = _client(backend, None, sleeps, rate_limit_base_seconds=4.0)
    client.generate_structured(_prompt(), {"type": "object"})
    assert backend.attempts == 7 and len(sleeps) == 6
    for attempt, delay in enumerate(sleeps):
        step = min(180.0, 4.0 * 2**attempt)
        assert step / 2 <= delay <= step, (attempt, delay)
    assert min(sleeps) >= 2.0  # no immediate retry


def test_jitter_spreads_clients_refused_together() -> None:
    delays = []
    for _ in range(8):
        sleeps: list[float] = []
        _client(_Scripted(["429"]), None, sleeps).generate_structured(_prompt(), {})
        delays.append(sleeps[0])
    assert len(set(delays)) > 1


def test_retry_after_is_a_floor_for_a_rate_limit() -> None:
    class _After(_Scripted):
        def _next(self, model: str) -> Completion:
            self.attempts += 1
            if self.attempts == 1:
                raise TransientLLMError("quota", retry_after=30.0, rate_limited=True)
            return Completion(text="{}", usage=TokenUsage(), model=model, structured={})

    sleeps: list[float] = []
    _client(_After(), None, sleeps).generate_structured(_prompt(), {})
    assert sleeps == [30.0]


def test_rate_limit_and_transport_budgets_are_separate() -> None:
    sleeps: list[float] = []
    backend = _Scripted(["429", "429", "5xx", "429"])
    client = _client(backend, None, sleeps, max_retries=1, rate_limit_retries=3)
    client.generate_structured(_prompt(), {})
    assert backend.attempts == 5
    with pytest.raises(TransientLLMError):
        _client(_Scripted(["429"] * 4), None, [], rate_limit_retries=3).generate_structured(
            _prompt(), {}
        )


def test_slots_cap_attempts_across_clients(tmp_path: Path) -> None:
    backend = _Scripted(hold=0.05)
    clients = [
        _client(backend, CallGovernor(tmp_path / "gate", slots=2, poll_seconds=0.01), [])
        for _ in range(3)
    ]
    threads = [
        threading.Thread(target=c.generate_structured, args=(_prompt(i), {}))
        for i, c in enumerate(clients * 3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert backend.attempts == 9
    assert backend.peak == 2


def test_the_gate_trips_above_ten_percent_and_stops_every_client(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    governor = CallGovernor(gate, slots=2, min_samples=20)
    for _ in range(18):
        governor.record(rate_limited=False)
    governor.record(rate_limited=True)
    governor.record(rate_limited=True)  # 2 of 20 = 10%: not above the threshold
    assert not governor.stop_file.exists()
    with pytest.raises(RateLimitTripped, match="3 of 21"):
        governor.record(rate_limited=True)
    assert governor.stop_file.exists()
    other = _Scripted()
    with pytest.raises(RateLimitTripped):
        _client(other, CallGovernor(gate), []).generate_structured(_prompt(), {})
    assert other.attempts == 0


def test_a_tripped_gate_does_not_retry_the_refused_call(tmp_path: Path) -> None:
    governor = CallGovernor(tmp_path / "gate", min_samples=1)
    backend = _Scripted(["429"] * 5)
    with pytest.raises(RateLimitTripped):
        _client(backend, governor, []).generate_structured(_prompt(), {})
    assert backend.attempts == 1


def test_the_window_forgets_old_outcomes(tmp_path: Path) -> None:
    now = [1000.0]
    governor = CallGovernor(tmp_path / "gate", clock=lambda: now[0], min_samples=1)
    governor.record(rate_limited=False)
    assert governor.window_counts() == (1, 0)
    now[0] += 601
    assert governor.window_counts() == (0, 0)


def test_live_clients_take_the_gate_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GATE_ENV, str(tmp_path / "gate"))
    fake = build_client(LLMConfig(backend="fake"))
    assert fake._governor is None  # offline backends never take a slot
    live = build_client(LLMConfig(backend="anthropic_api"))
    assert live._governor is not None and live._governor.slots == 2
    pinned = build_client(LLMConfig(backend="bedrock", gate_dir=str(tmp_path / "g"), gate_slots=1))
    assert pinned._governor is not None and pinned._governor.slots == 1
    monkeypatch.delenv(GATE_ENV)
    assert build_client(LLMConfig(backend="anthropic_api"))._governor is None


def test_cache_hits_take_no_slot(tmp_path: Path) -> None:
    governor = CallGovernor(tmp_path / "gate", slots=1)
    backend = _Scripted()
    client = LLMClient(
        backend,
        config=LLMConfig(backend="scripted", cache_dir=str(tmp_path / "cache")),
        governor=governor,
    )
    client.generate_structured(_prompt(), {})
    governor.stop_file.write_text("manual\n")
    client.generate_structured(_prompt(), {})  # a cache hit: no slot, no check
    assert backend.attempts == 1
