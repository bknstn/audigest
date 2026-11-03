"""Local source fingerprinting and URL classification tests."""

import hashlib
from pathlib import Path

import pytest

from audigest.errors import SourceResolutionError
from audigest.sources.detect import detect_source
from audigest.sources.local import LocalFileResolver


def test_local_resolver_fingerprints_original_in_place(tmp_path: Path) -> None:
    media = tmp_path / "sample.wav"
    contents = b"synthetic audio bytes"
    media.write_bytes(contents)

    resolved = LocalFileResolver().resolve(media)

    assert resolved.media_path == media.resolve()
    assert resolved.source_info.type == "local"
    assert resolved.source_info.uri == media.resolve().as_uri()
    assert resolved.source_info.title == "sample"
    assert resolved.content_sha256 == hashlib.sha256(contents).hexdigest()
    assert media.read_bytes() == contents
    assert list(tmp_path.iterdir()) == [media]


def test_local_resolver_accepts_file_uri(tmp_path: Path) -> None:
    media = tmp_path / "sample file.mp3"
    media.write_bytes(b"audio")

    assert LocalFileResolver().resolve(media.as_uri()).media_path == media.resolve()


@pytest.mark.parametrize("suffix", [".pdf", ".jsonl", ".png"])
def test_local_resolver_rejects_unsupported_file_types(tmp_path: Path, suffix: str) -> None:
    media = tmp_path / f"document{suffix}"
    media.write_text("not media", encoding="utf-8")

    with pytest.raises(SourceResolutionError, match="Unsupported local source type"):
        LocalFileResolver().resolve(media)


@pytest.mark.parametrize("suffix", [".txt", ".json", ".srt", ".vtt"])
def test_local_resolver_accepts_transcript_inputs(tmp_path: Path, suffix: str) -> None:
    media = tmp_path / f"transcript{suffix}"
    media.write_text("synthetic transcript input", encoding="utf-8")

    resolved = LocalFileResolver().resolve(media)

    assert resolved.source_info.type == "local"
    assert resolved.media_path == media.resolve()


def test_local_resolver_rejects_missing_files_and_directories(tmp_path: Path) -> None:
    resolver = LocalFileResolver()
    with pytest.raises(SourceResolutionError, match="does not exist"):
        resolver.resolve(tmp_path / "missing.wav")
    with pytest.raises(SourceResolutionError, match="regular media file"):
        resolver.resolve(tmp_path)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://open.spotify.com/episode/abc", "spotify"),
        ("https://example.test/feed.rss", "rss"),
        ("https://example.test/feed?format=rss", "rss"),
        ("https://example.test/audio.mp3?download=1", "direct"),
        ("https://example.test/episode.html", "direct"),
        ("https://example.test/episode", "direct"),
        ("https://example.test/download.bin", "direct"),
    ],
)
def test_detect_source_classifies_recognized_urls(url: str, expected: str) -> None:
    assert detect_source(url) == expected


def test_detect_source_rejects_unsupported_schemes_and_malformed_urls() -> None:
    with pytest.raises(SourceResolutionError, match="Unsupported URL scheme"):
        detect_source("ftp://example.test/audio.mp3")
    with pytest.raises(SourceResolutionError, match="Invalid source URL"):
        detect_source("http://[")


def test_local_resolver_explains_known_but_unimplemented_url_types() -> None:
    with pytest.raises(SourceResolutionError, match="recognized as youtube"):
        LocalFileResolver().resolve("https://youtube.com/watch?v=abc")


def test_local_resolver_rejects_nul_bytes_as_an_invalid_path() -> None:
    with pytest.raises(SourceResolutionError, match="does not exist or cannot be accessed"):
        LocalFileResolver().resolve("\x00.wav")


def test_local_resolver_sanitizes_home_expansion_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_expanduser(self: Path) -> Path:
        raise RuntimeError("unknown local account")

    monkeypatch.setattr(Path, "expanduser", fail_expanduser)
    with pytest.raises(SourceResolutionError, match="does not exist or cannot be accessed"):
        LocalFileResolver().resolve("~/sample.wav")
