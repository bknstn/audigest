"""Offline tests for the local Whisper adapter boundary."""

import importlib
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from audigest.errors import MissingDependencyError, ProviderError
from audigest.stt.local import (
    MODEL_BACKEND,
    MODEL_DIR_ENV,
    LocalWhisperTranscriptionProvider,
    _default_model_factory,
)


def make_model_dir(root: Path) -> Path:
    model_dir = root / "synthetic-model"
    model_dir.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (model_dir / name).write_text("synthetic model asset", encoding="utf-8")
    return model_dir


class MockModel:
    def __init__(
        self,
        segments: Any = None,
        language: str | None = "fr",
        error: Exception | None = None,
    ) -> None:
        self.segments = (
            segments
            if segments is not None
            else [SimpleNamespace(start=0.25, end=1.75, text="Synthetic local transcript.")]
        )
        self.language = language
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    def transcribe(self, media_path: str, **options: Any) -> Any:
        self.calls.append((media_path, options.get("language")))
        assert options["vad_filter"] is False
        assert options["word_timestamps"] is False
        if self.error is not None:
            raise self.error
        return self.segments, SimpleNamespace(language=self.language)


def test_default_factory_uses_explicit_local_cpu_int8_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir = make_model_dir(tmp_path)
    captured: dict[str, Any] = {}

    class MockWhisperModel:
        def __init__(self, path: str, **options: Any) -> None:
            captured.update(path=path, **options)

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(WhisperModel=MockWhisperModel)
            if name == "faster_whisper"
            else pytest.fail(f"unexpected import: {name}")
        ),
    )

    _default_model_factory(model_dir)

    assert captured == {
        "path": str(model_dir),
        "device": "cpu",
        "compute_type": "int8",
        "local_files_only": True,
    }


def test_transcription_preserves_timing_language_and_media_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir = make_model_dir(tmp_path)
    monkeypatch.setenv(MODEL_DIR_ENV, str(model_dir))
    media = tmp_path / "source.wav"
    original = b"synthetic local audio"
    media.write_bytes(original)
    model = MockModel()

    draft = LocalWhisperTranscriptionProvider(model_factory=lambda _path: model).transcribe(
        media,
        language="en",
    )

    assert draft.provider == "local"
    assert draft.model == f"synthetic-model ({MODEL_BACKEND})"
    assert str(tmp_path) not in draft.model
    assert draft.language == "fr"
    assert draft.segments[0].start == 0.25
    assert draft.segments[0].end == 1.75
    assert draft.segments[0].text == "Synthetic local transcript."
    assert model.calls == [(str(media), "en")]
    assert media.read_bytes() == original


def test_detected_language_falls_back_to_requested_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir = make_model_dir(tmp_path)
    monkeypatch.setenv(MODEL_DIR_ENV, str(model_dir))
    media = tmp_path / "source.wav"
    media.write_bytes(b"synthetic")
    model = MockModel(language=None)

    draft = LocalWhisperTranscriptionProvider(model_factory=lambda _path: model).transcribe(
        media,
        language="pl",
    )

    assert draft.language == "pl"


@pytest.mark.parametrize(
    "segment",
    [
        SimpleNamespace(start=-0.1, end=1.0, text="bad"),
        SimpleNamespace(start=0.0, end=math.inf, text="bad"),
        SimpleNamespace(start=2.0, end=1.0, text="bad"),
        SimpleNamespace(start=0.0, end=None, text="bad"),
        SimpleNamespace(start=None, end=None, text="   "),
    ],
)
def test_invalid_segments_fail_as_sanitized_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    segment: Any,
) -> None:
    model_dir = make_model_dir(tmp_path)
    monkeypatch.setenv(MODEL_DIR_ENV, str(model_dir))
    media = tmp_path / "source.wav"
    media.write_bytes(b"synthetic")
    provider = LocalWhisperTranscriptionProvider(
        model_factory=lambda _path: MockModel(segments=[segment])
    )

    with pytest.raises(ProviderError, match="invalid|empty") as error:
        provider.transcribe(media, language=None)

    assert error.value.exit_code == 5


def test_segment_order_is_validated_and_lazy_generator_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir = make_model_dir(tmp_path)
    monkeypatch.setenv(MODEL_DIR_ENV, str(model_dir))
    media = tmp_path / "source.wav"
    media.write_bytes(b"synthetic")
    out_of_order = MockModel(
        segments=[
            SimpleNamespace(start=2.0, end=3.0, text="later"),
            SimpleNamespace(start=1.0, end=1.5, text="earlier"),
        ]
    )
    with pytest.raises(ProviderError, match="transcription failed"):
        LocalWhisperTranscriptionProvider(model_factory=lambda _path: out_of_order).transcribe(
            media,
            language=None,
        )

    def broken_segments() -> Any:
        yield SimpleNamespace(start=0.0, end=1.0, text="first")
        raise RuntimeError("private model failure")

    lazy_model = MockModel(segments=broken_segments())
    with pytest.raises(ProviderError, match="transcription failed") as error:
        LocalWhisperTranscriptionProvider(model_factory=lambda _path: lazy_model).transcribe(
            media,
            language=None,
        )
    assert "private model failure" not in str(error.value)


def test_no_segments_is_a_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir = make_model_dir(tmp_path)
    monkeypatch.setenv(MODEL_DIR_ENV, str(model_dir))
    media = tmp_path / "silent.wav"
    media.write_bytes(b"synthetic silent media")
    provider = LocalWhisperTranscriptionProvider(model_factory=lambda _path: MockModel(segments=[]))

    with pytest.raises(ProviderError, match="no transcript segments"):
        provider.transcribe(media, language=None)


def test_model_factory_failures_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir = make_model_dir(tmp_path)
    monkeypatch.setenv(MODEL_DIR_ENV, str(model_dir))
    media = tmp_path / "source.wav"
    media.write_bytes(b"synthetic")

    def broken_factory(_path: Path) -> Any:
        raise RuntimeError("private model path and details")

    with pytest.raises(ProviderError, match="transcription failed") as error:
        LocalWhisperTranscriptionProvider(model_factory=broken_factory).transcribe(
            media,
            language=None,
        )
    assert str(tmp_path) not in str(error.value)
    assert "private model path" not in str(error.value)

    broken_model = MockModel(error=RuntimeError("private decoder details"))
    with pytest.raises(ProviderError, match="transcription failed") as error:
        LocalWhisperTranscriptionProvider(model_factory=lambda _path: broken_model).transcribe(
            media,
            language=None,
        )
    assert "private decoder details" not in str(error.value)


def test_missing_model_directory_or_required_assets_is_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    media = tmp_path / "source.wav"
    media.write_bytes(b"synthetic")
    provider = LocalWhisperTranscriptionProvider(
        model_factory=lambda _path: pytest.fail("must validate model assets before loading")
    )
    monkeypatch.delenv(MODEL_DIR_ENV, raising=False)
    with pytest.raises(MissingDependencyError, match=MODEL_DIR_ENV) as error:
        provider.transcribe(media, language=None)
    assert error.value.exit_code == 7

    model_dir = make_model_dir(tmp_path)
    (model_dir / "tokenizer.json").unlink()
    monkeypatch.setenv(MODEL_DIR_ENV, str(model_dir))
    with pytest.raises(MissingDependencyError, match="tokenizer.json"):
        provider.transcribe(media, language=None)


def test_missing_optional_sdk_is_reported_only_when_used(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir = make_model_dir(tmp_path)
    monkeypatch.setenv(MODEL_DIR_ENV, str(model_dir))
    media = tmp_path / "source.wav"
    media.write_bytes(b"synthetic")

    def no_sdk(name: str) -> Any:
        assert name == "faster_whisper"
        raise ModuleNotFoundError("synthetic missing optional SDK")

    monkeypatch.setattr(importlib, "import_module", no_sdk)
    with pytest.raises(MissingDependencyError, match="local.*extra") as error:
        LocalWhisperTranscriptionProvider().transcribe(media, language=None)
    assert error.value.exit_code == 7
