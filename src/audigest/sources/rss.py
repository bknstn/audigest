"""Strict, non-fetching parser for the RSS 2.0 elements used by ingestion."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from audigest.errors import SourceResolutionError

PODCAST_NAMESPACE = "https://podcastindex.org/namespace/1.0"
_PODCAST_TRANSCRIPT_TAG = f"{{{PODCAST_NAMESPACE}}}transcript"
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
_XML_ROOT_TAG = re.compile(r"<([A-Za-z_][\w:.-]*)(?=[\s/>])")
_RSS_DOCTYPE_ROOT = re.compile(r"<!DOCTYPE\s+([A-Za-z_][\w:.-]*)\b", re.IGNORECASE)
_FORBIDDEN_XML = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b|\bxml\s*:\s*base\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RSSTranscriptDeclaration:
    """Unvalidated transcript declaration retained for selected-item handling."""

    url: str | None
    mime_type: str | None
    language: str | None
    relation: str | None


@dataclass(frozen=True, slots=True)
class RSSEnclosureDeclaration:
    """Unvalidated enclosure attributes from one RSS item."""

    url: str | None
    mime_type: str | None
    length: str | None


@dataclass(frozen=True, slots=True)
class RSSItem:
    """Episode metadata and raw resource declarations for one channel item."""

    guid: str | None
    link: str | None
    title: str | None
    author: str | None
    pub_date: str | None
    transcripts: tuple[RSSTranscriptDeclaration, ...]
    enclosures: tuple[RSSEnclosureDeclaration, ...]


@dataclass(frozen=True, slots=True)
class RSSFeed:
    """Bounded RSS channel metadata and episode item list."""

    title: str
    language: str | None
    author: str | None
    items: tuple[RSSItem, ...]


def looks_like_rss_document(content: bytes) -> bool:
    """Return whether the bounded response appears to have an RSS root element."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    offset = 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset >= len(text):
            return False
        if text.startswith("<?", offset):
            terminator = text.find("?>", offset + 2)
            if terminator < 0:
                return False
            offset = terminator + 2
            continue
        if text.startswith("<!--", offset):
            terminator = text.find("-->", offset + 4)
            if terminator < 0:
                return False
            offset = terminator + 3
            continue
        if text.startswith(("<!DOCTYPE", "<!doctype"), offset):
            doctype = _RSS_DOCTYPE_ROOT.match(text, offset)
            return doctype is not None and doctype.group(1) == "rss"
        root = _XML_ROOT_TAG.match(text, offset)
        return root is not None and root.group(1) == "rss"
    return False


def _child_text(parent: ET.Element, name: str) -> str | None:
    child = parent.find(name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _required_channel_text(channel: ET.Element, name: str) -> str:
    value = _child_text(channel, name)
    if value is None:
        raise SourceResolutionError("RSS 2.0 channel is missing a required element")
    return value


def _parse_item(element: ET.Element) -> RSSItem:
    author = _child_text(element, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author")
    author = author or _child_text(element, "author")
    transcripts = tuple(
        RSSTranscriptDeclaration(
            url=child.attrib.get("url"),
            mime_type=child.attrib.get("type"),
            language=child.attrib.get("language"),
            relation=child.attrib.get("rel"),
        )
        for child in element.findall(_PODCAST_TRANSCRIPT_TAG)
    )
    enclosures = tuple(
        RSSEnclosureDeclaration(
            url=child.attrib.get("url"),
            mime_type=child.attrib.get("type"),
            length=child.attrib.get("length"),
        )
        for child in element.findall("enclosure")
    )
    return RSSItem(
        guid=_child_text(element, "guid"),
        link=_child_text(element, "link"),
        title=_child_text(element, "title"),
        author=author,
        pub_date=_child_text(element, "pubDate"),
        transcripts=transcripts,
        enclosures=enclosures,
    )


def parse_rss_feed(content: bytes) -> RSSFeed:
    """Validate and extract one RSS 2.0 channel without resolving any resource URL."""

    try:
        xml = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceResolutionError("RSS feed is not valid UTF-8 XML") from None
    if _FORBIDDEN_XML.search(xml):
        raise SourceResolutionError("RSS feed uses forbidden DTD, entity, or xml:base markup")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise SourceResolutionError("RSS feed is malformed XML") from None

    if root.tag != "rss" or root.attrib.get("version") != "2.0":
        raise SourceResolutionError("Only RSS 2.0 feeds are supported")
    if any("{" + _XML_NAMESPACE + "}base" in element.attrib for element in root.iter()):
        raise SourceResolutionError("RSS feed xml:base is not supported")
    channels = root.findall("channel")
    if len(channels) != 1:
        raise SourceResolutionError("RSS 2.0 feed must contain exactly one channel")
    channel = channels[0]

    # RSS 2.0 requires title, link, and description on the channel. Validate the
    # feed structure globally while leaving episode resource declarations raw
    # until an item is selected.
    title = _required_channel_text(channel, "title")
    _required_channel_text(channel, "link")
    _required_channel_text(channel, "description")
    item_elements = channel.findall("item")
    if not item_elements:
        raise SourceResolutionError("RSS channel contains no episode items")
    if len(item_elements) > 500:
        raise SourceResolutionError("RSS feed exceeds the 500 episode limit")

    channel_author = _child_text(
        channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author"
    ) or _child_text(channel, "managingEditor")
    return RSSFeed(
        title=title,
        language=_child_text(channel, "language"),
        author=channel_author,
        items=tuple(_parse_item(item) for item in item_elements),
    )


__all__ = [
    "PODCAST_NAMESPACE",
    "RSSFeed",
    "RSSItem",
    "RSSEnclosureDeclaration",
    "RSSTranscriptDeclaration",
    "looks_like_rss_document",
    "parse_rss_feed",
]
