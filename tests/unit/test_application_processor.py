"""Offline tests for application evidence, cache identity, and output rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from audigest.errors import ProcessorError
from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.processors.application import (
    ApplicationRecommendationDraft,
    ApplicationResponse,
    ProjectEvidenceDraft,
    TranscriptEvidenceDraft,
    application_id_for,
    application_input_json,
    canonical_recommendations,
    render_application_markdown,
)
from audigest.processors.project_context import read_project_context


def make_transcript(
    *,
    segments: list[TranscriptSegment] | None = None,
) -> Transcript:
    selected_segments = (
        segments
        if segments is not None
        else [
            TranscriptSegment(
                start=3.0,
                end=5.0,
                speaker="Speaker A",
                text="A synthetic event handler retries safely.",
            ),
            TranscriptSegment(text="A second untimed passage."),
        ]
    )
    return Transcript(
        id="b" * 64,
        source=SourceInfo(type="local", uri="file:///private/path/episode.txt", title="Synthetic"),
        provider="fixture",
        model="fixture-v1",
        language="en",
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        segments=selected_segments,
        text="\n".join(segment.text for segment in selected_segments),
    )


def make_project(tmp_path: Path, *, name: str = "project") -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "README.md").write_text(
        "Synthetic service retries requests with bounded delays.",
        encoding="utf-8",
    )
    return project


def recommendation(
    *,
    segment_index: int = 0,
    transcript_quote: str = "retries safely",
    relative_path: str = "README.md",
    project_quote: str = "bounded delays",
) -> ApplicationRecommendationDraft:
    return ApplicationRecommendationDraft(
        idea="Add bounded retries to the service.",
        relevance="The project already retries requests.",
        suggested_action="Document or test the retry delay limit.",
        transcript_evidence=TranscriptEvidenceDraft(
            segment_index=segment_index,
            quote=transcript_quote,
        ),
        project_evidence=ProjectEvidenceDraft(
            relative_path=relative_path,
            quote=project_quote,
        ),
    )


def test_apply_input_contains_only_title_segments_and_selected_relative_files(
    tmp_path: Path,
) -> None:
    project_path = make_project(tmp_path, name="private-project-root")
    (project_path / "unused.md").write_text("Do not include this file.", encoding="utf-8")
    transcript = make_transcript()
    context = read_project_context(project_path)

    payload = json.loads(application_input_json(transcript, context))

    assert payload == {
        "title": "Synthetic",
        "segments": [
            {"segment_index": 0, "text": "A synthetic event handler retries safely."},
            {"segment_index": 1, "text": "A second untimed passage."},
        ],
        "project_files": [
            {
                "relative_path": "README.md",
                "text": "Synthetic service retries requests with bounded delays.",
            }
        ],
    }
    serialized = application_input_json(transcript, context)
    assert str(project_path) not in serialized
    assert "unused.md" not in serialized
    assert "private/path" not in serialized


def test_canonical_recommendations_validate_both_sources_and_copy_only_transcript_timing(
    tmp_path: Path,
) -> None:
    project = read_project_context(make_project(tmp_path))
    transcript = make_transcript()
    response = ApplicationResponse(
        overview="The transcript suggests one useful reliability improvement.",
        recommendations=[recommendation()],
    )

    result = canonical_recommendations(response, transcript.segments, project.files)

    assert result[0].transcript_evidence.segment_index == 0
    assert result[0].transcript_evidence.start == 3.0
    assert result[0].transcript_evidence.end == 5.0
    assert result[0].transcript_evidence.speaker == "Speaker A"
    assert result[0].project_evidence.relative_path == "README.md"
    assert result[0].project_evidence.quote == "bounded delays"
    untimed = ApplicationResponse(
        overview="A second suggestion is supported by untimed source text.",
        recommendations=[
            recommendation(
                segment_index=1,
                transcript_quote="untimed passage",
            )
        ],
    )
    untimed_result = canonical_recommendations(untimed, transcript.segments, project.files)
    assert untimed_result[0].transcript_evidence.start is None
    assert untimed_result[0].transcript_evidence.end is None
    assert untimed_result[0].transcript_evidence.speaker is None


@pytest.mark.parametrize(
    ("draft", "message"),
    [
        (recommendation(segment_index=9), "outside the transcript"),
        (
            recommendation(transcript_quote="invented transcript quote"),
            "does not match its transcript",
        ),
        (recommendation(relative_path="not-selected.md"), "file that was not selected"),
        (
            recommendation(project_quote="invented project quote"),
            "does not match its selected project",
        ),
    ],
)
def test_unsupported_transcript_or_project_evidence_is_rejected(
    draft: ApplicationRecommendationDraft,
    message: str,
    tmp_path: Path,
) -> None:
    transcript = make_transcript()
    project = read_project_context(make_project(tmp_path))
    response = ApplicationResponse(
        overview="A synthetic overview.",
        recommendations=[draft],
    )

    with pytest.raises(ProcessorError, match=message) as error:
        canonical_recommendations(response, transcript.segments, project.files)

    assert error.value.exit_code == 6


def test_zero_recommendations_is_valid_and_artifact_ids_cover_project_identity_and_content(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "one" / "same-name"
    second_root = tmp_path / "two" / "same-name"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / "README.md").write_text("Synthetic project context.", encoding="utf-8")
    (second_root / "README.md").write_text("Synthetic project context.", encoding="utf-8")
    transcript = make_transcript()
    first = read_project_context(first_root)
    same_identity = ApplicationResponse(overview="No relevant recommendations.", recommendations=[])

    first_id = application_id_for(transcript, first, "gpt-6-luna")
    assert same_identity.recommendations == []
    assert first_id != application_id_for(
        transcript, read_project_context(second_root), "gpt-6-luna"
    )
    assert first_id != application_id_for(transcript, first, "another-model")

    (first_root / "README.md").write_text("Changed synthetic project context.", encoding="utf-8")
    assert first_id != application_id_for(
        transcript, read_project_context(first_root), "gpt-6-luna"
    )

    changed_transcript = make_transcript(
        segments=[
            TranscriptSegment(
                start=3.5,
                end=5.5,
                speaker="Speaker A",
                text="A synthetic event handler retries safely.",
            ),
            TranscriptSegment(text="A second untimed passage."),
        ]
    )
    assert first_id != application_id_for(changed_transcript, first, "gpt-6-luna")


def test_strict_schema_rejects_blank_fields_and_more_than_six_recommendations() -> None:
    with pytest.raises(ValidationError):
        ApplicationResponse(overview=" \t", recommendations=[])
    with pytest.raises(ValidationError):
        ApplicationResponse(
            overview="A valid overview.",
            recommendations=[recommendation() for _ in range(7)],
        )


def test_markdown_renderer_includes_project_and_both_evidence_sources(tmp_path: Path) -> None:
    project = read_project_context(make_project(tmp_path))
    transcript = make_transcript()
    response = ApplicationResponse(
        overview="The project has a retry flow that could use bounded delays.",
        recommendations=[recommendation()],
    )
    from audigest.processors.application import (
        APPLICATION_VERSION,
        ApplicationArtifact,
        context_metadata,
    )

    artifact = ApplicationArtifact(
        id=application_id_for(transcript, project, "gpt-6-luna"),
        transcript_id=transcript.id,
        content_sha256="a" * 64,
        project_name=project.name,
        context_files=context_metadata(project),
        processor_version=APPLICATION_VERSION,
        prompt_version="application-prompt-v1",
        provider="openai",
        model="gpt-6-luna",
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        overview=response.overview,
        recommendations=canonical_recommendations(response, transcript.segments, project.files),
    )

    rendered = render_application_markdown(artifact)

    assert rendered.startswith("# Project application\n")
    assert "Project: project" in rendered
    assert "Suggested action: Document or test the retry delay limit." in rendered
    assert (
        "Transcript evidence (segment 1 [00:00:03.000–00:00:05.000], "
        'speaker Speaker A): "retries safely"' in rendered
    )
    assert 'Project evidence (README.md): "bounded delays"' in rendered
