"""Offline fake-provider and routing tests."""

from pathlib import Path

import pytest

from audigest.errors import ConfigurationError, ProviderError
from audigest.stt.elevenlabs import ElevenLabsTranscriptionProvider
from audigest.stt.fake import FakeSTTProvider
from audigest.stt.local import LocalWhisperTranscriptionProvider
from audigest.stt.router import get_provider


def test_fake_provider_is_deterministic_and_uses_language_fallback(tmp_path: Path) -> None:
    provider = FakeSTTProvider()
    first = provider.transcribe(tmp_path / "one.wav", language=None)
    second = provider.transcribe(tmp_path / "another.mp3", language=None)

    assert first == second
    assert first.provider == "fake"
    assert first.model == "fake-v1"
    assert first.language == "en"
    assert first.segments[0].start == 0.0


def test_fake_provider_preserves_explicit_language(tmp_path: Path) -> None:
    draft = FakeSTTProvider().transcribe(tmp_path / "input.wav", language="pl")

    assert draft.language == "pl"


def test_router_requires_explicit_fake_provider() -> None:
    assert get_provider("fake").name == "fake"
    with pytest.raises(ProviderError, match="does not select an STT provider"):
        get_provider("auto")


def test_openai_is_available_only_when_explicitly_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")

    assert get_provider("openai").name == "openai"
    with pytest.raises(ProviderError, match="does not select an STT provider"):
        get_provider("auto")


def test_elevenlabs_is_available_only_when_explicitly_selected() -> None:
    assert isinstance(get_provider("elevenlabs"), ElevenLabsTranscriptionProvider)


def test_local_whisper_is_available_only_when_explicitly_selected() -> None:
    assert isinstance(get_provider("local"), LocalWhisperTranscriptionProvider)


def test_router_rejects_unknown_provider_names() -> None:
    with pytest.raises(ConfigurationError, match="Unknown STT provider"):
        get_provider("not-a-provider")
