"""Offline YouTube acquisition tests using synthetic extractor metadata and bodies."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.errors import MissingDependencyError, RetrievalError, SourceResolutionError
from audigest.models import SourceInfo, TranscriptDraft, TranscriptSegment
from audigest.sources.base import ResolvedSource
from audigest.sources.remote import RemoteMediaInput, RemoteTranscriptInput
from audigest.sources.youtube import (
    _run_youtube_metadata,
    _yt_dlp_command,
    parse_youtube_metadata,
    select_audio_format,
    select_caption_track,
    youtube_reference,
)

VIDEO_ID = "abcDEF_1234"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nSynthetic caption.\n"


def prepare_environment(monkeypatch: pytest.MonkeyPatch, cache_dir: Path) -> None:
    monkeypatch.setenv("AUDIGEST_CACHE_DIR", str(cache_dir))
    config_file = cache_dir.parent / "config.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config_file))
    monkeypatch.delenv("AUDIGEST_STT_PROVIDER", raising=False)
    monkeypatch.delenv("AUDIGEST_LANGUAGE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.headers = {"Content-Length": str(len(content))}
        self.offset = 0
        self.closed = False

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def read(self, size: int = -1) -> bytes:
        return self.read1(size)

    def read1(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.content)
        result = self.content[self.offset : self.offset + size]
        self.offset += len(result)
        return result


def make_metadata(
    *,
    subtitles: dict[str, list[dict[str, str]]] | None = None,
    automatic: dict[str, list[dict[str, str]]] | None = None,
    formats: list[dict[str, Any]] | None = None,
    original_language: str | None = "fr",
    upload_date: str = "20250102",
    title: str = "Synthetic episode",
    duration: float = 42.0,
) -> Any:
    reference = youtube_reference(VIDEO_URL)
    payload = {
        "id": VIDEO_ID,
        "title": title,
        "uploader": "Synthetic channel",
        "duration": duration,
        "upload_date": upload_date,
        "original_language": original_language,
        "subtitles": subtitles or {},
        "automatic_captions": automatic or {},
        "formats": formats or [],
        "availability": "public",
    }
    return parse_youtube_metadata(reference, json.dumps(payload).encode("utf-8"))


def audio_format(url: str = "https://media.example/audio?token=first") -> dict[str, Any]:
    return {
        "format_id": "140",
        "protocol": "https",
        "ext": "m4a",
        "url": url,
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "abr": 128.0,
    }


def install_caption_response(
    monkeypatch: pytest.MonkeyPatch,
    contents: list[bytes],
) -> tuple[list[str], list[FakeResponse]]:
    requested: list[str] = []
    responses = [FakeResponse(content) for content in contents]

    def open_response(url: str, **_: object) -> tuple[FakeResponse, str]:
        requested.append(url)
        return responses[len(requested) - 1], url

    monkeypatch.setattr("audigest.sources.remote._open_redirected_response", open_response)
    return requested, responses


def install_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = False,
) -> tuple[list[Path], list[bytes]]:
    paths: list[Path] = []
    payloads: list[bytes] = []

    class Provider:
        name = "fake"
        model = "fake-v1"

        def transcribe(self, path: Path, *, language: str | None) -> TranscriptDraft:
            paths.append(path)
            payloads.append(path.read_bytes())
            if fail:
                raise RuntimeError("synthetic provider error with private details")
            return TranscriptDraft(
                provider=self.name,
                model=self.model,
                language=language,
                segments=[TranscriptSegment(text="Synthetic fallback transcript.")],
            )

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: Provider())
    return paths, payloads


def install_audio_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: bytes = b"synthetic audio bytes",
) -> tuple[list[str], list[Path]]:
    requested: list[str] = []
    temporary_paths: list[Path] = []

    @contextmanager
    def fake_fetch(
        url: str,
        *,
        suffix_override: str | None = None,
        source_info_override: SourceInfo | None = None,
        source_fingerprint_override: str | None = None,
        locator_fingerprint_override: str | None = None,
    ) -> Iterator[RemoteMediaInput]:
        requested.append(url)
        assert suffix_override == ".m4a"
        assert source_info_override is not None
        assert source_fingerprint_override is not None
        assert locator_fingerprint_override is not None
        directory = tmp_path / f"youtube-download-{len(requested)}"
        directory.mkdir()
        path = directory / "source.m4a"
        path.write_bytes(body)
        temporary_paths.append(directory)
        resolved = ResolvedSource(
            source_info=source_info_override,
            media_path=path,
            content_sha256=hashlib.sha256(body).hexdigest(),
        )
        try:
            yield RemoteMediaInput(
                resolved_source=resolved,
                source_fingerprint=source_fingerprint_override,
                locator_fingerprint=locator_fingerprint_override,
            )
        finally:
            shutil.rmtree(directory)

    monkeypatch.setattr("audigest.service.fetch_remote_media", fake_fetch)
    return requested, temporary_paths


@pytest.mark.parametrize(
    "url",
    [
        VIDEO_URL,
        f"https://youtu.be/{VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
    ],
)
def test_youtube_url_forms_canonicalize_by_video_id(url: str) -> None:
    reference = youtube_reference(url)

    assert reference.video_id == VIDEO_ID
    assert reference.url == VIDEO_URL
    assert reference.resolved_source.source_info.type == "youtube"
    assert reference.resolved_source.source_info.uri == VIDEO_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PLsynthetic",
        "https://www.youtube.com/watch?v=short",
        f"https://user:secret@www.youtube.com/watch?v={VIDEO_ID}",
        "https://youtube.example/watch?v=abcDEF_1234",
    ],
)
def test_youtube_url_rejects_playlists_credentials_and_malformed_ids(url: str) -> None:
    with pytest.raises(SourceResolutionError):
        youtube_reference(url)


def test_youtube_metadata_requires_matching_public_finite_video() -> None:
    reference = youtube_reference(VIDEO_URL)
    for patch in (
        {"id": "differentID1"},
        {"_type": "playlist"},
        {"availability": "needs_auth"},
        {"is_private": True},
        {"is_live": True},
        {"duration": float("inf")},
    ):
        payload = {"id": VIDEO_ID, "duration": 1, **patch}
        with pytest.raises(RetrievalError):
            parse_youtube_metadata(reference, json.dumps(payload).encode())

    huge_integer = b'{"id":"abcDEF_1234","duration":' + b"9" * 5000 + b"}"
    with pytest.raises(RetrievalError):
        parse_youtube_metadata(reference, huge_integer)


def test_youtube_caption_preference_is_deterministic() -> None:
    metadata = make_metadata(
        subtitles={
            "fr": [{"ext": "vtt", "url": "https://youtube.com/fr.vtt"}],
            "en": [{"ext": "vtt", "url": "https://youtube.com/en.vtt"}],
        },
        automatic={
            "en": [{"ext": "srt", "url": "https://youtube.com/en-auto.srt"}],
            "de": [{"ext": "vtt", "url": "https://youtube.com/de.vtt"}],
        },
    )
    assert select_caption_track(metadata, "en") == metadata.captions[1]
    assert select_caption_track(metadata, "es") == metadata.captions[0]

    no_original = make_metadata(
        subtitles={
            "zz": [{"ext": "vtt", "url": "https://youtube.com/zz.vtt"}],
            "aa": [{"ext": "vtt", "url": "https://youtube.com/aa.vtt"}],
        },
        original_language=None,
    )
    assert select_caption_track(no_original, None).language == "aa"

    caption_only = make_metadata(
        subtitles={"en": [{"ext": "vtt", "url": "https://youtube.com/en.vtt"}]},
        formats=[],
    )
    assert caption_only.captions
    assert not caption_only.audio_formats


def test_youtube_audio_formats_exclude_video_and_manifests() -> None:
    metadata = make_metadata(
        formats=[
            {**audio_format(), "protocol": "https_dash_segments", "format_id": "dash"},
            {**audio_format(), "protocol": "m3u8_native", "format_id": "hls"},
            {**audio_format(), "format_id": "video", "vcodec": "avc1"},
            audio_format("https://media.example/audio?signature=fresh"),
        ]
    )
    assert len(metadata.audio_formats) == 1
    assert select_audio_format(metadata, "fake").request_url.endswith("signature=fresh")


def test_ytdlp_command_disables_user_configuration_and_manifest_routes() -> None:
    command = _yt_dlp_command(VIDEO_URL)

    for option in (
        "--ignore-config",
        "--no-playlist",
        "--ignore-no-formats-error",
        "--no-update",
        "--no-cache-dir",
        "--no-cookies",
        "--no-cookies-from-browser",
        "--no-geo-bypass",
        "--no-remote-components",
        "--no-check-formats",
        "youtube:skip=hls,dash",
    ):
        assert option in command
    assert "--cookies" not in command
    assert "--cookies-from-browser" not in command
    assert VIDEO_URL == command[-1]


def test_ytdlp_metadata_bounds_output_and_execution_and_joins_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "audigest.sources.youtube._yt_dlp_command",
        lambda _url: [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 5000000)"],
    )
    with pytest.raises(RetrievalError, match="4 MiB"):
        _run_youtube_metadata(VIDEO_URL)
    assert not any(thread.name == "audigest-youtube-metadata" for thread in threading.enumerate())

    monkeypatch.setattr("audigest.sources.youtube._EXECUTION_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(
        "audigest.sources.youtube._yt_dlp_command",
        lambda _url: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    with pytest.raises(RetrievalError, match="60-second limit"):
        _run_youtube_metadata(VIDEO_URL)
    assert not any(thread.name == "audigest-youtube-metadata" for thread in threading.enumerate())


def test_ytdlp_failure_sanitizes_extractor_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "audigest.sources.youtube._yt_dlp_command",
        lambda _url: [
            sys.executable,
            "-c",
            "import sys; print('synthetic-private-token', file=sys.stderr); sys.exit(1)",
        ],
    )
    with pytest.raises(RetrievalError) as error:
        _run_youtube_metadata(VIDEO_URL)
    assert "synthetic-private-token" not in str(error.value)
    assert not any(thread.name == "audigest-youtube-metadata" for thread in threading.enumerate())


def test_youtube_missing_extra_uses_dependency_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
    result = CliRunner().invoke(app, ["ingest", VIDEO_URL, "--json"])
    assert result.exit_code == MissingDependencyError.exit_code
    assert result.stdout == ""
    assert "youtube" in result.stderr
    assert "extra" in result.stderr


def test_supplied_local_transcript_skips_youtube_metadata_and_audio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    prepare_environment(monkeypatch, cache)
    notes = tmp_path / "publisher.md"
    notes.write_text("Supplied transcript wins.", encoding="utf-8")
    original = notes.read_bytes()
    monkeypatch.setattr(
        "audigest.service.get_youtube_metadata",
        lambda *_: pytest.fail("supplied transcript must skip metadata"),
    )
    monkeypatch.setattr(
        "audigest.service.fetch_remote_media",
        lambda *_args, **_kwargs: pytest.fail("supplied transcript must skip audio"),
    )
    result = CliRunner().invoke(
        app,
        ["ingest", f"https://youtu.be/{VIDEO_ID}", "--transcript", str(notes), "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "Supplied transcript wins."
    assert payload["source"]["type"] == "youtube"
    assert payload["source"]["uri"] == VIDEO_URL
    assert payload["origin"]["kind"] == "text"
    assert notes.read_bytes() == original


def test_supplied_remote_transcript_skips_youtube_metadata_and_audio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    prepare_environment(monkeypatch, cache)
    reference = youtube_reference(VIDEO_URL)
    canonical_source = reference.resolved_source
    transcript = RemoteTranscriptInput(
        content=b"Remote supplied text.",
        suffix=".txt",
        resolved_source=ResolvedSource(
            source_info=SourceInfo(type="direct", uri="https://publisher.example/notes.txt"),
            media_path=Path("notes.txt"),
            content_sha256=hashlib.sha256(b"Remote supplied text.").hexdigest(),
        ),
        origin_uri="https://publisher.example/notes.txt",
        locator_fingerprint=hashlib.sha256(b"publisher transcript URL").hexdigest(),
    )
    monkeypatch.setattr(
        "audigest.service.fetch_remote_transcript",
        lambda _url: transcript,
    )
    monkeypatch.setattr(
        "audigest.service.get_youtube_metadata",
        lambda *_: pytest.fail("supplied transcript must skip metadata"),
    )
    monkeypatch.setattr(
        "audigest.service.fetch_remote_media",
        lambda *_args, **_kwargs: pytest.fail("supplied transcript must skip audio"),
    )

    result = CliRunner().invoke(
        app,
        ["ingest", VIDEO_URL, "--transcript", "https://publisher.example/notes.txt", "--json"],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "Remote supplied text."
    assert payload["source"]["type"] == "youtube"
    assert payload["source"]["uri"] == canonical_source.source_info.uri
    assert payload["origin"]["uri"] == "https://publisher.example/notes.txt"


def test_manual_captions_win_before_openai_and_preserve_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    prepare_environment(monkeypatch, cache)
    signed_manual = "https://www.youtube.com/api/timedtext?v=abcDEF_1234&lang=en&sig=secret-manual"
    signed_auto = "https://www.youtube.com/api/timedtext?v=abcDEF_1234&lang=en&sig=secret-auto"
    metadata = make_metadata(
        subtitles={"en": [{"ext": "vtt", "url": signed_manual}]},
        automatic={"en": [{"ext": "srt", "url": signed_auto}]},
    )
    monkeypatch.setattr("audigest.service.get_youtube_metadata", lambda _ref: metadata)
    requested, responses = install_caption_response(monkeypatch, [VTT])
    monkeypatch.setattr(
        "audigest.service.get_provider",
        lambda *_: pytest.fail("usable captions must win before STT provider selection"),
    )
    result = CliRunner().invoke(
        app,
        ["ingest", VIDEO_URL, "--provider", "openai", "--language", "en", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert requested == [signed_manual]
    assert all(response.closed for response in responses)
    assert payload["provider"] == "youtube-captions"
    assert payload["language"] == "en"
    assert payload["source"] == {
        "type": "youtube",
        "uri": VIDEO_URL,
        "title": "Synthetic episode",
        "author": "Synthetic channel",
        "published_at": "2025-01-02T00:00:00Z",
        "duration_seconds": 42.0,
    }
    assert payload["origin"]["kind"] == "captions"
    assert "manual/en" in payload["origin"]["uri"]
    assert "secret" not in payload["origin"]["uri"]
    assert payload["segments"][0]["start"] == 1.0


def test_caption_cache_identity_ignores_rotated_signed_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    prepare_environment(monkeypatch, cache)
    signed_urls = [
        "https://www.youtube.com/api/timedtext?v=abcDEF_1234&lang=en&sig=first",
        "https://www.youtube.com/api/timedtext?v=abcDEF_1234&lang=en&sig=rotated",
    ]
    metadata = [
        make_metadata(subtitles={"en": [{"ext": "vtt", "url": url}]}, original_language="en")
        for url in signed_urls
    ]
    iterator = iter(metadata)
    monkeypatch.setattr("audigest.service.get_youtube_metadata", lambda _ref: next(iterator))
    requested, _responses = install_caption_response(monkeypatch, [VTT, VTT])
    monkeypatch.setattr(
        "audigest.service.get_provider",
        lambda *_: pytest.fail("caption cache hit must not select an STT provider"),
    )
    runner = CliRunner()
    first = runner.invoke(app, ["ingest", VIDEO_URL, "--language", "en", "--json"])
    second = runner.invoke(app, ["ingest", VIDEO_URL, "--language", "en", "--json"])

    assert first.exit_code == second.exit_code == 0
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["id"] == second_payload["id"]
    assert first_payload["created_at"] == second_payload["created_at"]
    assert len(requested) == 2
    assert "rotated" not in second_payload["origin"]["uri"]


def test_malformed_selected_youtube_caption_does_not_fallback_to_stt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    metadata = make_metadata(
        subtitles={"en": [{"ext": "vtt", "url": "https://youtube.com/captions?sig=x"}]}
    )
    monkeypatch.setattr("audigest.service.get_youtube_metadata", lambda _ref: metadata)
    requested, responses = install_caption_response(monkeypatch, [b"WEBVTT\n\nnot a cue\n"])
    monkeypatch.setattr(
        "audigest.service.get_provider",
        lambda *_: pytest.fail("malformed selected captions must not fall back to STT"),
    )
    monkeypatch.setattr(
        "audigest.service.fetch_remote_media",
        lambda *_args, **_kwargs: pytest.fail("malformed selected captions must not fetch audio"),
    )

    result = CliRunner().invoke(app, ["ingest", VIDEO_URL, "--provider", "fake", "--json"])
    assert result.exit_code == 3
    assert result.stdout == ""
    assert "malformed cue" in result.stderr
    assert requested
    assert responses[0].closed


def test_youtube_auto_is_cache_only_when_no_caption_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    prepare_environment(monkeypatch, cache)
    metadata = make_metadata(formats=[audio_format()])
    monkeypatch.setattr("audigest.service.get_youtube_metadata", lambda _ref: metadata)
    monkeypatch.setattr(
        "audigest.service.fetch_remote_media",
        lambda *_args, **_kwargs: pytest.fail("auto cache miss must not fetch audio"),
    )
    monkeypatch.setattr(
        "audigest.service.get_provider",
        lambda *_: pytest.fail("auto must not select an STT provider"),
    )
    result = CliRunner().invoke(app, ["ingest", VIDEO_URL, "--json"])

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "cache-only" in result.stderr
    assert not cache.exists()


def test_youtube_stt_fallback_downloads_audio_cleans_temp_and_show_is_source_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    prepare_environment(monkeypatch, cache)
    metadata = make_metadata(formats=[audio_format()])
    monkeypatch.setattr("audigest.service.get_youtube_metadata", lambda _ref: metadata)
    requested, temporary_paths = install_audio_download(monkeypatch, tmp_path)
    provider_paths, provider_payloads = install_provider(monkeypatch)
    result = CliRunner().invoke(
        app,
        ["ingest", VIDEO_URL, "--provider", "fake", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["provider"] == "fake"
    assert payload["source"]["type"] == "youtube"
    assert payload["source"]["uri"] == VIDEO_URL
    assert payload["origin"]["kind"] == "stt"
    assert payload["origin"]["uri"] == VIDEO_URL
    assert payload["origin"]["format"] == "m4a"
    assert provider_payloads == [b"synthetic audio bytes"]
    assert provider_paths[0].suffix == ".m4a"
    assert requested == [audio_format()["url"]]
    assert not temporary_paths[0].exists()

    monkeypatch.setattr(
        "audigest.service.get_youtube_metadata",
        lambda *_: pytest.fail("show must not resolve the source"),
    )
    monkeypatch.setattr(
        "audigest.service.fetch_remote_media",
        lambda *_args, **_kwargs: pytest.fail("show must not download media"),
    )
    shown = CliRunner().invoke(app, ["show", payload["id"]])
    assert shown.exit_code == 0
    assert "Synthetic fallback transcript." in shown.stdout


def test_youtube_stt_cache_reuses_metadata_identity_without_audio_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    metadata = [
        make_metadata(formats=[audio_format("https://media.example/audio?token=expired")]),
        make_metadata(formats=[audio_format("https://media.example/audio?token=renewed")]),
        make_metadata(formats=[audio_format("https://media.example/audio?token=language")]),
        make_metadata(formats=[audio_format("https://media.example/audio?token=final")]),
        make_metadata(
            formats=[audio_format("https://media.example/audio?token=freshness")],
            duration=43.0,
        ),
    ]
    iterator = iter(metadata)
    monkeypatch.setattr("audigest.service.get_youtube_metadata", lambda _ref: next(iterator))
    requested, _temporary = install_audio_download(monkeypatch, tmp_path)
    _paths, payloads = install_provider(monkeypatch)
    runner = CliRunner()
    first = runner.invoke(
        app, ["ingest", VIDEO_URL, "--provider", "fake", "--language", "en", "--json"]
    )
    second = runner.invoke(
        app, ["ingest", VIDEO_URL, "--provider", "fake", "--language", "en", "--json"]
    )
    different_language = runner.invoke(
        app, ["ingest", VIDEO_URL, "--provider", "fake", "--language", "fr", "--json"]
    )
    final_en = runner.invoke(
        app, ["ingest", VIDEO_URL, "--provider", "fake", "--language", "en", "--json"]
    )
    changed_metadata = runner.invoke(
        app, ["ingest", VIDEO_URL, "--provider", "fake", "--language", "en", "--json"]
    )

    assert all(
        result.exit_code == 0
        for result in (first, second, different_language, final_en, changed_metadata)
    )
    first_id = json.loads(first.stdout)["id"]
    assert first_id == json.loads(second.stdout)["id"] == json.loads(final_en.stdout)["id"]
    assert json.loads(different_language.stdout)["id"] != first_id
    assert json.loads(changed_metadata.stdout)["id"] != first_id
    assert "token=" not in json.loads(first.stdout)["origin"]["uri"]
    assert len(payloads) == 3
    assert requested == [
        "https://media.example/audio?token=expired",
        "https://media.example/audio?token=language",
        "https://media.example/audio?token=freshness",
    ]


def test_youtube_keep_media_retains_download_only_after_success_without_json_noise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    prepare_environment(monkeypatch, cache)
    metadata = make_metadata(formats=[audio_format()])
    monkeypatch.setattr("audigest.service.get_youtube_metadata", lambda _ref: metadata)
    _requested, temporary_paths = install_audio_download(monkeypatch, tmp_path)
    install_provider(monkeypatch)
    result = CliRunner().invoke(
        app,
        ["ingest", VIDEO_URL, "--provider", "fake", "--keep-media", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert result.stdout.strip().startswith("{")
    assert "Kept downloaded media:" in result.stderr
    retained_files = list((cache / "media").glob("*.m4a"))
    assert len(retained_files) == 1
    assert retained_files[0].read_bytes() == b"synthetic audio bytes"
    assert not temporary_paths[0].exists()
    assert json.loads(result.stdout)["id"] == payload["id"]


def test_youtube_provider_failure_cleans_downloaded_audio_and_sanitizes_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    metadata = make_metadata(formats=[audio_format()])
    monkeypatch.setattr("audigest.service.get_youtube_metadata", lambda _ref: metadata)
    _requested, temporary_paths = install_audio_download(monkeypatch, tmp_path)
    install_provider(monkeypatch, fail=True)

    result = CliRunner().invoke(
        app,
        ["ingest", VIDEO_URL, "--provider", "fake", "--json"],
    )

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "synthetic provider error" not in result.stderr
    assert not temporary_paths[0].exists()


def test_transcribe_rejects_youtube_without_metadata_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    monkeypatch.setattr(
        "audigest.service.get_youtube_metadata",
        lambda *_: pytest.fail("transcribe must reject YouTube before metadata lookup"),
    )
    result = CliRunner().invoke(app, ["transcribe", VIDEO_URL, "--provider", "fake"])

    assert result.exit_code == 3
    assert "use 'ingest' for YouTube" in result.stderr
