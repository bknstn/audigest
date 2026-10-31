"""Application service for local and direct remote transcript acquisition."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from audigest.acquisition import AcquisitionResult, LocalTranscriptAcquirer
from audigest.config import load_config
from audigest.errors import AudigestError, ProviderError, SourceResolutionError
from audigest.models import Transcript, TranscriptOrigin
from audigest.renderers import render_markdown
from audigest.sources.base import SUPPORTED_MEDIA_EXTENSIONS
from audigest.sources.detect import detect_source
from audigest.sources.local import LocalFileResolver
from audigest.sources.remote import fetch_remote_transcript
from audigest.storage import FileTranscriptStore, cache_id_for
from audigest.stt.router import get_provider


@dataclass(frozen=True)
class TranscriptionResult:
    """Canonical transcript plus its persisted location and cache status."""

    transcript: Transcript
    json_path: Path
    cached: bool
    used_stt: bool


def _run_local_source(
    source: str | Path,
    *,
    explicit_stt: bool,
    provider: str | None = None,
    language: str | None = None,
    no_cache: bool = False,
    keep_media: bool = False,
    cache_dir: Path | str | None = None,
    config_file: Path | str | None = None,
) -> TranscriptionResult:
    """Run either transcript-first ingestion or explicit media-only STT.

    In transcript-first mode, direct inputs and prioritized sidecars are
    prepared before cache lookup. Explicit STT skips all acquisition and uses
    only the local media fingerprint, allowing it to reuse prior STT work
    without consuming transcript/caption cache entries. ``no_cache`` skips
    lookup and still stores the newly produced transcript.
    """
    del keep_media  # Local originals are never copied, removed, or retained elsewhere.
    try:
        source_type = detect_source(source)
    except ValueError:
        raise SourceResolutionError("Source URL is malformed") from None
    acquisition: AcquisitionResult | None
    if source_type == "direct":
        if explicit_stt:
            raise SourceResolutionError(
                "The 'transcribe' command accepts local file media only; use 'ingest' for remote "
                "transcript URLs."
            )
        remote = fetch_remote_transcript(str(source).strip())
        resolved = remote.resolved_source
        acquisition = LocalTranscriptAcquirer().acquire_remote(remote)
    elif source_type == "local":
        resolved = LocalFileResolver().resolve(source)
        if explicit_stt and resolved.media_path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
            raise SourceResolutionError(
                "The 'transcribe' command accepts supported local file media only; "
                "use 'ingest' for transcript, text, or caption files."
            )
        acquisition = None if explicit_stt else LocalTranscriptAcquirer().acquire(resolved)
    else:
        raise SourceResolutionError(
            f"Source type '{source_type}' is not supported yet; provide a local file path or "
            "a direct HTTP(S) transcript URL."
        )
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


def ingest(
    source: str | Path,
    *,
    provider: str | None = None,
    language: str | None = None,
    no_cache: bool = False,
    keep_media: bool = False,
    cache_dir: Path | str | None = None,
    config_file: Path | str | None = None,
) -> TranscriptionResult:
    """Obtain the best available transcript before considering STT fallback."""

    return _run_local_source(
        source,
        explicit_stt=False,
        provider=provider,
        language=language,
        no_cache=no_cache,
        keep_media=keep_media,
        cache_dir=cache_dir,
        config_file=config_file,
    )


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
    """Run explicit STT on a local media file without transcript acquisition."""

    return _run_local_source(
        source,
        explicit_stt=True,
        provider=provider,
        language=language,
        no_cache=no_cache,
        keep_media=keep_media,
        cache_dir=cache_dir,
        config_file=config_file,
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
