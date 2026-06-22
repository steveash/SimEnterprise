"""Load and validate a run config from a TOML or JSON file.

Thin I/O layer over :class:`~enterprise_sim.core.config.models.RunConfig`: read
the file, parse it by extension, and hand the mapping to Pydantic for
validation. Pydantic raises ``ValidationError`` on bad data; this module adds a
:class:`ConfigError` only for the I/O-and-parse concerns it owns (missing file,
unknown extension, malformed syntax).
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from enterprise_sim.core.config.models import RunConfig


class ConfigError(Exception):
    """Raised when a config file cannot be read or parsed (not for validation)."""


def load_config(path: str | Path) -> RunConfig:
    """Read, parse, and validate a run config from ``path``.

    The format is chosen by file extension: ``.toml`` or ``.json``.

    Args:
        path: Path to a ``.toml`` or ``.json`` config file.

    Returns:
        The validated :class:`RunConfig`.

    Raises:
        ConfigError: The file is missing, has an unsupported extension, or
            contains malformed TOML/JSON.
        pydantic.ValidationError: The parsed data violates the schema.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc

    if suffix == ".toml":
        try:
            data: Any = tomllib.loads(raw.decode("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    elif suffix == ".json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    else:
        raise ConfigError(f"unsupported config extension {suffix!r} for {path}; use .toml or .json")

    return load_config_from_mapping(data, source=path)


def load_config_from_mapping(data: Any, *, source: str | Path | None = None) -> RunConfig:
    """Validate an already-parsed mapping into a :class:`RunConfig`.

    Args:
        data: The parsed config, expected to be a mapping at the top level.
        source: Optional path, used only to enrich the error message.

    Raises:
        ConfigError: ``data`` is not a mapping.
        pydantic.ValidationError: The data violates the schema.
    """
    if not isinstance(data, Mapping):
        where = f" in {source}" if source is not None else ""
        raise ConfigError(f"config root must be a table/object{where}, got {type(data).__name__}")
    return RunConfig.model_validate(data)
