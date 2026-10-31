"""Offline tests for the optional ElevenLabs Scribe adapter."""

import builtins
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from audigest.errors import MissingDependencyError, ProviderError
from audigest.stt.elevenlabs import (
    BASE_URL,
    MAX_FILE_SIZE,
    MODEL,
    SUPPORTED_AUDIO_EXTENSIONS,
    TIMEOUT_SECONDS,
    ElevenLabsTranscriptionProvider,
    _make_httpx_client,
)

SYNTHETIC_KEY = "synthetic-elevenlabs-test-key-never-valid"


class MockHttpxClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class MockSpeechToText:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.request: dict[str, Any] | None = None
        self.uploaded_bytes: bytes | None = None

    def convert(self, **request: Any) -> Any:
        self.request = request
        self.uploaded_bytes = request["file"].read()
        if self.error is not None:
            raise self.error
        return self.response


class MockClient:
    def __init__(self, speech_to_text: MockSpeechToText) -> None:
        self.speech_to_text = speech_to_text


def configure_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    speech_to_text: MockSpeechToText,
    httpx_client: MockHttpxClient,
) -> Callable[..., MockClient]:

    def factory(**_options: Any) -> MockClient:
        return MockClient(speech_to_text)

    monkeypatch.setattr("audigest.stt.elevenlabs._make_httpx_client", lambda: httpx_client)
    return factory


def test_request_uses_bounded_scribe_v2_and_normalizes_untimed_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "synthetic.wav"
    original = b"synthetic local audio bytes"
    media_path.write_bytes(original)
    httpx_client = MockHttpxClient()
    stt = MockSpeechToText(response={"text": "Bonjour, monde.", "language_code": "fr", "words": []})
    captured: dict[str, Any] = {}

    def factory(**options: Any) -> MockClient:
        captured.update(options)
        return MockClient(stt)

    monkeypatch.setattr("audigest.stt.elevenlabs._make_httpx_client", lambda: httpx_client)
    draft = ElevenLabsTranscriptionProvider(client_factory=factory).transcribe(
        media_path,
        language="en",
    )

    assert captured == {
        "api_key": SYNTHETIC_KEY,
        "base_url": BASE_URL,
        "timeout": TIMEOUT_SECONDS,
        "follow_redirects": False,
        "httpx_client": httpx_client,
    }
    assert stt.request is not None
    assert stt.request["model_id"] == MODEL == "scribe_v2"
    assert stt.request["language_code"] == "en"
    assert stt.request["timestamps_granularity"] == "none"
    assert stt.request["diarize"] is False
    assert stt.request["tag_audio_events"] is False
    assert stt.request["webhook"] is False
    assert stt.request["request_options"] == {
        "max_retries": 0,
        "timeout_in_seconds": 60,
    }
    assert stt.uploaded_bytes == original
    assert media_path.read_bytes() == original
    assert httpx_client.closed
    assert draft.provider == "elevenlabs"
    assert draft.model == MODEL
    assert draft.language == "fr"
    assert len(draft.segments) == 1
    assert draft.segments[0].text == "Bonjour, monde."
    assert draft.segments[0].start is None
    assert draft.segments[0].end is None


@pytest.mark.parametrize(
    ("detected_language", "requested_language", "expected_language"),
    [(None, "pl", "pl"), (None, None, None), ("es", None, "es")],
)
def test_language_uses_detected_code_or_requested_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    detected_language: str | None,
    requested_language: str | None,
    expected_language: str | None,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "language.wav"
    media_path.write_bytes(b"synthetic")
    stt = MockSpeechToText(response={"text": "Synthetic text.", "language_code": detected_language})
    httpx_client = MockHttpxClient()
    factory = configure_mock_client(monkeypatch, stt, httpx_client)
    draft = ElevenLabsTranscriptionProvider(client_factory=factory).transcribe(
        media_path,
        language=requested_language,
    )

    assert draft.language == expected_language
    assert httpx_client.closed


def test_missing_api_key_fails_before_client_or_sdk_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    media_path = tmp_path / "missing-key.wav"
    media_path.write_bytes(b"synthetic")

    def unexpected_client(**_: Any) -> Any:
        pytest.fail("client must not be created without an explicit API key")

    with pytest.raises(ProviderError, match="ELEVENLABS_API_KEY") as error:
        ElevenLabsTranscriptionProvider(client_factory=unexpected_client).transcribe(
            media_path,
            language=None,
        )
    assert error.value.exit_code == 5


def test_missing_optional_sdk_fails_only_when_provider_is_used(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "missing-sdk.wav"
    media_path.write_bytes(b"synthetic")
    real_import = builtins.__import__

    def import_without_elevenlabs(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "elevenlabs.client":
            raise ModuleNotFoundError("synthetic missing SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_elevenlabs)

    with pytest.raises(MissingDependencyError, match="optional SDK"):
        ElevenLabsTranscriptionProvider().transcribe(media_path, language=None)
    assert MissingDependencyError.exit_code == 7


@pytest.mark.parametrize(
    ("error_name", "status_code", "expected_message"),
    [
        ("AuthenticationError", 401, "rejected ELEVENLABS_API_KEY"),
        ("RateLimitError", 429, "rate-limited the request"),
        ("APITimeoutError", None, "timed out after 60 seconds"),
        ("BadRequestError", 400, "could not process this media file"),
        ("SDKError", None, "ElevenLabs transcription failed"),
    ],
)
def test_sdk_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_name: str,
    status_code: int | None,
    expected_message: str,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "failure.wav"
    media_path.write_bytes(b"synthetic")
    error_type = type(error_name, (Exception,), {"status_code": status_code})
    secret_error = error_type(f"private response with {SYNTHETIC_KEY}")
    httpx_client = MockHttpxClient()
    factory = configure_mock_client(
        monkeypatch,
        MockSpeechToText(error=secret_error),
        httpx_client,
    )

    with pytest.raises(ProviderError) as raised:
        ElevenLabsTranscriptionProvider(client_factory=factory).transcribe(
            media_path,
            language=None,
        )

    assert expected_message in str(raised.value)
    assert SYNTHETIC_KEY not in str(raised.value)
    assert "private response" not in str(raised.value)
    assert httpx_client.closed


@pytest.mark.parametrize(
    ("filename", "size", "expected_message"),
    [
        ("unsupported.caf", 10, "Supported file extensions"),
        ("too-large.wav", MAX_FILE_SIZE + 1, "3 GB or smaller"),
    ],
)
def test_unsupported_formats_and_large_files_fail_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
    size: int,
    expected_message: str,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / filename
    with media_path.open("wb") as output:
        output.seek(size - 1)
        output.write(b"\0")

    def unexpected_client(**_: Any) -> Any:
        pytest.fail("invalid uploads must be rejected locally")

    with pytest.raises(ProviderError, match=expected_message):
        ElevenLabsTranscriptionProvider(client_factory=unexpected_client).transcribe(
            media_path,
            language=None,
        )


@pytest.mark.parametrize("response", [{"text": ""}, {"text": "  "}, {}])
def test_empty_or_invalid_responses_fail_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: dict[str, str],
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "empty-response.wav"
    media_path.write_bytes(b"synthetic")
    httpx_client = MockHttpxClient()
    factory = configure_mock_client(
        monkeypatch,
        MockSpeechToText(response=response),
        httpx_client,
    )

    with pytest.raises(ProviderError, match="empty or invalid transcription response"):
        ElevenLabsTranscriptionProvider(client_factory=factory).transcribe(
            media_path,
            language=None,
        )
    assert httpx_client.closed


def test_invalid_requested_language_is_rejected_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "invalid-language.wav"
    media_path.write_bytes(b"synthetic")

    def unexpected_client(**_: Any) -> Any:
        pytest.fail("invalid language hints must be rejected locally")

    with pytest.raises(ProviderError, match="ISO 639-1 or ISO 639-3"):
        ElevenLabsTranscriptionProvider(client_factory=unexpected_client).transcribe(
            media_path,
            language="en-US",
        )


def test_httpx_transport_has_no_retries_and_redirects_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpx = ModuleType("httpx")
    transport_options: dict[str, Any] = {}
    client_options: dict[str, Any] = {}

    class Transport:
        def __init__(self, **options: Any) -> None:
            transport_options.update(options)

    def make_client(**options: Any) -> object:
        client_options.update(options)
        return object()

    httpx.HTTPTransport = Transport  # type: ignore[attr-defined]
    httpx.Client = make_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", httpx)

    _make_httpx_client()

    assert transport_options == {"retries": 0}
    assert client_options["timeout"] == TIMEOUT_SECONDS
    assert client_options["follow_redirects"] is False
    assert isinstance(client_options["transport"], Transport)
    assert ".wav" in SUPPORTED_AUDIO_EXTENSIONS
