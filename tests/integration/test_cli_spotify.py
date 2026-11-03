"""Offline tests for explicit Spotify episode to RSS mapping."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.models import TranscriptSegment
from audigest.stt.base import TranscriptDraft
from audigest.stt.router import get_provider

EPISODE_ID = "A" * 22
SPOTIFY_URL = f"https://open.spotify.com/episode/{EPISODE_ID}"


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
        pytest.fail("Spotify transcript routes must not select an STT provider")

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
                redirect_body = FakeResponse(b"redirect body is not read")
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


def oembed(title: str) -> FakeResponse:
    return FakeResponse(
        json.dumps(
            {
                "provider_name": "Spotify",
                "type": "rich",
                "title": title,
                "html": "<iframe src='https://open.spotify.com/embed/episode/private'></iframe>",
                "thumbnail_url": "https://i.scdn.co/image/private",
            }
        ).encode()
    )


def rss_feed(items: str) -> bytes:
    return f"""<?xml version='1.0' encoding='UTF-8'?>
    <rss version='2.0' xmlns:podcast='https://podcastindex.org/namespace/1.0'>
      <channel><title>Publisher</title><link>https://publisher.example/</link>
      <description>Feed</description><language>en</language>{items}</channel>
    </rss>""".encode()


def item(guid: str, title: str, body: str) -> str:
    return f"<item><guid>{guid}</guid><title>{title}</title>{body}</item>"


def enclosure_item(guid: str, title: str) -> str:
    return item(
        guid,
        title,
        f"<enclosure url='/{guid}.mp3' type='audio/mpeg' length='1'/>",
    )


def metadata_url() -> str:
    return (
        "https://open.spotify.com/oembed?url="
        f"https%3A%2F%2Fopen.spotify.com%2Fepisode%2F{EPISODE_ID}"
    )


def test_spotify_maps_exact_normalized_title_and_prefers_transcript_over_enclosure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.php?format=rss&secret=feed-private"
    feed = rss_feed(
        item(
            "episode-guid",
            "a  great\n episode",
            "<author>Episode Host</author>"
            "<pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>"
            "<podcast:transcript url='/episode.txt?signature=resource-private' "
            "type='text/plain' language='en'/>"
            "<enclosure url='/episode.mp3' type='audio/mpeg' length='5'/>",
        )
        + item(
            "unrelated-guid",
            "Unrelated Episode",
            "<podcast:transcript type='unsupported'/>"
            "<enclosure url='/unrelated.mp3' type='audio/mpeg' length='5'/>",
        )
    )
    requests, opened = install_responses(
        monkeypatch,
        [
            oembed(" A Great Episode "),
            FakeResponse(feed),
            FakeResponse(b"Publisher transcript."),
        ],
    )

    result = CliRunner().invoke(
        app,
        [
            "ingest",
            f"{SPOTIFY_URL}?si=url-private",
            "--feed",
            feed_url,
            "--episode",
            "episode-guid",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"] == {
        "type": "spotify",
        "uri": SPOTIFY_URL,
        "title": "A Great Episode",
        "author": "Episode Host",
        "published_at": "2024-01-01T12:00:00Z",
        "duration_seconds": None,
    }
    assert payload["origin"]["uri"] == "https://publisher.example/episode.txt"
    assert payload["text"] == "Publisher transcript."
    assert requests == [
        metadata_url(),
        feed_url,
        "https://publisher.example/episode.txt?signature=resource-private",
    ]
    assert all(response.closed for response in opened)
    assert all(secret not in result.stdout + result.stderr for secret in ("private", "signature"))


def test_spotify_supplied_transcript_skips_metadata_and_feed_even_with_selector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    transcript = tmp_path / "provided.txt"
    transcript.write_text("Provided transcript.", encoding="utf-8")
    requests, _opened = install_responses(monkeypatch, [])

    result = CliRunner().invoke(
        app,
        [
            "ingest",
            f"{SPOTIFY_URL}?signed=private",
            "--feed",
            "https://publisher.example/feed.rss?secret=private",
            "--episode",
            "episode-guid",
            "--transcript",
            str(transcript),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"]["uri"] == SPOTIFY_URL
    assert payload["text"] == "Provided transcript."
    assert requests == []


@pytest.mark.parametrize(
    ("items", "selector", "expected_error"),
    [
        (
            enclosure_item("one", "A Different Episode") + enclosure_item("two", "Another Episode"),
            None,
            "matches 0 RSS episodes",
        ),
        (
            enclosure_item("one", "Mapped Episode") + enclosure_item("two", " mapped   episode "),
            None,
            "matches 2 RSS episodes",
        ),
        (
            enclosure_item("one", "Different Episode") + enclosure_item("two", "Mapped Episode"),
            "one",
            "does not match Spotify metadata",
        ),
    ],
)
def test_spotify_title_mapping_fails_safely_before_fetching_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    items: str,
    selector: str | None,
    expected_error: str,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    responses: list[FakeResponse | Redirect | BaseException] = [
        oembed("Mapped Episode"),
        FakeResponse(rss_feed(items)),
    ]
    requests, _opened = install_responses(monkeypatch, responses)
    args = [
        "ingest",
        SPOTIFY_URL,
        "--feed",
        "https://publisher.example/feed.rss",
    ]
    if selector is not None:
        args.extend(["--episode", selector])
    args.append("--json")

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 3
    assert expected_error in result.stderr
    assert requests == [metadata_url(), "https://publisher.example/feed.rss"]
    assert "/one.mp3" not in result.stderr


@pytest.mark.parametrize(
    ("args", "exit_code"),
    [
        (["ingest", "./local.txt", "--feed", "https://publisher.example/feed.rss"], 2),
        (["ingest", SPOTIFY_URL, "--episode", "guid"], 2),
        (["ingest", SPOTIFY_URL, "--feed", "https://open.spotify.com/feed.rss"], 2),
        (["ingest", SPOTIFY_URL], 2),
        (["transcribe", SPOTIFY_URL, "--provider", "fake"], 3),
    ],
)
def test_spotify_and_feed_option_errors_do_not_request_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    args: list[str],
    exit_code: int,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    requests, _opened = install_responses(monkeypatch, [])

    result = CliRunner().invoke(app, args)

    assert result.exit_code == exit_code
    assert requests == []


def test_spotify_title_changes_invalidate_cache_and_human_output_is_reviewable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"

    def responses(title: str, text: str) -> list[FakeResponse]:
        return [
            oembed(title),
            FakeResponse(
                rss_feed(
                    item(
                        "episode-guid",
                        title,
                        "<podcast:transcript url='/episode.txt' type='text/plain'/>",
                    )
                )
            ),
            FakeResponse(text.encode()),
        ]

    requests, _opened = install_responses(
        monkeypatch,
        responses("Mapped Episode", "First transcript.")
        + responses("Mapped Episode Revised", "Second transcript."),
    )
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["ingest", SPOTIFY_URL, "--feed", feed_url],
    )
    second = runner.invoke(
        app,
        ["ingest", SPOTIFY_URL, "--feed", feed_url, "--json"],
    )

    assert first.exit_code == 0, first.stderr
    assert "Transcript ID:" in first.stdout
    assert second.exit_code == 0, second.stderr
    assert json.loads(second.stdout)["source"]["title"] == "Mapped Episode Revised"
    assert len(requests) == 6


def test_spotify_text_cache_checks_current_content_and_no_cache_bypasses_hit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"

    def response(text: str) -> list[FakeResponse]:
        return [
            oembed("Mapped Episode"),
            FakeResponse(
                rss_feed(
                    item(
                        "episode-guid",
                        "Mapped Episode",
                        "<podcast:transcript url='/episode.txt' type='text/plain'/>",
                    )
                )
            ),
            FakeResponse(text.encode()),
        ]

    requests, _opened = install_responses(
        monkeypatch,
        response("Same transcript.")
        + response("Same transcript.")
        + response("Changed transcript.")
        + response("Changed transcript."),
    )
    runner = CliRunner()
    args = ["ingest", SPOTIFY_URL, "--feed", feed_url, "--json"]
    first = runner.invoke(app, args)
    cached = runner.invoke(app, args)
    changed = runner.invoke(app, args)
    bypassed = runner.invoke(app, [*args, "--no-cache"])

    assert first.exit_code == cached.exit_code == changed.exit_code == bypassed.exit_code == 0
    first_payload = json.loads(first.stdout)
    cached_payload = json.loads(cached.stdout)
    changed_payload = json.loads(changed.stdout)
    bypassed_payload = json.loads(bypassed.stdout)
    assert first_payload["id"] == cached_payload["id"]
    assert "Using cached transcript." in cached.stderr
    assert changed_payload["id"] != cached_payload["id"]
    assert bypassed_payload["id"] == changed_payload["id"]
    assert "Using cached transcript." not in bypassed.stderr
    assert len(requests) == 12


def test_spotify_stt_auto_cache_miss_does_not_select_provider_even_with_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    monkeypatch.setattr("audigest.service.get_provider", get_provider)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key-must-not-be-used")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item(
            "episode-guid",
            "Mapped Episode",
            "<enclosure url='/episode.mp3' type='audio/mpeg' length='5'/>",
        )
    )
    requests, _opened = install_responses(
        monkeypatch,
        [oembed("Mapped Episode"), FakeResponse(feed), FakeResponse(b"audio bytes")],
    )

    result = CliRunner().invoke(app, ["ingest", SPOTIFY_URL, "--feed", feed_url, "--json"])

    assert result.exit_code == 5
    assert "does not select an STT provider" in result.stderr
    assert "synthetic-key" not in result.stderr
    assert requests == [metadata_url(), feed_url, "https://publisher.example/episode.mp3"]


@pytest.mark.parametrize("keep_media", [False, True])
def test_spotify_enclosure_stt_uses_stable_origin_and_existing_media_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    keep_media: bool,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item(
            "episode-guid",
            "Mapped Episode",
            "<enclosure url='/episode?signature=media-private' type='audio/mpeg' length='5'/>",
        )
    )
    requests, opened = install_responses(
        monkeypatch,
        [oembed("Mapped Episode"), FakeResponse(feed), FakeResponse(b"audio bytes")],
    )
    media_paths: list[Path] = []

    class Provider:
        name = "fake"
        model = "fake-spotify-v1"

        def transcribe(self, media_path: Path, *, language: str | None) -> TranscriptDraft:
            media_paths.append(media_path)
            assert media_path.read_bytes() == b"audio bytes"
            return TranscriptDraft(
                provider=self.name,
                model=self.model,
                language=language or "en",
                segments=[TranscriptSegment(start=0, end=1, text="Synthetic words.")],
            )

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: Provider())
    args = ["ingest", SPOTIFY_URL, "--feed", feed_url, "--provider", "fake", "--json"]
    if keep_media:
        args.append("--keep-media")

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"]["type"] == "spotify"
    assert payload["source"]["uri"] == SPOTIFY_URL
    assert payload["origin"]["uri"] == SPOTIFY_URL
    assert requests == [
        metadata_url(),
        feed_url,
        "https://publisher.example/episode?signature=media-private",
    ]
    assert media_paths and not media_paths[0].exists()
    retained = list((cache_dir / "media").glob("*.mp3")) if keep_media else []
    assert len(retained) == int(keep_media)
    assert "media-private" not in result.stdout + result.stderr
    assert all(response.closed for response in opened)


def test_spotify_enclosure_redirect_to_cdn_is_blocked_before_cdn_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item(
            "episode-guid",
            "Mapped Episode",
            "<enclosure url='/episode.mp3' type='audio/mpeg' length='5'/>",
        )
    )
    requests, _opened = install_responses(
        monkeypatch,
        [
            oembed("Mapped Episode"),
            FakeResponse(feed),
            Redirect("https://cdn.scdn.co/audio/private.mp3?signature=private"),
        ],
    )

    result = CliRunner().invoke(
        app,
        ["ingest", SPOTIFY_URL, "--feed", feed_url, "--provider", "fake", "--json"],
    )

    assert result.exit_code == 4
    assert requests == [
        metadata_url(),
        feed_url,
        "https://publisher.example/episode.mp3",
    ]
    assert "signature" not in result.stderr
