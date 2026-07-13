"""Shared pytest fixtures (spec 0002).

The one cross-cutting fixture today is the record-mode cassette redaction guard: after a
keyed ``ESIM_CASSETTES=record`` session wrote cassettes, scan every recorded file for a
credential before the session ends (belt-and-braces over ``Completion.to_dict``, which
cannot carry one by construction — spec 0002 §1). In the default keyless/replay session
it is a no-op, so it never touches the quality gate.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.llm_stubs import CASSETTE_ROOT, recording_enabled, scan_cassette_for_secrets


@pytest.fixture(scope="session", autouse=True)
def _redact_recorded_cassettes() -> Iterator[None]:
    """Scan every recorded cassette for a leaked credential once record mode finishes."""
    yield
    if not (recording_enabled() and CASSETTE_ROOT.is_dir()):
        return
    for scenario_dir in sorted(p for p in CASSETTE_ROOT.iterdir() if p.is_dir()):
        scan_cassette_for_secrets(scenario_dir)
