"""Provider-neutral summary content, identity, citation validation, and rendering."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from audigest.errors import ProcessorError
from audigest.models import Transcript, TranscriptSegment

PROCESSOR_VERSION = "summary-v1"
PROMPT_VERSION = "summary-prompt-v1"
PROVIDER: Literal["openai"] = "openai"
REASONING_EFFORT: Literal["low"] = "low"
MAX_OUTPUT_TOKENS = 6000
MAX_INPUT_BYTES = 256 * 1024
MAX_KEY_POINTS = 6


class SummaryPointDraft(BaseModel):
    """One model-authored key point and its exact citation to source text."""

    model_config = ConfigDict(extra="forbid", strict=True)

    point: str = Field(min_length=1, max_length=500)
    segment_index: StrictInt = Field(ge=0)
    quote: str = Field(min_length=1, max_length=240)


class SummaryResponse(BaseModel):
    """Constrained model response before transcript timing is attached."""

    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=4000)
    key_points: list[SummaryPointDraft] = Field(min_length=1, max_length=MAX_KEY_POINTS)

    @field_validator("summary")
    @classmethod
    def nonblank_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be blank")
        return value


class SummaryCitation(BaseModel):
    """Validated source quote enriched only from the cited canonical segment."""

    model_config = ConfigDict(extra="forbid")

    segment_index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=240)
    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    speaker: str | None = None


class SummaryKeyPoint(BaseModel):
    """Canonical summary point with an attached source citation."""

    model_config = ConfigDict(extra="forbid")

    point: str = Field(min_length=1, max_length=500)
    citation: SummaryCitation


class SummaryArtifact(BaseModel):
    """A cached processor result separate from the source transcript artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    processor_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    provider: Literal["openai"] = "openai"
    model: str = Field(min_length=1)
    reasoning_effort: Literal["low"] = REASONING_EFFORT
    max_output_tokens: int = Field(default=MAX_OUTPUT_TOKENS, gt=0)
    created_at: datetime
    summary: str = Field(min_length=1, max_length=4000)
    key_points: list[SummaryKeyPoint] = Field(min_length=1, max_length=MAX_KEY_POINTS)

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


def summary_input_json(transcript: Transcript) -> str:
    """Serialize only the optional title and indexed segment text sent to the model."""

    payload = {
        "title": transcript.source.title,
        "segments": [
            {"segment_index": index, "text": segment.text}
            for index, segment in enumerate(transcript.segments)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def summary_content_sha256(transcript: Transcript) -> str:
    """Hash model input and canonical citation context that is copied to the output."""

    content = {
        "input": json.loads(summary_input_json(transcript)),
        "citation_context": [
            {"start": segment.start, "end": segment.end, "speaker": segment.speaker}
            for segment in transcript.segments
        ],
    }
    encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def summary_id_for(transcript: Transcript, model: str) -> str:
    """Return a deterministic identity covering input, prompt, provider, model, and options."""

    identity = {
        "processor_version": PROCESSOR_VERSION,
        "prompt_version": PROMPT_VERSION,
        "transcript_id": transcript.id,
        "content_sha256": summary_content_sha256(transcript),
        "provider": PROVIDER,
        "model": model,
        "reasoning_effort": REASONING_EFFORT,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_key_points(
    response: SummaryResponse,
    segments: list[TranscriptSegment],
) -> list[SummaryKeyPoint]:
    """Validate evidence references and copy timing/speaker from canonical segments."""

    key_points: list[SummaryKeyPoint] = []
    for draft in response.key_points:
        if not draft.point.strip() or not draft.quote.strip():
            raise ProcessorError("OpenAI returned a summary point with empty text or evidence.")
        if draft.segment_index >= len(segments):
            raise ProcessorError("OpenAI returned a citation outside the transcript segment range.")
        segment = segments[draft.segment_index]
        if draft.quote not in segment.text:
            raise ProcessorError("OpenAI returned a quote that does not match its cited segment.")
        key_points.append(
            SummaryKeyPoint(
                point=draft.point,
                citation=SummaryCitation(
                    segment_index=draft.segment_index,
                    quote=draft.quote,
                    start=segment.start,
                    end=segment.end,
                    speaker=segment.speaker,
                ),
            )
        )
    return key_points


def render_summary_markdown(summary: SummaryArtifact) -> str:
    """Render concise summary prose and evidence references, omitting absent timestamps."""

    lines = ["# Summary", "", summary.summary, "", "## Key points", ""]
    for point in summary.key_points:
        citation = point.citation
        source_note = f"segment {citation.segment_index + 1}"
        if citation.start is not None and citation.end is not None:
            source_note += (
                f" [{_format_timestamp(citation.start)}–{_format_timestamp(citation.end)}]"
            )
        if citation.speaker is not None:
            source_note += f", speaker {citation.speaker}"
        quote = json.dumps(citation.quote, ensure_ascii=False)
        lines.extend((f"- {point.point}", f"  - Evidence ({source_note}): {quote}"))
    lines.append("")
    return "\n".join(lines)


def render_summary_json(summary: SummaryArtifact) -> str:
    """Return a machine-readable summary artifact as one JSON object."""

    return json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _format_timestamp(seconds: float) -> str:
    milliseconds = int(seconds * 1000 + 0.5)
    minutes, remainder = divmod(milliseconds, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{millis:03}"


__all__ = [
    "MAX_INPUT_BYTES",
    "MAX_KEY_POINTS",
    "MAX_OUTPUT_TOKENS",
    "PROCESSOR_VERSION",
    "PROMPT_VERSION",
    "PROVIDER",
    "REASONING_EFFORT",
    "SummaryArtifact",
    "SummaryCitation",
    "SummaryKeyPoint",
    "SummaryResponse",
    "canonical_key_points",
    "render_summary_json",
    "render_summary_markdown",
    "summary_id_for",
    "summary_input_json",
    "summary_content_sha256",
]
