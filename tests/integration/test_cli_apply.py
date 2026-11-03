"""Offline CLI tests for read-only, canonical-only project application."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audigest.cli import app
from audigest.errors import MissingDependencyError, ProcessorError
from audigest.models import Transcript
from audigest.processors.application import (
    ApplicationArtifact,
    ApplicationRecommendationDraft,
    ApplicationResponse,
    ProjectEvidenceDraft,
    TranscriptEvidenceDraft,
    render_application_markdown,
)


class SyntheticApplicationProvider:
    name = "openai"

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls: list[tuple[str, list[str]]] = []

    def apply(self, transcript: Transcript, project: Any) -> ApplicationResponse:
        self.calls.append((transcript.id, [item.relative_path for item in project.files]))
        if not transcript.segments or not project.files:
            return ApplicationResponse(
                overview="No relevant project changes are suggested.",
                recommendations=[],
            )
        segment = transcript.segments[0]
        context_file = project.files[0]
        return ApplicationResponse(
            overview="The project could make one reliability improvement.",
            recommendations=[
                ApplicationRecommendationDraft(
                    idea="Add a bounded retry policy.",
                    relevance="The project currently retries requests.",
                    suggested_action="Document and test the retry limit.",
                    transcript_evidence=TranscriptEvidenceDraft(
                        segment_index=0,
                        quote=segment.text[:40],
                    ),
                    project_evidence=ProjectEvidenceDraft(
                        relative_path=context_file.relative_path,
                        quote=context_file.text[:40],
                    ),
                )
            ],
        )


def prepare_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f"cache_dir = {json.dumps(str(cache))}\n", encoding="utf-8")
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
    return cache


def create_transcript(tmp_path: Path) -> tuple[str, Path]:
    source = tmp_path / "synthetic-transcript.txt"
    source.write_text(
        "A synthetic event handler retries safely and limits duplicate requests.",
        encoding="utf-8",
    )
    ingested = CliRunner().invoke(app, ["ingest", str(source), "--json"])
    assert ingested.exit_code == 0, ingested.stderr
    return json.loads(ingested.stdout)["id"], source


def create_project(tmp_path: Path, *, name: str = "synthetic-project") -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "README.md").write_text(
        "Synthetic service retries requests with bounded delays.",
        encoding="utf-8",
    )
    return project


def snapshot_project(root: Path) -> dict[Path, tuple[str, int, bytes | str | None]]:
    snapshot: dict[Path, tuple[str, int, bytes | str | None]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        info = path.lstat()
        if path.is_symlink():
            snapshot[relative] = ("symlink", info.st_mtime_ns, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", info.st_mtime_ns, None)
        elif path.is_file():
            snapshot[relative] = ("file", info.st_mtime_ns, path.read_bytes())
    return snapshot


def install_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: SyntheticApplicationProvider,
) -> None:
    monkeypatch.setattr(
        "audigest.processors.apply_service.OpenAIApplicationProvider",
        lambda model: provider if model == provider.model else pytest.fail("unexpected model"),
    )


def test_apply_uses_only_stored_transcript_default_readme_and_keeps_project_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = prepare_environment(monkeypatch, tmp_path)
    transcript_id, source = create_transcript(tmp_path)
    project = create_project(tmp_path, name="private-project-name")
    (project / "unselected.txt").write_text("Unselected private context.", encoding="utf-8")
    source.unlink()
    project_before = snapshot_project(project)
    canonical_before = {
        path.relative_to(cache): path.read_bytes()
        for path in (cache / "items" / transcript_id).rglob("*")
        if path.is_file()
    }
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("apply must not resolve a source, acquire a transcript, or run STT")

    monkeypatch.setattr("audigest.service.LocalFileResolver.resolve", unexpected)
    monkeypatch.setattr("audigest.service.LocalTranscriptAcquirer.acquire", unexpected)
    monkeypatch.setattr("audigest.service.get_provider", unexpected)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--provider", "openai", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["transcript_id"] == transcript_id
    assert payload["project_name"] == "private-project-name"
    assert [item["relative_path"] for item in payload["context_files"]] == ["README.md"]
    assert payload["recommendations"][0]["transcript_evidence"]["quote"] in (
        "A synthetic event handler retries safely and limits duplicate requests."
    )
    assert payload["recommendations"][0]["project_evidence"]["relative_path"] == "README.md"
    assert str(project) not in result.stdout
    assert "Unselected private context" not in result.stdout
    assert provider.calls == [(transcript_id, ["README.md"])]
    assert snapshot_project(project) == project_before
    assert {
        path.relative_to(cache): path.read_bytes()
        for path in (cache / "items" / transcript_id).rglob("*")
        if path.is_file()
    } == canonical_before

    application_id = payload["id"]
    entry = cache / "outputs" / "applications" / application_id
    stored_markdown = (entry / "application.md").read_text(encoding="utf-8")
    assert json.loads((entry / "application.json").read_text(encoding="utf-8")) == payload

    monkeypatch.setattr(
        "audigest.processors.apply_service.OpenAIApplicationProvider",
        lambda _model: pytest.fail("auto cache hit must not select the SDK"),
    )
    cached = runner.invoke(app, ["apply", transcript_id, "--project", str(project), "--json"])
    assert cached.exit_code == 0, cached.stderr
    assert json.loads(cached.stdout) == payload
    assert "Using cached application." in cached.stderr

    human = runner.invoke(app, ["apply", transcript_id, "--project", str(project)])
    assert human.exit_code == 0, human.stderr
    assert human.stdout == stored_markdown
    assert human.stdout.startswith("# Project application\n")
    assert snapshot_project(project) == project_before


def test_explicit_context_files_are_deduplicated_sorted_and_never_globbed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    (project / "design.md").write_text("Synthetic design decisions.", encoding="utf-8")
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)
    runner = CliRunner()
    selected = [
        "apply",
        transcript_id,
        "--project",
        str(project),
        "--provider",
        "openai",
        "--context",
        "design.md",
        "--context",
        "README.md",
        "--context",
        "design.md",
        "--json",
    ]

    first = runner.invoke(app, selected)
    assert first.exit_code == 0, first.stderr
    payload = json.loads(first.stdout)
    assert [item["relative_path"] for item in payload["context_files"]] == [
        "README.md",
        "design.md",
    ]
    assert provider.calls == [(transcript_id, ["README.md", "design.md"])]

    reversed_order = runner.invoke(
        app,
        [
            "apply",
            transcript_id,
            "--project",
            str(project),
            "--context",
            "README.md",
            "--context",
            "design.md",
            "--provider",
            "auto",
            "--json",
        ],
    )
    assert reversed_order.exit_code == 0, reversed_order.stderr
    assert json.loads(reversed_order.stdout) == payload
    assert len(provider.calls) == 1

    glob_like = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--context", "*.md", "--json"],
    )
    assert glob_like.exit_code == 3
    assert "*.md" in glob_like.stderr


def test_context_changes_invalidate_cache_but_unselected_files_do_not(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    (project / "unselected.md").write_text("First unselected contents.", encoding="utf-8")
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--provider", "openai", "--json"],
    )
    assert first.exit_code == 0, first.stderr
    first_payload = json.loads(first.stdout)
    (project / "unselected.md").write_text("Changed unselected contents.", encoding="utf-8")

    unchanged = runner.invoke(app, ["apply", transcript_id, "--project", str(project), "--json"])
    assert unchanged.exit_code == 0, unchanged.stderr
    assert json.loads(unchanged.stdout) == first_payload
    assert len(provider.calls) == 1

    (project / "README.md").write_text(
        "Synthetic service retries requests with a stricter bounded delay.",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "audigest.processors.apply_service.OpenAIApplicationProvider",
        lambda _model: pytest.fail("changed context must miss auto cache without selecting API"),
    )
    miss = runner.invoke(app, ["apply", transcript_id, "--project", str(project), "--json"])
    assert miss.exit_code == 6
    assert miss.stdout == ""
    assert "cache-only" in miss.stderr

    replacement = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, replacement)
    refreshed = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--provider", "openai", "--json"],
    )
    assert refreshed.exit_code == 0, refreshed.stderr
    assert json.loads(refreshed.stdout)["id"] != first_payload["id"]
    assert len(replacement.calls) == 1
    assert (cache / "outputs" / "applications" / first_payload["id"]).is_dir()


def test_no_cache_refreshes_only_when_provider_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--provider", "openai", "--json"],
    )
    first_payload = json.loads(first.stdout)

    no_provider = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--no-cache", "--json"],
    )
    assert no_provider.exit_code == 6
    assert "cache-only" in no_provider.stderr
    assert len(provider.calls) == 1

    refreshed = runner.invoke(
        app,
        [
            "apply",
            transcript_id,
            "--project",
            str(project),
            "--provider",
            "openai",
            "--no-cache",
            "--json",
        ],
    )
    assert refreshed.exit_code == 0, refreshed.stderr
    assert json.loads(refreshed.stdout)["id"] == first_payload["id"]
    assert json.loads(refreshed.stdout)["created_at"] != first_payload["created_at"]
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "context_path",
    [
        "../README.md",
        "/tmp/private.md",
        ".env",
        "docs/.secret.md",
        "./README.md",
    ],
)
def test_unsafe_context_paths_fail_before_provider_call(
    context_path: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)

    result = CliRunner().invoke(
        app,
        [
            "apply",
            transcript_id,
            "--project",
            str(project),
            "--context",
            context_path,
            "--provider",
            "openai",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert provider.calls == []
    assert str(project) not in result.stderr


@pytest.mark.parametrize("kind", ["file-symlink", "directory-symlink", "directory", "fifo"])
def test_symlink_and_nonregular_project_context_are_rejected(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("Outside private data.", encoding="utf-8")
    if kind == "file-symlink":
        (project / "linked.md").symlink_to(outside / "secret.md")
        relative = "linked.md"
    elif kind == "directory-symlink":
        (project / "linked").symlink_to(outside, target_is_directory=True)
        relative = "linked/secret.md"
    elif kind == "directory":
        (project / "folder").mkdir()
        relative = "folder"
    else:
        os.mkfifo(project / "pipe")
        relative = "pipe"
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)

    result = CliRunner().invoke(
        app,
        [
            "apply",
            transcript_id,
            "--project",
            str(project),
            "--context",
            relative,
            "--provider",
            "openai",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert provider.calls == []
    assert "Outside private data" not in result.stderr
    assert str(project) not in result.stderr


@pytest.mark.parametrize(
    ("filename", "contents", "expected_exit"),
    [
        ("missing.md", None, 3),
        ("empty.md", b"", 2),
        ("invalid.md", b"\xff", 2),
        ("large.md", b"x" * (64 * 1024 + 1), 2),
    ],
)
def test_missing_empty_invalid_utf8_and_oversized_context_categories(
    filename: str,
    contents: bytes | None,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    if contents is not None:
        (project / filename).write_bytes(contents)
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)

    result = CliRunner().invoke(
        app,
        [
            "apply",
            transcript_id,
            "--project",
            str(project),
            "--context",
            filename,
            "--provider",
            "openai",
            "--json",
        ],
    )

    assert result.exit_code == expected_exit
    assert result.stdout == ""
    assert provider.calls == []
    assert "\xff" not in result.stderr
    assert str(project) not in result.stderr


def test_context_file_count_limit_is_enforced_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)
    arguments = ["apply", transcript_id, "--project", str(project), "--provider", "openai"]
    for number in range(17):
        arguments.extend(("--context", f"file-{number:02}.md"))
    arguments.append("--json")

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 2
    assert "no more than 16" in result.stderr
    assert provider.calls == []


def test_missing_project_and_missing_transcript_have_stable_categories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)
    runner = CliRunner()

    bad_project = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(tmp_path / "missing-project"), "--json"],
    )
    missing_transcript = runner.invoke(
        app,
        [
            "apply",
            "missing-transcript-id",
            "--project",
            str(create_project(tmp_path)),
            "--json",
        ],
    )

    assert bad_project.exit_code == 2
    assert str(tmp_path / "missing-project") not in bad_project.stderr
    assert missing_transcript.exit_code == 3
    assert "was not found in the local cache" in missing_transcript.stderr
    assert provider.calls == []


@pytest.mark.parametrize("component", ["outputs", "applications", "entry"])
def test_no_cache_write_rejects_cache_symlinks_that_point_into_project(
    component: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)
    runner = CliRunner()
    initial = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--provider", "openai", "--json"],
    )
    assert initial.exit_code == 0, initial.stderr
    application_id = json.loads(initial.stdout)["id"]
    target = project / "cache-target"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("Keep this project file unchanged.", encoding="utf-8")
    entry = cache / "outputs" / "applications" / application_id
    if component == "entry":
        shutil.rmtree(entry)
        entry.symlink_to(target, target_is_directory=True)
    elif component == "applications":
        shutil.rmtree(cache / "outputs" / "applications")
        (cache / "outputs" / "applications").symlink_to(target, target_is_directory=True)
    else:
        shutil.rmtree(cache / "outputs")
        (cache / "outputs").symlink_to(target, target_is_directory=True)
    project_before = snapshot_project(project)

    result = runner.invoke(
        app,
        [
            "apply",
            transcript_id,
            "--project",
            str(project),
            "--provider",
            "openai",
            "--no-cache",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert "symbolic link" in result.stderr
    assert snapshot_project(project) == project_before
    assert sentinel.read_text(encoding="utf-8") == "Keep this project file unchanged."


def test_cache_read_rejects_symlinked_application_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)
    runner = CliRunner()
    created = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--provider", "openai", "--json"],
    )
    assert created.exit_code == 0, created.stderr
    application_id = json.loads(created.stdout)["id"]
    entry = cache / "outputs" / "applications" / application_id
    target = project / "application-cache-target"
    target.mkdir()
    (target / "application.json").write_text("synthetic outside artifact", encoding="utf-8")
    (target / "application.md").write_text("synthetic outside markdown", encoding="utf-8")
    shutil.rmtree(entry)
    entry.symlink_to(target, target_is_directory=True)
    project_before = snapshot_project(project)
    monkeypatch.setattr(
        "audigest.processors.apply_service.OpenAIApplicationProvider",
        lambda _model: pytest.fail("symlinked cache hit must fail before provider selection"),
    )

    result = runner.invoke(app, ["apply", transcript_id, "--project", str(project), "--json"])

    assert result.exit_code == 2
    assert "symbolic link" in result.stderr
    assert snapshot_project(project) == project_before


def test_cache_location_inside_project_is_rejected_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    monkeypatch.setenv("AUDIGEST_CACHE_DIR", str(project / "cache"))
    project_before = snapshot_project(project)
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)

    result = CliRunner().invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--provider", "openai", "--json"],
    )

    assert result.exit_code == 2
    assert "inside the project" in result.stderr
    assert snapshot_project(project) == project_before
    assert provider.calls == []


@pytest.mark.parametrize(
    "corruption",
    [
        "transcript_quote",
        "transcript_index",
        "timestamp",
        "speaker",
        "project_path",
        "project_quote",
        "overview",
    ],
)
def test_cached_application_evidence_is_revalidated_against_current_sources(
    corruption: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    provider = SyntheticApplicationProvider("gpt-6-luna")
    install_provider(monkeypatch, provider)
    runner = CliRunner()
    created = runner.invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--provider", "openai", "--json"],
    )
    assert created.exit_code == 0, created.stderr
    application_id = json.loads(created.stdout)["id"]
    entry = cache / "outputs" / "applications" / application_id
    artifact_path = entry / "application.json"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    recommendation = payload["recommendations"][0]
    if corruption == "transcript_quote":
        recommendation["transcript_evidence"]["quote"] = "not in canonical transcript"
    elif corruption == "transcript_index":
        recommendation["transcript_evidence"]["segment_index"] = 5
    elif corruption == "timestamp":
        recommendation["transcript_evidence"]["start"] = 50.0
        recommendation["transcript_evidence"]["end"] = 55.0
    elif corruption == "speaker":
        recommendation["transcript_evidence"]["speaker"] = "Invented speaker"
    elif corruption == "project_path":
        recommendation["project_evidence"]["relative_path"] = "not-selected.md"
    elif corruption == "project_quote":
        recommendation["project_evidence"]["quote"] = "not in selected project text"
    else:
        payload["overview"] = " \t "
    corrupted = ApplicationArtifact.model_validate(payload)
    artifact_path.write_text(corrupted.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (entry / "application.md").write_text(
        render_application_markdown(corrupted),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "audigest.processors.apply_service.OpenAIApplicationProvider",
        lambda _model: pytest.fail("corrupt cache must not call provider"),
    )

    result = runner.invoke(app, ["apply", transcript_id, "--project", str(project), "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "application cache evidence does not match" in result.stderr.lower()


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
def test_llm_selection_precedence_and_stt_configuration_independence(
    arguments: list[str],
    environment: dict[str, str],
    expected_model: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    cache = tmp_path / "cache"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        (
            'provider = "invalid-stt-provider"\nlanguage = 123\n'
            f"cache_dir = {json.dumps(str(cache))}\n"
        ),
        encoding="utf-8",
    )
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AUDIGEST_STT_PROVIDER", "invalid-stt-provider")
    monkeypatch.setenv("AUDIGEST_LANGUAGE", "not a valid STT language")
    selected_models: list[str] = []
    monkeypatch.setattr(
        "audigest.processors.apply_service.OpenAIApplicationProvider",
        lambda model: selected_models.append(model) or SyntheticApplicationProvider(model),
    )

    result = CliRunner().invoke(
        app,
        ["apply", transcript_id, "--project", str(project), *arguments, "--json"],
    )

    assert result.exit_code == 0, result.stderr
    assert selected_models == [expected_model]
    assert json.loads(result.stdout)["model"] == expected_model


@pytest.mark.parametrize(
    ("arguments", "environment", "transcript_arg", "expected_exit"),
    [
        (["--provider", "fake"], {}, None, 2),
        (["--model", "invalid model"], {}, None, 2),
        ([], {"AUDIGEST_LLM_PROVIDER": "elevenlabs"}, None, 2),
        ([], {"AUDIGEST_LLM_MODEL": "invalid model"}, None, 2),
        ([], {}, "../../invalid", 2),
        ([], {}, "missing-transcript-id", 3),
    ],
)
def test_invalid_llm_selection_and_transcript_ids_have_expected_exit_categories(
    arguments: list[str],
    environment: dict[str, str],
    transcript_arg: str | None,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    result = CliRunner().invoke(
        app,
        [
            "apply",
            transcript_arg or transcript_id,
            "--project",
            str(project),
            *arguments,
            "--json",
        ],
    )

    assert result.exit_code == expected_exit
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("failure", "expected_exit", "expected_message"),
    [
        (RuntimeError("private provider response"), 6, "failed unexpectedly"),
        (MissingDependencyError("expected missing SDK"), 7, "expected missing SDK"),
        (ProcessorError("expected provider error"), 6, "expected provider error"),
    ],
)
def test_provider_failures_are_sanitized_and_expected_errors_preserved(
    failure: Exception,
    expected_exit: int,
    expected_message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)

    class FailingProvider:
        name = "openai"
        model = "gpt-6-luna"

        def apply(self, _transcript: Transcript, _project: Any) -> ApplicationResponse:
            raise failure

    monkeypatch.setattr(
        "audigest.processors.apply_service.OpenAIApplicationProvider",
        lambda _model: FailingProvider(),
    )

    result = CliRunner().invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--provider", "openai", "--json"],
    )

    assert result.exit_code == expected_exit
    assert result.stdout == ""
    assert expected_message in result.stderr
    if isinstance(failure, RuntimeError):
        assert "private provider response" not in result.stderr


def test_cache_path_inside_project_error_does_not_expose_project_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_environment(monkeypatch, tmp_path)
    transcript_id, _source = create_transcript(tmp_path)
    project = create_project(tmp_path)
    monkeypatch.setenv("AUDIGEST_CACHE_DIR", str(project / "generated-cache"))

    result = CliRunner().invoke(
        app,
        ["apply", transcript_id, "--project", str(project), "--json"],
    )

    assert result.exit_code == 2
    assert str(project) not in result.stderr
    assert not (project / "generated-cache").exists()
