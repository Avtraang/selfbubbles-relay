"""Unit tests for the send-engine chain (plan 2026-10-06, step R2) and the
``/health`` additions.

* ``engines.base``: the capability set, ``SendResult`` / ``Unsupported``.
* ``engines.chain.build_chain``: membership from ``BEEPER_TOKEN`` /
  ``BB_PASSWORD`` / ``SEND_APPLESCRIPT_FALLBACK``, order override and
  validation through ``SEND_ENGINES``.
* ``engines.chain.deliver``: first ok wins, ``handles`` filtering, 501 when
  nothing is capable, 502 with joined details when everything failed, an
  upstream HTTP status passed through when a single engine proxied one, raw
  return values wrapped as ``payload``.
* ``engines.bluebubbles``: every call site through ``httpx.MockTransport`` --
  path, method, the PRESENCE of the ``password`` query parameter (never its
  value: the handler records parameter names only), payload keys, ``tempGuid``
  prefixes, timeouts, the FaceTime bridge, ``contacts`` / ``ping``.
* ``engines.applescript``: runner + outbox hooks, ``_guid_variants`` retry,
  the frozen failure detail, ``Unsupported`` for the rest.
* ``engines.beeper``: ``bp:`` routing, text + reply only, attachment
  ``Unsupported`` (KB item 16 made honest).
* ``engines.features.derive_features``: the derivation table and overrides.
* ``/health`` (authenticated) through the relay under test: ``engines``,
  ``features``, ``protocol``; the unauthenticated answer unchanged; a ``bp:``
  attachment answered with a clean 501.

Synthetic values only; nothing here touches the network, ``osascript``,
``~/Library`` or a real token / password.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from engines import (PROTOCOL, Capability, DeliveryError, EngineError, SendResult,
                     Unsupported, build_chain, deliver, derive_features, engine_names,
                     first_with)
from engines.applescript import FAILED as AS_FAILED
from engines.applescript import AppleScriptEngine, _AS_FILE, _AS_TEXT, _guid_variants
from engines.beeper import FAILED as BEEPER_FAILED
from engines.beeper import BeeperEngine
from engines.bluebubbles import BlueBubblesEngine
from engines.features import FEATURES
from tests.conftest import RELAY_STUB_ENV, has_r6

CHAT = "iMessage;-;+15550001234"      # synthetic
ANY_CHAT = "any;-;+15550001234"
BP_CHAT = "bp:42"
TEXT = "synthetic test message"
MSG_GUID = "p:0/11111111-2222-3333-4444-555555555555"
BB_URL = "http://bb.invalid:1234"
PASSWORD = "stub-bb-password-not-real"
STUB_TOKEN = RELAY_STUB_ENV["IMSG_TOKEN"]
AUTH = {"X-Imsg-Token": STUB_TOKEN}


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

@dataclass
class Seen:
    """One BlueBubbles request as the mock transport saw it; ``param_keys`` are
    the query parameter NAMES only."""

    method: str
    path: str                 # decoded, as httpx reports it
    param_keys: list[str]
    json: Any = None
    content_type: str = ""
    body: bytes = b""
    raw_path: bytes = b""     # on the wire (percent-encoded)


@dataclass
class BBServer:
    """``httpx.MockTransport`` handler with scripted answers (status, body)."""

    answers: list[tuple[int, Any]] = field(default_factory=list)
    seen: list[Seen] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        ctype = request.headers.get("content-type", "")
        parsed = json.loads(body) if ctype.startswith("application/json") else None
        self.seen.append(Seen(request.method, request.url.path,
                              sorted(k for k, _ in request.url.params.multi_items()),
                              parsed, ctype, body, request.url.raw_path))
        if not self.answers:
            raise AssertionError("unexpected BlueBubbles request: no scripted answer")
        status, payload = self.answers.pop(0)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)


@dataclass
class FakeEngine:
    """A scripted chain member: ``results`` is consumed in order."""

    name: str
    via: str
    capabilities: frozenset
    guids: Any = None                     # None = handles everything; else a prefix
    results: list[Any] = field(default_factory=list)
    calls: list[tuple] = field(default_factory=list)
    facetime: Any = None

    def configured(self) -> bool:
        return True

    def ping(self) -> bool:
        return True

    def handles(self, chat_guid: str) -> bool:
        return self.guids is None or chat_guid.startswith(self.guids)

    async def _next(self, *args: Any) -> Any:
        self.calls.append(args)
        res = self.results.pop(0)
        if isinstance(res, BaseException):
            raise res
        return res

    async def send_text(self, chat_guid, text, reply_to_guid=None):
        return await self._next("send_text", chat_guid, text, reply_to_guid)

    async def send_attachment(self, chat_guid, name, content, content_type):
        return await self._next("send_attachment", chat_guid, name, content, content_type)

    async def react(self, chat_guid, message_guid, reaction):
        return await self._next("react", chat_guid, message_guid, reaction)

    async def create_chat(self, addresses, text):
        return await self._next("create_chat", addresses, text)

    async def chat_icon(self, chat_guid):
        return await self._next("chat_icon", chat_guid)

    def contacts(self):
        return []


def ok(via: str, **kw: Any) -> SendResult:
    return SendResult(True, via, **kw)


def failed(via: str, detail: str, **kw: Any) -> SendResult:
    return SendResult(False, via, detail, **kw)


async def run(coro):
    return await coro


def sync(coro):
    import asyncio
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------

def test_capability_set_is_the_planned_one():
    # the R2 set, plus UNSEND and EDIT (step R6)
    assert {c.name for c in Capability} == {"TEXT", "ATTACHMENT", "REPLY", "REACT",
                                            "CREATE_CHAT", "CHAT_ICON", "CONTACTS", "FACETIME",
                                            "UNSEND", "EDIT"}
    assert Capability.UNSEND.value == "unsend" and Capability.EDIT.value == "edit"


def test_engine_capabilities_and_via_strings_are_frozen():
    bb = BlueBubblesEngine(BB_URL, PASSWORD)
    # everything but EDIT: BlueBubbles' edit call does nothing on macOS 27 (step R6)
    assert bb.capabilities == frozenset(Capability) - {Capability.EDIT}
    assert Capability.UNSEND in bb.capabilities
    assert bb.via == "bb" and bb.name == "bluebubbles"
    assert bb.facetime is not None
    osa = AppleScriptEngine()
    assert osa.capabilities == {Capability.TEXT, Capability.ATTACHMENT}
    assert osa.via == "applescript" and osa.facetime is None
    beeper = BeeperEngine("stub")
    assert beeper.capabilities == {Capability.TEXT, Capability.REPLY}
    assert beeper.via == "gmessages" and beeper.name == "beeper" and beeper.facetime is None
    # no pre-R6 engine other than BlueBubbles gained anything
    for engine in (osa, beeper):
        assert not {Capability.UNSEND, Capability.EDIT} & engine.capabilities


def test_handles_splits_on_the_beeper_prefix():
    bb, osa, beeper = BlueBubblesEngine(BB_URL, PASSWORD), AppleScriptEngine(), BeeperEngine("stub")
    for guid in (CHAT, ANY_CHAT, "SMS;-;+15550001234", "chat123"):
        assert bb.handles(guid) and osa.handles(guid) and not beeper.handles(guid)
    assert beeper.handles(BP_CHAT) and not bb.handles(BP_CHAT) and not osa.handles(BP_CHAT)


def test_unsupported_names_engine_and_verb():
    with pytest.raises(Unsupported) as info:
        sync(AppleScriptEngine().react(CHAT, MSG_GUID, "love"))
    assert info.value.detail == "applescript cannot react"
    assert isinstance(info.value, EngineError)
    with pytest.raises(Unsupported):
        sync(BeeperEngine("stub").send_attachment(BP_CHAT, "a.png", b"x", "image/png"))
    with pytest.raises(Unsupported):
        AppleScriptEngine().contacts()


# ---------------------------------------------------------------------------
# build_chain
# ---------------------------------------------------------------------------

def test_build_chain_default_membership_follows_the_config():
    assert engine_names(build_chain({})) == ["applescript"]
    assert engine_names(build_chain({"BB_PASSWORD": "x"})) == ["bluebubbles", "applescript"]
    assert engine_names(build_chain({"BEEPER_TOKEN": "t", "BB_PASSWORD": "x"})) == \
        ["beeper", "bluebubbles", "applescript"]
    assert engine_names(build_chain({"BEEPER_TOKEN": "t"})) == ["beeper", "applescript"]
    assert engine_names(build_chain({"BB_PASSWORD": "x", "SEND_APPLESCRIPT_FALLBACK": "0"})) == \
        ["bluebubbles"]
    assert engine_names(build_chain({"SEND_APPLESCRIPT_FALLBACK": "0"})) == []
    assert engine_names(build_chain({"BB_PASSWORD": "  "})) == ["bluebubbles", "applescript"]


def test_build_chain_uses_bb_url_and_passes_the_applescript_engine_through():
    osa = AppleScriptEngine()
    chain = build_chain({"BB_PASSWORD": "x", "BB_URL": "http://bb.invalid:9/"}, applescript=osa)
    assert chain[0].url == "http://bb.invalid:9"
    assert chain[1] is osa


def test_send_engines_overrides_order_and_drops_unconfigured_names():
    env = {"BEEPER_TOKEN": "t", "BB_PASSWORD": "x", "SEND_ENGINES": "applescript, bluebubbles"}
    assert engine_names(build_chain(env)) == ["applescript", "bluebubbles"]
    env = {"SEND_ENGINES": "bluebubbles,applescript"}          # no password: BB left out
    assert engine_names(build_chain(env)) == ["applescript"]
    env = {"BB_PASSWORD": "x", "SEND_ENGINES": "bluebubbles,bluebubbles"}
    assert engine_names(build_chain(env)) == ["bluebubbles"]
    # an explicit list ignores SEND_APPLESCRIPT_FALLBACK (it names what it wants)
    env = {"SEND_ENGINES": "applescript", "SEND_APPLESCRIPT_FALLBACK": "0"}
    assert engine_names(build_chain(env)) == ["applescript"]


def test_send_engines_unknown_name_is_a_value_error():
    with pytest.raises(ValueError) as info:
        build_chain({"SEND_ENGINES": "bluebubbles,pimsg"})
    assert "pimsg" in str(info.value) and "known:" in str(info.value)


def test_first_with_filters_by_capability_and_guid():
    chain = build_chain({"BEEPER_TOKEN": "t", "BB_PASSWORD": "x"})
    assert first_with(chain, Capability.TEXT, BP_CHAT).name == "beeper"
    assert first_with(chain, Capability.TEXT, CHAT).name == "bluebubbles"
    assert first_with(chain, Capability.ATTACHMENT, BP_CHAT) is None
    assert first_with(chain, Capability.FACETIME).name == "bluebubbles"
    assert first_with(build_chain({}), Capability.CONTACTS) is None


# ---------------------------------------------------------------------------
# deliver
# ---------------------------------------------------------------------------

def _logs() -> tuple[list[str], Any]:
    lines: list[str] = []
    return lines, lines.append


def test_deliver_first_ok_wins_and_nothing_after_it_is_called():
    a = FakeEngine("a", "va", frozenset({Capability.TEXT}), results=[ok("va", payload={"k": 1})])
    b = FakeEngine("b", "vb", frozenset({Capability.TEXT}), results=[ok("vb")])
    lines, log = _logs()
    res = sync(deliver([a, b], Capability.TEXT, CHAT, TEXT, log=log, reply_to_guid=MSG_GUID))
    assert res == SendResult(True, "va", payload={"k": 1})
    assert a.calls == [("send_text", CHAT, TEXT, MSG_GUID)] and b.calls == []
    assert lines == []                                   # nothing to report on a first-try success


def test_deliver_falls_through_on_failure_and_logs_the_hop():
    a = FakeEngine("a", "va", frozenset({Capability.TEXT}), results=[failed("va", "a failed (x)")])
    b = FakeEngine("b", "vb", frozenset({Capability.TEXT}), results=[ok("vb")])
    lines, log = _logs()
    res = sync(deliver([a, b], Capability.TEXT, CHAT, TEXT, log=log))
    assert res.ok and res.via == "vb"
    assert lines == [f"[send] a failed (a failed (x)) — trying b",
                     f"[send] delivered via b -> {CHAT}"]


def test_deliver_skips_engines_without_the_capability_or_the_guid():
    only_react = FakeEngine("r", "vr", frozenset({Capability.REACT}), results=[ok("vr")])
    bp_only = FakeEngine("bp", "vbp", frozenset({Capability.TEXT}), guids="bp:", results=[ok("vbp")])
    text = FakeEngine("t", "vt", frozenset({Capability.TEXT}), results=[ok("vt")])
    res = sync(deliver([only_react, bp_only, text], Capability.TEXT, CHAT, TEXT, log=lambda s: None))
    assert res.via == "vt"
    assert only_react.calls == [] and bp_only.calls == []


def test_deliver_501_when_no_engine_is_capable():
    chain = [FakeEngine("r", "vr", frozenset({Capability.REACT}))]
    with pytest.raises(DeliveryError) as info:
        sync(deliver(chain, Capability.ATTACHMENT, CHAT, "a.png", b"x", "image/png"))
    assert (info.value.status, info.value.detail) == \
        (501, "no configured engine can send attachments in this chat")
    with pytest.raises(DeliveryError) as info:
        sync(deliver([], Capability.CREATE_CHAT, None, ["+15550001234"], TEXT))
    assert (info.value.status, info.value.detail) == (501, "no configured engine can create a chat")


def test_deliver_502_joins_every_failure_detail_in_order():
    a = FakeEngine("a", "va", frozenset({Capability.TEXT}),
                   results=[failed("va", "BlueBubbles failed (HTTP 500: boom)", status=500, body="boom")])
    b = FakeEngine("b", "vb", frozenset({Capability.TEXT}), results=[failed("vb", AS_FAILED)])
    with pytest.raises(DeliveryError) as info:
        sync(deliver([a, b], Capability.TEXT, CHAT, TEXT, log=lambda s: None))
    assert info.value.status == 502
    assert info.value.detail == "BlueBubbles failed (HTTP 500: boom); AppleScript fallback failed"


def test_deliver_single_engine_upstream_status_passes_through():
    a = FakeEngine("a", "va", frozenset({Capability.REACT}),
                   results=[failed("va", "BlueBubbles failed (HTTP 422: nope)", status=422, body="nope")])
    with pytest.raises(DeliveryError) as info:
        sync(deliver([a], Capability.REACT, CHAT, MSG_GUID, "love", log=lambda s: None))
    assert (info.value.status, info.value.detail) == (422, "nope")
    # ...but a single failure WITHOUT an upstream status is a 502 with its detail
    b = FakeEngine("b", "vb", frozenset({Capability.TEXT}), results=[failed("vb", BEEPER_FAILED)])
    with pytest.raises(DeliveryError) as info:
        sync(deliver([b], Capability.TEXT, BP_CHAT, TEXT, log=lambda s: None))
    assert (info.value.status, info.value.detail) == (502, BEEPER_FAILED)


def test_deliver_wraps_raw_returns_and_engine_errors():
    a = FakeEngine("a", "va", frozenset({Capability.CREATE_CHAT, Capability.CHAT_ICON}),
                   results=["iMessage;-;new", None, EngineError("unreachable: x"),
                            EngineError("HTTP 400: bad", status=400, body="bad"),
                            RuntimeError("unwrapped")])
    res = sync(deliver([a], Capability.CREATE_CHAT, None, ["+15550001234"], TEXT))
    assert res == SendResult(True, "va", payload="iMessage;-;new")
    assert sync(deliver([a], Capability.CHAT_ICON, CHAT)).payload is None     # ok, just no icon
    with pytest.raises(DeliveryError) as info:
        sync(deliver([a], Capability.CHAT_ICON, CHAT, log=lambda s: None))
    assert (info.value.status, info.value.detail) == (502, "unreachable: x")
    with pytest.raises(DeliveryError) as info:
        sync(deliver([a], Capability.CHAT_ICON, CHAT, log=lambda s: None))
    assert (info.value.status, info.value.detail) == (400, "bad")
    with pytest.raises(DeliveryError) as info:
        sync(deliver([a], Capability.CHAT_ICON, CHAT, log=lambda s: None))
    assert (info.value.status, info.value.detail) == (502, "a failed (unwrapped)")


def test_deliver_treats_unsupported_as_a_failure_not_a_crash():
    a = FakeEngine("a", "va", frozenset({Capability.TEXT}),
                   results=[Unsupported("a", Capability.TEXT)])
    b = FakeEngine("b", "vb", frozenset({Capability.TEXT}), results=[ok("vb")])
    assert sync(deliver([a, b], Capability.TEXT, CHAT, TEXT, log=lambda s: None)).via == "vb"


# ---------------------------------------------------------------------------
# BlueBubbles engine (httpx.MockTransport; password param presence only)
# ---------------------------------------------------------------------------

@pytest.fixture
def bbs() -> BBServer:
    return BBServer()


@pytest.fixture
def bb(bbs) -> BlueBubblesEngine:
    return BlueBubblesEngine(BB_URL + "/", PASSWORD, transport=bbs.transport())


def test_bb_engine_strips_trailing_slash_and_is_configured_only_with_a_password():
    assert BlueBubblesEngine(BB_URL + "/", PASSWORD).url == BB_URL
    assert BlueBubblesEngine(BB_URL, PASSWORD).configured()
    assert not BlueBubblesEngine(BB_URL, "").configured()


def test_bb_send_text(bb, bbs):
    bbs.answers = [(200, {"status": 200, "data": {"guid": "synthetic"}})]
    res = sync(bb.send_text(CHAT, TEXT))
    assert res == SendResult(True, "bb", payload={"status": 200, "data": {"guid": "synthetic"}})
    s = bbs.seen[0]
    assert (s.method, s.path, s.param_keys) == ("POST", "/api/v1/message/text", ["password"])
    assert set(s.json) == {"chatGuid", "message", "method", "tempGuid"}
    assert s.json["chatGuid"] == CHAT and s.json["message"] == TEXT
    assert s.json["method"] == "private-api" and s.json["tempGuid"].startswith("relay-")


def test_bb_send_reply_adds_selected_message_and_part_index(bb, bbs):
    bbs.answers = [(200, {})]
    sync(bb.send_text(CHAT, TEXT, reply_to_guid=MSG_GUID))
    s = bbs.seen[0]
    assert s.json["selectedMessageGuid"] == MSG_GUID and s.json["partIndex"] == 0


def test_bb_send_text_failures_are_results_not_exceptions(bb, bbs):
    bbs.answers = [(500, "synthetic BB failure")]
    res = sync(bb.send_text(CHAT, TEXT))
    assert res == SendResult(False, "bb", "BlueBubbles failed (HTTP 500: synthetic BB failure)",
                             status=500, body="synthetic BB failure")
    bbs.answers = [(0, httpx.ConnectError("synthetic connection refused"))]
    res = sync(bb.send_text(CHAT, TEXT))
    assert res.ok is False and res.status is None
    assert res.detail == "BlueBubbles failed (synthetic connection refused)"


def test_bb_send_text_accepts_a_2xx_whose_body_is_not_json(bb, bbs):
    # A proxy or an odd BlueBubbles build can answer 200 with a non-JSON body.
    # The message was accepted, so this must be an ok result; a failure here
    # would make the chain fall through to AppleScript and send it twice.
    bbs.answers = [(200, "<html>ok</html>")]
    res = sync(bb.send_text(CHAT, TEXT))
    assert res == SendResult(True, "bb", payload={})
    assert len(bbs.seen) == 1


def test_bb_send_attachment_is_multipart_with_the_long_timeout(bb, bbs, monkeypatch):
    seen_timeouts: list[Any] = []
    real = httpx.AsyncClient

    class Spy(real):
        def __init__(self, *a: Any, **kw: Any):
            seen_timeouts.append(kw.get("timeout"))
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Spy)
    bbs.answers = [(200, {"status": 200})]
    res = sync(bb.send_attachment(CHAT, "synthetic.png", b"\x89PNG synthetic", "image/png"))
    assert res == SendResult(True, "bb")                         # no payload: shape has no "via"
    s = bbs.seen[0]
    assert (s.method, s.path, s.param_keys) == ("POST", "/api/v1/message/attachment", ["password"])
    assert s.content_type.startswith("multipart/form-data")
    for part in (b'name="chatGuid"', b'name="tempGuid"', b'name="name"', b'name="method"',
                 b'name="attachment"; filename="synthetic.png"', b"relay-att-", b"private-api",
                 b"\x89PNG synthetic"):
        assert part in s.body, part
    assert isinstance(seen_timeouts[0], httpx.Timeout)
    assert seen_timeouts[0].read == 300.0 and seen_timeouts[0].connect == 10.0


def test_bb_react(bb, bbs):
    bbs.answers = [(200, {}), (422, "synthetic BB rejection")]
    assert sync(bb.react(CHAT, MSG_GUID, "love")) == SendResult(True, "bb")
    s = bbs.seen[0]
    assert (s.method, s.path, s.param_keys) == ("POST", "/api/v1/message/react", ["password"])
    assert s.json == {"chatGuid": CHAT, "selectedMessageGuid": MSG_GUID,
                      "reaction": "love", "partIndex": 0}
    res = sync(bb.react(CHAT, MSG_GUID, "love"))
    assert (res.ok, res.status, res.body) == (False, 422, "synthetic BB rejection")


def test_bb_create_chat(bb, bbs):
    bbs.answers = [(200, {"status": 200, "data": {"guid": "iMessage;-;synthetic-new"}})]
    assert sync(bb.create_chat(["+15550001234"], TEXT)) == "iMessage;-;synthetic-new"
    s = bbs.seen[0]
    assert (s.method, s.path, s.param_keys) == ("POST", "/api/v1/chat/new", ["password"])
    assert set(s.json) == {"addresses", "message", "method", "service", "tempGuid"}
    assert s.json["addresses"] == ["+15550001234"] and s.json["service"] == "iMessage"
    assert s.json["tempGuid"].startswith("relay-new-")
    bbs.answers = [(400, "synthetic BB rejection")]
    with pytest.raises(EngineError) as info:
        sync(bb.create_chat(["+15550001234"], TEXT))
    assert (info.value.status, info.value.body) == (400, "synthetic BB rejection")
    bbs.answers = [(0, httpx.ConnectError("synthetic refused"))]
    with pytest.raises(EngineError) as info:
        sync(bb.create_chat(["+15550001234"], TEXT))
    assert info.value.status is None and "synthetic refused" in info.value.detail


def test_bb_chat_icon(bb, bbs):
    bbs.answers = [(200, b"\x89PNG icon"), (404, ""), (200, b""),
                   (0, httpx.ConnectError("synthetic refused"))]
    assert sync(bb.chat_icon("iMessage;+;chat100")) == b"\x89PNG icon"
    s = bbs.seen[0]
    assert (s.method, s.path, s.param_keys) == ("GET", "/api/v1/chat/iMessage;+;chat100/icon",
                                                 ["password"])
    assert s.raw_path.startswith(b"/api/v1/chat/iMessage%3B%2B%3Bchat100/icon")   # guid quoted
    assert sync(bb.chat_icon("iMessage;+;chat100")) is None
    assert sync(bb.chat_icon("iMessage;+;chat100")) is None      # empty body = no icon
    with pytest.raises(EngineError) as info:
        sync(bb.chat_icon("iMessage;+;chat100"))
    assert info.value.detail.startswith("BlueBubbles unreachable: ")


@pytest.mark.parametrize("guid", ["..", ".", "", "..."])
def test_bb_chat_icon_makes_no_request_for_a_guid_that_is_only_dots(bb, bbs, guid):
    """``/api/v1/chat/../icon`` is ``/api/v1/icon`` once httpx has resolved the
    dot segment: not a chat, so nothing is asked (and the password stays home)."""
    assert sync(bb.chat_icon(guid)) is None
    assert bbs.seen == []


def test_bb_chat_icon_guid_with_separators_stays_one_segment(bb, bbs):
    bbs.answers = [(404, "")]
    assert sync(bb.chat_icon("../../message/text?x=1#f")) is None
    assert bbs.seen[0].raw_path.startswith(b"/api/v1/chat/..%2F..%2Fmessage%2Ftext%3Fx%3D1%23f/icon")


def test_bb_contacts_and_ping(bb, bbs):
    bbs.answers = [(200, {"data": [{"displayName": "Synthetic Person"}]}), (500, "down"),
                   (0, httpx.ConnectError("synthetic refused")), (200, {}), (401, "")]
    assert bb.contacts() == [{"displayName": "Synthetic Person"}]
    s = bbs.seen[0]
    assert (s.method, s.path, s.param_keys) == ("GET", "/api/v1/contact", ["password"])
    with pytest.raises(EngineError) as info:
        bb.contacts()
    assert info.value.detail == "BlueBubbles returned HTTP 500: down"
    with pytest.raises(EngineError) as info:
        bb.contacts()
    assert info.value.detail.startswith("request error: ")
    assert bb.ping() is True
    assert bbs.seen[-1].path == "/api/v1/ping" and bbs.seen[-1].param_keys == ["password"]
    assert bb.ping() is False
    assert BlueBubblesEngine("http://127.0.0.1:9", PASSWORD).ping() is False   # discard port


def test_bb_facetime_bridge(bb, bbs):
    bbs.answers = [(200, {"data": {"link": "https://facetime.apple.com/join#synthetic"}}),
                   (200, {}), (500, "synthetic BB failure"),
                   (200, {}), (403, "nope"),
                   (200, {"data": {"link": "https://facetime.apple.com/join#synthetic2"}}),
                   (200, {"data": {}})]
    ft = bb.facetime
    assert sync(ft.answer("synthetic-uuid")) == "https://facetime.apple.com/join#synthetic"
    s = bbs.seen[0]
    assert (s.method, s.path, s.param_keys) == ("POST", "/api/v1/facetime/answer/synthetic-uuid",
                                                 ["password"])
    assert sync(ft.answer("synthetic-uuid")) is None
    with pytest.raises(EngineError) as info:
        sync(ft.answer("synthetic-uuid"))
    assert (info.value.status, info.value.body) == (500, "synthetic BB failure")
    assert sync(ft.leave("synthetic-uuid")) is None
    assert bbs.seen[-1].path == "/api/v1/facetime/leave/synthetic-uuid"
    with pytest.raises(EngineError) as info:
        sync(ft.leave("synthetic-uuid"))
    assert info.value.status == 403
    assert sync(ft.new_link()) == "https://facetime.apple.com/join#synthetic2"
    assert bbs.seen[-1].path == "/api/v1/facetime/session"
    assert sync(ft.new_link()) is None


def test_bb_error_messages_never_carry_the_password(bb, bbs):
    bbs.answers = [(500, "synthetic BB failure"), (0, httpx.ConnectError("refused"))]
    for res in (sync(bb.send_text(CHAT, TEXT)), sync(bb.send_text(CHAT, TEXT))):
        assert PASSWORD not in res.detail and PASSWORD not in (res.body or "")
    with pytest.raises(EngineError) as info:
        BlueBubblesEngine("http://127.0.0.1:9", PASSWORD).contacts()
    assert PASSWORD not in str(info.value)


# ---------------------------------------------------------------------------
# AppleScript engine
# ---------------------------------------------------------------------------

@dataclass
class Runner:
    results: list[bool] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def __call__(self, script: str, *args: str) -> bool:
        self.calls.append((script, tuple(args)))
        return self.results.pop(0)


def test_applescript_text_tries_guid_variants_until_one_works(tmp_path):
    run = Runner([False, True])
    osa = AppleScriptEngine(runner=run, outbox=tmp_path / "outbox")
    assert sync(osa.send_text(ANY_CHAT, TEXT, reply_to_guid=MSG_GUID)) == SendResult(True, "applescript")
    assert run.calls == [(_AS_TEXT, (ANY_CHAT, TEXT)),
                         (_AS_TEXT, ("iMessage;" + ANY_CHAT[len("any;"):], TEXT))]
    run = Runner([False])
    osa = AppleScriptEngine(runner=run, outbox=tmp_path / "outbox")
    assert sync(osa.send_text(CHAT, TEXT)) == SendResult(False, "applescript", AS_FAILED)
    assert run.calls == [(_AS_TEXT, (CHAT, TEXT))]


def test_applescript_attachment_stages_the_file_in_the_outbox_and_prunes_old_ones(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    stale = outbox / "0-stale.bin"
    stale.write_bytes(b"old")
    import os
    import time
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    fresh = outbox / "1-fresh.bin"
    fresh.write_bytes(b"new")

    run = Runner([True])
    osa = AppleScriptEngine(runner=run, outbox=lambda: outbox)       # callable hook
    res = sync(osa.send_attachment(CHAT, "../../note.txt", b"synthetic body", "text/plain"))
    assert res == SendResult(True, "applescript")
    script, args = run.calls[0]
    assert script == _AS_FILE and args[0] == CHAT
    staged = Path(args[1])
    assert staged.parent == outbox and staged.name.endswith("-note.txt")   # basename only
    assert staged.read_bytes() == b"synthetic body"
    assert not stale.exists() and fresh.exists()


def test_applescript_default_hooks_resolve_at_call_time(monkeypatch, tmp_path):
    import engines.applescript as mod
    run = Runner([True, True])
    monkeypatch.setattr(mod, "_applescript", run)
    monkeypatch.setattr(mod, "OUTBOX", tmp_path / "outbox")
    osa = AppleScriptEngine()
    assert sync(osa.send_text(CHAT, TEXT)).ok
    assert sync(osa.send_attachment(CHAT, "a.bin", b"x", "application/octet-stream")).ok
    assert Path(run.calls[1][1][1]).parent == tmp_path / "outbox"
    assert osa.configured() and osa.ping()


def test_guid_variants_pinned():
    assert list(_guid_variants(CHAT)) == [CHAT]
    assert list(_guid_variants(ANY_CHAT)) == [ANY_CHAT, "iMessage;" + ANY_CHAT[len("any;"):]]
    assert list(_guid_variants("SMS;-;+15550001234")) == ["SMS;-;+15550001234"]


# ---------------------------------------------------------------------------
# Beeper engine
# ---------------------------------------------------------------------------

def test_beeper_engine_sends_text_and_replies_through_the_injected_send():
    seen: list[tuple] = []

    async def fake_send(chat_guid, text, reply_to=None):
        seen.append((chat_guid, text, reply_to))
        return len(seen) == 1

    e = BeeperEngine("stub", send=fake_send)
    assert e.configured() and not BeeperEngine("").configured()
    assert sync(e.send_text(BP_CHAT, TEXT, reply_to_guid=MSG_GUID)) == SendResult(True, "gmessages")
    assert sync(e.send_text(BP_CHAT, TEXT)) == SendResult(False, "gmessages", BEEPER_FAILED)
    assert seen == [(BP_CHAT, TEXT, MSG_GUID), (BP_CHAT, TEXT, None)]


def test_beeper_engine_default_send_is_looked_up_on_beeper_at_call_time(monkeypatch):
    import beeper

    async def fake_send(chat_guid, text, reply_to=None):
        return True

    monkeypatch.setattr(beeper, "send", fake_send)
    assert sync(BeeperEngine("stub").send_text(BP_CHAT, TEXT)).ok


# ---------------------------------------------------------------------------
# derive_features
# ---------------------------------------------------------------------------

def test_derive_features_table():
    off = {"facetime": False, "map": False, "translate": False, "voice": False}
    assert derive_features({}) == off
    assert list(derive_features({})) == list(FEATURES) == ["facetime", "map", "translate", "voice"]
    assert derive_features({"BB_PASSWORD": "x"}) == {**off, "facetime": True, "voice": True}
    assert derive_features({"BB_PASSWORD": "x", "FT_AUTOADMIT": "0"})["facetime"] is True   # objection 1
    assert derive_features({"FT_AUTOADMIT": "1"})["facetime"] is False
    assert derive_features({"MAPKIT_TOKEN": "m"})["map"] is False
    assert derive_features({"HA_TOKEN": "h"})["map"] is False
    assert derive_features({"MAPKIT_TOKEN": "m", "HA_TOKEN": "h"})["map"] is True
    assert derive_features({"OLLAMA_MODEL": "m"})["translate"] is True
    assert derive_features({"MARIAN_URL": "http://127.0.0.1:8701"})["translate"] is True
    assert derive_features({"OLLAMA_URL": "http://localhost:11434"})["translate"] is False
    assert derive_features({"BB_PASSWORD": " "}) == off          # blank = unset


def test_derive_features_overrides():
    env = {"BB_PASSWORD": "x", "MAPKIT_TOKEN": "m", "HA_TOKEN": "h", "OLLAMA_MODEL": "q"}
    assert derive_features(env) == {"facetime": True, "map": True, "translate": True, "voice": True}
    assert derive_features({**env, "FEATURE_VOICE": "0"})["voice"] is False
    assert derive_features({**env, "FEATURE_VOICE": "0"})["facetime"] is True   # independent
    assert derive_features({"FEATURE_MAP": "1"})["map"] is True
    for truthy in ("1", "true", "YES", "On"):
        assert derive_features({f"FEATURE_TRANSLATE": truthy})["translate"] is True, truthy
    for falsy in ("0", "false", "no", "off", "maybe"):
        assert derive_features({**env, "FEATURE_FACETIME": falsy})["facetime"] is False, falsy
    assert derive_features({**env, "FEATURE_FACETIME": "  "})["facetime"] is True   # blank = no override


# ---------------------------------------------------------------------------
# the relay under test: /health additions and the bp: attachment 501
# ---------------------------------------------------------------------------

@pytest.fixture
def r(relay_module):
    """The relay under test; skips when it predates the engine chain
    (``relay.py`` before the cutover), like the other post-cutover glue tests."""
    mod = relay_module.module
    if not hasattr(mod, "_chain"):
        pytest.skip(f"{relay_module.name} has no send-engine chain (pre-R2)")
    return mod


@pytest.fixture
def client(r) -> TestClient:
    """ASGI test client; no ``with``, so startup hooks never run."""
    return TestClient(r.app)


def test_health_authenticated_adds_engines_features_protocol(r, client):
    body = client.get("/health", headers=AUTH).json()
    # step R6 appended "capabilities"; a relay before it answers the R2 shape
    assert list(body) == ["ok", "cursor", "contacts", "self", "bb_reachable",
                          "engines", "features", "protocol",
                          *(["capabilities"] if has_r6(r) else [])]
    assert body["engines"] == ["bluebubbles", "applescript"]      # stub env: BB_PASSWORD, no Beeper
    assert body["protocol"] == PROTOCOL == 1
    assert list(body["features"]) == ["facetime", "map", "translate", "voice"]
    assert all(isinstance(v, bool) for v in body["features"].values())
    assert body["features"]["facetime"] is True and body["features"]["voice"] is True
    assert body["features"] == r.FEATURES
    assert body["bb_reachable"] is False


def test_health_unauthenticated_is_unchanged(client):
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/health", headers={"X-Imsg-Token": "wrong"}).json() == {"ok": True}


def test_health_engines_follow_the_module_config(r, client, monkeypatch):
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    assert client.get("/health", headers=AUTH).json()["engines"] == ["applescript"]
    monkeypatch.setattr(r.beeper, "BEEPER_TOKEN", "stub-beeper-token-not-real")
    assert client.get("/health", headers=AUTH).json()["engines"] == ["beeper", "applescript"]
    monkeypatch.setattr(r, "SEND_APPLESCRIPT_FALLBACK", "0")
    assert client.get("/health", headers=AUTH).json()["engines"] == ["beeper"]


def test_bp_attachment_is_a_clean_501(r, client, monkeypatch):
    """KB item 16 made honest: an attachment into a Google Messages thread used
    to be handed to BlueBubbles (which cannot route a bp: guid); now no engine
    claims it and the app gets a spoken 501, with or without Beeper configured."""
    for token in ("", "stub-beeper-token-not-real"):
        monkeypatch.setattr(r.beeper, "BEEPER_TOKEN", token)
        resp = client.post("/send_attachment", data={"chat_guid": BP_CHAT},
                           files={"file": ("a.png", b"\x89PNG", "image/png")}, headers=AUTH)
        assert resp.status_code == 501, token
        assert resp.json() == {"detail": "no configured engine can send attachments in this chat"}


def test_react_and_create_chat_without_bb_are_501(r, client, monkeypatch, tmp_path):
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    resp = client.post("/react", json={"chat_guid": CHAT, "message_guid": MSG_GUID,
                                       "reaction": "love"}, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == \
        (501, "no configured engine can react in this chat")
    for path in ("/ft_link",):
        resp = client.post(path, headers=AUTH)
        assert (resp.status_code, resp.json()["detail"]) == \
            (501, "no configured engine can handle FaceTime"), path
    resp = client.post("/ft_answer", params={"uuid": "synthetic"}, headers=AUTH)
    assert resp.status_code == 501
    resp = client.post("/ft_decline", params={"uuid": "synthetic"}, headers=AUTH)
    assert resp.status_code == 501


def test_relay_exposes_the_applescript_hooks_under_their_old_names(r):
    import engines.applescript as mod
    assert r._AS_TEXT is mod._AS_TEXT and r._AS_FILE is mod._AS_FILE
    assert r._guid_variants is mod._guid_variants
    assert r.OUTBOX == mod.OUTBOX
    assert r._applescript is mod._applescript
