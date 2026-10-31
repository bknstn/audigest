"""Bounded retrieval of explicitly supplied remote transcript documents."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from audigest.errors import RetrievalError, SourceResolutionError
from audigest.models import SourceInfo
from audigest.sources.base import (
    SUPPORTED_MEDIA_EXTENSIONS,
    SUPPORTED_TEXT_EXTENSIONS,
    ResolvedSource,
)

MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MAX_REMOTE_MEDIA_BYTES = 100 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15
TOTAL_REQUEST_TIMEOUT_SECONDS = 60
_READ_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class RemoteTranscriptInput:
    """Fetched transcript bytes and stable source identity without a temporary file."""

    content: bytes
    suffix: str
    resolved_source: ResolvedSource
    origin_uri: str
    locator_fingerprint: str


@dataclass(frozen=True, slots=True)
class RemoteMediaInput:
    """Downloaded remote media in a temporary directory and its cache identity."""

    resolved_source: ResolvedSource
    source_fingerprint: str
    locator_fingerprint: str


@dataclass(frozen=True, slots=True)
class RemoteMediaReference:
    """Remote media identity available without downloading the media bytes."""

    resolved_source: ResolvedSource
    source_fingerprint: str
    locator_fingerprint: str
    suffix: str


def _normalized_http_url(url: str) -> tuple[str, str]:
    """Validate an unauthenticated HTTP(S) URL and return request/public forms."""

    if any(character.isspace() or ord(character) < 32 for character in url):
        raise SourceResolutionError("Remote transcript URL is invalid")
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise SourceResolutionError("Remote transcript URL is invalid") from None

    if scheme not in {"http", "https"} or not hostname:
        raise SourceResolutionError("Remote transcript URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise SourceResolutionError("Credentials in remote transcript URLs are not allowed")

    host = hostname.lower().rstrip(".")
    if not host:
        raise SourceResolutionError("Remote transcript URL must include a hostname")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    request_url = urlunsplit((scheme, host, parsed.path or "/", parsed.query, ""))
    public_url = urlunsplit((scheme, host, parsed.path or "/", "", ""))
    return request_url, public_url


class _RedirectResponse(Exception):
    """Internal signal used to process one closed redirect response at a time."""

    def __init__(self, location: str | None) -> None:
        self.location = location


class _NoAutoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Surface redirects to the caller without reading their response bodies."""

    def _capture_redirect(
        self,
        request: Any,
        file_pointer: Any,
        status: int,
        message: str,
        headers: Any,
    ) -> None:
        _ = request, status, message
        location = headers.get("Location")
        file_pointer.close()
        raise _RedirectResponse(location)

    def http_error_301(self, request: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        self._capture_redirect(request, fp, code, msg, headers)

    def http_error_302(self, request: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        self._capture_redirect(request, fp, code, msg, headers)

    def http_error_303(self, request: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        self._capture_redirect(request, fp, code, msg, headers)

    def http_error_307(self, request: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        self._capture_redirect(request, fp, code, msg, headers)

    def http_error_308(self, request: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        self._capture_redirect(request, fp, code, msg, headers)


def _open_redirected_response(
    request_url: str,
    *,
    deadline: float,
    accept: str,
) -> tuple[Any, str]:
    """Follow validated redirects manually, closing each redirect without reading it."""

    opener = urllib.request.build_opener(_NoAutoRedirectHandler())
    redirect_count = 0
    while True:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise RetrievalError("Remote request exceeded its time limit")
        request = urllib.request.Request(
            request_url,
            headers={"Accept": accept},
            method="GET",
        )
        try:
            response = opener.open(
                request,
                timeout=min(REQUEST_TIMEOUT_SECONDS, remaining_time),
            )
        except _RedirectResponse as redirect:
            if redirect_count >= 5:
                raise RetrievalError("Remote request exceeded the redirect limit") from None
            if not redirect.location:
                raise RetrievalError("Remote redirect has no target") from None
            target = urljoin(request_url, redirect.location)
            try:
                request_url, _ = _normalized_http_url(target)
            except SourceResolutionError:
                raise RetrievalError("Remote redirect target is not allowed") from None
            redirect_count += 1
            continue
        except RetrievalError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            except Exception:
                pass
            raise RetrievalError("Remote request failed (HTTP or redirect error)") from None
        except Exception:
            raise RetrievalError("Remote request failed (HTTP or timeout error)") from None
        return response, request_url


def _check_content_length(response: Any, limit: int, label: str) -> int | None:
    content_length = response.headers.get("Content-Length")
    if content_length is None:
        return None
    normalized_length = content_length.strip()
    if not re.fullmatch(r"[0-9]+", normalized_length):
        raise RetrievalError(f"Remote {label} response has an invalid content length")
    declared = int(normalized_length)
    if declared > limit:
        raise RetrievalError(f"Remote {label} exceeds the configured size limit")
    return declared


def remote_media_reference(url: str) -> RemoteMediaReference:
    """Create the public identity of a direct remote media URL without fetching it."""

    request_url, public_url = _normalized_http_url(url)
    parsed = urlsplit(request_url)
    suffix = PurePosixPath(unquote(parsed.path)).suffix.lower()
    if suffix not in SUPPORTED_MEDIA_EXTENSIONS:
        raise SourceResolutionError("Remote media URL must have a supported audio/video extension")
    locator_fingerprint = hashlib.sha256(request_url.encode("utf-8")).hexdigest()
    identity = hashlib.sha256(
        json.dumps(["remote-media-locator-v1", locator_fingerprint]).encode("utf-8")
    ).hexdigest()
    resolved = ResolvedSource(
        source_info=SourceInfo(type="direct", uri=public_url),
        media_path=Path(f"source{suffix}"),
        content_sha256=identity,
    )
    return RemoteMediaReference(
        resolved_source=resolved,
        source_fingerprint=identity,
        locator_fingerprint=locator_fingerprint,
        suffix=suffix,
    )


@contextmanager
def fetch_remote_media(
    url: str,
    *,
    suffix_override: str | None = None,
    source_info_override: SourceInfo | None = None,
    source_fingerprint_override: str | None = None,
    locator_fingerprint_override: str | None = None,
) -> Iterator[RemoteMediaInput]:
    """Stream supported remote media to a temporary file and clean it after use."""

    if suffix_override is None:
        reference = remote_media_reference(url)
        suffix = reference.suffix
        source_info = source_info_override or reference.resolved_source.source_info
        locator_fingerprint = locator_fingerprint_override or reference.locator_fingerprint
    else:
        request_url, public_url = _normalized_http_url(url)
        suffix = suffix_override.lower()
        if suffix not in SUPPORTED_MEDIA_EXTENSIONS:
            raise SourceResolutionError("Downloaded media format is not supported")
        source_info = source_info_override or SourceInfo(type="direct", uri=public_url)
        locator_fingerprint = (
            locator_fingerprint_override or hashlib.sha256(request_url.encode("utf-8")).hexdigest()
        )
    deadline = time.monotonic() + TOTAL_REQUEST_TIMEOUT_SECONDS
    try:
        temporary_directory = tempfile.TemporaryDirectory(prefix="audigest-media-")
    except Exception:
        raise RetrievalError("Remote media temporary storage is unavailable") from None
    with temporary_directory as temp_dir:
        response, _final_url = _open_redirected_response(
            _normalized_http_url(url)[0],
            deadline=deadline,
            accept="audio/*, video/*, application/octet-stream, */*",
        )
        media_path = Path(temp_dir) / f"source{suffix}"
        digest = hashlib.sha256()
        total = 0
        try:
            with response, media_path.open("wb") as output:
                declared_length = _check_content_length(response, MAX_REMOTE_MEDIA_BYTES, "media")
                while True:
                    if time.monotonic() >= deadline:
                        raise RetrievalError("Remote media request exceeded its time limit")
                    read_size = min(_READ_CHUNK_SIZE, MAX_REMOTE_MEDIA_BYTES + 1 - total)
                    chunk = getattr(response, "read1", response.read)(read_size)
                    if time.monotonic() >= deadline:
                        raise RetrievalError("Remote media request exceeded its time limit")
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_REMOTE_MEDIA_BYTES:
                        raise RetrievalError("Remote media exceeds the 100 MiB size limit")
                    digest.update(chunk)
                    output.write(chunk)
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError(
                "Remote media download failed or temporary storage was unavailable"
            ) from None
        if declared_length is not None and total != declared_length:
            raise RetrievalError("Remote media response length did not match its content length")
        media_hash = digest.hexdigest()
        source_fingerprint = (
            source_fingerprint_override
            or hashlib.sha256(
                json.dumps(
                    ["remote-media-acquisition-v1", locator_fingerprint, media_hash],
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        resolved_source = ResolvedSource(
            source_info=source_info,
            media_path=media_path,
            content_sha256=media_hash,
        )
        yield RemoteMediaInput(
            resolved_source=resolved_source,
            source_fingerprint=source_fingerprint,
            locator_fingerprint=locator_fingerprint,
        )


def _fetch_bounded_remote_bytes(url: str, *, accept: str, limit: int, label: str) -> bytes:
    request_url, _ = _normalized_http_url(url)
    deadline = time.monotonic() + TOTAL_REQUEST_TIMEOUT_SECONDS
    response, _final_url = _open_redirected_response(request_url, deadline=deadline, accept=accept)
    try:
        with response:
            declared_length = _check_content_length(response, limit, label)
            content = bytearray()
            while len(content) <= limit:
                if time.monotonic() >= deadline:
                    raise RetrievalError(f"Remote {label} request exceeded its time limit")
                remaining = limit + 1 - len(content)
                read_chunk = getattr(response, "read1", response.read)
                chunk = read_chunk(min(_READ_CHUNK_SIZE, remaining))
                if time.monotonic() >= deadline:
                    raise RetrievalError(f"Remote {label} request exceeded its time limit")
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > limit:
                raise RetrievalError(f"Remote {label} exceeds the configured size limit")
            if declared_length is not None and len(content) != declared_length:
                raise RetrievalError(
                    f"Remote {label} response length did not match its content length"
                )
    except RetrievalError:
        raise
    except Exception:
        raise RetrievalError(f"Remote {label} response could not be read") from None
    return bytes(content)


def fetch_remote_caption(
    url: str,
    *,
    suffix: str,
    source_info: SourceInfo,
    origin_uri: str,
    locator_fingerprint: str,
) -> RemoteTranscriptInput:
    """Fetch one bounded YouTube caption track without persisting its signed URL."""

    normalized_url, _ = _normalized_http_url(url)
    if urlsplit(normalized_url).scheme != "https":
        raise RetrievalError("YouTube caption track must use HTTPS")
    normalized_suffix = suffix.lower()
    if normalized_suffix not in {".vtt", ".srt"}:
        raise SourceResolutionError("YouTube caption format must be VTT or SRT")
    content = _fetch_bounded_remote_bytes(
        normalized_url,
        accept="text/vtt, application/x-subrip, text/plain, */*",
        limit=MAX_TRANSCRIPT_BYTES,
        label="transcript",
    )
    resolved_source = ResolvedSource(
        source_info=source_info,
        media_path=Path(f"youtube-caption{normalized_suffix}"),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return RemoteTranscriptInput(
        content=content,
        suffix=normalized_suffix,
        resolved_source=resolved_source,
        origin_uri=origin_uri,
        locator_fingerprint=locator_fingerprint,
    )


def fetch_remote_transcript(url: str) -> RemoteTranscriptInput:
    """Fetch a bounded direct transcript URL without persisting its locator query."""

    request_url, public_url = _normalized_http_url(url)
    try:
        parsed = urlsplit(request_url)
        suffix = PurePosixPath(unquote(parsed.path)).suffix.lower()
    except ValueError:
        raise SourceResolutionError("Remote transcript URL is invalid") from None
    if suffix in SUPPORTED_MEDIA_EXTENSIONS:
        raise SourceResolutionError(
            "Remote media URLs are not supported; provide a local file or a direct remote "
            "transcript/caption URL ending in .txt, .md, .json, .srt, or .vtt."
        )
    if suffix not in SUPPORTED_TEXT_EXTENSIONS:
        raise SourceResolutionError(
            "Direct HTTP(S) ingestion currently supports transcript or caption URLs ending "
            "in .txt, .md, .json, .srt, or .vtt."
        )

    locator_url = request_url
    content = _fetch_bounded_remote_bytes(
        request_url,
        accept="text/plain, text/vtt, application/json, */*",
        limit=MAX_TRANSCRIPT_BYTES,
        label="transcript",
    )

    locator_fingerprint = hashlib.sha256(locator_url.encode("utf-8")).hexdigest()
    resolved_source = ResolvedSource(
        source_info=SourceInfo(type="direct", uri=public_url),
        media_path=Path(PurePosixPath(parsed.path).name),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return RemoteTranscriptInput(
        content=content,
        suffix=suffix,
        resolved_source=resolved_source,
        origin_uri=public_url,
        locator_fingerprint=locator_fingerprint,
    )


__all__ = [
    "MAX_TRANSCRIPT_BYTES",
    "REQUEST_TIMEOUT_SECONDS",
    "TOTAL_REQUEST_TIMEOUT_SECONDS",
    "RemoteTranscriptInput",
    "fetch_remote_transcript",
    "fetch_remote_caption",
]
