"""Explicit local Whisper speech-to-text using a prepared CTranslate2 model."""

from __future__ import annotations

import importlib
import math
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from audigest.errors import MissingDependencyError, ProviderError
from audigest.models import TranscriptDraft, TranscriptSegment

MODEL_DIR_ENV = "AUDIGEST_WHISPER_MODEL_DIR"
MODEL_BACKEND = "faster-whisper/ctranslate2-cpu-int8"
_REQUIRED_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")
_LANGUAGE_CODE = re.compile(r"[A-Za-z]{2,3}\Z")

ModelFactory = Callable[..., Any]


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _model_dir() -> Path:
    raw_path = os.environ.get(MODEL_DIR_ENV)
    if raw_path is None or not raw_path.strip():
        raise MissingDependencyError(
            f"Local Whisper requires {MODEL_DIR_ENV} to point to an already prepared "
            "CTranslate2 model directory. Audigest does not download model files."
        )
    try:
        path = Path(raw_path).expanduser()
        if not path.is_dir() or any(not (path / name).is_file() for name in _REQUIRED_MODEL_FILES):
            raise MissingDependencyError(
                f"{MODEL_DIR_ENV} must point to a local CTranslate2 model directory containing "
                "model.bin, config.json, and tokenizer.json. Prepare the model separately; "
                "Audigest does not download model files."
            )
    except (OSError, RuntimeError, ValueError):
        raise MissingDependencyError(
            f"{MODEL_DIR_ENV} must point to a readable local CTranslate2 model directory."
        ) from None
    return path


def _default_model_factory(model_path: Path) -> Any:
    try:
        module = importlib.import_module("faster_whisper")
    except ImportError:
        raise MissingDependencyError(
            "Local Whisper requires the optional faster-whisper package. Install Audigest with "
            "the 'local' extra (uv sync --extra local)."
        ) from None

    model_type = getattr(module, "WhisperModel", None)
    if model_type is None:
        raise MissingDependencyError(
            "The installed faster-whisper package does not provide WhisperModel. Reinstall the "
            "'local' extra."
        )
    return model_type(
        str(model_path),
        device="cpu",
        compute_type="int8",
        local_files_only=True,
    )


def _segment(value: Any) -> TranscriptSegment:
    text = _field(value, "text")
    start = _field(value, "start")
    end = _field(value, "end")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("Local Whisper returned an empty or invalid transcript segment.")
    for timestamp in (start, end):
        if timestamp is not None and (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
        ):
            raise ProviderError("Local Whisper returned an invalid segment timestamp.")
    try:
        return TranscriptSegment(start=start, end=end, text=text)
    except ValidationError:
        raise ProviderError("Local Whisper returned invalid segment timing or text.") from None


def _response_language(info: Any, requested_language: str | None) -> str | None:
    language = _field(info, "language")
    if language is None:
        return requested_language
    if not isinstance(language, str) or _LANGUAGE_CODE.fullmatch(language) is None:
        raise ProviderError("Local Whisper returned an invalid language code.")
    return language


class LocalWhisperTranscriptionProvider:
    """Use only a pre-existing local CTranslate2 model, with no model downloads."""

    name = "local"

    def __init__(self, *, model_factory: ModelFactory | None = None) -> None:
        self._model_factory = model_factory
        self.model = MODEL_BACKEND

    def transcribe(self, media_path: Path, *, language: str | None) -> TranscriptDraft:
        """Transcribe local media and preserve the SDK's validated segment timing."""

        model_dir = _model_dir()
        if language is not None and _LANGUAGE_CODE.fullmatch(language) is None:
            raise ProviderError("Local Whisper language hints must be two- or three-letter codes.")
        try:
            size = media_path.stat().st_size
        except OSError:
            raise ProviderError(
                "Could not read the local media file for Whisper transcription."
            ) from None
        if size <= 0:
            raise ProviderError("Local Whisper cannot transcribe an empty media file.")

        model_factory = self._model_factory or _default_model_factory
        try:
            model = model_factory(model_dir)
            raw_segments, info = model.transcribe(
                str(media_path),
                language=language,
                vad_filter=False,
                word_timestamps=False,
            )
            segments = [_segment(raw_segment) for raw_segment in raw_segments]
            if not segments:
                raise ProviderError("Local Whisper returned no transcript segments.")
            response_language = _response_language(info, language)
            return TranscriptDraft(
                provider=self.name,
                model=f"{model_dir.name or 'model'} ({MODEL_BACKEND})",
                language=response_language,
                segments=segments,
            )
        except (MissingDependencyError, ProviderError):
            raise
        except Exception:
            raise ProviderError(
                "Local Whisper transcription failed. Check the local model directory and media "
                "file."
            ) from None


__all__ = ["LocalWhisperTranscriptionProvider", "MODEL_BACKEND", "MODEL_DIR_ENV"]
