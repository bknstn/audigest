"""Bounded metadata and track selection for public single-video YouTube sources."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from audigest.errors import MissingDependencyError, RetrievalError, SourceResolutionError
from audigest.models import SourceInfo
from audigest.sources.base import SUPPORTED_MEDIA_EXTENSIONS, ResolvedSource

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_READ_CHUNK_SIZE = 64 * 1024
_SOCKET_TIMEOUT_SECONDS = 15
_EXECUTION_TIMEOUT_SECONDS = 60
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)


@dataclass(frozen=True, slots=True)
class YouTubeReference:
    """Validated single-video YouTube identity independent of URL form."""

    video_id: str
    url: str
    resolved_source: ResolvedSource


@dataclass(frozen=True, slots=True)
class YouTubeCaptionTrack:
    """Caption-track locator kept in memory and excluded from persistent identity."""

    language: str
    kind: str
    suffix: str
    request_url: str


@dataclass(frozen=True, slots=True)
class YouTubeAudioFormat:
    """Validated direct audio format; manifest-based formats are excluded."""

    suffix: str
    request_url: str
    bitrate: float
    format_id: str


@dataclass(frozen=True, slots=True)
class YouTubeMetadata:
    """Sanitized subset of extractor metadata required by acquisition."""

    reference: YouTubeReference
    source_info: SourceInfo
    freshness_fingerprint: str
    original_language: str | None
    captions: tuple[YouTubeCaptionTrack, ...]
    audio_formats: tuple[YouTubeAudioFormat, ...]


def youtube_reference(source: str) -> YouTubeReference:
    """Validate supported public video URL forms and canonicalize by video ID."""

    try:
        parsed = urlsplit(source.strip())
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        raise SourceResolutionError("YouTube video URL is malformed") from None
    if scheme not in {"http", "https"} or hostname not in _YOUTUBE_HOSTS:
        raise SourceResolutionError("Use a public YouTube watch, youtu.be, shorts, or embed URL")
    if parsed.username is not None or parsed.password is not None:
        raise SourceResolutionError("Credentials in YouTube URLs are not allowed")
    if port not in {None, 80 if scheme == "http" else 443}:
        raise SourceResolutionError("YouTube URL must use its standard HTTP(S) port")

    path_parts = [part for part in parsed.path.split("/") if part]
    video_id: str | None = None
    if hostname == "youtu.be":
        if len(path_parts) == 1:
            video_id = path_parts[0]
    elif path_parts == ["watch"]:
        values = query.get("v", [])
        if len(values) == 1:
            video_id = values[0]
    elif len(path_parts) == 2 and path_parts[0] in {"shorts", "embed"}:
        video_id = path_parts[1]

    if video_id is None or _VIDEO_ID.fullmatch(video_id) is None:
        raise SourceResolutionError(
            "YouTube URL must identify exactly one public video using an 11-character video ID"
        )

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    source_info = SourceInfo(type="youtube", uri=canonical_url)
    source_hash = hashlib.sha256(
        json.dumps(["youtube-source-v1", video_id], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return YouTubeReference(
        video_id=video_id,
        url=canonical_url,
        resolved_source=ResolvedSource(
            source_info=source_info,
            media_path=Path(f"{video_id}.m4a"),
            content_sha256=source_hash,
        ),
    )


def _yt_dlp_command(url: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-cookies",
        "--no-cookies-from-browser",
        "--no-update",
        "--no-cache-dir",
        "--no-geo-bypass",
        "--no-remote-components",
        "--no-playlist",
        "--ignore-no-formats-error",
        "--no-check-formats",
        "--extractor-args",
        "youtube:skip=hls,dash",
        "--dump-single-json",
        "--skip-download",
        "--quiet",
        "--no-progress",
        "--no-warnings",
        "--socket-timeout",
        str(_SOCKET_TIMEOUT_SECONDS),
        "--retries",
        "0",
        "--extractor-retries",
        "0",
        "--fragment-retries",
        "0",
        url,
    ]


def _stop_process(
    process: subprocess.Popen[bytes],
    reader: threading.Thread,
    reader_stop: threading.Event,
) -> None:
    reader_stop.set()
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if reader.ident is not None:
        reader.join(timeout=2)
    if process.stdout is not None and not reader.is_alive():
        try:
            process.stdout.close()
        except OSError:
            pass


def _run_youtube_metadata(url: str) -> bytes:
    """Run yt-dlp without user config, capturing bounded stdout and no stderr."""

    import importlib.util

    try:
        available = importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ValueError):
        available = False
    if not available:
        raise MissingDependencyError(
            "YouTube ingestion requires the optional yt-dlp package. Install audigest with "
            "the 'youtube' extra."
        )

    try:
        process = subprocess.Popen(
            _yt_dlp_command(url),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
    except OSError:
        raise RetrievalError("Could not start YouTube metadata retrieval") from None
    if process.stdout is None:
        _ = process.wait()
        raise RetrievalError("Could not read YouTube metadata")
    stdout = process.stdout

    messages: queue.Queue[bytes | None] = queue.Queue(maxsize=1)
    reader_failed = threading.Event()
    reader_stop = threading.Event()

    def publish(message: bytes | None) -> bool:
        while not reader_stop.is_set():
            try:
                messages.put(message, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def read_stdout() -> None:
        try:
            while not reader_stop.is_set():
                chunk = stdout.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                if not publish(chunk):
                    return
        except Exception:
            reader_failed.set()
        finally:
            publish(None)

    reader = threading.Thread(target=read_stdout, name="audigest-youtube-metadata", daemon=True)
    try:
        reader.start()
    except RuntimeError:
        _stop_process(process, reader, reader_stop)
        raise RetrievalError("Could not start bounded YouTube metadata reader") from None
    deadline = time.monotonic() + _EXECUTION_TIMEOUT_SECONDS
    output = bytearray()
    completed = False
    try:
        while not completed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetrievalError("YouTube metadata retrieval exceeded its 60-second limit")
            try:
                message = messages.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if message is None:
                completed = True
                continue
            if len(output) + len(message) > _MAX_METADATA_BYTES:
                raise RetrievalError("YouTube metadata exceeded the 4 MiB output limit")
            output.extend(message)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetrievalError("YouTube metadata retrieval exceeded its 60-second limit")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise RetrievalError(
                "YouTube metadata retrieval exceeded its 60-second limit"
            ) from None
        if reader_failed.is_set() or return_code != 0:
            raise RetrievalError("Could not retrieve public YouTube metadata")
        if not output:
            raise RetrievalError("YouTube returned no video metadata")
        reader.join(timeout=1)
        if reader.is_alive():
            raise RetrievalError("Could not finish reading YouTube metadata")
        if process.stdout is not None:
            process.stdout.close()
        return bytes(output)
    except RetrievalError:
        raise
    except Exception:
        raise RetrievalError("Could not retrieve public YouTube metadata") from None
    finally:
        if not completed or process.poll() is None:
            _stop_process(process, reader, reader_stop)
        else:
            reader_stop.set()
            reader.join(timeout=1)
            if process.stdout is not None and not reader.is_alive():
                process.stdout.close()


def _optional_text(value: Any, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 1000:
        raise RetrievalError(f"YouTube metadata field '{name}' is invalid")
    return value


def _optional_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RetrievalError(f"YouTube metadata field '{name}' is invalid")
    try:
        number = float(value)
    except OverflowError:
        raise RetrievalError(f"YouTube metadata field '{name}' is invalid") from None
    if not math.isfinite(number) or number < 0:
        raise RetrievalError(f"YouTube metadata field '{name}' is invalid")
    return number


def _validate_request_url(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 16384:
        raise RetrievalError(f"YouTube {label} URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RetrievalError(f"YouTube {label} URL is invalid") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80 if parsed.scheme.lower() == "http" else 443}
    ):
        raise RetrievalError(f"YouTube {label} URL is not a public HTTP(S) URL")
    return value


def _parse_tracks(raw: Any, kind: str) -> list[YouTubeCaptionTrack]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise RetrievalError("YouTube returned invalid caption metadata")
    tracks: list[YouTubeCaptionTrack] = []
    for language, entries in raw.items():
        if not isinstance(language, str) or not language or len(language) > 100:
            raise RetrievalError("YouTube returned invalid caption language metadata")
        if not isinstance(entries, list):
            raise RetrievalError("YouTube returned invalid caption track metadata")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RetrievalError("YouTube returned invalid caption track metadata")
            suffix_value = entry.get("ext")
            url = entry.get("url")
            if not isinstance(suffix_value, str) or suffix_value not in {"vtt", "srt"}:
                continue
            request_url = _validate_request_url(url, "caption")
            tracks.append(
                YouTubeCaptionTrack(
                    language=language,
                    kind=kind,
                    suffix=f".{suffix_value}",
                    request_url=request_url,
                )
            )
    return tracks


def _parse_audio_formats(raw: Any) -> list[YouTubeAudioFormat]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RetrievalError("YouTube returned invalid audio format metadata")
    formats: list[YouTubeAudioFormat] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise RetrievalError("YouTube returned invalid audio format metadata")
        if (
            entry.get("vcodec") != "none"
            or entry.get("acodec") is None
            or entry.get("acodec") == "none"
        ):
            continue
        protocol = entry.get("protocol")
        suffix_value = entry.get("ext")
        if (
            not isinstance(protocol, str)
            or protocol not in {"https", "http"}
            or not isinstance(suffix_value, str)
            or f".{suffix_value}" not in SUPPORTED_MEDIA_EXTENSIONS
        ):
            continue
        request_url = _validate_request_url(entry.get("url"), "audio format")
        bitrate = _optional_number(entry.get("abr"), "audio bitrate") or 0.0
        format_id = entry.get("format_id")
        if not isinstance(format_id, str) or not format_id:
            format_id = f"{suffix_value}-{bitrate:g}"
        formats.append(
            YouTubeAudioFormat(
                suffix=f".{suffix_value}",
                request_url=request_url,
                bitrate=bitrate,
                format_id=format_id[:100],
            )
        )
    return formats


def parse_youtube_metadata(reference: YouTubeReference, payload: bytes) -> YouTubeMetadata:
    """Validate bounded yt-dlp JSON and retain only safe, useful fields."""

    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        raise RetrievalError("YouTube returned invalid metadata JSON") from None
    if (
        not isinstance(raw, dict)
        or raw.get("id") != reference.video_id
        or raw.get("_type") == "playlist"
        or raw.get("_type") == "multi_video"
        or "entries" in raw
    ):
        raise RetrievalError("YouTube did not return metadata for the requested single video")

    availability = raw.get("availability")
    if availability is not None and (
        not isinstance(availability, str) or availability not in {"public", "unlisted"}
    ):
        raise RetrievalError("YouTube video is not publicly available")
    live_status = raw.get("live_status")
    if live_status is not None and not isinstance(live_status, str):
        raise RetrievalError("YouTube metadata field 'live_status' is invalid")
    if raw.get("is_private") is True or raw.get("is_live") is True:
        raise RetrievalError("Private or live YouTube videos are not supported")
    if live_status in {"is_live", "is_upcoming"}:
        raise RetrievalError("Live and upcoming YouTube videos are not supported")
    age_limit = _optional_number(raw.get("age_limit"), "age_limit")
    if age_limit is not None and age_limit > 0:
        raise RetrievalError("Age-restricted YouTube videos are not supported")

    title = _optional_text(raw.get("title"), "title")
    author = _optional_text(raw.get("uploader"), "uploader")
    duration = _optional_number(raw.get("duration"), "duration")
    upload_date = _optional_text(raw.get("upload_date"), "upload_date")
    release_timestamp = _optional_number(raw.get("release_timestamp"), "release_timestamp")
    original_language = _optional_text(
        raw.get("original_language") or raw.get("language"), "language"
    )

    published_at = None
    if upload_date is not None:
        try:
            published_at = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            raise RetrievalError("YouTube metadata field 'upload_date' is invalid") from None
    elif release_timestamp is not None:
        try:
            published_at = datetime.fromtimestamp(release_timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise RetrievalError("YouTube metadata field 'release_timestamp' is invalid") from None

    captions = _parse_tracks(raw.get("subtitles"), "manual")
    captions.extend(_parse_tracks(raw.get("automatic_captions"), "automatic"))
    audio_formats = _parse_audio_formats(raw.get("formats"))
    freshness_payload = json.dumps(
        [
            "youtube-metadata-freshness-v1",
            reference.video_id,
            title,
            author,
            duration,
            upload_date,
            release_timestamp,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    source_info = SourceInfo(
        type="youtube",
        uri=reference.resolved_source.source_info.uri,
        title=title,
        author=author,
        published_at=published_at,
        duration_seconds=duration,
    )
    return YouTubeMetadata(
        reference=reference,
        source_info=source_info,
        freshness_fingerprint=hashlib.sha256(freshness_payload).hexdigest(),
        original_language=original_language,
        captions=tuple(captions),
        audio_formats=tuple(audio_formats),
    )


def get_youtube_metadata(reference: YouTubeReference) -> YouTubeMetadata:
    """Fetch bounded public video metadata through the optional yt-dlp command."""

    return parse_youtube_metadata(reference, _run_youtube_metadata(reference.url))


def _language_match(track_language: str, preferred: str | None) -> int | None:
    if preferred is None:
        return None
    track = track_language.replace("_", "-").casefold()
    wanted = preferred.replace("_", "-").casefold()
    if track == wanted:
        return 0
    if track.split("-", maxsplit=1)[0] == wanted.split("-", maxsplit=1)[0]:
        return 1
    return None


def select_caption_track(
    metadata: YouTubeMetadata, requested_language: str | None
) -> YouTubeCaptionTrack | None:
    """Prefer requested language, original language, then deterministic available tracks."""

    if not metadata.captions:
        return None

    def preference(track: YouTubeCaptionTrack) -> tuple[int, str, int, int]:
        requested_match = _language_match(track.language, requested_language)
        if requested_match is not None:
            language_rank = requested_match
        else:
            original_match = _language_match(track.language, metadata.original_language)
            language_rank = 2 + original_match if original_match is not None else 4
        return (
            language_rank,
            track.language.casefold(),
            0 if track.kind == "manual" else 1,
            0 if track.suffix == ".vtt" else 1,
        )

    return min(metadata.captions, key=preference)


def select_audio_format(metadata: YouTubeMetadata, provider: str) -> YouTubeAudioFormat:
    """Choose a direct audio-only format supported by the requested STT adapter."""

    if provider == "openai":
        from audigest.stt.openai import SUPPORTED_AUDIO_EXTENSIONS

        candidates = [
            audio_format
            for audio_format in metadata.audio_formats
            if audio_format.suffix in SUPPORTED_AUDIO_EXTENSIONS
        ]
    else:
        candidates = list(metadata.audio_formats)
    if not candidates:
        raise RetrievalError("YouTube did not expose a supported direct audio-only format")
    return min(candidates, key=lambda item: (-item.bitrate, item.suffix, item.format_id))


__all__ = [
    "YouTubeAudioFormat",
    "YouTubeCaptionTrack",
    "YouTubeMetadata",
    "YouTubeReference",
    "get_youtube_metadata",
    "parse_youtube_metadata",
    "select_audio_format",
    "select_caption_track",
    "youtube_reference",
]
