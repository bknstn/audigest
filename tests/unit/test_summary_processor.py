"""Offline tests for structured summary content and the OpenAI boundary."""

import importlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from audigest.errors import MissingDependencyError, ProcessorError
from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.processors.openai import (
    BASE_URL,
    DEFAULT_MODEL,
    TIMEOUT_SECONDS,
    OpenAISummaryProvider,
)
from audigest.processors.summary import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_TOKENS,
    REASONING_EFFORT,
    SummaryPointDraft,
    SummaryResponse,
    canonical_key_points,
    summary_content_sha256,
    summary_id_for,
    summary_input_json,
)

SYNTHETIC_KEY = "synthetic-summary-key-never-valid"


def make_transcript(
    *,
    title: str | None = "Synthetic title",
    segments: list[TranscriptSegment] | None = None,
) -> Transcript:
    selected_segments = (
        segments
        if segments is not None
        else [
            TranscriptSegment(
                start=1.0,
                end=2.5,
                speaker="Speaker A",
                text="The synthetic system keeps a local copy.",
            ),
            TranscriptSegment(text="A second untimed segment."),
        ]
    )
    return Transcript(
        id="a" * 64,
        source=SourceInfo(type="local", uri="file:///private/path/source.wav", title=title),
        provider="fake",
        model="fake-v1",
        language="en",
        created_at=datetime(2026, 10, 31, tzinfo=UTC),
        segments=selected_segments,
        text="\n".join(segment.text for segment in selected_segments),
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


def test_model_input_excludes_private_source_metadata_and_hash_covers_citation_context() -> None:
    transcript = make_transcript()
    request = json.loads(summary_input_json(transcript))

    assert request == {
        "title": "Synthetic title",
        "segments": [
            {"segment_index": 0, "text": "The synthetic system keeps a local copy."},
            {"segment_index": 1, "text": "A second untimed segment."},
        ],
    }
    assert "private/path" not in summary_input_json(transcript)
    assert "uri" not in request

    changed_timing = make_transcript(
        segments=[
            TranscriptSegment(
                start=1.1,
                end=2.5,
                speaker="Speaker A",
                text="The synthetic system keeps a local copy.",
            ),
            TranscriptSegment(text="A second untimed segment."),
        ]
    )
    assert summary_content_sha256(transcript) != summary_content_sha256(changed_timing)
    assert summary_id_for(transcript, "gpt-6-luna") != summary_id_for(changed_timing, "gpt-6-luna")
    assert summary_id_for(transcript, "gpt-6-luna") != summary_id_for(transcript, "other-model")


def test_citations_copy_only_canonical_timing_and_speaker() -> None:
    transcript = make_transcript()
    response = SummaryResponse(
        summary="A short summary.",
        key_points=[
            SummaryPointDraft(
                point="The system keeps data locally.",
                segment_index=0,
                quote="keeps a local copy",
            ),
            SummaryPointDraft(
                point="Another untimed fact.",
                segment_index=1,
                quote="untimed segment",
            ),
        ],
    )

    key_points = canonical_key_points(response, transcript.segments)

    assert key_points[0].citation.start == 1.0
    assert key_points[0].citation.end == 2.5
    assert key_points[0].citation.speaker == "Speaker A"
    assert key_points[1].citation.start is None
    assert key_points[1].citation.end is None
    assert key_points[1].citation.speaker is None


@pytest.mark.parametrize(
    ("segment_index", "quote", "expected"),
    [
        (3, "not here", "outside the transcript segment range"),
        (0, "invented quote", "does not match its cited segment"),
    ],
)
def test_invalid_citations_fail_without_changing_transcript(
    segment_index: int,
    quote: str,
    expected: str,
) -> None:
    transcript = make_transcript()
    before = transcript.model_dump(mode="json")
    response = SummaryResponse(
        summary="A short summary.",
        key_points=[SummaryPointDraft(point="A point.", segment_index=segment_index, quote=quote)],
    )

    with pytest.raises(ProcessorError, match=expected) as error:
        canonical_key_points(response, transcript.segments)

    assert error.value.exit_code == 6
    assert transcript.model_dump(mode="json") == before


def test_openai_request_uses_fixed_bounded_structured_responses_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    response_body = SummaryResponse(
        summary="The transcript describes a local-first system.",
        key_points=[
            SummaryPointDraft(
                point="It keeps output local.",
                segment_index=0,
                quote="keeps a local copy",
            )
        ],
    )
    responses = MockResponses(SimpleNamespace(status="completed", output_parsed=response_body))
    client = MockClient(responses)
    client_options: dict[str, Any] = {}

    def factory(**options: Any) -> MockClient:
        client_options.update(options)
        return client

    result = OpenAISummaryProvider(DEFAULT_MODEL, client_factory=factory).summarize(
        make_transcript()
    )

    assert result == response_body
    assert client_options == {
        "api_key": SYNTHETIC_KEY,
        "base_url": BASE_URL,
        "timeout": TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    assert responses.request is not None
    assert responses.request["model"] == "gpt-6-luna"
    assert responses.request["store"] is False
    assert responses.request["max_output_tokens"] == MAX_OUTPUT_TOKENS == 6000
    assert responses.request["reasoning"] == {"effort": REASONING_EFFORT}
    assert responses.request["tools"] == []
    assert responses.request["text_format"] is SummaryResponse
    assert "untrusted data" in responses.request["instructions"]
    request_text = responses.request["input"]
    assert "file:///private/path" not in request_text
    assert json.loads(request_text)["segments"][0]["segment_index"] == 0
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
            "refused to summarize",
        ),
        (SimpleNamespace(status="completed", output_parsed=None), None, "no complete structured"),
        (None, RuntimeError(f"private response {SYNTHETIC_KEY}"), "summarization failed"),
    ],
)
def test_openai_failures_are_sanitized_and_categorized(
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
    error: Exception | None,
    message: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    client = MockClient(MockResponses(response, error))
    provider = OpenAISummaryProvider(
        DEFAULT_MODEL,
        client_factory=lambda **_options: client,
    )

    with pytest.raises(ProcessorError, match=message) as raised:
        provider.summarize(make_transcript())

    assert raised.value.exit_code == 6
    assert SYNTHETIC_KEY not in str(raised.value)
    assert client.closed


def test_missing_key_and_oversized_input_fail_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAISummaryProvider(
        DEFAULT_MODEL,
        client_factory=lambda **_options: pytest.fail("missing key must fail before SDK setup"),
    )
    with pytest.raises(ProcessorError, match="OPENAI_API_KEY") as missing_key:
        provider.summarize(make_transcript())
    assert missing_key.value.exit_code == 6

    large_transcript = make_transcript(segments=[TranscriptSegment(text="x" * MAX_INPUT_BYTES)])
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    with pytest.raises(ProcessorError, match="maximum input is 256 KiB") as oversized:
        provider.summarize(large_transcript)
    assert oversized.value.exit_code == 6


@pytest.mark.parametrize(
    "segments",
    [[], [TranscriptSegment(text=" \n\t ")]],
)
def test_empty_or_whitespace_transcript_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    segments: list[TranscriptSegment],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    provider = OpenAISummaryProvider(
        DEFAULT_MODEL,
        client_factory=lambda **_options: pytest.fail("empty text must fail before API setup"),
    )

    with pytest.raises(ProcessorError, match="no non-whitespace text") as error:
        provider.summarize(make_transcript(segments=segments))

    assert error.value.exit_code == 6


def test_missing_optional_openai_sdk_is_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)

    def no_sdk(name: str) -> Any:
        assert name == "openai"
        raise ModuleNotFoundError("synthetic missing optional SDK")

    monkeypatch.setattr(importlib, "import_module", no_sdk)
    with pytest.raises(MissingDependencyError, match="openai.*extra") as error:
        OpenAISummaryProvider(DEFAULT_MODEL).summarize(make_transcript())
    assert error.value.exit_code == 7
