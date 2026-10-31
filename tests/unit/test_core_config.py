"""Configuration precedence and sanitization tests."""

from pathlib import Path

import pytest

from audigest.config import load_config
from audigest.errors import ConfigurationError


def test_config_precedence_is_cli_then_environment_then_toml_then_default(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'provider = "fake"\nlanguage = "fr"\ncache_dir = "from-file"\n', encoding="utf-8"
    )
    env = {
        "AUDIGEST_CONFIG_FILE": str(config_file),
        "AUDIGEST_STT_PROVIDER": "openai",
        "AUDIGEST_LANGUAGE": "de",
        "AUDIGEST_CACHE_DIR": str(tmp_path / "from-env"),
    }

    from_cli = load_config(
        provider="fake",
        language="es",
        cache_dir=tmp_path / "from-cli",
        environ=env,
    )
    assert from_cli.provider == "fake"
    assert from_cli.language == "es"
    assert from_cli.cache_dir == tmp_path / "from-cli"
    assert from_cli.config_file == config_file

    from_env = load_config(environ={**env, "AUDIGEST_STT_PROVIDER": "local"})
    assert from_env.provider == "local"
    assert from_env.language == "de"
    assert from_env.cache_dir == tmp_path / "from-env"

    from_file = load_config(environ={"AUDIGEST_CONFIG_FILE": str(config_file)})
    assert from_file.provider == "fake"
    assert from_file.language == "fr"
    assert from_file.cache_dir == Path("from-file")


def test_config_defaults_to_auto_with_no_language(tmp_path: Path) -> None:
    config_file = tmp_path / "empty.toml"
    config_file.write_text("", encoding="utf-8")
    config = load_config(
        config_file=config_file,
        environ={"AUDIGEST_CACHE_DIR": str(tmp_path / "cache")},
    )

    assert config.provider == "auto"
    assert config.language is None
    assert config.cache_dir == tmp_path / "cache"


def test_config_errors_are_actionable_and_do_not_echo_values(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('provider = "secret-value"\n', encoding="utf-8")

    with pytest.raises(ConfigurationError) as error:
        load_config(environ={"AUDIGEST_CONFIG_FILE": str(config_file)})
    assert "secret-value" not in str(error.value)
    assert "STT provider" in str(error.value)


def test_config_rejects_unknown_toml_keys_without_reading_them_as_secrets(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('api_key = "do-not-log-this"\n', encoding="utf-8")

    with pytest.raises(ConfigurationError) as error:
        load_config(config_file=config_file, environ={})
    assert "api_key" in str(error.value)
    assert "do-not-log-this" not in str(error.value)


def test_config_rejects_invalid_paths_and_explicit_missing_config(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="invalid path"):
        load_config(cache_dir="\x00", environ={})
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_config(config_file=tmp_path / "missing.toml", environ={})


def test_config_reports_invalid_utf8_without_echoing_file_content(tmp_path: Path) -> None:
    config_file = tmp_path / "invalid-utf8.toml"
    config_file.write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigurationError, match="Could not read configuration file"):
        load_config(config_file=config_file, environ={})
