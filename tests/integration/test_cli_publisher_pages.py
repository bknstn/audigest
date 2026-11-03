"""Offline CLI coverage for narrow publisher episode-page discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.models import TranscriptDraft, TranscriptSegment


class FakeResponse:
    def __init__(self, content: bytes, headers: dict[str, str] | None = None) -> None:
        self.content = content
        self.headers = headers or {}
        self.offset = 0
        self.closed = False

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
        return result

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


@dataclass(frozen=True)
class Redirect:
    target: str
    response: FakeResponse | None = None


def prepare_environment(monkeypatch: pytest.MonkeyPatch, cache_dir: Path) -> None:
    monkeypatch.setenv("AUDIGEST_CACHE_DIR", str(cache_dir))
    config_file = cache_dir.parent / "config.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config_file))
    for name in (
        "AUDIGEST_STT_PROVIDER",
        "AUDIGEST_LANGUAGE",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    def unexpected_provider(*_: object, **__: object) -> None:
        pytest.fail("publisher transcript routes must not select an STT provider")

    monkeypatch.setattr("audigest.service.get_provider", unexpected_provider)


def install_responses(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[FakeResponse | Redirect | BaseException],
) -> tuple[list[str], list[FakeResponse]]:
    requests: list[str] = []
    opened: list[FakeResponse] = []

    class Opener:
        def __init__(self, handler: Any) -> None:
            self.handler = handler

        def open(self, request: Any, *, timeout: float) -> FakeResponse:
            _ = timeout
            requests.append(request.full_url)
            response = responses.pop(0)
            if isinstance(response, Redirect):
                redirect_body = response.response or FakeResponse(b"redirect body must not be read")
                self.handler.http_error_302(
                    request,
                    redirect_body,
                    302,
                    "Found",
                    {"Location": response.target},
                )
            if isinstance(response, BaseException):
                raise response
            opened.append(response)
            return response

    monkeypatch.setattr(
        "audigest.sources.remote.urllib.request.build_opener",
        lambda handler: Opener(handler),
    )
    return requests, opened


def install_fake_stt(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Path], list[bytes]]:
    paths: list[Path] = []
    contents: list[bytes] = []

    class Provider:
        name = "fake"
        model = "fake-publisher-v1"

        def transcribe(self, media_path: Path, *, language: str | None = None) -> TranscriptDraft:
            paths.append(media_path)
            contents.append(media_path.read_bytes())
            return TranscriptDraft(
                provider=self.name,
                model=self.model,
                language=language,
                segments=[TranscriptSegment(text="Synthetic publisher audio transcript.")],
            )

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: Provider())
    return paths, contents


def test_publisher_inline_wins_and_human_ingest_keeps_id_artifact_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    page_url = "https://publisher.example/episode?signature=query-secret"
    page = b"""<html lang="fr"><body>
      <div id="transcript">Visible inline transcript.</div>
      <video><track kind="captions" srclang="en" src="/preferred.vtt"></video>
    </body></html>"""
    requests, opened = install_responses(monkeypatch, [FakeResponse(page)])

    result = CliRunner().invoke(app, ["ingest", page_url])

    assert result.exit_code == 0, result.stderr
    assert "Transcript ID:" in result.stdout
    assert "Markdown:" in result.stdout
    assert "Visible inline transcript." not in result.stdout
    assert requests == [page_url]
    assert all(response.closed for response in opened)
    transcript_json = next(cache_dir.glob("items/*/transcript.json"))
    payload = json.loads(transcript_json.read_text(encoding="utf-8"))
    assert payload["text"] == "Visible inline transcript."
    assert payload["source"]["uri"] == "https://publisher.example/episode"
    assert payload["origin"]["uri"] == "https://publisher.example/episode"
    assert "query-secret" not in result.stdout + result.stderr + transcript_json.read_text()


def test_text_link_beats_inline_and_caption_links_resolve_after_redirect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    page_url = "https://publisher.example/start?page=query-secret"
    page = b"""<html><head><link rel="transcript" href="../transcript.txt"></head>
      <body><div class="transcript">Lower priority inline.</div>
      <a href="/caption.vtt" title="Transcript">Caption</a></body></html>"""
    requests, opened = install_responses(
        monkeypatch,
        [
            Redirect("https://cdn.example/series/episode/page.html?redirect=query-secret"),
            FakeResponse(page),
            FakeResponse(b"Higher priority linked transcript."),
        ],
    )

    result = CliRunner().invoke(app, ["ingest", page_url, "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "Higher priority linked transcript."
    assert payload["source"]["uri"] == "https://publisher.example/start"
    assert payload["origin"]["uri"] == "https://cdn.example/series/transcript.txt"
    assert requests == [
        page_url,
        "https://cdn.example/series/episode/page.html?redirect=query-secret",
        "https://cdn.example/series/transcript.txt",
    ]
    assert all(response.closed for response in opened)
    assert "query-secret" not in result.stdout + result.stderr


def test_requested_language_selects_a_declared_caption_track(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    page = b"""<html lang="fr"><video>
      <track kind="captions" srclang="fr" src="/fr.vtt">
      <track kind="subtitles" srclang="en" src="/en.vtt">
    </video></html>"""
    english_captions = b"WEBVTT\n\n00:01.000 --> 00:02.000\nSelected English caption.\n"
    requests, opened = install_responses(
        monkeypatch,
        [FakeResponse(page), FakeResponse(english_captions)],
    )

    result = CliRunner().invoke(
        app,
        ["ingest", "https://publisher.example/tracks", "--language", "en", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "Selected English caption."
    assert payload["language"] == "en"
    assert payload["segments"][0]["start"] == 1.0
    assert payload["segments"][0]["end"] == 2.0
    assert payload["provider"] == "publisher-captions"
    assert requests == [
        "https://publisher.example/tracks",
        "https://publisher.example/en.vtt",
    ]
    assert all(response.closed for response in opened)


def test_supplied_local_transcript_skips_publisher_page_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    transcript = tmp_path / "provided.md"
    transcript.write_text("Provided transcript.", encoding="utf-8")
    requests, _opened = install_responses(monkeypatch, [])

    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "https://publisher.example/episode.html?token=private",
            "--transcript",
            str(transcript),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "Provided transcript."
    assert requests == []
    assert payload["source"]["uri"] == "https://publisher.example/episode.html"
    assert payload["origin"]["kind"] == "text"


def test_dynamic_unrelated_page_markup_does_not_invalidate_selected_text_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    url = "https://publisher.example/episode"
    responses = [
        FakeResponse(b'<html><link rel="transcript" href="/episode.txt"><p>one</p></html>'),
        FakeResponse(b"Stable selected text."),
        FakeResponse(b'<html><link rel="transcript" href="/episode.txt"><p>two</p></html>'),
        FakeResponse(b"Stable selected text."),
        FakeResponse(b'<html><link rel="transcript" href="/episode.txt"><p>three</p></html>'),
        FakeResponse(b"Changed selected text."),
        FakeResponse(b'<html><link rel="transcript" href="/episode.txt"><p>four</p></html>'),
        FakeResponse(b"Changed selected text."),
    ]
    requests, opened = install_responses(monkeypatch, responses)
    runner = CliRunner()

    first = runner.invoke(app, ["ingest", url, "--json"])
    repeated = runner.invoke(app, ["ingest", url, "--json"])
    changed = runner.invoke(app, ["ingest", url, "--json"])
    refreshed = runner.invoke(app, ["ingest", url, "--json", "--no-cache"])

    assert all(result.exit_code == 0 for result in (first, repeated, changed, refreshed))
    first_payload, repeated_payload, changed_payload, refreshed_payload = map(
        lambda result: json.loads(result.stdout), (first, repeated, changed, refreshed)
    )
    assert repeated_payload == first_payload
    assert "Using cached transcript." in repeated.stderr
    assert changed_payload["id"] != first_payload["id"]
    assert refreshed_payload["id"] == changed_payload["id"]
    assert refreshed_payload["created_at"] != changed_payload["created_at"]
    assert len(requests) == 8
    assert all(response.closed for response in opened)


def test_unmarked_page_and_malformed_selected_caption_fail_without_stt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    requests, _opened = install_responses(
        monkeypatch,
        [
            FakeResponse(b"<html><body>Unmarked page text that is not a transcript.</body></html>"),
            FakeResponse(
                b'<html><video><track kind="captions" src="/captions.vtt"></video></html>'
            ),
            FakeResponse(b"WEBVTT\n\nMalformed cue."),
        ],
    )
    runner = CliRunner()

    unmarked = runner.invoke(app, ["ingest", "https://publisher.example/unmarked", "--json"])
    malformed = runner.invoke(app, ["ingest", "https://publisher.example/captioned", "--json"])

    assert unmarked.exit_code == 3
    assert malformed.exit_code == 3
    assert unmarked.stdout == malformed.stdout == ""
    assert "direct audio/video source" in unmarked.stderr
    assert "malformed cue" in malformed.stderr
    assert len(requests) == 3
    assert not (cache_dir / "items").exists()


def test_empty_marked_inline_block_fails_before_media_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    requests, _opened = install_responses(
        monkeypatch,
        [
            FakeResponse(
                b'<html><div class="transcript"><p>  </p></div>'
                b'<audio src="/fallback.mp3"></audio></html>'
            )
        ],
    )

    result = CliRunner().invoke(app, ["ingest", "https://publisher.example/empty", "--json"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "inline transcript is empty" in result.stderr
    assert requests == ["https://publisher.example/empty"]


def test_publisher_media_is_page_provenanced_and_temp_download_is_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    requests, opened = install_responses(
        monkeypatch,
        [
            FakeResponse(b'<html><video src="/episode.mp3?token=signed"></video></html>'),
            FakeResponse(b"synthetic publisher audio"),
        ],
    )
    provider_paths, provider_contents = install_fake_stt(monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "https://publisher.example/episode?secret=private",
            "--provider",
            "fake",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"]["uri"] == "https://publisher.example/episode"
    assert payload["origin"]["uri"] == "https://publisher.example/episode"
    assert "private" not in result.stdout + result.stderr
    assert "signed" not in result.stdout + result.stderr
    assert requests == [
        "https://publisher.example/episode?secret=private",
        "https://publisher.example/episode.mp3?token=signed",
    ]
    assert provider_contents == [b"synthetic publisher audio"]
    assert len(provider_paths) == 1 and not provider_paths[0].exists()
    assert all(response.closed for response in opened)
    assert list(tmp_path.glob("audigest-media-*")) == []


def test_ambiguous_and_spotify_media_sources_are_rejected_before_media_get(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    requests, _opened = install_responses(
        monkeypatch,
        [
            FakeResponse(b'<html><audio src="/one.mp3"><source src="/two.mp3"></audio></html>'),
            FakeResponse(
                b'<html><audio src="https://open.spotify.com/episode/track.mp3"></audio></html>'
            ),
        ],
    )
    runner = CliRunner()

    ambiguous = runner.invoke(app, ["ingest", "https://publisher.example/ambiguous", "--json"])
    spotify = runner.invoke(app, ["ingest", "https://publisher.example/spotify", "--json"])

    assert ambiguous.exit_code == spotify.exit_code == 3
    assert "multiple direct media sources" in ambiguous.stderr
    assert "Spotify" in spotify.stderr
    assert len(requests) == 2


@pytest.mark.parametrize(
    "spotify_url",
    [
        "https://open.spotify.com/episode/track.mp3",
        "https://audio-cdn.scdn.co/episode.mp3",
    ],
)
def test_publisher_media_redirect_to_spotify_is_blocked_and_cleaned(
    spotify_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    page_url = "https://publisher.example/episode"
    media_url = "https://media.publisher.example/episode.mp3"
    redirect_body = FakeResponse(b"redirect body must not be read")
    requests, opened = install_responses(
        monkeypatch,
        [
            FakeResponse(b'<html><audio src="https://media.publisher.example/episode.mp3">'),
            Redirect(spotify_url, response=redirect_body),
        ],
    )
    temporary_root = tmp_path / "tracked-publisher-download"

    class TemporaryDirectory:
        def __init__(self, *, prefix: str) -> None:
            _ = prefix

        def __enter__(self) -> str:
            temporary_root.mkdir()
            return str(temporary_root)

        def __exit__(self, *_: object) -> None:
            temporary_root.rmdir()

    monkeypatch.setattr("audigest.sources.remote.tempfile.TemporaryDirectory", TemporaryDirectory)

    result = CliRunner().invoke(app, ["ingest", page_url, "--provider", "fake", "--json"])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert "redirects to Spotify" in result.stderr
    assert requests == [page_url, media_url]
    assert spotify_url not in requests
    assert redirect_body.closed
    assert all(response.closed for response in opened)
    assert not temporary_root.exists()


def test_retained_publisher_media_is_copied_only_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    install_responses(
        monkeypatch,
        [
            FakeResponse(b'<html><audio src="/episode.wav"></audio></html>'),
            FakeResponse(b"synthetic retained bytes"),
        ],
    )
    provider_paths, _contents = install_fake_stt(monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "https://publisher.example/episode",
            "--provider",
            "fake",
            "--keep-media",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    kept = list((cache_dir / "media").glob("*.wav"))
    assert len(kept) == 1
    assert kept[0].read_bytes() == b"synthetic retained bytes"
    assert len(provider_paths) == 1 and not provider_paths[0].exists()
    assert list(tmp_path.glob("audigest-media-*")) == []
