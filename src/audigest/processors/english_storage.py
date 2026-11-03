"""Atomic storage for generated English-learning artifacts."""

from __future__ import annotations

import re
import stat
from pathlib import Path

from pydantic import ValidationError

from audigest.errors import StorageError
from audigest.processors.english import EnglishArtifact, render_english_markdown
from audigest.storage import atomic_write_text

_SAFE_ENGLISH_ID = re.compile(r"[0-9a-f]{64}\Z")


class FileEnglishStore:
    """Persist English JSON and Markdown under the cache outputs directory."""

    def __init__(self, cache_root: str | Path) -> None:
        try:
            self.cache_root = Path(cache_root).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            raise StorageError("English cache root cannot be resolved safely") from None
        self.root = self.cache_root / "outputs" / "english"

    def path_for(self, english_id: str) -> Path:
        self._validate_id(english_id)
        return self.root / english_id

    def get(self, english_id: str) -> EnglishArtifact | None:
        """Read a complete valid result, returning None only when absent."""

        entry = self.path_for(english_id)
        if not self._ensure_directory(self.cache_root, create=False):
            raise StorageError("English cache root is missing")
        if not self._ensure_directory(self.cache_root / "outputs", create=False):
            return None
        if not self._ensure_directory(self.root, create=False):
            return None
        if not self._ensure_directory(entry, create=False):
            return None
        try:
            json_path = entry / "english.json"
            markdown_path = entry / "english.md"
            if not self._is_regular_file(json_path) or not self._is_regular_file(markdown_path):
                raise StorageError("English cache entry is incomplete or corrupt")
            artifact = EnglishArtifact.model_validate_json(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
        except StorageError:
            raise
        except (OSError, ValidationError, ValueError):
            raise StorageError("English cache entry is corrupt or unreadable") from None
        if artifact.id != english_id or markdown != render_english_markdown(artifact):
            raise StorageError("English cache entry is inconsistent")
        return artifact

    def put(self, artifact: EnglishArtifact) -> tuple[Path, Path]:
        """Atomically write JSON and Markdown output."""

        entry = self.path_for(artifact.id)
        if not self._ensure_directory(self.cache_root, create=False):
            raise StorageError("English cache root is missing")
        self._ensure_directory(self.cache_root / "outputs", create=True)
        self._ensure_directory(self.root, create=True)
        self._ensure_directory(entry, create=True)
        try:
            json_path = entry / "english.json"
            markdown_path = entry / "english.md"
            atomic_write_text(json_path, artifact.model_dump_json(indent=2) + "\n")
            atomic_write_text(markdown_path, render_english_markdown(artifact))
        except StorageError:
            raise
        except OSError:
            raise StorageError("Could not write the English cache entry") from None
        return json_path, markdown_path

    @staticmethod
    def _validate_id(english_id: str) -> None:
        if not _SAFE_ENGLISH_ID.fullmatch(english_id):
            raise StorageError("Invalid English cache ID")

    def _ensure_directory(self, path: Path, *, create: bool) -> bool:
        """Reject symlinked descendants before reading or creating entries."""

        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                path.mkdir()
                info = path.lstat()
            except OSError:
                raise StorageError("Could not create an English cache directory") from None
        except OSError:
            raise StorageError("English cache directory cannot be inspected safely") from None

        if stat.S_ISLNK(info.st_mode):
            raise StorageError("English cache path contains a symbolic link")
        if not stat.S_ISDIR(info.st_mode):
            raise StorageError("English cache path is not a directory")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise StorageError("English cache directory cannot be resolved safely") from None
        if not resolved.is_relative_to(self.cache_root):
            raise StorageError("English cache path escapes the configured cache root")
        return True

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise StorageError("English cache file cannot be inspected safely") from None
        return stat.S_ISREG(info.st_mode)


__all__ = ["FileEnglishStore"]
