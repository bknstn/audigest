"""Offline tests for direct remote transcript and caption ingestion."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.models import SourceInfo, Transcript, TranscriptOrigin, TranscriptSegment
from audigest.sources.remote import MAX_TRANSCRIPT_BYTES


def prepare_environment(monkeypatch: pytest.MonkeyPatch, cache_dir: Path) -> None:
    monkeypatch.setenv("AUDIGEST_CACHE_DIR", str(cache_dir))
    config_file = cache_dir.parent / "config.toml"
    config_file.write_text("")
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config_file))
    monkeypatch.delenv("AUDIGEST_STT_PROVIDER", raising=False)
    monkeypatch.delenv("AUDIGEST_LANGUAGE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def unexpected_provider(*_: object, **__: object) -> None:
        pytest.fail("direct remote transcript acquisition must never select an STT provider")

    monkeypatch.setattr("audigest.service.get_provider", unexpected_provider)


class FakeResponse:
    def __init__(self, content: bytes, headers: dict[str, str] | None = None) -> None:
        self.content = content
        self.headers = headers or {}
        self.offset = 0
        self.closed = False
        self.bytes_read = 0

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.content)
        result = self.content[self.offset : self.offset + size]
        self.offset += len(result)
        self.bytes_read += len(result)
        return result

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


class RedirectResponse(FakeResponse):
    def read(self, size: int = -1) -> bytes:
        _ = size
        raise AssertionError("redirect response body must never be read")


def install_responses(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[FakeResponse | BaseException],
) -> tuple[list[tuple[str, int]], list[FakeResponse]]:
    requests: list[tuple[str, int]] = []
    opened_responses: list[FakeResponse] = []

    class FakeOpener:
        def open(self, request: Any, *, timeout: int) -> FakeResponse:
            requests.append((request.full_url, timeout))
            result = responses.pop(0)
            if isinstance(result, BaseException):
                raise result
            opened_responses.append(result)
            return result

    monkeypatch.setattr(
        "audigest.sources.remote.urllib.request.build_opener",
        lambda *_handlers: FakeOpener(),
    )
    return requests, opened_responses


def _canonical_json(*, origin: bool) -> bytes:
    transcript = Transcript(
        id="b" * 64,
        source=SourceInfo(
            type="youtube",
            uri="https://media.example/watch/episode",
            title="Synthetic represented source",
            author="Synthetic author",
        ),
        origin=(
            TranscriptOrigin(
                kind="captions", uri="https://media.example/captions.vtt", format="vtt"
            )
            if origin
            else None
        ),
        provider="imported-provider",
        model="imported-model",
        language="fr",
        created_at=datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC),
        segments=[TranscriptSegment(start=2.0, end=3.5, text="Imported remote JSON.")],
        text="Imported remote JSON.",
    )
    payload = transcript.model_dump(mode="json")
    if not origin:
        payload.pop("origin")
    return json.dumps(payload).encode("utf-8")


@pytest.mark.parametrize(
    ("suffix", "body", "provider", "origin_kind", "origin_format"),
    [
        (".txt", b"Remote transcript: caf\xc3\xa9.\n", "text", "text", "txt"),
        (".md", b"# Remote notes\n\nHello.\n", "text", "text", "md"),
        (
            ".srt",
            b"1\n00:00:01,250 --> 00:00:02,500\nTimed SRT.\n",
            "captions",
            "captions",
            "srt",
        ),
        (
            ".vtt",
            b"WEBVTT\n\n00:01.000 --> 00:02.500\nTimed VTT.\n",
            "captions",
            "captions",
            "vtt",
        ),
        (".json", _canonical_json(origin=True), "imported-provider", "captions", "vtt"),
    ],
)
def test_remote_transcript_formats_reuse_canonical_parsers_without_stt(
    suffix: str,
    body: bytes,
    provider: str,
    origin_kind: str,
    origin_format: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    response = FakeResponse(body, {"Content-Length": str(len(body))})
    requests, _ = install_responses(monkeypatch, [response])

    url = f"https://Example.test/transcript{suffix}?access_token=synthetic-secret#section"
    result = CliRunner().invoke(app, ["ingest", url, "--provider", "openai", "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["provider"] == provider
    assert payload["source"]["type"] == ("youtube" if suffix == ".json" else "direct")
    assert payload["source"]["uri"] == (
        "https://media.example/watch/episode"
        if suffix == ".json"
        else f"https://example.test/transcript{suffix}"
    )
    assert payload["origin"] == (
        {"kind": "captions", "uri": "https://media.example/captions.vtt", "format": "vtt"}
        if suffix == ".json"
        else {
            "kind": origin_kind,
            "uri": f"https://example.test/transcript{suffix}",
            "format": origin_format,
        }
    )
    if suffix in {".srt", ".vtt"}:
        assert payload["segments"][0]["start"] is not None
    assert requests == [
        (f"https://example.test/transcript{suffix}?access_token=synthetic-secret", 15)
    ]
    assert response.closed
    assert "synthetic-secret" not in result.stdout + result.stderr
    assert all(
        "synthetic-secret" not in path.read_text(encoding="utf-8")
        for path in cache_dir.rglob("*")
        if path.is_file()
    )


def test_legacy_remote_canonical_gets_remote_origin_and_embedded_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    response = FakeResponse(_canonical_json(origin=False))
    install_responses(monkeypatch, [response])

    result = CliRunner().invoke(
        app, ["ingest", "https://example.test/old.json?sig=synthetic", "--json"]
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"]["uri"] == "https://media.example/watch/episode"
    assert payload["origin"] == {
        "kind": "canonical",
        "uri": "https://example.test/old.json",
        "format": "json",
    }
    assert payload["provider"] == "imported-provider"
    assert payload["model"] == "imported-model"
    assert payload["language"] == "fr"
    assert payload["segments"][0]["start"] == 2.0


def test_remote_fetch_is_validated_before_cache_reuse_and_changed_bytes_invalidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    responses = [
        FakeResponse(b"First remote body."),
        FakeResponse(b"First remote body."),
        FakeResponse(b"Changed remote body."),
        FakeResponse(b"Changed remote body."),
    ]
    requests, opened = install_responses(monkeypatch, responses)

    runner = CliRunner()
    url = "https://example.test/transcript.txt?sig=synthetic-token"
    first = runner.invoke(app, ["ingest", url, "--provider", "openai", "--json"])
    repeated = runner.invoke(app, ["ingest", url, "--provider", "auto", "--json"])
    changed = runner.invoke(app, ["ingest", url, "--provider", "openai", "--json"])
    refreshed = runner.invoke(app, ["ingest", url, "--provider", "openai", "--json", "--no-cache"])

    assert all(result.exit_code == 0 for result in (first, repeated, changed, refreshed))
    first_payload = json.loads(first.stdout)
    repeated_payload = json.loads(repeated.stdout)
    changed_payload = json.loads(changed.stdout)
    refreshed_payload = json.loads(refreshed.stdout)
    assert repeated_payload == first_payload
    assert "Using cached transcript." in repeated.stderr
    assert changed_payload["id"] != first_payload["id"]
    assert changed_payload["text"] == "Changed remote body."
    assert refreshed_payload["id"] == changed_payload["id"]
    assert refreshed_payload["created_at"] != changed_payload["created_at"]
    assert len(requests) == 4
    assert all(response.closed for response in opened)


def test_query_variants_have_distinct_cache_identity_but_sanitized_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    install_responses(monkeypatch, [FakeResponse(b"Same body."), FakeResponse(b"Same body.")])
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["ingest", "https://example.test/transcript.txt?token=alpha", "--json"],
    )
    second = runner.invoke(
        app,
        ["ingest", "https://example.test/transcript.txt?token=beta", "--json"],
    )

    assert first.exit_code == second.exit_code == 0
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["id"] != second_payload["id"]
    assert first_payload["origin"]["uri"] == second_payload["origin"]["uri"]
    assert "alpha" not in first.stdout + second.stdout
    assert "beta" not in first.stdout + second.stdout
    assert all(
        "alpha" not in path.read_text(encoding="utf-8")
        and "beta" not in path.read_text(encoding="utf-8")
        for path in cache_dir.rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize("body", [b"", b"\xff", b"WEBVTT\n\ninvalid cue\n"])
def test_invalid_remote_transcript_fails_as_source_error_without_stt(
    body: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    install_responses(monkeypatch, [FakeResponse(body)])

    def unexpected_provider(*_: object, **__: object) -> None:
        pytest.fail("invalid remote content must fail before STT")

    monkeypatch.setattr("audigest.service.get_provider", unexpected_provider)
    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "https://example.test/captions.vtt?token=synthetic-secret",
            "--provider",
            "openai",
            "--json",
        ],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "synthetic-secret" not in result.stderr
    assert not (cache_dir / "items").exists()


@pytest.mark.parametrize("failure", ["length", "actual", "timeout", "http"])
def test_remote_retrieval_failures_are_bounded_category_four_and_sanitized(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    secret_url = "https://example.test/transcript.txt?token=synthetic-secret"
    response = FakeResponse(
        b"x" * (MAX_TRANSCRIPT_BYTES + 1),
        {"Content-Length": str(MAX_TRANSCRIPT_BYTES + 1)} if failure == "length" else {},
    )
    if failure in {"length", "actual"}:
        boundary: FakeResponse | BaseException = response
    elif failure == "timeout":
        boundary = TimeoutError("synthetic-secret timeout detail")
    else:
        boundary = urllib.error.HTTPError(
            secret_url,
            503,
            "synthetic-secret response detail",
            {},
            io.BytesIO(b"synthetic-secret body"),
        )
    install_responses(monkeypatch, [boundary])

    result = CliRunner().invoke(app, ["ingest", secret_url, "--json"])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert "synthetic-secret" not in result.stderr
    if failure == "length":
        assert response.bytes_read == 0
        assert response.closed
    if failure == "actual":
        assert response.bytes_read == MAX_TRANSCRIPT_BYTES + 1
        assert response.closed


@pytest.mark.parametrize("late_body", [b"partial body", b""])
def test_slow_trickle_response_hits_total_deadline_and_closes(
    late_body: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    clock = [0.0]

    class SlowResponse(FakeResponse):
        def read1(self, size: int = -1) -> bytes:
            clock[0] += 61.0
            _ = size
            return late_body

    response = SlowResponse(late_body)
    install_responses(monkeypatch, [response])
    monkeypatch.setattr("audigest.sources.remote.time.monotonic", lambda: clock[0])

    result = CliRunner().invoke(
        app,
        ["ingest", "https://example.test/transcript.txt?token=synthetic-secret", "--json"],
    )

    assert result.exit_code == 4
    assert result.stdout == ""
    assert "time limit" in result.stderr
    assert "synthetic-secret" not in result.stderr
    assert response.closed


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://user:synthetic-secret@example.test/transcript.txt",
        "ftp://example.test/transcript.txt",
    ],
)
def test_unsafe_or_non_http_input_is_rejected_before_request(
    unsafe_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    requests: list[str] = []

    def unexpected_opener(*_: object) -> Any:
        requests.append("opened")
        pytest.fail("unsafe URLs must be rejected before network access")

    monkeypatch.setattr("audigest.sources.remote.urllib.request.build_opener", unexpected_opener)
    result = CliRunner().invoke(app, ["ingest", unsafe_url, "--json"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "synthetic-secret" not in result.stderr
    assert requests == []


@pytest.mark.parametrize(
    "redirect_url",
    [
        "file:///private/transcript.txt?token=synthetic-secret",
        "https://user:synthetic-secret@example.test/transcript.txt",
    ],
)
def test_unsafe_redirects_fail_as_retrieval_error_without_leaking_target(
    redirect_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    redirect_response = FakeResponse(b"")
    requested_targets: list[str] = []

    class RedirectOpener:
        def __init__(self, handler: Any) -> None:
            self.handler = handler

        def open(self, request: Any, *, timeout: int) -> FakeResponse:
            _ = timeout
            requested_targets.append(request.full_url)
            return self.handler.http_error_302(
                request,
                redirect_response,
                302,
                "Found",
                {"Location": redirect_url},
            )

    monkeypatch.setattr(
        "audigest.sources.remote.urllib.request.build_opener",
        lambda handler: RedirectOpener(handler),
    )
    result = CliRunner().invoke(app, ["ingest", "https://example.test/transcript.txt", "--json"])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert "synthetic-secret" not in result.stderr
    assert requested_targets == ["https://example.test/transcript.txt"]
    assert redirect_response.closed


def test_remote_redirects_are_followed_manually_with_bounded_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    requests: list[tuple[str, float]] = []
    redirect_bodies = [RedirectResponse(b"ignored"), RedirectResponse(b"ignored")]
    all_redirect_bodies = list(redirect_bodies)
    final_response = FakeResponse(b"After two redirects.")

    class RedirectingOpener:
        def __init__(self, handler: Any) -> None:
            self.handler = handler

        def open(self, request: Any, *, timeout: float) -> FakeResponse:
            requests.append((request.full_url, timeout))
            if len(requests) == 1:
                self.handler.http_error_302(
                    request,
                    redirect_bodies.pop(0),
                    302,
                    "Found",
                    {"Location": "/step.txt"},
                )
            if len(requests) == 2:
                self.handler.http_error_302(
                    request,
                    redirect_bodies.pop(0),
                    302,
                    "Found",
                    {"Location": "https://cdn.example/final.txt"},
                )
            return final_response

    monkeypatch.setattr(
        "audigest.sources.remote.urllib.request.build_opener",
        lambda handler: RedirectingOpener(handler),
    )
    result = CliRunner().invoke(
        app,
        ["ingest", "https://example.test/start.txt?token=private", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "After two redirects."
    assert payload["origin"]["uri"] == "https://example.test/start.txt"
    assert [url for url, _timeout in requests] == [
        "https://example.test/start.txt?token=private",
        "https://example.test/step.txt",
        "https://cdn.example/final.txt",
    ]
    assert all(0 < timeout <= 15 for _url, timeout in requests)
    assert all(response.closed for response in all_redirect_bodies)
    assert final_response.closed
    assert "private" not in result.stdout + result.stderr


def test_redirect_limit_is_enforced_without_reading_redirect_bodies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    calls: list[str] = []
    redirect_responses: list[RedirectResponse] = []

    class LoopingOpener:
        def __init__(self, handler: Any) -> None:
            self.handler = handler

        def open(self, request: Any, *, timeout: float) -> FakeResponse:
            _ = timeout
            calls.append(request.full_url)
            response = RedirectResponse(b"ignored")
            redirect_responses.append(response)
            return self.handler.http_error_302(
                request,
                response,
                302,
                "Found",
                {"Location": request.full_url},
            )

    monkeypatch.setattr(
        "audigest.sources.remote.urllib.request.build_opener",
        lambda handler: LoopingOpener(handler),
    )
    result = CliRunner().invoke(app, ["ingest", "https://example.test/loop.txt", "--json"])

    assert result.exit_code == 4
    assert "redirect limit" in result.stderr
    assert len(calls) == 6
    assert all(response.closed for response in redirect_responses)


@pytest.mark.parametrize("suffix", [".txt", ".wav"])
def test_transcribe_rejects_remote_media_or_transcripts_without_request(
    suffix: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")

    def unexpected_opener(*_: object) -> Any:
        pytest.fail("transcribe is local-media-only")

    monkeypatch.setattr("audigest.sources.remote.urllib.request.build_opener", unexpected_opener)
    result = CliRunner().invoke(
        app,
        [
            "transcribe",
            f"https://example.test/source{suffix}",
            "--provider",
            "openai",
            "--json",
        ],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "local file media only" in result.stderr


def test_remote_ingest_artifact_can_be_shown_without_a_second_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    requests, opened = install_responses(monkeypatch, [FakeResponse(b"Saved remote transcript.")])
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", "https://example.test/transcript.txt", "--json"])
    assert ingested.exit_code == 0, ingested.stderr
    transcript_id = json.loads(ingested.stdout)["id"]
    shown = runner.invoke(app, ["show", transcript_id, "--json"])

    assert shown.exit_code == 0, shown.stderr
    assert json.loads(shown.stdout) == json.loads(ingested.stdout)
    assert len(requests) == 1
    assert all(response.closed for response in opened)
    assert list((tmp_path / "cache").rglob("*.tmp")) == []
