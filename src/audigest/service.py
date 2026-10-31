"""Application service for local and direct remote transcript acquisition."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from audigest.acquisition import AcquisitionResult, LocalTranscriptAcquirer
from audigest.config import load_config
from audigest.errors import AudigestError, ProviderError, SourceResolutionError, StorageError
from audigest.models import Transcript, TranscriptOrigin
from audigest.renderers import render_markdown
from audigest.sources.base import SUPPORTED_MEDIA_EXTENSIONS, ResolvedSource
from audigest.sources.detect import detect_source
from audigest.sources.local import LocalFileResolver
from audigest.sources.remote import (
    RemoteTranscriptInput,
    fetch_remote_media,
    fetch_remote_transcript,
    remote_media_reference,
)
from audigest.storage import FileTranscriptStore, cache_id_for
from audigest.stt.router import get_provider


@dataclass(frozen=True)
class TranscriptionResult:
    """Canonical transcript plus its persisted location and cache status."""

    transcript: Transcript
    json_path: Path
    cached: bool
    used_stt: bool
    retained_media_path: Path | None = None


def _combine_source_fingerprint(
    source_identity: str,
    transcript_uri: str,
    transcript_fingerprint: str,
) -> str:
    encoded = json.dumps(
        ["supplied-transcript-v1", source_identity, transcript_uri, transcript_fingerprint],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _supplied_transcript(
    media_source: ResolvedSource,
    transcript_source: str,
    *,
    source_identity: str,
) -> AcquisitionResult:
    """Resolve a selected local or remote transcript for an audio/video source."""

    try:
        transcript_type = detect_source(transcript_source)
    except ValueError:
        raise SourceResolutionError("Supplied transcript source is invalid") from None

    acquirer = LocalTranscriptAcquirer()
    result: AcquisitionResult | None
    if transcript_type == "direct":
        supplied_remote: RemoteTranscriptInput = fetch_remote_transcript(transcript_source)
        result = acquirer.acquire_remote(supplied_remote)
        transcript_uri = supplied_remote.origin_uri
        suffix = supplied_remote.suffix
        if suffix != ".json":
            result = AcquisitionResult(
                draft=result.draft,
                source_fingerprint=result.source_fingerprint,
                source_info=media_source.source_info,
            )
    elif transcript_type == "local":
        resolved_transcript = LocalFileResolver().resolve(transcript_source)
        if resolved_transcript.media_path.suffix.lower() not in {
            ".json",
            ".txt",
            ".md",
            ".srt",
            ".vtt",
        }:
            raise SourceResolutionError(
                "--transcript must identify a canonical JSON, text/Markdown, SRT, or VTT "
                "transcript."
            )
        result = acquirer.acquire(resolved_transcript)
        if result is None:
            raise SourceResolutionError("--transcript did not contain an existing transcript")
        transcript_uri = resolved_transcript.source_info.uri
        if result.source_info is None:
            result = AcquisitionResult(
                draft=result.draft,
                source_fingerprint=result.source_fingerprint,
                source_info=media_source.source_info,
            )
    else:
        raise SourceResolutionError(
            "--transcript must be a local transcript file or direct HTTP(S) transcript URL."
        )

    return AcquisitionResult(
        draft=result.draft,
        source_fingerprint=_combine_source_fingerprint(
            source_identity,
            transcript_uri,
            result.source_fingerprint,
        ),
        source_info=result.source_info,
    )


def _retain_media(
    temp_media_path: Path | None, store: FileTranscriptStore, fingerprint: str
) -> Path | None:
    if temp_media_path is None:
        return None
    destination_dir = store.root / "media"
    destination = destination_dir / f"{fingerprint}{temp_media_path.suffix.lower()}"
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".remote-media.", suffix=".tmp", dir=destination_dir
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(temp_media_path, temporary)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                file_descriptor = os.open(
                    destination,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                )
                with os.fdopen(file_descriptor, "rb") as existing:
                    if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                        raise StorageError(
                            "Existing retained media path is not a regular file"
                        ) from None
                    existing_digest = hashlib.sha256()
                    while block := existing.read(1024 * 1024):
                        existing_digest.update(block)
                staged_digest = hashlib.sha256()
                with temporary.open("rb") as staged:
                    while block := staged.read(1024 * 1024):
                        staged_digest.update(block)
                if existing_digest.digest() != staged_digest.digest():
                    raise StorageError(
                        "Existing retained media differs; refusing to overwrite it"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)
    except StorageError:
        raise
    except OSError:
        raise StorageError("Could not retain downloaded media in the configured cache") from None
    return destination


def _run_resolved_source(
    resolved: ResolvedSource,
    *,
    acquisition: AcquisitionResult | None,
    source_fingerprint: str,
    provider: str | None,
    language: str | None,
    no_cache: bool,
    cache_dir: Path | str | None,
    config_file: Path | str | None,
    keep_media: bool,
    temp_media_path: Path | None = None,
) -> TranscriptionResult:
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
            retained = (
                _retain_media(temp_media_path, store, source_fingerprint) if keep_media else None
            )
            return TranscriptionResult(
                transcript=cached_transcript,
                json_path=store.transcript_path_for(cached_transcript.id),
                cached=True,
                used_stt=False,
                retained_media_path=retained,
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
            draft = selected_provider.transcribe(resolved.media_path, language=config.language)
        except AudigestError:
            raise
        except Exception:
            raise ProviderError(
                f"Provider '{selected_provider.name}' failed while transcribing the source."
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
        transcript = draft.finalize(source_info, id=cache_key, created_at=datetime.now(UTC))
    except AudigestError:
        raise
    except Exception:
        if used_stt:
            raise ProviderError(
                f"Provider '{draft.provider}' failed while normalizing the source."
            ) from None
        raise SourceResolutionError(
            "Existing transcript input could not be normalized into the canonical format."
        ) from None

    json_path = store.put(
        transcript,
        markdown=render_markdown(transcript),
        resolved_source=resolved,
        source_fingerprint=source_fingerprint,
        requested_language=config.language,
    )
    retained = _retain_media(temp_media_path, store, source_fingerprint) if keep_media else None
    return TranscriptionResult(
        transcript=transcript,
        json_path=json_path,
        cached=False,
        used_stt=used_stt,
        retained_media_path=retained,
    )


def _run_local_source(
    source: str | Path,
    *,
    explicit_stt: bool,
    transcript: str | None = None,
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
        source_url = str(source).strip()
        try:
            remote_reference = remote_media_reference(source_url)
        except SourceResolutionError:
            remote_reference = None
        if remote_reference is not None:
            resolved = remote_reference.resolved_source
            if transcript is not None:
                acquisition = _supplied_transcript(
                    resolved,
                    transcript,
                    source_identity=remote_reference.source_fingerprint,
                )
                return _run_resolved_source(
                    resolved,
                    acquisition=acquisition,
                    source_fingerprint=acquisition.source_fingerprint,
                    provider=provider,
                    language=language,
                    no_cache=no_cache,
                    cache_dir=cache_dir,
                    config_file=config_file,
                    keep_media=keep_media,
                )
            with fetch_remote_media(source_url) as downloaded:
                return _run_resolved_source(
                    downloaded.resolved_source,
                    acquisition=None,
                    source_fingerprint=downloaded.source_fingerprint,
                    provider=provider,
                    language=language,
                    no_cache=no_cache,
                    cache_dir=cache_dir,
                    config_file=config_file,
                    keep_media=keep_media,
                    temp_media_path=downloaded.resolved_source.media_path,
                )
        if transcript is not None:
            raise SourceResolutionError("--transcript can only be used with an audio/video source")
        remote = fetch_remote_transcript(source_url)
        resolved = remote.resolved_source
        acquisition = LocalTranscriptAcquirer().acquire_remote(remote)
    elif source_type == "local":
        resolved = LocalFileResolver().resolve(source)
        if explicit_stt and resolved.media_path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
            raise SourceResolutionError(
                "The 'transcribe' command accepts supported local file media only; "
                "use 'ingest' for transcript, text, or caption files."
            )
        if transcript is not None:
            if resolved.media_path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
                raise SourceResolutionError(
                    "--transcript can only be used with an audio/video source"
                )
            acquisition = _supplied_transcript(
                resolved,
                transcript,
                source_identity=resolved.content_sha256,
            )
        else:
            acquisition = None if explicit_stt else LocalTranscriptAcquirer().acquire(resolved)
    else:
        raise SourceResolutionError(
            f"Source type '{source_type}' is not supported yet; provide a local file path or "
            "a direct HTTP(S) transcript URL."
        )
    source_fingerprint = (
        resolved.content_sha256 if acquisition is None else acquisition.source_fingerprint
    )
    return _run_resolved_source(
        resolved,
        acquisition=acquisition,
        source_fingerprint=source_fingerprint,
        provider=provider,
        language=language,
        no_cache=no_cache,
        cache_dir=cache_dir,
        config_file=config_file,
        keep_media=keep_media,
    )


def ingest(
    source: str | Path,
    *,
    transcript: str | None = None,
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
        transcript=transcript,
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
