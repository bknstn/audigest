"""Offline tests for the deliberately narrow publisher HTML parser."""

import pytest

from audigest.errors import SourceResolutionError
from audigest.sources.publisher import parse_publisher_html


def test_parser_extracts_nested_marked_text_and_handles_void_elements() -> None:
    page = parse_publisher_html(
        b"""<!doctype html>
        <html lang="en-US"><body>
          <div class="episode transcript featured">
            <p>Hello <strong>from markup</strong><br>next line.</p>
            <script><div id="transcript">script bait</div></script>
            <style>.caption { content: "style bait" }</style>
            <noscript><div id="transcript">noscript bait</div></noscript>
          </div>
          <a href="/captions.vtt" title="Transcript">captions</a>
          <video><source src="/media/episode.mp4"><track kind="captions"
            srclang="en" src="/captions/en.vtt"></video>
        </body></html>"""
    )

    assert page.language == "en-US"
    assert [item.text for item in page.inline_transcripts] == ["Hello from markup\nnext line."]
    assert page.media_sources[0].href == "/media/episode.mp4"
    assert page.caption_tracks[0].href == "/captions/en.vtt"
    assert page.caption_links[0].href == "/captions.vtt"
    assert page.transcript_links == ()
    assert "script bait" not in " ".join(item.text for item in page.inline_transcripts)
    assert "style bait" not in " ".join(item.text for item in page.inline_transcripts)
    assert "noscript bait" not in " ".join(item.text for item in page.inline_transcripts)


def test_parser_finds_explicit_transcript_links_and_decodes_encoded_suffix() -> None:
    page = parse_publisher_html(
        b"""<html><head><link rel="alternate transcript" href="/notes%2Etxt"></head>
        <body><a href="/captions.vtt" title="Transcript"><span>download</span> text</a>
        <audio src="/episode.mp3"><source src="/episode.mp3"></audio></body></html>"""
    )

    assert [(link.href, link.language) for link in page.transcript_links] == [
        ("/notes%2Etxt", None)
    ]
    assert [(link.href, link.language) for link in page.caption_links] == [("/captions.vtt", None)]
    assert [item.href for item in page.media_sources] == ["/episode.mp3", "/episode.mp3"]


def test_parser_flushes_unclosed_explicit_transcript_anchor_at_end_of_document() -> None:
    page = parse_publisher_html(
        b'<html><body><a href="/transcript.txt" title="Transcript">Transcript'
    )

    assert len(page.transcript_links) == 1
    assert page.transcript_links[0].href == "/transcript.txt"


def test_unclosed_marked_transcript_container_is_rejected() -> None:
    with pytest.raises(SourceResolutionError, match="unclosed marked transcript") as error:
        parse_publisher_html(b'<html><body><div id="transcript">unfinished page tail')

    assert error.value.exit_code == 3


def test_nested_noscript_does_not_reenable_page_discovery_early() -> None:
    page = parse_publisher_html(
        b"""<html><body><noscript><noscript>nested</noscript>
        <a href="/bait.txt" rel="transcript">Transcript</a>
        <div id="transcript">hidden bait</div></noscript>
        <p>ordinary unmarked text</p></body></html>"""
    )

    assert page.transcript_links == ()
    assert page.inline_transcripts == ()


def test_invalid_utf8_html_fails_without_parser_fallback() -> None:
    with pytest.raises(SourceResolutionError, match="not valid UTF-8") as error:
        parse_publisher_html(b"<html><div id='transcript'>\xff</div></html>")
    assert error.value.exit_code == 3
