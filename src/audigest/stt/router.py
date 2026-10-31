"""Explicit STT provider selection."""

from __future__ import annotations

from audigest.errors import ConfigurationError, ProviderError
from audigest.stt.base import TranscriptionProvider
from audigest.stt.fake import FakeSTTProvider

_KNOWN_PROVIDERS = frozenset({"auto", "fake", "openai", "elevenlabs", "local"})


def get_provider(name: str) -> TranscriptionProvider:
    """Return an implemented provider or fail without attempting fallback/network."""

    if name == "fake":
        return FakeSTTProvider()
    if name == "auto":
        raise ProviderError(
            "No STT provider is available for 'auto' yet. Select --provider fake to use the "
            "deterministic fake provider."
        )
    if name in _KNOWN_PROVIDERS:
        raise ProviderError(
            f"The '{name}' STT provider is not implemented in this version. Select --provider "
            "fake to use the deterministic offline provider."
        )
    raise ConfigurationError(
        "Unknown STT provider. Choose one of: auto, fake, openai, elevenlabs, local"
    )


__all__ = ["get_provider"]
