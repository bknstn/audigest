"""Explicit STT provider selection."""

from __future__ import annotations

from audigest.errors import ConfigurationError, ProviderError
from audigest.stt.base import TranscriptionProvider
from audigest.stt.elevenlabs import ElevenLabsTranscriptionProvider
from audigest.stt.fake import FakeSTTProvider
from audigest.stt.local import LocalWhisperTranscriptionProvider
from audigest.stt.openai import OpenAITranscriptionProvider

_KNOWN_PROVIDERS = frozenset({"auto", "fake", "openai", "elevenlabs", "local"})


def get_provider(name: str) -> TranscriptionProvider:
    """Return an implemented provider or fail without attempting fallback/network."""

    if name == "fake":
        return FakeSTTProvider()
    if name == "openai":
        return OpenAITranscriptionProvider()
    if name == "elevenlabs":
        return ElevenLabsTranscriptionProvider()
    if name == "local":
        return LocalWhisperTranscriptionProvider()
    if name == "auto":
        raise ProviderError(
            "Provider 'auto' reuses matching cached transcripts but does not select an STT "
            "provider. Choose --provider fake, --provider openai, --provider elevenlabs, or "
            "--provider local to run STT."
        )
    if name in _KNOWN_PROVIDERS:
        raise ProviderError(
            f"The '{name}' STT provider is not implemented in this version. Select --provider "
            "fake for deterministic offline STT, --provider openai for OpenAI STT, "
            "--provider elevenlabs for ElevenLabs STT, or --provider local for local Whisper STT."
        )
    raise ConfigurationError(
        "Unknown STT provider. Choose one of: auto, fake, openai, elevenlabs, local"
    )


__all__ = ["get_provider"]
