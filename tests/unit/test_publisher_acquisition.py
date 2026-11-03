"""Offline selection and provenance checks for publisher-page acquisition."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from audigest.errors import SourceResolutionError
from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.sources.base import ResolvedSource
from audigest.sources.publisher_acquisition import (
    PublisherTranscriptSelection,
    prepare_publisher_page,
)
from audigest.sources.remote import (
    BoundedRemoteDocument,
    RemoteTranscriptInput,
    remote_page_reference,
)

PAGE_URL = "https://publisher.example/episode"


def _remote_input(content: bytes, suffix: str, url: str) -> RemoteTranscriptInput:
    source = SourceInfo(type="direct", uri=url.split("?", maxsplit=1)[0])
    digest = hashlib.sha256(content).hexdigest()
    return RemoteTranscriptInput(
        content=content,
        suffix=suffix,
        resolved_source=ResolvedSource(
            source_info=source,
            media_path=Path(f"transcript{suffix}"),
            content_sha256=digest,
        ),
        origin_uri=source.uri,
        locator_fingerprint=hashlib.sha256(url.encode("utf-8")).hexdigest(),
    )


def test_inline_transcript_precedes_preferred_language_caption_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = BoundedRemoteDocument(
        content=(
            b'<html lang="fr"><body><div id="transcript">Inline source text.</div>'
            b'<video><track kind="captions" srclang="en" src="/captions/en.vtt">'
            b"</video></body></html>"
        ),
        final_url=PAGE_URL,
    )
    monkeypatch.setattr(
        "audigest.sources.publisher_acquisition.fetch_bounded_remote_document",
        lambda *_args, **_kwargs: document,
    )
    monkeypatch.setattr(
        "audigest.sources.publisher_acquisition.fetch_remote_transcript",
        lambda *_args, **_kwargs: pytest.fail("lower-priority caption must not be fetched"),
    )

    preparation = prepare_publisher_page(remote_page_reference(PAGE_URL), requested_language="en")

    assert isinstance(preparation, PublisherTranscriptSelection)
    assert preparation.acquisition.draft.provider == "publisher-html"
    assert preparation.acquisition.draft.segments[0].text == "Inline source text."
    assert preparation.acquisition.draft.language == "fr"


def test_canonical_json_keeps_embedded_language_none_instead_of_html_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = Transcript(
        id="a" * 64,
        source=SourceInfo(type="direct", uri="https://origin.example/source"),
        provider="imported",
        model=None,
        language=None,
        created_at=datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC),
        segments=[TranscriptSegment(text="Canonical transcript.")],
        text="Canonical transcript.",
    )
    json_bytes = json.dumps(transcript.model_dump(mode="json")).encode("utf-8")
    document = BoundedRemoteDocument(
        content=(
            b'<html lang="en"><head><link rel="transcript" href="/canonical.json"></head></html>'
        ),
        final_url=PAGE_URL,
    )
    remote = _remote_input(json_bytes, ".json", "https://publisher.example/canonical.json")
    monkeypatch.setattr(
        "audigest.sources.publisher_acquisition.fetch_bounded_remote_document",
        lambda *_args, **_kwargs: document,
    )
    requested: list[str] = []

    def fetch_selected(url: str) -> RemoteTranscriptInput:
        requested.append(url)
        return remote

    monkeypatch.setattr(
        "audigest.sources.publisher_acquisition.fetch_remote_transcript", fetch_selected
    )

    preparation = prepare_publisher_page(remote_page_reference(PAGE_URL), requested_language=None)

    assert isinstance(preparation, PublisherTranscriptSelection)
    assert requested == ["https://publisher.example/canonical.json"]
    assert preparation.acquisition.draft.language is None
    assert preparation.acquisition.draft.provider == "imported"
    assert preparation.acquisition.source_info == transcript.source


def test_unsupported_labeled_transcript_fails_without_media_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = BoundedRemoteDocument(
        content=(
            b'<html><a href="/episode-transcript.html" title="Transcript">read it</a>'
            b'<audio src="/episode.mp3"></audio></html>'
        ),
        final_url=PAGE_URL,
    )
    monkeypatch.setattr(
        "audigest.sources.publisher_acquisition.fetch_bounded_remote_document",
        lambda *_args, **_kwargs: document,
    )

    with pytest.raises(SourceResolutionError, match="unsupported format") as error:
        prepare_publisher_page(remote_page_reference(PAGE_URL), requested_language=None)

    assert error.value.exit_code == 3
