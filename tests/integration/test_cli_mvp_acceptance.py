"""Offline end-to-end acceptance for transcript reuse across all processors."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.processors.application import (
    ApplicationRecommendationDraft,
    ApplicationResponse,
    ProjectEvidenceDraft,
    TranscriptEvidenceDraft,
)
from audigest.processors.english import EnglishItemDraft, EnglishResponse
from audigest.processors.summary import SummaryPointDraft, SummaryResponse


class SyntheticResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.headers: dict[str, str] = {}
        self.offset = 0

    def __enter__(self) -> SyntheticResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.content)
        result = self.content[self.offset : self.offset + size]
        self.offset += len(result)
        return result

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


class SyntheticSummaryProvider:
    name = "openai"
    model = "gpt-6-luna"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def summarize(self, transcript: Transcript) -> SummaryResponse:
        self.calls.append(transcript.id)
        return SummaryResponse(
            summary="The synthetic speaker recommends cautious, tested changes.",
            key_points=[
                SummaryPointDraft(
                    point="Delay risky changes until tests pass.",
                    segment_index=index,
                    quote=segment.text,
                )
                for index, segment in enumerate(transcript.segments)
            ],
        )


class SyntheticApplicationProvider:
    name = "openai"
    model = "gpt-6-luna"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply(self, transcript: Transcript, project: Any) -> ApplicationResponse:
        self.calls.append(transcript.id)
        project_file = project.files[0]
        return ApplicationResponse(
            overview="The project can adopt the transcript's review practices.",
            recommendations=[
                ApplicationRecommendationDraft(
                    idea="Gate changes on passing tests.",
                    relevance="The project documents its release checks.",
                    suggested_action="Add a test gate to the release checklist.",
                    transcript_evidence=TranscriptEvidenceDraft(
                        segment_index=index,
                        quote=segment.text,
                    ),
                    project_evidence=ProjectEvidenceDraft(
                        relative_path=project_file.relative_path,
                        quote=project_file.text,
                    ),
                )
                for index, segment in enumerate(transcript.segments)
            ],
        )


class SyntheticEnglishProvider:
    name = "openai"
    model = "gpt-6-luna"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def extract(self, transcript: Transcript, level: str) -> EnglishResponse:
        self.calls.append((transcript.id, level))
        return EnglishResponse(
            overview="Two useful expressions appear in the synthetic transcript.",
            items=[
                EnglishItemDraft(
                    kind="phrasal_verb",
                    expression="put off",
                    segment_index=0,
                    quote=transcript.segments[0].text,
                    meaning="To delay doing something.",
                    usage_note="Often followed by a task or event.",
                    examples=["We put off the review until Friday."],
                ),
                EnglishItemDraft(
                    kind="collocation",
                    expression="retry limit",
                    segment_index=1,
                    quote=transcript.segments[1].text,
                    meaning="A maximum number of repeated attempts.",
                    usage_note="Common in software and service operations.",
                    examples=["The client reached its retry limit."],
                ),
            ],
        )


def _snapshot_tree(root: Path) -> dict[Path, tuple[int, bytes]]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root): (path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


def _set_cache(monkeypatch: pytest.MonkeyPatch, cache: Path) -> None:
    config = cache.parent / "config.toml"
    config.write_text(f"cache_dir = {json.dumps(str(cache))}\n", encoding="utf-8")
    monkeypatch.setenv("AUDIGEST_CONFIG_FILE", str(config))
    for name in (
        "AUDIGEST_CACHE_DIR",
        "AUDIGEST_STT_PROVIDER",
        "AUDIGEST_LANGUAGE",
        "AUDIGEST_LLM_PROVIDER",
        "AUDIGEST_LLM_MODEL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_ingest_show_and_processors_share_canonical_with_keyless_cache_repeats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    _set_cache(monkeypatch, cache)
    source_path = tmp_path / "acceptance-input.json"
    transcript = Transcript(
        id="synthetic-source-id",
        source=SourceInfo(
            type="local",
            uri=str(source_path),
            title="Synthetic acceptance transcript",
        ),
        provider="fixture",
        language="en",
        created_at=datetime.now(UTC),
        segments=[
            TranscriptSegment(
                start=3.0,
                end=5.5,
                speaker="Host",
                text="We put off risky changes until tests pass.",
            ),
            TranscriptSegment(text="Keep the retry limit and timeout clear before shipping."),
        ],
        text="We put off risky changes until tests pass.\n"
        "Keep the retry limit and timeout clear before shipping.",
    )
    source_path.write_text(transcript.model_dump_json(), encoding="utf-8")

    feed_url = "https://publisher.example/acceptance.rss"
    feed = b"""<?xml version='1.0' encoding='UTF-8'?>
    <rss version='2.0'><channel><title>Synthetic feed</title>
      <link>https://publisher.example/</link><description>Synthetic feed.</description>
      <item><guid>acceptance-episode</guid><title>Acceptance episode</title></item>
    </channel></rss>"""
    requests: list[str] = []

    class Opener:
        def __init__(self, _handler: object) -> None:
            pass

        def open(self, request: Any, *, timeout: float) -> SyntheticResponse:
            _ = timeout
            requests.append(request.full_url)
            assert request.full_url == feed_url
            return SyntheticResponse(feed)

    monkeypatch.setattr(
        "audigest.sources.remote.urllib.request.build_opener",
        lambda handler: Opener(handler),
    )
    runner = CliRunner()
    ingest = runner.invoke(
        app,
        [
            "ingest",
            feed_url,
            "--episode",
            "acceptance-episode",
            "--transcript",
            str(source_path),
            "--json",
        ],
    )
    assert ingest.exit_code == 0, ingest.stderr
    canonical = json.loads(ingest.stdout)
    assert requests == [feed_url]
    transcript_id = canonical["id"]
    assert [segment["start"] for segment in canonical["segments"]] == [3.0, None]
    assert [segment["end"] for segment in canonical["segments"]] == [5.5, None]
    assert canonical["segments"][0]["speaker"] == "Host"
    assert canonical["origin"] == {
        "kind": "canonical",
        "uri": source_path.resolve().as_uri(),
        "format": "json",
    }
    source_path.unlink()
    assert not source_path.exists()

    project = tmp_path / "sample-project"
    project.mkdir()
    (project / "README.md").write_text(
        "Release checklist: run tests and keep retries bounded.", encoding="utf-8"
    )
    project_before = _snapshot_tree(project)
    canonical_before = _snapshot_tree(cache / "items")
    refs_before = _snapshot_tree(cache / "refs")

    # Processors depend on the stored transcript, not STT language/provider settings.
    monkeypatch.setenv("AUDIGEST_LANGUAGE", "not a valid STT language")
    monkeypatch.setenv("AUDIGEST_STT_PROVIDER", "not-a-provider")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("stored-transcript processors must not reacquire or transcribe sources")

    monkeypatch.setattr("audigest.service.LocalFileResolver.resolve", unexpected)
    monkeypatch.setattr("audigest.service.LocalTranscriptAcquirer.acquire", unexpected)
    monkeypatch.setattr("audigest.service.get_provider", unexpected)
    monkeypatch.setattr(
        "audigest.sources.remote.urllib.request.build_opener",
        lambda *_args, **_kwargs: pytest.fail("processors must not fetch source URLs"),
    )

    summary_provider = SyntheticSummaryProvider()
    application_provider = SyntheticApplicationProvider()
    english_provider = SyntheticEnglishProvider()
    monkeypatch.setattr(
        "audigest.processors.service.OpenAISummaryProvider",
        lambda model: summary_provider if model == "gpt-6-luna" else unexpected(model),
    )
    monkeypatch.setattr(
        "audigest.processors.apply_service.OpenAIApplicationProvider",
        lambda model: application_provider if model == "gpt-6-luna" else unexpected(model),
    )
    monkeypatch.setattr(
        "audigest.processors.english_service.OpenAIEnglishProvider",
        lambda model: english_provider if model == "gpt-6-luna" else unexpected(model),
    )

    show_json = runner.invoke(app, ["show", transcript_id, "--json"])
    assert show_json.exit_code == 0, show_json.stderr
    assert json.loads(show_json.stdout) == canonical

    commands = {
        "summary": ["summarize", transcript_id, "--provider", "openai", "--json"],
        "application": [
            "apply",
            transcript_id,
            "--project",
            str(project),
            "--provider",
            "openai",
            "--json",
        ],
        "english": [
            "english",
            transcript_id,
            "--level",
            "B2",
            "--provider",
            "openai",
            "--json",
        ],
    }
    payloads: dict[str, dict[str, Any]] = {}
    for name, args in commands.items():
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stderr
        payloads[name] = json.loads(result.stdout)
        assert result.stdout.startswith("{")
        assert payloads[name]["transcript_id"] == transcript_id

    summary_payload = payloads["summary"]
    summary_citations = [point["citation"] for point in summary_payload["key_points"]]
    assert [citation["start"] for citation in summary_citations] == [3.0, None]
    assert [citation["end"] for citation in summary_citations] == [5.5, None]
    assert [citation["speaker"] for citation in summary_citations] == ["Host", None]
    assert len(payloads["application"]["recommendations"]) == 2
    application_evidence = [
        recommendation["transcript_evidence"]
        for recommendation in payloads["application"]["recommendations"]
    ]
    assert [evidence["start"] for evidence in application_evidence] == [3.0, None]
    assert [evidence["end"] for evidence in application_evidence] == [5.5, None]
    assert [evidence["speaker"] for evidence in application_evidence] == ["Host", None]
    assert len(payloads["english"]["items"]) == 2
    english_citations = [item["citation"] for item in payloads["english"]["items"]]
    assert [citation["start"] for citation in english_citations] == [3.0, None]
    assert [citation["end"] for citation in english_citations] == [5.5, None]
    assert [citation["speaker"] for citation in english_citations] == ["Host", None]
    assert summary_provider.calls == [transcript_id]
    assert application_provider.calls == [transcript_id]
    assert english_provider.calls == [(transcript_id, "B2")]
    assert "OPENAI_API_KEY" not in ingest.stdout
    assert "OPENAI_API_KEY" not in os.environ

    output_paths = {
        "summary": cache / "outputs" / "summaries" / summary_payload["id"],
        "application": cache / "outputs" / "applications" / payloads["application"]["id"],
        "english": cache / "outputs" / "english" / payloads["english"]["id"],
    }
    markdown_names = {
        "summary": "summary.md",
        "application": "application.md",
        "english": "english.md",
    }
    for name, output_dir in output_paths.items():
        json_name = {
            "summary": "summary.json",
            "application": "application.json",
            "english": "english.json",
        }[name]
        assert json.loads((output_dir / json_name).read_text(encoding="utf-8")) == payloads[name]
        assert (output_dir / markdown_names[name]).is_file()

    assert summary_provider.calls == [transcript_id]
    assert _snapshot_tree(cache / "items") == canonical_before
    assert _snapshot_tree(cache / "refs") == refs_before
    assert _snapshot_tree(project) == project_before
    complete_cache_before_repeats = _snapshot_tree(cache)

    def no_model(*_args: object, **_kwargs: object) -> None:
        pytest.fail("auto cache hits must not create or call an SDK/model")

    monkeypatch.setattr("audigest.processors.service.OpenAISummaryProvider", no_model)
    monkeypatch.setattr("audigest.processors.apply_service.OpenAIApplicationProvider", no_model)
    monkeypatch.setattr("audigest.processors.english_service.OpenAIEnglishProvider", no_model)
    cached_commands = {
        "summary": ["summarize", transcript_id, "--provider", "auto"],
        "application": ["apply", transcript_id, "--project", str(project), "--provider", "auto"],
        "english": ["english", transcript_id, "--level", "B2", "--provider", "auto"],
    }
    markdown_results: dict[str, str] = {}
    for name, args in cached_commands.items():
        cached_json = runner.invoke(app, [*args, "--json"])
        assert cached_json.exit_code == 0, cached_json.stderr
        assert json.loads(cached_json.stdout) == payloads[name]
        assert cached_json.stdout.startswith("{")
        human = runner.invoke(app, args)
        assert human.exit_code == 0, human.stderr
        markdown_path = output_paths[name] / markdown_names[name]
        markdown_results[name] = markdown_path.read_text(encoding="utf-8")
        assert human.stdout == markdown_results[name]
    human_show = runner.invoke(app, ["show", transcript_id])
    assert human_show.exit_code == 0, human_show.stderr
    stored_transcript_markdown = (cache / "items" / transcript_id / "transcript.md").read_text(
        encoding="utf-8"
    )
    assert human_show.stdout == stored_transcript_markdown
    assert _snapshot_tree(cache) == complete_cache_before_repeats
    assert _snapshot_tree(project) == project_before

    assert "00:00:03.000–00:00:05.500" in markdown_results["summary"]
    assert "segment 2" in markdown_results["summary"]
    assert "00:00:03.000–00:00:05.500" in markdown_results["application"]
    assert "segment 2" in markdown_results["application"]
    assert "00:00:03.000–00:00:05.500" in markdown_results["english"]
    assert "segment 2" in markdown_results["english"]
