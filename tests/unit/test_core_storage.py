"""Cache identity, safe paths, and file store behavior tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from audigest.errors import StorageError
from audigest.models import SourceInfo, Transcript, TranscriptSegment
from audigest.sources.local import ResolvedSource
from audigest.storage import FileTranscriptStore, cache_id_for, source_lookup_id_for


def _resolved(content_sha256: str = "a" * 64) -> ResolvedSource:
    return ResolvedSource(
        source_info=SourceInfo(type="local", uri="file:///tmp/source.wav", title="source"),
        media_path=Path("/tmp/source.wav"),
        content_sha256=content_sha256,
    )


def _cache_transcript(
    source: ResolvedSource | None = None,
    *,
    source_fingerprint: str = "e" * 64,
    requested_language: str | None = "en",
    provider: str = "fake",
    model: str | None = "fake-v1",
) -> Transcript:
    source = source or _resolved()
    cache_id = cache_id_for(
        source,
        provider,
        model,
        requested_language,
        source_fingerprint=source_fingerprint,
    )
    return Transcript(
        id=cache_id,
        source=source.source_info,
        provider=provider,
        model=model,
        language=requested_language,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        segments=[TranscriptSegment(start=0.0, end=1.0, text="Offline sample.")],
    )


def test_cache_id_changes_with_content_and_transcription_options() -> None:
    source = _resolved()
    baseline = cache_id_for(source, "fake", "fake-v1", "en")

    assert len(baseline) == 64
    assert cache_id_for(_resolved("c" * 64), "fake", "fake-v1", "en") != baseline
    assert cache_id_for(source, "fake", "fake-v1", "pl") != baseline
    assert cache_id_for(source, "fake", "other-model", "en") != baseline
    assert cache_id_for(source, "other", "fake-v1", "en") != baseline
    assert cache_id_for(source, "fake", "fake-v1", "en", source_fingerprint="d" * 64) != baseline


def test_file_store_writes_and_reads_canonical_artifacts(tmp_path: Path) -> None:
    store = FileTranscriptStore(tmp_path / "cache")
    transcript = _cache_transcript()

    output_path = store.put(
        transcript,
        "# Transcript\n\nOffline sample.\n",
        resolved_source=_resolved(),
        source_fingerprint="e" * 64,
        requested_language="en",
    )

    entry = store.path_for(transcript.id)
    assert output_path == entry / "transcript.json"
    assert (entry / "source.json").is_file()
    assert (entry / "transcript.md").read_text(encoding="utf-8").startswith("# Transcript")
    assert store.get(transcript.id) == transcript
    assert store.find_for_source(_resolved(), "e" * 64, "en") == transcript


def test_file_store_reports_missing_entry_as_cache_miss(tmp_path: Path) -> None:
    store = FileTranscriptStore(tmp_path / "cache")
    assert store.get("c" * 64) is None
    assert store.find_for_source(_resolved(), "e" * 64, "en") is None


def test_file_store_rejects_traversal_ids(tmp_path: Path) -> None:
    store = FileTranscriptStore(tmp_path / "cache")

    with pytest.raises(StorageError, match="Invalid transcript cache ID"):
        store.get("../outside")
    with pytest.raises(StorageError, match="Invalid transcript cache ID"):
        store.path_for("..\\outside")


def test_file_store_fails_clearly_for_corrupt_entries(tmp_path: Path) -> None:
    store = FileTranscriptStore(tmp_path / "cache")
    transcript = _cache_transcript()
    store.put(
        transcript,
        "# Transcript\n",
        resolved_source=_resolved(),
        source_fingerprint="e" * 64,
        requested_language="en",
    )
    store.transcript_path_for(transcript.id).write_text("{invalid json", encoding="utf-8")

    with pytest.raises(StorageError, match="corrupt or unreadable"):
        store.get(transcript.id)


def test_file_store_fails_clearly_for_incomplete_entries(tmp_path: Path) -> None:
    store = FileTranscriptStore(tmp_path / "cache")
    store.path_for("d" * 64).mkdir(parents=True)

    with pytest.raises(StorageError, match="incomplete or corrupt"):
        store.get("d" * 64)


def test_file_store_cleans_temporary_file_when_atomic_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileTranscriptStore(tmp_path / "cache")
    transcript = _cache_transcript()

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("audigest.storage.os.replace", fail_replace)
    with pytest.raises(StorageError, match="Could not write transcript cache entry"):
        store.put(
            transcript,
            "# Transcript\n",
            resolved_source=_resolved(),
            source_fingerprint="e" * 64,
            requested_language="en",
        )

    assert list(store.path_for(transcript.id).iterdir()) == []


def test_file_store_updates_latest_source_pointer(tmp_path: Path) -> None:
    store = FileTranscriptStore(tmp_path / "cache")
    source = _resolved()
    first = _cache_transcript(requested_language=None)
    latest = _cache_transcript(requested_language=None, provider="text", model=None)
    put_kwargs = {
        "resolved_source": source,
        "source_fingerprint": "e" * 64,
        "requested_language": None,
    }

    store.put(first, "first", **put_kwargs)
    store.put(latest, "latest", **put_kwargs)

    assert store.find_for_source(source, "e" * 64, None) == latest


def test_file_store_fails_on_corrupt_source_pointer(tmp_path: Path) -> None:
    store = FileTranscriptStore(tmp_path / "cache")
    source = _resolved()
    transcript = _cache_transcript(source, requested_language=None)
    store.put(
        transcript,
        "# Transcript\n",
        resolved_source=source,
        source_fingerprint="e" * 64,
        requested_language=None,
    )
    refs = list(store.refs_root.glob("*.json"))
    assert len(refs) == 1
    refs[0].write_text("{invalid", encoding="utf-8")

    with pytest.raises(StorageError, match="Source cache reference is corrupt"):
        store.find_for_source(source, "e" * 64, None)


def test_file_store_rejects_source_pointer_to_unrelated_valid_transcript(
    tmp_path: Path,
) -> None:
    store = FileTranscriptStore(tmp_path / "cache")
    source = _resolved()
    other_source = ResolvedSource(
        source_info=SourceInfo(type="local", uri="file:///tmp/other.wav", title="other"),
        media_path=Path("/tmp/other.wav"),
        content_sha256="f" * 64,
    )
    transcript = _cache_transcript(source, requested_language=None)
    unrelated = _cache_transcript(
        other_source,
        source_fingerprint="f" * 64,
        requested_language=None,
    )
    store.put(
        unrelated,
        "unrelated",
        resolved_source=other_source,
        source_fingerprint="f" * 64,
        requested_language=None,
    )
    store.put(
        transcript,
        "transcript",
        resolved_source=source,
        source_fingerprint="e" * 64,
        requested_language=None,
    )
    lookup_id = source_lookup_id_for(source, "e" * 64, None)
    ref_path = store.refs_root / f"{lookup_id}.json"
    reference = ref_path.read_text(encoding="utf-8")
    assert transcript.id in reference
    ref_path.write_text(reference.replace(transcript.id, unrelated.id), encoding="utf-8")

    with pytest.raises(StorageError, match="unrelated transcript"):
        store.find_for_source(source, "e" * 64, None)
