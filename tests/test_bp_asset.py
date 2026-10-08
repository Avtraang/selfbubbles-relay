"""``GET /bp_asset``: only the URL schemes Beeper Desktop's asset endpoint takes
(step R5, 2026-10-07).

Google Messages attachments reach the phone through ``/bp_asset?u=<asset URL>``:
the relay hands the URL to Beeper Desktop's local asset endpoint
(``beeper.asset_url``) and returns the bytes. That endpoint takes ``mxc://``,
``localmxc://`` and ``file://`` URLs (the three ``beeper.msg_to_dict``
documents; Beeper Desktop 4.3.160 checks for exactly these itself); the route
used to pass ANY string on. It now refuses everything else with
``400 unsupported asset url`` before Beeper is asked, and with the same answer
a ``file://`` URL whose path has a ``..`` segment or a NUL byte. What Beeper
Desktop serves for an accepted URL is still Beeper's decision, which no test
here can pin.

``beeper.asset_url`` is replaced by a recorder: nothing talks to Beeper.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import RELAY_STUB_ENV

AUTH = {"X-Imsg-Token": RELAY_STUB_ENV["IMSG_TOKEN"]}
PNG = b"\x89PNG\r\n\x1a\n synthetic image bytes"

ACCEPTED = (
    "mxc://beeper.example/synthetic-media-id",
    "localmxc://local.beeper.example/synthetic?encryptedFileInfoJSON=%7B%7D",
    "file:///Users/synthetic/Library/Application%20Support/BeeperTexts/media/synthetic.png",
    # dots that are part of a name are not navigation
    "file:///Users/synthetic/Library/Application%20Support/BeeperTexts/media/synthetic..final.png",
    "file:///Users/synthetic/Library/Application%20Support/BeeperTexts/media/.hidden/..synthetic",
    "mxc://beeper.example/a/../b",                               # not a file path: Beeper's to interpret
)

#: ``file://`` URLs that climb out of their starting directory, or carry a NUL.
NAVIGATING = (
    "file:///Users/synthetic/Library/Application%20Support/BeeperTexts/media/../../../../.ssh/id_synthetic",
    "file:///Users/synthetic/media/..",
    "file:///a/%2e%2e/%2e%2e/x", "file:///a/%2E%2E/x", "file:///a/.%2e/x", "file:///a/..%2Fx",
    "file:///a\\..\\x", "file://localhost/a/../etc/hosts",
    "file:///etc/hosts%00.png", "file:///etc/hosts\x00.png",
)

REFUSED = (
    "https://example.test/picture.png", "http://192.168.0.5:8123/api/states", "http://127.0.0.1:8700/threads",
    "ftp://example.test/a", "data:image/png;base64,AAAA", "javascript:alert(1)",
    "/etc/hosts", "~/Library/Messages/chat.db", "etc/hosts", "file:/etc/hosts", "file:etc/hosts",
    "FILE:///etc/hosts", "Mxc://beeper.example/x", " mxc://beeper.example/x", "mxc:/beeper.example/x",
    "localmxc:local", "xmxc://beeper.example/x", "//beeper.example/x",
)


@pytest.fixture
def asked(r5, monkeypatch) -> list:
    """Records what reaches ``beeper.asset_url`` and answers a PNG."""
    calls: list = []

    async def fake_asset_url(src: str):
        calls.append(src)
        return PNG, "application/octet-stream"

    monkeypatch.setattr(r5.beeper, "asset_url", fake_asset_url)
    return calls


@pytest.fixture
def client(r5) -> TestClient:
    """ASGI test client; no ``with``, so startup hooks never run."""
    return TestClient(r5.app)


def test_the_allow_list_is_the_three_schemes_beepers_asset_endpoint_takes(r5):
    assert r5.BP_ASSET_SCHEMES == ("mxc://", "localmxc://", "file://")


@pytest.mark.parametrize("url", ACCEPTED)
def test_beeper_asset_urls_are_proxied_as_before(asked, client, url):
    for param in ("u", "src"):                                   # `src` is the pre-2026-09-27 alias
        resp = client.get("/bp_asset", params={param: url}, headers=AUTH)
        assert resp.status_code == 200, param
        assert resp.content == PNG
        assert resp.headers["content-type"] == "image/png"       # sniffed from the bytes, as before
        assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert asked == [url, url]                                   # handed on exactly as given


@pytest.mark.parametrize("url", REFUSED)
def test_any_other_url_is_refused_before_beeper_is_asked(asked, client, url):
    for param in ("u", "src"):
        resp = client.get("/bp_asset", params={param: url}, headers=AUTH)
        assert (resp.status_code, resp.json()) == (400, {"detail": "unsupported asset url"}), param
    assert asked == []


@pytest.mark.parametrize("url", NAVIGATING)
def test_a_file_url_that_navigates_is_refused_before_beeper_is_asked(r5, asked, client, url):
    """Whether Beeper Desktop would serve such a path was not measured (that
    needs a Beeper token); the relay does not ask it."""
    assert r5._file_url_navigates(url) is True
    for param in ("u", "src"):
        resp = client.get("/bp_asset", params={param: url}, headers=AUTH)
        assert (resp.status_code, resp.json()) == (400, {"detail": "unsupported asset url"}), param
    assert asked == []


def test_file_url_navigation_is_judged_on_the_decoded_path_once(r5):
    for url in ACCEPTED:
        if url.startswith("file://"):
            assert r5._file_url_navigates(url) is False, url
    # a doubly encoded ".." decodes to the literal name "%2e%2e", which is a name
    assert r5._file_url_navigates("file:///a/%252e%252e/x") is False


def test_missing_url_unavailable_asset_and_missing_token_are_unchanged(r5, asked, client, monkeypatch):
    resp = client.get("/bp_asset", headers=AUTH)
    assert (resp.status_code, resp.json()) == (422, {"detail": "u required"})
    resp = client.get("/bp_asset", params={"u": ""}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (422, {"detail": "u required"})
    assert client.get("/bp_asset", params={"u": ACCEPTED[0]}).status_code == 401
    assert client.get("/bp_asset", params={"u": REFUSED[0]}).status_code == 401      # auth comes first
    assert asked == []

    async def nothing(src: str):
        return None                                              # Beeper off, or it answered >= 400

    monkeypatch.setattr(r5.beeper, "asset_url", nothing)
    resp = client.get("/bp_asset", params={"u": ACCEPTED[0]}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (404, {"detail": "asset unavailable"})


def test_the_urls_beeper_messages_carry_pass_the_allow_list(r5):
    """``beeper.msg_to_dict`` builds ``/bp_asset?u=...`` from a message's
    ``srcURL``; every shape it documents starts with an accepted scheme."""
    for src in ACCEPTED:
        msg = r5.beeper.msg_to_dict({"id": "m1", "attachments": [{"id": "a1", "srcURL": src}]}, "bp:1", False)
        url = msg["attachments"][0]["url"]
        assert url.startswith("/bp_asset?u=")
        from urllib.parse import parse_qs, urlsplit
        assert parse_qs(urlsplit(url).query)["u"] == [src]
        assert src.startswith(r5.BP_ASSET_SCHEMES)
