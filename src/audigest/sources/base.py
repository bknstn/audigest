"""Source resolver interfaces shared by source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from audigest.models import SourceInfo

SUPPORTED_MEDIA_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".aac",
        ".aif",
        ".aiff",
        ".avi",
        ".caf",
        ".flac",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
        ".wma",
    }
)
SUPPORTED_TEXT_EXTENSIONS = frozenset({".json", ".srt", ".txt", ".vtt"})
SUPPORTED_SOURCE_EXTENSIONS = SUPPORTED_MEDIA_EXTENSIONS | SUPPORTED_TEXT_EXTENSIONS


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """Resolved source identity plus the original media path and fingerprint."""

    source_info: SourceInfo
    media_path: Path
    content_sha256: str


class SourceResolver(Protocol):
    """Resolve a user-supplied source into canonical metadata and a media path."""

    def resolve(self, source: str | Path) -> ResolvedSource:
        """Return resolved metadata and a path to the original or temporary media."""


__all__ = [
    "ResolvedSource",
    "SourceInfo",
    "SourceResolver",
    "SUPPORTED_MEDIA_EXTENSIONS",
    "SUPPORTED_SOURCE_EXTENSIONS",
    "SUPPORTED_TEXT_EXTENSIONS",
]
