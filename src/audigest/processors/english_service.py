"""Canonical-only English-learning orchestration and cache validation."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

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
from audigest.processors.english import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_TOKENS,
    PROCESSOR_VERSION,
    PROMPT_VERSION,
    CEFRLevel,
    EnglishArtifact,
    EnglishItemDraft,
    EnglishResponse,
    canonical_english_items,
    english_content_sha256,
    english_id_for,
    english_input_json,
)
from audigest.processors.english_storage import FileEnglishStore
from audigest.processors.llm import AnthropicProcessorProvider
from audigest.processors.llm_config import (
    DEFAULT_MODELS,
    LLMProviderName,
    cache_targets,
    reasoning_effort_for,
    select_llm,
)
from audigest.processors.openai_english import OpenAIEnglishProvider
from audigest.storage import FileTranscriptStore

_LEVELS = frozenset({"A1", "A2", "B1", "B2", "C1", "C2"})


class EnglishProvider(Protocol):
    """Boundary for English-learning extraction from a stored transcript."""

    name: str
    model: str

    def extract(self, transcript: Transcript, level: CEFRLevel) -> EnglishResponse:
        """Return an un-enriched structured response for one CEFR level."""


@dataclass(frozen=True, slots=True)
class EnglishResult:
    """English-learning artifact plus cache paths and whether it was reused."""

    artifact: EnglishArtifact
    json_path: Path
    markdown_path: Path
    cached: bool


def _level(value: str) -> CEFRLevel:
    if not isinstance(value, str) or value not in _LEVELS:
        raise ConfigurationError("English level must be one of: A1, A2, B1, B2, C1, C2")
    return cast(CEFRLevel, value)


def _check_cached_identity(
    artifact: EnglishArtifact,
    transcript: Transcript,
    *,
    level: CEFRLevel,
    english_id: str,
    provider: LLMProviderName,
    model: str,
) -> None:
    if (
        artifact.id != english_id
        or artifact.transcript_id != transcript.id
        or artifact.content_sha256 != english_content_sha256(transcript, level)
        or artifact.level != level
        or artifact.processor_version != PROCESSOR_VERSION
        or artifact.prompt_version != PROMPT_VERSION
        or artifact.provider != provider
        or artifact.model != model
        or artifact.reasoning_effort != reasoning_effort_for(provider)
        or artifact.max_output_tokens != MAX_OUTPUT_TOKENS
    ):
        raise StorageError("English cache entry does not match the transcript and request")

    try:
        response = EnglishResponse(
            overview=artifact.overview,
            items=[
                EnglishItemDraft(
                    kind=item.kind,
                    expression=item.expression,
                    segment_index=item.citation.segment_index,
                    quote=item.citation.quote,
                    meaning=item.meaning,
                    usage_note=item.usage_note,
                    examples=item.examples,
                )
                for item in artifact.items
            ],
        )
        canonical_items = canonical_english_items(response, transcript.segments)
    except (ProcessorError, ValidationError):
        raise StorageError(
            "English cache evidence does not match the canonical transcript"
        ) from None
    if canonical_items != artifact.items:
        raise StorageError("English cache evidence does not match the canonical transcript")


def english_transcript(
    transcript_id: str,
    level: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    no_cache: bool = False,
    provider_factory: Callable[[str], EnglishProvider] | None = None,
) -> EnglishResult:
    """Create a separate English-learning result without resolving the source."""

    selected_level = _level(level)
    # Use only the configured cache path; STT provider/language settings are irrelevant here.
    config = load_config(provider="auto", language="")
    transcript = FileTranscriptStore(config.cache_dir).get(transcript_id)
    if transcript is None:
        raise SourceResolutionError(
            f"Transcript '{transcript_id}' was not found in the local cache; "
            "run 'audigest ingest <source>' first."
        )
    if not transcript.text.strip():
        raise ProcessorError("Cannot process a transcript with no non-whitespace text.")

    selected_provider, requested_model = select_llm(provider, model, os.environ)
    english_store = FileEnglishStore(config.cache_dir)

    if not no_cache:
        for cached_provider, cached_model in cache_targets(selected_provider, requested_model):
            english_id = english_id_for(
                transcript, selected_level, cached_model, provider=cached_provider
            )
            cached = english_store.get(english_id)
            if cached is not None:
                _check_cached_identity(
                    cached,
                    transcript,
                    level=selected_level,
                    english_id=english_id,
                    provider=cached_provider,
                    model=cached_model,
                )
                entry = english_store.path_for(english_id)
                return EnglishResult(
                    artifact=cached,
                    json_path=entry / "english.json",
                    markdown_path=entry / "english.md",
                    cached=True,
                )

    if selected_provider == "auto":
        raise ProcessorError(
            "No matching cached English result was found; provider 'auto' is cache-only. "
            "Choose --provider openai or --provider anthropic to create one."
        )

    input_json = english_input_json(transcript, selected_level)
    if len(input_json.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ProcessorError(
            "Transcript is too large for English processing in one request "
            "(maximum input is 256 KiB)."
        )

    selected_model = requested_model or DEFAULT_MODELS[selected_provider]
    english_id = english_id_for(
        transcript, selected_level, selected_model, provider=selected_provider
    )
    try:
        factory = provider_factory or (
            OpenAIEnglishProvider if selected_provider == "openai" else AnthropicProcessorProvider
        )
        selected = factory(selected_model)
        if selected.name != selected_provider or selected.model != selected_model:
            raise ProcessorError("The selected English provider returned inconsistent provenance.")
        response = selected.extract(transcript, selected_level)
    except AudigestError:
        raise
    except Exception:
        raise ProcessorError("The selected English provider failed unexpectedly.") from None

    if not isinstance(response, EnglishResponse):
        try:
            response = EnglishResponse.model_validate(response)
        except Exception:
            raise ProcessorError(
                "The English provider returned an invalid structured response."
            ) from None
    else:
        try:
            response = EnglishResponse.model_validate(response.model_dump())
        except Exception:
            raise ProcessorError(
                "The English provider returned an invalid structured response."
            ) from None
    try:
        items = canonical_english_items(response, transcript.segments)
    except ValidationError:
        raise ProcessorError(
            "The English provider returned invalid structured item content."
        ) from None
    content_sha256 = english_content_sha256(transcript, selected_level)
    try:
        artifact = EnglishArtifact(
            id=english_id,
            transcript_id=transcript.id,
            content_sha256=content_sha256,
            level=selected_level,
            processor_version=PROCESSOR_VERSION,
            prompt_version=PROMPT_VERSION,
            provider=selected_provider,
            model=selected_model,
            reasoning_effort=reasoning_effort_for(selected_provider),
            max_output_tokens=MAX_OUTPUT_TOKENS,
            created_at=datetime.now(UTC),
            overview=response.overview,
            items=items,
        )
    except Exception:
        raise ProcessorError("The English provider returned invalid artifact content.") from None
    json_path, markdown_path = english_store.put(artifact)
    return EnglishResult(
        artifact=artifact,
        json_path=json_path,
        markdown_path=markdown_path,
        cached=False,
    )


__all__ = ["EnglishProvider", "EnglishResult", "english_transcript"]
