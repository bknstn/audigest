"""Local transcript cache with deterministic identities and atomic file writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from platformdirs import user_cache_path
from pydantic import ValidationError

from audigest.errors import StorageError
from audigest.models import SourceInfo, Transcript
from audigest.sources.base import ResolvedSource

_SAFE_CACHE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class TranscriptStore(Protocol):
    """Persistence contract for canonical transcripts."""

    def get(self, cache_id: str) -> Transcript | None:
        """Return a cached transcript, or ``None`` when there is no entry."""

    def find_for_source(
        self,
        resolved_source: ResolvedSource,
        source_fingerprint: str,
        requested_language: str | None,
    ) -> Transcript | None:
        """Return the latest cache entry for a source identity and language."""

    def put(
        self,
        transcript: Transcript,
        markdown: str,
        *,
        resolved_source: ResolvedSource,
        source_fingerprint: str,
        requested_language: str | None,
    ) -> Path:
        """Persist all canonical artifacts and return the transcript JSON path."""


def cache_id_for(
    resolved_source: ResolvedSource,
    provider: str,
    model: str | None,
    language: str | None,
    *,
    source_fingerprint: str | None = None,
) -> str:
    """Return a stable ID covering source identity, fingerprint, and STT options."""

    identity = [
        resolved_source.source_info.uri,
        source_fingerprint or resolved_source.content_sha256,
        provider,
        model,
        language,
    ]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_lookup_id_for(
    resolved_source: ResolvedSource,
    source_fingerprint: str,
    requested_language: str | None,
) -> str:
    """Return the provider-independent pointer key for a source/language pair."""

    identity = [resolved_source.source_info.uri, source_fingerprint, requested_language]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 text artifact in its destination directory."""

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class FileTranscriptStore:
    """Store source metadata, canonical JSON, and Markdown under one cache root."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = user_cache_path("audigest") if root is None else Path(root).expanduser()

    @property
    def items_root(self) -> Path:
        """Directory that contains cache entries."""

        return self.root / "items"

    @property
    def refs_root(self) -> Path:
        """Directory that stores latest source-to-cache pointers."""

        return self.root / "refs"

    def path_for(self, cache_id: str) -> Path:
        """Return the directory for a safe cache ID."""

        self._validate_cache_id(cache_id)
        return self.items_root / cache_id

    def transcript_path_for(self, cache_id: str) -> Path:
        """Return the canonical JSON artifact path for a safe cache ID."""

        return self.path_for(cache_id) / "transcript.json"

    def get(self, cache_id: str) -> Transcript | None:
        """Load a cache entry; incomplete or invalid entries fail clearly."""

        entry = self.path_for(cache_id)
        try:
            if not entry.exists():
                return None
            if not entry.is_dir():
                raise StorageError(f"Transcript cache entry '{cache_id}' is not a directory")

            source_path = entry / "source.json"
            transcript_path = entry / "transcript.json"
            markdown_path = entry / "transcript.md"
            if (
                not source_path.is_file()
                or not transcript_path.is_file()
                or not markdown_path.is_file()
            ):
                raise StorageError(f"Transcript cache entry '{cache_id}' is incomplete or corrupt")

            source = SourceInfo.model_validate_json(source_path.read_text(encoding="utf-8"))
            transcript = Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
        except StorageError:
            raise
        except (OSError, ValidationError, ValueError) as exc:
            raise StorageError(
                f"Transcript cache entry '{cache_id}' is corrupt or unreadable"
            ) from exc

        if transcript.id != cache_id or transcript.source != source:
            raise StorageError(f"Transcript cache entry '{cache_id}' is inconsistent")
        return transcript

    def find_for_source(
        self,
        resolved_source: ResolvedSource,
        source_fingerprint: str,
        requested_language: str | None,
    ) -> Transcript | None:
        """Resolve the latest atomic cache pointer for a source and requested language."""

        if not _SHA256.fullmatch(source_fingerprint):
            raise StorageError("Invalid source fingerprint")
        lookup_id = source_lookup_id_for(resolved_source, source_fingerprint, requested_language)
        ref_path = self.refs_root / f"{lookup_id}.json"
        try:
            if not ref_path.exists():
                return None
            reference = json.loads(ref_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError("Source cache reference is corrupt or unreadable") from exc

        expected = {
            "schema_version": 1,
            "source_uri": resolved_source.source_info.uri,
            "source_fingerprint": source_fingerprint,
            "requested_language": requested_language,
        }
        if not isinstance(reference, dict) or any(
            reference.get(key) != value for key, value in expected.items()
        ):
            raise StorageError("Source cache reference is inconsistent")
        cache_id = reference.get("transcript_id")
        if not isinstance(cache_id, str):
            raise StorageError("Source cache reference is corrupt")
        transcript = self.get(cache_id)
        if transcript is None:
            raise StorageError("Source cache reference points to a missing transcript")
        expected_cache_id = cache_id_for(
            resolved_source,
            provider=transcript.provider,
            model=transcript.model,
            language=requested_language,
            source_fingerprint=source_fingerprint,
        )
        if transcript.id != expected_cache_id:
            raise StorageError("Source cache reference points to an unrelated transcript")
        return transcript

    def put(
        self,
        transcript: Transcript,
        markdown: str,
        *,
        resolved_source: ResolvedSource,
        source_fingerprint: str,
        requested_language: str | None,
    ) -> Path:
        """Atomically replace each artifact file with a complete encoded value."""

        if not _SHA256.fullmatch(source_fingerprint):
            raise StorageError("Invalid source fingerprint")
        expected_cache_id = cache_id_for(
            resolved_source,
            provider=transcript.provider,
            model=transcript.model,
            language=requested_language,
            source_fingerprint=source_fingerprint,
        )
        if transcript.id != expected_cache_id:
            raise StorageError("Transcript ID does not match the source cache identity")
        entry = self.path_for(transcript.id)
        try:
            entry.mkdir(parents=True, exist_ok=True)
            source_json = transcript.source.model_dump_json(indent=2) + "\n"
            transcript_json = transcript.model_dump_json(indent=2) + "\n"
            self._atomic_write(entry / "source.json", source_json)
            self._atomic_write(entry / "transcript.json", transcript_json)
            self._atomic_write(entry / "transcript.md", markdown)
            self.refs_root.mkdir(parents=True, exist_ok=True)
            lookup_id = source_lookup_id_for(
                resolved_source, source_fingerprint, requested_language
            )
            reference = {
                "schema_version": 1,
                "source_uri": resolved_source.source_info.uri,
                "source_fingerprint": source_fingerprint,
                "requested_language": requested_language,
                "transcript_id": transcript.id,
            }
            reference_json = json.dumps(reference, ensure_ascii=False, indent=2) + "\n"
            self._atomic_write(self.refs_root / f"{lookup_id}.json", reference_json)
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(f"Could not write transcript cache entry '{transcript.id}'") from exc
        return entry / "transcript.json"

    @staticmethod
    def _validate_cache_id(cache_id: str) -> None:
        if not _SAFE_CACHE_ID.fullmatch(cache_id):
            raise StorageError("Invalid transcript cache ID")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        atomic_write_text(path, content)


__all__ = [
    "FileTranscriptStore",
    "TranscriptStore",
    "atomic_write_text",
    "cache_id_for",
    "source_lookup_id_for",
]
