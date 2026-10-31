"""Deterministic acquisition of existing local transcripts and captions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from audigest.errors import SourceResolutionError
from audigest.models import SourceInfo, Transcript, TranscriptDraft, TranscriptSegment
from audigest.sources.base import ResolvedSource

_SIDECAR_SUFFIXES = (".transcript.json", ".vtt", ".srt", ".txt")
_FINGERPRINT_VERSION = "local-transcript-acquisition-v1"
_TIMESTAMP = r"(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{1,3}"
_TIMING_LINE = re.compile(rf"^\s*(?P<start>{_TIMESTAMP})\s*-->\s*(?P<end>{_TIMESTAMP})(?:\s+.*)?$")


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """Prepared transcript draft plus identity of the selected transcript input."""

    draft: TranscriptDraft
    source_fingerprint: str
    source_info: SourceInfo | None = None


class TranscriptAcquirer(Protocol):
    """Acquire existing transcript material before any STT fallback."""

    def acquire(self, resolved: ResolvedSource) -> AcquisitionResult | None:
        """Return an existing transcript/caption draft when one is available."""


class LocalTranscriptAcquirer:
    """Read direct local text or the first applicable adjacent sidecar."""

    def acquire(self, resolved: ResolvedSource) -> AcquisitionResult | None:
        """Prefer direct transcript inputs, then prioritized media sidecars."""

        source_path = resolved.media_path
        suffix = source_path.suffix.lower()
        if suffix in {".txt", ".json", ".srt", ".vtt"}:
            return self._acquire_file(source_path)

        for sidecar_suffix in _SIDECAR_SUFFIXES:
            candidate = source_path.with_name(f"{source_path.stem}{sidecar_suffix}")
            try:
                exists = candidate.exists()
                is_file = candidate.is_file() if exists else False
            except OSError as exc:
                raise SourceResolutionError("Could not inspect adjacent transcript files") from exc
            if exists:
                if not is_file:
                    raise SourceResolutionError(
                        "Adjacent transcript candidate is not a regular file"
                    )
                return self._acquire_file(
                    candidate,
                    source_fingerprint=resolved.content_sha256,
                    source_info=resolved.source_info,
                )
        return None

    def _acquire_file(
        self,
        path: Path,
        *,
        source_fingerprint: str | None = None,
        source_info: SourceInfo | None = None,
    ) -> AcquisitionResult:
        suffix = path.suffix.lower()
        content, input_fingerprint = self._read_text(path)
        strategy = "direct" if source_fingerprint is None else "sidecar"
        effective_fingerprint = _effective_fingerprint(
            strategy,
            path,
            input_fingerprint,
            source_fingerprint,
        )
        if suffix == ".txt":
            if not content.strip():
                raise SourceResolutionError("Text transcript input is empty")
            draft = TranscriptDraft(
                provider="text",
                model=None,
                language=None,
                segments=[TranscriptSegment(text=content)],
            )
        elif suffix == ".json":
            draft, canonical_source = self._canonical_json(content)
            source_info = canonical_source
        elif suffix in {".srt", ".vtt"}:
            draft = _caption_draft(content, suffix)
        else:
            raise SourceResolutionError("Unsupported transcript input type")
        return AcquisitionResult(
            draft=draft,
            source_fingerprint=effective_fingerprint,
            source_info=source_info,
        )

    @staticmethod
    def _read_text(path: Path) -> tuple[str, str]:
        try:
            content_bytes = path.read_bytes()
            content = content_bytes.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise SourceResolutionError(
                "Could not read transcript or caption input as UTF-8"
            ) from exc
        return content, hashlib.sha256(content_bytes).hexdigest()

    @staticmethod
    def _canonical_json(content: str) -> tuple[TranscriptDraft, SourceInfo]:
        try:
            transcript = Transcript.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise SourceResolutionError(
                "Canonical transcript JSON is invalid or inconsistent with its segments"
            ) from exc
        draft = TranscriptDraft(
            provider=transcript.provider,
            model=transcript.model,
            language=transcript.language,
            segments=transcript.segments,
        )
        return draft, transcript.source


def _effective_fingerprint(
    strategy: str,
    path: Path,
    input_sha256: str,
    source_sha256: str | None,
) -> str:
    """Bind parser version, selected input identity, and exact input bytes."""

    encoded = json.dumps(
        [
            _FINGERPRINT_VERSION,
            strategy,
            path.name,
            path.suffix.lower(),
            source_sha256,
            input_sha256,
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_timestamp(value: str) -> float:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        if minutes >= 60 or seconds >= 60:
            raise ValueError("invalid timestamp range")
        return float(hours * 3600 + minutes * 60) + seconds
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        if seconds >= 60:
            raise ValueError("invalid timestamp range")
        return float(minutes * 60) + seconds
    raise ValueError("invalid timestamp format")


def _caption_draft(content: str, suffix: str) -> TranscriptDraft:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n[ \t]*\n", normalized) if normalized else []
    if suffix == ".vtt" and blocks and blocks[0].startswith("WEBVTT"):
        blocks = blocks[1:]

    segments: list[TranscriptSegment] = []
    for block in blocks:
        lines = block.splitlines()
        if lines and lines[0].strip().upper().startswith(("NOTE", "STYLE", "REGION")):
            continue
        timing_index = next(
            (index for index, line in enumerate(lines[:2]) if "-->" in line),
            None,
        )
        if timing_index is None:
            raise SourceResolutionError("Caption input contains a malformed cue")
        match = _TIMING_LINE.fullmatch(lines[timing_index])
        if match is None:
            raise SourceResolutionError("Caption input contains a malformed cue timestamp")
        caption_text = "\n".join(lines[timing_index + 1 :]).strip()
        if not caption_text:
            raise SourceResolutionError("Caption input contains an empty cue")
        try:
            start = _parse_timestamp(match.group("start"))
            end = _parse_timestamp(match.group("end"))
            segments.append(TranscriptSegment(start=start, end=end, text=caption_text))
        except (ValueError, ValidationError) as exc:
            raise SourceResolutionError("Caption input contains an invalid cue timestamp") from exc

    if not segments:
        raise SourceResolutionError("Caption input contains no transcript cues")
    try:
        return TranscriptDraft(
            provider="captions",
            model=None,
            language=None,
            segments=segments,
        )
    except ValidationError as exc:
        raise SourceResolutionError("Caption cues must be ordered by start time") from exc


__all__ = ["AcquisitionResult", "LocalTranscriptAcquirer", "TranscriptAcquirer"]
