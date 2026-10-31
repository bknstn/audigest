"""Offline CLI tests for local Whisper fallback and cache behavior."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.stt.local import LocalWhisperTranscriptionProvider


class MockModel:
    def __init__(self, media_bytes: bytes) -> None:
        self.media_bytes = media_bytes
        self.calls: list[tuple[str, str | None]] = []

    def transcribe(self, media_path: str, **options: Any) -> Any:
        self.calls.append((media_path, options.get("language")))
        assert options["vad_filter"] is False
        assert options["word_timestamps"] is False
        assert Path(media_path).read_bytes() == self.media_bytes
        return [SimpleNamespace(start=0.0, end=1.25, text="Local synthetic transcript.")], (
            SimpleNamespace(language="pl")
        )


def prepare_environment(monkeypatch: pytest.MonkeyPatch, cache_dir: Path) -> None:
    monkeypatch.setenv("AUDIGEST_CACHE_DIR", str(cache_dir))
    config_file = cache_dir.parent / "config.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config_file))
    for variable in (
        "AUDIGEST_STT_PROVIDER",
        "AUDIGEST_LANGUAGE",
        "AUDIGEST_WHISPER_MODEL_DIR",
        "OPENAI_API_KEY",
        "ELEVENLABS_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)


def make_model_dir(root: Path) -> Path:
    model_dir = root / "prepared-model"
    model_dir.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (model_dir / name).write_text("synthetic model asset", encoding="utf-8")
    return model_dir


def test_local_fallback_persists_timing_and_reuses_without_model_or_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    model_dir = make_model_dir(tmp_path)
    monkeypatch.setenv("AUDIGEST_WHISPER_MODEL_DIR", str(model_dir))
    media = tmp_path / "source.wav"
    original = b"synthetic source media"
    media.write_bytes(original)
    model = MockModel(original)
    monkeypatch.setattr(
        "audigest.stt.router.LocalWhisperTranscriptionProvider",
        lambda: LocalWhisperTranscriptionProvider(model_factory=lambda _path: model),
    )

    runner = CliRunner()
    first = runner.invoke(
        app,
        ["transcribe", str(media), "--provider", "local", "--language", "en", "--json"],
    )
    assert first.exit_code == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["provider"] == "local"
    assert payload["language"] == "pl"
    assert payload["origin"]["kind"] == "stt"
    assert payload["segments"] == [
        {"start": 0.0, "end": 1.25, "text": "Local synthetic transcript.", "speaker": None}
    ]
    assert str(tmp_path) not in payload["model"]
    assert model.calls == [(str(media), "en")]
    assert media.read_bytes() == original

    monkeypatch.delenv("AUDIGEST_WHISPER_MODEL_DIR", raising=False)
    monkeypatch.setattr(
        LocalWhisperTranscriptionProvider,
        "transcribe",
        lambda *_args, **_kwargs: pytest.fail("cache hit must bypass local model setup"),
    )
    second = runner.invoke(
        app,
        ["transcribe", str(media), "--provider", "openai", "--language", "en", "--json"],
    )
    assert second.exit_code == 0, second.stderr
    assert json.loads(second.stdout) == payload
    assert "Using cached transcript." in second.stderr
    assert model.calls == [(str(media), "en")]


def test_acquired_text_bypasses_local_model_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    text = tmp_path / "transcript.md"
    text.write_text("Already transcribed text.\n", encoding="utf-8")
    monkeypatch.setattr(
        "audigest.stt.router.LocalWhisperTranscriptionProvider",
        lambda: pytest.fail("existing text must bypass local Whisper setup"),
    )

    result = CliRunner().invoke(
        app,
        ["ingest", str(text), "--provider", "local", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["text"] == "Already transcribed text.\n"
    assert "AUDIGEST_WHISPER_MODEL_DIR" not in result.stderr


def test_auto_never_selects_local_whisper_on_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    media = tmp_path / "source.wav"
    media.write_bytes(b"synthetic source media")
    monkeypatch.setattr(
        "audigest.stt.router.LocalWhisperTranscriptionProvider",
        lambda: pytest.fail("auto must never construct the local provider"),
    )

    result = CliRunner().invoke(
        app,
        ["transcribe", str(media), "--provider", "auto", "--json"],
    )

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "does not select an STT provider" in result.stderr
    assert not cache_dir.exists()
