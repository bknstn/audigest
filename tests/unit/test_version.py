"""Package and CLI version reporting."""

from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from audigest import __version__
from audigest.cli import app


def test_package_version_matches_distribution_metadata() -> None:
    assert distribution_version("audigest") == __version__


def test_version_option_is_eager_and_independent_of_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(tmp_path / "missing-config.toml"))
    monkeypatch.setenv("AUDIGEST_STT_PROVIDER", "unsupported-provider")
    monkeypatch.setenv("AUDIGEST_LANGUAGE", "not a language")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("--version must not load config, resolve sources, or select providers")

    monkeypatch.setattr("audigest.config.load_config", unexpected)
    monkeypatch.setattr("audigest.service.load_config", unexpected)
    monkeypatch.setattr("audigest.processors.service.load_config", unexpected)
    monkeypatch.setattr("audigest.processors.apply_service.load_config", unexpected)
    monkeypatch.setattr("audigest.processors.english_service.load_config", unexpected)
    monkeypatch.setattr("audigest.service.LocalFileResolver.resolve", unexpected)
    monkeypatch.setattr("audigest.service.LocalTranscriptAcquirer.acquire", unexpected)
    monkeypatch.setattr("audigest.service.get_provider", unexpected)
    monkeypatch.setattr("audigest.processors.service.OpenAISummaryProvider", unexpected)
    monkeypatch.setattr("audigest.processors.apply_service.OpenAIApplicationProvider", unexpected)
    monkeypatch.setattr("audigest.processors.english_service.OpenAIEnglishProvider", unexpected)

    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"audigest {__version__}\n"
    assert result.stderr == ""
