"""Deterministic, offline fake provider for architecture tests and smoke runs."""

from __future__ import annotations

from pathlib import Path

from audigest.models import TranscriptSegment
from audigest.stt.base import TranscriptDraft


class FakeSTTProvider:
    """Return a fixed authored transcript independent of file contents."""

    name = "fake"
    model = "fake-v1"

    def transcribe(self, media_path: Path, *, language: str | None) -> TranscriptDraft:
        """Create fixed segments without reading media or using the network."""

        _ = media_path
        return TranscriptDraft(
            provider=self.name,
            model=self.model,
            language=language or "en",
            segments=[
                TranscriptSegment(start=0.0, end=1.0, text="Synthetic transcript segment."),
                TranscriptSegment(
                    start=1.0,
                    end=2.5,
                    text="The fake provider is deterministic and offline.",
                ),
            ],
        )


__all__ = ["FakeSTTProvider"]
