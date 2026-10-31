"""Offline fake-provider and routing tests."""

from pathlib import Path

import pytest

from audigest.errors import ConfigurationError, ProviderError
from audigest.stt.fake import FakeSTTProvider
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
    with pytest.raises(ProviderError, match="Select --provider fake"):
        get_provider("auto")


@pytest.mark.parametrize("name", ["openai", "elevenlabs", "local"])
def test_router_explains_unimplemented_providers(name: str) -> None:
    with pytest.raises(ProviderError, match="not implemented"):
        get_provider(name)


def test_router_rejects_unknown_provider_names() -> None:
    with pytest.raises(ConfigurationError, match="Unknown STT provider"):
        get_provider("not-a-provider")
