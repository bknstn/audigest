"""Offline CLI tests for canonical-only summarization and its separate cache."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.errors import MissingDependencyError, ProcessorError
from audigest.models import Transcript
from audigest.processors.summary import (
    SummaryArtifact,
    SummaryPointDraft,
    SummaryResponse,
    render_summary_markdown,
)


class SyntheticSummaryProvider:
    name = "openai"

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls: list[str] = []

    def summarize(self, transcript: Transcript) -> SummaryResponse:
        self.calls.append(transcript.id)
        quote = transcript.segments[0].text[:32]
        return SummaryResponse(
            summary="A concise summary of the synthetic transcript.",
            key_points=[
                SummaryPointDraft(
                    point="The source contains a synthetic main idea.",
                    segment_index=0,
                    quote=quote,
                )
            ],
        )


def prepare_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cache_dir = tmp_path / "cache"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f"cache_dir = {json.dumps(str(cache_dir))}\n", encoding="utf-8")
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config_file))
    for variable in (
        "AUDIGEST_CACHE_DIR",
        "AUDIGEST_STT_PROVIDER",
        "AUDIGEST_LANGUAGE",
        "AUDIGEST_LLM_PROVIDER",
        "AUDIGEST_LLM_MODEL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    return cache_dir


def snapshot_tree(root: Path) -> dict[Path, tuple[int, bytes]]:
    return {
        path.relative_to(root): (path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("suffix", "contents", "timed"),
    [
        (".vtt", "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nSynthetic timed caption.\n", True),
        (".txt", "Synthetic untimed transcript with a useful idea.", False),
    ],
)
def test_summarize_removed_source_json_markdown_and_cache_hit(
    suffix: str,
    contents: str,
    timed: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    source = tmp_path / f"synthetic{suffix}"
    source.write_text(contents, encoding="utf-8")
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])
    assert ingested.exit_code == 0, ingested.stderr
    transcript_id = json.loads(ingested.stdout)["id"]
    canonical_before = snapshot_tree(cache_dir / "items")
    reference_before = snapshot_tree(cache_dir / "refs")
    source.unlink()

    def unexpected(*_: object, **__: object) -> None:
        pytest.fail("summarize must use only the stored canonical transcript")

    monkeypatch.setattr("audigest.service.LocalFileResolver.resolve", unexpected)
    monkeypatch.setattr("audigest.service.LocalTranscriptAcquirer.acquire", unexpected)
    monkeypatch.setattr("audigest.service.get_provider", unexpected)
    provider = SyntheticSummaryProvider("gpt-6-luna")
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda model: provider if model == provider.model else pytest.fail("unexpected model"),
    )
    first = runner.invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--json"],
    )

    assert first.exit_code == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["transcript_id"] == transcript_id
    assert payload["provider"] == "openai"
    assert payload["model"] == "gpt-6-luna"
    assert payload["summary"] == "A concise summary of the synthetic transcript."
    citation = payload["key_points"][0]["citation"]
    assert citation["segment_index"] == 0
    assert citation["quote"] in contents
    assert (citation["start"] is not None and citation["end"] is not None) == timed
    assert provider.calls == [transcript_id]
    assert "private" not in first.stdout.lower()
    assert "OPENAI_API_KEY" not in first.stdout

    summary_dir = cache_dir / "outputs" / "summaries" / payload["id"]
    stored_json = json.loads((summary_dir / "summary.json").read_text(encoding="utf-8"))
    stored_markdown = (summary_dir / "summary.md").read_text(encoding="utf-8")
    assert stored_json == payload
    if timed:
        assert "00:00:01.000–00:00:02.500" in stored_markdown
    else:
        assert "[00:" not in stored_markdown
        assert citation["start"] is None and citation["end"] is None
    assert snapshot_tree(cache_dir / "items") == canonical_before
    assert snapshot_tree(cache_dir / "refs") == reference_before

    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda _model: pytest.fail("cached auto summary must bypass SDK and provider"),
    )
    repeated = runner.invoke(app, ["summarize", transcript_id, "--provider", "auto", "--json"])
    assert repeated.exit_code == 0, repeated.stderr
    assert json.loads(repeated.stdout) == payload
    assert "Using cached summary." in repeated.stderr
    assert repeated.stdout.startswith("{")
    assert snapshot_tree(cache_dir / "items") == canonical_before

    human = runner.invoke(app, ["summarize", transcript_id])
    assert human.exit_code == 0, human.stderr
    assert human.stdout == stored_markdown
    assert human.stdout.startswith("# Summary\n")


def test_auto_cache_miss_fails_without_selecting_provider_or_creating_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    source = tmp_path / "synthetic.txt"
    source.write_text("Stored transcript data.", encoding="utf-8")
    ingested = CliRunner().invoke(app, ["ingest", str(source), "--json"])
    transcript_id = json.loads(ingested.stdout)["id"]
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda _model: pytest.fail("auto must not select a provider"),
    )

    result = CliRunner().invoke(app, ["summarize", transcript_id, "--json"])

    assert result.exit_code == 6
    assert result.stdout == ""
    assert "provider 'auto' is cache-only" in result.stderr
    assert not (cache_dir / "outputs").exists()
    assert "OPENAI_API_KEY" not in result.stderr


def test_content_and_timing_changes_invalidate_and_no_cache_refreshes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    source = tmp_path / "synthetic.vtt"
    source.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nSynthetic cited caption.\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])
    transcript_id = json.loads(ingested.stdout)["id"]
    source.unlink()
    provider = SyntheticSummaryProvider("gpt-6-luna")
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda _model: provider,
    )

    first = runner.invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--json"],
    )
    assert first.exit_code == 0, first.stderr
    first_payload = json.loads(first.stdout)
    transcript_path = cache_dir / "items" / transcript_id / "transcript.json"
    stored_transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    stored_transcript["segments"][0]["start"] = 1.25
    stored_transcript["segments"][0]["end"] = 2.75
    transcript_path.write_text(json.dumps(stored_transcript), encoding="utf-8")

    changed = runner.invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--json"],
    )
    assert changed.exit_code == 0, changed.stderr
    changed_payload = json.loads(changed.stdout)
    assert changed_payload["id"] != first_payload["id"]
    assert changed_payload["key_points"][0]["citation"]["start"] == 1.25
    assert len(provider.calls) == 2

    refreshed = runner.invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--no-cache", "--json"],
    )
    assert refreshed.exit_code == 0, refreshed.stderr
    refreshed_payload = json.loads(refreshed.stdout)
    assert refreshed_payload["id"] == changed_payload["id"]
    assert refreshed_payload["created_at"] != changed_payload["created_at"]
    assert len(provider.calls) == 3


def test_corrupt_summary_cache_fails_without_provider_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    source = tmp_path / "synthetic.txt"
    source.write_text("A stored summary input.", encoding="utf-8")
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])
    transcript_id = json.loads(ingested.stdout)["id"]
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda model: SyntheticSummaryProvider(model),
    )
    created = runner.invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--json"],
    )
    summary_id = json.loads(created.stdout)["id"]
    (cache_dir / "outputs" / "summaries" / summary_id / "summary.json").write_text(
        "{invalid", encoding="utf-8"
    )
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda _model: pytest.fail("corrupt cache must not silently call the provider"),
    )

    result = runner.invoke(app, ["summarize", transcript_id, "--provider", "auto", "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "summary cache entry is corrupt" in result.stderr.lower()


@pytest.mark.parametrize("corruption", ["quote", "index", "timing", "speaker", "summary"])
def test_cache_hit_revalidates_summary_against_canonical_segments(
    corruption: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    source = tmp_path / "synthetic.vtt"
    source.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nSynthetic cited caption.\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])
    transcript_id = json.loads(ingested.stdout)["id"]
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda model: SyntheticSummaryProvider(model),
    )
    created = runner.invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--json"],
    )
    assert created.exit_code == 0, created.stderr
    summary_id = json.loads(created.stdout)["id"]
    summary_dir = cache_dir / "outputs" / "summaries" / summary_id
    summary_path = summary_dir / "summary.json"
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    point = payload["key_points"][0]
    if corruption == "quote":
        point["citation"]["quote"] = "not found in the transcript"
    elif corruption == "index":
        point["citation"]["segment_index"] = 1
    elif corruption == "timing":
        point["citation"]["start"] = 9.0
        point["citation"]["end"] = 10.0
    elif corruption == "speaker":
        point["citation"]["speaker"] = "Unknown speaker"
    else:
        payload["summary"] = " \t "
    corrupted = SummaryArtifact.model_validate(payload)
    summary_path.write_text(corrupted.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (summary_dir / "summary.md").write_text(render_summary_markdown(corrupted), encoding="utf-8")
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda _model: pytest.fail("a corrupt cache hit must not call OpenAI"),
    )

    result = runner.invoke(app, ["summarize", transcript_id, "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "summary cache citations do not match the canonical transcript" in result.stderr.lower()


def test_empty_stored_transcript_is_rejected_before_provider_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    source = tmp_path / "empty.txt"
    source.write_text("A transcript that will be made whitespace-only in cache.", encoding="utf-8")
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])
    assert ingested.exit_code == 0, ingested.stderr
    transcript_id = json.loads(ingested.stdout)["id"]
    canonical_path = tmp_path / "cache" / "items" / transcript_id / "transcript.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    canonical["segments"] = [{"text": " \t\n"}]
    canonical["text"] = " \t\n"
    canonical_path.write_text(json.dumps(canonical), encoding="utf-8")
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda _model: pytest.fail("empty transcript must fail before provider selection"),
    )

    result = runner.invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--json"],
    )

    assert result.exit_code == 6
    assert result.stdout == ""
    assert "no non-whitespace text" in result.stderr


@pytest.mark.parametrize(
    ("arguments", "environment", "expected_model"),
    [
        (
            ["--provider", "openai", "--model", "cli-model"],
            {"AUDIGEST_LLM_PROVIDER": "auto", "AUDIGEST_LLM_MODEL": "env-model"},
            "cli-model",
        ),
        ([], {"AUDIGEST_LLM_PROVIDER": "openai", "AUDIGEST_LLM_MODEL": "env-model"}, "env-model"),
        (["--provider", "openai"], {}, "gpt-6-luna"),
    ],
)
def test_llm_selection_precedence_is_independent_of_stt_settings(
    arguments: list[str],
    environment: dict[str, str],
    expected_model: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'provider = "local"\nlanguage = "fr"\ncache_dir = {json.dumps(str(cache_dir))}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDIGEST_STT_PROVIDER", "local")
    monkeypatch.setenv("AUDIGEST_LANGUAGE", "fr")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    source = tmp_path / "synthetic.txt"
    source.write_text("A stored transcript for selection tests.", encoding="utf-8")
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])
    transcript_id = json.loads(ingested.stdout)["id"]
    selected_models: list[str] = []
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda model: selected_models.append(model) or SyntheticSummaryProvider(model),
    )

    result = runner.invoke(
        app,
        ["summarize", transcript_id, *arguments, "--json"],
    )

    assert result.exit_code == 0, result.stderr
    assert selected_models == [expected_model]
    assert json.loads(result.stdout)["model"] == expected_model


@pytest.mark.parametrize(
    ("arguments", "environment", "selected_id", "expected_exit"),
    [
        (["--provider", "fake"], {}, None, 2),
        (["--model", "invalid model"], {}, None, 2),
        ([], {"AUDIGEST_LLM_PROVIDER": "elevenlabs"}, None, 2),
        ([], {"AUDIGEST_LLM_MODEL": "invalid model"}, None, 2),
        ([], {}, "../../invalid", 2),
        ([], {}, "missing-transcript-id", 3),
    ],
)
def test_invalid_selection_and_transcript_ids_have_stable_error_categories(
    arguments: list[str],
    environment: dict[str, str],
    selected_id: str | None,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    source = tmp_path / "synthetic.txt"
    source.write_text("A stored transcript.", encoding="utf-8")
    ingested = CliRunner().invoke(app, ["ingest", str(source), "--json"])
    transcript_id = json.loads(ingested.stdout)["id"]

    result = CliRunner().invoke(
        app,
        ["summarize", selected_id or transcript_id, *arguments, "--json"],
    )

    assert result.exit_code == expected_exit
    assert result.stdout == ""
    if expected_exit == 2:
        assert "error:" in result.stderr.lower()
    elif expected_exit == 3:
        assert "was not found in the local cache" in result.stderr


@pytest.mark.parametrize(
    ("failure", "expected_exit", "expected_message"),
    [
        (RuntimeError("private provider payload"), 6, "failed unexpectedly"),
        (MissingDependencyError("expected missing SDK"), 7, "expected missing SDK"),
        (ProcessorError("expected provider failure"), 6, "expected provider failure"),
    ],
)
def test_unexpected_provider_errors_are_sanitized_and_expected_errors_preserved(
    failure: Exception,
    expected_exit: int,
    expected_message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    source = tmp_path / "synthetic.txt"
    source.write_text("A provider failure regression input.", encoding="utf-8")
    ingested = CliRunner().invoke(app, ["ingest", str(source), "--json"])
    transcript_id = json.loads(ingested.stdout)["id"]

    class FailingProvider:
        name = "openai"
        model = "gpt-6-luna"

        def summarize(self, _transcript: Transcript) -> SummaryResponse:
            raise failure

    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda _model: FailingProvider(),
    )
    result = CliRunner().invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--json"],
    )

    assert result.exit_code == expected_exit
    assert result.stdout == ""
    assert expected_message in result.stderr
    if isinstance(failure, RuntimeError):
        assert "private provider payload" not in result.stderr


def test_invalid_quote_and_storage_failure_do_not_touch_canonical_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    source = tmp_path / "synthetic.txt"
    source.write_text("The original text is retained.", encoding="utf-8")
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])
    transcript_id = json.loads(ingested.stdout)["id"]
    canonical_before = snapshot_tree(cache_dir / "items")

    class BadCitationProvider(SyntheticSummaryProvider):
        def summarize(self, transcript: Transcript) -> SummaryResponse:
            self.calls.append(transcript.id)
            return SummaryResponse(
                summary="A generated paragraph.",
                key_points=[
                    SummaryPointDraft(
                        point="An unsupported claim.",
                        segment_index=0,
                        quote="this quote does not exist",
                    )
                ],
            )

    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda model: BadCitationProvider(model),
    )
    invalid = runner.invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--json"],
    )
    assert invalid.exit_code == 6
    assert invalid.stdout == ""
    assert "does not match its cited segment" in invalid.stderr
    assert not (cache_dir / "outputs").exists()
    assert snapshot_tree(cache_dir / "items") == canonical_before

    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda model: SyntheticSummaryProvider(model),
    )
    monkeypatch.setattr(
        "audigest.processors.storage.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private storage path")),
    )
    failed_write = runner.invoke(
        app,
        ["summarize", transcript_id, "--provider", "openai", "--json"],
    )
    assert failed_write.exit_code == 2
    assert failed_write.stdout == ""
    assert "private storage path" not in failed_write.stderr
    assert snapshot_tree(cache_dir / "items") == canonical_before
