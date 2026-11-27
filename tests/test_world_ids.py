"""Example world ids are pinned: a new config field at its default must not move them.

The ids below are the ones the dataset catalog pins (and the golden world's).
If a change moves one, either it changes the world (bump the example on purpose
and tell the catalog) or a new field is missing from runner._LATE_FIELDS.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from enterprise_sim.assembly import compute_world_id
from enterprise_sim.core.config import load_config

_ROOT = Path(__file__).resolve().parents[1] / "examples"

PINNED = {
    "golden.toml": "golden-slice-co-5b1e84522235",
    "worlds/coding-team-clean.toml": "larkspur-labs-be9528a22f71",
    "worlds/coding-team-messy.toml": "quillfeather-software-37ace7b3d492",
    "worlds/data-eng-platform.toml": "brightwater-data-4ce82a188ccf",
    "worlds/analytics-structured-clean.toml": "corvid-analytics-876dab12c875",
    "worlds/analytics-structured-messy.toml": "corvid-analytics-251be658ee48",
    # [data] is not part of a world's id: a data-only variant shares its twin's id.
    "worlds/analytics-structured-messy-varied.toml": "corvid-analytics-251be658ee48",
    "worlds/analytics-structured-messy-m.toml": "corvid-analytics-6956ae50b9a3",
    "worlds/analytics-structured-messy-varied-m.toml": "corvid-analytics-6956ae50b9a3",
    "worlds/data-eng-platform-attributes.toml": "brightwater-data-4ce82a188ccf",
    "worlds/coding-team-sessions.toml": "larkspur-labs-6cea26b7e38b",
    "worlds/analytics-homonyms.toml": "corvid-analytics-c16b7775e533",
    "worlds/analytics-reorg.toml": "corvid-analytics-3cbc904785d6",
    "worlds/analytics-merger.toml": "corvid-analytics-43bb577ed0ae",
    "worlds/docs-only-professional-bonus-clean.toml": "harrow-vale-advisory-cba54c864d9b",
    "worlds/docs-only-professional-bonus-messy.toml": "harrow-vale-advisory-cba54c864d9b",
    "worlds/enterprise-service-latency.toml": "meridian-holdings-8ea3a90f92f0",
    "worlds/enterprise-ap.toml": "meridian-holdings-8ea3a90f92f0",
    "worlds/enterprise-ap-month-end.toml": "meridian-holdings-8ea3a90f92f0",
    "worlds/ops-sre.toml": "tessellate-cloud-2866bc14c03c",
    "worlds/ops-sre-access.toml": "tessellate-cloud-2866bc14c03c",
    "worlds/ops-sre-concurrent.toml": "tessellate-cloud-bbe8d5c52137",
    "worlds/enterprise-concurrent.toml": "meridian-holdings-3c8e86496b9e",
    "worlds/access-fixture-small.toml": "corvid-analytics-c6b0e22966ae",
    "worlds/access-fixture-tiny.toml": "fixture-tiny-b5f3436f1728",
    "worlds/dataeng-pipeline-runs.toml": "basalt-digital-0436d7b7368d",
    "worlds/ladder/ladder-s.toml": "corvid-analytics-c6b0e22966ae",
    "worlds/ladder/ladder-m.toml": "corvid-analytics-b37b855d7066",
    "worlds/ladder/ladder-l.toml": "corvid-analytics-1af9e07d410e",
    "worlds/ladder/tiered/ladder-m.toml": "corvid-analytics-2b62b9268e09",
    "worlds/ladder/tiered/ladder-l.toml": "corvid-analytics-95ccf3ed897c",
    "worlds/docs-only-professional.toml": "harrow-vale-advisory-cba54c864d9b",
    "worlds/retail-chain-ops.toml": "tallow-street-goods-7220fc0e985a",
    "worlds/enterprise-sprawl.toml": "meridian-holdings-8ea3a90f92f0",
    "worlds/startup-long-horizon.toml": "fennel-robotics-506abcb1dc54",
}


@pytest.mark.parametrize(("config", "world_id"), sorted(PINNED.items()))
def test_example_world_ids_are_stable(config: str, world_id: str) -> None:
    cfg = load_config(_ROOT / config)
    backend = cfg.model.offline_backend.value
    assert compute_world_id(cfg, backend=backend) == world_id
