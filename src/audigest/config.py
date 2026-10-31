"""Small, explicit configuration loader with CLI > environment > file precedence."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_cache_path, user_config_path

from audigest.errors import ConfigurationError

_PROVIDERS = frozenset({"auto", "fake", "openai", "elevenlabs", "local"})
_CONFIG_KEYS = frozenset({"provider", "language", "cache_dir"})


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Resolved application configuration; it contains no credentials."""

    provider: str
    language: str | None
    cache_dir: Path
    config_file: Path


def _as_path(value: Any, *, setting: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ConfigurationError(f"{setting} must be a non-empty path")
    if "\x00" in str(value):
        raise ConfigurationError(f"{setting} contains an invalid path")
    try:
        return Path(value).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError(f"{setting} is not a valid path") from exc


def _provider(value: Any) -> str:
    if not isinstance(value, str) or value not in _PROVIDERS:
        choices = ", ".join(sorted(_PROVIDERS))
        raise ConfigurationError(f"STT provider must be one of: {choices}")
    return value


def _language(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError("language must be a string")
    cleaned = value.strip()
    if not cleaned:
        return None
    if any(character.isspace() for character in cleaned):
        raise ConfigurationError("language must not contain whitespace")
    return cleaned


def _read_config(path: Path, *, required: bool) -> dict[str, Any]:
    try:
        exists = path.exists()
    except (OSError, ValueError) as exc:
        raise ConfigurationError("Configuration file path is invalid or inaccessible") from exc
    if not exists:
        if required:
            raise ConfigurationError(f"Configuration file does not exist at {path}")
        return {}
    try:
        with path.open("rb") as config_stream:
            parsed = tomllib.load(config_stream)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Could not read configuration file at {path}") from exc

    if not isinstance(parsed, dict):
        raise ConfigurationError(f"Configuration file at {path} must contain a TOML table")
    unexpected = set(parsed) - _CONFIG_KEYS
    if unexpected:
        keys = ", ".join(sorted(unexpected))
        raise ConfigurationError(f"Unsupported configuration key(s) in {path}: {keys}")
    return parsed


def load_config(
    *,
    provider: str | None = None,
    language: str | None = None,
    cache_dir: str | Path | None = None,
    config_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Resolve settings in CLI, environment, TOML, and default order.

    The TOML file may define only ``provider``, ``language``, and ``cache_dir``.
    Credentials and unrelated environment variables are never inspected.
    """

    env = os.environ if environ is None else environ
    if config_file is not None:
        selected_config_file = _as_path(config_file, setting="config_file")
        config_required = True
    elif env.get("AUDIGEST_CONFIG_FILE"):
        selected_config_file = _as_path(env["AUDIGEST_CONFIG_FILE"], setting="AUDIGEST_CONFIG_FILE")
        config_required = True
    else:
        selected_config_file = user_config_path("audigest") / "config.toml"
        config_required = False

    file_values = _read_config(selected_config_file, required=config_required)

    selected_provider = _provider(
        provider
        if provider is not None
        else env.get("AUDIGEST_STT_PROVIDER", file_values.get("provider", "auto"))
    )
    selected_language = _language(
        language
        if language is not None
        else env.get("AUDIGEST_LANGUAGE", file_values.get("language"))
    )
    raw_cache_dir = (
        cache_dir
        if cache_dir is not None
        else env.get("AUDIGEST_CACHE_DIR", file_values.get("cache_dir"))
    )
    selected_cache_dir = (
        user_cache_path("audigest")
        if raw_cache_dir is None
        else _as_path(raw_cache_dir, setting="cache_dir")
    )

    return AppConfig(
        provider=selected_provider,
        language=selected_language,
        cache_dir=selected_cache_dir,
        config_file=selected_config_file,
    )
