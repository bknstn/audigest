"""Atomic storage for project application artifacts separate from canonical transcripts."""

from __future__ import annotations

import re
import stat
from pathlib import Path

from pydantic import ValidationError

from audigest.errors import StorageError
from audigest.processors.application import ApplicationArtifact, render_application_markdown
from audigest.storage import atomic_write_text

_SAFE_APPLICATION_ID = re.compile(r"[0-9a-f]{64}\Z")


class FileApplicationStore:
    """Persist application JSON and Markdown under the cache outputs directory."""

    def __init__(self, cache_root: str | Path) -> None:
        try:
            self.cache_root = Path(cache_root).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            raise StorageError("Application cache root cannot be resolved safely") from None
        self.root = self.cache_root / "outputs" / "applications"

    def path_for(self, application_id: str) -> Path:
        """Return the entry path for a safe content-addressed ID."""

        self._validate_id(application_id)
        return self.root / application_id

    def get(self, application_id: str) -> ApplicationArtifact | None:
        """Read a complete valid artifact, returning ``None`` only when absent."""

        entry = self.path_for(application_id)
        if not self._ensure_directory(self.cache_root, create=False):
            raise StorageError("Application cache root is missing")
        if not self._ensure_directory(self.cache_root / "outputs", create=False):
            return None
        if not self._ensure_directory(self.root, create=False):
            return None
        if not self._ensure_directory(entry, create=False):
            return None
        try:
            json_path = entry / "application.json"
            markdown_path = entry / "application.md"
            if not self._is_regular_file(json_path):
                raise StorageError("Application cache entry is incomplete or corrupt")
            if not self._is_regular_file(markdown_path):
                raise StorageError("Application cache entry is incomplete or corrupt")
            artifact = ApplicationArtifact.model_validate_json(
                json_path.read_text(encoding="utf-8")
            )
            markdown = markdown_path.read_text(encoding="utf-8")
        except StorageError:
            raise
        except (OSError, ValidationError, ValueError):
            raise StorageError("Application cache entry is corrupt or unreadable") from None
        if artifact.id != application_id or markdown != render_application_markdown(artifact):
            raise StorageError("Application cache entry is inconsistent")
        return artifact

    def put(self, artifact: ApplicationArtifact) -> tuple[Path, Path]:
        """Atomically save JSON and Markdown in their separate cache directory."""

        entry = self.path_for(artifact.id)
        if not self._ensure_directory(self.cache_root, create=False):
            raise StorageError("Application cache root is missing")
        self._ensure_directory(self.cache_root / "outputs", create=True)
        self._ensure_directory(self.root, create=True)
        self._ensure_directory(entry, create=True)
        try:
            json_path = entry / "application.json"
            markdown_path = entry / "application.md"
            atomic_write_text(json_path, artifact.model_dump_json(indent=2) + "\n")
            atomic_write_text(markdown_path, render_application_markdown(artifact))
        except StorageError:
            raise
        except OSError:
            raise StorageError("Could not write the application cache entry") from None
        return json_path, markdown_path

    @staticmethod
    def _validate_id(application_id: str) -> None:
        if not _SAFE_APPLICATION_ID.fullmatch(application_id):
            raise StorageError("Invalid application cache ID")

    def _ensure_directory(self, path: Path, *, create: bool) -> bool:
        """Reject symlinked descendants before reading or creating cache entries."""

        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                path.mkdir()
                info = path.lstat()
            except OSError:
                raise StorageError("Could not create an application cache directory") from None
        except OSError:
            raise StorageError("Application cache directory cannot be inspected safely") from None

        if stat.S_ISLNK(info.st_mode):
            raise StorageError("Application cache path contains a symbolic link")
        if not stat.S_ISDIR(info.st_mode):
            raise StorageError("Application cache path is not a directory")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise StorageError("Application cache directory cannot be resolved safely") from None
        if not resolved.is_relative_to(self.cache_root):
            raise StorageError("Application cache path escapes the configured cache root")
        return True

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise StorageError("Application cache file cannot be inspected safely") from None
        return stat.S_ISREG(info.st_mode)


__all__ = ["FileApplicationStore"]
