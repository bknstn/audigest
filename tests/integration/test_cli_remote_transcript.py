"""Offline tests for direct remote transcript and caption ingestion."""

from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.errors import StorageError
from audigest.models import (
    SourceInfo,
    Transcript,
    TranscriptDraft,
    TranscriptOrigin,
    TranscriptSegment,
)
from audigest.sources.remote import MAX_TRANSCRIPT_BYTES
from audigest.stt.router import get_provider


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


class FailingReadResponse(FakeResponse):
    def read1(self, size: int = -1) -> bytes:
        _ = size
        raise http.client.IncompleteRead(b"partial", 100)


def install_fake_stt(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = False,
) -> tuple[list[Path], list[bytes]]:
    paths: list[Path] = []
    contents: list[bytes] = []

    class Provider:
        name = "fake"
        model = "fake-v1"

        def transcribe(self, media_path: Path, *, language: str | None = None) -> TranscriptDraft:
            paths.append(media_path)
            contents.append(media_path.read_bytes())
            if fail:
                raise RuntimeError("synthetic provider failure")
            return TranscriptDraft(
                provider=self.name,
                model=self.model,
                language=language,
                segments=[TranscriptSegment(text="Synthetic downloaded audio transcript.")],
            )

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: Provider())
    return paths, contents


def track_download_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    temporary_root = tmp_path / "tracked-download-temp"

    class TemporaryDirectory:
        def __init__(self, *, prefix: str) -> None:
            _ = prefix

        def __enter__(self) -> str:
            temporary_root.mkdir()
            return str(temporary_root)

        def __exit__(self, *_: object) -> None:
            shutil.rmtree(temporary_root, ignore_errors=True)

    monkeypatch.setattr("audigest.sources.remote.tempfile.TemporaryDirectory", TemporaryDirectory)
    return temporary_root


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


def test_remote_media_download_is_streamed_cached_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    media = b"synthetic remote audio bytes"
    changed = b"changed remote audio bytes"
    responses = [
        FakeResponse(media),
        FakeResponse(media),
        FakeResponse(changed),
        FakeResponse(changed),
    ]
    requests, opened = install_responses(monkeypatch, responses)
    provider_paths, contents = install_fake_stt(monkeypatch)
    runner = CliRunner()
    url = "https://media.example/episode.mp3?token=synthetic-secret"

    first = runner.invoke(app, ["ingest", url, "--provider", "fake", "--language", "en", "--json"])
    cached = runner.invoke(app, ["ingest", url, "--provider", "auto", "--language", "en", "--json"])
    changed_result = runner.invoke(
        app, ["ingest", url, "--provider", "fake", "--language", "en", "--json"]
    )
    refreshed = runner.invoke(
        app,
        ["ingest", url, "--provider", "fake", "--language", "en", "--json", "--no-cache"],
    )

    assert all(result.exit_code == 0 for result in (first, cached, changed_result, refreshed))
    first_payload = json.loads(first.stdout)
    cached_payload = json.loads(cached.stdout)
    changed_payload = json.loads(changed_result.stdout)
    refreshed_payload = json.loads(refreshed.stdout)
    assert cached_payload == first_payload
    assert "Using cached transcript." in cached.stderr
    assert changed_payload["id"] != first_payload["id"]
    assert refreshed_payload["id"] == changed_payload["id"]
    assert refreshed_payload["created_at"] != changed_payload["created_at"]
    assert first_payload["source"] == {
        "type": "direct",
        "uri": "https://media.example/episode.mp3",
        "title": None,
        "author": None,
        "published_at": None,
        "duration_seconds": None,
    }
    assert first_payload["origin"]["uri"] == "https://media.example/episode.mp3"
    assert contents == [media, changed, changed]
    assert len(provider_paths) == 3
    assert all(not path.exists() for path in provider_paths)
    assert all(response.closed for response in opened)
    assert len(requests) == 4
    assert "synthetic-secret" not in first.stdout + first.stderr


def test_remote_media_cache_isolated_by_language(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    install_responses(monkeypatch, [FakeResponse(b"audio") for _ in range(3)])
    provider_paths, _contents = install_fake_stt(monkeypatch)
    runner = CliRunner()
    url = "https://media.example/lesson.wav"
    english = runner.invoke(
        app, ["ingest", url, "--provider", "fake", "--language", "en", "--json"]
    )
    polish = runner.invoke(app, ["ingest", url, "--provider", "fake", "--language", "pl", "--json"])
    replay = runner.invoke(app, ["ingest", url, "--provider", "auto", "--language", "en", "--json"])

    assert english.exit_code == polish.exit_code == replay.exit_code == 0
    assert json.loads(english.stdout)["id"] != json.loads(polish.stdout)["id"]
    assert json.loads(replay.stdout) == json.loads(english.stdout)
    assert len(provider_paths) == 2


def test_remote_media_locator_queries_have_distinct_cache_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    install_responses(monkeypatch, [FakeResponse(b"same audio"), FakeResponse(b"same audio")])
    provider_paths, _contents = install_fake_stt(monkeypatch)
    runner = CliRunner()
    alpha = runner.invoke(
        app,
        ["ingest", "https://media.example/episode.mp3?token=alpha", "--provider", "fake", "--json"],
    )
    beta = runner.invoke(
        app,
        ["ingest", "https://media.example/episode.mp3?token=beta", "--provider", "fake", "--json"],
    )

    assert alpha.exit_code == beta.exit_code == 0
    alpha_payload = json.loads(alpha.stdout)
    beta_payload = json.loads(beta.stdout)
    assert alpha_payload["id"] != beta_payload["id"]
    assert alpha_payload["source"]["uri"] == beta_payload["source"]["uri"]
    assert "alpha" not in alpha.stdout + alpha.stderr
    assert "beta" not in beta.stdout + beta.stderr
    assert len(provider_paths) == 2


def test_remote_media_auto_cache_miss_does_not_select_paid_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    response = FakeResponse(b"uncached remote audio")
    requests, opened = install_responses(monkeypatch, [response])
    monkeypatch.setattr("audigest.service.get_provider", get_provider)

    result = CliRunner().invoke(
        app,
        ["ingest", "https://media.example/uncached.mp3", "--provider", "auto", "--json"],
    )

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "does not select an STT provider" in result.stderr
    assert "OPENAI_API_KEY" not in result.stderr
    assert len(requests) == 1
    assert response.closed


def test_supplied_local_transcript_precedes_sidecar_and_skips_remote_media_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    media = tmp_path / "episode.wav"
    media.write_bytes(b"synthetic local media")
    media.with_suffix(".txt").write_text("Sidecar must lose.", encoding="utf-8")
    supplied = tmp_path / "publisher.md"
    supplied.write_text("# Supplied transcript\n\nIt wins.", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["ingest", str(media), "--transcript", str(supplied), "--provider", "openai", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"]["uri"] == media.resolve().as_uri()
    assert payload["origin"] == {
        "kind": "text",
        "uri": supplied.resolve().as_uri(),
        "format": "md",
    }
    assert payload["text"] == "# Supplied transcript\n\nIt wins."

    def unexpected_request(*_: object) -> Any:
        pytest.fail("a supplied transcript must skip remote media download")

    monkeypatch.setattr("audigest.sources.remote.urllib.request.build_opener", unexpected_request)
    remote = CliRunner().invoke(
        app,
        [
            "ingest",
            "https://media.example/episode.mp3?signature=synthetic-secret",
            "--transcript",
            str(supplied),
            "--provider",
            "openai",
            "--json",
        ],
    )
    assert remote.exit_code == 0, remote.stderr
    remote_payload = json.loads(remote.stdout)
    assert remote_payload["source"]["uri"] == "https://media.example/episode.mp3"
    assert remote_payload["origin"]["uri"] == supplied.resolve().as_uri()
    assert "synthetic-secret" not in remote.stdout + remote.stderr
    assert "Sidecar must lose." not in remote_payload["text"]


def test_changed_supplied_transcript_invalidates_media_cache_without_stt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    media = tmp_path / "episode.wav"
    media.write_bytes(b"synthetic audio")
    transcript = tmp_path / "publisher.txt"
    transcript.write_text("First supplied text.", encoding="utf-8")
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["ingest", str(media), "--transcript", str(transcript), "--provider", "openai", "--json"],
    )
    transcript.write_text("Changed supplied text.", encoding="utf-8")
    second = runner.invoke(
        app,
        ["ingest", str(media), "--transcript", str(transcript), "--provider", "openai", "--json"],
    )

    assert first.exit_code == second.exit_code == 0
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["id"] != second_payload["id"]
    assert first_payload["text"] == "First supplied text."
    assert second_payload["text"] == "Changed supplied text."


def test_supplied_remote_transcript_skips_remote_media_and_preserves_canonical_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    transcript_url = "https://publisher.example/existing.vtt?sig=synthetic-secret"
    vtt = FakeResponse(b"WEBVTT\n\n00:01.000 --> 00:02.000\nPublisher cue.\n")
    requests, opened = install_responses(monkeypatch, [vtt])
    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "https://media.example/episode.mp4?media-token=private",
            "--transcript",
            transcript_url,
            "--provider",
            "openai",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"]["uri"] == "https://media.example/episode.mp4"
    assert payload["origin"] == {
        "kind": "captions",
        "uri": "https://publisher.example/existing.vtt",
        "format": "vtt",
    }
    assert payload["segments"][0]["start"] == 1.0
    assert requests == [(transcript_url, 15)]
    assert vtt.closed
    assert "private" not in result.stdout + result.stderr
    assert "synthetic-secret" not in result.stdout + result.stderr

    source_file = tmp_path / "preserved.json"
    embedded_origin = TranscriptOrigin(
        kind="captions", uri="https://publisher.example/embedded.vtt", format="vtt"
    )
    canonical = Transcript(
        id="c" * 64,
        source=SourceInfo(type="youtube", uri="https://represented.example/episode"),
        origin=embedded_origin,
        provider="existing-provider",
        model="existing-model",
        language="de",
        created_at=datetime(2026, 10, 31, tzinfo=UTC),
        segments=[TranscriptSegment(start=3.0, end=4.0, text="Canonical source.")],
    )
    source_file.write_text(canonical.model_dump_json(), encoding="utf-8")
    canonical_result = CliRunner().invoke(
        app,
        [
            "ingest",
            "https://media.example/episode.wav",
            "--transcript",
            str(source_file),
            "--json",
        ],
    )
    assert canonical_result.exit_code == 0, canonical_result.stderr
    canonical_payload = json.loads(canonical_result.stdout)
    assert canonical_payload["source"] == canonical.source.model_dump(mode="json")
    assert canonical_payload["origin"] == embedded_origin.model_dump(mode="json")
    assert canonical_payload["provider"] == "existing-provider"


def test_invalid_supplied_transcript_fails_without_sidecar_or_stt_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    media = tmp_path / "episode.wav"
    media.write_bytes(b"local media")
    media.with_suffix(".txt").write_text("Valid sidecar must not be used.", encoding="utf-8")
    missing = tmp_path / "missing.txt"
    result = CliRunner().invoke(
        app,
        ["ingest", str(media), "--transcript", str(missing), "--provider", "fake", "--json"],
    )
    assert result.exit_code == 3
    assert result.stdout == ""
    assert "does not exist" in result.stderr
    assert not (cache_dir / "items").exists()

    malformed = FakeResponse(b"WEBVTT\n\ninvalid cue\n")
    requests, opened = install_responses(monkeypatch, [malformed])
    remote = CliRunner().invoke(
        app,
        [
            "ingest",
            "https://media.example/episode.wav",
            "--transcript",
            "https://publisher.example/captions.vtt?secret=synthetic-secret",
            "--provider",
            "fake",
            "--json",
        ],
    )
    assert remote.exit_code == 3
    assert remote.stdout == ""
    assert "synthetic-secret" not in remote.stderr
    assert requests == [("https://publisher.example/captions.vtt?secret=synthetic-secret", 15)]
    assert all(response.closed for response in opened)


def test_transcript_option_is_restricted_to_ingest_media_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    direct_text = tmp_path / "notes.txt"
    direct_text.write_text("Not a media source.", encoding="utf-8")
    transcript = tmp_path / "captions.srt"
    transcript.write_text("1\n00:00:01,000 --> 00:00:02,000\nCaption.\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["ingest", str(direct_text), "--transcript", str(transcript), "--json"],
    )
    assert result.exit_code == 3
    assert "only be used with an audio/video source" in result.stderr
    transcribe_result = CliRunner().invoke(
        app,
        ["transcribe", str(tmp_path / "media.wav"), "--transcript", str(transcript)],
    )
    assert transcribe_result.exit_code == 2
    assert transcribe_result.stdout == ""


@pytest.mark.parametrize("failure", ["oversize", "declared_oversize", "truncated", "read_error"])
def test_remote_media_download_failures_close_and_remove_partial_files(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    temporary_root = track_download_temp(monkeypatch, tmp_path)
    if failure == "oversize":
        monkeypatch.setattr("audigest.sources.remote.MAX_REMOTE_MEDIA_BYTES", 8)
        response = FakeResponse(b"123456789")
    elif failure == "declared_oversize":
        monkeypatch.setattr("audigest.sources.remote.MAX_REMOTE_MEDIA_BYTES", 8)
        response = FakeResponse(b"short", {"Content-Length": "9"})
    elif failure == "truncated":
        response = FakeResponse(b"short", {"Content-Length": "10"})
    else:
        response = FailingReadResponse(b"")
    install_responses(monkeypatch, [response])
    result = CliRunner().invoke(
        app,
        ["ingest", "https://media.example/episode.mp3", "--provider", "fake", "--json"],
    )

    assert result.exit_code == 4
    assert result.stdout == ""
    assert response.closed
    if failure == "declared_oversize":
        assert response.bytes_read == 0
    assert not temporary_root.exists()


def test_remote_media_provider_and_store_failures_clean_temporary_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    temporary_root = track_download_temp(monkeypatch, tmp_path)
    responses = [FakeResponse(b"provider failure bytes"), FakeResponse(b"store failure bytes")]
    _requests, opened = install_responses(monkeypatch, responses)
    paths, _contents = install_fake_stt(monkeypatch, fail=True)
    provider_failure = CliRunner().invoke(
        app,
        ["ingest", "https://media.example/provider.wav", "--provider", "fake", "--keep-media"],
    )
    assert provider_failure.exit_code == 5
    assert paths and not paths[0].exists()
    assert not temporary_root.exists()
    assert not (cache_dir / "media").exists()

    paths.clear()
    storage_paths, _contents = install_fake_stt(monkeypatch)

    def fail_store(*_: object, **__: object) -> Path:
        raise StorageError("synthetic cache failure")

    monkeypatch.setattr("audigest.service.FileTranscriptStore.put", fail_store)
    store_failure = CliRunner().invoke(
        app,
        ["ingest", "https://media.example/store.wav", "--provider", "fake", "--keep-media"],
    )
    assert store_failure.exit_code == 2
    assert storage_paths and not storage_paths[-1].exists()
    assert not temporary_root.exists()
    assert not (cache_dir / "media").exists()
    assert all(response.closed for response in opened)


def test_keep_media_is_opt_in_idempotent_and_never_overwrites_existing_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    body = b"synthetic retained remote audio"
    responses = [FakeResponse(body) for _ in range(5)]
    install_responses(monkeypatch, responses)
    provider_paths, _contents = install_fake_stt(monkeypatch)
    runner = CliRunner()
    url = "https://media.example/retained.wav?token=private"

    first = runner.invoke(app, ["ingest", url, "--provider", "fake", "--keep-media", "--json"])
    assert first.exit_code == 0, first.stderr
    kept_line = next(
        line for line in first.stderr.splitlines() if line.startswith("Kept downloaded media:")
    )
    kept_path = Path(kept_line.split(": ", 1)[1])
    assert kept_path.is_file()
    assert kept_path.read_bytes() == body
    assert str(kept_path) not in first.stdout

    repeated = runner.invoke(app, ["ingest", url, "--provider", "fake", "--keep-media", "--json"])
    assert repeated.exit_code == 0, repeated.stderr
    assert (
        Path(
            next(
                line.split(": ", 1)[1]
                for line in repeated.stderr.splitlines()
                if line.startswith("Kept downloaded media:")
            )
        )
        == kept_path
    )
    assert kept_path.read_bytes() == body

    user_bytes = b"pre-existing cache user file"
    kept_path.write_bytes(user_bytes)
    mismatch = runner.invoke(app, ["ingest", url, "--provider", "fake", "--keep-media", "--json"])
    assert mismatch.exit_code == 2
    assert kept_path.read_bytes() == user_bytes

    target = tmp_path / "user-original.wav"
    target.write_bytes(b"untouched user original")
    kept_path.unlink()
    kept_path.symlink_to(target)
    symlink_result = runner.invoke(
        app, ["ingest", url, "--provider", "fake", "--keep-media", "--json"]
    )
    assert symlink_result.exit_code == 2
    assert kept_path.is_symlink()
    assert target.read_bytes() == b"untouched user original"
    if hasattr(os, "mkfifo"):
        kept_path.unlink()
        os.mkfifo(kept_path)
        fifo_result = runner.invoke(
            app, ["ingest", url, "--provider", "fake", "--keep-media", "--json"]
        )
        assert fifo_result.exit_code == 2
        assert kept_path.is_fifo()
    assert len(provider_paths) == 1
