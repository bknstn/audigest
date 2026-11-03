"""Canonical-only project application orchestration and output-cache lookup."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
from audigest.processors.application import (
    APPLICATION_VERSION,
    DEFAULT_MODEL,
    MAX_INPUT_BYTES,
    PROMPT_VERSION,
    PROVIDER,
    ApplicationArtifact,
    ApplicationRecommendationDraft,
    ApplicationResponse,
    ProjectEvidenceDraft,
    TranscriptEvidenceDraft,
    application_content_sha256,
    application_id_for,
    application_input_json,
    canonical_recommendations,
    context_metadata,
)
from audigest.processors.application_storage import FileApplicationStore
from audigest.processors.openai_apply import ApplicationProvider, OpenAIApplicationProvider
from audigest.processors.project_context import (
    ProjectContext,
    ensure_cache_outside_project,
    read_project_context,
    resolve_project_root,
)
from audigest.processors.summary import MAX_OUTPUT_TOKENS, REASONING_EFFORT
from audigest.storage import FileTranscriptStore

_MODEL_NAME = re.compile(r"[^\s\x00]{1,128}\Z")


@dataclass(frozen=True, slots=True)
class ApplicationResult:
    """Application result plus cache paths and whether it was reused."""

    artifact: ApplicationArtifact
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
    if selected_provider not in {"auto", PROVIDER}:
        raise ConfigurationError("LLM provider must be one of: auto, openai")
    selected_model = (
        model if model is not None else environ.get("AUDIGEST_LLM_MODEL", DEFAULT_MODEL)
    )
    if not isinstance(selected_model, str) or _MODEL_NAME.fullmatch(selected_model) is None:
        raise ConfigurationError("LLM model must be a non-empty name of at most 128 characters")
    return selected_provider, selected_model


def _check_cached_identity(
    artifact: ApplicationArtifact,
    transcript: Transcript,
    project: ProjectContext,
    *,
    application_id: str,
    model: str,
) -> None:
    if (
        artifact.id != application_id
        or artifact.transcript_id != transcript.id
        or artifact.content_sha256 != application_content_sha256(transcript, project)
        or artifact.project_name != project.name
        or artifact.context_files != context_metadata(project)
        or artifact.processor_version != APPLICATION_VERSION
        or artifact.prompt_version != PROMPT_VERSION
        or artifact.provider != PROVIDER
        or artifact.model != model
        or artifact.reasoning_effort != REASONING_EFFORT
        or artifact.max_output_tokens != MAX_OUTPUT_TOKENS
    ):
        raise StorageError("Application cache entry does not match the transcript and project")

    try:
        response = ApplicationResponse(
            overview=artifact.overview,
            recommendations=[
                ApplicationRecommendationDraft(
                    idea=recommendation.idea,
                    relevance=recommendation.relevance,
                    suggested_action=recommendation.suggested_action,
                    transcript_evidence=TranscriptEvidenceDraft(
                        segment_index=recommendation.transcript_evidence.segment_index,
                        quote=recommendation.transcript_evidence.quote,
                    ),
                    project_evidence=ProjectEvidenceDraft(
                        relative_path=recommendation.project_evidence.relative_path,
                        quote=recommendation.project_evidence.quote,
                    ),
                )
                for recommendation in artifact.recommendations
            ],
        )
        current_recommendations = canonical_recommendations(
            response,
            transcript.segments,
            project.files,
        )
    except (ProcessorError, ValidationError):
        raise StorageError(
            "Application cache evidence does not match the canonical transcript and selected "
            "project files"
        ) from None
    if current_recommendations != artifact.recommendations:
        raise StorageError(
            "Application cache evidence does not match the canonical transcript and selected "
            "project files"
        )


def apply_to_project(
    transcript_id: str,
    project: str | Path,
    *,
    context_paths: Sequence[str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    no_cache: bool = False,
    provider_factory: Callable[[str], ApplicationProvider] | None = None,
) -> ApplicationResult:
    """Apply one stored transcript to explicitly selected project context, read-only."""

    # Need the shared cache path only; ignore STT provider and language settings.
    config = load_config(provider="auto", language="")
    project_root = resolve_project_root(project)
    ensure_cache_outside_project(config.cache_dir, project_root)
    transcript = FileTranscriptStore(config.cache_dir).get(transcript_id)
    if transcript is None:
        raise SourceResolutionError(
            f"Transcript '{transcript_id}' was not found in the local cache; "
            "run 'audigest ingest <source>' first."
        )
    if not transcript.text.strip():
        raise ProcessorError("Cannot apply an empty transcript.")

    # Files are freshly read and validated before application cache lookup.
    selected_project = read_project_context(project_root, context_paths)
    selected_provider, selected_model = _selection(provider, model, os.environ)
    application_id = application_id_for(transcript, selected_project, selected_model)
    application_store = FileApplicationStore(config.cache_dir)

    if not no_cache:
        cached = application_store.get(application_id)
        if cached is not None:
            _check_cached_identity(
                cached,
                transcript,
                selected_project,
                application_id=application_id,
                model=selected_model,
            )
            entry = application_store.path_for(application_id)
            return ApplicationResult(
                artifact=cached,
                json_path=entry / "application.json",
                markdown_path=entry / "application.md",
                cached=True,
            )

    if selected_provider == "auto":
        raise ProcessorError(
            "No matching cached application was found; provider 'auto' is cache-only. "
            "Choose --provider openai to create recommendations."
        )

    input_json = application_input_json(transcript, selected_project)
    if len(input_json.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ProcessorError(
            "Combined transcript and project context is too large for one request "
            "(maximum serialized input is 256 KiB)."
        )

    try:
        if provider_factory is None:
            selected: ApplicationProvider = OpenAIApplicationProvider(selected_model)
        else:
            selected = provider_factory(selected_model)
        if selected.name != PROVIDER or selected.model != selected_model:
            raise ProcessorError(
                "The selected application provider returned inconsistent provenance."
            )
        response = selected.apply(transcript, selected_project)
    except AudigestError:
        raise
    except Exception:
        raise ProcessorError("The selected application provider failed unexpectedly.") from None

    if not isinstance(response, ApplicationResponse):
        try:
            response = ApplicationResponse.model_validate(response)
        except Exception:
            raise ProcessorError(
                "The application provider returned an invalid structured response."
            ) from None
    recommendations = canonical_recommendations(
        response,
        transcript.segments,
        selected_project.files,
    )
    try:
        artifact = ApplicationArtifact(
            id=application_id,
            transcript_id=transcript.id,
            content_sha256=application_content_sha256(transcript, selected_project),
            project_name=selected_project.name,
            context_files=context_metadata(selected_project),
            processor_version=APPLICATION_VERSION,
            prompt_version=PROMPT_VERSION,
            provider=PROVIDER,
            model=selected_model,
            reasoning_effort=REASONING_EFFORT,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            created_at=datetime.now(UTC),
            overview=response.overview,
            recommendations=recommendations,
        )
    except Exception:
        raise ProcessorError(
            "The application provider returned invalid artifact content."
        ) from None
    json_path, markdown_path = application_store.put(artifact)
    return ApplicationResult(
        artifact=artifact,
        json_path=json_path,
        markdown_path=markdown_path,
        cached=False,
    )


__all__ = ["ApplicationResult", "apply_to_project"]
