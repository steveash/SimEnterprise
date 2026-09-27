"""The ``sell_merchandise`` playbook template registers, lints clean, and conforms."""

from __future__ import annotations

from enterprise_sim.authoring.lint import lint_playbook
from enterprise_sim.authoring.testkit import check_playbook
from enterprise_sim.core.registry import (
    PLAYBOOKS,
    PROCESSES,
    TEMPLATE_MODULE_PREFIX,
    load_templates,
)


def test_sell_merchandise_registers_lints_and_conforms() -> None:
    load_templates()
    playbook = PLAYBOOKS.get("sell_merchandise").build()  # type: ignore[attr-defined]
    assert lint_playbook(playbook).ok
    assert check_playbook(playbook) == []
    mine = [
        n
        for n in PROCESSES.names()
        if PROCESSES.origin(n) == f"{TEMPLATE_MODULE_PREFIX}.sell_merchandise"
    ]
    assert len(mine) == 5
