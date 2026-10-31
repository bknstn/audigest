"""Application service for the local-file transcription slice."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from audigest.acquisition import LocalTranscriptAcquirer
from audigest.config import load_config
from audigest.errors import AudigestError, ProviderError, SourceResolutionError
from audigest.models import Transcript, TranscriptOrigin
from audigest.renderers import render_markdown
from audigest.sources.detect import detect_source
from audigest.sources.local import LocalFileResolver
from audigest.storage import FileTranscriptStore, cache_id_for
from audigest.stt.router import get_provider


@dataclass(frozen=True)
class TranscriptionResult:
    """Canonical transcript plus its persisted location and cache status."""

    transcript: Transcript
    json_path: Path
    cached: bool
    used_stt: bool


def transcribe(
    source: str | Path,
    *,
    provider: str | None = None,
    language: str | None = None,
    no_cache: bool = False,
    keep_media: bool = False,
    cache_dir: Path | str | None = None,
    config_file: Path | str | None = None,
) -> TranscriptionResult:
    """Acquire existing transcript data before falling back to STT.

    Direct transcript, text, and caption inputs and recognized sidecars are
    prepared before cache lookup so their effective content fingerprint
    participates in cache identity. ``no_cache`` skips the cache pointer read
    but still honors those local inputs and persists the result. Local files
    are read in place; ``keep_media`` has no effect for local sources.
    """
    del keep_media  # Local originals are never copied, removed, or retained elsewhere.
    try:
        source_type = detect_source(source)
    except ValueError:
        raise SourceResolutionError("Source URL is malformed") from None
    if source_type != "local":
        raise SourceResolutionError(
            f"Source type '{source_type}' is not supported yet; provide a local file path."
        )

    resolved = LocalFileResolver().resolve(source)
    acquisition = LocalTranscriptAcquirer().acquire(resolved)
    source_fingerprint = (
        resolved.content_sha256 if acquisition is None else acquisition.source_fingerprint
    )
    config = load_config(
        provider=provider,
        language=language,
        cache_dir=cache_dir,
        config_file=config_file,
    )
    store = FileTranscriptStore(config.cache_dir)

    if not no_cache:
        cached_transcript = store.find_for_source(
            resolved,
            source_fingerprint,
            requested_language=config.language,
        )
        if cached_transcript is not None:
            return TranscriptionResult(
                transcript=cached_transcript,
                json_path=store.transcript_path_for(cached_transcript.id),
                cached=True,
                used_stt=False,
            )

    used_stt = acquisition is None
    source_info = resolved.source_info
    if acquisition is not None:
        draft = acquisition.draft
        source_info = acquisition.source_info or source_info
        if draft.language is None and config.language is not None:
            draft = draft.model_copy(update={"language": config.language})
    else:
        selected_provider = get_provider(config.provider)
        try:
            draft = selected_provider.transcribe(
                resolved.media_path,
                language=config.language,
            )
        except AudigestError:
            raise
        except Exception:
            raise ProviderError(
                f"Provider '{selected_provider.name}' failed while transcribing the local source."
            ) from None
        draft = draft.model_copy(
            update={
                "origin": TranscriptOrigin(
                    kind="stt",
                    uri=resolved.source_info.uri,
                    format=resolved.media_path.suffix.lower()[1:],
                )
            }
        )

    cache_key = cache_id_for(
        resolved,
        provider=draft.provider,
        model=draft.model,
        language=config.language,
        source_fingerprint=source_fingerprint,
    )
    try:
        transcript = draft.finalize(
            source_info,
            id=cache_key,
            created_at=datetime.now(UTC),
        )
    except AudigestError:
        raise
    except Exception:
        if used_stt:
            raise ProviderError(
                f"Provider '{draft.provider}' failed while normalizing the local source."
            ) from None
        raise SourceResolutionError(
            "Existing transcript input could not be normalized into the canonical format."
        ) from None

    markdown = render_markdown(transcript)
    json_path = store.put(
        transcript,
        markdown=markdown,
        resolved_source=resolved,
        source_fingerprint=source_fingerprint,
        requested_language=config.language,
    )
    return TranscriptionResult(
        transcript=transcript,
        json_path=json_path,
        cached=False,
        used_stt=used_stt,
    )


def show_transcript(
    transcript_id: str,
    *,
    cache_dir: Path | str | None = None,
    config_file: Path | str | None = None,
) -> Transcript:
    """Load an already stored transcript by cache ID without touching its source."""

    config = load_config(cache_dir=cache_dir, config_file=config_file)
    transcript = FileTranscriptStore(config.cache_dir).get(transcript_id)
    if transcript is None:
        raise SourceResolutionError(
            f"Transcript '{transcript_id}' was not found in the local cache; "
            "run 'audigest ingest <source>' first."
        )
    return transcript
