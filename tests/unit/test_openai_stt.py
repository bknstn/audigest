"""Offline tests for the optional OpenAI transcription adapter."""

import builtins
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from audigest.errors import MissingDependencyError, ProviderError
from audigest.stt.openai import (
    BASE_URL,
    MAX_FILE_SIZE,
    MODEL,
    TIMEOUT_SECONDS,
    OpenAITranscriptionProvider,
)

SYNTHETIC_KEY = "synthetic-test-key-never-valid"


class MockTranscriptions:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.request: dict[str, Any] | None = None
        self.uploaded_bytes: bytes | None = None

    def create(self, **request: Any) -> Any:
        self.request = request
        self.uploaded_bytes = request["file"].read()
        if self.error is not None:
            raise self.error
        return self.response


class MockClient:
    def __init__(self, transcriptions: MockTranscriptions) -> None:
        self.audio = SimpleNamespace(transcriptions=transcriptions)
        self.closed = False

    def __enter__(self) -> "MockClient":
        return self

    def __exit__(self, *args: Any) -> None:
        _ = args
        self.closed = True


def test_openai_request_is_bounded_explicit_and_normalizes_untimed_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "synthetic.wav"
    media_path.write_bytes(b"synthetic audio bytes")
    transcriptions = MockTranscriptions(
        response={"text": "Bonjour.", "languages": [{"code": "fr"}]}
    )
    client = MockClient(transcriptions)
    client_options: dict[str, Any] = {}

    def make_client(**options: Any) -> MockClient:
        client_options.update(options)
        return client

    draft = OpenAITranscriptionProvider(client_factory=make_client).transcribe(
        media_path,
        language="en",
    )

    assert client_options == {
        "api_key": SYNTHETIC_KEY,
        "base_url": BASE_URL,
        "timeout": TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    assert transcriptions.request is not None
    assert transcriptions.request["model"] == MODEL == "gpt-transcribe"
    assert transcriptions.request["response_format"] == "json"
    assert transcriptions.request["extra_body"] == {"languages": ["en"]}
    assert "language" not in transcriptions.request
    assert transcriptions.uploaded_bytes == b"synthetic audio bytes"
    assert client.closed
    assert draft.provider == "openai"
    assert draft.model == MODEL
    assert draft.language == "fr"
    assert len(draft.segments) == 1
    assert draft.segments[0].text == "Bonjour."
    assert draft.segments[0].start is None
    assert draft.segments[0].end is None


@pytest.mark.parametrize(
    ("languages", "requested_language", "expected_language"),
    [
        ([], "pl", "pl"),
        ([], None, None),
        ([{"code": "es"}], None, "es"),
        ([{"code": "fr"}, {"code": "de"}], None, None),
    ],
)
def test_response_language_uses_one_detected_code_or_a_user_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    languages: list[dict[str, str]],
    requested_language: str | None,
    expected_language: str | None,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "language.wav"
    media_path.write_bytes(b"synthetic")
    client = MockClient(
        MockTranscriptions(response={"text": "Synthetic words.", "languages": languages})
    )

    draft = OpenAITranscriptionProvider(client_factory=lambda **_: client).transcribe(
        media_path,
        language=requested_language,
    )

    assert draft.language == expected_language


def test_missing_api_key_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    media_path = tmp_path / "missing-key.wav"
    media_path.write_bytes(b"synthetic")

    def unexpected_client(**_: Any) -> Any:
        pytest.fail("client must not be created without an explicit API key")

    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        OpenAITranscriptionProvider(client_factory=unexpected_client).transcribe(
            media_path,
            language=None,
        )


def test_optional_sdk_is_imported_only_on_explicit_provider_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "missing-sdk.wav"
    media_path.write_bytes(b"synthetic")
    real_import = builtins.__import__

    def import_without_openai(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "openai":
            raise ModuleNotFoundError("synthetic missing SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_openai)

    with pytest.raises(MissingDependencyError, match="optional SDK"):
        OpenAITranscriptionProvider().transcribe(media_path, language=None)


@pytest.mark.parametrize(
    ("error_name", "status_code", "expected_message"),
    [
        ("AuthenticationError", 401, "rejected OPENAI_API_KEY"),
        ("PermissionDeniedError", 403, "denied this transcription request"),
        ("RateLimitError", 429, "rate-limited the request"),
        ("APITimeoutError", None, "timed out after 60 seconds"),
        ("APIConnectionError", None, "Could not connect to the OpenAI API"),
        ("BadRequestError", 400, "could not process this audio file"),
        ("PayloadTooLargeError", 413, "25 MB or smaller"),
    ],
)
def test_sdk_errors_are_actionable_without_exposing_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_name: str,
    status_code: int | None,
    expected_message: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "failure.wav"
    media_path.write_bytes(b"synthetic")
    error_type = type(error_name, (Exception,), {"status_code": status_code})
    private_error = error_type("synthetic-secret-from-sdk-exception")
    client = MockClient(MockTranscriptions(error=private_error))

    with pytest.raises(ProviderError) as raised:
        OpenAITranscriptionProvider(client_factory=lambda **_: client).transcribe(
            media_path,
            language=None,
        )

    assert expected_message in str(raised.value)
    assert "synthetic-secret-from-sdk-exception" not in str(raised.value)
    assert SYNTHETIC_KEY not in str(raised.value)
    assert client.closed


@pytest.mark.parametrize(
    ("filename", "size", "expected_message"),
    [
        ("unsupported.mkv", 10, "Supported file extensions"),
        ("too-large.wav", MAX_FILE_SIZE + 1, "25 MB or smaller"),
    ],
)
def test_unsupported_formats_and_large_files_fail_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
    size: int,
    expected_message: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / filename
    with media_path.open("wb") as output:
        output.seek(size - 1)
        output.write(b"\0")

    def unexpected_client(**_: Any) -> Any:
        pytest.fail("invalid uploads must be rejected locally")

    with pytest.raises(ProviderError, match=expected_message):
        OpenAITranscriptionProvider(client_factory=unexpected_client).transcribe(
            media_path,
            language=None,
        )


@pytest.mark.parametrize("response", [{"text": ""}, {"text": "  "}, {}])
def test_empty_or_invalid_responses_fail_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: dict[str, str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    media_path = tmp_path / "empty-response.wav"
    media_path.write_bytes(b"synthetic")
    client = MockClient(MockTranscriptions(response=response))

    with pytest.raises(ProviderError, match="empty or invalid transcription response"):
        OpenAITranscriptionProvider(client_factory=lambda **_: client).transcribe(
            media_path,
            language=None,
        )
