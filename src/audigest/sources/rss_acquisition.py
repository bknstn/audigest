"""Select and acquire one explicitly supplied RSS 2.0 episode."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from audigest.acquisition import AcquisitionResult, LocalTranscriptAcquirer
from audigest.errors import SourceResolutionError
from audigest.models import SourceInfo
from audigest.sources.base import SUPPORTED_MEDIA_EXTENSIONS, ResolvedSource
from audigest.sources.podcast_transcript import (
    parse_podcast_html_transcript,
    parse_podcast_json_transcript,
)
from audigest.sources.remote import (
    MAX_REMOTE_MEDIA_BYTES,
    BoundedRemoteDocument,
    RemoteURLReference,
    fetch_bounded_remote_document,
    fetch_remote_transcript,
    is_spotify_media_url,
    remote_url_reference,
)
from audigest.sources.rss import RSSFeed, RSSItem, parse_rss_feed

_TRANSCRIPT_MIME_SUFFIX = {
    "application/json": ".json",
    "application/x-subrip": ".srt",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/vtt": ".vtt",
}
_MEDIA_MIME_SUFFIX = {
    "audio/aac": ".aac",
    "audio/aiff": ".aiff",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-aiff": ".aiff",
    "audio/x-flac": ".flac",
    "audio/x-wav": ".wav",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpeg",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
}
_SUPPORTED_TRANSCRIPT_SUFFIXES = frozenset({".json", ".md", ".srt", ".txt", ".vtt", ".html"})


@dataclass(frozen=True, slots=True)
class SelectedRSSEpisode:
    """One selected episode with safe source identity and feed metadata."""

    feed: RSSFeed
    item: RSSItem
    feed_reference: RemoteURLReference
    final_feed_url: str
    resolved_source: ResolvedSource
    episode_identity_hash: str
    metadata_hash: str
    source_identity: str


@dataclass(frozen=True, slots=True)
class RSSTranscriptSelection:
    """Selected, normalized transcript acquisition for an RSS episode."""

    resolved_source: ResolvedSource
    acquisition: AcquisitionResult


@dataclass(frozen=True, slots=True)
class RSSMediaSelection:
    """One eligible publisher-hosted RSS enclosure."""

    resolved_source: ResolvedSource
    request_url: str
    suffix: str
    locator_fingerprint: str


RSSPreparation = RSSTranscriptSelection | RSSMediaSelection


def _digest(parts: object) -> str:
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalize_item_link(final_feed_url: str, link: str | None) -> str | None:
    if link is None:
        return None
    try:
        absolute_link = urljoin(final_feed_url, link)
        normalized = remote_url_reference(absolute_link).request_url
        fragment = urlsplit(absolute_link).fragment
        return f"{normalized}#{fragment}" if fragment else normalized
    except (SourceResolutionError, ValueError):
        return None


def _select_item(feed: RSSFeed, final_feed_url: str, selector: str | None) -> RSSItem:
    if selector is None:
        if len(feed.items) != 1:
            raise SourceResolutionError(
                "RSS feed contains multiple episodes; specify --episode with an exact GUID "
                "or item link."
            )
        return feed.items[0]

    try:
        selector_reference = remote_url_reference(selector)
        fragment = urlsplit(selector).fragment
        normalized_selector = (
            f"{selector_reference.request_url}#{fragment}"
            if fragment
            else selector_reference.request_url
        )
    except (SourceResolutionError, ValueError):
        normalized_selector = None
    matches = [
        item
        for item in feed.items
        if item.guid == selector
        or (
            normalized_selector is not None
            and _normalize_item_link(final_feed_url, item.link) == normalized_selector
        )
    ]
    if not matches:
        raise SourceResolutionError(
            "No RSS episode matches --episode; provide an exact episode GUID or full item link."
        )
    if len(matches) > 1:
        raise SourceResolutionError("The RSS episode selector matches more than one item.")
    return matches[0]


def _published_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _episode_identity(item: RSSItem) -> str:
    if item.guid is not None or item.link is not None:
        basis = ["rss-episode-identity-v1", item.guid, item.link]
    else:
        basis = ["rss-single-episode-metadata-v1", item.title, item.pub_date]
    return _digest(basis)


def select_rss_episode(
    feed_reference: RemoteURLReference,
    document: BoundedRemoteDocument,
    *,
    selector: str | None,
) -> SelectedRSSEpisode:
    """Parse one feed and select an episode without fetching its declared resources."""

    feed = parse_rss_feed(document.content)
    item = _select_item(feed, document.final_url, selector)
    identity_hash = _episode_identity(item)
    metadata_hash = _digest(
        [
            "rss-episode-metadata-v1",
            identity_hash,
            feed.title,
            feed.author,
            feed.language,
            item.title,
            item.author,
            item.pub_date,
        ]
    )
    source_uri = f"{feed_reference.public_url}#episode-{identity_hash}"
    source_info = SourceInfo(
        type="rss",
        uri=source_uri,
        title=item.title or feed.title,
        author=item.author or feed.author,
        published_at=_published_at(item.pub_date),
    )
    source_identity = _digest(
        [
            "rss-selected-episode-v1",
            feed_reference.locator_fingerprint,
            identity_hash,
            metadata_hash,
        ]
    )
    return SelectedRSSEpisode(
        feed=feed,
        item=item,
        feed_reference=feed_reference,
        final_feed_url=document.final_url,
        resolved_source=ResolvedSource(
            source_info=source_info,
            media_path=Path("rss-episode"),
            content_sha256=source_identity,
        ),
        episode_identity_hash=identity_hash,
        metadata_hash=metadata_hash,
        source_identity=source_identity,
    )


def _language_rank(candidate: str | None, preferred: str | None) -> int | None:
    if candidate is None or preferred is None:
        return None
    actual = candidate.replace("_", "-").casefold()
    wanted = preferred.replace("_", "-").casefold()
    if actual == wanted:
        return 0
    if actual.split("-", maxsplit=1)[0] == wanted.split("-", maxsplit=1)[0]:
        return 1
    return None


def _language_key(
    language: str | None,
    requested_language: str | None,
    channel_language: str | None,
    stable_key: str,
) -> tuple[int, int, str, str]:
    effective = language or channel_language
    requested_rank = _language_rank(effective, requested_language)
    if requested_rank is not None:
        return 0, requested_rank, (effective or "").casefold(), stable_key
    channel_rank = _language_rank(effective, channel_language)
    if channel_rank is not None:
        return 1, channel_rank, (effective or "").casefold(), stable_key
    return 2, 0, (effective or "").casefold(), stable_key


def _clean_language(value: str | None) -> str | None:
    if value is None:
        return None
    language = value.strip()
    if not language or any(character.isspace() for character in language):
        return None
    return language


def _mime_type(value: str | None) -> str | None:
    if value is None:
        return None
    mime = value.split(";", maxsplit=1)[0].strip().casefold()
    return mime or None


def _resolve_selected_url(episode: SelectedRSSEpisode, raw_url: str | None) -> RemoteURLReference:
    if raw_url is None or not raw_url.strip():
        raise SourceResolutionError("Selected RSS transcript or enclosure has no URL")
    try:
        return remote_url_reference(urljoin(episode.final_feed_url, raw_url.strip()))
    except (SourceResolutionError, ValueError):
        raise SourceResolutionError("Selected RSS resource URL is invalid") from None


def _transcript_format(
    reference: RemoteURLReference,
    mime_type: str | None,
    *,
    relation: str | None,
) -> tuple[str, bool]:
    mime = _mime_type(mime_type)
    if mime is None:
        raise SourceResolutionError("Selected RSS transcript has no MIME type")
    mime_suffix = _TRANSCRIPT_MIME_SUFFIX.get(mime)
    if mime_suffix is None:
        raise SourceResolutionError("Selected RSS transcript format is not supported")
    suffix = reference.suffix or mime_suffix
    if suffix not in _SUPPORTED_TRANSCRIPT_SUFFIXES:
        raise SourceResolutionError("Selected RSS transcript format is not supported")
    if reference.suffix and mime_suffix != reference.suffix:
        raise SourceResolutionError("Selected RSS transcript MIME type does not match its URL")
    if relation == "captions":
        return suffix, True
    return suffix, suffix in {".srt", ".vtt"}


def _resource_fingerprint(
    episode: SelectedRSSEpisode,
    locator_fingerprint: str,
    content_sha256: str,
    *,
    requested_language: str | None,
    selected_language: str | None,
    kind: str,
) -> str:
    return _digest(
        [
            "rss-transcript-acquisition-v1",
            episode.feed_reference.locator_fingerprint,
            episode.episode_identity_hash,
            episode.metadata_hash,
            locator_fingerprint,
            content_sha256,
            requested_language,
            selected_language,
            kind,
        ]
    )


def _podcast_json_document(content: bytes) -> bool:
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (RecursionError, UnicodeDecodeError, TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and "schema_version" not in payload
        and payload.get("version") == "1.0.0"
    )


def _acquire_transcript(
    episode: SelectedRSSEpisode,
    declaration_index: int,
    *,
    requested_language: str | None,
) -> RSSTranscriptSelection:
    declaration = episode.item.transcripts[declaration_index]
    reference = _resolve_selected_url(episode, declaration.url)
    relation = (declaration.relation or "").strip().casefold()
    suffix, is_caption = _transcript_format(
        reference,
        declaration.mime_type,
        relation=relation,
    )
    selected_language = _clean_language(declaration.language) or _clean_language(
        episode.feed.language
    )
    kind = "rss-caption" if is_caption else "rss-transcript"
    origin_kind = "captions" if is_caption else "text"
    if suffix == ".html":
        document = fetch_bounded_remote_document(
            reference.request_url,
            accept="text/html, application/xhtml+xml, */*",
            limit=2 * 1024 * 1024,
            label="transcript",
        )
        draft = parse_podcast_html_transcript(
            document.content,
            language=selected_language,
            origin_uri=reference.public_url,
            origin_kind=origin_kind,
        )
        content_sha256 = hashlib.sha256(document.content).hexdigest()
        source_fingerprint = _resource_fingerprint(
            episode,
            reference.locator_fingerprint,
            content_sha256,
            requested_language=requested_language,
            selected_language=selected_language,
            kind=kind,
        )
        return RSSTranscriptSelection(
            resolved_source=episode.resolved_source,
            acquisition=AcquisitionResult(
                draft=draft,
                source_fingerprint=source_fingerprint,
                source_info=episode.resolved_source.source_info,
            ),
        )

    remote = fetch_remote_transcript(reference.request_url, suffix_hint=suffix)
    if suffix == ".json" and _podcast_json_document(remote.content):
        draft = parse_podcast_json_transcript(
            remote.content,
            language=selected_language,
            origin_uri=reference.public_url,
            origin_kind=origin_kind,
        )
        acquired_source_info = episode.resolved_source.source_info
    else:
        acquired = LocalTranscriptAcquirer().acquire_remote(remote)
        draft = acquired.draft
        if suffix == ".json":
            if acquired.source_info is None:
                raise SourceResolutionError("Canonical RSS transcript has no source metadata")
            acquired_source_info = acquired.source_info
        else:
            acquired_source_info = episode.resolved_source.source_info
        if suffix != ".json" and draft.language is None and selected_language is not None:
            draft = draft.model_copy(update={"language": selected_language})
    if is_caption and draft.provider == "captions":
        draft = draft.model_copy(update={"provider": "rss-captions"})
    source_fingerprint = _resource_fingerprint(
        episode,
        reference.locator_fingerprint,
        remote.resolved_source.content_sha256,
        requested_language=requested_language,
        selected_language=selected_language,
        kind=kind,
    )
    return RSSTranscriptSelection(
        resolved_source=episode.resolved_source,
        acquisition=AcquisitionResult(
            draft=draft,
            source_fingerprint=source_fingerprint,
            source_info=acquired_source_info,
        ),
    )


def _select_declared_transcript(
    episode: SelectedRSSEpisode,
    *,
    requested_language: str | None,
) -> RSSTranscriptSelection | None:
    declarations = episode.item.transcripts
    if not declarations:
        return None
    if len(declarations) > 16:
        raise SourceResolutionError("Selected RSS episode exceeds the transcript declaration limit")

    classified: list[tuple[tuple[int, int, str, str], int, bool]] = []
    unsupported: list[int] = []
    for index, declaration in enumerate(declarations):
        mime = _mime_type(declaration.mime_type)
        relation = (declaration.relation or "").strip().casefold()
        if not declaration.url or not declaration.url.strip() or mime is None:
            raise SourceResolutionError("Selected RSS transcript declaration is incomplete")
        if relation == "captions":
            is_caption = True
        elif mime in {"text/vtt", "application/x-subrip"}:
            is_caption = True
        elif mime in _TRANSCRIPT_MIME_SUFFIX:
            is_caption = False
        else:
            unsupported.append(index)
            continue
        try:
            reference = _resolve_selected_url(episode, declaration.url)
            suffix, is_caption = _transcript_format(reference, mime, relation=relation)
        except SourceResolutionError:
            # A declared candidate in the selected episode is not silently
            # skipped in favor of another transcript or a paid STT fallback.
            raise
        language = _clean_language(declaration.language) or _clean_language(episode.feed.language)
        classified.append(
            (
                _language_key(
                    language,
                    requested_language,
                    episode.feed.language,
                    reference.request_url,
                ),
                index,
                is_caption,
            )
        )

    text_candidates = [candidate for candidate in classified if not candidate[2]]
    caption_candidates = [candidate for candidate in classified if candidate[2]]
    if text_candidates:
        selected = min(text_candidates)
    elif unsupported:
        raise SourceResolutionError(
            "Selected RSS episode declares an unsupported transcript format"
        )
    elif caption_candidates:
        selected = min(caption_candidates)
    else:
        return None
    return _acquire_transcript(
        episode,
        selected[1],
        requested_language=requested_language,
    )


def _media_suffix(reference: RemoteURLReference, mime_type: str | None) -> str:
    mime = _mime_type(mime_type)
    if mime is None or not mime.startswith(("audio/", "video/")):
        raise SourceResolutionError("Selected RSS enclosure does not declare audio or video media")
    suffix = reference.suffix or _MEDIA_MIME_SUFFIX.get(mime, "")
    if suffix not in SUPPORTED_MEDIA_EXTENSIONS:
        raise SourceResolutionError("Selected RSS enclosure format is not supported")
    return suffix


def _select_media_enclosure(
    episode: SelectedRSSEpisode,
    *,
    requested_language: str | None,
) -> RSSMediaSelection:
    media_by_url: dict[str, tuple[RemoteURLReference, str]] = {}
    for enclosure in episode.item.enclosures:
        mime = _mime_type(enclosure.mime_type)
        raw_suffix = ""
        if enclosure.url:
            try:
                raw_suffix = urlsplit(enclosure.url).path.rsplit("/", maxsplit=1)[-1].lower()
                raw_suffix = (
                    "." + raw_suffix.rsplit(".", maxsplit=1)[-1] if "." in raw_suffix else ""
                )
            except ValueError:
                raw_suffix = ""
        looks_like_media = bool(mime and mime.startswith(("audio/", "video/"))) or (
            raw_suffix in SUPPORTED_MEDIA_EXTENSIONS
        )
        if not looks_like_media:
            continue
        if enclosure.length is None:
            raise SourceResolutionError("Selected RSS enclosure has an invalid length")
        declared_length = enclosure.length.strip()
        if not declared_length.isascii() or not declared_length.isdigit():
            raise SourceResolutionError("Selected RSS enclosure has an invalid length")
        significant_length = declared_length.lstrip("0") or "0"
        maximum_length = str(MAX_REMOTE_MEDIA_BYTES)
        if len(significant_length) > len(maximum_length) or (
            len(significant_length) == len(maximum_length) and significant_length > maximum_length
        ):
            raise SourceResolutionError("Selected RSS enclosure exceeds the 100 MiB media limit")
        reference = _resolve_selected_url(episode, enclosure.url)
        if is_spotify_media_url(reference.request_url):
            raise SourceResolutionError(
                "Spotify player and CDN media are not supported as RSS enclosures."
            )
        suffix = _media_suffix(reference, mime)
        media_by_url[reference.request_url] = (reference, suffix)

    if not media_by_url:
        raise SourceResolutionError(
            "Selected RSS episode has no supported transcript or publisher-hosted media enclosure."
        )
    if len(media_by_url) > 1:
        raise SourceResolutionError(
            "Selected RSS episode has multiple direct media enclosures; choose an episode with one."
        )
    reference, suffix = next(iter(media_by_url.values()))
    locator = _digest(
        [
            "rss-media-locator-v1",
            episode.feed_reference.locator_fingerprint,
            episode.episode_identity_hash,
            episode.metadata_hash,
            reference.locator_fingerprint,
            requested_language,
        ]
    )
    return RSSMediaSelection(
        resolved_source=episode.resolved_source,
        request_url=reference.request_url,
        suffix=suffix,
        locator_fingerprint=locator,
    )


def acquire_rss_episode(
    episode: SelectedRSSEpisode,
    *,
    requested_language: str | None,
) -> RSSPreparation:
    """Choose transcript declarations before exactly one eligible media enclosure."""

    transcript = _select_declared_transcript(
        episode,
        requested_language=requested_language,
    )
    if transcript is not None:
        return transcript
    return _select_media_enclosure(episode, requested_language=requested_language)


__all__ = [
    "RSSMediaSelection",
    "RSSPreparation",
    "RSSTranscriptSelection",
    "SelectedRSSEpisode",
    "acquire_rss_episode",
    "select_rss_episode",
]
