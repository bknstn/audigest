"""Explicit OpenAI speech-to-text fallback using the official Python SDK."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from audigest.errors import MissingDependencyError, ProviderError
from audigest.models import TranscriptDraft, TranscriptSegment

MODEL = "gpt-transcribe"
BASE_URL = "https://api.openai.com/v1"
TIMEOUT_SECONDS = 60.0
MAX_FILE_SIZE = 25 * 1024 * 1024
SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"}
)

ClientFactory = Callable[..., Any]


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _language_code(value: Any) -> str | None:
    code = _field(value, "code")
    if not isinstance(code, str):
        return None
    cleaned = code.strip()
    if not cleaned or any(character.isspace() for character in cleaned):
        return None
    return cleaned


def _response_language(response: Any, requested_language: str | None) -> str | None:
    detected = _field(response, "languages")
    if isinstance(detected, (list, tuple)) and len(detected) == 1:
        detected_code = _language_code(detected[0])
        if detected_code is not None:
            return detected_code
    return requested_language


def _sanitized_error(error: Exception) -> ProviderError:
    error_name = type(error).__name__
    status = getattr(error, "status_code", None)

    if error_name in {"APITimeoutError", "TimeoutError"} or isinstance(error, TimeoutError):
        return ProviderError("OpenAI transcription timed out after 60 seconds.")
    if error_name in {"APIConnectionError", "ConnectError", "ConnectionError"} or isinstance(
        error, ConnectionError
    ):
        return ProviderError("Could not connect to the OpenAI API. Check the network and retry.")
    if error_name == "AuthenticationError" or status == 401:
        return ProviderError("OpenAI rejected OPENAI_API_KEY. Check the key and API access.")
    if error_name in {"PermissionDeniedError", "AuthorizationError"} or status == 403:
        return ProviderError("OpenAI denied this transcription request. Check API account access.")
    if error_name == "RateLimitError" or status == 429:
        return ProviderError(
            "OpenAI rate-limited the request or the API account has no available quota. "
            "Check account limits and billing."
        )
    if error_name in {"PayloadTooLargeError", "RequestTooLargeError"} or status == 413:
        return ProviderError("OpenAI audio uploads must be 25 MB or smaller.")
    if error_name in {"BadRequestError", "InvalidRequestError"} or status == 400:
        return ProviderError(
            "OpenAI could not process this audio file. Verify it is valid and in a supported "
            "format."
        )
    return ProviderError("OpenAI transcription failed. Check the local audio file and retry.")


class OpenAITranscriptionProvider:
    """Call the OpenAI transcription API only when explicitly selected as fallback."""

    name = "openai"
    model = MODEL

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory

    def _make_client(self, api_key: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(
                api_key=api_key,
                base_url=BASE_URL,
                timeout=TIMEOUT_SECONDS,
                max_retries=0,
            )
        try:
            from openai import OpenAI
        except ImportError:
            raise MissingDependencyError(
                "OpenAI transcription requires the optional SDK. Install audigest with "
                "the 'openai' extra (uv sync --extra openai)."
            ) from None
        return OpenAI(
            api_key=api_key,
            base_url=BASE_URL,
            timeout=TIMEOUT_SECONDS,
            max_retries=0,
        )

    def transcribe(self, media_path: Path, *, language: str | None) -> TranscriptDraft:
        """Transcribe one supported local audio file without modifying it."""

        if media_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
            raise ProviderError(
                "OpenAI transcription does not support this local media format. Supported file "
                f"extensions: {supported}."
            )
        try:
            file_size = media_path.stat().st_size
        except OSError:
            raise ProviderError(
                "Could not read the local audio file for OpenAI transcription."
            ) from None
        if file_size <= 0:
            raise ProviderError("OpenAI cannot transcribe an empty audio file.")
        if file_size > MAX_FILE_SIZE:
            raise ProviderError("OpenAI audio uploads must be 25 MB or smaller.")

        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key is None or not api_key.strip():
            raise ProviderError(
                "OpenAI fallback requires OPENAI_API_KEY in the environment. Set it or select "
                "another STT provider."
            )

        try:
            with self._make_client(api_key) as client:
                with media_path.open("rb") as audio_file:
                    request: dict[str, Any] = {
                        "model": self.model,
                        "file": audio_file,
                        "response_format": "json",
                    }
                    if language is not None:
                        request["extra_body"] = {"languages": [language]}
                    response = client.audio.transcriptions.create(**request)
        except (ProviderError, MissingDependencyError):
            raise
        except Exception as error:
            raise _sanitized_error(error) from None

        text = _field(response, "text")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("OpenAI returned an empty or invalid transcription response.")

        return TranscriptDraft(
            provider=self.name,
            model=self.model,
            language=_response_language(response, language),
            segments=[TranscriptSegment(text=text)],
        )


__all__ = ["OpenAITranscriptionProvider"]
