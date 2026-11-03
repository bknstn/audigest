"""Narrow parsing helpers for explicitly marked publisher episode-page content."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from audigest.errors import SourceResolutionError

_IGNORED_TAGS = frozenset({"script", "style", "noscript"})
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
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_TRANSCRIPT_LABEL = re.compile(r"\btranscript\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PublisherTranscriptLink:
    """A supported or explicitly labeled transcript URL discovered in HTML."""

    href: str
    language: str | None


@dataclass(frozen=True, slots=True)
class PublisherCaptionLink:
    """An explicitly labeled direct SRT or VTT caption URL."""

    href: str
    language: str | None


@dataclass(frozen=True, slots=True)
class PublisherInlineTranscript:
    """Plain text from a marked inline transcript container."""

    text: str
    language: str | None


@dataclass(frozen=True, slots=True)
class PublisherCaptionTrack:
    """A standard HTML captions/subtitles track."""

    href: str
    kind: str
    language: str | None


@dataclass(frozen=True, slots=True)
class PublisherMediaSource:
    """A direct media URL from an audio/video ``src`` or nested ``source``."""

    href: str


@dataclass(frozen=True, slots=True)
class PublisherPage:
    """Only the narrow, explicitly marked content extracted from an episode page."""

    language: str | None
    transcript_links: tuple[PublisherTranscriptLink, ...]
    caption_links: tuple[PublisherCaptionLink, ...]
    unsupported_transcript_links: tuple[PublisherTranscriptLink, ...]
    inline_transcripts: tuple[PublisherInlineTranscript, ...]
    caption_tracks: tuple[PublisherCaptionTrack, ...]
    unsupported_caption_tracks: tuple[PublisherCaptionTrack, ...]
    media_sources: tuple[PublisherMediaSource, ...]


@dataclass(slots=True)
class _Anchor:
    href: str
    language: str | None
    label_parts: list[str]


class _PublisherPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page_language: str | None = None
        self.transcript_links: list[PublisherTranscriptLink] = []
        self.caption_links: list[PublisherCaptionLink] = []
        self.unsupported_transcript_links: list[PublisherTranscriptLink] = []
        self.inline_transcripts: list[PublisherInlineTranscript] = []
        self.caption_tracks: list[PublisherCaptionTrack] = []
        self.unsupported_caption_tracks: list[PublisherCaptionTrack] = []
        self.media_sources: list[PublisherMediaSource] = []
        self._ignored_stack: list[str] = []
        self._anchors: list[_Anchor] = []
        self._media_stack: list[str] = []
        self._inline_stack: list[str] = []
        self._inline_text: list[str] | None = None
        self._inline_language: str | None = None

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {name.lower(): value or "" for name, value in attrs_list}
        if self._ignored_stack:
            if tag in _IGNORED_TAGS:
                self._ignored_stack.append(tag)
            return
        if tag in _IGNORED_TAGS:
            self._ignored_stack.append(tag)
            return

        if tag == "html" and self.page_language is None:
            self.page_language = _clean_language(attrs.get("lang"))
        if tag == "link":
            self._record_link(attrs)
        elif tag == "a" and attrs.get("href"):
            self._anchors.append(
                _Anchor(
                    href=attrs["href"],
                    language=_clean_language(attrs.get("hreflang") or attrs.get("lang")),
                    label_parts=[
                        attrs.get("rel", ""),
                        attrs.get("title", ""),
                        attrs.get("aria-label", ""),
                    ],
                )
            )

        if tag in {"audio", "video"}:
            self._record_media(attrs.get("src"))
            self._media_stack.append(tag)
        elif tag == "source" and self._media_stack:
            self._record_media(attrs.get("src"))

        if tag == "track":
            kind = attrs.get("kind", "").strip().casefold()
            href = attrs.get("src", "")
            if href and kind in {"captions", "subtitles"}:
                track = PublisherCaptionTrack(
                    href=href,
                    kind=kind,
                    language=_clean_language(attrs.get("srclang") or attrs.get("lang")),
                )
                if _looks_like_supported_caption(href):
                    self.caption_tracks.append(track)
                else:
                    self.unsupported_caption_tracks.append(track)

        marker = _is_transcript_container(attrs)
        if self._inline_text is None and marker and tag not in _VOID_TAGS:
            self._inline_text = []
            self._inline_language = _clean_language(attrs.get("lang"))
            self._inline_stack = [tag]
        elif self._inline_text is not None and tag not in _VOID_TAGS:
            self._inline_stack.append(tag)
        if self._inline_text is not None and tag in _BLOCK_TAGS | {"br"}:
            self._add_inline_newline()

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs_list)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._ignored_stack:
            for index in range(len(self._ignored_stack) - 1, -1, -1):
                if self._ignored_stack[index] == tag:
                    del self._ignored_stack[index:]
                    break
            return

        if tag == "a" and self._anchors:
            anchor = self._anchors.pop()
            label = " ".join(part.strip() for part in anchor.label_parts if part.strip())
            if _TRANSCRIPT_LABEL.search(label):
                self._record_transcript_link(anchor.href, anchor.language)

        if tag in {"audio", "video"}:
            for index in range(len(self._media_stack) - 1, -1, -1):
                if self._media_stack[index] == tag:
                    del self._media_stack[index:]
                    break

        if self._inline_text is not None:
            if tag in _BLOCK_TAGS:
                self._add_inline_newline()
            for index in range(len(self._inline_stack) - 1, -1, -1):
                if self._inline_stack[index] == tag:
                    del self._inline_stack[index:]
                    if not self._inline_stack:
                        self._finish_inline()
                    break

    def handle_data(self, data: str) -> None:
        if self._ignored_stack:
            return
        for anchor in self._anchors:
            anchor.label_parts.append(data)
        if self._inline_text is not None:
            self._inline_text.append(data)

    def finish(self) -> None:
        while self._anchors:
            self._finish_anchor()
        if self._inline_text is not None:
            raise SourceResolutionError(
                "Publisher page contains an unclosed marked transcript container"
            )

    def _record_link(self, attrs: dict[str, str]) -> None:
        href = attrs.get("href", "")
        if not href:
            return
        rel = attrs.get("rel", "")
        label = " ".join(
            value for value in (rel, attrs.get("title", ""), attrs.get("aria-label", "")) if value
        )
        if _TRANSCRIPT_LABEL.search(label):
            self._record_transcript_link(
                href,
                _clean_language(attrs.get("hreflang") or attrs.get("lang")),
            )

    def _finish_anchor(self) -> None:
        anchor = self._anchors.pop()
        label = " ".join(part.strip() for part in anchor.label_parts if part.strip())
        if _TRANSCRIPT_LABEL.search(label):
            self._record_transcript_link(anchor.href, anchor.language)

    def _record_transcript_link(self, href: str, language: str | None) -> None:
        if _looks_like_supported_caption(href):
            self.caption_links.append(PublisherCaptionLink(href=href, language=language))
            return
        link = PublisherTranscriptLink(href=href, language=language)
        if _looks_like_supported_transcript(href):
            self.transcript_links.append(link)
        else:
            self.unsupported_transcript_links.append(link)

    def _record_media(self, href: str | None) -> None:
        if href:
            self.media_sources.append(PublisherMediaSource(href=href))

    def _add_inline_newline(self) -> None:
        if self._inline_text and self._inline_text[-1] != "\n":
            self._inline_text.append("\n")

    def _finish_inline(self) -> None:
        if self._inline_text is None:
            return
        raw_text = "".join(self._inline_text).replace("\r\n", "\n").replace("\r", "\n")
        lines = [re.sub(r"[\t\f\v ]+", " ", line).strip() for line in raw_text.split("\n")]
        text = "\n".join(line for line in lines if line)
        self.inline_transcripts.append(
            PublisherInlineTranscript(text=text, language=self._inline_language)
        )
        self._inline_text = None
        self._inline_language = None
        self._inline_stack = []


def _clean_language(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or any(character.isspace() for character in normalized):
        return None
    return normalized


def _is_transcript_container(attrs: dict[str, str]) -> bool:
    if attrs.get("id", "").strip().casefold() == "transcript":
        return True
    return "transcript" in {token.casefold() for token in attrs.get("class", "").split()}


def _path_suffix(value: str) -> str:
    from urllib.parse import unquote, urlsplit

    try:
        path = unquote(urlsplit(value).path)
        return path.rsplit("/", maxsplit=1)[-1].lower().rsplit(".", maxsplit=1)[-1]
    except ValueError:
        return ""


def _looks_like_supported_transcript(value: str) -> bool:
    return _path_suffix(value) in {"json", "md", "srt", "txt", "vtt"}


def _looks_like_supported_caption(value: str) -> bool:
    return _path_suffix(value) in {"srt", "vtt"}


def parse_publisher_html(content: bytes) -> PublisherPage:
    """Decode strict UTF-8 HTML and collect only declared transcript/media elements."""

    try:
        html = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceResolutionError("Publisher episode page is not valid UTF-8 HTML") from None
    parser = _PublisherPageParser()
    try:
        parser.feed(html)
        parser.close()
        parser.finish()
    except SourceResolutionError:
        raise
    except Exception:
        raise SourceResolutionError("Publisher episode page could not be parsed safely") from None
    return PublisherPage(
        language=parser.page_language,
        transcript_links=tuple(parser.transcript_links),
        caption_links=tuple(parser.caption_links),
        unsupported_transcript_links=tuple(parser.unsupported_transcript_links),
        inline_transcripts=tuple(parser.inline_transcripts),
        caption_tracks=tuple(parser.caption_tracks),
        unsupported_caption_tracks=tuple(parser.unsupported_caption_tracks),
        media_sources=tuple(parser.media_sources),
    )


__all__ = [
    "PublisherCaptionLink",
    "PublisherCaptionTrack",
    "PublisherInlineTranscript",
    "PublisherMediaSource",
    "PublisherPage",
    "PublisherTranscriptLink",
    "parse_publisher_html",
]
