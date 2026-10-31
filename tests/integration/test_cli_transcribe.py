"""End-to-end tests for the local fake-provider CLI slice."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.errors import MissingDependencyError
from audigest.models import SourceInfo, Transcript, TranscriptOrigin, TranscriptSegment
from audigest.sources.local import LocalFileResolver
from audigest.storage import FileTranscriptStore, cache_id_for
from audigest.stt.openai import OpenAITranscriptionProvider
from audigest.stt.router import get_provider

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample.wav"


@pytest.fixture
def command() -> str:
    """Default transcript-first command; media tests override to exercise both paths."""
    return "ingest"


def prepare_environment(monkeypatch: pytest.MonkeyPatch, cache_dir: Path) -> None:
    monkeypatch.setenv("AUDIGEST_CACHE_DIR", str(cache_dir))
    config_file = cache_dir.parent / "config.toml"
    config_file.write_text("")
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config_file))
    monkeypatch.delenv("AUDIGEST_STT_PROVIDER", raising=False)
    monkeypatch.delenv("AUDIGEST_LANGUAGE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def make_canonical_transcript(text: str = "Synthetic canonical transcript.") -> Transcript:
    return Transcript(
        id="authored-source-id",
        source=SourceInfo(
            type="local",
            uri="file:///synthetic/original.wav",
            title="Original recording",
            author="Synthetic speaker",
        ),
        provider="imported-provider",
        model="imported-model",
        language="fr",
        created_at=datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC),
        segments=[
            TranscriptSegment(
                start=1.0,
                end=2.5,
                text=text,
                speaker="Synthetic speaker",
            )
        ],
    )


def test_help_and_command_help_work() -> None:
    runner = CliRunner()

    root_help = runner.invoke(app, ["--help"])
    ingest_help = runner.invoke(app, ["ingest", "--help"])
    transcribe_help = runner.invoke(app, ["transcribe", "--help"])

    assert root_help.exit_code == 0
    assert "Usage: audigest" in root_help.stdout
    assert "ingest" in root_help.stdout
    assert "show" in root_help.stdout
    assert "transcribe" in root_help.stdout
    assert ingest_help.exit_code == 0
    assert transcribe_help.exit_code == 0
    for option in ("--provider", "--language", "--json", "--no-cache", "--keep-media"):
        assert option in ingest_help.stdout
        assert option in transcribe_help.stdout
    assert "canonical JSON" in ingest_help.stdout
    assert "Timing may be" in ingest_help.stdout
    assert "unavailable." in ingest_help.stdout
    assert "STT fallback" in ingest_help.stdout
    assert "Explicitly run STT" in transcribe_help.stdout
    assert "sidecars are ignored" in transcribe_help.stdout


@pytest.mark.parametrize(
    ("first_command", "second_command"),
    [("ingest", "transcribe"), ("transcribe", "ingest")],
)
def test_commands_share_cached_transcripts(
    first_command: str,
    second_command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    runner = CliRunner()

    first = runner.invoke(
        app,
        [first_command, str(FIXTURE), "--provider", "fake", "--json"],
    )
    second = runner.invoke(
        app,
        [second_command, str(FIXTURE), "--provider", "auto", "--json"],
    )

    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.stdout)["id"] == json.loads(second.stdout)["id"]
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert "Using cached transcript." in second.stderr


def test_openai_cli_fallback_persists_and_replays_without_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    synthetic_key = "synthetic-cli-test-key-never-valid"
    monkeypatch.setenv("OPENAI_API_KEY", synthetic_key)
    calls = 0
    request: dict[str, Any] = {}

    class MockTranscriptions:
        def create(self, **kwargs: Any) -> dict[str, Any]:
            request.update(kwargs)
            assert kwargs["file"].read() == FIXTURE.read_bytes()
            return {"text": "Synthetic CLI transcription.", "languages": [{"code": "en"}]}

    class MockClient:
        def __init__(self) -> None:
            self.audio = type("Audio", (), {})()
            self.audio.transcriptions = MockTranscriptions()
            self.closed = False

        def __enter__(self) -> "MockClient":
            return self

        def __exit__(self, *args: Any) -> None:
            _ = args
            self.closed = True

    client = MockClient()

    def make_client(**options: Any) -> MockClient:
        nonlocal calls
        calls += 1
        assert options["base_url"] == "https://api.openai.com/v1"
        assert options["timeout"] == 60.0
        assert options["max_retries"] == 0
        return client

    monkeypatch.setattr(
        "audigest.stt.router.OpenAITranscriptionProvider",
        lambda: OpenAITranscriptionProvider(client_factory=make_client),
    )
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["ingest", str(FIXTURE), "--provider", "openai", "--language", "en", "--json"],
    )

    assert first.exit_code == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["provider"] == "openai"
    assert payload["model"] == "gpt-transcribe"
    assert payload["language"] == "en"
    assert payload["text"] == "Synthetic CLI transcription."
    assert payload["origin"] == {
        "kind": "stt",
        "uri": FIXTURE.resolve().as_uri(),
        "format": "wav",
    }
    assert payload["segments"] == [
        {"start": None, "end": None, "text": "Synthetic CLI transcription.", "speaker": None}
    ]
    assert request["extra_body"] == {"languages": ["en"]}
    assert client.closed
    assert synthetic_key not in first.stdout + first.stderr
    assert all(
        synthetic_key not in path.read_text(encoding="utf-8")
        for path in cache_dir.rglob("*")
        if path.is_file()
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    second = runner.invoke(
        app,
        ["transcribe", str(FIXTURE), "--provider", "openai", "--language", "en", "--json"],
    )

    assert second.exit_code == 0, second.stderr
    assert json.loads(second.stdout) == payload
    assert "Using cached transcript." in second.stderr
    assert synthetic_key not in second.stdout + second.stderr
    assert calls == 1


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_auto_does_not_use_openai_key_implicitly(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    synthetic_key = "synthetic-auto-test-key-never-valid"
    monkeypatch.setenv("OPENAI_API_KEY", synthetic_key)

    def unexpected_openai_client(*_: Any, **__: Any) -> Any:
        pytest.fail("auto must not create an OpenAI client, even when a key is set")

    monkeypatch.setattr(OpenAITranscriptionProvider, "_make_client", unexpected_openai_client)

    result = CliRunner().invoke(
        app,
        [command, str(FIXTURE), "--provider", "auto", "--json"],
    )

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "does not select an STT provider" in result.stderr
    assert synthetic_key not in result.stderr


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_json_mode_is_pure_and_persists_all_three_artifacts(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [command, str(FIXTURE), "--provider", "fake", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    transcript_id = payload["id"]
    assert payload["schema_version"] == "1"
    assert payload["source"]["type"] == "local"
    assert payload["provider"] == "fake"
    assert "Resolving source" in result.stderr
    assert "Transcript ID" not in result.stdout
    item_dir = cache_dir / "items" / transcript_id
    assert (item_dir / "source.json").is_file()
    assert json.loads((item_dir / "transcript.json").read_text()) == payload
    assert (item_dir / "transcript.md").is_file()


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_human_mode_sends_progress_to_stderr_and_result_paths_to_stdout(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")

    result = CliRunner().invoke(
        app,
        [command, str(FIXTURE), "--provider", "fake"],
    )

    assert result.exit_code == 0, result.stderr
    assert "Transcription complete." in result.stderr
    assert "Transcript ID:" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout


def test_direct_canonical_json_preserves_transcript_content_without_selecting_provider(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    transcript_file = tmp_path / "transcript.json"
    canonical = make_canonical_transcript()
    legacy_payload = canonical.model_dump(mode="json")
    legacy_payload.pop("origin")
    transcript_file.write_text(json.dumps(legacy_payload), encoding="utf-8")

    result = CliRunner().invoke(app, [command, str(transcript_file), "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["id"] != canonical.id
    assert payload["source"] == canonical.source.model_dump(mode="json")
    assert payload["provider"] == canonical.provider
    assert payload["model"] == canonical.model
    assert payload["language"] == canonical.language
    assert payload["segments"][0]["speaker"] == "Synthetic speaker"
    assert payload["segments"][0]["start"] == 1.0
    assert payload["text"] == canonical.text
    assert payload["origin"] == {
        "kind": "canonical",
        "uri": transcript_file.resolve().as_uri(),
        "format": "json",
    }
    assert "Using existing local transcript" in result.stderr


def test_embedded_canonical_origin_survives_cache_round_trip(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    transcript_file = tmp_path / "with-origin.json"
    embedded_origin = TranscriptOrigin(
        kind="captions", uri="file:///synthetic/captions.vtt", format="vtt"
    )
    canonical = make_canonical_transcript().model_copy(update={"origin": embedded_origin})
    transcript_file.write_text(canonical.model_dump_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, [command, str(transcript_file), "--provider", "auto", "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["origin"] == embedded_origin.model_dump(mode="json")
    assert payload["source"] == canonical.source.model_dump(mode="json")
    assert payload["provider"] == canonical.provider
    assert payload["model"] == canonical.model
    assert payload["language"] == canonical.language
    assert payload["segments"] == canonical.model_dump(mode="json")["segments"]
    assert payload["created_at"] != canonical.model_dump(mode="json")["created_at"]
    stored = json.loads(
        (tmp_path / "cache" / "items" / payload["id"] / "transcript.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["origin"] == embedded_origin.model_dump(mode="json")
    replay = runner.invoke(app, [command, str(transcript_file), "--provider", "fake", "--json"])
    assert replay.exit_code == 0, replay.stderr
    assert json.loads(replay.stdout) == payload
    assert "Using cached transcript." in replay.stderr


def test_utf8_text_is_acquired_as_untimed_transcript_even_with_provider_option(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    text_file = tmp_path / "notes.txt"
    text_file.write_text(
        "Élise describes a fresh start.\nA short second sentence.", encoding="utf-8"
    )

    result = CliRunner().invoke(
        app,
        [command, str(text_file), "--provider", "openai", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["provider"] == "text"
    assert "Élise describes a fresh start." in payload["text"]
    assert "A short second sentence." in payload["text"]
    assert all(
        segment["start"] is None and segment["end"] is None for segment in payload["segments"]
    )
    assert "Using existing local transcript" in result.stderr


def test_markdown_input_preserves_content_origin_and_cache_behavior(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    markdown_file = tmp_path / "notes.md"
    authored = "# Żółć и 中文\n\n- **First** line.\n\n```text\nraw code\n```\n"
    markdown_file.write_bytes(b"\xef\xbb\xbf" + authored.encode("utf-8"))
    runner = CliRunner()

    first = runner.invoke(app, [command, str(markdown_file), "--provider", "openai", "--json"])
    cached = runner.invoke(app, [command, str(markdown_file), "--provider", "auto", "--json"])

    assert first.exit_code == cached.exit_code == 0, first.stderr + cached.stderr
    first_payload = json.loads(first.stdout)
    assert first_payload["provider"] == "text"
    assert first_payload["text"] == authored
    assert first_payload["segments"] == [
        {"start": None, "end": None, "text": authored, "speaker": None}
    ]
    assert first_payload["origin"] == {
        "kind": "text",
        "uri": markdown_file.resolve().as_uri(),
        "format": "md",
    }
    assert json.loads(cached.stdout) == first_payload
    assert "Using cached transcript." in cached.stderr
    markdown_artifact = cache_dir / "items" / first_payload["id"] / "transcript.md"
    rendered_markdown = markdown_artifact.read_text(encoding="utf-8")
    assert "| Origin kind | text |" in rendered_markdown
    assert f"| Origin URI | {markdown_file.resolve().as_uri()} |" in rendered_markdown

    updated = authored + "\nA changed final paragraph.\n"
    markdown_file.write_text(updated, encoding="utf-8")
    changed = runner.invoke(
        app,
        [command, str(markdown_file), "--provider", "openai", "--json"],
    )

    assert changed.exit_code == 0, changed.stderr
    changed_payload = json.loads(changed.stdout)
    assert changed_payload["provider"] == "text"
    assert changed_payload["text"] == updated
    assert changed_payload["id"] != first_payload["id"]
    assert "Using cached transcript." not in changed.stderr

    no_cache = runner.invoke(
        app,
        [command, str(markdown_file), "--provider", "openai", "--json", "--no-cache"],
    )
    assert no_cache.exit_code == 0, no_cache.stderr
    no_cache_payload = json.loads(no_cache.stdout)
    assert no_cache_payload["id"] == changed_payload["id"]
    assert no_cache_payload["created_at"] != changed_payload["created_at"]
    assert "Using cached transcript." not in no_cache.stderr

    replay = runner.invoke(app, [command, str(markdown_file), "--provider", "auto", "--json"])
    assert replay.exit_code == 0, replay.stderr
    assert json.loads(replay.stdout) == no_cache_payload
    assert "Using cached transcript." in replay.stderr


@pytest.mark.parametrize(
    ("suffix", "content", "expected_start", "expected_text"),
    [
        (".srt", "1\n00:00:01,250 --> 00:00:02,500\nSRT caption.\n", 1.25, "SRT caption."),
        (".vtt", "WEBVTT\n\n00:00:03.000 --> 00:00:04.500\nVTT caption.\n", 3.0, "VTT caption."),
    ],
)
def test_direct_caption_files_preserve_timing_without_stt(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
    content: str,
    expected_start: float,
    expected_text: str,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    caption_file = tmp_path / f"captions{suffix}"
    caption_file.write_text(content, encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [command, str(caption_file), "--provider", "openai", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["provider"] == "captions"
    assert payload["origin"] == {
        "kind": "captions",
        "uri": caption_file.resolve().as_uri(),
        "format": suffix[1:],
    }
    assert payload["segments"][0]["start"] == expected_start
    assert payload["segments"][0]["text"] == expected_text


def test_media_sidecar_order_prefers_canonical_json_first(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    media_file = tmp_path / "episode.wav"
    shutil.copyfile(FIXTURE, media_file)
    canonical_sidecar = media_file.with_suffix(".transcript.json")
    sidecar_transcript = make_canonical_transcript("Canonical sidecar wins.")
    canonical_sidecar.write_text(sidecar_transcript.model_dump_json(), encoding="utf-8")
    media_file.with_suffix(".vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nVTT loses.\n",
        encoding="utf-8",
    )
    media_file.with_suffix(".srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nSRT loses.\n",
        encoding="utf-8",
    )
    media_file.with_suffix(".txt").write_text("Text loses.\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [command, str(media_file), "--provider", "openai", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "Canonical sidecar wins."
    assert payload["source"] == sidecar_transcript.source.model_dump(mode="json")


@pytest.mark.parametrize(
    ("sidecars", "expected_text", "expected_provider"),
    [
        (
            {".vtt", ".srt", ".txt"},
            "VTT wins.",
            "captions",
        ),
        ({".srt", ".txt"}, "SRT wins.", "captions"),
        ({".txt"}, "TXT wins.", "text"),
    ],
)
def test_media_sidecars_follow_vtt_srt_text_order(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecars: set[str],
    expected_text: str,
    expected_provider: str,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    media_file = tmp_path / "episode.wav"
    shutil.copyfile(FIXTURE, media_file)
    sidecar_content = {
        ".vtt": "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nVTT wins.\n",
        ".srt": "1\n00:00:00,000 --> 00:00:01,000\nSRT wins.\n",
        ".txt": "TXT wins.",
    }
    for suffix in sidecars:
        media_file.with_suffix(suffix).write_text(sidecar_content[suffix], encoding="utf-8")

    result = CliRunner().invoke(app, [command, str(media_file), "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == expected_text
    assert payload["provider"] == expected_provider
    selected_suffix = next(suffix for suffix in (".vtt", ".srt", ".txt") if suffix in sidecars)
    assert payload["origin"] == {
        "kind": "captions" if selected_suffix in {".vtt", ".srt"} else "text",
        "uri": media_file.with_suffix(selected_suffix).resolve().as_uri(),
        "format": selected_suffix[1:],
    }


def test_malformed_existing_sidecar_fails_without_falling_back_to_stt(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    media_file = tmp_path / "episode.wav"
    shutil.copyfile(FIXTURE, media_file)
    media_file.with_suffix(".transcript.json").write_text("{ malformed", encoding="utf-8")
    media_file.with_suffix(".vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nValid lower-priority caption.\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [command, str(media_file), "--provider", "fake", "--json"],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "transcript" in result.stderr.lower() or "json" in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()
    assert not (cache_dir / "items").exists()


def test_explicit_transcribe_ignores_malformed_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    media_file = tmp_path / "episode.wav"
    shutil.copyfile(FIXTURE, media_file)
    media_file.with_suffix(".transcript.json").write_text("{ malformed", encoding="utf-8")
    fake = get_provider("fake")
    provider_calls = 0

    class SpyProvider:
        name = fake.name
        model = fake.model

        def transcribe(self, media_path: Path, *, language: str | None = None) -> Any:
            nonlocal provider_calls
            provider_calls += 1
            return fake.transcribe(media_path, language=language)

    def unexpected_acquisition(*_: object, **__: object) -> None:
        pytest.fail("explicit transcribe must not inspect transcript sidecars")

    monkeypatch.setattr("audigest.service.LocalTranscriptAcquirer.acquire", unexpected_acquisition)
    monkeypatch.setattr("audigest.service.get_provider", lambda _name: SpyProvider())
    runner = CliRunner()
    result = runner.invoke(app, ["transcribe", str(media_file), "--provider", "fake", "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["provider"] == "fake"
    assert payload["origin"]["kind"] == "stt"
    assert payload["text"]
    refreshed = runner.invoke(
        app, ["transcribe", str(media_file), "--provider", "fake", "--json", "--no-cache"]
    )
    assert refreshed.exit_code == 0, refreshed.stderr
    assert json.loads(refreshed.stdout)["id"] == payload["id"]
    assert json.loads(refreshed.stdout)["created_at"] != payload["created_at"]
    assert provider_calls == 2


@pytest.mark.parametrize(
    ("suffix", "sidecar_text", "expected_provider", "expected_text"),
    [
        (".txt", "Synthetic adjacent text.", "text", "Synthetic adjacent text."),
        (
            ".vtt",
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nSynthetic caption.\n",
            "captions",
            "Synthetic caption.",
        ),
    ],
)
def test_ingest_uses_sidecar_while_transcribe_keeps_stt_cache_separate(
    suffix: str,
    sidecar_text: str,
    expected_provider: str,
    expected_text: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    media_file = tmp_path / "episode.wav"
    shutil.copyfile(FIXTURE, media_file)
    sidecar = media_file.with_suffix(suffix)
    runner = CliRunner()

    sidecar.write_text(sidecar_text, encoding="utf-8")
    acquired = runner.invoke(app, ["ingest", str(media_file), "--provider", "auto", "--json"])
    assert acquired.exit_code == 0, acquired.stderr
    acquired_payload = json.loads(acquired.stdout)
    assert acquired_payload["provider"] == expected_provider
    assert acquired_payload["text"] == expected_text

    stt_cache_miss = runner.invoke(
        app, ["transcribe", str(media_file), "--provider", "auto", "--json"]
    )
    assert stt_cache_miss.exit_code == 5
    assert stt_cache_miss.stdout == ""
    assert "does not select an STT provider" in stt_cache_miss.stderr

    transcribed = runner.invoke(
        app, ["transcribe", str(media_file), "--provider", "fake", "--json"]
    )
    assert transcribed.exit_code == 0, transcribed.stderr
    transcribed_payload = json.loads(transcribed.stdout)
    assert transcribed_payload["provider"] == "fake"
    assert transcribed_payload["origin"]["kind"] == "stt"
    assert transcribed_payload["id"] != acquired_payload["id"]

    acquired_again = runner.invoke(app, ["ingest", str(media_file), "--provider", "auto", "--json"])
    transcribed_again = runner.invoke(
        app, ["transcribe", str(media_file), "--provider", "auto", "--json"]
    )
    assert acquired_again.exit_code == transcribed_again.exit_code == 0
    assert json.loads(acquired_again.stdout) == acquired_payload
    assert json.loads(transcribed_again.stdout) == transcribed_payload


@pytest.mark.parametrize("suffix", [".json", ".txt", ".md", ".srt", ".vtt"])
def test_transcribe_rejects_transcript_and_caption_inputs_before_provider(
    suffix: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    direct_input = tmp_path / f"input{suffix}"
    direct_input.write_text("synthetic input", encoding="utf-8")

    def unexpected_provider(*_: object, **__: object) -> None:
        pytest.fail("non-media input must be rejected before provider selection")

    monkeypatch.setattr("audigest.service.get_provider", unexpected_provider)
    result = CliRunner().invoke(
        app, ["transcribe", str(direct_input), "--provider", "fake", "--json"]
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "use 'ingest'" in result.stderr


def test_changed_adjacent_sidecar_invalidates_same_media_cache(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    media_file = tmp_path / "episode.wav"
    shutil.copyfile(FIXTURE, media_file)
    sidecar = media_file.with_suffix(".txt")
    sidecar.write_text("First synthetic sidecar text.", encoding="utf-8")
    runner = CliRunner()
    args = [command, str(media_file), "--provider", "auto", "--json"]

    first = runner.invoke(app, args)
    sidecar.write_text("Changed synthetic sidecar text.", encoding="utf-8")
    second = runner.invoke(app, args)

    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.stdout)["text"] == "First synthetic sidecar text."
    assert json.loads(second.stdout)["text"] == "Changed synthetic sidecar text."
    assert json.loads(first.stdout)["id"] != json.loads(second.stdout)["id"]


def test_new_sidecar_takes_precedence_over_prior_fake_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    media_file = tmp_path / "episode.wav"
    shutil.copyfile(FIXTURE, media_file)
    runner = CliRunner()

    transcribed = runner.invoke(
        app, ["transcribe", str(media_file), "--provider", "fake", "--json"]
    )
    media_file.with_suffix(".txt").write_text(
        "New sidecar transcript wins over older STT cache.",
        encoding="utf-8",
    )
    acquired = runner.invoke(app, ["ingest", str(media_file), "--provider", "auto", "--json"])

    assert transcribed.exit_code == acquired.exit_code == 0
    prior_payload = json.loads(transcribed.stdout)
    acquired_payload = json.loads(acquired.stdout)
    assert prior_payload["provider"] == "fake"
    assert acquired_payload["provider"] == "text"
    assert acquired_payload["text"] == "New sidecar transcript wins over older STT cache."
    assert acquired_payload["id"] != prior_payload["id"]
    assert "Using existing local transcript" in acquired.stderr

    transcribed_again = runner.invoke(
        app, ["transcribe", str(media_file), "--provider", "auto", "--json"]
    )
    assert transcribed_again.exit_code == 0, transcribed_again.stderr
    assert json.loads(transcribed_again.stdout) == prior_payload


def test_no_cache_still_uses_direct_text_input(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    text_file = tmp_path / "notes.txt"
    text_file.write_text("A direct text transcript.", encoding="utf-8")
    runner = CliRunner()
    args = [command, str(text_file), "--provider", "openai", "--json"]

    initial = runner.invoke(app, args)
    refreshed = runner.invoke(app, [*args, "--no-cache"])

    assert initial.exit_code == refreshed.exit_code == 0
    initial_payload = json.loads(initial.stdout)
    refreshed_payload = json.loads(refreshed.stdout)
    assert initial_payload["provider"] == refreshed_payload["provider"] == "text"
    assert refreshed_payload["text"] == "A direct text transcript."
    assert initial_payload["created_at"] != refreshed_payload["created_at"]


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_legacy_stt_cache_without_origin_is_reused_unchanged(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    resolved = LocalFileResolver().resolve(FIXTURE)
    fingerprint = resolved.content_sha256
    cache_id = cache_id_for(
        resolved,
        provider="fake",
        model="fake-v1",
        language=None,
        source_fingerprint=fingerprint,
    )
    legacy_transcript = Transcript(
        id=cache_id,
        source=resolved.source_info,
        provider="fake",
        model="fake-v1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        segments=[TranscriptSegment(text="Legacy cached transcript.")],
    )
    store = FileTranscriptStore(cache_dir)
    store.put(
        legacy_transcript,
        "# Transcript\n\nLegacy cached transcript.\n",
        resolved_source=resolved,
        source_fingerprint=fingerprint,
        requested_language=None,
    )
    transcript_path = store.transcript_path_for(cache_id)
    legacy_payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    legacy_payload.pop("origin")
    transcript_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    result = CliRunner().invoke(app, [command, str(FIXTURE), "--provider", "auto", "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["id"] == cache_id
    assert payload["origin"] is None
    assert payload["text"] == "Legacy cached transcript."
    assert "Using cached transcript." in result.stderr


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_corrupt_source_cache_reference_is_storage_error_not_stt_fallback(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    runner = CliRunner()
    args = [command, str(FIXTURE), "--provider", "fake", "--json"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.stderr

    reference = next((cache_dir / "refs").glob("*.json"))
    reference.write_text("{ invalid", encoding="utf-8")
    broken = runner.invoke(app, args)

    assert broken.exit_code == 2
    assert broken.stdout == ""
    assert "cache" in broken.stderr.lower() or "reference" in broken.stderr.lower()
    assert "traceback" not in broken.stderr.lower()


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_cache_hit_skips_provider_invocation(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    provider = get_provider("fake")
    calls = 0

    class SpyProvider:
        name = provider.name
        model = provider.model

        def transcribe(self, media_path: Path, *, language: str | None = None) -> Any:
            nonlocal calls
            calls += 1
            return provider.transcribe(media_path, language=language)

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: SpyProvider())
    runner = CliRunner()
    args = [command, str(FIXTURE), "--provider", "fake", "--json"]

    first = runner.invoke(app, args)
    second = runner.invoke(
        app,
        [command, str(FIXTURE), "--provider", "auto", "--json"],
    )

    assert first.exit_code == second.exit_code == 0
    assert calls == 1
    assert json.loads(first.stdout)["id"] == json.loads(second.stdout)["id"]
    assert "Using cached transcript." in second.stderr


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_no_cache_skips_reads_but_writes_a_fresh_transcript(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    provider = get_provider("fake")
    calls = 0

    class SpyProvider:
        name = provider.name
        model = provider.model

        def transcribe(self, media_path: Path, *, language: str | None = None) -> Any:
            nonlocal calls
            calls += 1
            return provider.transcribe(media_path, language=language)

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: SpyProvider())
    runner = CliRunner()
    base_args = [command, str(FIXTURE), "--provider", "fake", "--json"]

    initial = runner.invoke(app, base_args)
    fresh = runner.invoke(app, [*base_args, "--no-cache"])
    cached = runner.invoke(app, base_args)

    assert initial.exit_code == fresh.exit_code == cached.exit_code == 0
    assert calls == 2
    assert json.loads(initial.stdout)["id"] == json.loads(fresh.stdout)["id"]
    assert json.loads(fresh.stdout)["created_at"] != json.loads(initial.stdout)["created_at"]
    assert json.loads(cached.stdout)["created_at"] == json.loads(fresh.stdout)["created_at"]


def test_transcribe_cache_isolated_by_requested_language(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    provider = get_provider("fake")
    calls = 0

    class SpyProvider:
        name = provider.name
        model = provider.model

        def transcribe(self, media_path: Path, *, language: str | None = None) -> Any:
            nonlocal calls
            calls += 1
            return provider.transcribe(media_path, language=language)

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: SpyProvider())
    runner = CliRunner()
    english = runner.invoke(
        app, ["transcribe", str(FIXTURE), "--provider", "fake", "--language", "en", "--json"]
    )
    polish = runner.invoke(
        app, ["transcribe", str(FIXTURE), "--provider", "fake", "--language", "pl", "--json"]
    )
    english_replay = runner.invoke(
        app, ["transcribe", str(FIXTURE), "--provider", "auto", "--language", "en", "--json"]
    )

    assert english.exit_code == polish.exit_code == english_replay.exit_code == 0
    english_payload = json.loads(english.stdout)
    polish_payload = json.loads(polish.stdout)
    assert english_payload["language"] == "en"
    assert polish_payload["language"] == "pl"
    assert english_payload["id"] != polish_payload["id"]
    assert json.loads(english_replay.stdout) == english_payload
    assert calls == 2


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_content_mutation_invalidates_cache_for_same_path(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    local_source = tmp_path / "sample.wav"
    shutil.copyfile(FIXTURE, local_source)
    runner = CliRunner()
    args = [command, str(local_source), "--provider", "fake", "--json"]

    original = runner.invoke(app, args)
    local_source.write_bytes(local_source.read_bytes() + b"changed")
    changed = runner.invoke(app, args)

    assert original.exit_code == changed.exit_code == 0
    assert json.loads(original.stdout)["id"] != json.loads(changed.stdout)["id"]


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_invalid_arguments_exit_two_with_no_stdout(command: str) -> None:
    result = CliRunner().invoke(app, [command, "--unknown-option"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr


@pytest.mark.parametrize(
    ("source", "provider", "expected_code", "message"),
    [
        ("/definitely/missing/audigest.wav", "fake", 3, "does not exist"),
        ("https://example.com/audio.wav", "fake", 3, "local file"),
        ("http://[", "fake", 3, "invalid source"),
        (str(FIXTURE), "auto", 5, "does not select an STT provider"),
        (str(FIXTURE), "openai", 5, "OPENAI_API_KEY"),
    ],
)
@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_source_and_provider_failures_are_actionable_and_clean(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
    provider: str,
    expected_code: int,
    message: str,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")

    result = CliRunner().invoke(app, [command, source, "--provider", provider, "--json"])

    assert result.exit_code == expected_code
    assert result.stdout == ""
    assert message.lower() in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_missing_and_broken_explicit_config_are_clean_errors(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    missing_config = tmp_path / "missing.toml"
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(missing_config))

    missing_result = CliRunner().invoke(
        app,
        [command, str(FIXTURE), "--provider", "fake", "--json"],
    )
    broken_config = tmp_path / "broken.toml"
    broken_config.write_text("provider = [\n")
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(broken_config))
    broken_result = CliRunner().invoke(
        app,
        [command, str(FIXTURE), "--provider", "fake", "--json"],
    )

    assert missing_result.exit_code == 2
    assert missing_result.stdout == ""
    assert "does not exist" in missing_result.stderr.lower()
    assert "traceback" not in missing_result.stderr.lower()
    assert broken_result.exit_code == 2
    assert broken_result.stdout == ""
    assert "configuration file" in broken_result.stderr.lower()
    assert "traceback" not in broken_result.stderr.lower()


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_invalid_cached_json_is_a_clean_storage_failure(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    runner = CliRunner()
    args = [command, str(FIXTURE), "--provider", "fake"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.stderr

    transcript_path = next((tmp_path / "cache").glob("items/*/transcript.json"))
    transcript_path.write_text("not valid JSON")
    broken = runner.invoke(app, args)

    assert broken.exit_code == 2
    assert broken.stdout == ""
    assert "cache" in broken.stderr.lower()
    assert "traceback" not in broken.stderr.lower()


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_provider_failure_is_sanitized_and_preserves_local_source(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    local_source = tmp_path / "original.wav"
    shutil.copyfile(FIXTURE, local_source)
    original_bytes = local_source.read_bytes()

    class FailingProvider:
        name = "fake"
        model = "fake-v1"

        def transcribe(self, media_path: Path, *, language: str | None = None) -> Any:
            raise RuntimeError("synthetic-secret=private-test-value")

    monkeypatch.setattr("audigest.service.get_provider", lambda _name: FailingProvider())

    result = CliRunner().invoke(
        app,
        [command, str(local_source), "--provider", "fake", "--json"],
    )

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "provider 'fake' failed" in result.stderr.lower()
    assert "private-test-value" not in result.stderr
    assert "traceback" not in result.stderr.lower()
    assert local_source.read_bytes() == original_bytes
    assert not (cache_dir / "items").exists()


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_missing_dependency_error_keeps_exit_seven_without_partial_artifacts(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    prepare_environment(monkeypatch, cache_dir)
    local_source = tmp_path / "original.wav"
    shutil.copyfile(FIXTURE, local_source)
    original_bytes = local_source.read_bytes()

    class DependencyFailureProvider:
        name = "fake"
        model = "fake-v1"

        def transcribe(self, media_path: Path, *, language: str | None = None) -> Any:
            raise MissingDependencyError("Synthetic local runtime is missing.")

    monkeypatch.setattr(
        "audigest.service.get_provider",
        lambda _name: DependencyFailureProvider(),
    )

    result = CliRunner().invoke(
        app,
        [command, str(local_source), "--provider", "fake", "--json"],
    )

    assert result.exit_code == 7
    assert result.stdout == ""
    assert "synthetic local runtime is missing" in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()
    assert local_source.read_bytes() == original_bytes
    assert not (cache_dir / "items").exists()


@pytest.mark.parametrize("command", ["ingest", "transcribe"])
def test_keep_media_never_changes_or_copies_a_local_original(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path / "cache")
    local_source = tmp_path / "original.wav"
    shutil.copyfile(FIXTURE, local_source)
    original_bytes = local_source.read_bytes()

    result = CliRunner().invoke(
        app,
        [command, str(local_source), "--provider", "fake", "--keep-media"],
    )

    assert result.exit_code == 0, result.stderr
    assert local_source.read_bytes() == original_bytes
    assert list(tmp_path.glob("*.wav")) == [local_source]
