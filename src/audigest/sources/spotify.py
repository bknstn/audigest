"""Public Spotify episode URL validation and bounded oEmbed metadata."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote, urlsplit

from audigest.errors import SourceResolutionError
from audigest.models import SourceInfo
from audigest.sources.base import ResolvedSource
from audigest.sources.remote import (
    MAX_TRANSCRIPT_BYTES,
    RemoteURLReference,
    fetch_bounded_remote_document,
    is_spotify_media_url,
)
from audigest.sources.rss_acquisition import SelectedRSSEpisode, select_rss_episode

_SPOTIFY_EPISODE_ID = re.compile(r"[A-Za-z0-9]{22}\Z")
_SPOTIFY_OEMBED_ENDPOINT = "https://open.spotify.com/oembed"
MAX_SPOTIFY_TITLE_CHARS = 512


@dataclass(frozen=True, slots=True)
class SpotifyEpisodeReference:
    """Canonical public Spotify episode identity, without user URL parameters."""

    episode_id: str
    canonical_url: str
    resolved_source: ResolvedSource


@dataclass(frozen=True, slots=True)
class SpotifyEpisodeMetadata:
    """Validated oEmbed title, excluding the response HTML and image URLs."""

    title: str


def spotify_episode_reference(source: str) -> SpotifyEpisodeReference:
    """Accept one public ``open.spotify.com/episode/<base62-id>`` URL."""

    if source != source.strip():
        raise SourceResolutionError("Spotify episode URL is malformed")
    raw_url = source.strip()
    if not raw_url or any(character.isspace() or ord(character) < 32 for character in raw_url):
        raise SourceResolutionError("Spotify episode URL is malformed")
    try:
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise SourceResolutionError("Spotify episode URL is malformed") from None

    if scheme not in {"http", "https"} or hostname != "open.spotify.com":
        raise SourceResolutionError("Use a public open.spotify.com episode URL")
    if parsed.username is not None or parsed.password is not None:
        raise SourceResolutionError("Credentials in Spotify URLs are not allowed")
    if port not in {None, 80 if scheme == "http" else 443}:
        raise SourceResolutionError("Spotify episode URL must use its standard HTTP(S) port")

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) != 2 or path_parts[0] != "episode":
        raise SourceResolutionError("Spotify URL must identify one public episode")
    episode_id = path_parts[1]
    if _SPOTIFY_EPISODE_ID.fullmatch(episode_id) is None:
        raise SourceResolutionError("Spotify episode ID is invalid")

    canonical_url = f"https://open.spotify.com/episode/{episode_id}"
    source_identity = hashlib.sha256(
        json.dumps(["spotify-episode-v1", episode_id], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SpotifyEpisodeReference(
        episode_id=episode_id,
        canonical_url=canonical_url,
        resolved_source=ResolvedSource(
            source_info=SourceInfo(type="spotify", uri=canonical_url),
            media_path=Path("spotify-episode"),
            content_sha256=source_identity,
        ),
    )


def spotify_episode_metadata(
    reference: SpotifyEpisodeReference,
) -> SpotifyEpisodeMetadata:
    """Fetch and validate only the public oEmbed title for the canonical episode."""

    request_url = f"{_SPOTIFY_OEMBED_ENDPOINT}?url={quote(reference.canonical_url, safe='')}"
    document = fetch_bounded_remote_document(
        request_url,
        accept="application/json",
        limit=MAX_TRANSCRIPT_BYTES,
        label="Spotify episode metadata",
    )
    try:
        payload = json.loads(document.content.decode("utf-8"), parse_constant=_reject_constant)
    except (RecursionError, UnicodeDecodeError, TypeError, ValueError):
        raise SourceResolutionError("Spotify episode metadata is not valid JSON") from None
    if not isinstance(payload, dict):
        raise SourceResolutionError("Spotify episode metadata is invalid")
    provider_name = payload.get("provider_name")
    response_type = payload.get("type")
    title = payload.get("title")
    if (
        not isinstance(provider_name, str)
        or provider_name.strip().casefold() != "spotify"
        or not isinstance(response_type, str)
        or response_type.strip().casefold() != "rich"
        or not isinstance(title, str)
    ):
        raise SourceResolutionError("Spotify episode metadata is invalid")
    normalized_title = title.strip()
    if not normalized_title:
        raise SourceResolutionError("Spotify episode metadata has no title")
    if len(normalized_title) > MAX_SPOTIFY_TITLE_CHARS:
        raise SourceResolutionError("Spotify episode title exceeds the 512-character limit")
    try:
        normalized_title.encode("utf-8")
    except UnicodeEncodeError:
        raise SourceResolutionError("Spotify episode title contains invalid characters") from None
    if any(
        (ord(character) < 32 and character not in "\t\n\r") or ord(character) == 127
        for character in normalized_title
    ):
        raise SourceResolutionError("Spotify episode title contains invalid characters")
    return SpotifyEpisodeMetadata(title=normalized_title)


def prepare_spotify_episode(
    reference: SpotifyEpisodeReference,
    feed_reference: RemoteURLReference,
    *,
    selector: str | None,
) -> SelectedRSSEpisode:
    """Read current metadata/feed and return the title-matched RSS episode."""

    if is_spotify_media_url(feed_reference.request_url):
        raise SourceResolutionError("Spotify episodes require a publisher-hosted RSS feed")
    metadata = spotify_episode_metadata(reference)
    document = fetch_bounded_remote_document(
        feed_reference.request_url,
        accept="application/rss+xml, application/xml, text/xml, */*",
        limit=MAX_TRANSCRIPT_BYTES,
        label="RSS feed",
    )
    selected = select_rss_episode(
        feed_reference,
        document,
        selector=selector,
        expected_title=metadata.title,
    )
    selection_context = hashlib.sha256(
        json.dumps(
            [
                "spotify-rss-selection-v1",
                reference.episode_id,
                metadata.title,
                selected.source_identity,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    source_identity = hashlib.sha256(
        json.dumps(
            ["spotify-episode-cache-v1", reference.episode_id, selection_context],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    spotify_source = SourceInfo(
        type="spotify",
        uri=reference.canonical_url,
        title=metadata.title,
        author=selected.item.author or selected.feed.author,
        published_at=selected.resolved_source.source_info.published_at,
    )
    resolved = replace(
        selected.resolved_source,
        source_info=spotify_source,
        content_sha256=source_identity,
    )
    return replace(
        selected,
        resolved_source=resolved,
        source_identity=source_identity,
        selection_context=selection_context,
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


__all__ = [
    "MAX_SPOTIFY_TITLE_CHARS",
    "SpotifyEpisodeMetadata",
    "SpotifyEpisodeReference",
    "prepare_spotify_episode",
    "spotify_episode_metadata",
    "spotify_episode_reference",
]
