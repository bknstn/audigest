"""Offline validation for the bounded RSS 2.0 parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from audigest.errors import SourceResolutionError
from audigest.models import SourceInfo
from audigest.sources.base import ResolvedSource
from audigest.sources.remote import RemoteURLReference
from audigest.sources.rss import (
    RSSFeed,
    RSSItem,
    looks_like_rss_document,
    parse_rss_feed,
)
from audigest.sources.rss_acquisition import SelectedRSSEpisode, _resource_fingerprint
from audigest.storage import cache_id_for


def rss_document(items: str = "<item><guid>episode-1</guid></item>") -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0'><channel><title>Feed</title>"
        "<link>https://example.test/</link><description>Test feed</description>"
        f"{items}</channel></rss>"
    ).encode()


def test_rss_detection_checks_the_document_root_and_skips_xml_comments() -> None:
    assert looks_like_rss_document(b"<!-- generated --><rss version='2.0'></rss>")
    assert looks_like_rss_document(rss_document())
    assert not looks_like_rss_document(
        b"<html><script>const example = '<rss version=2.0>';</script></html>"
    )
    assert not looks_like_rss_document(b"<html><p>Example: &lt;rss&gt;</p></html>")


def test_rss_parser_keeps_unselected_resource_declarations_unvalidated() -> None:
    payload = rss_document(
        "<item><guid>one</guid><podcast:transcript xmlns:podcast='"
        "https://podcastindex.org/namespace/1.0'/></item>"
        "<item><guid>two</guid></item>"
    )

    feed = parse_rss_feed(payload)

    assert [item.guid for item in feed.items] == ["one", "two"]
    assert feed.items[0].transcripts[0].url is None


@pytest.mark.parametrize(
    "payload",
    [
        b"<rss version='2.0'><channel><title>x</title><link>x</link><description>x</description>"
        b"<!DOCTYPE rss [<!ENTITY test 'expanded'>]><item><guid>&test;</guid></item>"
        b"</channel></rss>",
        b"<rss version='2.0'><channel xml:base='https://evil.test/'><title>x</title>"
        b"<link>x</link><description>x</description><item /></channel></rss>",
        b"<rss version='2.0'><channel><title>x</title><link>x</link><description>x</description>"
        b"<item>",
        b"\xff",
        b"<feed xmlns='http://www.w3.org/2005/Atom' />",
    ],
)
def test_rss_parser_rejects_forbidden_or_unsupported_documents(payload: bytes) -> None:
    with pytest.raises(SourceResolutionError):
        parse_rss_feed(payload)


def test_extensionless_rss_sniff_catches_doctype_but_not_html_doctype() -> None:
    assert looks_like_rss_document(
        b"<!DOCTYPE rss PUBLIC '-//Example//DTD RSS 2.0//EN' 'https://example.test/rss.dtd'>"
        b"<rss version='2.0'>"
    )
    assert not looks_like_rss_document(b"<!DOCTYPE html><html></html>")


def test_rss_sniff_scans_repeated_comments_linearly_before_html_or_rss() -> None:
    comments = b"<!--x-->" * 24
    assert not looks_like_rss_document(comments + b"<html><body>example</body></html>")
    assert looks_like_rss_document(comments + b"<rss version='2.0'></rss>")


def test_rss_parser_enforces_global_episode_count_limit() -> None:
    payload = rss_document("<item />" * 501)

    with pytest.raises(SourceResolutionError, match="500 episode limit"):
        parse_rss_feed(payload)


def test_rss_fingerprint_and_cache_id_remain_compatible_without_spotify_context() -> None:
    resolved = ResolvedSource(
        source_info=SourceInfo(
            type="rss",
            uri="https://publisher.example/feed.rss#episode-frozen",
            title="Frozen episode",
        ),
        media_path=Path("rss-episode"),
        content_sha256="f" * 64,
    )
    episode = SelectedRSSEpisode(
        feed=RSSFeed(title="Feed", language=None, author=None, items=()),
        item=RSSItem(
            guid="frozen-guid",
            link=None,
            title="Frozen episode",
            author=None,
            pub_date=None,
            transcripts=(),
            enclosures=(),
        ),
        feed_reference=RemoteURLReference(
            request_url="https://publisher.example/feed.rss",
            public_url="https://publisher.example/feed.rss",
            suffix=".rss",
            locator_fingerprint="a" * 64,
        ),
        final_feed_url="https://publisher.example/feed.rss",
        resolved_source=resolved,
        episode_identity_hash="b" * 64,
        metadata_hash="c" * 64,
        source_identity="f" * 64,
    )

    fingerprint = _resource_fingerprint(
        episode,
        "d" * 64,
        "e" * 64,
        requested_language="en",
        selected_language="fr",
        kind="rss-transcript",
    )

    assert fingerprint == "c5d702767331de2968b6ae71dbda2eec9b9826ca160ce0be27ec348eb984ea56"
    assert (
        cache_id_for(
            resolved,
            provider="rss-text",
            model=None,
            language="en",
            source_fingerprint=fingerprint,
        )
        == "7481549525e5d5f43d4f8873fcaae415ee804deb750005275e910b99f138ca7c"
    )
