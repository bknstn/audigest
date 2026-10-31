"""Tests for canonical transcript renderers."""

import json
from datetime import UTC, datetime

from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.renderers import render_json, render_markdown
from audigest.renderers.markdown import format_timestamp


def make_transcript() -> Transcript:
    return Transcript(
        id="abc123",
        source=SourceInfo(
            type="local",
            uri="/tmp/example.wav",
            title="Sample | title",
            author="Speaker A",
            duration_seconds=12.345,
        ),
        language="en",
        provider="fake",
        model="deterministic",
        created_at=datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC),
        segments=[
            TranscriptSegment(
                start=0.0,
                end=1.2345,
                text="Synthetic speech for a rendering test.",
                speaker="Speaker 1",
            ),
            TranscriptSegment(start=1.2345, end=3.5, text="A second segment."),
        ],
        text="Synthetic speech for a rendering test.\nA second segment.",
    )


def test_json_renderer_returns_canonical_transcript_object() -> None:
    transcript = make_transcript()

    rendered = render_json(transcript)

    assert json.loads(rendered) == transcript.model_dump(mode="json")
    assert "speaker" in rendered
    assert "created_at" in rendered


def test_markdown_renderer_includes_structured_metadata_timestamps_and_speakers() -> None:
    rendered = render_markdown(make_transcript())

    assert "## Metadata" in rendered
    assert "| Provider | fake |" in rendered
    assert "| Source type | local |" in rendered
    assert "| Title | Sample \\| title |" in rendered
    assert "[00:00:00.000 – 00:00:01.235] **Speaker 1:**" in rendered
    assert "[00:00:01.235 – 00:00:03.500] A second segment." in rendered
    assert "## Full text" in rendered


def test_timestamp_formatter_carries_rounding_into_hours_and_includes_milliseconds() -> None:
    assert format_timestamp(0) == "00:00:00.000"
    assert format_timestamp(59.9996) == "00:01:00.000"
    assert format_timestamp(3_600.125) == "01:00:00.125"


def test_markdown_renderer_omits_timestamps_for_untimed_segments() -> None:
    transcript = Transcript(
        id="untimed",
        source=SourceInfo(type="local", uri="file:///sample.txt"),
        provider="text",
        created_at=datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC),
        segments=[TranscriptSegment(text="Text with no timing.", speaker="Narrator")],
    )

    rendered = render_markdown(transcript)
    payload = json.loads(render_json(transcript))

    assert "- **Narrator:** Text with no timing." in rendered
    assert "[00:" not in rendered
    assert "None" not in rendered
    assert payload["segments"][0]["start"] is None
    assert payload["segments"][0]["end"] is None
