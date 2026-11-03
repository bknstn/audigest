"""Offline converters for explicitly declared Podcasting 2.0 transcripts."""

from __future__ import annotations

import pytest

from audigest.errors import SourceResolutionError
from audigest.sources.podcast_transcript import (
    parse_podcast_html_transcript,
    parse_podcast_json_transcript,
)


def parse_json(content: bytes):
    return parse_podcast_json_transcript(
        content,
        language="en",
        origin_uri="https://cdn.example/transcript.json",
        origin_kind="text",
    )


def test_podcast_json_preserves_paired_times_and_drops_start_only_times() -> None:
    draft = parse_json(
        b'{"version":"1.0.0","segments":['
        b'{"body":"Timed","startTime":2,"endTime":4,"speaker":"Host"},'
        b'{"body":"Untimed","startTime":7}]}'
    )

    assert [(segment.start, segment.end) for segment in draft.segments] == [
        (2.0, 4.0),
        (None, None),
    ]
    assert draft.segments[0].speaker == "Host"
    assert draft.origin is not None and draft.origin.format == "podcast-json"


@pytest.mark.parametrize(
    "content",
    [
        b'{"version":"1.0.0","segments":[{"body":"bad","endTime":4}]}',
        b'{"version":"1.0.0","segments":[{"body":"bad","startTime":' + b"9" * 400 + b"}]}",
        b'{"version":"1.0.0","segments":[{"body":"bad","startTime":5,"endTime":4}]}',
        b'{"version":"1.0.0","segments":[]}',
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_podcast_json_rejects_invalid_or_pathological_documents(content: bytes) -> None:
    with pytest.raises(SourceResolutionError):
        parse_json(content)


def test_podcast_html_extracts_paragraphs_and_citations_without_invented_timing() -> None:
    draft = parse_podcast_html_transcript(
        b"""<html><body><main id='transcript'>
        <p><cite>Host</cite>First<br>line<time>00:03</time>end.</p>
        <script><p>Injected script</p></script>
        <noscript><div><noscript><p>Nested bait</p></noscript></div></noscript>
        </main><footer><p>Footer text</p></footer></body></html>""",
        language=None,
        origin_uri="https://cdn.example/transcript.html",
        origin_kind="text",
    )

    assert [segment.text for segment in draft.segments] == ["First line end."]
    assert draft.segments[0].speaker == "Host"
    assert (draft.segments[0].start, draft.segments[0].end) == (None, None)
    assert draft.origin is not None and draft.origin.uri.endswith("transcript.html")


def test_podcast_html_rejects_unclosed_paragraph_and_excessive_nesting() -> None:
    common = {
        "language": None,
        "origin_uri": "https://cdn.example/transcript.html",
        "origin_kind": "text",
    }
    with pytest.raises(SourceResolutionError, match="unclosed paragraph"):
        parse_podcast_html_transcript(b"<div id='transcript'><p>open", **common)
    with pytest.raises(SourceResolutionError, match="nesting is too deep"):
        parse_podcast_html_transcript(
            b"<div id='transcript'>" + b"<div>" * 130 + b"<p>text</p>", **common
        )


def test_podcast_html_does_not_fall_back_from_empty_or_unclosed_marked_container() -> None:
    common = {
        "language": None,
        "origin_uri": "https://cdn.example/transcript.html",
        "origin_kind": "text",
    }
    with pytest.raises(SourceResolutionError, match="contains no paragraphs"):
        parse_podcast_html_transcript(
            b"<div id='transcript'></div><p>Promotional boilerplate.</p>", **common
        )
    with pytest.raises(SourceResolutionError, match="contains no paragraphs"):
        parse_podcast_html_transcript(b"<main></main><p>Promotional boilerplate.</p>", **common)
    with pytest.raises(SourceResolutionError, match="container is unclosed"):
        parse_podcast_html_transcript(b"<div id='transcript'><p>Selected text.</p>", **common)
