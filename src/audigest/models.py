"""Canonical, provider-independent transcript models."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceType = Literal["local", "youtube", "direct", "rss", "spotify"]


class SourceInfo(BaseModel):
    """Public metadata identifying the source represented by a transcript."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: SourceType
    uri: str = Field(min_length=1)
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None

    @field_validator("duration_seconds")
    @classmethod
    def validate_duration(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("duration_seconds must be finite and non-negative")
        return value

    @field_validator("published_at")
    @classmethod
    def validate_published_at_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("published_at must be timezone-aware")
        return value


class TranscriptSegment(BaseModel):
    """A piece of canonical transcript text with optional timing."""

    model_config = ConfigDict(extra="forbid")

    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    text: str
    speaker: str | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> TranscriptSegment:
        if (self.start is None) != (self.end is None):
            raise ValueError("segment start and end must either both be present or both be absent")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("segment end must be greater than or equal to start")
        return self


class TranscriptOrigin(BaseModel):
    """Where transcript material was obtained, distinct from the represented source."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["text", "captions", "canonical", "stt"]
    uri: str = Field(min_length=1)
    format: str | None = Field(default=None, min_length=1)


def _segment_text(value: Any) -> str | None:
    if isinstance(value, TranscriptSegment):
        return value.text
    if isinstance(value, dict):
        segment_text = value.get("text")
        if isinstance(segment_text, str):
            return segment_text
    return None


def _check_segment_order(segments: list[TranscriptSegment]) -> None:
    previous_start: float | None = None
    for segment in segments:
        if segment.start is None:
            continue
        if previous_start is not None and segment.start < previous_start:
            raise ValueError("segments must be ordered by start time")
        previous_start = segment.start


class Transcript(BaseModel):
    """Versioned transcript artifact with text derived from its segments."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    id: str = Field(min_length=1)
    source: SourceInfo
    origin: TranscriptOrigin | None = None
    provider: str = Field(min_length=1)
    model: str | None = None
    language: str | None = None
    created_at: datetime
    segments: list[TranscriptSegment]
    text: str

    @model_validator(mode="before")
    @classmethod
    def derive_and_check_text(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        raw_segments = value.get("segments")
        if not isinstance(raw_segments, list):
            return value

        segment_texts = [_segment_text(segment) for segment in raw_segments]
        if any(segment_text is None for segment_text in segment_texts):
            return value
        expected = "\n".join(
            segment_text for segment_text in segment_texts if segment_text is not None
        )
        if "text" in value and value["text"] != expected:
            raise ValueError("text must match the text derived from segments")
        result = dict(value)
        result["text"] = expected
        return result

    @field_validator("created_at")
    @classmethod
    def validate_created_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @field_validator("provider")
    @classmethod
    def validate_provider_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider must not be empty")
        return value

    @model_validator(mode="after")
    def validate_segment_order(self) -> Transcript:
        _check_segment_order(self.segments)
        return self


class TranscriptDraft(BaseModel):
    """Provider-neutral transcript material before source identity is attached."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str | None = None
    language: str | None = None
    origin: TranscriptOrigin | None = None
    segments: list[TranscriptSegment]

    @field_validator("provider")
    @classmethod
    def validate_provider_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider name must not be empty")
        return value

    @model_validator(mode="after")
    def validate_segment_order(self) -> TranscriptDraft:
        _check_segment_order(self.segments)
        return self

    def finalize(
        self,
        source_info: SourceInfo,
        *,
        id: str,
        created_at: datetime,
    ) -> Transcript:
        """Attach source and artifact identity to create a canonical transcript."""

        return Transcript(
            id=id,
            source=source_info,
            origin=self.origin,
            provider=self.provider,
            model=self.model,
            language=self.language,
            created_at=created_at,
            segments=self.segments,
            text="\n".join(segment.text for segment in self.segments),
        )
