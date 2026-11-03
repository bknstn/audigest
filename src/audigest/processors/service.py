"""Stored-transcript summary orchestration and deterministic summary-cache lookup."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from audigest.config import load_config
from audigest.errors import (
    AudigestError,
    ConfigurationError,
    ProcessorError,
    SourceResolutionError,
    StorageError,
)
from audigest.models import Transcript
from audigest.processors.openai import DEFAULT_MODEL, OpenAISummaryProvider
from audigest.processors.storage import FileSummaryStore
from audigest.processors.summary import (
    MAX_OUTPUT_TOKENS,
    PROCESSOR_VERSION,
    PROMPT_VERSION,
    PROVIDER,
    REASONING_EFFORT,
    SummaryArtifact,
    SummaryPointDraft,
    SummaryResponse,
    canonical_key_points,
    summary_content_sha256,
    summary_id_for,
)
from audigest.storage import FileTranscriptStore

_MODEL_NAME = re.compile(r"[^\s\x00]{1,128}\Z")


class SummaryProvider(Protocol):
    """Provider boundary for structured summary generation."""

    name: str
    model: str

    def summarize(self, transcript: Transcript) -> SummaryResponse:
        """Return an un-enriched summary response for a stored transcript."""


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """Summary content plus cache paths and whether it was reused."""

    artifact: SummaryArtifact
    json_path: Path
    markdown_path: Path
    cached: bool


def _selection(
    provider: str | None,
    model: str | None,
    environ: Mapping[str, str],
) -> tuple[str, str]:
    selected_provider = (
        provider if provider is not None else environ.get("AUDIGEST_LLM_PROVIDER", "auto")
    )
    if selected_provider not in {"auto", "openai"}:
        raise ConfigurationError("LLM provider must be one of: auto, openai")
    selected_model = (
        model if model is not None else environ.get("AUDIGEST_LLM_MODEL", DEFAULT_MODEL)
    )
    if not isinstance(selected_model, str) or _MODEL_NAME.fullmatch(selected_model) is None:
        raise ConfigurationError("LLM model must be a non-empty name of at most 128 characters")
    return selected_provider, selected_model


def _check_cached_identity(
    artifact: SummaryArtifact,
    transcript: Transcript,
    *,
    summary_id: str,
    model: str,
) -> None:
    if (
        artifact.id != summary_id
        or artifact.transcript_id != transcript.id
        or artifact.content_sha256 != summary_content_sha256(transcript)
        or artifact.processor_version != PROCESSOR_VERSION
        or artifact.prompt_version != PROMPT_VERSION
        or artifact.provider != PROVIDER
        or artifact.model != model
        or artifact.reasoning_effort != REASONING_EFFORT
        or artifact.max_output_tokens != MAX_OUTPUT_TOKENS
    ):
        raise StorageError("Summary cache entry does not match the transcript and request")

    try:
        response = SummaryResponse(
            summary=artifact.summary,
            key_points=[
                SummaryPointDraft(
                    point=point.point,
                    segment_index=point.citation.segment_index,
                    quote=point.citation.quote,
                )
                for point in artifact.key_points
            ],
        )
        canonical_points = canonical_key_points(response, transcript.segments)
    except (ProcessorError, ValidationError):
        raise StorageError(
            "Summary cache citations do not match the canonical transcript"
        ) from None
    if canonical_points != artifact.key_points:
        raise StorageError("Summary cache citations do not match the canonical transcript")


def summarize_transcript(
    transcript_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    no_cache: bool = False,
    provider_factory: Callable[[str], SummaryProvider] | None = None,
) -> SummaryResult:
    """Summarize one canonical cache entry without resolving its original source."""

    config = load_config(provider="auto")
    transcript = FileTranscriptStore(config.cache_dir).get(transcript_id)
    if transcript is None:
        raise SourceResolutionError(
            f"Transcript '{transcript_id}' was not found in the local cache; "
            "run 'audigest ingest <source>' first."
        )
    if not transcript.text.strip():
        raise ProcessorError("Cannot summarize a transcript with no non-whitespace text.")
    selected_provider, selected_model = _selection(provider, model, os.environ)
    summary_id = summary_id_for(transcript, selected_model)
    summary_store = FileSummaryStore(config.cache_dir)

    if not no_cache:
        cached = summary_store.get(summary_id)
        if cached is not None:
            _check_cached_identity(
                cached,
                transcript,
                summary_id=summary_id,
                model=selected_model,
            )
            return SummaryResult(
                artifact=cached,
                json_path=summary_store.path_for(summary_id) / "summary.json",
                markdown_path=summary_store.path_for(summary_id) / "summary.md",
                cached=True,
            )

    if selected_provider == "auto":
        raise ProcessorError(
            "No matching cached summary was found; provider 'auto' is cache-only. "
            "Choose --provider openai to create a summary."
        )

    try:
        if provider_factory is None:
            selected: SummaryProvider = OpenAISummaryProvider(selected_model)
        else:
            selected = provider_factory(selected_model)
        if selected.name != PROVIDER or selected.model != selected_model:
            raise ProcessorError("The selected summary provider returned inconsistent provenance.")
        response = selected.summarize(transcript)
    except AudigestError:
        raise
    except Exception:
        raise ProcessorError("The selected summary provider failed unexpectedly.") from None
    if not isinstance(response, SummaryResponse):
        try:
            response = SummaryResponse.model_validate(response)
        except Exception:
            raise ProcessorError(
                "The summary provider returned an invalid structured response."
            ) from None
    key_points = canonical_key_points(response, transcript.segments)
    summary_sha256 = summary_content_sha256(transcript)
    try:
        artifact = SummaryArtifact(
            id=summary_id,
            transcript_id=transcript.id,
            content_sha256=summary_sha256,
            processor_version=PROCESSOR_VERSION,
            prompt_version=PROMPT_VERSION,
            provider=PROVIDER,
            model=selected_model,
            reasoning_effort=REASONING_EFFORT,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            created_at=datetime.now(UTC),
            summary=response.summary,
            key_points=key_points,
        )
    except Exception:
        raise ProcessorError("The summary provider returned invalid artifact content.") from None
    json_path, markdown_path = summary_store.put(artifact)
    return SummaryResult(
        artifact=artifact,
        json_path=json_path,
        markdown_path=markdown_path,
        cached=False,
    )


__all__ = ["SummaryProvider", "SummaryResult", "summarize_transcript"]
