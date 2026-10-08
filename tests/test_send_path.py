"""Characterization tests for the relay's send path (plan 2026-10-06, step R1).

These pin what ``/send``, ``/send_attachment``, ``/react`` and ``/create_chat``
do TODAY, so that R2 (the send-engine chain) can be checked against them:

* the BlueBubbles URL path per operation, the base being ``BB_URL``;
* the PRESENCE of the ``password`` query param -- never its value: the
  recording fake keeps only the parameter *names*;
* the payload keys (JSON for text / react / create, multipart ``data`` +
  ``files`` for attachments), the ``method`` / ``service`` constants and the
  ``tempGuid`` prefixes;
* the ``via`` strings: ``"bb"``, ``"applescript"``, ``"gmessages"``;
* the AppleScript fallback for text and attachments on a BlueBubbles answer
  ``>= 400`` and on a transport exception (and the 502 when it fails too);
  ``_guid_variants`` (``any;`` retried as ``iMessage;``);
* the attachment success shape pinned **as-is**: ``{"ok": true}`` with NO
  ``via`` (``ChatVM.noteSendPath`` keys on ``via``; plan objection #13);
* ``react`` and ``create_chat`` have no fallback: a BlueBubbles error is
  passed through with its status;
* the guard-order bug (plan section 4, KB section 11): a missing
  ``BB_PASSWORD`` used to raise 500 BEFORE the AppleScript fallback could run.
  R1 pinned that as an ``xfail(strict=True)``; R2 (the engine chain) flipped
  it: with no password the chain is ``[applescript]``, BlueBubbles is never
  called, and the text / file goes out.  The old 500 pin is replaced by that.

Nothing here talks to the network: ``httpx.AsyncClient`` is replaced by a
recording fake (scripted answers or exceptions), ``_applescript`` (the
``osascript`` runner) by a recorder, ``OUTBOX`` by a directory under
``tmp_path`` (never ``~/Library/Messages``), and ``beeper.send`` by an async
stub.  The module under test is the one the conftest imports
(``RELAY_MODULE``: ``relay`` by default, ``relay_new`` before a cutover) under
the placeholder environment; ``/create_chat`` runs against the synthetic
compat database.  No real token, password, number or message appears here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.compat_fixture import C1S_GUID, FIND_CASES
from tests.conftest import RELAY_STUB_ENV
from tests.test_relay_compat import (  # noqa: F401  (relay_state: autouse snapshot/restore)
    compat_db,
    relay_state,
)

STUB_TOKEN = RELAY_STUB_ENV["IMSG_TOKEN"]          # a placeholder, never the real token
AUTH = {"X-Imsg-Token": STUB_TOKEN}

CHAT = "iMessage;-;+15550001234"                    # synthetic
ANY_CHAT = "any;-;+15550001234"                     # the "any;" spelling the app may send
TEXT = "synthetic test message"
MSG_GUID = "p:0/11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# recording fakes
# ---------------------------------------------------------------------------

@dataclass
class Call:
    """One recorded BlueBubbles call.  ``param_keys`` is the sorted list of query
    parameter NAMES: the values (the password) are dropped at record time so
    no assertion message can ever contain them."""

    method: str
    url: str
    param_keys: list[str]
    json: Any = None
    data: Any = None
    files: Any = None
    timeout: Any = None

    @property
    def path(self) -> str:
        return urlsplit(self.url).path


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, text: str | None = None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else ("" if body is None else str(body))

    def json(self) -> Any:
        return self._body


@dataclass
class BBRecorder:
    """Scripted stand-in for ``httpx.AsyncClient``.

    ``answers`` is consumed in order: a ``FakeResponse`` is returned, an
    ``Exception`` instance is raised (a transport failure).  Every call is
    appended to ``calls``.
    """

    answers: list[Any] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def client_factory(self):
        recorder = self

        class _Client:
            def __init__(self, *, timeout: Any = None, **_: Any):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def _call(self, method: str, url: str, *, params: Any = None,
                            json: Any = None, data: Any = None, files: Any = None) -> FakeResponse:
                keys = sorted(dict(params or {}).keys())
                recorder.calls.append(Call(method, url, keys, json=json, data=data,
                                           files=files, timeout=self.timeout))
                if not recorder.answers:
                    raise AssertionError("unexpected BlueBubbles call: no scripted answer")
                answer = recorder.answers.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                return answer

            async def post(self, url: str, **kw: Any) -> FakeResponse:
                return await self._call("POST", url, **kw)

            async def get(self, url: str, **kw: Any) -> FakeResponse:
                return await self._call("GET", url, **kw)

        return _Client


@dataclass
class ScriptRecorder:
    """Stand-in for ``relay._applescript`` (the ``osascript`` runner): records
    ``(script, args)`` and answers from ``results`` in order (``True`` = sent)."""

    results: list[bool] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def __call__(self, script: str, *args: str) -> bool:
        self.calls.append((script, tuple(args)))
        if not self.results:
            raise AssertionError("unexpected osascript call: no scripted result")
        return self.results.pop(0)


@pytest.fixture
def r(relay_module):
    return relay_module.module


@pytest.fixture
def bb(r, monkeypatch) -> BBRecorder:
    rec = BBRecorder()
    monkeypatch.setattr(r.httpx, "AsyncClient", rec.client_factory())
    return rec


@pytest.fixture
def osa(r, monkeypatch, tmp_path) -> ScriptRecorder:
    rec = ScriptRecorder()
    monkeypatch.setattr(r, "_applescript", rec)
    # Never stage files under the real ~/Library/Messages/RelayOutbox.
    monkeypatch.setattr(r, "OUTBOX", tmp_path / "outbox")
    return rec


@pytest.fixture
def client(r) -> TestClient:
    """ASGI test client; no ``with``, so startup hooks (poll loop, FCM) never run."""
    return TestClient(r.app)


def _bb_ok(body: Any = None) -> FakeResponse:
    return FakeResponse(200, body if body is not None else {"status": 200, "data": {}})


# ---------------------------------------------------------------------------
# /send: text via BlueBubbles
# ---------------------------------------------------------------------------

def test_send_text_posts_message_text_with_password_param_and_payload_keys(r, bb, osa, client):
    bb.answers = [_bb_ok({"status": 200, "data": {"guid": "synthetic-bb-guid"}})]
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "bb",
                           "bb": {"status": 200, "data": {"guid": "synthetic-bb-guid"}}}

    assert len(bb.calls) == 1
    call = bb.calls[0]
    assert call.method == "POST"
    assert call.url.startswith(r.BB_URL + "/")
    assert call.path == "/api/v1/message/text"
    assert call.param_keys == ["password"]              # presence only, never the value
    assert call.timeout == 15
    assert set(call.json) == {"chatGuid", "message", "method", "tempGuid"}
    assert call.json["chatGuid"] == CHAT
    assert call.json["message"] == TEXT
    assert call.json["method"] == "private-api"
    assert call.json["tempGuid"].startswith("relay-")
    assert call.data is None and call.files is None
    assert osa.calls == []                               # no fallback on success


def test_send_reply_adds_selected_message_guid_and_part_index(bb, osa, client):
    bb.answers = [_bb_ok()]
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT, "reply_to_guid": MSG_GUID},
                       headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["via"] == "bb"
    call = bb.calls[0]
    assert call.path == "/api/v1/message/text"
    assert set(call.json) == {"chatGuid", "message", "method", "tempGuid",
                              "selectedMessageGuid", "partIndex"}
    assert call.json["selectedMessageGuid"] == MSG_GUID
    assert call.json["partIndex"] == 0


def test_send_falls_back_to_applescript_when_bb_answers_400_or_more(r, bb, osa, client):
    bb.answers = [FakeResponse(500, text="synthetic BB failure")]
    osa.results = [True]
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "applescript"}
    assert bb.calls[0].path == "/api/v1/message/text"
    assert osa.calls == [(r._AS_TEXT, (CHAT, TEXT))]


def test_send_falls_back_to_applescript_when_bb_raises(r, bb, osa, client):
    bb.answers = [httpx.ConnectError("synthetic connection refused")]
    osa.results = [True]
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "applescript"}
    assert len(bb.calls) == 1
    assert osa.calls == [(r._AS_TEXT, (CHAT, TEXT))]


def test_send_returns_502_when_bb_and_applescript_both_fail(r, bb, osa, client):
    bb.answers = [FakeResponse(503, text="synthetic BB down")]
    osa.results = [False]
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT}, headers=AUTH)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "BlueBubbles failed" in detail
    assert "AppleScript fallback failed" in detail
    assert osa.calls == [(r._AS_TEXT, (CHAT, TEXT))]


def test_send_fallback_retries_any_guid_as_imessage_guid(r, bb, osa, client):
    bb.answers = [FakeResponse(500, text="synthetic BB failure")]
    osa.results = [False, True]
    resp = client.post("/send", json={"chat_guid": ANY_CHAT, "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "applescript"}
    assert osa.calls == [(r._AS_TEXT, (ANY_CHAT, TEXT)),
                         (r._AS_TEXT, ("iMessage;" + ANY_CHAT[len("any;"):], TEXT))]


def test_send_fallback_stops_at_first_guid_variant_that_works(r, bb, osa, client):
    bb.answers = [FakeResponse(500, text="synthetic BB failure")]
    osa.results = [True]
    resp = client.post("/send", json={"chat_guid": ANY_CHAT, "text": TEXT}, headers=AUTH)
    assert resp.json() == {"ok": True, "via": "applescript"}
    assert osa.calls == [(r._AS_TEXT, (ANY_CHAT, TEXT))]


def test_guid_variants_pinned(r):
    assert list(r._guid_variants(CHAT)) == [CHAT]
    assert list(r._guid_variants(ANY_CHAT)) == [ANY_CHAT, "iMessage;" + ANY_CHAT[len("any;"):]]
    assert list(r._guid_variants("SMS;-;+15550001234")) == ["SMS;-;+15550001234"]


# ---------------------------------------------------------------------------
# /send: Beeper (Google Messages) guids never touch BlueBubbles or AppleScript
# ---------------------------------------------------------------------------

@pytest.fixture
def beeper_on(r, monkeypatch):
    """Beeper joins the send chain only when BEEPER_TOKEN is set (R2); the stub
    environment leaves it empty, so these tests switch it on with a placeholder."""
    monkeypatch.setattr(r.beeper, "BEEPER_TOKEN", "stub-beeper-token-not-real")


def test_send_beeper_guid_goes_to_beeper_with_via_gmessages(r, bb, osa, client, monkeypatch,
                                                            beeper_on):
    seen: list[tuple[str, str, str | None]] = []

    async def fake_send(chat_guid: str, text: str, reply_to: str | None = None) -> bool:
        seen.append((chat_guid, text, reply_to))
        return True

    monkeypatch.setattr(r.beeper, "send", fake_send)
    guid = r.beeper.PREFIX + "42"
    assert r.beeper.is_beeper_guid(guid)
    resp = client.post("/send", json={"chat_guid": guid, "text": TEXT, "reply_to_guid": MSG_GUID},
                       headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "gmessages"}
    assert seen == [(guid, TEXT, MSG_GUID)]
    assert bb.calls == [] and osa.calls == []


def test_send_beeper_failure_is_502_without_applescript(r, bb, osa, client, monkeypatch,
                                                        beeper_on):
    async def fake_send(chat_guid: str, text: str, reply_to: str | None = None) -> bool:
        return False

    monkeypatch.setattr(r.beeper, "send", fake_send)
    resp = client.post("/send", json={"chat_guid": r.beeper.PREFIX + "42", "text": TEXT},
                       headers=AUTH)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Google Messages send failed"
    assert bb.calls == [] and osa.calls == []


# ---------------------------------------------------------------------------
# /send_attachment
# ---------------------------------------------------------------------------

def _attachment(name: str = "synthetic.png", content: bytes = b"\x89PNG synthetic bytes",
                ctype: str = "image/png") -> dict[str, Any]:
    return {"file": (name, content, ctype)}


def test_send_attachment_posts_multipart_and_success_shape_has_no_via(r, bb, osa, client):
    bb.answers = [_bb_ok()]
    resp = client.post("/send_attachment", data={"chat_guid": CHAT}, files=_attachment(),
                       headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}                    # pinned AS-IS: no "via" key

    assert len(bb.calls) == 1
    call = bb.calls[0]
    assert call.method == "POST"
    assert call.url.startswith(r.BB_URL + "/")
    assert call.path == "/api/v1/message/attachment"
    assert call.param_keys == ["password"]
    assert call.json is None
    assert set(call.data) == {"chatGuid", "tempGuid", "name", "method"}
    assert call.data["chatGuid"] == CHAT
    assert call.data["name"] == "synthetic.png"
    assert call.data["method"] == "private-api"
    assert call.data["tempGuid"].startswith("relay-att-")
    assert set(call.files) == {"attachment"}
    assert call.files["attachment"] == ("synthetic.png", b"\x89PNG synthetic bytes", "image/png")
    assert isinstance(call.timeout, httpx.Timeout)
    assert call.timeout.read == 300.0
    assert call.timeout.connect == 10.0
    assert osa.calls == []


def test_send_attachment_falls_back_to_applescript_when_bb_answers_400_or_more(
        r, bb, osa, client, tmp_path):
    bb.answers = [FakeResponse(500, text="synthetic BB failure")]
    osa.results = [True]
    content = b"synthetic file body"
    resp = client.post("/send_attachment", data={"chat_guid": CHAT},
                       files=_attachment("note.txt", content, "text/plain"), headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "applescript"}
    assert bb.calls[0].path == "/api/v1/message/attachment"

    assert len(osa.calls) == 1
    script, args = osa.calls[0]
    assert script == r._AS_FILE
    assert args[0] == CHAT
    staged = Path(args[1])
    assert staged.parent == r.OUTBOX == tmp_path / "outbox"      # staged under the patched OUTBOX
    assert staged.name.endswith("-note.txt")
    assert staged.read_bytes() == content


def test_send_attachment_falls_back_to_applescript_when_bb_raises(r, bb, osa, client):
    # A failure before the request can have arrived. One after it (a read
    # timeout) must NOT fall back: tests/test_send_once.py.
    bb.answers = [httpx.ConnectTimeout("synthetic connect timeout")]
    osa.results = [True]
    resp = client.post("/send_attachment", data={"chat_guid": CHAT}, files=_attachment(),
                       headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "applescript"}
    assert len(bb.calls) == 1
    assert len(osa.calls) == 1 and osa.calls[0][0] == r._AS_FILE


def test_send_attachment_returns_502_when_bb_and_applescript_both_fail(r, bb, osa, client):
    bb.answers = [FakeResponse(500, text="synthetic BB failure")]
    osa.results = [False]
    resp = client.post("/send_attachment", data={"chat_guid": CHAT}, files=_attachment(),
                       headers=AUTH)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "BlueBubbles failed" in detail and "AppleScript fallback failed" in detail


def test_send_attachment_empty_file_is_400_before_any_bb_call(bb, osa, client):
    resp = client.post("/send_attachment", data={"chat_guid": CHAT},
                       files=_attachment("empty.bin", b"", "application/octet-stream"),
                       headers=AUTH)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "empty file"
    assert bb.calls == [] and osa.calls == []


# ---------------------------------------------------------------------------
# /react: BlueBubbles only, no fallback
# ---------------------------------------------------------------------------

def test_react_posts_message_react_with_payload_keys(r, bb, osa, client):
    bb.answers = [_bb_ok()]
    resp = client.post("/react", json={"chat_guid": CHAT, "message_guid": MSG_GUID,
                                       "reaction": "love"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    call = bb.calls[0]
    assert call.method == "POST"
    assert call.url.startswith(r.BB_URL + "/")
    assert call.path == "/api/v1/message/react"
    assert call.param_keys == ["password"]
    assert call.timeout == 15
    assert set(call.json) == {"chatGuid", "selectedMessageGuid", "reaction", "partIndex"}
    assert call.json == {"chatGuid": CHAT, "selectedMessageGuid": MSG_GUID,
                         "reaction": "love", "partIndex": 0}
    assert osa.calls == []


def test_react_passes_bb_error_status_through_without_applescript(bb, osa, client):
    bb.answers = [FakeResponse(422, text="synthetic BB rejection")]
    resp = client.post("/react", json={"chat_guid": CHAT, "message_guid": MSG_GUID,
                                       "reaction": "like"}, headers=AUTH)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "synthetic BB rejection"
    assert osa.calls == []


# ---------------------------------------------------------------------------
# /create_chat: existing recipient set -> message/text; new set -> chat/new
# ---------------------------------------------------------------------------

def test_create_chat_reuses_existing_chat_via_message_text(r, bb, osa, client, compat_db):
    bb.answers = [_bb_ok()]
    resp = client.post("/create_chat", json={"addresses": FIND_CASES["one_to_one_phone"],
                                             "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "chat_guid": C1S_GUID}
    call = bb.calls[0]
    assert call.path == "/api/v1/message/text"
    assert call.param_keys == ["password"]
    assert call.timeout == 15
    assert set(call.json) == {"chatGuid", "message", "method", "tempGuid"}
    assert call.json["chatGuid"] == C1S_GUID
    assert call.json["message"] == TEXT
    assert call.json["method"] == "private-api"
    assert call.json["tempGuid"].startswith("relay-")
    assert osa.calls == []


def test_create_chat_new_recipient_set_posts_chat_new(r, bb, osa, client, compat_db):
    bb.answers = [_bb_ok({"status": 200, "data": {"guid": "iMessage;-;synthetic-new"}})]
    addrs = FIND_CASES["no_match_unknown"]
    resp = client.post("/create_chat", json={"addresses": addrs, "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "chat_guid": "iMessage;-;synthetic-new"}
    call = bb.calls[0]
    assert call.method == "POST"
    assert call.url.startswith(r.BB_URL + "/")
    assert call.path == "/api/v1/chat/new"
    assert call.param_keys == ["password"]
    assert call.timeout == 30
    assert set(call.json) == {"addresses", "message", "method", "service", "tempGuid"}
    assert call.json["addresses"] == [r.normalize_address(a) for a in addrs]
    assert call.json["message"] == TEXT
    assert call.json["method"] == "private-api"
    assert call.json["service"] == "iMessage"
    assert call.json["tempGuid"].startswith("relay-new-")
    assert osa.calls == []


def test_create_chat_passes_bb_error_status_through_without_applescript(bb, osa, client, compat_db):
    bb.answers = [FakeResponse(400, text="synthetic BB rejection")]
    resp = client.post("/create_chat", json={"addresses": FIND_CASES["no_match_unknown"],
                                             "text": TEXT}, headers=AUTH)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "synthetic BB rejection"
    assert osa.calls == []


def test_create_chat_requires_addresses_and_text(bb, osa, client):
    for body in ({"addresses": [], "text": TEXT},
                 {"addresses": ["   "], "text": TEXT},
                 {"addresses": FIND_CASES["one_to_one_phone"], "text": "   "}):
        resp = client.post("/create_chat", json=body, headers=AUTH)
        assert resp.status_code == 400, body
        assert resp.json()["detail"] == "addresses and text required"
    assert bb.calls == [] and osa.calls == []


# ---------------------------------------------------------------------------
# auth: the send endpoints sit behind the token middleware
# ---------------------------------------------------------------------------

def test_send_endpoints_require_token(bb, osa, client):
    assert client.post("/send", json={"chat_guid": CHAT, "text": TEXT}).status_code == 401
    assert client.post("/send_attachment", data={"chat_guid": CHAT},
                       files=_attachment()).status_code == 401
    assert client.post("/react", json={"chat_guid": CHAT, "message_guid": MSG_GUID,
                                       "reaction": "love"}).status_code == 401
    assert client.post("/create_chat", json={"addresses": [CHAT], "text": TEXT}).status_code == 401
    assert bb.calls == [] and osa.calls == []


# ---------------------------------------------------------------------------
# the guard-order fix: no BB_PASSWORD -> the chain is [applescript] (R1 xfail,
# flipped by R2)
# ---------------------------------------------------------------------------

def _pre_r2(r) -> None:
    """Against ``relay.py`` before the cutover these are still the R1 xfails."""
    if not hasattr(r, "_chain"):
        pytest.xfail("guard-order bug: 'BB_PASSWORD not set' raises 500 before the "
                     "AppleScript fallback (plan section 4, KB section 11); R2 flips this")


def test_send_text_without_bb_password_still_reaches_applescript(r, bb, osa, client, monkeypatch):
    _pre_r2(r)
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    osa.results = [True]
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "applescript"}
    assert osa.calls == [(r._AS_TEXT, (CHAT, TEXT))]
    assert bb.calls == []                                # BlueBubbles is not in the chain


def test_send_attachment_without_bb_password_still_reaches_applescript(
        r, bb, osa, client, monkeypatch):
    _pre_r2(r)
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    osa.results = [True]
    resp = client.post("/send_attachment", data={"chat_guid": CHAT}, files=_attachment(),
                       headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "applescript"}
    assert len(osa.calls) == 1 and osa.calls[0][0] == r._AS_FILE
    assert bb.calls == []


def test_missing_bb_password_without_applescript_is_a_spoken_501(r, bb, osa, client, monkeypatch):
    """Replaces R1's "500 BB_PASSWORD not set" pin: with no BlueBubbles password
    and the AppleScript fallback switched off there is no engine at all, and the
    chain says so (501) instead of a bare 500; neither side is called."""
    if not hasattr(r, "_chain"):
        pytest.skip("pre-R2 relay: no send-engine chain")
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    monkeypatch.setattr(r, "SEND_APPLESCRIPT_FALLBACK", "0")
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT}, headers=AUTH)
    assert resp.status_code == 501
    assert resp.json()["detail"] == "no configured engine can send text in this chat"
    assert bb.calls == [] and osa.calls == []
