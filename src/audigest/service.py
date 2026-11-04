"""Application service for local and direct remote transcript acquisition."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from audigest.acquisition import AcquisitionResult, LocalTranscriptAcquirer
from audigest.config import load_config
from audigest.errors import (
    AudigestError,
    ConfigurationError,
    ProviderError,
    SourceResolutionError,
    StorageError,
)
from audigest.models import Transcript, TranscriptOrigin
from audigest.renderers import render_markdown
from audigest.sources.base import (
    SUPPORTED_MEDIA_EXTENSIONS,
    SUPPORTED_TEXT_EXTENSIONS,
    ResolvedSource,
)
from audigest.sources.detect import detect_source
from audigest.sources.local import LocalFileResolver
from audigest.sources.publisher_acquisition import (
    PublisherTranscriptSelection,
    prepare_publisher_page,
)
from audigest.sources.remote import (
    MAX_TRANSCRIPT_BYTES,
    BoundedRemoteDocument,
    RemotePageReference,
    RemoteTranscriptInput,
    RemoteURLReference,
    fetch_bounded_remote_document,
    fetch_remote_caption,
    fetch_remote_media,
    fetch_remote_transcript,
    is_spotify_media_url,
    remote_media_reference,
    remote_page_reference,
    remote_url_reference,
)
from audigest.sources.rss import looks_like_rss_document
from audigest.sources.rss_acquisition import (
    RSSMediaSelection,
    RSSPreparation,
    RSSTranscriptSelection,
    acquire_rss_episode,
    select_rss_episode,
)
from audigest.sources.spotify import (
    prepare_spotify_episode,
    spotify_episode_reference,
)
from audigest.sources.youtube import (
    get_youtube_metadata,
    select_audio_format,
    select_caption_track,
    youtube_reference,
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


def _run_publisher_page(
    page_reference: RemotePageReference,
    *,
    document: BoundedRemoteDocument | None = None,
    provider: str | None,
    language: str | None,
    no_cache: bool,
    keep_media: bool,
    cache_dir: Path | str | None,
    config_file: Path | str | None,
) -> TranscriptionResult:
    """Prepare one publisher page, then run its acquired transcript or media."""
    config = load_config(
        provider=provider,
        language=language,
        cache_dir=cache_dir,
        config_file=config_file,
    )
    preparation = prepare_publisher_page(
        page_reference,
        requested_language=config.language,
        document=document,
    )
    if isinstance(preparation, PublisherTranscriptSelection):
        acquisition = preparation.acquisition
        return _run_resolved_source(
            page_reference.resolved_source,
            acquisition=acquisition,
            source_fingerprint=acquisition.source_fingerprint,
            provider=config.provider,
            language=config.language,
            no_cache=no_cache,
            cache_dir=config.cache_dir,
            config_file=config_file,
            keep_media=keep_media,
        )

    with fetch_remote_media(
        preparation.request_url,
        suffix_override=preparation.suffix,
        source_info_override=preparation.page_reference.resolved_source.source_info,
        locator_fingerprint_override=preparation.locator_fingerprint,
    ) as downloaded:
        return _run_resolved_source(
            downloaded.resolved_source,
            acquisition=None,
            source_fingerprint=downloaded.source_fingerprint,
            provider=config.provider,
            language=config.language,
            no_cache=no_cache,
            cache_dir=config.cache_dir,
            config_file=config_file,
            keep_media=keep_media,
            temp_media_path=downloaded.resolved_source.media_path,
        )


def _run_rss_source(
    feed_reference: RemoteURLReference,
    *,
    document: BoundedRemoteDocument | None,
    episode: str | None,
    transcript: str | None,
    provider: str | None,
    language: str | None,
    no_cache: bool,
    keep_media: bool,
    cache_dir: Path | str | None,
    config_file: Path | str | None,
) -> TranscriptionResult:
    """Resolve one RSS episode, then use the shared transcript or media pipeline."""
    config = load_config(
        provider=provider,
        language=language,
        cache_dir=cache_dir,
        config_file=config_file,
    )
    if document is None:
        document = fetch_bounded_remote_document(
            feed_reference.request_url,
            accept="application/rss+xml, application/xml, text/xml, */*",
            limit=MAX_TRANSCRIPT_BYTES,
            label="RSS feed",
        )
    selected_episode = select_rss_episode(
        feed_reference,
        document,
        selector=episode,
    )

    if transcript is not None:
        acquisition = _supplied_transcript(
            selected_episode.resolved_source,
            transcript,
            source_identity=selected_episode.source_identity,
        )
        return _run_resolved_source(
            selected_episode.resolved_source,
            acquisition=acquisition,
            source_fingerprint=acquisition.source_fingerprint,
            provider=config.provider,
            language=config.language,
            no_cache=no_cache,
            cache_dir=config.cache_dir,
            config_file=config_file,
            keep_media=keep_media,
        )

    preparation = acquire_rss_episode(
        selected_episode,
        requested_language=config.language,
    )
    return _run_rss_preparation(
        preparation,
        provider=config.provider,
        language=config.language,
        no_cache=no_cache,
        keep_media=keep_media,
        cache_dir=config.cache_dir,
        config_file=config_file,
    )


def _run_rss_preparation(
    preparation: RSSPreparation,
    *,
    provider: str,
    language: str | None,
    no_cache: bool,
    keep_media: bool,
    cache_dir: Path,
    config_file: Path | str | None,
) -> TranscriptionResult:
    """Send an RSS-selected transcript or enclosure through shared processing."""

    if isinstance(preparation, RSSTranscriptSelection):
        acquisition = preparation.acquisition
        return _run_resolved_source(
            preparation.resolved_source,
            acquisition=acquisition,
            source_fingerprint=acquisition.source_fingerprint,
            provider=provider,
            language=language,
            no_cache=no_cache,
            cache_dir=cache_dir,
            config_file=config_file,
            keep_media=keep_media,
        )
    assert isinstance(preparation, RSSMediaSelection)
    with fetch_remote_media(
        preparation.request_url,
        suffix_override=preparation.suffix,
        source_info_override=preparation.resolved_source.source_info,
        locator_fingerprint_override=preparation.locator_fingerprint,
    ) as downloaded:
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


def _spotify_feed_reference(feed: str) -> RemoteURLReference:
    """Validate an explicit HTTP(S) RSS endpoint without making a request."""

    try:
        reference = remote_url_reference(feed.strip())
    except SourceResolutionError:
        raise ConfigurationError("--feed must be a valid HTTP(S) RSS feed URL") from None
    if is_spotify_media_url(reference.request_url):
        raise ConfigurationError("--feed must identify a publisher-hosted RSS feed")
    return reference


def _run_spotify_source(
    source: str,
    *,
    feed: str | None,
    episode: str | None,
    transcript: str | None,
    provider: str | None,
    language: str | None,
    no_cache: bool,
    keep_media: bool,
    cache_dir: Path | str | None,
    config_file: Path | str | None,
) -> TranscriptionResult:
    """Map a public Spotify episode title to one explicitly supplied RSS item."""

    reference = spotify_episode_reference(source)
    if episode is not None and not episode.strip():
        raise ConfigurationError("--episode must contain an exact RSS GUID or item link")
    if episode is not None and feed is None:
        raise ConfigurationError("--episode with a Spotify URL requires --feed")
    feed_reference = _spotify_feed_reference(feed) if feed is not None else None

    if transcript is not None:
        acquisition = _supplied_transcript(
            reference.resolved_source,
            transcript,
            source_identity=reference.resolved_source.content_sha256,
        )
        return _run_resolved_source(
            reference.resolved_source,
            acquisition=acquisition,
            source_fingerprint=acquisition.source_fingerprint,
            provider=provider,
            language=language,
            no_cache=no_cache,
            cache_dir=cache_dir,
            config_file=config_file,
            keep_media=keep_media,
        )

    if feed_reference is None:
        raise ConfigurationError("Spotify ingestion requires an explicit --feed or --transcript")

    config = load_config(
        provider=provider,
        language=language,
        cache_dir=cache_dir,
        config_file=config_file,
    )
    selected_episode = prepare_spotify_episode(
        reference,
        feed_reference,
        selector=episode,
    )
    preparation = acquire_rss_episode(
        selected_episode,
        requested_language=config.language,
    )
    return _run_rss_preparation(
        preparation,
        provider=config.provider,
        language=config.language,
        no_cache=no_cache,
        keep_media=keep_media,
        cache_dir=config.cache_dir,
        config_file=config_file,
    )


def _run_local_source(
    source: str | Path,
    *,
    explicit_stt: bool,
    episode: str | None = None,
    feed: str | None = None,
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
    if feed is not None and source_type != "spotify":
        raise ConfigurationError("--feed is valid only with a public Spotify episode URL")
    if episode is not None and source_type != "spotify":
        if not episode.strip():
            raise ConfigurationError("--episode must contain an exact RSS GUID or item link")
        if source_type not in {"direct", "rss"}:
            raise ConfigurationError("--episode is valid only with an HTTP(S) RSS feed")
        try:
            episode_feed_reference = remote_url_reference(str(source).strip())
        except SourceResolutionError:
            raise ConfigurationError("--episode requires an HTTP(S) RSS feed URL") from None
        if source_type == "direct" and episode_feed_reference.suffix:
            raise ConfigurationError(
                "--episode can force RSS parsing only for an extensionless HTTP(S) source"
            )
        if source_type == "rss" and episode_feed_reference.suffix in (
            SUPPORTED_MEDIA_EXTENSIONS | SUPPORTED_TEXT_EXTENSIONS
        ):
            raise ConfigurationError("--episode is invalid for direct media or transcript URLs")
    if source_type == "youtube":
        if explicit_stt:
            raise SourceResolutionError(
                "The 'transcribe' command accepts local media only; use 'ingest' for YouTube."
            )
        return _run_youtube_source(
            str(source),
            transcript=transcript,
            provider=provider,
            language=language,
            no_cache=no_cache,
            keep_media=keep_media,
            cache_dir=cache_dir,
            config_file=config_file,
        )
    if source_type == "spotify":
        if explicit_stt:
            raise SourceResolutionError(
                "The 'transcribe' command accepts local media only; use 'ingest' for Spotify."
            )
        return _run_spotify_source(
            str(source),
            feed=feed,
            episode=episode,
            transcript=transcript,
            provider=provider,
            language=language,
            no_cache=no_cache,
            keep_media=keep_media,
            cache_dir=cache_dir,
            config_file=config_file,
        )
    acquisition: AcquisitionResult | None
    if source_type == "rss":
        if explicit_stt:
            raise SourceResolutionError(
                "The 'transcribe' command accepts local file media only; use 'ingest' "
                "for RSS feeds."
            )
        return _run_rss_source(
            remote_url_reference(str(source).strip()),
            document=None,
            episode=episode,
            transcript=transcript,
            provider=provider,
            language=language,
            no_cache=no_cache,
            keep_media=keep_media,
            cache_dir=cache_dir,
            config_file=config_file,
        )
    if source_type == "direct":
        if explicit_stt:
            raise SourceResolutionError(
                "The 'transcribe' command accepts local file media only; use 'ingest' for remote "
                "transcript URLs."
            )
        source_url = str(source).strip()
        url_reference = remote_url_reference(source_url)
        if episode is not None:
            return _run_rss_source(
                url_reference,
                document=None,
                episode=episode,
                transcript=transcript,
                provider=provider,
                language=language,
                no_cache=no_cache,
                keep_media=keep_media,
                cache_dir=cache_dir,
                config_file=config_file,
            )
        if is_spotify_media_url(source_url):
            raise SourceResolutionError(
                "Spotify player and CDN media URLs are not supported; use a publisher-hosted "
                "episode URL."
            )
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
        if url_reference.suffix in SUPPORTED_TEXT_EXTENSIONS:
            if transcript is not None:
                raise SourceResolutionError(
                    "--transcript can only be used with an audio/video or publisher-page source"
                )
            remote = fetch_remote_transcript(source_url)
            resolved = remote.resolved_source
            acquisition = LocalTranscriptAcquirer().acquire_remote(remote)
        else:
            page_reference = remote_page_reference(source_url)
            if transcript is not None:
                acquisition = _supplied_transcript(
                    page_reference.resolved_source,
                    transcript,
                    source_identity=page_reference.source_fingerprint,
                )
                return _run_resolved_source(
                    page_reference.resolved_source,
                    acquisition=acquisition,
                    source_fingerprint=acquisition.source_fingerprint,
                    provider=provider,
                    language=language,
                    no_cache=no_cache,
                    cache_dir=cache_dir,
                    config_file=config_file,
                    keep_media=keep_media,
                )
            if not url_reference.suffix:
                document = fetch_bounded_remote_document(
                    url_reference.request_url,
                    accept=(
                        "text/html, application/xhtml+xml, application/rss+xml, "
                        "application/xml, text/xml, */*"
                    ),
                    limit=MAX_TRANSCRIPT_BYTES,
                    label="publisher page or RSS feed",
                )
                if looks_like_rss_document(document.content):
                    return _run_rss_source(
                        url_reference,
                        document=document,
                        episode=None,
                        transcript=None,
                        provider=provider,
                        language=language,
                        no_cache=no_cache,
                        keep_media=keep_media,
                        cache_dir=cache_dir,
                        config_file=config_file,
                    )
                return _run_publisher_page(
                    page_reference,
                    document=document,
                    provider=provider,
                    language=language,
                    no_cache=no_cache,
                    keep_media=keep_media,
                    cache_dir=cache_dir,
                    config_file=config_file,
                )
            return _run_publisher_page(
                page_reference,
                provider=provider,
                language=language,
                no_cache=no_cache,
                keep_media=keep_media,
                cache_dir=cache_dir,
                config_file=config_file,
            )
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


def _run_youtube_source(
    source: str,
    *,
    transcript: str | None,
    provider: str | None,
    language: str | None,
    no_cache: bool,
    keep_media: bool,
    cache_dir: Path | str | None,
    config_file: Path | str | None,
) -> TranscriptionResult:
    """Acquire YouTube captions first, then reuse STT cache before audio retrieval."""

    reference = youtube_reference(source)
    config = load_config(
        provider=provider,
        language=language,
        cache_dir=cache_dir,
        config_file=config_file,
    )
    if transcript is not None:
        acquisition = _supplied_transcript(
            reference.resolved_source,
            transcript,
            source_identity=reference.resolved_source.content_sha256,
        )
        return _run_resolved_source(
            reference.resolved_source,
            acquisition=acquisition,
            source_fingerprint=acquisition.source_fingerprint,
            provider=config.provider,
            language=config.language,
            no_cache=no_cache,
            cache_dir=config.cache_dir,
            config_file=config_file,
            keep_media=keep_media,
        )

    metadata = get_youtube_metadata(reference)
    source_info = metadata.source_info
    resolved = replace(reference.resolved_source, source_info=source_info)
    track = select_caption_track(metadata, config.language)
    if track is not None:
        origin_uri = f"{reference.url}#captions/{track.kind}/{quote(track.language, safe='')}"
        track_identity = hashlib.sha256(
            json.dumps(
                ["youtube-caption-track-v1", reference.video_id, track.language, track.kind],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        remote_caption = fetch_remote_caption(
            track.request_url,
            suffix=track.suffix,
            source_info=source_info,
            origin_uri=origin_uri,
            locator_fingerprint=track_identity,
        )
        acquisition = LocalTranscriptAcquirer().acquire_remote(remote_caption)
        acquisition = AcquisitionResult(
            draft=acquisition.draft.model_copy(
                update={"language": track.language, "provider": "youtube-captions"}
            ),
            source_fingerprint=acquisition.source_fingerprint,
            source_info=source_info,
        )
        return _run_resolved_source(
            remote_caption.resolved_source,
            acquisition=acquisition,
            source_fingerprint=acquisition.source_fingerprint,
            provider=config.provider,
            language=config.language,
            no_cache=no_cache,
            cache_dir=config.cache_dir,
            config_file=config_file,
            keep_media=False,
        )

    source_fingerprint = metadata.freshness_fingerprint
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

    if config.provider == "auto":
        raise ProviderError(
            "No usable public YouTube captions were found and provider 'auto' is cache-only. "
            "Choose --provider fake, --provider openai, --provider elevenlabs, or --provider local "
            "to download audio and run STT."
        )
    _ = get_provider(config.provider)
    audio_format = select_audio_format(metadata, config.provider)
    with fetch_remote_media(
        audio_format.request_url,
        suffix_override=audio_format.suffix,
        source_info_override=source_info,
        source_fingerprint_override=source_fingerprint,
        locator_fingerprint_override=source_fingerprint,
    ) as downloaded:
        return _run_resolved_source(
            downloaded.resolved_source,
            acquisition=None,
            source_fingerprint=source_fingerprint,
            provider=config.provider,
            language=config.language,
            no_cache=True,
            cache_dir=config.cache_dir,
            config_file=config_file,
            keep_media=keep_media,
            temp_media_path=downloaded.resolved_source.media_path,
        )


def ingest(
    source: str | Path,
    *,
    episode: str | None = None,
    feed: str | None = None,
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
        episode=episode,
        feed=feed,
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

    config = load_config(
        provider="auto",
        language="",
        cache_dir=cache_dir,
        config_file=config_file,
    )
    transcript = FileTranscriptStore(config.cache_dir).get(transcript_id)
    if transcript is None:
        raise SourceResolutionError(
            f"Transcript '{transcript_id}' was not found in the local cache; "
            "run 'audigest ingest <source>' first."
        )
    return transcript
