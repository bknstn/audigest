"""Offline tests for safe, bounded project context selection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import audigest.processors.project_context as project_context
from audigest.errors import ConfigurationError, SourceResolutionError
from audigest.processors.project_context import (
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_FILES,
    MAX_CONTEXT_PATH_CHARS,
    normalize_context_paths,
    read_project_context,
)


def test_default_context_is_only_readme_and_requested_names_are_sorted_and_deduplicated(
    tmp_path: Path,
) -> None:
    project = tmp_path / "synthetic-project"
    (project / "docs").mkdir(parents=True)
    (project / "README.md").write_text("Synthetic project overview.", encoding="utf-8")
    (project / "docs" / "design.md").write_text("Synthetic project design.", encoding="utf-8")
    (project / "unselected.txt").write_text("Do not include this file.", encoding="utf-8")

    default = read_project_context(project)
    selected = read_project_context(
        project,
        ["docs/design.md", "README.md", "docs/design.md"],
    )

    assert default.name == "synthetic-project"
    assert [(item.relative_path, item.text) for item in default.files] == [
        ("README.md", "Synthetic project overview.")
    ]
    assert [item.relative_path for item in selected.files] == ["README.md", "docs/design.md"]
    assert [item.size_bytes for item in selected.files] == [
        len("Synthetic project overview."),
        len("Synthetic project design."),
    ]
    assert all(str(project) not in item.relative_path for item in selected.files)


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        "/outside/README.md",
        "C:/outside/README.md",
        "../README.md",
        "docs/../README.md",
        ".env",
        "docs/.private.md",
        "./README.md",
        "docs//design.md",
        "docs\\design.md",
        "bad\x00name.md",
        "a" * (MAX_CONTEXT_PATH_CHARS + 1),
    ],
)
def test_invalid_context_paths_are_rejected_before_read(relative_path: str) -> None:
    with pytest.raises(ConfigurationError):
        normalize_context_paths([relative_path])


def test_context_file_limit_is_checked_after_duplicate_removal() -> None:
    names = [f"docs/file-{index:02}.md" for index in range(MAX_CONTEXT_FILES + 1)]

    with pytest.raises(ConfigurationError, match="no more than"):
        normalize_context_paths(names)

    assert len(normalize_context_paths(names[:-1] + [names[0]])) == MAX_CONTEXT_FILES


@pytest.mark.parametrize("relative_path", ["missing.md", "docs/missing.md"])
def test_missing_context_file_has_source_resolution_category(
    relative_path: str,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(SourceResolutionError, match="was not found") as error:
        read_project_context(project, [relative_path])

    assert error.value.exit_code == 3


def test_symlink_file_and_symlink_directory_are_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "README.md").write_text("Outside synthetic context.", encoding="utf-8")
    (project / "README.md").symlink_to(outside / "README.md")
    (project / "docs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="symbolic link"):
        read_project_context(project, ["README.md"])
    with pytest.raises(ConfigurationError, match="symbolic link"):
        read_project_context(project, ["docs/README.md"])


def test_directories_and_fifos_are_rejected_without_blocking(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "folder").mkdir()
    os.mkfifo(project / "pipe")

    with pytest.raises(ConfigurationError, match="not a regular file"):
        read_project_context(project, ["folder"])
    with pytest.raises(ConfigurationError, match="not a regular file"):
        read_project_context(project, ["pipe"])


@pytest.mark.parametrize(
    ("name", "payload", "message"),
    [
        ("empty.md", b"", "empty"),
        ("blank.md", b" \t\n", "whitespace-only"),
        ("invalid.md", b"\xff", "valid UTF-8"),
        ("large.md", b"x" * (MAX_CONTEXT_BYTES + 1), "total limit"),
    ],
)
def test_empty_invalid_and_oversized_context_fail_safely(
    name: str,
    payload: bytes,
    message: str,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / name).write_bytes(payload)

    with pytest.raises(ConfigurationError, match=message) as error:
        read_project_context(project, [name])

    assert error.value.exit_code == 2
    decoded_payload = payload.decode("utf-8", errors="ignore")
    if decoded_payload:
        assert decoded_payload not in str(error.value)


def test_total_context_byte_limit_applies_across_selected_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "one.md").write_bytes(b"a" * (MAX_CONTEXT_BYTES // 2 + 1))
    (project / "two.md").write_bytes(b"b" * (MAX_CONTEXT_BYTES // 2))

    with pytest.raises(ConfigurationError, match="total limit"):
        read_project_context(project, ["one.md", "two.md"])


def test_os_read_failure_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("Synthetic context.", encoding="utf-8")

    def fail_read(_file_descriptor: int, _length: int) -> bytes:
        raise OSError("private filesystem detail")

    monkeypatch.setattr(project_context.os, "read", fail_read)
    with pytest.raises(ConfigurationError, match="cannot be read safely") as error:
        read_project_context(project)

    assert error.value.exit_code == 2
    assert "private filesystem detail" not in str(error.value)


def test_directory_fstat_failure_closes_open_descriptor_and_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    (project / "nested").mkdir(parents=True)
    (project / "nested" / "README.md").write_text("Synthetic nested context.", encoding="utf-8")
    real_open = os.open
    real_fstat = os.fstat
    real_close = os.close
    nested_descriptors: set[int] = set()
    closed_descriptors: set[int] = set()

    def tracking_open(path: str | bytes | os.PathLike[str], *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if path == "nested":
            nested_descriptors.add(descriptor)
        return descriptor

    def failing_fstat(file_descriptor: int) -> os.stat_result:
        if file_descriptor in nested_descriptors:
            raise OSError("private fstat detail")
        return real_fstat(file_descriptor)

    def tracking_close(file_descriptor: int) -> None:
        if file_descriptor in nested_descriptors:
            closed_descriptors.add(file_descriptor)
        real_close(file_descriptor)

    monkeypatch.setattr(project_context.os, "open", tracking_open)
    monkeypatch.setattr(project_context.os, "fstat", failing_fstat)
    monkeypatch.setattr(project_context.os, "close", tracking_close)

    with pytest.raises(ConfigurationError, match="changed or cannot be opened safely") as error:
        read_project_context(project, ["nested/README.md"])

    assert error.value.exit_code == 2
    assert "private fstat detail" not in str(error.value)
    assert nested_descriptors == closed_descriptors
    for descriptor in nested_descriptors:
        with pytest.raises(OSError):
            real_fstat(descriptor)
