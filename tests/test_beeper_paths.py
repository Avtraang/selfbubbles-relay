"""A Beeper chat id is one path segment (step R5 review, 2026-10-07).

``beeper.py`` builds its API paths from the chat guid the client sends:
``bp:401`` becomes ``/v1/chats/401/messages`` (list, send) and
``/v1/chats/401/read``, requested with the Beeper Desktop token. The id used to
go in as it came, and ``httpx`` resolves dot segments before it sends, so

* ``/send`` with ``chat_guid = "bp:123/archive#"`` POSTed to ``/v1/chats/123/archive``,
* ``"bp:../../v1/accounts#"`` reached ``/v1/accounts``, by POST and by GET.

``beeper.chat_path_id`` now refuses an id that is empty, only dots, or that
contains ``/``, ``\\``, ``?``, ``#``, ``%``, whitespace or a control character;
the three callers then make no request at all. A real id (a small number, or a
Matrix room id) goes into the path exactly as before.

Nothing here talks to Beeper Desktop: ``httpx.AsyncClient`` gets an
``httpx.MockTransport`` that records the method and the path as ``httpx`` would
put them on the wire. The token is a stub. This is ``beeper.py``, which both
relay modules share, so the tests run on either.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import beeper
from tests.conftest import RELAY_STUB_ENV
from tests.test_relay_compat import relay_state  # noqa: F401  (autouse: snapshot/restore the relay's state)

AUTH = {"X-Imsg-Token": RELAY_STUB_ENV["IMSG_TOKEN"]}
STUB_BEEPER_TOKEN = "stub-beeper-token-not-real"

#: Guids whose id is a real one: a local chat number, a Matrix room id.
REAL = {"bp:401": "401", "bp:7": "7", "bp:!synthetic:beeper.local": "!synthetic:beeper.local",
        "bp:!AbC-123_x:beeper.example": "!AbC-123_x:beeper.example", "bp:1.2": "1.2"}

#: Guids whose "id" is path navigation, a second segment, a query, a fragment or a line break.
HOSTILE = ("bp:", "bp:.", "bp:..", "bp:...", "bp:../../v1/accounts#", "bp:../../v1/accounts",
           "bp:123/archive#", "bp:123/archive", "bp:123?x=1", "bp:123#frag", "bp:%2e%2e", "bp:..%2F..%2Fv1",
           "bp:a\\b", "bp:a b", "bp:a\tb", "bp:401\n[beeper] forged line", "bp:401\x00", "bp:401\x7f")


def sync(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(monkeypatch):
    """Beeper Desktop's API as a recording mock; ``api.seen`` is ``[(method, raw path)]``."""
    class Api:
        seen: list = []
        status = 200
        body: object = {"items": []}
        headers: list = []

    Api.seen, Api.headers = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        Api.seen.append((request.method, request.url.raw_path.decode()))
        Api.headers.append(request.headers.get("authorization"))
        if isinstance(Api.body, (dict, list)):
            return httpx.Response(Api.status, json=Api.body)
        return httpx.Response(Api.status, text=str(Api.body))

    real_client = httpx.AsyncClient

    def client(**kw):
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(beeper.httpx, "AsyncClient", client)
    monkeypatch.setattr(beeper, "_enabled", True)
    monkeypatch.setattr(beeper, "BEEPER_TOKEN", STUB_BEEPER_TOKEN)
    monkeypatch.setattr(beeper, "BEEPER_URL", "http://beeper.invalid:23373")
    return Api


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("guid, cid", sorted(REAL.items()))
def test_a_real_chat_id_is_returned_as_it_is(guid, cid):
    assert beeper.chat_path_id(guid) == cid == beeper.local_id(guid)


@pytest.mark.parametrize("guid", HOSTILE)
def test_an_id_that_is_not_one_path_segment_is_refused(guid):
    assert beeper.chat_path_id(guid) is None


# ---------------------------------------------------------------------------
# the three callers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("guid, cid", sorted(REAL.items()))
def test_real_ids_reach_the_same_paths_as_before(api, guid, cid):
    assert sync(beeper.send(guid, "synthetic text")) is True
    assert sync(beeper.fetch_messages(guid, limit=50)) == []
    assert sync(beeper.mark_read(guid)) is True
    assert api.seen == [("POST", f"/v1/chats/{cid}/messages"),
                        ("GET", f"/v1/chats/{cid}/messages?limit=50"),
                        ("POST", f"/v1/chats/{cid}/read")]
    assert set(api.headers) == {f"Bearer {STUB_BEEPER_TOKEN}"}


@pytest.mark.parametrize("guid", HOSTILE)
def test_a_hostile_id_makes_no_request_at_all(api, capsys, guid):
    assert sync(beeper.send(guid, "synthetic text")) is False
    assert sync(beeper.send(guid, "synthetic text", reply_to="m1")) is False
    assert sync(beeper.fetch_messages(guid)) == []
    assert sync(beeper.fetch_messages(guid, limit=500, cursor="c1")) == []
    assert sync(beeper.mark_read(guid)) is False
    assert api.seen == []                                            # the token never left
    out = capsys.readouterr().out
    assert out == (beeper.REFUSED_CHAT_ID + "\n") * 4                # send x2, fetch x2; mark_read is silent
    assert "forged line" not in out and "accounts" not in out and "archive" not in out


def test_disabled_bridge_still_does_nothing(api, monkeypatch):
    monkeypatch.setattr(beeper, "_enabled", False)
    assert sync(beeper.send("bp:401", "x")) is False
    assert sync(beeper.fetch_messages("bp:401")) == []
    assert sync(beeper.mark_read("bp:401")) is False
    assert api.seen == []


# ---------------------------------------------------------------------------
# through the relay's routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(relay_module) -> TestClient:
    """ASGI test client; no ``with``, so startup hooks never run."""
    return TestClient(relay_module.module.app)


@pytest.mark.parametrize("guid", ["bp:../../v1/accounts#", "bp:123/archive#", "bp:..", "bp:123?x=1"])
def test_the_routes_that_take_a_chat_guid_cannot_reach_another_endpoint(api, client, guid):
    # /search?chat= reads the chat's messages (GET), /read marks it read (POST)
    resp = client.get("/search", params={"q": "synthetic", "chat": guid}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (200, {"results": []})
    resp = client.post("/read", json={"chat_guid": guid, "rowid": 0}, headers=AUTH)
    assert resp.status_code == 200
    assert api.seen == []


def test_the_search_route_still_reads_a_real_beeper_chat(api, client):
    api.body = {"items": [{"id": "m1", "text": "a synthetic needle", "timestamp": "2026-01-02T03:04:05Z",
                           "isSender": False, "senderName": "Stand In"}]}
    resp = client.get("/search", params={"q": "needle", "chat": "bp:401"}, headers=AUTH)
    assert resp.status_code == 200 and len(resp.json()["results"]) == 1
    assert api.seen == [("GET", "/v1/chats/401/messages?limit=500")]
