"""Schema, validation, and file-loading tests for run configs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from enterprise_sim.core.config import (
    CompanySize,
    ConfigError,
    LLMBackend,
    RunConfig,
    load_config,
    load_config_from_mapping,
)
from pydantic import ValidationError

_EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "demo.toml"


def _minimal_mapping() -> dict[str, object]:
    return {
        "company": {"name": "Acme", "vertical": "software", "size": "small"},
        "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
    }


def test_sample_config_loads_and_validates() -> None:
    config = load_config(_EXAMPLE)
    assert isinstance(config, RunConfig)
    assert config.company.name == "Northwind Tools"
    assert config.company.size is CompanySize.SMALL
    assert config.seed == 42
    assert config.output_dir == Path("runs/demo")
    assert config.simulation.period_start == date(2026, 1, 1)
    assert config.model.backend is LLMBackend.ANTHROPIC_API
    assert len(config.projects) == 2
    assert config.projects[0].name == "Onboarding revamp"


def test_defaults_applied_for_minimal_config() -> None:
    config = load_config_from_mapping(_minimal_mapping())
    assert config.seed == 0
    assert config.output_dir == Path("runs")
    assert config.projects == ()
    # The engine's actual default render backend is the deterministic ``fake`` (D31),
    # so a config with no [model] block records that rather than claiming a real one.
    assert config.model.backend is LLMBackend.FAKE
    assert config.model.realism == pytest.approx(0.7)


def test_fake_backend_selectable_from_config() -> None:
    data = _minimal_mapping()
    data["model"] = {"backend": "fake"}
    config = load_config_from_mapping(data)
    assert config.model.backend is LLMBackend.FAKE


def test_model_aws_region_profile_default_none() -> None:
    config = load_config_from_mapping(_minimal_mapping())
    assert config.model.aws_region is None
    assert config.model.aws_profile is None


def test_model_aws_region_profile_round_trip_to_llm_config() -> None:
    # A [model] block with Bedrock overrides must carry through llm_config_for onto
    # the LLMConfig the client is built from (spec 0001, slice 2).
    from enterprise_sim.assembly.runner import llm_config_for

    data = _minimal_mapping()
    data["model"] = {"backend": "bedrock", "aws_region": "us-west-2", "aws_profile": "sim"}
    config = load_config_from_mapping(data)
    assert config.model.aws_region == "us-west-2"
    assert config.model.aws_profile == "sim"

    llm_config = llm_config_for(config, backend="bedrock")
    assert llm_config.aws_region == "us-west-2"
    assert llm_config.aws_profile == "sim"


def test_backend_enum_matches_backend_factory() -> None:
    # Config files, the CLI --backend choices, and build_backend must accept the
    # same names; a backend added to one place has to land in the others.
    from enterprise_sim.cli import _BACKEND_CHOICES
    from enterprise_sim.core.llm import build_backend

    for backend in LLMBackend:
        assert build_backend(backend.value).name == backend.value

    # The CLI's shared --backend choices constant (finding F7) is the enum's values
    # in enum order; asserting it here keeps the six argparse sites from drifting.
    assert _BACKEND_CHOICES == tuple(backend.value for backend in LLMBackend)


def test_config_is_frozen() -> None:
    config = load_config_from_mapping(_minimal_mapping())
    with pytest.raises(ValidationError):
        config.seed = 5


def test_unknown_key_is_rejected() -> None:
    data = _minimal_mapping()
    data["nonsense"] = True
    with pytest.raises(ValidationError):
        load_config_from_mapping(data)


def test_inverted_period_is_rejected() -> None:
    data = _minimal_mapping()
    data["simulation"] = {"period_start": "2026-02-01", "period_end": "2026-01-01"}
    with pytest.raises(ValidationError, match="period_end"):
        load_config_from_mapping(data)


def test_invalid_size_is_rejected() -> None:
    data = _minimal_mapping()
    data["company"] = {"name": "Acme", "vertical": "software", "size": "ginormous"}
    with pytest.raises(ValidationError):
        load_config_from_mapping(data)


def test_empty_company_name_is_rejected() -> None:
    data = _minimal_mapping()
    data["company"] = {"name": "", "vertical": "software", "size": "small"}
    with pytest.raises(ValidationError):
        load_config_from_mapping(data)


def test_realism_out_of_range_is_rejected() -> None:
    data = _minimal_mapping()
    data["model"] = {"realism": 1.5}
    with pytest.raises(ValidationError):
        load_config_from_mapping(data)


def test_negative_seed_is_rejected() -> None:
    data = _minimal_mapping()
    data["seed"] = -1
    with pytest.raises(ValidationError):
        load_config_from_mapping(data)


def test_missing_file_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config("does-not-exist.toml")


def test_unsupported_extension_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("company: {}\n")
    with pytest.raises(ConfigError, match="unsupported config extension"):
        load_config(path)


def test_malformed_toml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("this is = = not toml")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_json_config_loads(tmp_path: Path) -> None:
    import json

    path = tmp_path / "config.json"
    path.write_text(json.dumps(_minimal_mapping()))
    config = load_config(path)
    assert config.company.name == "Acme"


def test_non_mapping_root_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="must be a table/object"):
        load_config_from_mapping([1, 2, 3])
