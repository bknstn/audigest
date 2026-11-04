"""Deterministic source classification without network access."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from audigest.errors import SourceResolutionError
from audigest.models import SourceType
from audigest.sources.base import SUPPORTED_MEDIA_EXTENSIONS, SUPPORTED_TEXT_EXTENSIONS

_FEED_EXTENSIONS = frozenset({".atom", ".rss", ".xml"})


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def detect_source(source: str | Path) -> SourceType:
    """Classify local paths and recognized URL forms without making network requests.

    The selected source adapter validates and resolves remote representations.
    """

    raw_source = str(source).strip()
    if not raw_source:
        raise SourceResolutionError("Source must be a local path or a supported URL")
    try:
        parsed = urlsplit(raw_source)
    except ValueError as exc:
        raise SourceResolutionError("Invalid source URL or path") from exc
    scheme = parsed.scheme.lower()

    if scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise SourceResolutionError("Only local file:// URLs are supported")
        return "local"
    if not scheme:
        return "local"
    if scheme not in {"http", "https"}:
        raise SourceResolutionError("Unsupported URL scheme; use a local path or an HTTP(S) URL")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise SourceResolutionError("URL must include a hostname")
    if (
        _host_is(host, "youtube.com")
        or _host_is(host, "youtube-nocookie.com")
        or host == "youtu.be"
    ):
        return "youtube"
    if _host_is(host, "spotify.com"):
        return "spotify"

    path = unquote(parsed.path)
    suffix = PurePosixPath(path).suffix.lower()
    query = parsed.query.lower()
    if suffix in _FEED_EXTENSIONS or "format=rss" in query or "format=atom" in query:
        return "rss"
    if suffix in SUPPORTED_MEDIA_EXTENSIONS | SUPPORTED_TEXT_EXTENSIONS:
        return "direct"
    # Other public HTTP(S) paths may be publisher episode pages. The service
    # decides whether the bounded HTML surface has usable transcript/media data.
    return "direct"


def is_url_source(source: str | Path) -> bool:
    """Return whether a source uses an URL scheme rather than a local path."""

    try:
        scheme = urlsplit(str(source).strip()).scheme.lower()
    except ValueError:
        return False
    return scheme in {"http", "https", "file"}


__all__ = ["SourceType", "detect_source", "is_url_source"]
