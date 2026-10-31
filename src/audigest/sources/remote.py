"""Bounded retrieval of explicitly supplied remote transcript documents."""

from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.request
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
    deadline = time.monotonic() + TOTAL_REQUEST_TIMEOUT_SECONDS
    opener = urllib.request.build_opener(_NoAutoRedirectHandler())
    try:
        redirect_count = 0
        while True:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise RetrievalError("Remote transcript request exceeded its time limit")
            request = urllib.request.Request(
                request_url,
                headers={"Accept": "text/plain, text/vtt, application/json, */*"},
                method="GET",
            )
            try:
                response = opener.open(
                    request,
                    timeout=min(REQUEST_TIMEOUT_SECONDS, remaining_time),
                )
            except _RedirectResponse as redirect:
                if redirect_count >= 5:
                    raise RetrievalError("Remote transcript exceeded the redirect limit") from None
                if not redirect.location:
                    raise RetrievalError("Remote transcript redirect has no target") from None
                target = urljoin(request_url, redirect.location)
                try:
                    request_url, _ = _normalized_http_url(target)
                except SourceResolutionError:
                    raise RetrievalError(
                        "Remote transcript redirect target is not allowed"
                    ) from None
                redirect_count += 1
                continue
            break

        with response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                normalized_length = content_length.strip()
                if not re.fullmatch(r"[0-9]+", normalized_length):
                    raise RetrievalError("Remote transcript response has an invalid content length")
                if int(normalized_length) > MAX_TRANSCRIPT_BYTES:
                    raise RetrievalError("Remote transcript exceeds the 2 MiB size limit")

            content = bytearray()
            while len(content) <= MAX_TRANSCRIPT_BYTES:
                if time.monotonic() >= deadline:
                    raise RetrievalError("Remote transcript request exceeded its time limit")
                remaining = MAX_TRANSCRIPT_BYTES + 1 - len(content)
                read_chunk = getattr(response, "read1", response.read)
                chunk = read_chunk(min(_READ_CHUNK_SIZE, remaining))
                if time.monotonic() >= deadline:
                    raise RetrievalError("Remote transcript request exceeded its time limit")
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > MAX_TRANSCRIPT_BYTES:
                raise RetrievalError("Remote transcript exceeds the 2 MiB size limit")
    except RetrievalError:
        raise
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RetrievalError("Remote transcript request failed (HTTP or redirect error)") from None
    except Exception:
        raise RetrievalError("Remote transcript request failed (HTTP or timeout error)") from None

    locator_fingerprint = hashlib.sha256(locator_url.encode("utf-8")).hexdigest()
    resolved_source = ResolvedSource(
        source_info=SourceInfo(type="direct", uri=public_url),
        media_path=Path(PurePosixPath(parsed.path).name),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return RemoteTranscriptInput(
        content=bytes(content),
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
]
