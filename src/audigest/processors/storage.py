"""Atomic storage for processor artifacts under the local cache's outputs directory."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError

from audigest.errors import StorageError
from audigest.processors.summary import SummaryArtifact, render_summary_markdown
from audigest.storage import atomic_write_text

_SAFE_SUMMARY_ID = re.compile(r"[0-9a-f]{64}\Z")


class FileSummaryStore:
    """Persist summary JSON and Markdown independently of canonical transcript entries."""

    def __init__(self, cache_root: str | Path) -> None:
        self.root = Path(cache_root).expanduser() / "outputs" / "summaries"

    def path_for(self, summary_id: str) -> Path:
        self._validate_id(summary_id)
        return self.root / summary_id

    def get(self, summary_id: str) -> SummaryArtifact | None:
        """Read and validate an existing summary, returning None only when absent."""

        entry = self.path_for(summary_id)
        try:
            if not entry.exists():
                return None
            if not entry.is_dir():
                raise StorageError("Summary cache entry is not a directory")
            json_path = entry / "summary.json"
            markdown_path = entry / "summary.md"
            if not json_path.is_file() or not markdown_path.is_file():
                raise StorageError("Summary cache entry is incomplete or corrupt")
            artifact = SummaryArtifact.model_validate_json(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
        except StorageError:
            raise
        except (OSError, ValidationError, ValueError):
            raise StorageError("Summary cache entry is corrupt or unreadable") from None
        if artifact.id != summary_id or markdown != render_summary_markdown(artifact):
            raise StorageError("Summary cache entry is inconsistent")
        return artifact

    def put(self, artifact: SummaryArtifact) -> tuple[Path, Path]:
        """Atomically write a completed summary and its rendered Markdown."""

        entry = self.path_for(artifact.id)
        try:
            entry.mkdir(parents=True, exist_ok=True)
            json_path = entry / "summary.json"
            markdown_path = entry / "summary.md"
            atomic_write_text(json_path, artifact.model_dump_json(indent=2) + "\n")
            atomic_write_text(markdown_path, render_summary_markdown(artifact))
        except StorageError:
            raise
        except OSError:
            raise StorageError("Could not write the summary cache entry") from None
        return json_path, markdown_path

    @staticmethod
    def _validate_id(summary_id: str) -> None:
        if not _SAFE_SUMMARY_ID.fullmatch(summary_id):
            raise StorageError("Invalid summary cache ID")


__all__ = ["FileSummaryStore"]
