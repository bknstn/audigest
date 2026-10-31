"""Read-only local media source adapter."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit

from audigest.errors import SourceResolutionError
from audigest.models import SourceInfo
from audigest.sources.base import SUPPORTED_SOURCE_EXTENSIONS, ResolvedSource
from audigest.sources.detect import detect_source


class LocalFileResolver:
    """Validate and fingerprint a local media file without copying or changing it."""

    def __init__(self, allowed_extensions: frozenset[str] = SUPPORTED_SOURCE_EXTENSIONS) -> None:
        self.allowed_extensions = frozenset(extension.lower() for extension in allowed_extensions)

    def resolve(self, source: str | Path) -> ResolvedSource:
        """Return local file metadata and SHA-256 after a streaming read."""

        source_type = detect_source(source)
        if source_type != "local":
            raise SourceResolutionError(
                f"This source is recognized as {source_type}, but only local files can be resolved "
                "in this version."
            )

        raw_source = str(source)
        try:
            if urlsplit(raw_source).scheme.lower() == "file":
                parsed = urlsplit(raw_source)
                candidate = Path(unquote(parsed.path))
            else:
                candidate = Path(source).expanduser()
            media_path = candidate.resolve(strict=True)
            file_stat = media_path.stat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise SourceResolutionError(
                "Local media file does not exist or cannot be accessed"
            ) from exc

        if not stat.S_ISREG(file_stat.st_mode):
            raise SourceResolutionError("Source must be an existing regular media file")
        if media_path.suffix.lower() not in self.allowed_extensions:
            raise SourceResolutionError(
                "Unsupported local source type. Use audio/video media, UTF-8 text/Markdown, "
                "canonical JSON, SRT, or VTT."
            )
        if not os.access(media_path, os.R_OK):
            raise SourceResolutionError("Local media file is not readable")

        digest = hashlib.sha256()
        try:
            with media_path.open("rb") as media_file:
                while block := media_file.read(1024 * 1024):
                    digest.update(block)
        except OSError as exc:
            raise SourceResolutionError("Local media file could not be read") from exc

        source_info = SourceInfo(type="local", uri=media_path.as_uri(), title=media_path.stem)
        return ResolvedSource(
            source_info=source_info,
            media_path=media_path,
            content_sha256=digest.hexdigest(),
        )


__all__ = ["LocalFileResolver", "ResolvedSource", "SUPPORTED_SOURCE_EXTENSIONS"]
