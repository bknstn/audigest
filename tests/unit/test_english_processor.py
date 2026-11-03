"""Offline tests for English-learning evidence, cache identity, and rendering."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from audigest.errors import ProcessorError
from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.processors.english import (
    EnglishItemDraft,
    EnglishResponse,
    canonical_english_items,
    english_content_sha256,
    english_id_for,
    english_input_json,
    render_english_markdown,
)


def make_transcript(
    *,
    title: str | None = "Synthetic English lesson",
    segments: list[TranscriptSegment] | None = None,
    transcript_id: str = "a" * 64,
    language: str | None = "en",
) -> Transcript:
    selected_segments = (
        segments
        if segments is not None
        else [
            TranscriptSegment(
                start=3.0,
                end=5.5,
                speaker="Speaker A",
                text="I put off the decision until Monday.",
            ),
            TranscriptSegment(text="This second segment is untimed."),
        ]
    )
    return Transcript(
        id=transcript_id,
        source=SourceInfo(type="local", uri="file:///private/synthetic.wav", title=title),
        provider="fake",
        model="synthetic-v1",
        language=language,
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        segments=selected_segments,
        text="\n".join(segment.text for segment in selected_segments),
    )


def make_response(
    *,
    segment_index: int = 0,
    quote: str = "I put off the decision until Monday.",
    expression: str = "put off",
) -> EnglishResponse:
    return EnglishResponse(
        overview="This transcript contains a useful phrasal verb.",
        items=[
            EnglishItemDraft(
                kind="phrasal_verb",
                expression=expression,
                segment_index=segment_index,
                quote=quote,
                meaning="To delay doing something.",
                usage_note="Often followed by an object and a time phrase.",
                examples=["We put off the meeting until Friday."],
            )
        ],
    )


def test_english_input_excludes_private_metadata_and_hash_covers_evidence_context() -> None:
    transcript = make_transcript()
    request = english_input_json(transcript, "B2")
    assert request == (
        '{"title":"Synthetic English lesson","level":"B2","segments":['
        '{"segment_index":0,"text":"I put off the decision until Monday."},'
        '{"segment_index":1,"text":"This second segment is untimed."}]}'
    )
    assert "file:///private" not in request
    assert "provider" not in request
    assert "language" not in request
    assert english_content_sha256(transcript, "B2") != english_content_sha256(transcript, "C1")
    assert english_id_for(transcript, "B2", "gpt-6-luna") != english_id_for(
        transcript, "C1", "gpt-6-luna"
    )
    assert english_id_for(transcript, "B2", "gpt-6-luna") != english_id_for(
        transcript, "B2", "other-model"
    )

    changed_timing = make_transcript(
        segments=[
            TranscriptSegment(
                start=3.25,
                end=5.5,
                speaker="Speaker A",
                text="I put off the decision until Monday.",
            ),
            TranscriptSegment(text="This second segment is untimed."),
        ]
    )
    assert english_content_sha256(transcript, "B2") != english_content_sha256(changed_timing, "B2")


def test_evidence_uses_exact_canonical_quote_and_timing_only() -> None:
    transcript = make_transcript()
    items = canonical_english_items(make_response(), transcript.segments)

    assert len(items) == 1
    assert items[0].citation.segment_index == 0
    assert items[0].citation.quote == transcript.segments[0].text
    assert items[0].citation.start == 3.0
    assert items[0].citation.end == 5.5
    assert items[0].citation.speaker == "Speaker A"

    untimed_response = EnglishResponse(
        overview="A useful expression appears in an untimed segment.",
        items=[
            EnglishItemDraft(
                kind="phrase",
                expression="second segment",
                segment_index=1,
                quote="This second segment is untimed.",
                meaning="A phrase referring to the next part.",
                usage_note="Use it to distinguish a following point.",
                examples=["The second segment contains another example."],
            )
        ],
    )
    untimed = canonical_english_items(untimed_response, transcript.segments)[0]
    assert untimed.citation.start is None
    assert untimed.citation.end is None
    assert untimed.citation.speaker is None


@pytest.mark.parametrize(
    ("segment_index", "quote", "expression", "message"),
    [
        (3, "not here", "not", "outside the transcript"),
        (0, "invented evidence", "invented", "does not match its transcript segment"),
        (0, "I put off the decision until Monday.", "puts off", "not copied from its cited quote"),
    ],
)
def test_invalid_source_evidence_is_a_processor_error(
    segment_index: int,
    quote: str,
    expression: str,
    message: str,
) -> None:
    transcript = make_transcript()

    with pytest.raises(ProcessorError, match=message) as error:
        canonical_english_items(
            make_response(segment_index=segment_index, quote=quote, expression=expression),
            transcript.segments,
        )

    assert error.value.exit_code == 6


def test_examples_are_separate_from_source_context_and_empty_results_are_valid() -> None:
    transcript = make_transcript()
    duplicate_source = EnglishResponse(
        overview="An item must have generated examples separate from its source quote.",
        items=[
            EnglishItemDraft(
                kind="phrase",
                expression="put off",
                segment_index=0,
                quote="I put off the decision until Monday.",
                meaning="To delay.",
                usage_note="Use with a task or event.",
                examples=["I put off the decision until Monday."],
            )
        ],
    )
    with pytest.raises(ProcessorError, match="separate from source context"):
        canonical_english_items(duplicate_source, transcript.segments)

    empty = EnglishResponse(overview="No useful English items were identified.", items=[])
    assert canonical_english_items(empty, transcript.segments) == []


def test_response_schema_rejects_blank_and_oversized_fields() -> None:
    with pytest.raises(ValidationError):
        EnglishResponse(overview=" \t ", items=[])
    with pytest.raises(ValidationError):
        EnglishResponse(
            overview="No items.",
            items=[
                EnglishItemDraft(
                    kind="vocabulary",
                    expression="x" * 161,
                    segment_index=0,
                    quote="x",
                    meaning="Meaning.",
                    usage_note="Usage.",
                    examples=["Example."],
                )
            ],
        )
    with pytest.raises(ValidationError):
        EnglishItemDraft(
            kind="vocabulary",
            expression="x",
            segment_index=0,
            quote="x",
            meaning="Meaning.",
            usage_note="Usage.",
            examples=["x" * 401],
        )
    assert (
        EnglishItemDraft.model_json_schema()["properties"]["examples"]["items"]["maxLength"] == 400
    )


def test_markdown_separates_generated_examples_from_cited_context() -> None:
    transcript = make_transcript()
    artifact_response = make_response()
    item = canonical_english_items(artifact_response, transcript.segments)[0]
    from audigest.processors.english import MAX_OUTPUT_TOKENS, EnglishArtifact

    artifact = EnglishArtifact(
        id="b" * 64,
        transcript_id=transcript.id,
        content_sha256="c" * 64,
        level="B2",
        processor_version="english-v1",
        prompt_version="english-prompt-v1",
        provider="openai",
        model="gpt-6-luna",
        max_output_tokens=MAX_OUTPUT_TOKENS,
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        overview=artifact_response.overview,
        items=[item],
    )
    markdown = render_english_markdown(artifact)
    assert "# English learning — B2" in markdown
    assert "**Examples:**\n- We put off the meeting until Friday." in markdown
    assert (
        "**Source context (segment 1 [00:00:03.000–00:00:05.500], speaker Speaker A):**" in markdown
    )
    assert "> I put off the decision until Monday." in markdown

    artifact.items = []
    markdown_without_items = render_english_markdown(artifact)
    assert "No high-value English items were identified." in markdown_without_items

    untimed_transcript = make_transcript(
        segments=[TranscriptSegment(text="I put off the meeting.")]
    )
    untimed_response = EnglishResponse(
        overview="An expression comes from untimed text.",
        items=[
            EnglishItemDraft(
                kind="phrasal_verb",
                expression="put off",
                segment_index=0,
                quote="I put off the meeting.",
                meaning="To delay an event.",
                usage_note="Use it for a planned event.",
                examples=["They put off the trip."],
            )
        ],
    )
    untimed_artifact = artifact.model_copy(
        update={
            "overview": untimed_response.overview,
            "items": canonical_english_items(untimed_response, untimed_transcript.segments),
        }
    )
    untimed_markdown = render_english_markdown(untimed_artifact)
    assert "Source context (segment 1):" in untimed_markdown
    assert "[00:" not in untimed_markdown


def test_multiline_source_context_prefixes_each_line_as_a_quote() -> None:
    quote = "I put off the decision until Monday.\nIt was a difficult choice."
    transcript = make_transcript(segments=[TranscriptSegment(text=quote)])
    response = EnglishResponse(
        overview="A useful phrasal verb appears in a two-line context.",
        items=[
            EnglishItemDraft(
                kind="phrasal_verb",
                expression="put off",
                segment_index=0,
                quote=quote,
                meaning="To delay doing something.",
                usage_note="It often takes an object.",
                examples=["We put off the meeting until Friday."],
            )
        ],
    )
    item = canonical_english_items(response, transcript.segments)[0]
    from audigest.processors.english import EnglishArtifact

    artifact = EnglishArtifact(
        id="d" * 64,
        transcript_id=transcript.id,
        content_sha256="e" * 64,
        level="B2",
        processor_version="english-v1",
        prompt_version="english-prompt-v1",
        provider="openai",
        model="gpt-6-luna",
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        overview=response.overview,
        items=[item],
    )

    markdown = render_english_markdown(artifact)

    assert "> I put off the decision until Monday.\n> It was a difficult choice." in markdown
