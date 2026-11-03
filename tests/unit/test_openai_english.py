"""Offline tests for the fixed OpenAI English-learning Responses boundary."""

import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from audigest.errors import MissingDependencyError, ProcessorError
from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.processors.english import MAX_INPUT_BYTES, EnglishItemDraft, EnglishResponse
from audigest.processors.openai_english import (
    BASE_URL,
    DEFAULT_MODEL,
    MAX_OUTPUT_TOKENS,
    REASONING_EFFORT,
    TIMEOUT_SECONDS,
    OpenAIEnglishProvider,
)

SYNTHETIC_KEY = "synthetic-english-key-never-valid"


def make_transcript(segment_text: str = "I put off the decision until Monday.") -> Transcript:
    segment = TranscriptSegment(start=0.5, end=2.0, text=segment_text)
    return Transcript(
        id="d" * 64,
        source=SourceInfo(type="local", uri="file:///private/english-source.txt", title="A title"),
        provider="fake",
        language="fr",
        created_at="2026-11-03T00:00:00Z",
        segments=[segment],
        text=segment.text,
    )


def make_response() -> EnglishResponse:
    return EnglishResponse(
        overview="A phrasal verb appears in the transcript.",
        items=[
            EnglishItemDraft(
                kind="phrasal_verb",
                expression="put off",
                segment_index=0,
                quote="I put off the decision until Monday.",
                meaning="To delay doing something.",
                usage_note="It can be followed by an event or task.",
                examples=["They put off the launch until spring."],
            )
        ],
    )


class MockResponses:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.request: dict[str, Any] | None = None

    def parse(self, **request: Any) -> Any:
        self.request = request
        if self.error is not None:
            raise self.error
        return self.response


class MockClient:
    def __init__(self, responses: MockResponses) -> None:
        self.responses = responses
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_openai_request_uses_fixed_structured_options_and_only_public_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    response_body = make_response()
    responses = MockResponses(SimpleNamespace(status="completed", output_parsed=response_body))
    client = MockClient(responses)
    client_options: dict[str, Any] = {}

    def factory(**options: Any) -> MockClient:
        client_options.update(options)
        return client

    result = OpenAIEnglishProvider(DEFAULT_MODEL, client_factory=factory).extract(
        make_transcript(), "B2"
    )

    assert result == response_body
    assert client_options == {
        "api_key": SYNTHETIC_KEY,
        "base_url": BASE_URL,
        "timeout": TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    assert responses.request is not None
    request = responses.request
    assert request["model"] == "gpt-6-luna"
    assert request["store"] is False
    assert request["max_output_tokens"] == MAX_OUTPUT_TOKENS == 8000
    assert request["reasoning"] == {"effort": REASONING_EFFORT} == {"effort": "low"}
    assert request["tools"] == []
    assert request["text_format"] is EnglishResponse
    assert "untrusted data" in request["instructions"]
    assert "zero items" in request["instructions"]
    assert "file:///private" not in request["input"]
    assert "language" not in json.loads(request["input"])
    assert json.loads(request["input"])["level"] == "B2"
    assert client.closed


@pytest.mark.parametrize(
    ("response", "error", "message"),
    [
        (SimpleNamespace(status="incomplete", output_parsed=None), None, "did not complete"),
        (
            SimpleNamespace(
                status="completed",
                output_parsed=None,
                output=[
                    SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="private")])
                ],
            ),
            None,
            "refused to process",
        ),
        (SimpleNamespace(status="completed", output_parsed=None), None, "no complete structured"),
        (None, RuntimeError(f"private provider data {SYNTHETIC_KEY}"), "processing failed"),
    ],
)
def test_provider_failures_are_sanitized(
    response: Any,
    error: Exception | None,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    client = MockClient(MockResponses(response, error))
    provider = OpenAIEnglishProvider(DEFAULT_MODEL, client_factory=lambda **_options: client)

    with pytest.raises(ProcessorError, match=message) as raised:
        provider.extract(make_transcript(), "B2")

    assert raised.value.exit_code == 6
    assert SYNTHETIC_KEY not in str(raised.value)
    assert "private provider data" not in str(raised.value)
    assert client.closed


@pytest.mark.parametrize(
    "segment_text",
    ["", " \t\n "],
)
def test_empty_text_fails_before_client_creation(
    segment_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    provider = OpenAIEnglishProvider(
        DEFAULT_MODEL,
        client_factory=lambda **_options: pytest.fail("empty input must not create a client"),
    )

    with pytest.raises(ProcessorError, match="no non-whitespace text") as error:
        provider.extract(make_transcript(segment_text), "B2")
    assert error.value.exit_code == 6


def test_missing_key_and_oversized_input_fail_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIEnglishProvider(
        DEFAULT_MODEL,
        client_factory=lambda **_options: pytest.fail("missing key must fail before SDK setup"),
    )
    with pytest.raises(ProcessorError, match="OPENAI_API_KEY") as missing_key:
        provider.extract(make_transcript(), "B2")
    assert missing_key.value.exit_code == 6

    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    oversized = make_transcript("x" * MAX_INPUT_BYTES)
    with pytest.raises(ProcessorError, match="maximum input is 256 KiB") as too_large:
        provider.extract(oversized, "B2")
    assert too_large.value.exit_code == 6


def test_missing_optional_sdk_is_a_dependency_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)

    def no_sdk(name: str) -> Any:
        assert name == "openai"
        raise ModuleNotFoundError("synthetic missing optional SDK")

    monkeypatch.setattr(importlib, "import_module", no_sdk)
    with pytest.raises(MissingDependencyError, match="openai.*extra") as error:
        OpenAIEnglishProvider(DEFAULT_MODEL).extract(make_transcript(), "B2")
    assert error.value.exit_code == 7
