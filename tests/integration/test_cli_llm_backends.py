"""Exercise the processor CLI against each real SDK with an offline HTTP transport."""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.processors.llm import ANTHROPIC_BASE_URL, OPENAI_BASE_URL
from audigest.processors.llm_config import DEFAULT_MODELS, LEGACY_OPENAI_MODEL

_SENTENCE = "We can roll out changes in small batches."
_KEY = "synthetic-test-key"
_RESPONSES = {
    "summarize": {
        "summary": "The speaker recommends a small rollout.",
        "key_points": [{"point": "Roll out gradually.", "segment_index": 0, "quote": _SENTENCE}],
    },
    "apply": {
        "overview": "Release gradually.",
        "recommendations": [
            {
                "idea": "Stage the rollout.",
                "relevance": "The project publishes weekly.",
                "suggested_action": "Try a small batch.",
                "transcript_evidence": {"segment_index": 0, "quote": _SENTENCE},
                "project_evidence": {"relative_path": "README.md", "quote": "Weekly release."},
            }
        ],
    },
    "english": {
        "overview": "Phrasal verb for releases.",
        "items": [
            {
                "kind": "phrasal_verb",
                "expression": "roll out",
                "segment_index": 0,
                "quote": _SENTENCE,
                "meaning": "Introduce something gradually.",
                "usage_note": "Often used for releases.",
                "examples": ["They roll out the update tomorrow."],
            }
        ],
    },
}


@pytest.fixture
def processor_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, list[str]]:
    for name in (
        "AUDIGEST_LLM_PROVIDER",
        "AUDIGEST_LLM_MODEL",
        "AUDIGEST_STT_PROVIDER",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUDIGEST_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    source = tmp_path / "sample.txt"
    source.write_text(_SENTENCE, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("Weekly release.", encoding="utf-8")

    # An LLM selection must never be interpreted as an STT selection.
    monkeypatch.setenv("AUDIGEST_LLM_PROVIDER", "anthropic")
    result = CliRunner().invoke(app, ["ingest", str(source), "--json"])
    assert result.exit_code == 0, result.stderr
    monkeypatch.delenv("AUDIGEST_LLM_PROVIDER")
    monkeypatch.setenv("AUDIGEST_STT_PROVIDER", "irrelevant-to-processors")
    transcript_id = json.loads(result.stdout)["id"]
    return {
        "summarize": ["summarize", transcript_id, "--json"],
        "apply": ["apply", transcript_id, "--project", str(project), "--json"],
        "english": ["english", transcript_id, "--level", "B2", "--json"],
    }


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    response_text: str,
    *,
    status: int = 200,
) -> list[tuple[Any, dict[str, Any]]]:
    sdk = pytest.importorskip(provider)
    httpx = pytest.importorskip("httpx")
    class_name = "OpenAI" if provider == "openai" else "Anthropic"
    client_type = getattr(sdk, class_name)
    requests = []

    def handle(request: Any) -> Any:
        requests.append((request, json.loads(request.content)))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": f"private payload {_KEY}"}})
        if provider == "openai":
            body = {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": response_text}],
                    }
                ],
            }
        else:
            body = {
                "type": "message",
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": response_text}],
            }
        return httpx.Response(200, json=body)

    def build_client(**options: Any) -> Any:
        assert options["timeout"] == 60.0
        assert options["max_retries"] == 0
        http_client = httpx.Client(transport=httpx.MockTransport(handle))
        return client_type(http_client=http_client, **options)

    monkeypatch.setattr(sdk, class_name, build_client)
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", _KEY)
    return requests


@pytest.mark.parametrize("processor", tuple(_RESPONSES))
@pytest.mark.parametrize("provider", ("openai", "anthropic"))
def test_processor_structured_sdk_request_and_keyless_cache(
    provider: str,
    processor: str,
    processor_args: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests = _mock_http(monkeypatch, provider, json.dumps(_RESPONSES[processor]))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unused-bearer-token")
    runner = CliRunner()
    command = processor_args[processor]

    monkeypatch.setenv("AUDIGEST_LLM_PROVIDER", provider)
    miss = runner.invoke(app, [*command, "--provider", "auto"])
    assert miss.exit_code == 6
    assert "cache-only" in miss.stderr
    assert not requests

    with caplog.at_level(logging.DEBUG, logger=f"{provider}._base_client"):
        generated = runner.invoke(app, command)
    assert generated.exit_code == 0, generated.stderr
    result = json.loads(generated.stdout)
    assert result["provider"] == provider
    assert _SENTENCE not in caplog.text
    assert _KEY not in caplog.text
    assert result["model"] == DEFAULT_MODELS[provider]
    assert len(requests) == 1
    request, body = requests[0]
    assert body["model"] == result["model"]
    assert body["tools"] == []
    if provider == "openai":
        assert str(request.url) == f"{OPENAI_BASE_URL}/responses"
        assert request.headers["authorization"] == f"Bearer {_KEY}"
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["reasoning"] == {"effort": "low"}
        assert result["reasoning_effort"] == "low"
        input_json = body["input"]
    else:
        assert str(request.url) == f"{ANTHROPIC_BASE_URL}/v1/messages"
        assert request.headers["x-api-key"] == _KEY
        assert "authorization" not in request.headers
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert result["reasoning_effort"] is None
        input_json = body["messages"][0]["content"]
    assert json.loads(input_json)["segments"] == [{"segment_index": 0, "text": _SENTENCE}]

    monkeypatch.delenv(f"{provider.upper()}_API_KEY")
    cached = runner.invoke(app, [*command, "--provider", "auto"])
    assert cached.exit_code == 0, cached.stderr
    assert json.loads(cached.stdout) == result
    assert len(requests) == 1


@pytest.mark.parametrize("provider", ("openai", "anthropic"))
@pytest.mark.parametrize("processor", ("apply", "english"))
def test_each_backend_rejects_unverifiable_processor_evidence(
    provider: str,
    processor: str,
    processor_args: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = json.loads(json.dumps(_RESPONSES[processor]))
    if processor == "apply":
        response["recommendations"][0]["project_evidence"]["quote"] = "Unlisted project detail"
    else:
        response["items"][0]["expression"] = "unlisted expression"
    requests = _mock_http(monkeypatch, provider, json.dumps(response))
    runner = CliRunner()
    command = processor_args[processor]

    rejected = runner.invoke(app, [*command, "--provider", provider])
    assert rejected.exit_code == 6
    assert not rejected.stdout
    assert "quote" in rejected.stderr.lower()
    assert len(requests) == 1
    cached = runner.invoke(app, [*command, "--provider", "auto"])
    assert cached.exit_code == 6
    assert "cache-only" in cached.stderr
    assert len(requests) == 1


@pytest.mark.parametrize("processor", tuple(_RESPONSES))
def test_cross_provider_isolation_and_legacy_openai_reuse(
    processor: str,
    processor_args: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(_RESPONSES[processor])
    openai = _mock_http(monkeypatch, "openai", payload)
    anthropic = _mock_http(monkeypatch, "anthropic", payload)
    command = processor_args[processor]
    runner = CliRunner()

    # Model strings are deliberately equal to prove the provider participates in the cache ID.
    monkeypatch.setenv("AUDIGEST_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("AUDIGEST_LLM_MODEL", "ignored-env-model")
    legacy = runner.invoke(app, [*command, "--provider", "openai", "--model", LEGACY_OPENAI_MODEL])
    assert legacy.exit_code == 0, legacy.stderr
    legacy_output = json.loads(legacy.stdout)
    assert legacy_output["model"] == LEGACY_OPENAI_MODEL
    assert len(openai) == 1
    assert not anthropic

    monkeypatch.delenv("AUDIGEST_LLM_MODEL")
    monkeypatch.delenv("OPENAI_API_KEY")
    for provider in ("auto", "openai"):
        reused = runner.invoke(app, [*command, "--provider", provider])
        assert reused.exit_code == 0, reused.stderr
        assert json.loads(reused.stdout) == legacy_output

    fresh = runner.invoke(app, [*command, "--model", LEGACY_OPENAI_MODEL])
    assert fresh.exit_code == 0, fresh.stderr
    fresh_output = json.loads(fresh.stdout)
    assert fresh_output["provider"] == "anthropic"
    assert fresh_output["model"] == LEGACY_OPENAI_MODEL
    assert fresh_output["id"] != legacy_output["id"]
    assert len(anthropic) == 1
    assert len(openai) == 1


@pytest.mark.parametrize("provider", ("openai", "anthropic"))
@pytest.mark.parametrize(
    ("status", "response_text", "message"),
    [
        (404, "", "model is unavailable"),
        (429, "", "rate limit"),
        (400, "", "rejected the structured request"),
        (200, '{"summary":"bad schema"}', "invalid structured response"),
        (
            200,
            '{"summary":"No source evidence","key_points":'
            '[{"point":"Fake point","segment_index":0,"quote":"Unsaid words"}]}',
            "does not match its cited segment",
        ),
    ],
)
def test_sdk_errors_and_invalid_evidence_are_sanitized(
    provider: str,
    status: int,
    response_text: str,
    message: str,
    processor_args: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _mock_http(monkeypatch, provider, response_text, status=status)
    result = CliRunner().invoke(app, [*processor_args["summarize"], "--provider", provider])
    assert result.exit_code == 6, result.stderr
    assert not result.stdout
    assert message in result.stderr.lower()
    assert "private payload" not in result.stderr
    assert _KEY not in result.stderr
    assert len(requests) == 1


@pytest.mark.parametrize("provider", ("openai", "anthropic"))
def test_only_selected_sdk_and_key_are_used(
    provider: str,
    processor_args: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted_imports = []
    original = importlib.import_module

    def unavailable(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in ("openai", "anthropic"):
            attempted_imports.append(name)
            raise ImportError("private import failure")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", unavailable)
    other_provider = "anthropic" if provider == "openai" else "openai"
    monkeypatch.setenv(f"{other_provider.upper()}_API_KEY", _KEY)
    command = processor_args["summarize"]
    runner = CliRunner()

    missing_key = runner.invoke(app, [*command, "--provider", provider])
    assert missing_key.exit_code == 6
    assert f"{provider.upper()}_API_KEY" in missing_key.stderr
    assert not attempted_imports

    monkeypatch.setenv(f"{provider.upper()}_API_KEY", _KEY)
    missing_sdk = runner.invoke(app, [*command, "--provider", provider])
    assert missing_sdk.exit_code == 7
    assert f"'{provider}' extra" in missing_sdk.stderr
    assert "private import failure" not in missing_sdk.stderr
    assert attempted_imports == [provider]
