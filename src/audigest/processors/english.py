"""Provider-neutral English-learning content, evidence, identity, and rendering."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from audigest.errors import ProcessorError
from audigest.models import Transcript, TranscriptSegment

CEFRLevel = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
EnglishItemKind = Literal[
    "vocabulary",
    "phrase",
    "collocation",
    "phrasal_verb",
    "idiom",
    "construction",
    "grammar",
]
ExampleSentence = Annotated[str, Field(min_length=1, max_length=400)]

PROCESSOR_VERSION = "english-v1"
PROMPT_VERSION = "english-prompt-v1"
PROVIDER: Literal["openai"] = "openai"
REASONING_EFFORT: Literal["low"] = "low"
MAX_OUTPUT_TOKENS = 8000
MAX_INPUT_BYTES = 256 * 1024
MAX_ITEMS = 12


class EnglishItemDraft(BaseModel):
    """Model-authored English item with an exact canonical transcript citation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: EnglishItemKind
    expression: str = Field(min_length=1, max_length=160)
    segment_index: StrictInt = Field(ge=0)
    quote: str = Field(min_length=1, max_length=500)
    meaning: str = Field(min_length=1, max_length=600)
    usage_note: str = Field(min_length=1, max_length=600)
    examples: list[ExampleSentence] = Field(min_length=1, max_length=2)

    @field_validator("expression", "quote", "meaning", "usage_note")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("English item text must not be blank")
        return value

    @field_validator("examples")
    @classmethod
    def validate_examples(cls, value: list[str]) -> list[str]:
        if any(not example.strip() for example in value):
            raise ValueError("examples must not be blank")
        return value


class EnglishResponse(BaseModel):
    """Structured provider output before canonical citation data is attached."""

    model_config = ConfigDict(extra="forbid", strict=True)

    overview: str = Field(min_length=1, max_length=2000)
    items: list[EnglishItemDraft] = Field(max_length=MAX_ITEMS)

    @field_validator("overview")
    @classmethod
    def require_nonblank_overview(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("overview must not be blank")
        return value


class EnglishCitation(BaseModel):
    """Validated source quote enriched only from its canonical segment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=500)
    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    speaker: str | None = None


class EnglishItem(BaseModel):
    """One useful English expression and its separately generated examples."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EnglishItemKind
    expression: str = Field(min_length=1, max_length=160)
    meaning: str = Field(min_length=1, max_length=600)
    usage_note: str = Field(min_length=1, max_length=600)
    examples: list[ExampleSentence] = Field(min_length=1, max_length=2)
    citation: EnglishCitation

    @field_validator("expression", "meaning", "usage_note")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("English item text must not be blank")
        return value

    @field_validator("examples")
    @classmethod
    def validate_examples(cls, value: list[str]) -> list[str]:
        if any(not example.strip() for example in value):
            raise ValueError("examples must not be blank")
        return value


class EnglishArtifact(BaseModel):
    """Separate, cacheable English-learning output tied to one transcript and level."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    level: CEFRLevel
    processor_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    provider: Literal["openai"] = PROVIDER
    model: str = Field(min_length=1)
    reasoning_effort: Literal["low"] = REASONING_EFFORT
    max_output_tokens: int = Field(default=MAX_OUTPUT_TOKENS, gt=0)
    created_at: datetime
    overview: str = Field(min_length=1, max_length=2000)
    items: list[EnglishItem] = Field(max_length=MAX_ITEMS)

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @field_validator("overview")
    @classmethod
    def require_nonblank_overview(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("overview must not be blank")
        return value


def english_input_json(transcript: Transcript, level: CEFRLevel) -> str:
    """Serialize only transcript title, requested level, and indexed segment text."""

    payload = {
        "title": transcript.source.title,
        "level": level,
        "segments": [
            {"segment_index": index, "text": segment.text}
            for index, segment in enumerate(transcript.segments)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def english_content_sha256(transcript: Transcript, level: CEFRLevel) -> str:
    """Hash provider input and the canonical timing/speaker context used in output."""

    content = {
        "input": json.loads(english_input_json(transcript, level)),
        "citation_context": [
            {"start": segment.start, "end": segment.end, "speaker": segment.speaker}
            for segment in transcript.segments
        ],
    }
    encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def english_id_for(transcript: Transcript, level: CEFRLevel, model: str) -> str:
    """Return deterministic identity for all consumed content and generation options."""

    identity = {
        "processor_version": PROCESSOR_VERSION,
        "prompt_version": PROMPT_VERSION,
        "transcript_id": transcript.id,
        "content_sha256": english_content_sha256(transcript, level),
        "level": level,
        "provider": PROVIDER,
        "model": model,
        "reasoning_effort": REASONING_EFFORT,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_english_items(
    response: EnglishResponse,
    segments: list[TranscriptSegment],
) -> list[EnglishItem]:
    """Validate exact evidence and copy optional timing and speaker from canonical segments."""

    items: list[EnglishItem] = []
    for draft in response.items:
        if not all(
            value.strip()
            for value in (
                draft.expression,
                draft.quote,
                draft.meaning,
                draft.usage_note,
                *draft.examples,
            )
        ):
            raise ProcessorError("The English processor returned blank item content.")
        if draft.segment_index >= len(segments):
            raise ProcessorError("An English item cites a segment outside the transcript.")
        segment = segments[draft.segment_index]
        if draft.quote not in segment.text:
            raise ProcessorError("An English item quote does not match its transcript segment.")
        if draft.expression not in draft.quote:
            raise ProcessorError("An English expression is not copied from its cited quote.")
        if any(example == draft.quote or example == segment.text for example in draft.examples):
            raise ProcessorError("Generated examples must be separate from source context.")
        items.append(
            EnglishItem(
                kind=draft.kind,
                expression=draft.expression,
                meaning=draft.meaning,
                usage_note=draft.usage_note,
                examples=draft.examples,
                citation=EnglishCitation(
                    segment_index=draft.segment_index,
                    quote=draft.quote,
                    start=segment.start,
                    end=segment.end,
                    speaker=segment.speaker,
                ),
            )
        )
    return items


def render_english_markdown(artifact: EnglishArtifact) -> str:
    """Render source context separately from generated example sentences."""

    lines = [f"# English learning — {artifact.level}", "", artifact.overview, ""]
    if not artifact.items:
        lines.extend(("## Items", "", "No high-value English items were identified.", ""))
        return "\n".join(lines)

    lines.extend(("## Items", ""))
    for item in artifact.items:
        lines.extend((f"### {item.expression} ({item.kind.replace('_', ' ')})", ""))
        lines.extend((f"**Meaning:** {item.meaning}", "", f"**Usage:** {item.usage_note}", ""))
        lines.append("**Examples:**")
        lines.extend(f"- {example}" for example in item.examples)
        citation = item.citation
        source_note = f"segment {citation.segment_index + 1}"
        if citation.start is not None and citation.end is not None:
            source_note += (
                f" [{_format_timestamp(citation.start)}–{_format_timestamp(citation.end)}]"
            )
        if citation.speaker is not None:
            source_note += f", speaker {citation.speaker}"
        lines.extend(("", f"**Source context ({source_note}):**"))
        lines.extend(f"> {line}" for line in citation.quote.splitlines())
        lines.append("")
    return "\n".join(lines)


def render_english_json(artifact: EnglishArtifact) -> str:
    """Return one machine-readable English-learning artifact."""

    return json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _format_timestamp(seconds: float) -> str:
    milliseconds = int(seconds * 1000 + 0.5)
    minutes, remainder = divmod(milliseconds, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{millis:03}"


__all__ = [
    "CEFRLevel",
    "EnglishArtifact",
    "EnglishCitation",
    "EnglishItem",
    "EnglishItemDraft",
    "EnglishItemKind",
    "EnglishResponse",
    "ExampleSentence",
    "MAX_INPUT_BYTES",
    "MAX_ITEMS",
    "MAX_OUTPUT_TOKENS",
    "PROCESSOR_VERSION",
    "PROMPT_VERSION",
    "PROVIDER",
    "REASONING_EFFORT",
    "canonical_english_items",
    "english_content_sha256",
    "english_id_for",
    "english_input_json",
    "render_english_json",
    "render_english_markdown",
]
