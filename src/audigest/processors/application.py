"""Provider-neutral application recommendations, evidence validation, and rendering."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from audigest.errors import ProcessorError
from audigest.models import Transcript, TranscriptSegment
from audigest.processors.openai import DEFAULT_MODEL
from audigest.processors.project_context import ProjectContext, ProjectContextFile
from audigest.processors.summary import MAX_OUTPUT_TOKENS, REASONING_EFFORT

APPLICATION_VERSION = "application-v1"
PROMPT_VERSION = "application-prompt-v1"
MAX_INPUT_BYTES = 256 * 1024
MAX_RECOMMENDATIONS = 6
PROVIDER: Literal["openai"] = "openai"


class TranscriptEvidenceDraft(BaseModel):
    """Model output pointing at one exact quote in a canonical segment."""

    model_config = ConfigDict(extra="forbid", strict=True)

    segment_index: StrictInt = Field(ge=0)
    quote: str = Field(min_length=1, max_length=240)

    @field_validator("quote")
    @classmethod
    def nonblank_quote(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("quote must not be blank")
        return value


class ProjectEvidenceDraft(BaseModel):
    """Model output pointing at one exact quote in a selected project file."""

    model_config = ConfigDict(extra="forbid", strict=True)

    relative_path: str = Field(min_length=1, max_length=1024)
    quote: str = Field(min_length=1, max_length=240)

    @field_validator("relative_path", "quote")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project evidence fields must not be blank")
        return value


class ApplicationRecommendationDraft(BaseModel):
    """One recommendation before canonical evidence metadata is attached."""

    model_config = ConfigDict(extra="forbid", strict=True)

    idea: str = Field(min_length=1, max_length=500)
    relevance: str = Field(min_length=1, max_length=800)
    suggested_action: str = Field(min_length=1, max_length=800)
    transcript_evidence: TranscriptEvidenceDraft
    project_evidence: ProjectEvidenceDraft

    @field_validator("idea", "relevance", "suggested_action")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("recommendation fields must not be blank")
        return value


class ApplicationResponse(BaseModel):
    """Structured response from an application provider."""

    model_config = ConfigDict(extra="forbid", strict=True)

    overview: str = Field(min_length=1, max_length=2000)
    recommendations: list[ApplicationRecommendationDraft] = Field(max_length=MAX_RECOMMENDATIONS)

    @field_validator("overview")
    @classmethod
    def nonblank_overview(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("overview must not be blank")
        return value


class ApplicationContextFile(BaseModel):
    """Non-absolute provenance for a selected project file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=1024)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TranscriptEvidence(BaseModel):
    """Validated transcript quote enriched only from its canonical segment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=240)
    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    speaker: str | None = None


class ProjectEvidence(BaseModel):
    """Validated project quote with its selected relative filename."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=1024)
    quote: str = Field(min_length=1, max_length=240)


class ApplicationRecommendation(BaseModel):
    """A project recommendation backed by both transcript and project quotes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idea: str = Field(min_length=1, max_length=500)
    relevance: str = Field(min_length=1, max_length=800)
    suggested_action: str = Field(min_length=1, max_length=800)
    transcript_evidence: TranscriptEvidence
    project_evidence: ProjectEvidence

    @field_validator("idea", "relevance", "suggested_action")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("recommendation fields must not be blank")
        return value


class ApplicationArtifact(BaseModel):
    """Separate, cacheable result tied to one transcript and selected project files."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_name: str = Field(min_length=1, max_length=255)
    context_files: list[ApplicationContextFile] = Field(min_length=1, max_length=16)
    processor_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    provider: Literal["openai"] = PROVIDER
    model: str = Field(min_length=1)
    reasoning_effort: Literal["low"] = REASONING_EFFORT
    max_output_tokens: int = Field(default=MAX_OUTPUT_TOKENS, gt=0)
    created_at: datetime
    overview: str = Field(min_length=1, max_length=2000)
    recommendations: list[ApplicationRecommendation] = Field(max_length=MAX_RECOMMENDATIONS)

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


def application_input_json(transcript: Transcript, project: ProjectContext) -> str:
    """Serialize only transcript title/segments and the selected relative files."""

    payload = {
        "title": transcript.source.title,
        "segments": [
            {"segment_index": index, "text": segment.text}
            for index, segment in enumerate(transcript.segments)
        ],
        "project_files": [
            {"relative_path": context_file.relative_path, "text": context_file.text}
            for context_file in project.files
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def application_content_sha256(transcript: Transcript, project: ProjectContext) -> str:
    """Hash canonical evidence context, project identity, names, and exact file bytes."""

    content = {
        "input": json.loads(application_input_json(transcript, project)),
        "transcript_evidence_context": [
            {"start": segment.start, "end": segment.end, "speaker": segment.speaker}
            for segment in transcript.segments
        ],
        "project_identity_sha256": project.identity_sha256,
        "project_files": [
            {
                "relative_path": context_file.relative_path,
                "content_sha256": context_file.content_sha256,
            }
            for context_file in project.files
        ],
    }
    encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def application_id_for(
    transcript: Transcript,
    project: ProjectContext,
    model: str,
) -> str:
    """Return a cache identity covering transcript, project, provider, and options."""

    identity = {
        "processor_version": APPLICATION_VERSION,
        "prompt_version": PROMPT_VERSION,
        "transcript_id": transcript.id,
        "content_sha256": application_content_sha256(transcript, project),
        "project_identity_sha256": project.identity_sha256,
        "context_files": [
            {
                "relative_path": context_file.relative_path,
                "content_sha256": context_file.content_sha256,
            }
            for context_file in project.files
        ],
        "provider": PROVIDER,
        "model": model,
        "reasoning_effort": REASONING_EFFORT,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def context_metadata(project: ProjectContext) -> list[ApplicationContextFile]:
    """Build safe, non-absolute metadata for the selected files."""

    return [
        ApplicationContextFile(
            relative_path=context_file.relative_path,
            content_sha256=context_file.content_sha256,
        )
        for context_file in project.files
    ]


def canonical_recommendations(
    response: ApplicationResponse,
    transcript_segments: list[TranscriptSegment],
    project_files: tuple[ProjectContextFile, ...],
) -> list[ApplicationRecommendation]:
    """Check both evidence sources and copy transcript timing from canonical data."""

    project_by_path = {
        context_file.relative_path: context_file.text for context_file in project_files
    }
    recommendations: list[ApplicationRecommendation] = []
    for draft in response.recommendations:
        if (
            not draft.idea.strip()
            or not draft.relevance.strip()
            or not draft.suggested_action.strip()
        ):
            raise ProcessorError("The application provider returned blank recommendation text.")
        transcript_evidence = draft.transcript_evidence
        if transcript_evidence.segment_index >= len(transcript_segments):
            raise ProcessorError("A recommendation cites a segment outside the transcript.")
        segment = transcript_segments[transcript_evidence.segment_index]
        if transcript_evidence.quote not in segment.text:
            raise ProcessorError("A recommendation quote does not match its transcript segment.")

        project_evidence = draft.project_evidence
        project_text = project_by_path.get(project_evidence.relative_path)
        if project_text is None:
            raise ProcessorError("A recommendation cites a project file that was not selected.")
        if project_evidence.quote not in project_text:
            raise ProcessorError("A recommendation quote does not match its selected project file.")

        recommendations.append(
            ApplicationRecommendation(
                idea=draft.idea,
                relevance=draft.relevance,
                suggested_action=draft.suggested_action,
                transcript_evidence=TranscriptEvidence(
                    segment_index=transcript_evidence.segment_index,
                    quote=transcript_evidence.quote,
                    start=segment.start,
                    end=segment.end,
                    speaker=segment.speaker,
                ),
                project_evidence=ProjectEvidence(
                    relative_path=project_evidence.relative_path,
                    quote=project_evidence.quote,
                ),
            )
        )
    return recommendations


def render_application_markdown(application: ApplicationArtifact) -> str:
    """Render reviewable suggestions and their transcript/project evidence."""

    lines = [
        "# Project application",
        "",
        f"Project: {application.project_name}",
        "",
        "## Overview",
        "",
        application.overview,
        "",
        "## Recommendations",
        "",
    ]
    if not application.recommendations:
        lines.extend(("No project-specific recommendations were identified.", ""))
    for number, recommendation in enumerate(application.recommendations, start=1):
        transcript = recommendation.transcript_evidence
        transcript_source = f"segment {transcript.segment_index + 1}"
        if transcript.start is not None and transcript.end is not None:
            transcript_source += (
                f" [{_format_timestamp(transcript.start)}–{_format_timestamp(transcript.end)}]"
            )
        if transcript.speaker is not None:
            transcript_source += f", speaker {transcript.speaker}"
        lines.extend(
            (
                f"### {number}. {recommendation.idea}",
                "",
                f"- Relevance: {recommendation.relevance}",
                f"- Suggested action: {recommendation.suggested_action}",
                f"- Transcript evidence ({transcript_source}): "
                f"{json.dumps(transcript.quote, ensure_ascii=False)}",
                f"- Project evidence ({recommendation.project_evidence.relative_path}): "
                f"{json.dumps(recommendation.project_evidence.quote, ensure_ascii=False)}",
                "",
            )
        )
    return "\n".join(lines)


def render_application_json(application: ApplicationArtifact) -> str:
    """Return the application artifact as formatted JSON."""

    return json.dumps(application.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _format_timestamp(seconds: float) -> str:
    milliseconds = int(seconds * 1000 + 0.5)
    minutes, remainder = divmod(milliseconds, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{millis:03}"


__all__ = [
    "APPLICATION_VERSION",
    "DEFAULT_MODEL",
    "MAX_INPUT_BYTES",
    "MAX_RECOMMENDATIONS",
    "PROMPT_VERSION",
    "PROVIDER",
    "ApplicationArtifact",
    "ApplicationContextFile",
    "ApplicationRecommendation",
    "ApplicationRecommendationDraft",
    "ApplicationResponse",
    "ProjectEvidence",
    "ProjectEvidenceDraft",
    "TranscriptEvidence",
    "TranscriptEvidenceDraft",
    "application_content_sha256",
    "application_id_for",
    "application_input_json",
    "canonical_recommendations",
    "context_metadata",
    "render_application_json",
    "render_application_markdown",
]
