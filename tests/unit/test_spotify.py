"""Offline validation for public Spotify episode metadata."""

from __future__ import annotations

import json

import pytest

from audigest.errors import SourceResolutionError
from audigest.sources.remote import BoundedRemoteDocument
from audigest.sources.spotify import spotify_episode_metadata, spotify_episode_reference

EPISODE_ID = "A" * 22


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/episode/{EPISODE_ID}?si=private#fragment",
        f"http://open.spotify.com:80/episode/{EPISODE_ID}",
    ],
)
def test_episode_reference_canonicalizes_supported_public_urls(url: str) -> None:
    reference = spotify_episode_reference(url)

    assert reference.episode_id == EPISODE_ID
    assert reference.canonical_url == f"https://open.spotify.com/episode/{EPISODE_ID}"
    assert reference.resolved_source.source_info.type == "spotify"
    assert reference.resolved_source.source_info.uri == reference.canonical_url


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/show/{EPISODE_ID}",
        f"https://open.spotify.com/track/{EPISODE_ID}",
        f"https://open.spotify.com/episode/{EPISODE_ID[:-1]}",
        f"https://open.spotify.com/episode/{'!' * 22}",
        f"https://www.open.spotify.com/episode/{EPISODE_ID}",
        f"https://spotify.com/episode/{EPISODE_ID}",
        f"https://user:secret@open.spotify.com/episode/{EPISODE_ID}",
        f"https://open.spotify.com:444/episode/{EPISODE_ID}",
        f"https://open.spotify.com/episode/{EPISODE_ID}/player",
        f" https://open.spotify.com/episode/{EPISODE_ID}",
    ],
)
def test_episode_reference_rejects_non_episode_and_unsafe_urls(url: str) -> None:
    with pytest.raises(SourceResolutionError):
        spotify_episode_reference(url)


def test_oembed_reads_only_validated_title_and_uses_fixed_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []
    document = BoundedRemoteDocument(
        content=json.dumps(
            {
                "provider_name": "Spotify",
                "type": "rich",
                "title": "A useful title",
                "html": "<iframe src='https://open.spotify.com/embed/episode/private'></iframe>",
                "thumbnail_url": "https://i.scdn.co/image/private",
            }
        ).encode(),
        final_url="https://open.spotify.com/oembed",
    )

    def fetch(url: str, **kwargs: object) -> BoundedRemoteDocument:
        requests.append(url)
        assert kwargs["accept"] == "application/json"
        assert kwargs["limit"] == 2 * 1024 * 1024
        return document

    monkeypatch.setattr("audigest.sources.spotify.fetch_bounded_remote_document", fetch)
    reference = spotify_episode_reference(
        f"https://open.spotify.com/episode/{EPISODE_ID}?signed=secret"
    )

    metadata = spotify_episode_metadata(reference)

    assert metadata.title == "A useful title"
    assert requests == [
        "https://open.spotify.com/oembed?url="
        f"https%3A%2F%2Fopen.spotify.com%2Fepisode%2F{EPISODE_ID}"
    ]
    assert "private" not in repr(metadata)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"provider_name":"Other","type":"rich","title":"x"}',
        b'{"provider_name":"Spotify","type":"video","title":"x"}',
        b'{"provider_name":"Spotify","type":"rich","title":"  "}',
        b'{"provider_name":"Spotify","type":"rich","title":"\\ud800"}',
        b'{"provider_name":"Spotify","type":"rich","title":"x"} \xff',
    ],
)
def test_oembed_rejects_invalid_metadata_without_exposing_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    monkeypatch.setattr(
        "audigest.sources.spotify.fetch_bounded_remote_document",
        lambda *_args, **_kwargs: BoundedRemoteDocument(
            content=payload,
            final_url="https://open.spotify.com/oembed",
        ),
    )

    with pytest.raises(SourceResolutionError) as caught:
        spotify_episode_metadata(
            spotify_episode_reference(f"https://open.spotify.com/episode/{EPISODE_ID}")
        )

    assert "Spotify episode" in str(caught.value)
    assert "xff" not in str(caught.value)


def test_oembed_rejects_oversized_title(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "provider_name": "Spotify",
            "type": "rich",
            "title": "x" * 513,
        }
    ).encode()
    monkeypatch.setattr(
        "audigest.sources.spotify.fetch_bounded_remote_document",
        lambda *_args, **_kwargs: BoundedRemoteDocument(
            content=payload,
            final_url="https://open.spotify.com/oembed",
        ),
    )

    with pytest.raises(SourceResolutionError, match="512-character limit"):
        spotify_episode_metadata(
            spotify_episode_reference(f"https://open.spotify.com/episode/{EPISODE_ID}")
        )
