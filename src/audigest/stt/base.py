"""Provider-independent transcription result and provider protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from audigest.models import TranscriptDraft


class TranscriptionProvider(Protocol):
    """Synchronous adapter contract for providers that consume local media."""

    name: str
    model: str

    def transcribe(self, media_path: Path, *, language: str | None) -> TranscriptDraft:
        """Return a provider-neutral draft for the supplied media file."""


__all__ = ["TranscriptionProvider", "TranscriptDraft"]
