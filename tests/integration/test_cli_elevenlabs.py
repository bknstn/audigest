"""Offline CLI tests for ElevenLabs STT and cache reuse."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.stt.elevenlabs import ElevenLabsTranscriptionProvider

SYNTHETIC_KEY = "synthetic-elevenlabs-cli-key-never-valid"


class MockHttpxClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class MockSpeechToText:
    def __init__(self, expected_media: bytes) -> None:
        self.expected_media = expected_media
        self.calls = 0

    def convert(self, **request: Any) -> dict[str, Any]:
        self.calls += 1
        assert request["file"].read() == self.expected_media
        assert request["model_id"] == "scribe_v2"
        return {"text": "Synthetic Scribe transcript.", "language_code": "en"}


def prepare_environment(monkeypatch: pytest.MonkeyPatch, cache_dir: Path) -> None:
    monkeypatch.setenv("AUDIGEST_CACHE_DIR", str(cache_dir))
    config_file = cache_dir.parent / "config.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config_file))
    for variable in (
        "AUDIGEST_STT_PROVIDER",
        "AUDIGEST_LANGUAGE",
        "OPENAI_API_KEY",
        "ELEVENLABS_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_elevenlabs_fallback_persists_and_replays_across_provider_without_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    media = tmp_path / "synthetic.wav"
    original = b"synthetic audio supplied to ElevenLabs"
    media.write_bytes(original)
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)
    httpx_client = MockHttpxClient()
    speech_to_text = MockSpeechToText(original)
    constructor_options: dict[str, Any] = {}

    def client_factory(**options: Any) -> Any:
        constructor_options.update(options)
        return type("MockClient", (), {"speech_to_text": speech_to_text})()

    monkeypatch.setattr(
        "audigest.stt.elevenlabs._make_httpx_client",
        lambda: httpx_client,
    )
    monkeypatch.setattr(
        "audigest.stt.router.ElevenLabsTranscriptionProvider",
        lambda: ElevenLabsTranscriptionProvider(client_factory=client_factory),
    )
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["ingest", str(media), "--provider", "elevenlabs", "--language", "en", "--json"],
    )

    assert first.exit_code == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["provider"] == "elevenlabs"
    assert payload["model"] == "scribe_v2"
    assert payload["language"] == "en"
    assert payload["text"] == "Synthetic Scribe transcript."
    assert payload["origin"]["kind"] == "stt"
    assert payload["segments"] == [
        {"start": None, "end": None, "text": "Synthetic Scribe transcript.", "speaker": None}
    ]
    assert constructor_options["base_url"] == "https://api.elevenlabs.io"
    assert constructor_options["timeout"] == 60.0
    assert constructor_options["follow_redirects"] is False
    assert constructor_options["httpx_client"] is httpx_client
    assert speech_to_text.calls == 1
    assert httpx_client.closed
    assert media.read_bytes() == original
    assert SYNTHETIC_KEY not in first.stdout + first.stderr
    assert all(
        SYNTHETIC_KEY not in path.read_text(encoding="utf-8")
        for path in cache_dir.rglob("*")
        if path.is_file()
    )

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(
        ElevenLabsTranscriptionProvider,
        "_make_client",
        lambda *_args, **_kwargs: pytest.fail("matching ElevenLabs cache must bypass SDK creation"),
    )
    cached_elevenlabs = runner.invoke(
        app,
        ["ingest", str(media), "--provider", "elevenlabs", "--language", "en", "--json"],
    )
    assert cached_elevenlabs.exit_code == 0, cached_elevenlabs.stderr
    assert json.loads(cached_elevenlabs.stdout) == payload

    monkeypatch.setattr(
        "audigest.stt.openai.OpenAITranscriptionProvider._make_client",
        lambda *_args, **_kwargs: pytest.fail("matching cache must bypass the selected provider"),
    )
    second = runner.invoke(
        app,
        ["transcribe", str(media), "--provider", "openai", "--language", "en", "--json"],
    )

    assert second.exit_code == 0, second.stderr
    assert json.loads(second.stdout) == payload
    assert "Using cached transcript." in second.stderr
    assert speech_to_text.calls == 1


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_auto_does_not_select_elevenlabs_even_when_key_is_set(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)

    def unexpected_transcription(*_: Any, **__: Any) -> Any:
        pytest.fail("auto must not select ElevenLabs, even when a key is set")

    monkeypatch.setattr(ElevenLabsTranscriptionProvider, "transcribe", unexpected_transcription)
    fixture = Path(__file__).parents[1] / "fixtures" / "sample.wav"
    result = CliRunner().invoke(
        app,
        [command, str(fixture), "--provider", "auto", "--json"],
    )

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "does not select an STT provider" in result.stderr
    assert SYNTHETIC_KEY not in result.stderr
    assert not cache_dir.exists()


def test_cli_reports_missing_elevenlabs_extra_as_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)

    def import_without_elevenlabs(name: str, package: str | None = None) -> Any:
        _ = package
        if name == "elevenlabs.client":
            raise ModuleNotFoundError("synthetic missing optional SDK")
        pytest.fail(f"Unexpected optional SDK import: {name}")

    monkeypatch.setattr(
        "audigest.stt.elevenlabs.importlib.import_module",
        import_without_elevenlabs,
    )
    fixture = Path(__file__).parents[1] / "fixtures" / "sample.wav"
    result = CliRunner().invoke(
        app,
        ["transcribe", str(fixture), "--provider", "elevenlabs", "--json"],
    )

    assert result.exit_code == 7
    assert result.stdout == ""
    assert "Install audigest" in result.stderr
    assert "elevenlabs" in result.stderr
    assert SYNTHETIC_KEY not in result.stderr
