"""Validation tests for the canonical transcript contract."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from audigest.models import SourceInfo, Transcript, TranscriptSegment


def _transcript_data() -> dict[str, object]:
    return {
        "id": "a" * 64,
        "source": {"type": "local", "uri": "file:///tmp/sample.wav", "title": "sample"},
        "provider": "fake",
        "model": "fake-v1",
        "language": "en",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "segments": [
            {"start": 0.0, "end": 2.0, "text": "First speaker.", "speaker": "A"},
            {"start": 1.5, "end": 3.0, "text": "Second speaker.", "speaker": "B"},
        ],
    }


def test_transcript_derives_text_and_round_trips_json() -> None:
    transcript = Transcript.model_validate(_transcript_data())

    assert transcript.schema_version == "1"
    assert transcript.text == "First speaker.\nSecond speaker."
    restored = Transcript.model_validate_json(transcript.model_dump_json())
    assert restored == transcript


def test_transcript_rejects_inconsistent_text_in_json() -> None:
    data = _transcript_data()
    data["text"] = "not the segment text"

    with pytest.raises(ValidationError, match="text must match"):
        Transcript.model_validate(data)


def test_transcript_rejects_out_of_order_segments_but_allows_overlap() -> None:
    transcript = Transcript.model_validate(_transcript_data())
    assert transcript.segments[1].start < transcript.segments[0].end

    data = _transcript_data()
    data["segments"] = [
        {"start": 2.0, "end": 3.0, "text": "Later."},
        {"start": 1.0, "end": 2.5, "text": "Earlier."},
    ]
    with pytest.raises(ValidationError, match="ordered by start time"):
        Transcript.model_validate(data)


@pytest.mark.parametrize(
    ("start", "end"),
    [(-0.1, 0.0), (0.0, -0.1), (2.0, 1.0), (float("inf"), float("inf")), (float("nan"), 1.0)],
)
def test_segment_rejects_negative_non_finite_or_reversed_timestamps(
    start: float,
    end: float,
) -> None:
    with pytest.raises(ValidationError):
        TranscriptSegment(start=start, end=end, text="invalid")


def test_transcript_requires_timezone_aware_created_at() -> None:
    data = _transcript_data()
    data["created_at"] = datetime(2026, 1, 1)

    with pytest.raises(ValidationError, match="timezone-aware"):
        Transcript.model_validate(data)


def test_segments_allow_untimed_text_but_require_timestamps_in_pairs() -> None:
    untimed = TranscriptSegment(text="An authored sentence without timing.")
    assert untimed.start is None
    assert untimed.end is None

    with pytest.raises(ValidationError, match="both be present or both be absent"):
        TranscriptSegment(start=1.0, text="Only one timestamp")

    transcript = Transcript.model_validate(
        {
            **_transcript_data(),
            "segments": [
                {"text": "Plain text first"},
                {"start": 2.0, "end": 3.0, "text": "Timed cue"},
                {"text": "Plain text last"},
            ],
        }
    )
    assert [segment.start for segment in transcript.segments] == [None, 2.0, None]


def test_source_info_has_versioned_source_types_and_optional_metadata() -> None:
    source = SourceInfo(type="local", uri="file:///tmp/sample.wav")

    assert source.author is None
    assert source.published_at is None
    with pytest.raises(ValidationError):
        SourceInfo(type="unknown", uri="file:///tmp/sample.wav")  # type: ignore[arg-type]
