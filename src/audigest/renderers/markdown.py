"""Markdown rendering for canonical transcripts."""

from datetime import date, datetime
from typing import Any

from audigest.models import Transcript


def format_timestamp(seconds: float) -> str:
    """Format seconds as a stable ``HH:MM:SS.mmm`` timestamp."""
    milliseconds = int(max(0, seconds) * 1000 + 0.5)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{milliseconds:03}"


def _metadata_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:g}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(transcript: Transcript) -> str:
    """Render transcript metadata and timestamped segments as Markdown."""
    source = transcript.source
    rows = (
        ("Transcript ID", transcript.id),
        ("Schema version", transcript.schema_version),
        ("Source type", source.type),
        ("Source URI", source.uri),
        ("Origin kind", transcript.origin.kind if transcript.origin else None),
        ("Origin URI", transcript.origin.uri if transcript.origin else None),
        ("Origin format", transcript.origin.format if transcript.origin else None),
        ("Title", source.title),
        ("Author", source.author),
        ("Published at", source.published_at),
        ("Duration (seconds)", source.duration_seconds),
        ("Language", transcript.language),
        ("Provider", transcript.provider),
        ("Model", transcript.model),
        ("Created at", transcript.created_at),
    )
    lines = [
        "# Transcript",
        "",
        "## Metadata",
        "",
        "| Field | Value |",
        "| --- | --- |",
    ]
    lines.extend(f"| {key} | {_metadata_value(value)} |" for key, value in rows)
    lines.extend(("", "## Segments", ""))

    for segment in transcript.segments:
        time_range = ""
        if segment.start is not None and segment.end is not None:
            time_range = f"[{format_timestamp(segment.start)} – {format_timestamp(segment.end)}] "
        speaker = f"**{_metadata_value(segment.speaker)}:** " if segment.speaker else ""
        lines.append(f"- {time_range}{speaker}{segment.text}")

    lines.extend(("", "## Full text", "", transcript.text, ""))
    return "\n".join(lines)
