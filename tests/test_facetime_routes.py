"""``/ft_answer``, ``/ft_decline`` and ``/ft_link`` against a scripted BlueBubbles.

The three routes are thin proxies over BlueBubbles' FaceTime endpoints
(``engines.bluebubbles.BlueBubblesFaceTime``). ``httpx.AsyncClient`` is the
recording fake from ``tests/test_send_path.py``: nothing dials out, and the
password is recorded as a parameter NAME only.

Pinned here, unchanged by step R5:

* success: ``{"link": ...}`` / ``{"ok": true}``, the BlueBubbles path and the
  90 s / 30 s timeouts, and ``_launch_autoadmit`` called with the link
  (``incoming=True`` only after an answer);
* a 2xx without a link: ``502 BlueBubbles returned no link``;
* a BlueBubbles HTTP error: its status and body passed through;
* no ``uuid``: 422; no token: 401; no BlueBubbles engine: 501.

Changed by step R5 (2026-10-07): a TRANSPORT error on the way to BlueBubbles
(refused, timed out) used to escape as the raw ``httpx`` exception and reach
the client as a bare ``500 Internal Server Error``. The bridge now raises
``EngineError("BlueBubbles unreachable")`` and the routes answer
``502 {"detail": "BlueBubbles unreachable"}``. That lives in the engine, which
``relay.py`` shares, so these tests run on either relay module; only the new
log lines are specific to the R5 relay.

Also step R5, from the review of it:

* the call id. ``uuid`` went into the BlueBubbles path as it came, and
  ``httpx`` resolves dot segments, so ``uuid=../../message/text`` made the
  relay POST to any other BlueBubbles endpoint with the server password. The
  engine now sends the id as ONE percent-encoded path segment and refuses an
  id that is empty or only dots; the R5 routes answer 422 to anything that
  does not look like a call id before BlueBubbles is asked.
* a 2xx answer that is not the expected JSON (another service on BlueBubbles'
  port, an HTML page) was a bare 500 as well: it is
  ``502 {"detail": "BlueBubbles returned an unexpected answer"}`` now.
* every exception on the way to BlueBubbles counts as unreachable, not only
  ``httpx``'s own (a ``BB_URL`` with a port above 65535 raises an
  ``ExceptionGroup`` around an ``OverflowError``).

The auto-admit rig is never started: ``_launch_autoadmit`` is replaced by a
recorder, ``FT_AUTOADMIT=0`` is set and ``subprocess.Popen`` raises.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from engines import EngineError
from engines.bluebubbles import BAD_CALL_ID, UNEXPECTED, UNREACHABLE, BlueBubblesEngine, _call_segment
from tests.conftest import RELAY_STUB_ENV, has_r5
from tests.test_send_path import BBRecorder, FakeResponse

AUTH = {"X-Imsg-Token": RELAY_STUB_ENV["IMSG_TOKEN"]}
UUID = "synthetic-call-uuid"
LINK = "https://facetime.apple.com/join#v=1&p=synthetic"

TRANSPORT_ERRORS = (httpx.ConnectError("synthetic refused: SECRET-DETAIL"),
                    httpx.ConnectTimeout("synthetic connect timeout: SECRET-DETAIL"),
                    httpx.ReadTimeout("synthetic read timeout: SECRET-DETAIL"),
                    httpx.RemoteProtocolError("synthetic disconnect: SECRET-DETAIL"))


@pytest.fixture
def r(relay_module):
    mod = relay_module.module
    if not hasattr(mod, "_chain"):
        pytest.skip(f"{relay_module.name} has no send-engine chain (pre-R2)")
    return mod


@pytest.fixture
def bb(r, monkeypatch) -> BBRecorder:
    rec = BBRecorder()
    monkeypatch.setattr(r.httpx, "AsyncClient", rec.client_factory())
    return rec


@pytest.fixture
def launched(r, monkeypatch) -> list:
    """Records ``_launch_autoadmit`` calls; the rig itself can never start."""
    calls: list = []
    monkeypatch.setenv("FT_AUTOADMIT", "0")
    monkeypatch.setattr(r.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the rig must not start")))
    monkeypatch.setattr(r, "_launch_autoadmit",
                        lambda link, incoming=False: calls.append((link, incoming)))
    return calls


@pytest.fixture
def client(r) -> TestClient:
    """ASGI test client; no ``with``, so startup hooks never run."""
    return TestClient(r.app)


def _link_answer(link=LINK) -> FakeResponse:
    return FakeResponse(200, {"status": 200, "data": {"link": link}})


# ---------------------------------------------------------------------------
# success, unchanged
# ---------------------------------------------------------------------------

def test_ft_answer_returns_the_link_and_starts_the_admit_hook_in_incoming_mode(bb, launched, client):
    bb.answers = [_link_answer()]
    resp = client.post("/ft_answer", params={"uuid": UUID}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (200, {"link": LINK})
    call, = bb.calls
    assert (call.method, call.path, call.param_keys) == ("POST", f"/api/v1/facetime/answer/{UUID}", ["password"])
    assert call.timeout == 90
    assert launched == [(LINK, True)]


def test_ft_link_returns_the_link_and_starts_the_admit_hook_in_outbound_mode(bb, launched, client):
    bb.answers = [_link_answer()]
    resp = client.post("/ft_link", headers=AUTH)
    assert (resp.status_code, resp.json()) == (200, {"link": LINK})
    call, = bb.calls
    assert (call.method, call.path, call.param_keys) == ("POST", "/api/v1/facetime/session", ["password"])
    assert call.timeout == 90
    assert launched == [(LINK, False)]


def test_ft_decline_answers_ok(bb, launched, client):
    bb.answers = [FakeResponse(200, {"status": 200})]
    resp = client.post("/ft_decline", params={"uuid": UUID}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (200, {"ok": True})
    call, = bb.calls
    assert (call.method, call.path, call.param_keys) == ("POST", f"/api/v1/facetime/leave/{UUID}", ["password"])
    assert call.timeout == 30
    assert launched == []


# ---------------------------------------------------------------------------
# the existing failures, unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [{"status": 200, "data": {}}, {"status": 200}, {}, None])
def test_a_2xx_without_a_link_is_a_502(bb, launched, client, body):
    bb.answers = [FakeResponse(200, body), FakeResponse(200, body)]
    for path, params in (("/ft_answer", {"uuid": UUID}), ("/ft_link", {})):
        resp = client.post(path, params=params, headers=AUTH)
        assert (resp.status_code, resp.json()) == (502, {"detail": "BlueBubbles returned no link"}), path
    assert launched == []


@pytest.mark.parametrize("status", [400, 404, 500])
def test_a_bluebubbles_http_error_passes_through_with_its_status_and_body(bb, launched, client, status):
    text = f"synthetic BlueBubbles answer {status}"
    bb.answers = [FakeResponse(status, text=text) for _ in range(3)]
    for path, params in (("/ft_answer", {"uuid": UUID}), ("/ft_decline", {"uuid": UUID}), ("/ft_link", {})):
        resp = client.post(path, params=params, headers=AUTH)
        assert (resp.status_code, resp.json()) == (status, {"detail": text}), path
    assert launched == []


def test_uuid_is_required_and_the_token_too(bb, launched, client):
    assert client.post("/ft_answer", headers=AUTH).status_code == 422
    assert client.post("/ft_decline", headers=AUTH).status_code == 422
    for path, params in (("/ft_answer", {"uuid": UUID}), ("/ft_decline", {"uuid": UUID}), ("/ft_link", {})):
        assert client.post(path, params=params).status_code == 401, path
        assert client.post(path, params=params, headers={"X-Imsg-Token": "wrong"}).status_code == 401, path
    assert bb.calls == [] and launched == []


def test_without_bluebubbles_every_route_is_a_501(r, bb, launched, client, monkeypatch):
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    for path, params in (("/ft_answer", {"uuid": UUID}), ("/ft_decline", {"uuid": UUID}), ("/ft_link", {})):
        resp = client.post(path, params=params, headers=AUTH)
        assert (resp.status_code, resp.json()) == \
            (501, {"detail": "no configured engine can handle FaceTime"}), path
    assert bb.calls == [] and launched == []


# ---------------------------------------------------------------------------
# step R5: a transport error is a 502 with a short detail, not a bare 500
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error", TRANSPORT_ERRORS, ids=lambda e: type(e).__name__)
def test_bluebubbles_unreachable_is_a_502_on_every_route(bb, launched, client, error):
    bb.answers = [error, error, error]
    for path, params in (("/ft_answer", {"uuid": UUID}), ("/ft_decline", {"uuid": UUID}), ("/ft_link", {})):
        resp = client.post(path, params=params, headers=AUTH)      # the test client re-raises a 500
        assert resp.status_code == 502, path
        assert resp.json() == {"detail": "BlueBubbles unreachable"}, path
        assert "SECRET-DETAIL" not in resp.text and "synthetic" not in resp.text
    assert len(bb.calls) == 3 and launched == []


def test_the_bridge_raises_an_engine_error_for_a_transport_failure():
    assert UNREACHABLE == "BlueBubbles unreachable"                 # frozen: the app may show it
    # the last three are not httpx errors: what a port above 65535 in BB_URL
    # raises inside the event loop, and two plain ones
    out_of_range_port = ExceptionGroup("unhandled errors in a TaskGroup",
                                       [OverflowError("connect(): port must be 0-65535. SECRET-DETAIL")])
    for error in (*TRANSPORT_ERRORS, httpx.InvalidURL("synthetic bad BB_URL"),
                  out_of_range_port, OSError("synthetic: SECRET-DETAIL"), RuntimeError("synthetic: SECRET-DETAIL")):
        def handler(request: httpx.Request, error=error) -> httpx.Response:
            raise error

        engine = BlueBubblesEngine("http://bb.invalid:1234", "stub-bb-password-not-real",
                                   transport=httpx.MockTransport(handler))
        for call in (engine.facetime.answer(UUID), engine.facetime.leave(UUID), engine.facetime.new_link()):
            with pytest.raises(EngineError) as info:
                asyncio.run(call)
            e = info.value
            assert (e.detail, e.status, e.body) == (UNREACHABLE, None, None)
            assert e.__cause__ is error                              # kept for the relay's log line
            assert "SECRET-DETAIL" not in str(e) and "stub-bb-password-not-real" not in str(e)


# ---------------------------------------------------------------------------
# step R5: the call id is one path segment, whatever the client sends
# ---------------------------------------------------------------------------

#: Ids that are not a call: path navigation, separators, a query, a fragment, a line break.
HOSTILE_IDS = ("../../message/text", "..", ".", "...", "a/b", "a/../../chat/new", "a?x=1", "a#frag",
               "a\nb", "a b", "%2e%2e", "..%2F..%2Fmessage%2Ftext", "-leading-hyphen", "x" * 129)
#: Ids the routes accept: BlueBubbles reports a call by UUID.
CALL_IDS = ("A1B2C3D4-0000-4000-8000-ABCDEF012345", "a1b2c3d4-0000-4000-8000-abcdef012345",
            UUID, "call_1", "v1.call-2", "7", "x" * 128)


def _bridge(seen: list) -> BlueBubblesEngine:
    """A real engine over ``httpx.MockTransport``: ``seen`` gets the method and
    the path exactly as ``httpx`` would put it on the wire."""
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode().split("?", 1)[0]))
        return httpx.Response(200, json={"status": 200, "data": {"link": LINK}})

    return BlueBubblesEngine("http://bb.invalid:1234", "stub-bb-password-not-real",
                             transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("uuid", HOSTILE_IDS)
def test_no_call_id_reaches_a_path_outside_the_facetime_endpoints(uuid):
    """The engine, called directly (what the pre-R5 relay's routes do, having no
    check of their own). Either nothing is sent, or what is sent is exactly
    ``/api/v1/facetime/<verb>/<one segment>``."""
    seen: list = []
    engine = _bridge(seen)
    for verb, call in (("answer", engine.facetime.answer), ("leave", engine.facetime.leave)):
        seen.clear()
        if not uuid.strip("."):
            with pytest.raises(EngineError) as info:
                asyncio.run(call(uuid))
            assert (info.value.detail, info.value.status) == (BAD_CALL_ID, 400)
            assert seen == []                                       # refused before anything is sent
            continue
        asyncio.run(call(uuid))
        (method, path), = seen
        prefix = f"/api/v1/facetime/{verb}/"
        assert method == "POST" and path.startswith(prefix), path
        segment = path[len(prefix):]
        assert segment and "/" not in segment and segment not in (".", ".."), path
        assert "message" not in path.split("/")[1:4] and "chat" not in path.split("/")[1:4]


def test_a_real_call_id_goes_into_the_path_unchanged():
    assert BAD_CALL_ID == "invalid FaceTime call id"
    for uuid in CALL_IDS:
        assert _call_segment(uuid) == uuid
        seen: list = []
        asyncio.run(_bridge(seen).facetime.leave(uuid))
        assert seen == [("POST", f"/api/v1/facetime/leave/{uuid}")]
    assert _call_segment("../../message/text") == "..%2F..%2Fmessage%2Ftext"
    for dots in ("", ".", "..", "..."):
        with pytest.raises(EngineError):
            _call_segment(dots)


@pytest.mark.parametrize("uuid", HOSTILE_IDS)
def test_the_routes_refuse_an_id_that_is_not_a_call_id(r5, bb, launched, client, capsys, uuid):
    for path in ("/ft_answer", "/ft_decline"):
        resp = client.post(path, params={"uuid": uuid}, headers=AUTH)
        assert (resp.status_code, resp.json()) == (422, {"detail": "uuid is not a FaceTime call id"}), path
    assert bb.calls == [] and launched == []                        # BlueBubbles was never asked
    assert capsys.readouterr().out == ""                            # and the id never reached a log line


@pytest.mark.parametrize("uuid", CALL_IDS)
def test_the_routes_accept_what_a_call_id_looks_like(r5, bb, launched, client, uuid):
    bb.answers = [FakeResponse(200, {"status": 200})]
    resp = client.post("/ft_decline", params={"uuid": uuid}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (200, {"ok": True})
    assert bb.calls[0].path == f"/api/v1/facetime/leave/{uuid}"


def test_the_route_check_comes_after_the_token_and_before_the_engine(r5, bb, launched, client, monkeypatch):
    assert client.post("/ft_decline", params={"uuid": ".."}).status_code == 401          # auth first
    assert client.post("/ft_decline", params={"uuid": ""}, headers=AUTH).status_code == 422
    monkeypatch.setattr(r5, "BB_PASSWORD", "")                                           # no engine at all
    assert client.post("/ft_decline", params={"uuid": ".."}, headers=AUTH).status_code == 422
    assert client.post("/ft_decline", params={"uuid": UUID}, headers=AUTH).status_code == 501
    assert bb.calls == []


# ---------------------------------------------------------------------------
# step R5: a 2xx answer that is not BlueBubbles' JSON is a 502, not a bare 500
# ---------------------------------------------------------------------------

class NotJson(FakeResponse):
    """A 2xx whose body does not parse (an HTML page from another service)."""

    def __init__(self, text: str = "<html>synthetic page, not BlueBubbles</html>"):
        super().__init__(200, text=text)

    def json(self):
        import json
        return json.loads(self.text)


def _unexpected_answers() -> list:
    return [NotJson(), FakeResponse(200, {"data": "x"}), FakeResponse(200, ["a", "list"]),
            FakeResponse(200, "a string"), FakeResponse(200, {"data": {"link": 5}}),
            FakeResponse(200, {"data": ["not", "an", "object"]})]


def test_an_unexpected_2xx_answer_is_a_502_with_a_short_detail(bb, launched, client):
    assert UNEXPECTED == "BlueBubbles returned an unexpected answer"
    for path, params in (("/ft_answer", {"uuid": UUID}), ("/ft_link", {})):
        bb.answers = _unexpected_answers()
        for _ in range(len(bb.answers)):
            resp = client.post(path, params=params, headers=AUTH)          # the test client re-raises a 500
            assert (resp.status_code, resp.json()) == (502, {"detail": UNEXPECTED}), path
            assert "synthetic page" not in resp.text
    assert launched == []
    # declining does not read the body: any 2xx is a success, as before
    bb.answers = [NotJson()]
    resp = client.post("/ft_decline", params={"uuid": UUID}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (200, {"ok": True})


def test_the_bridge_reads_a_real_non_json_response_as_unexpected():
    for response in (httpx.Response(200, text="<html>not BlueBubbles</html>"),
                     httpx.Response(200, content=b""),
                     httpx.Response(200, json={"data": "x"}),
                     httpx.Response(200, json=[1, 2])):
        engine = BlueBubblesEngine("http://bb.invalid:1234", "stub-bb-password-not-real",
                                   transport=httpx.MockTransport(lambda request, r=response: r))
        for call in (engine.facetime.answer(UUID), engine.facetime.new_link()):
            with pytest.raises(EngineError) as info:
                asyncio.run(call)
            assert (info.value.detail, info.value.status, info.value.body) == (UNEXPECTED, None, None)
    # ... and the shapes that only lack a link stay "no link" (None), as before
    import json
    for body in ({"status": 200, "data": {}}, {"status": 200}, {}, None, {"data": None}, {"data": {"link": ""}}):
        raw = json.dumps(body).encode()                              # None is the JSON document `null`
        engine = BlueBubblesEngine("http://bb.invalid:1234", "stub-bb-password-not-real",
                                   transport=httpx.MockTransport(lambda request, c=raw: httpx.Response(200, content=c)))
        assert not asyncio.run(engine.facetime.answer(UUID))
        assert not asyncio.run(engine.facetime.new_link())


def test_an_unexpected_answer_is_logged_with_the_error_class_only(r5, bb, launched, client, capsys):
    bb.answers = [NotJson("<html>SECRET-DETAIL</html>"), FakeResponse(200, {"data": "SECRET-DETAIL"})]
    client.post("/ft_answer", params={"uuid": UUID}, headers=AUTH)
    client.post("/ft_link", headers=AUTH)
    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"[facetime] answer {UUID} failed: BlueBubbles returned an unexpected answer (JSONDecodeError)",
        "[facetime] link failed: BlueBubbles returned an unexpected answer (AttributeError)",
    ]
    assert "SECRET-DETAIL" not in out


def test_a_failure_is_logged_once_with_the_error_class_and_no_exception_text(r, bb, launched, client, capsys):
    if not has_r5(r):
        pytest.skip("the [facetime] ... failed lines for decline/link and the error class are R5")
    bb.answers = [httpx.ConnectError("synthetic refused: SECRET-DETAIL"),
                  httpx.ReadTimeout("synthetic read timeout: SECRET-DETAIL"),
                  FakeResponse(500, text="synthetic BlueBubbles answer")]
    client.post("/ft_answer", params={"uuid": UUID}, headers=AUTH)
    client.post("/ft_link", headers=AUTH)
    client.post("/ft_decline", params={"uuid": UUID}, headers=AUTH)
    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"[facetime] answer {UUID} failed: BlueBubbles unreachable (ConnectError)",
        "[facetime] link failed: BlueBubbles unreachable (ReadTimeout)",
        f"[facetime] decline {UUID} failed: HTTP 500: synthetic BlueBubbles answer",
    ]
    assert "SECRET-DETAIL" not in out and RELAY_STUB_ENV["BB_PASSWORD"] not in out
