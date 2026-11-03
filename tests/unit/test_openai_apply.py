"""Offline tests for the structured OpenAI application provider boundary."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from audigest.errors import MissingDependencyError, ProcessorError
from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.processors.application import (
    MAX_INPUT_BYTES,
    ApplicationResponse,
)
from audigest.processors.openai import BASE_URL, DEFAULT_MODEL, TIMEOUT_SECONDS
from audigest.processors.openai_apply import OpenAIApplicationProvider
from audigest.processors.project_context import ProjectContext, read_project_context
from audigest.processors.summary import MAX_OUTPUT_TOKENS, REASONING_EFFORT

SYNTHETIC_KEY = "synthetic-apply-key-never-valid"


def make_transcript(text: str = "A synthetic transcript idea.") -> Transcript:
    return Transcript(
        id="c" * 64,
        source=SourceInfo(type="local", uri="file:///private/path/source.txt", title="Synthetic"),
        provider="fixture",
        model="fixture-v1",
        language="en",
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        segments=[TranscriptSegment(text=text)],
        text=text,
    )


def make_project(tmp_path: Path) -> ProjectContext:
    project = tmp_path / "private-project-root"
    project.mkdir()
    (project / "README.md").write_text("Synthetic selected project text.", encoding="utf-8")
    return read_project_context(project)


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


def test_request_uses_bounded_structured_responses_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    expected = ApplicationResponse(
        overview="No project-specific suggestions are needed.", recommendations=[]
    )
    responses = MockResponses(SimpleNamespace(status="completed", output_parsed=expected))
    client = MockClient(responses)
    client_options: dict[str, Any] = {}

    def factory(**options: Any) -> MockClient:
        client_options.update(options)
        return client

    result = OpenAIApplicationProvider(DEFAULT_MODEL, client_factory=factory).apply(
        make_transcript(),
        make_project(tmp_path),
    )

    assert result == expected
    assert client_options == {
        "api_key": SYNTHETIC_KEY,
        "base_url": BASE_URL,
        "timeout": TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    assert responses.request is not None
    assert responses.request["model"] == "gpt-6-luna"
    assert responses.request["text_format"] is ApplicationResponse
    assert responses.request["reasoning"] == {"effort": REASONING_EFFORT}
    assert responses.request["max_output_tokens"] == MAX_OUTPUT_TOKENS == 6000
    assert responses.request["store"] is False
    assert responses.request["tools"] == []
    assert "untrusted data" in responses.request["instructions"]
    assert "zero to six recommendations" in responses.request["instructions"]
    request_payload = json.loads(responses.request["input"])
    assert request_payload["project_files"] == [
        {"relative_path": "README.md", "text": "Synthetic selected project text."}
    ]
    assert "private-project-root" not in responses.request["input"]
    assert "file:///private/path" not in responses.request["input"]
    assert client.closed


def test_oversized_combined_input_fails_before_client_or_key_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIApplicationProvider(
        DEFAULT_MODEL,
        client_factory=lambda **_options: pytest.fail("oversized input must not create a client"),
    )

    with pytest.raises(ProcessorError, match="maximum serialized input is 256 KiB") as error:
        provider.apply(make_transcript("x" * MAX_INPUT_BYTES), make_project(tmp_path))

    assert error.value.exit_code == 6


def test_missing_key_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIApplicationProvider(
        DEFAULT_MODEL,
        client_factory=lambda **_options: pytest.fail("missing key must fail before SDK setup"),
    )

    with pytest.raises(ProcessorError, match="OPENAI_API_KEY") as error:
        provider.apply(make_transcript(), make_project(tmp_path))

    assert error.value.exit_code == 6


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
            "refused to apply",
        ),
        (SimpleNamespace(status="completed", output_parsed=None), None, "no complete structured"),
        (None, RuntimeError(f"private project response {SYNTHETIC_KEY}"), "apply failed"),
    ],
)
def test_provider_failures_are_sanitized_and_client_is_closed(
    response: Any,
    error: Exception | None,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)
    client = MockClient(MockResponses(response, error))
    provider = OpenAIApplicationProvider(DEFAULT_MODEL, client_factory=lambda **_options: client)

    with pytest.raises(ProcessorError, match=message) as raised:
        provider.apply(make_transcript(), make_project(tmp_path))

    assert raised.value.exit_code == 6
    assert SYNTHETIC_KEY not in str(raised.value)
    assert "private project response" not in str(raised.value)
    assert client.closed


def test_missing_optional_sdk_is_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SYNTHETIC_KEY)

    def no_sdk(name: str) -> Any:
        assert name == "openai"
        raise ModuleNotFoundError("synthetic missing SDK")

    monkeypatch.setattr(importlib, "import_module", no_sdk)
    with pytest.raises(MissingDependencyError, match="openai.*extra") as error:
        OpenAIApplicationProvider(DEFAULT_MODEL).apply(make_transcript(), make_project(tmp_path))
    assert error.value.exit_code == 7
