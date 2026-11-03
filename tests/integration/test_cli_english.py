"""Offline CLI tests for canonical-only English learning and its separate cache."""

import json
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from audigest.cli import app
from audigest.errors import MissingDependencyError, ProcessorError
from audigest.models import Transcript
from audigest.processors.english import (
    EnglishArtifact,
    EnglishItemDraft,
    EnglishResponse,
    render_english_markdown,
)


class SyntheticEnglishProvider:
    name = "openai"

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def extract(self, transcript: Transcript, level: str) -> EnglishResponse:
        self.calls.append((transcript.id, level))
        if "put off" not in transcript.text:
            return EnglishResponse(
                overview="No high-value English items were identified at this level.", items=[]
            )
        quote = transcript.segments[0].text
        return EnglishResponse(
            overview=f"The transcript contains a useful item for {level} learners.",
            items=[
                EnglishItemDraft(
                    kind="phrasal_verb",
                    expression="put off",
                    segment_index=0,
                    quote=quote,
                    meaning="To delay doing something.",
                    usage_note="It can be followed by an event or task.",
                    examples=["We put off the meeting until Friday."],
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
    if not root.exists():
        return {}
    return {
        path.relative_to(root): (path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


def ingest_vtt(runner: CliRunner, tmp_path: Path, *, text: str | None = None) -> tuple[str, Path]:
    source = tmp_path / "synthetic.vtt"
    source_text = text or "I put off the decision until Monday."
    source.write_text(
        f"WEBVTT\n\n00:00:03.000 --> 00:00:05.500\n{source_text}\n",
        encoding="utf-8",
    )
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])
    assert ingested.exit_code == 0, ingested.stderr
    return json.loads(ingested.stdout)["id"], source


def patch_provider(monkeypatch: pytest.MonkeyPatch, provider: SyntheticEnglishProvider) -> None:
    monkeypatch.setattr(
        "audigest.processors.english_service.OpenAIEnglishProvider",
        lambda model: provider if model == provider.model else pytest.fail("unexpected model"),
    )


def test_english_uses_removed_canonical_source_json_markdown_and_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript_id, source = ingest_vtt(runner, tmp_path)
    canonical_before = snapshot_tree(cache_dir / "items")
    reference_before = snapshot_tree(cache_dir / "refs")
    source.unlink()

    def unexpected(*_: object, **__: object) -> None:
        pytest.fail("english must consume only the stored canonical transcript")

    monkeypatch.setattr("audigest.service.LocalFileResolver.resolve", unexpected)
    monkeypatch.setattr("audigest.service.LocalTranscriptAcquirer.acquire", unexpected)
    monkeypatch.setattr("audigest.service.get_provider", unexpected)
    provider = SyntheticEnglishProvider("gpt-6-luna")
    patch_provider(monkeypatch, provider)
    first = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--provider", "openai", "--json"],
    )

    assert first.exit_code == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["transcript_id"] == transcript_id
    assert payload["level"] == "B2"
    assert payload["provider"] == "openai"
    assert payload["model"] == "gpt-6-luna"
    assert payload["items"][0]["kind"] == "phrasal_verb"
    assert payload["items"][0]["expression"] == "put off"
    assert payload["items"][0]["citation"]["quote"] == "I put off the decision until Monday."
    assert payload["items"][0]["citation"]["start"] == 3.0
    assert payload["items"][0]["citation"]["end"] == 5.5
    assert payload["items"][0]["examples"] == ["We put off the meeting until Friday."]
    assert len(provider.calls) == 1
    assert first.stdout.startswith("{")
    assert "private" not in first.stdout.lower()
    assert "OPENAI_API_KEY" not in first.stdout

    english_dir = cache_dir / "outputs" / "english" / payload["id"]
    stored_json = json.loads((english_dir / "english.json").read_text(encoding="utf-8"))
    stored_markdown = (english_dir / "english.md").read_text(encoding="utf-8")
    assert stored_json == payload
    assert "00:00:03.000–00:00:05.500" in stored_markdown
    assert "**Examples:**\n- We put off the meeting until Friday." in stored_markdown
    assert "> I put off the decision until Monday." in stored_markdown
    assert snapshot_tree(cache_dir / "items") == canonical_before
    assert snapshot_tree(cache_dir / "refs") == reference_before

    monkeypatch.setattr(
        "audigest.processors.english_service.OpenAIEnglishProvider",
        lambda _model: pytest.fail("cached auto result must bypass SDK and provider"),
    )
    repeated = runner.invoke(app, ["english", transcript_id, "--level", "B2", "--json"])
    assert repeated.exit_code == 0, repeated.stderr
    assert json.loads(repeated.stdout) == payload
    assert "Using cached English result." in repeated.stderr

    human = runner.invoke(app, ["english", transcript_id, "--level", "B2"])
    assert human.exit_code == 0, human.stderr
    assert human.stdout == stored_markdown
    assert human.stdout.startswith("# English learning — B2\n")


def test_auto_cache_miss_fails_without_selecting_provider_or_creating_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-never-valid-key")
    runner = CliRunner()
    transcript_id, _source = ingest_vtt(runner, tmp_path)
    monkeypatch.setattr(
        "audigest.processors.english_service.OpenAIEnglishProvider",
        lambda _model: pytest.fail("auto must not select a provider"),
    )

    result = runner.invoke(app, ["english", transcript_id, "--level", "B2", "--json"])

    assert result.exit_code == 6
    assert result.stdout == ""
    assert "provider 'auto' is cache-only" in result.stderr
    assert not (cache_dir / "outputs").exists()
    assert "OPENAI_API_KEY" not in result.stderr


def test_level_content_and_timing_change_cache_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript_id, source = ingest_vtt(runner, tmp_path)
    source.unlink()
    provider = SyntheticEnglishProvider("gpt-6-luna")
    patch_provider(monkeypatch, provider)

    b2 = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--provider", "openai", "--json"],
    )
    assert b2.exit_code == 0, b2.stderr
    b2_payload = json.loads(b2.stdout)
    c1 = runner.invoke(
        app,
        ["english", transcript_id, "--level", "C1", "--provider", "openai", "--json"],
    )
    assert c1.exit_code == 0, c1.stderr
    c1_payload = json.loads(c1.stdout)
    assert c1_payload["id"] != b2_payload["id"]

    transcript_path = cache_dir / "items" / transcript_id / "transcript.json"
    stored = json.loads(transcript_path.read_text(encoding="utf-8"))
    stored["segments"][0]["text"] = "I put off the decision until Tuesday."
    stored["segments"][0]["start"] = 3.25
    stored["text"] = "I put off the decision until Tuesday."
    transcript_path.write_text(json.dumps(stored), encoding="utf-8")
    changed = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--provider", "openai", "--json"],
    )
    assert changed.exit_code == 0, changed.stderr
    changed_payload = json.loads(changed.stdout)
    assert changed_payload["id"] not in {b2_payload["id"], c1_payload["id"]}
    assert changed_payload["items"][0]["citation"]["quote"].endswith("Tuesday.")
    assert changed_payload["items"][0]["citation"]["start"] == 3.25
    assert len(provider.calls) == 3


def test_no_cache_bypasses_lookup_but_does_not_select_a_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript_id, source = ingest_vtt(runner, tmp_path)
    source.unlink()
    provider = SyntheticEnglishProvider("gpt-6-luna")
    patch_provider(monkeypatch, provider)
    created = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--provider", "openai", "--json"],
    )
    assert created.exit_code == 0, created.stderr
    original = json.loads(created.stdout)

    auto_recompute = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--no-cache", "--json"],
    )
    assert auto_recompute.exit_code == 6
    assert auto_recompute.stdout == ""
    assert "cache-only" in auto_recompute.stderr
    assert len(provider.calls) == 1

    recomputed = runner.invoke(
        app,
        [
            "english",
            transcript_id,
            "--level",
            "B2",
            "--provider",
            "openai",
            "--no-cache",
            "--json",
        ],
    )
    assert recomputed.exit_code == 0, recomputed.stderr
    refreshed = json.loads(recomputed.stdout)
    assert refreshed["id"] == original["id"]
    assert refreshed["created_at"] != original["created_at"]
    assert len(provider.calls) == 2
    assert (cache_dir / "outputs" / "english" / refreshed["id"] / "english.json").exists()


@pytest.mark.parametrize("corruption", ["quote", "index", "expression", "timing", "speaker"])
def test_cache_hit_revalidates_english_evidence_against_canonical_segments(
    corruption: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript_id, source = ingest_vtt(runner, tmp_path)
    source.unlink()
    provider = SyntheticEnglishProvider("gpt-6-luna")
    patch_provider(monkeypatch, provider)
    created = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--provider", "openai", "--json"],
    )
    assert created.exit_code == 0, created.stderr
    payload = json.loads(created.stdout)
    entry = cache_dir / "outputs" / "english" / payload["id"]
    artifact_data = json.loads((entry / "english.json").read_text(encoding="utf-8"))
    item = artifact_data["items"][0]
    if corruption == "quote":
        item["citation"]["quote"] = "not in the transcript"
    elif corruption == "index":
        item["citation"]["segment_index"] = 4
    elif corruption == "expression":
        item["expression"] = "invented phrase"
    elif corruption == "timing":
        item["citation"]["start"] = 90.0
        item["citation"]["end"] = 91.0
    else:
        item["citation"]["speaker"] = "Unknown speaker"
    corrupted = EnglishArtifact.model_validate(artifact_data)
    (entry / "english.json").write_text(
        corrupted.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (entry / "english.md").write_text(render_english_markdown(corrupted), encoding="utf-8")
    monkeypatch.setattr(
        "audigest.processors.english_service.OpenAIEnglishProvider",
        lambda _model: pytest.fail("corrupt cache must not call the provider"),
    )

    result = runner.invoke(app, ["english", transcript_id, "--level", "B2", "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "english cache evidence does not match" in result.stderr.lower()


def test_irrelevant_or_multilingual_transcript_can_return_zero_items(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    source = tmp_path / "synthetic.txt"
    source.write_text("Hola, ¿dónde está la biblioteca?", encoding="utf-8")
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(source), "--json"])
    assert ingested.exit_code == 0, ingested.stderr
    transcript_id = json.loads(ingested.stdout)["id"]
    provider = SyntheticEnglishProvider("gpt-6-luna")
    patch_provider(monkeypatch, provider)

    result = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--provider", "openai", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["items"] == []
    assert "No high-value English items" in render_english_markdown(
        EnglishArtifact.model_validate(payload)
    )


@pytest.mark.parametrize(
    ("arguments", "environment", "selected_id", "expected_exit"),
    [
        (["--level", "b2", "--provider", "openai"], {}, None, 2),
        (["--level", "B2", "--provider", "fake"], {}, None, 2),
        (["--level", "B2", "--model", "invalid model"], {}, None, 2),
        (["--level", "B2"], {"AUDIGEST_LLM_PROVIDER": "elevenlabs"}, None, 2),
        (["--level", "B2"], {"AUDIGEST_LLM_MODEL": "invalid model"}, None, 2),
        (["--level", "B2"], {}, "../../invalid", 2),
        (["--level", "B2"], {}, "missing-transcript-id", 3),
    ],
)
def test_invalid_levels_selections_and_ids_have_stable_error_categories(
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
    runner = CliRunner()
    transcript_id, _source = ingest_vtt(runner, tmp_path)

    result = runner.invoke(
        app,
        ["english", selected_id or transcript_id, *arguments, "--json"],
    )

    assert result.exit_code == expected_exit
    assert result.stdout == ""
    if expected_exit == 2:
        assert "error:" in result.stderr.lower()
    else:
        assert "was not found in the local cache" in result.stderr


@pytest.mark.parametrize(
    ("arguments", "environment", "expected_model"),
    [
        (
            ["--provider", "openai", "--model", "cli-model"],
            {"AUDIGEST_LLM_PROVIDER": "auto", "AUDIGEST_LLM_MODEL": "env-model"},
            "cli-model",
        ),
        (
            [],
            {"AUDIGEST_LLM_PROVIDER": "openai", "AUDIGEST_LLM_MODEL": "env-model"},
            "env-model",
        ),
        (["--provider", "openai"], {}, "gpt-6-luna"),
    ],
)
def test_llm_precedence_is_independent_of_stt_configuration(
    arguments: list[str],
    environment: dict[str, str],
    expected_model: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'provider = "local"\nlanguage = "fr"\ncache_dir = {json.dumps(str(cache_dir))}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("AUDIGEST_STT_PROVIDER", "local")
    monkeypatch.setenv("AUDIGEST_LANGUAGE", "fr")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    runner = CliRunner()
    transcript_id, _source = ingest_vtt(runner, tmp_path)
    selected_models: list[str] = []
    monkeypatch.setattr(
        "audigest.processors.english_service.OpenAIEnglishProvider",
        lambda model: selected_models.append(model) or SyntheticEnglishProvider(model),
    )

    result = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", *arguments, "--json"],
    )

    assert result.exit_code == 0, result.stderr
    assert selected_models == [expected_model]
    assert json.loads(result.stdout)["model"] == expected_model


def test_empty_stored_transcript_fails_before_selecting_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript_id, _source = ingest_vtt(runner, tmp_path)
    transcript_path = cache_dir / "items" / transcript_id / "transcript.json"
    stored = json.loads(transcript_path.read_text(encoding="utf-8"))
    stored["segments"] = [{"text": " \t\n "}]
    stored["text"] = " \t\n "
    transcript_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        "audigest.processors.english_service.OpenAIEnglishProvider",
        lambda _model: pytest.fail("empty transcript must fail before provider selection"),
    )

    result = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--provider", "openai", "--json"],
    )

    assert result.exit_code == 6
    assert result.stdout == ""
    assert "no non-whitespace text" in result.stderr


def test_cefr_level_is_required_by_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = ingest_vtt(CliRunner(), tmp_path)

    result = CliRunner().invoke(app, ["english", transcript_id, "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--level" in Text.from_ansi(result.stderr).plain


@pytest.mark.parametrize(
    ("failure", "expected_exit", "expected_message"),
    [
        (RuntimeError("private provider payload"), 6, "failed unexpectedly"),
        (MissingDependencyError("expected missing SDK"), 7, "expected missing SDK"),
        (ProcessorError("expected processor failure"), 6, "expected processor failure"),
    ],
)
def test_provider_failures_are_sanitized_or_preserved(
    failure: Exception,
    expected_exit: int,
    expected_message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript_id, _source = ingest_vtt(runner, tmp_path)

    class FailingProvider:
        name = "openai"
        model = "gpt-6-luna"

        def extract(self, _transcript: Transcript, _level: str) -> EnglishResponse:
            raise failure

    monkeypatch.setattr(
        "audigest.processors.english_service.OpenAIEnglishProvider",
        lambda _model: FailingProvider(),
    )
    result = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--provider", "openai", "--json"],
    )

    assert result.exit_code == expected_exit
    assert result.stdout == ""
    assert expected_message in result.stderr
    if isinstance(failure, RuntimeError):
        assert "private provider payload" not in result.stderr


def test_invalid_initial_evidence_does_not_write_outputs_or_change_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = prepare_environment(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript_id, _source = ingest_vtt(runner, tmp_path)
    canonical_before = snapshot_tree(cache_dir / "items")

    class BadProvider(SyntheticEnglishProvider):
        def extract(self, transcript: Transcript, level: str) -> EnglishResponse:
            self.calls.append((transcript.id, level))
            return EnglishResponse(
                overview="An unsupported quote.",
                items=[
                    EnglishItemDraft(
                        kind="phrase",
                        expression="invented",
                        segment_index=0,
                        quote="this is not in the segment",
                        meaning="A false item.",
                        usage_note="Unsupported.",
                        examples=["A synthetic sentence."],
                    )
                ],
            )

    patch_provider(monkeypatch, BadProvider("gpt-6-luna"))
    result = runner.invoke(
        app,
        ["english", transcript_id, "--level", "B2", "--provider", "openai", "--json"],
    )

    assert result.exit_code == 6
    assert result.stdout == ""
    assert "does not match its transcript segment" in result.stderr
    assert not (cache_dir / "outputs").exists()
    assert snapshot_tree(cache_dir / "items") == canonical_before
