"""Offline CLI coverage for bounded RSS episode ingestion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.errors import ProviderError
from audigest.models import (
    SourceInfo,
    Transcript,
    TranscriptDraft,
    TranscriptOrigin,
    TranscriptSegment,
)


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
        pytest.fail("RSS transcript routes must not select an STT provider")

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


def rss_feed(items: str, *, language: str = "en") -> bytes:
    return f"""<?xml version='1.0' encoding='UTF-8'?>
    <rss version='2.0' xmlns:podcast='https://podcastindex.org/namespace/1.0'>
      <channel><title>Example Feed</title><link>https://publisher.example/</link>
      <description>Synthetic feed</description><language>{language}</language>
      {items}</channel>
    </rss>""".encode()


def item(guid: str, body: str) -> str:
    return f"<item><guid>{guid}</guid><title>Episode {guid}</title>{body}</item>"


def test_one_episode_rss_redirect_resolves_relative_podcast_json_and_keeps_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss?signature=feed-private"
    feed = rss_feed(
        item(
            "episode-1",
            "<link>https://publisher.example/episodes/1</link>"
            "<author>Episode Host</author><pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>"
            "<podcast:transcript url='../transcripts/one' type='application/json' "
            "language='en' />",
        )
    )
    podcast_json = (
        b'{"version":"1.0.0","segments":['
        b'{"body":"Timed segment.","startTime":1,"endTime":3,"speaker":"Host"},'
        b'{"body":"Start-only is untimed.","startTime":4}]}'
    )
    requests, opened = install_responses(
        monkeypatch,
        [
            Redirect("https://cdn.example/shows/current/feed.rss?redirect=private"),
            FakeResponse(feed),
            FakeResponse(podcast_json),
        ],
    )

    result = CliRunner().invoke(app, ["ingest", feed_url, "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"]["type"] == "rss"
    assert payload["source"]["uri"].startswith("https://publisher.example/feed.rss#episode-")
    assert payload["source"]["title"] == "Episode episode-1"
    assert payload["source"]["author"] == "Episode Host"
    assert payload["provider"] == "podcast-json"
    assert payload["origin"]["uri"] == "https://cdn.example/shows/transcripts/one"
    assert [(segment["start"], segment["end"]) for segment in payload["segments"]] == [
        (1.0, 3.0),
        (None, None),
    ]
    assert requests == [
        feed_url,
        "https://cdn.example/shows/current/feed.rss?redirect=private",
        "https://cdn.example/shows/transcripts/one",
    ]
    assert all(response.closed for response in opened)
    assert "private" not in result.stdout + result.stderr


def test_extensionless_feed_is_fetched_once_and_unrelated_bad_item_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed?signature=private"
    bad_other_item = item(
        "other",
        "<podcast:transcript />",
    )
    selected = item(
        "selected",
        "<link>https://publisher.example/episodes/selected?episode=private</link>"
        "<podcast:transcript url='/selected.txt?token=secret' type='text/plain' language='fr'/>"
        "<podcast:transcript url='/ignored.json' type='application/json' "
        "language='en' rel='captions'/>",
    )
    feed = rss_feed(bad_other_item + selected)
    requests, opened = install_responses(
        monkeypatch,
        [FakeResponse(feed), FakeResponse(b"Selected selected transcript.")],
    )

    result = CliRunner().invoke(
        app,
        [
            "ingest",
            feed_url,
            "--episode",
            "https://publisher.example/episodes/selected?episode=private",
            "--language",
            "en",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "Selected selected transcript."
    assert payload["language"] == "fr"
    assert payload["origin"]["uri"] == "https://publisher.example/selected.txt"
    assert payload["source"]["type"] == "rss"
    assert requests == [feed_url, "https://publisher.example/selected.txt?token=secret"]
    assert all(response.closed for response in opened)
    assert "private" not in result.stdout + result.stderr
    assert "secret" not in result.stdout + result.stderr


def test_extensionless_rss_without_selector_autoselects_its_only_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/latest?signature=private"
    feed = rss_feed(item("only", "<podcast:transcript url='/latest.txt' type='text/plain'/>"))
    requests, _opened = install_responses(
        monkeypatch,
        [FakeResponse(feed), FakeResponse(b"Single extensionless episode.")],
    )

    result = CliRunner().invoke(app, ["ingest", feed_url, "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "Single extensionless episode."
    assert payload["source"]["type"] == "rss"
    assert requests == [feed_url, "https://publisher.example/latest.txt"]


def test_requested_language_selects_among_text_transcripts_before_caption_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item(
            "one",
            "<podcast:transcript url='/fr.txt' type='text/plain' language='fr'/>"
            "<podcast:transcript url='/en.txt' type='text/plain' language='en'/>"
            "<podcast:transcript url='/captions.vtt' type='text/vtt' rel='captions'/>",
        ),
        language="fr",
    )
    requests, _opened = install_responses(
        monkeypatch,
        [FakeResponse(feed), FakeResponse(b"English text transcript.")],
    )

    result = CliRunner().invoke(
        app,
        ["ingest", feed_url, "--language", "en", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "English text transcript."
    assert payload["language"] == "en"
    assert requests == [feed_url, "https://publisher.example/en.txt"]


def test_multiple_items_require_an_exact_selector_without_fetching_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item("one", "<podcast:transcript url='/one.txt' type='text/plain'/>")
        + item("two", "<enclosure url='/two.mp3' type='audio/mpeg' length='4'/>")
    )
    requests, opened = install_responses(monkeypatch, [FakeResponse(feed)])

    result = CliRunner().invoke(app, ["ingest", feed_url, "--json"])

    assert result.exit_code == 3
    assert "multiple episodes" in result.stderr
    assert requests == [feed_url]
    assert all(response.closed for response in opened)


def test_missing_episode_selector_and_ambiguous_matching_selector_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss?feed-secret=private"
    repeated = "https://publisher.example/episode?selector-secret=private"
    feed = rss_feed(
        item("one", f"<link>{repeated}</link>") + item("two", f"<link>{repeated}</link>")
    )
    requests, _opened = install_responses(
        monkeypatch,
        [FakeResponse(feed), FakeResponse(feed), FakeResponse(feed)],
    )

    missing = CliRunner().invoke(app, ["ingest", feed_url, "--json"])
    ambiguous = CliRunner().invoke(app, ["ingest", feed_url, "--episode", repeated, "--json"])
    unmatched = CliRunner().invoke(
        app,
        ["ingest", feed_url, "--episode", "private-guid-does-not-exist", "--json"],
    )

    assert missing.exit_code == 3
    assert ambiguous.exit_code == 3
    assert unmatched.exit_code == 3
    assert "selector-secret" not in missing.stderr + ambiguous.stderr + unmatched.stderr
    assert "private" not in missing.stderr + ambiguous.stderr + unmatched.stderr
    assert requests == [feed_url, feed_url, feed_url]


def test_episode_link_selector_matches_the_full_link_fragment_and_query():
    from audigest.sources.remote import BoundedRemoteDocument, remote_url_reference
    from audigest.sources.rss_acquisition import select_rss_episode

    feed_url = "https://publisher.example/feed.rss?token=private"
    feed = rss_feed(
        item("one", "<link>https://publisher.example/episode?item=1#part-one</link>")
        + item("two", "<link>https://publisher.example/episode?item=1#part-two</link>")
    )

    selected = select_rss_episode(
        remote_url_reference(feed_url),
        BoundedRemoteDocument(content=feed, final_url="https://publisher.example/feed.rss"),
        selector="https://publisher.example/episode?item=1#part-two",
    )

    assert selected.item.guid == "two"


def test_extensionless_html_with_rss_example_uses_publisher_parser_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    page_url = "https://publisher.example/episode"
    html = b"<html><body><script>const sample = '<rss version=2.0>';</script></body></html>"
    requests, opened = install_responses(monkeypatch, [FakeResponse(html)])

    result = CliRunner().invoke(app, ["ingest", page_url, "--json"])

    assert result.exit_code == 3
    assert "RSS 2.0" not in result.stderr
    assert requests == [page_url]
    assert all(response.closed for response in opened)


def test_episode_option_rejects_local_and_direct_media_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    requests, _opened = install_responses(monkeypatch, [])

    local_result = CliRunner().invoke(
        app,
        ["ingest", str(tmp_path / "missing.mp3"), "--episode", "private-guid"],
    )
    media_result = CliRunner().invoke(
        app,
        ["ingest", "https://publisher.example/audio.mp3", "--episode", "private-guid"],
    )
    transcript_result = CliRunner().invoke(
        app,
        ["ingest", "https://publisher.example/transcript.txt", "--episode", "private-guid"],
    )
    youtube_result = CliRunner().invoke(
        app,
        ["ingest", "https://youtu.be/abcdefghijk", "--episode", "private-guid"],
    )

    assert local_result.exit_code == 2
    assert media_result.exit_code == 2
    assert transcript_result.exit_code == 2
    assert youtube_result.exit_code == 2
    assert requests == []
    assert "private-guid" not in (
        local_result.stderr + media_result.stderr + transcript_result.stderr + youtube_result.stderr
    )


def test_transcribe_rejects_rss_before_any_request_or_provider_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    requests, _opened = install_responses(monkeypatch, [])

    result = CliRunner().invoke(app, ["transcribe", "https://publisher.example/feed.rss"])

    assert result.exit_code == 3
    assert "use 'ingest' for RSS feeds" in result.stderr
    assert requests == []


def test_supplied_transcript_skips_selected_episode_transcript_and_media_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    transcript_path = tmp_path / "supplied.txt"
    transcript_path.write_text("Provided RSS transcript.", encoding="utf-8")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item(
            "one",
            "<podcast:transcript url='/selected.txt' type='text/plain'/>"
            "<enclosure url='/episode.mp3' type='audio/mpeg' length='7'/>",
        )
    )
    requests, _opened = install_responses(monkeypatch, [FakeResponse(feed)])

    result = CliRunner().invoke(
        app,
        ["ingest", feed_url, "--transcript", str(transcript_path), "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "Provided RSS transcript."
    assert payload["source"]["type"] == "rss"
    assert requests == [feed_url]


def test_selected_broken_transcript_declaration_fails_without_media_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item(
            "one",
            "<podcast:transcript url='/broken.txt'/>"
            "<enclosure url='/episode.mp3' type='audio/mpeg' length='7'/>",
        )
    )
    requests, _opened = install_responses(monkeypatch, [FakeResponse(feed)])

    result = CliRunner().invoke(app, ["ingest", feed_url, "--json"])

    assert result.exit_code == 3
    assert "declaration is incomplete" in result.stderr
    assert requests == [feed_url]


@pytest.mark.parametrize(
    ("resource", "message"),
    [
        (
            "<enclosure url='/episode.mp3' type='audio/mpeg' length='" + ("9" * 5000) + "'/>",
            "exceeds the 100 MiB media limit",
        ),
        (
            "".join(
                "<podcast:transcript url='/transcript.txt' type='text/plain'/>" for _ in range(17)
            ),
            "transcript declaration limit",
        ),
    ],
)
def test_selected_episode_resource_limits_fail_before_resource_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resource: str,
    message: str,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    requests, _opened = install_responses(
        monkeypatch,
        [FakeResponse(rss_feed(item("one", resource)))],
    )

    result = CliRunner().invoke(app, ["ingest", feed_url, "--json"])

    assert result.exit_code == 3
    assert message in result.stderr
    assert requests == [feed_url]


def test_declared_json_captions_use_podcast_format_with_caption_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item(
            "one",
            "<podcast:transcript url='/captions' type='application/json' rel='captions' />",
        )
    )
    body = b'{"version":"1.0.0","segments":[{"body":"Caption content."}]}'
    requests, _opened = install_responses(monkeypatch, [FakeResponse(feed), FakeResponse(body)])

    result = CliRunner().invoke(app, ["ingest", feed_url, "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["provider"] == "podcast-json"
    assert payload["origin"]["kind"] == "captions"
    assert payload["origin"]["format"] == "podcast-json"
    assert requests == [feed_url, "https://publisher.example/captions"]


def test_explicitly_declared_podcast_html_reads_paragraphs_only_and_stays_untimed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item(
            "one",
            "<podcast:transcript url='/transcript' type='text/html' />",
        )
    )
    html = (
        b"<html><body><main><p><cite>Host</cite>First<br>line"
        b"<time>00:05</time>end.</p></main><footer><p>Boilerplate</p></footer></body></html>"
    )
    requests, _opened = install_responses(
        monkeypatch,
        [FakeResponse(feed), FakeResponse(html)],
    )

    result = CliRunner().invoke(app, ["ingest", feed_url, "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["provider"] == "podcast-html"
    assert payload["text"] == "First line end."
    assert payload["segments"][0]["speaker"] == "Host"
    assert (payload["segments"][0]["start"], payload["segments"][0]["end"]) == (None, None)
    assert payload["origin"]["uri"] == "https://publisher.example/transcript"
    assert requests == [feed_url, "https://publisher.example/transcript"]


def test_malformed_canonical_json_with_podcast_version_field_is_not_reinterpreted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item("one", "<podcast:transcript url='/transcript.json' type='application/json' />")
    )
    canonical = Transcript(
        id="original-id",
        source=SourceInfo(type="direct", uri="https://original.example/source"),
        origin=TranscriptOrigin(
            kind="text",
            uri="https://original.example/original.txt",
            format="txt",
        ),
        provider="embedded-provider",
        model="embedded-model",
        language="es",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        segments=[TranscriptSegment(text="Embedded canonical content.")],
        text="Embedded canonical content.",
    ).model_dump(mode="json")
    canonical["version"] = "1.0.0"
    requests, _opened = install_responses(
        monkeypatch,
        [
            FakeResponse(feed),
            FakeResponse(json.dumps(canonical).encode()),
        ],
    )

    result = CliRunner().invoke(app, ["ingest", feed_url, "--json"])

    assert result.exit_code == 3
    assert "Canonical transcript JSON is invalid" in result.stderr
    assert requests == [feed_url, "https://publisher.example/transcript.json"]


def test_selected_transcript_cache_ignores_changes_to_unrelated_episode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    selected = item(
        "selected",
        "<podcast:transcript url='/selected.txt' type='text/plain'/>",
    )
    first_feed = rss_feed(item("other", "<title>Version one</title>") + selected)
    second_feed = rss_feed(item("other", "<title>Version two</title>") + selected)
    third_feed = rss_feed(item("other", "<title>Version three</title>") + selected)
    requests, _opened = install_responses(
        monkeypatch,
        [
            FakeResponse(first_feed),
            FakeResponse(b"Stable selected transcript."),
            FakeResponse(second_feed),
            FakeResponse(b"Stable selected transcript."),
            FakeResponse(third_feed),
            FakeResponse(b"Changed selected transcript."),
        ],
    )
    args = ["ingest", feed_url, "--episode", "selected", "--json"]

    first = CliRunner().invoke(app, args)
    second = CliRunner().invoke(app, args)
    third = CliRunner().invoke(app, args)

    assert first.exit_code == 0, first.stderr
    assert second.exit_code == 0, second.stderr
    assert third.exit_code == 0, third.stderr
    assert json.loads(first.stdout)["id"] == json.loads(second.stdout)["id"]
    assert json.loads(first.stdout)["id"] != json.loads(third.stdout)["id"]
    assert "Using cached transcript." in second.stderr
    assert requests == [feed_url, "https://publisher.example/selected.txt"] * 3


def test_human_rss_ingest_keeps_existing_id_and_artifact_output_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(item("one", "<podcast:transcript url='/one.txt' type='text/plain'/>"))
    requests, _opened = install_responses(
        monkeypatch,
        [FakeResponse(feed), FakeResponse(b"Human mode transcript.")],
    )

    result = CliRunner().invoke(app, ["ingest", feed_url])

    assert result.exit_code == 0, result.stderr
    assert "Transcript ID:" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout
    assert "Human mode transcript." not in result.stdout
    assert requests == [feed_url, "https://publisher.example/one.txt"]


def test_rss_media_redirect_to_spotify_cdn_is_blocked_without_following_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    media_url = "https://media.publisher.example/episode.mp3"
    redirect = FakeResponse(b"redirect body must be closed, not read")
    feed = rss_feed(
        item(
            "one",
            "<enclosure url='https://media.publisher.example/episode.mp3' "
            "type='audio/mpeg' length='7'/>",
        )
    )
    requests, opened = install_responses(
        monkeypatch,
        [FakeResponse(feed), Redirect("https://cdn.scdn.co/episode.mp3", redirect)],
    )
    temporary_root = tmp_path / "tracked-rss-download"

    class TemporaryDirectory:
        def __init__(self, *, prefix: str) -> None:
            _ = prefix

        def __enter__(self) -> str:
            temporary_root.mkdir()
            return str(temporary_root)

        def __exit__(self, *_: object) -> None:
            temporary_root.rmdir()

    monkeypatch.setattr("audigest.sources.remote.tempfile.TemporaryDirectory", TemporaryDirectory)

    result = CliRunner().invoke(app, ["ingest", feed_url, "--provider", "fake", "--json"])

    assert result.exit_code == 4
    assert requests == [feed_url, media_url]
    assert redirect.closed
    assert all(response.closed for response in opened)
    assert not temporary_root.exists()


def test_rss_media_provider_failure_cleans_temporary_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    feed_url = "https://publisher.example/feed.rss"
    feed = rss_feed(
        item(
            "one",
            "<enclosure url='/episode.mp3' type='audio/mpeg' length='7'/>",
        )
    )
    requests, opened = install_responses(
        monkeypatch,
        [FakeResponse(feed), FakeResponse(b"media bytes")],
    )
    received_paths: list[Path] = []

    class Provider:
        name = "fake"
        model = "fake-rss-v1"

        def transcribe(self, media_path: Path, *, language: str | None = None) -> TranscriptDraft:
            _ = language
            received_paths.append(media_path)
            raise ProviderError("synthetic provider failure")

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: Provider())
    result = CliRunner().invoke(
        app,
        ["ingest", feed_url, "--provider", "fake", "--json"],
    )

    assert result.exit_code == 5
    assert requests == [feed_url, "https://publisher.example/episode.mp3"]
    assert all(response.closed for response in opened)
    assert len(received_paths) == 1
    assert not received_paths[0].exists()


@pytest.mark.parametrize("keep_media", [False, True])
def test_rss_media_enclosure_uses_episode_provenance_and_cleans_temp_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    keep_media: bool,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    feed_url = "https://publisher.example/feed.rss?feed-secret=private"
    feed = rss_feed(
        item(
            "one",
            "<link>https://publisher.example/episode/one</link>"
            "<enclosure url='https://media.example/audio' type='audio/mpeg' length='7' />",
        )
    )
    requests, opened = install_responses(
        monkeypatch,
        [FakeResponse(feed), FakeResponse(b"media bytes")],
    )
    received_paths: list[Path] = []

    class Provider:
        name = "fake"
        model = "fake-rss-v1"

        def transcribe(self, media_path: Path, *, language: str | None = None) -> TranscriptDraft:
            _ = language
            received_paths.append(media_path)
            return TranscriptDraft(
                provider=self.name,
                model=self.model,
                segments=[TranscriptSegment(text="Synthetic RSS media transcript.")],
            )

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: Provider())
    args = ["ingest", feed_url, "--provider", "fake", "--json"]
    if keep_media:
        args.append("--keep-media")

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"]["type"] == "rss"
    assert payload["origin"]["kind"] == "stt"
    assert payload["origin"]["uri"] == payload["source"]["uri"]
    assert "feed-secret" not in result.stdout + result.stderr
    assert requests == [feed_url, "https://media.example/audio"]
    assert all(response.closed for response in opened)
    assert len(received_paths) == 1
    assert not received_paths[0].exists()
    retained = list((cache_dir / "media").glob("*.mp3")) if keep_media else []
    assert len(retained) == int(keep_media)
    if retained:
        assert retained[0].read_bytes() == b"media bytes"
