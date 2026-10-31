"""Transcript-first local acquisition and deterministic caption parsing tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from audigest.acquisition import LocalTranscriptAcquirer
from audigest.errors import SourceResolutionError
from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.sources.local import LocalFileResolver


def _canonical_transcript() -> Transcript:
    return Transcript(
        id="a" * 64,
        source=SourceInfo(
            type="youtube",
            uri="https://youtube.example/watch?v=sample",
            title="Original episode",
            author="Synthetic author",
            duration_seconds=90.0,
        ),
        provider="fake",
        model="fake-v1",
        language="en",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        segments=[
            TranscriptSegment(text="Untimed imported text."),
            TranscriptSegment(start=4.0, end=5.5, text="Timed imported cue.", speaker="S1"),
        ],
    )


def test_text_input_preserves_authored_lines_without_timestamps(tmp_path: Path) -> None:
    text_path = tmp_path / "notes.txt"
    authored = "First authored line.\nSecond authored line.\n"
    text_path.write_text(authored, encoding="utf-8")
    resolved = LocalFileResolver().resolve(text_path)

    result = LocalTranscriptAcquirer().acquire(resolved)

    assert result is not None
    assert result.draft.provider == "text"
    assert result.draft.model is None
    assert result.draft.segments == [TranscriptSegment(text=authored)]
    assert result.draft.segments[0].start is None
    assert result.source_fingerprint != resolved.content_sha256
    assert len(result.source_fingerprint) == 64
    assert result.source_info is None


def test_empty_or_non_utf8_text_fails_as_source_error(tmp_path: Path) -> None:
    resolver = LocalFileResolver()
    acquirer = LocalTranscriptAcquirer()
    empty = tmp_path / "empty.txt"
    empty.write_text(" \n\t", encoding="utf-8")
    with pytest.raises(SourceResolutionError, match="empty"):
        acquirer.acquire(resolver.resolve(empty))

    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    with pytest.raises(SourceResolutionError, match="UTF-8"):
        acquirer.acquire(resolver.resolve(invalid))


def test_canonical_json_preserves_source_and_transcript_provenance(tmp_path: Path) -> None:
    input_path = tmp_path / "existing.json"
    original = _canonical_transcript()
    input_path.write_text(original.model_dump_json(), encoding="utf-8")
    result = LocalTranscriptAcquirer().acquire(LocalFileResolver().resolve(input_path))

    assert result is not None
    assert result.source_info == original.source
    assert result.draft.provider == original.provider
    assert result.draft.model == original.model
    assert result.draft.language == original.language
    assert result.draft.segments == original.segments
    assert result.source_fingerprint != LocalFileResolver().resolve(input_path).content_sha256


def test_invalid_canonical_json_fails_without_fallback(tmp_path: Path) -> None:
    input_path = tmp_path / "broken.json"
    input_path.write_text('{"schema_version":"1","text":"wrong"}', encoding="utf-8")

    with pytest.raises(SourceResolutionError, match="Canonical transcript JSON is invalid"):
        LocalTranscriptAcquirer().acquire(LocalFileResolver().resolve(input_path))


def test_srt_captions_are_parsed_with_timestamps(tmp_path: Path) -> None:
    input_path = tmp_path / "captions.srt"
    input_path.write_text(
        "1\n00:00:01,250 --> 00:00:02,500\nHello\nthere.\n\n"
        "2\n00:00:02,500 --> 00:00:03,750\nNext cue.\n",
        encoding="utf-8",
    )

    result = LocalTranscriptAcquirer().acquire(LocalFileResolver().resolve(input_path))

    assert result is not None
    assert result.draft.provider == "captions"
    assert result.draft.model is None
    assert result.draft.segments == [
        TranscriptSegment(start=1.25, end=2.5, text="Hello\nthere."),
        TranscriptSegment(start=2.5, end=3.75, text="Next cue."),
    ]


def test_vtt_captions_allow_short_timestamps_and_cue_settings(tmp_path: Path) -> None:
    input_path = tmp_path / "captions.vtt"
    input_path.write_text(
        "WEBVTT\n\n00:01.000 --> 00:02.250 align:start\nFirst cue.\n",
        encoding="utf-8",
    )

    result = LocalTranscriptAcquirer().acquire(LocalFileResolver().resolve(input_path))

    assert result is not None
    assert result.draft.segments[0].start == 1.0
    assert result.draft.segments[0].end == 2.25


def test_invalid_caption_candidate_fails_clearly(tmp_path: Path) -> None:
    input_path = tmp_path / "captions.srt"
    input_path.write_text("1\ninvalid timestamp\nNo cue timing.\n", encoding="utf-8")

    with pytest.raises(SourceResolutionError, match="malformed cue"):
        LocalTranscriptAcquirer().acquire(LocalFileResolver().resolve(input_path))


def test_sidecar_priority_and_effective_fingerprint(tmp_path: Path) -> None:
    media = tmp_path / "episode.mp3"
    media.write_bytes(b"synthetic media")
    canonical_path = tmp_path / "episode.transcript.json"
    canonical_path.write_text(_canonical_transcript().model_dump_json(), encoding="utf-8")
    (tmp_path / "episode.vtt").write_text(
        "WEBVTT\n\n00:01.000 --> 00:02.000\nLower priority captions.\n", encoding="utf-8"
    )
    (tmp_path / "episode.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nLower priority SRT.\n", encoding="utf-8"
    )
    (tmp_path / "episode.txt").write_text("Lower priority text", encoding="utf-8")

    resolver = LocalFileResolver()
    acquirer = LocalTranscriptAcquirer()
    resolved = resolver.resolve(media)
    first = acquirer.acquire(resolved)

    assert first is not None
    assert first.draft.provider == "fake"
    assert first.source_info is not None
    assert first.source_info == _canonical_transcript().source
    assert first.source_fingerprint != resolved.content_sha256

    canonical_path.write_text(
        _canonical_transcript().model_copy(update={"text": "different"}).model_dump_json(),
        encoding="utf-8",
    )
    # Pydantic's model_copy does not revalidate. This intentionally creates an
    # inconsistent sidecar so the acquirer must fail instead of using lower priority files.
    with pytest.raises(SourceResolutionError, match="Canonical transcript JSON is invalid"):
        acquirer.acquire(resolved)


def test_changed_sidecar_changes_effective_fingerprint(tmp_path: Path) -> None:
    media = tmp_path / "episode.wav"
    sidecar = tmp_path / "episode.txt"
    media.write_bytes(b"synthetic media")
    sidecar.write_text("First sidecar", encoding="utf-8")
    resolver = LocalFileResolver()
    acquirer = LocalTranscriptAcquirer()
    resolved = resolver.resolve(media)
    first = acquirer.acquire(resolved)
    sidecar.write_text("Changed sidecar", encoding="utf-8")
    second = acquirer.acquire(resolved)

    assert first is not None
    assert second is not None
    assert first.source_fingerprint != second.source_fingerprint


def test_changed_selected_sidecar_format_changes_effective_fingerprint(
    tmp_path: Path,
) -> None:
    media = tmp_path / "episode.wav"
    vtt = tmp_path / "episode.vtt"
    text_sidecar = tmp_path / "episode.txt"
    shared_content = "WEBVTT\n\n00:01.000 --> 00:02.000\nSame bytes.\n"
    media.write_bytes(b"synthetic media")
    vtt.write_text(shared_content, encoding="utf-8")
    text_sidecar.write_text(shared_content, encoding="utf-8")
    resolver = LocalFileResolver()
    acquirer = LocalTranscriptAcquirer()
    resolved = resolver.resolve(media)

    first = acquirer.acquire(resolved)
    vtt.unlink()
    second = acquirer.acquire(resolved)

    assert first is not None
    assert second is not None
    assert first.draft.provider == "captions"
    assert first.draft.segments[0].start == 1.0
    assert second.draft.provider == "text"
    assert second.draft.segments[0].start is None
    assert first.source_fingerprint != second.source_fingerprint


def test_non_regular_sidecar_fails_without_blocking(tmp_path: Path) -> None:
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are not available on this platform")
    media = tmp_path / "episode.wav"
    media.write_bytes(b"synthetic media")
    fifo_path = tmp_path / "episode.transcript.json"
    os.mkfifo(fifo_path)

    with pytest.raises(SourceResolutionError, match="not a regular file"):
        LocalTranscriptAcquirer().acquire(LocalFileResolver().resolve(media))


def test_no_sidecar_returns_none_for_stt_fallback(tmp_path: Path) -> None:
    media = tmp_path / "episode.wav"
    media.write_bytes(b"synthetic media")

    assert LocalTranscriptAcquirer().acquire(LocalFileResolver().resolve(media)) is None
