"""Read-only CLI access to saved canonical transcripts."""

import json
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from audigest.cli import app


def _configured_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cache_dir = tmp_path / "configured-cache"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'provider = "openai"\ncache_dir = {json.dumps(str(cache_dir))}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config_file))
    monkeypatch.delenv("AUDIGEST_CACHE_DIR", raising=False)
    monkeypatch.delenv("AUDIGEST_STT_PROVIDER", raising=False)
    monkeypatch.delenv("AUDIGEST_LANGUAGE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return cache_dir


def _cache_snapshot(cache_dir: Path) -> dict[Path, tuple[int, bytes]]:
    return {
        path.relative_to(cache_dir): (path.stat().st_mtime_ns, path.read_bytes())
        for path in cache_dir.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("suffix", "content", "origin_kind", "timed"),
    [
        (".txt", "Synthetic saved text.\nSecond line.", "text", False),
        (".md", "# Synthetic Markdown\n\n- First item.\n", "text", False),
        (".srt", "1\n00:00:01,250 --> 00:00:02,500\nSynthetic cue.\n", "captions", True),
    ],
)
def test_show_renders_saved_transcript_without_source_or_provider(
    suffix: str,
    content: str,
    origin_kind: str,
    timed: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = _configured_cache(monkeypatch, tmp_path)
    source = tmp_path / f"saved{suffix}"
    source.write_text(content, encoding="utf-8")

    def unexpected(*_: object, **__: object) -> None:
        pytest.fail("show must not resolve a source, acquire a transcript, or select STT")

    monkeypatch.setattr("audigest.service.get_provider", unexpected)
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])

    assert ingested.exit_code == 0, ingested.stderr
    ingested_payload = json.loads(ingested.stdout)
    transcript_id = ingested_payload["id"]
    item_dir = cache_dir / "items" / transcript_id
    stored_payload = json.loads((item_dir / "transcript.json").read_text(encoding="utf-8"))
    stored_markdown = (item_dir / "transcript.md").read_text(encoding="utf-8")
    assert stored_payload == ingested_payload
    assert stored_payload["origin"]["kind"] == origin_kind
    assert stored_payload["origin"]["uri"] == source.resolve().as_uri()
    assert all(
        (segment["start"] is not None and segment["end"] is not None) == timed
        for segment in stored_payload["segments"]
    )

    source.unlink()
    monkeypatch.setattr("audigest.service.LocalFileResolver.resolve", unexpected)
    monkeypatch.setattr("audigest.service.LocalTranscriptAcquirer.acquire", unexpected)
    before = _cache_snapshot(cache_dir)

    shown_json = runner.invoke(app, ["show", transcript_id, "--json"])
    shown_markdown = runner.invoke(app, ["show", transcript_id])
    repeated_json = runner.invoke(app, ["show", transcript_id, "--json"])

    assert shown_json.exit_code == shown_markdown.exit_code == repeated_json.exit_code == 0
    assert shown_json.stderr == shown_markdown.stderr == repeated_json.stderr == ""
    assert json.loads(shown_json.stdout) == stored_payload
    assert json.loads(repeated_json.stdout) == stored_payload
    assert shown_markdown.stdout == stored_markdown
    assert _cache_snapshot(cache_dir) == before


@pytest.mark.parametrize(
    ("transcript_id", "expected_code", "message"),
    [
        ("a" * 64, 3, "not found in the local cache"),
        ("../outside", 2, "invalid transcript cache id"),
    ],
)
def test_show_handles_missing_and_unsafe_ids_cleanly(
    transcript_id: str,
    expected_code: int,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = _configured_cache(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["show", transcript_id, "--json"])

    assert result.exit_code == expected_code
    assert result.stdout == ""
    assert message in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()
    assert not cache_dir.exists()


@pytest.mark.parametrize("state", ["corrupt", "incomplete"])
def test_show_reports_corrupt_or_incomplete_cache_entries(
    state: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = _configured_cache(monkeypatch, tmp_path)
    transcript_id = "b" * 64
    entry = cache_dir / "items" / transcript_id
    entry.mkdir(parents=True)
    if state == "corrupt":
        (entry / "source.json").write_text(
            '{"type":"local","uri":"file:///synthetic.wav"}', encoding="utf-8"
        )
        (entry / "transcript.json").write_text("{invalid", encoding="utf-8")
        (entry / "transcript.md").write_text("# Transcript\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["show", transcript_id, "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "incomplete or corrupt" in result.stderr.lower() or "corrupt or unreadable" in (
        result.stderr.lower()
    )


def test_show_only_exposes_json_as_an_output_option() -> None:
    result = CliRunner().invoke(app, ["show", "--help"])

    assert result.exit_code == 0
    output = Text.from_ansi(result.stdout).plain
    assert "--json" in output
    assert "--provider" not in output
    assert "--no-cache" not in output
