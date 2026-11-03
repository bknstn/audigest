"""Prepare one publisher episode page as existing transcript content or direct media."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urljoin

from audigest.acquisition import AcquisitionResult, LocalTranscriptAcquirer
from audigest.errors import SourceResolutionError
from audigest.models import TranscriptDraft, TranscriptOrigin, TranscriptSegment
from audigest.sources.base import SUPPORTED_MEDIA_EXTENSIONS, SUPPORTED_TEXT_EXTENSIONS
from audigest.sources.publisher import (
    PublisherCaptionLink,
    PublisherCaptionTrack,
    PublisherInlineTranscript,
    PublisherPage,
    PublisherTranscriptLink,
    parse_publisher_html,
)
from audigest.sources.remote import (
    MAX_TRANSCRIPT_BYTES,
    BoundedRemoteDocument,
    RemotePageReference,
    RemoteTranscriptInput,
    RemoteURLReference,
    fetch_bounded_remote_document,
    fetch_remote_transcript,
    is_spotify_media_url,
    remote_url_reference,
)


@dataclass(frozen=True, slots=True)
class PublisherTranscriptSelection:
    """Selected, already fetched publisher transcript content."""

    acquisition: AcquisitionResult


@dataclass(frozen=True, slots=True)
class PublisherMediaSelection:
    """One direct episode media URL selected from a publisher page."""

    request_url: str
    suffix: str
    locator_fingerprint: str
    page_reference: RemotePageReference


PublisherPreparation = PublisherTranscriptSelection | PublisherMediaSelection


def _language_match_rank(candidate: str | None, preferred: str | None) -> int | None:
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
    candidate_language: str | None,
    requested_language: str | None,
    page_language: str | None,
    stable_key: str,
) -> tuple[int, int, str, str]:
    effective_language = candidate_language or page_language
    requested_rank = _language_match_rank(effective_language, requested_language)
    if requested_rank is not None:
        return 0, requested_rank, (effective_language or "").casefold(), stable_key
    page_rank = _language_match_rank(effective_language, page_language)
    if page_rank is not None:
        return 1, page_rank, (effective_language or "").casefold(), stable_key
    return 2, 0, (effective_language or "").casefold(), stable_key


def _fingerprint(
    page_locator_fingerprint: str,
    selected_locator_fingerprint: str,
    content_sha256: str,
    *,
    requested_language: str | None,
    selected_language: str | None,
    selected_kind: str,
) -> str:
    encoded = json.dumps(
        [
            "publisher-page-acquisition-v1",
            page_locator_fingerprint,
            selected_locator_fingerprint,
            content_sha256,
            requested_language,
            selected_language,
            selected_kind,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_url(final_page_url: str, href: str, *, label: str) -> RemoteURLReference:
    try:
        return remote_url_reference(urljoin(final_page_url, href.strip()))
    except SourceResolutionError:
        raise SourceResolutionError(f"Publisher page contains an invalid {label} URL") from None


def _remote_acquisition(
    remote: RemoteTranscriptInput,
    page_reference: RemotePageReference,
    *,
    requested_language: str | None,
    selected_language: str | None,
    selected_kind: str,
) -> AcquisitionResult:
    acquired = LocalTranscriptAcquirer().acquire_remote(remote)
    draft = acquired.draft
    if remote.suffix in {".srt", ".vtt"} and draft.provider == "captions":
        draft = draft.model_copy(update={"provider": "publisher-captions"})
    if remote.suffix != ".json" and draft.language is None and selected_language is not None:
        draft = draft.model_copy(update={"language": selected_language})
    return AcquisitionResult(
        draft=draft,
        source_fingerprint=_fingerprint(
            page_reference.locator_fingerprint,
            remote.locator_fingerprint,
            remote.resolved_source.content_sha256,
            requested_language=requested_language,
            selected_language=selected_language,
            selected_kind=selected_kind,
        ),
        source_info=(
            acquired.source_info
            if remote.suffix == ".json"
            else page_reference.resolved_source.source_info
        ),
    )


def _linked_transcript(
    links: tuple[PublisherTranscriptLink, ...],
    page: PublisherPage,
    final_page_url: str,
    page_reference: RemotePageReference,
    requested_language: str | None,
) -> AcquisitionResult:
    selected_link = min(
        links,
        key=lambda link: _language_key(
            link.language,
            requested_language,
            page.language,
            link.href,
        ),
    )
    selected = _resolve_url(final_page_url, selected_link.href, label="transcript")
    if selected.suffix not in SUPPORTED_TEXT_EXTENSIONS:
        raise SourceResolutionError(
            "Publisher transcript link must end in .json, .txt, or .md; "
            "supply a supported transcript with --transcript."
        )
    remote = fetch_remote_transcript(selected.request_url)
    return _remote_acquisition(
        remote,
        page_reference,
        requested_language=requested_language,
        selected_language=selected_link.language or page.language,
        selected_kind="publisher-transcript-link",
    )


def _inline_transcript(
    blocks: tuple[PublisherInlineTranscript, ...],
    page: PublisherPage,
    page_reference: RemotePageReference,
    requested_language: str | None,
) -> AcquisitionResult:
    selected = min(
        blocks,
        key=lambda block: _language_key(
            block.language,
            requested_language,
            page.language,
            hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
        ),
    )
    if not selected.text.strip():
        raise SourceResolutionError("Selected publisher inline transcript is empty")
    selected_language = selected.language or page.language
    content_hash = hashlib.sha256(selected.text.encode("utf-8")).hexdigest()
    source_info = page_reference.resolved_source.source_info
    return AcquisitionResult(
        draft=TranscriptDraft(
            provider="publisher-html",
            model=None,
            language=selected_language,
            origin=TranscriptOrigin(kind="text", uri=source_info.uri, format="html"),
            segments=[TranscriptSegment(text=selected.text)],
        ),
        source_fingerprint=_fingerprint(
            page_reference.locator_fingerprint,
            page_reference.locator_fingerprint,
            content_hash,
            requested_language=requested_language,
            selected_language=selected_language,
            selected_kind="inline-transcript",
        ),
        source_info=source_info,
    )


def _caption_selection(
    links: tuple[PublisherCaptionLink, ...],
    tracks: tuple[PublisherCaptionTrack, ...],
    page: PublisherPage,
    final_page_url: str,
    page_reference: RemotePageReference,
    requested_language: str | None,
) -> AcquisitionResult:
    candidates = [(link.href, link.language, "publisher-caption-link") for link in links] + [
        (track.href, track.language, f"publisher-caption-track-{track.kind}") for track in tracks
    ]
    href, language, selected_kind = min(
        candidates,
        key=lambda item: _language_key(item[1], requested_language, page.language, item[0]),
    )
    selected = _resolve_url(final_page_url, href, label="caption")
    if selected.suffix not in {".srt", ".vtt"}:
        raise SourceResolutionError("Publisher captions must use SRT or VTT")
    remote = fetch_remote_transcript(selected.request_url)
    return _remote_acquisition(
        remote,
        page_reference,
        requested_language=requested_language,
        selected_language=language or page.language,
        selected_kind=selected_kind,
    )


def _media_selection(
    page: PublisherPage,
    final_page_url: str,
    page_reference: RemotePageReference,
    requested_language: str | None,
) -> PublisherMediaSelection:
    media_by_url: dict[str, RemoteURLReference] = {}
    for media_source in page.media_sources:
        candidate_url = urljoin(final_page_url, media_source.href.strip())
        if is_spotify_media_url(candidate_url):
            raise SourceResolutionError(
                "Spotify player and CDN media are not supported; use a publisher-hosted "
                "episode URL."
            )
        try:
            media_reference = remote_url_reference(candidate_url)
        except SourceResolutionError:
            continue
        if media_reference.suffix in SUPPORTED_MEDIA_EXTENSIONS:
            media_by_url[media_reference.request_url] = media_reference

    if not media_by_url:
        raise SourceResolutionError(
            "Publisher page has no supported transcript or direct audio/video source. "
            "Use a supported transcript with --transcript or a direct media URL."
        )
    if len(media_by_url) > 1:
        raise SourceResolutionError(
            "Publisher page lists multiple direct media sources; provide one media URL explicitly."
        )
    selected = next(iter(media_by_url.values()))
    identity = hashlib.sha256(
        json.dumps(
            [
                "publisher-page-media-v1",
                page_reference.locator_fingerprint,
                selected.locator_fingerprint,
                requested_language,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return PublisherMediaSelection(
        request_url=selected.request_url,
        suffix=selected.suffix,
        locator_fingerprint=identity,
        page_reference=page_reference,
    )


def prepare_publisher_page(
    page_reference: RemotePageReference,
    *,
    requested_language: str | None,
    document: BoundedRemoteDocument | None = None,
) -> PublisherPreparation:
    """Fetch one bounded page and select its highest-priority declared source."""

    if document is None:
        document = fetch_bounded_remote_document(
            page_reference.request_url,
            accept="text/html, application/xhtml+xml, */*",
            limit=MAX_TRANSCRIPT_BYTES,
            label="publisher page",
        )
    page = parse_publisher_html(document.content)

    if page.transcript_links:
        return PublisherTranscriptSelection(
            acquisition=_linked_transcript(
                page.transcript_links,
                page,
                document.final_url,
                page_reference,
                requested_language,
            )
        )
    if page.inline_transcripts:
        return PublisherTranscriptSelection(
            acquisition=_inline_transcript(
                page.inline_transcripts,
                page,
                page_reference,
                requested_language,
            )
        )
    if page.unsupported_transcript_links:
        raise SourceResolutionError(
            "Publisher page declares a transcript in an unsupported format; supply a supported "
            "transcript with --transcript."
        )
    if page.caption_links or page.caption_tracks:
        acquisition = _caption_selection(
            page.caption_links,
            page.caption_tracks,
            page,
            document.final_url,
            page_reference,
            requested_language,
        )
        return PublisherTranscriptSelection(acquisition=acquisition)
    if page.unsupported_caption_tracks:
        raise SourceResolutionError(
            "Publisher page declares captions in an unsupported format; use a VTT or SRT track."
        )
    return _media_selection(
        page,
        document.final_url,
        page_reference,
        requested_language,
    )


__all__ = [
    "PublisherMediaSelection",
    "PublisherPreparation",
    "PublisherTranscriptSelection",
    "prepare_publisher_page",
]
