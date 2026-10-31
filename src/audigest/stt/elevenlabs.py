"""Explicit ElevenLabs Scribe speech-to-text fallback."""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from audigest.errors import MissingDependencyError, ProviderError
from audigest.models import TranscriptDraft, TranscriptSegment

MODEL = "scribe_v2"
BASE_URL = "https://api.elevenlabs.io"
TIMEOUT_SECONDS = 60.0
MAX_FILE_SIZE = 3_000_000_000
SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".aac",
        ".aif",
        ".aiff",
        ".avi",
        ".flac",
        ".m4a",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
    }
)
_LANGUAGE_CODE = re.compile(r"[A-Za-z]{2,3}\Z")

ClientFactory = Callable[..., Any]


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _response_language(response: Any, requested_language: str | None) -> str | None:
    language = _field(response, "language_code")
    if language is None:
        return requested_language
    if not isinstance(language, str) or _LANGUAGE_CODE.fullmatch(language) is None:
        raise ProviderError("ElevenLabs returned an invalid language code.")
    return language


def _sanitized_error(error: Exception) -> ProviderError:
    name = type(error).__name__
    status = getattr(error, "status_code", None)
    if name in {"APITimeoutError", "TimeoutError", "ReadTimeout", "ConnectTimeout"} or isinstance(
        error, TimeoutError
    ):
        return ProviderError("ElevenLabs transcription timed out after 60 seconds.")
    if name in {"APIConnectionError", "ConnectError", "ConnectionError"} or isinstance(
        error, ConnectionError
    ):
        return ProviderError(
            "Could not connect to the ElevenLabs API. Check the network and retry."
        )
    if name in {"AuthenticationError", "UnauthorizedError"} or status == 401:
        return ProviderError(
            "ElevenLabs rejected ELEVENLABS_API_KEY. Check the key and API access."
        )
    if name in {"PermissionDeniedError", "ForbiddenError"} or status == 403:
        return ProviderError(
            "ElevenLabs denied this transcription request. Check account access and permissions."
        )
    if name == "RateLimitError" or status == 429:
        return ProviderError("ElevenLabs rate-limited the request. Check account limits and retry.")
    if status == 413:
        return ProviderError("ElevenLabs speech-to-text uploads must be 3 GB or smaller.")
    if name in {"BadRequestError", "UnprocessableEntityError"} or status in {400, 422}:
        return ProviderError(
            "ElevenLabs could not process this media file. Verify it is valid and in a supported "
            "format."
        )
    return ProviderError("ElevenLabs transcription failed. Check the local media file and retry.")


def _make_httpx_client() -> Any:
    """Create a fixed-timeout client with redirects and transport retries disabled."""

    import httpx

    return httpx.Client(
        transport=httpx.HTTPTransport(retries=0),
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
    )


class ElevenLabsTranscriptionProvider:
    """Call Scribe v2 only when explicitly selected as an STT fallback."""

    name = "elevenlabs"
    model = MODEL

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory

    def _make_client(self, api_key: str) -> tuple[Any, Any]:
        sdk_client: Any | None = None
        if self._client_factory is None:
            try:
                elevenlabs_module = importlib.import_module("elevenlabs.client")
                sdk_client = elevenlabs_module.ElevenLabs
            except ImportError:
                raise MissingDependencyError(
                    "ElevenLabs transcription requires the optional SDK. Install audigest with "
                    "the 'elevenlabs' extra (uv sync --extra elevenlabs)."
                ) from None

        try:
            httpx_client = _make_httpx_client()
        except ImportError:
            raise MissingDependencyError(
                "ElevenLabs transcription requires the optional SDK. Install audigest with "
                "the 'elevenlabs' extra (uv sync --extra elevenlabs)."
            ) from None

        try:
            options = {
                "api_key": api_key,
                "base_url": BASE_URL,
                "timeout": TIMEOUT_SECONDS,
                "follow_redirects": False,
                "httpx_client": httpx_client,
            }
            if self._client_factory is not None:
                client = self._client_factory(**options)
            else:
                assert sdk_client is not None
                client = sdk_client(**options)
        except Exception:
            httpx_client.close()
            raise
        return client, httpx_client

    def transcribe(self, media_path: Path, *, language: str | None) -> TranscriptDraft:
        """Transcribe one supported local media file without modifying it."""

        if media_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
            raise ProviderError(
                "ElevenLabs transcription does not support this local media format. Supported "
                f"file extensions: {supported}."
            )
        try:
            file_size = media_path.stat().st_size
        except OSError:
            raise ProviderError(
                "Could not read the local media file for ElevenLabs transcription."
            ) from None
        if file_size <= 0:
            raise ProviderError("ElevenLabs cannot transcribe an empty media file.")
        if file_size > MAX_FILE_SIZE:
            raise ProviderError("ElevenLabs speech-to-text uploads must be 3 GB or smaller.")
        if language is not None and _LANGUAGE_CODE.fullmatch(language) is None:
            raise ProviderError("ElevenLabs language hints must be ISO 639-1 or ISO 639-3 codes.")

        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if api_key is None or not api_key.strip():
            raise ProviderError(
                "ElevenLabs fallback requires ELEVENLABS_API_KEY in the environment. Set it or "
                "select another STT provider."
            )

        httpx_client = None
        try:
            client, httpx_client = self._make_client(api_key)
            with media_path.open("rb") as audio_file:
                response = client.speech_to_text.convert(
                    file=audio_file,
                    model_id=self.model,
                    language_code=language,
                    timestamps_granularity="none",
                    diarize=False,
                    tag_audio_events=False,
                    webhook=False,
                    request_options={
                        "max_retries": 0,
                        "timeout_in_seconds": int(TIMEOUT_SECONDS),
                    },
                )
        except (ProviderError, MissingDependencyError):
            raise
        except Exception as error:
            raise _sanitized_error(error) from None
        finally:
            if httpx_client is not None:
                try:
                    httpx_client.close()
                except Exception:
                    pass

        text = _field(response, "text")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("ElevenLabs returned an empty or invalid transcription response.")

        return TranscriptDraft(
            provider=self.name,
            model=self.model,
            language=_response_language(response, language),
            segments=[TranscriptSegment(text=text)],
        )


__all__ = ["ElevenLabsTranscriptionProvider", "SUPPORTED_AUDIO_EXTENSIONS"]
