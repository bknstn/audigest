"""Strict Podcasting 2.0 JSON and declared-HTML transcript converters."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from pydantic import ValidationError

from audigest.errors import SourceResolutionError
from audigest.models import TranscriptDraft, TranscriptOrigin, TranscriptSegment

_IGNORED_TAGS = frozenset(
    {"footer", "form", "head", "header", "nav", "noscript", "script", "style"}
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_BLOCK_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class _Paragraph:
    text: str
    speaker: str | None
    scope: Literal["marked", "semantic", "body"]


@dataclass(slots=True)
class _Frame:
    tag: str
    marked_transcript: bool
    semantic_transcript: bool


class _PodcastHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[_Paragraph] = []
        self._frames: list[_Frame] = []
        self._ignored_stack: list[str] = []
        self._paragraph_text: list[str] | None = None
        self._paragraph_scope: Literal["marked", "semantic", "body"] = "body"
        self._paragraph_speaker: str | None = None
        self._speaker: str | None = None
        self._cite_text: list[str] | None = None
        self._time_depth = 0
        self.has_marked_container = False
        self.has_semantic_container = False

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {name.lower(): value or "" for name, value in attrs_list}
        if self._ignored_stack:
            if tag in _IGNORED_TAGS:
                if len(self._ignored_stack) >= 128:
                    raise SourceResolutionError("Declared RSS HTML transcript nesting is too deep")
                self._ignored_stack.append(tag)
            return
        if tag in _IGNORED_TAGS:
            self._ignored_stack.append(tag)
            return
        if tag == "br" and self._paragraph_text is not None:
            self._paragraph_text.append(" ")
        if tag in _VOID_TAGS:
            return
        if len(self._frames) >= 128:
            raise SourceResolutionError("Declared RSS HTML transcript nesting is too deep")

        marked = attrs.get("id", "").strip().casefold() == "transcript" or (
            "transcript" in {token.casefold() for token in attrs.get("class", "").split()}
        )
        semantic = tag in {"article", "main"}
        self.has_marked_container = self.has_marked_container or marked
        self.has_semantic_container = self.has_semantic_container or semantic
        self._frames.append(_Frame(tag=tag, marked_transcript=marked, semantic_transcript=semantic))
        if tag == "p":
            if self._paragraph_text is not None:
                raise SourceResolutionError("Declared RSS HTML transcript has nested paragraphs")
            self._paragraph_text = []
            self._paragraph_speaker = self._speaker
            if any(frame.marked_transcript for frame in self._frames):
                self._paragraph_scope = "marked"
            elif any(frame.semantic_transcript for frame in self._frames):
                self._paragraph_scope = "semantic"
            else:
                self._paragraph_scope = "body"
        elif tag == "cite":
            self._cite_text = []
        elif tag == "time":
            self._time_depth += 1

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:
            if tag == "br" and self._paragraph_text is not None:
                self._paragraph_text.append(" ")
            return
        self.handle_starttag(tag, attrs_list)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._ignored_stack:
            for index in range(len(self._ignored_stack) - 1, -1, -1):
                if self._ignored_stack[index] == tag:
                    del self._ignored_stack[index:]
                    break
            return

        if tag == "p" and self._paragraph_text is not None:
            text = _BLOCK_WHITESPACE.sub(" ", "".join(self._paragraph_text)).strip()
            if text:
                self.paragraphs.append(
                    _Paragraph(text, self._paragraph_speaker, self._paragraph_scope)
                )
            self._paragraph_text = None
            self._paragraph_speaker = None
        elif tag == "cite" and self._cite_text is not None:
            speaker = _BLOCK_WHITESPACE.sub(" ", "".join(self._cite_text)).strip()
            if speaker:
                self._speaker = speaker
                if self._paragraph_text is not None:
                    self._paragraph_speaker = speaker
                    self._paragraph_text.append(" ")
            self._cite_text = None
        elif tag == "time" and self._time_depth:
            self._time_depth -= 1
            if self._paragraph_text is not None:
                self._paragraph_text.append(" ")

        for index in range(len(self._frames) - 1, -1, -1):
            if self._frames[index].tag == tag:
                del self._frames[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._ignored_stack or self._time_depth:
            return
        if self._cite_text is not None:
            self._cite_text.append(data)
        elif self._paragraph_text is not None:
            self._paragraph_text.append(data)

    def finish(self) -> None:
        if self._paragraph_text is not None:
            raise SourceResolutionError("Declared RSS HTML transcript has an unclosed paragraph")
        if any(frame.marked_transcript for frame in self._frames):
            raise SourceResolutionError("Declared RSS HTML transcript container is unclosed")
        if any(frame.semantic_transcript for frame in self._frames):
            raise SourceResolutionError("Declared RSS HTML semantic container is unclosed")


def _origin_kind(value: str) -> Literal["text", "captions"]:
    return "captions" if value == "captions" else "text"


def _podcast_time(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceResolutionError(f"Podcast JSON transcript has an invalid {field}")
    try:
        number = float(value)
    except OverflowError:
        raise SourceResolutionError(f"Podcast JSON transcript has an invalid {field}") from None
    if not math.isfinite(number) or number < 0:
        raise SourceResolutionError(f"Podcast JSON transcript has an invalid {field}")
    return number


def parse_podcast_json_transcript(
    content: bytes,
    *,
    language: str | None,
    origin_uri: str,
    origin_kind: str,
) -> TranscriptDraft:
    """Convert Podcasting 2.0 transcript JSON version 1.0.0 to canonical segments."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceResolutionError("Podcast JSON transcript is not valid UTF-8") from None
    try:
        payload = json.loads(text)
    except (RecursionError, TypeError, ValueError):
        raise SourceResolutionError("Podcast JSON transcript is malformed") from None
    if not isinstance(payload, dict) or payload.get("version") != "1.0.0":
        raise SourceResolutionError("Only Podcast JSON transcript version 1.0.0 is supported")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments or len(raw_segments) > 100_000:
        raise SourceResolutionError("Podcast JSON transcript has no valid segments")

    segments: list[TranscriptSegment] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            raise SourceResolutionError("Podcast JSON transcript contains an invalid segment")
        body = raw_segment.get("body")
        if not isinstance(body, str) or not body.strip():
            raise SourceResolutionError("Podcast JSON transcript contains an empty segment")
        speaker = raw_segment.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise SourceResolutionError("Podcast JSON transcript contains an invalid speaker")
        start = _podcast_time(raw_segment.get("startTime"), field="startTime")
        end = _podcast_time(raw_segment.get("endTime"), field="endTime")
        if raw_segment.get("endTime") is not None and raw_segment.get("startTime") is None:
            raise SourceResolutionError("Podcast JSON endTime requires a startTime")
        if start is not None and end is not None and end < start:
            raise SourceResolutionError("Podcast JSON transcript contains an invalid time range")
        # A startTime without endTime cannot satisfy the canonical paired-time
        # invariant; preserve the segment as untimed rather than inventing an end.
        if start is None or end is None:
            start = end = None
        try:
            segments.append(
                TranscriptSegment(
                    start=start,
                    end=end,
                    text=body,
                    speaker=(
                        speaker.strip() if isinstance(speaker, str) and speaker.strip() else None
                    ),
                )
            )
        except ValidationError:
            raise SourceResolutionError(
                "Podcast JSON transcript contains an invalid segment"
            ) from None

    try:
        return TranscriptDraft(
            provider="podcast-json",
            model=None,
            language=language,
            origin=TranscriptOrigin(
                kind=_origin_kind(origin_kind), uri=origin_uri, format="podcast-json"
            ),
            segments=segments,
        )
    except ValidationError:
        raise SourceResolutionError("Podcast JSON transcript is inconsistent") from None


def parse_podcast_html_transcript(
    content: bytes,
    *,
    language: str | None,
    origin_uri: str,
    origin_kind: str,
) -> TranscriptDraft:
    """Read only paragraph text from a selected, explicitly declared HTML transcript."""

    try:
        html = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceResolutionError("Podcast HTML transcript is not valid UTF-8") from None
    parser = _PodcastHTMLParser()
    try:
        parser.feed(html)
        parser.close()
        parser.finish()
    except SourceResolutionError:
        raise
    except Exception:
        raise SourceResolutionError("Podcast HTML transcript could not be parsed") from None

    marked = [paragraph for paragraph in parser.paragraphs if paragraph.scope == "marked"]
    semantic = [paragraph for paragraph in parser.paragraphs if paragraph.scope == "semantic"]
    if parser.has_marked_container:
        paragraphs = marked
    elif parser.has_semantic_container:
        paragraphs = semantic
    else:
        paragraphs = parser.paragraphs
    if not paragraphs:
        raise SourceResolutionError("Selected Podcast HTML transcript contains no paragraphs")
    try:
        return TranscriptDraft(
            provider="podcast-html",
            model=None,
            language=language,
            origin=TranscriptOrigin(
                kind=_origin_kind(origin_kind), uri=origin_uri, format="podcast-html"
            ),
            segments=[
                TranscriptSegment(text=paragraph.text, speaker=paragraph.speaker)
                for paragraph in paragraphs
            ],
        )
    except ValidationError:
        raise SourceResolutionError("Podcast HTML transcript is inconsistent") from None


__all__ = ["parse_podcast_html_transcript", "parse_podcast_json_transcript"]
