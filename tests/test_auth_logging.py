"""Token out of the logs (plan 2026-10-06, step R4; KB item 18).

The relay keeps ACCEPTING ``?token=`` on every route the middleware guards,
on ``/bb_event`` (BlueBubbles registers its webhook URL with it and cannot
set headers) and on the WebSocket upgrade. What changes is what gets written
down:

* ``mask_token`` rewrites the value of every ``token=`` / ``*_token=`` query
  parameter to ``***`` and leaves everything else alone;
* ``TokenMaskFilter`` does that to a log record's message and args, which is
  where uvicorn's access line (``'%s - "%s %s HTTP/%s" %d'`` with the path +
  query as an arg) and its ``"WebSocket %s" [accepted]`` line carry the URL;
* ``masked_log_config()`` is uvicorn's default config with that filter on
  every handler, and ``install_log_masking()`` puts it on the uvicorn loggers;
* ``MaskingStream`` masks anything print()ed through it (``__main__`` wraps
  ``sys.stdout`` / ``sys.stderr`` with it before the server starts);
* ``/bb_event`` and ``/ws`` still accept ``?token=`` (and the header), and
  still reject a wrong one.

Step R5 (2026-10-07):

* ``MaskingStream`` is line-buffered and forwards ``flush()``: under launchd
  stdout is a file, and the startup doctor table used to sit in Python's
  buffer until the first request. Checked on a recording stream and over a
  real pipe from a child process.
* a rejected WebSocket is an **HTTP 403 at the handshake** for a real client.
  The handler closes with code 1008 before accepting, which the in-process
  test client reports as ``WebSocketDisconnect(1008)``; over a socket uvicorn
  answers the upgrade request with 403 and no WebSocket ever opens. One test
  runs the app under uvicorn on a loopback port to pin exactly that.
* the same masking covers two more shapes: ``password=<value>`` (how the
  BlueBubbles password travels; no relay line prints it, but an upstream
  error body quoted in a log line could), and the WHOLE query string of the
  voice routes (``/v/prepare?q=...``), which is where an automation app that
  calls them by GET puts the dictated message.

The token used here is the conftest's placeholder, never the real one.
"""

from __future__ import annotations

import io
import logging
import logging.config
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.logging import AccessFormatter

from tests.conftest import RELAY_STUB_ENV, RELAY_STUB_SELF

STUB_TOKEN = RELAY_STUB_ENV["IMSG_TOKEN"]
SECRET = "synthetic-secret-value-0123"      # a placeholder that must never survive masking


@pytest.fixture
def r(relay_module):
    """The relay under test; skips when it predates the log masking (pre-R4)."""
    mod = relay_module.module
    if not hasattr(mod, "mask_token"):
        pytest.skip(f"{relay_module.name} has no token masking (pre-R4)")
    return mod


@pytest.fixture
def client(relay_module):
    """ASGI test client; no ``with``, so startup hooks never run."""
    return TestClient(relay_module.module.app)


# ---------------------------------------------------------------------------
# mask_token
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, masked", [
    (f"/ws?token={SECRET}", "/ws?token=***"),
    (f"/bb_event?token={SECRET}", "/bb_event?token=***"),
    (f"/map?token={SECRET}&x=1", "/map?token=***&x=1"),
    (f"/locations?x=1&token={SECRET}", "/locations?x=1&token=***"),
    (f"/a?TOKEN={SECRET}", "/a?TOKEN=***"),                        # case-insensitive
    (f"/a?access_token={SECRET}&b=2", "/a?access_token=***&b=2"),  # any *token= param
    (f'GET "/ws?token={SECRET}" 101', 'GET "/ws?token=***" 101'),  # quoted request line
    (f"https://relay.example/ws?token={SECRET}#frag", "https://relay.example/ws?token=***#frag"),
    (f"first /a?token={SECRET} then /b?token={SECRET}", "first /a?token=*** then /b?token=***"),
    ("/attachment/abc?f=jpg", "/attachment/abc?f=jpg"),            # nothing to mask
    ("/health?nonce=abc", "/health?nonce=abc"),
    ("token=", "token="),                                          # empty value stays
    ("registered device token (2 total)", "registered device token (2 total)"),
    ("", ""),
])
def test_mask_token(r, raw, masked):
    assert r.mask_token(raw) == masked
    assert SECRET not in r.mask_token(raw)


def test_mask_token_leaves_non_strings_alone(r):
    for v in (None, 7, 1.5, b"token=abc", ["token=abc"]):
        assert r.mask_token(v) is v


# A dictated sentence as it travels in a query string. Synthetic and distinctive.
DICTATED = "text%20Quokka%20the%20door%20code%20is%204821-ZEBRA"
DICTATED_WORDS = ("Quokka", "4821", "ZEBRA", "door")


@pytest.mark.parametrize("raw, masked", [
    # password=: the BlueBubbles password's query parameter
    (f"/api/v1/message/text?password={SECRET}", "/api/v1/message/text?password=***"),
    (f"http://127.0.0.1:1234/api/v1/ping?password={SECRET}&x=1", "http://127.0.0.1:1234/api/v1/ping?password=***&x=1"),
    (f"/x?PASSWORD={SECRET}", "/x?PASSWORD=***"),
    (f'HTTP 500: {{"url": "/api/v1/contact?password={SECRET}"}}', 'HTTP 500: {"url": "/api/v1/contact?password=***"}'),
    (f"/x?password={SECRET}&token={SECRET}", "/x?password=***&token=***"),
    ("password set, server unreachable", "password set, server unreachable"),      # the doctor's words
    ("no password (AppleScript only), server unreachable", "no password (AppleScript only), server unreachable"),
    ("password=", "password="),
    # the voice routes: the whole query string, key names included
    (f"/v/prepare?q={DICTATED}", "/v/prepare?***"),
    (f"/v/prepare?query={DICTATED}&token={SECRET}", "/v/prepare?***"),
    (f"/v/prepare/?q={DICTATED}", "/v/prepare/?***"),                             # the form a slash redirect logs
    ("/v/confirm?a=yes%20Quokka", "/v/confirm?***"),
    (f"/assistant/prepare?query={DICTATED}", "/assistant/prepare?***"),
    ("/assistant/confirm?token=abc123&answer=Quokka", "/assistant/confirm?***"),
    (f"GET /v/prepare?q={DICTATED}&token={SECRET} HTTP/1.1", "GET /v/prepare?*** HTTP/1.1"),
    (f'10.0.0.2:5000 - "POST /assistant/prepare?query={DICTATED} HTTP/1.1" 200',
     '10.0.0.2:5000 - "POST /assistant/prepare?*** HTTP/1.1" 200'),
    # a quote or an ampersand inside the value does not end what is masked
    ("GET /v/prepare?q=Quokka's%20\"door\"&x=4821-ZEBRA HTTP/1.1", "GET /v/prepare?*** HTTP/1.1"),
    # nothing to mask: no query string, or another route
    ("/v/prepare", "/v/prepare"),
    ('"POST /v/confirm HTTP/1.1" 200', '"POST /v/confirm HTTP/1.1" 200'),
    ("/search?q=prepare", "/search?q=prepare"),
    ("/thread/x/messages?limit=50", "/thread/x/messages?limit=50"),
    ("/xv/prepare2?q=1", "/xv/prepare2?q=1"),
])
def test_mask_token_also_covers_passwords_and_the_voice_routes(r5, raw, masked):
    out = r5.mask_token(raw)
    assert out == masked
    assert SECRET not in out
    if "/prepare" in raw or "/confirm" in raw:
        for word in DICTATED_WORDS:
            assert word not in out, word


# ---------------------------------------------------------------------------
# the logging filter on uvicorn's record shapes
# ---------------------------------------------------------------------------

def _capture(logger_name: str, formatter: logging.Formatter, add_filter) -> tuple[logging.Logger, io.StringIO]:
    logger = logging.getLogger(logger_name)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(formatter)
    if add_filter is not None:
        handler.addFilter(add_filter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger, buf, handler


def test_filter_masks_uvicorn_http_access_line(r):
    # The exact shape uvicorn's httptools/h11 protocols emit on "uvicorn.access".
    fmt = AccessFormatter('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                          use_colors=False)
    logger, buf, handler = _capture("test.access.http", fmt, r.TokenMaskFilter())
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:5000", "POST",
                    f"/bb_event?token={SECRET}", "1.1", 200)
        logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:5000", "GET",
                    "/attachment/abc?f=jpg", "1.1", 200)
    finally:
        logger.removeHandler(handler)
    out = buf.getvalue()
    assert SECRET not in out
    assert '"POST /bb_event?token=*** HTTP/1.1" 200' in out
    assert '"GET /attachment/abc?f=jpg HTTP/1.1" 200' in out      # untouched


def test_filter_masks_the_dictated_text_in_a_voice_route_access_line(r5):
    """MacroDroid/Tasker call ``/v/prepare`` by GET with the sentence in the
    query string, and uvicorn's access record carries path + query as one
    argument. The line keeps the route and loses the query."""
    fmt = AccessFormatter('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                          use_colors=False)
    logger, buf, handler = _capture("test.access.voice", fmt, r5.TokenMaskFilter())
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:5000", "GET",
                    f"/v/prepare?q={DICTATED}&token={SECRET}", "1.1", 200)
        logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:5000", "GET", "/v/confirm?a=yes%20Quokka", "1.1", 200)
        logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:5000", "POST",
                    f"/assistant/prepare?query={DICTATED}", "1.1", 200)
        logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:5000", "POST", "/v/prepare", "1.1", 200)
        logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:5000", "GET", "/search?q=dentist", "1.1", 200)
    finally:
        logger.removeHandler(handler)
    out = buf.getvalue()
    assert out.count('"GET /v/prepare?*** HTTP/1.1" 200') == 1
    assert '"GET /v/confirm?*** HTTP/1.1" 200' in out
    assert '"POST /assistant/prepare?*** HTTP/1.1" 200' in out
    assert '"POST /v/prepare HTTP/1.1" 200' in out                 # a body-carrying POST logs only the path
    assert '"GET /search?q=dentist HTTP/1.1" 200' in out           # search terms are still logged (documented)
    assert SECRET not in out
    for word in DICTATED_WORDS:
        assert word not in out, word


def test_the_voice_routes_still_read_their_fields_from_the_query_string(r5, monkeypatch):
    """The masking is log-only: the route still gets the sentence."""
    seen = []
    monkeypatch.setattr(r5, "resolve_assistant", lambda q: (seen.append(q), ("not_found", [], ""))[1])
    client = TestClient(r5.app)
    resp = client.post("/v/prepare", params={"q": "text Quokka the door code is 4821-ZEBRA", "token": STUB_TOKEN})
    assert resp.status_code == 200 and seen == ["text Quokka the door code is 4821-ZEBRA"]


def test_filter_masks_uvicorn_websocket_lines(r):
    # websockets_impl logs these on "uvicorn.error", with the path+query as an arg.
    logger, buf, handler = _capture("test.access.ws", logging.Formatter("%(message)s"),
                                    r.TokenMaskFilter())
    try:
        logger.info('%s - "WebSocket %s" [accepted]', "10.0.0.2:5001", f"/ws?token={SECRET}")
        logger.info('%s - "WebSocket %s" 403', "10.0.0.2:5001", f"/ws?token={SECRET}x")
    finally:
        logger.removeHandler(handler)
    out = buf.getvalue()
    assert SECRET not in out
    assert '"WebSocket /ws?token=***" [accepted]' in out
    assert '"WebSocket /ws?token=***" 403' in out


def test_filter_masks_message_and_dict_args(r):
    f = r.TokenMaskFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1,
                            f"webhook registered at /bb_event?token={SECRET}", (), None)
    assert f.filter(rec) is True
    assert rec.getMessage() == "webhook registered at /bb_event?token=***"
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "url=%(u)s n=%(n)d",
                            ({"u": f"/ws?token={SECRET}", "n": 3},), None)
    f.filter(rec)
    assert rec.getMessage() == "url=/ws?token=*** n=3"


def test_filter_masks_a_secret_that_spans_the_format_string_and_an_argument(r5, capsys):
    """``"...token=%s"`` with the value as an argument: masking the format as
    text used to turn ``%s`` into ``***``, the record then failed to format,
    and logging's error report printed the raw arguments to stderr. Masking
    only the arguments misses it too (the value alone has no ``token=``). The
    filter formats first and masks the result."""
    logger, buf, handler = _capture("test.span", logging.Formatter("%(message)s"), r5.TokenMaskFilter())
    try:
        logger.info("webhook registered at %s?token=%s (attempt %d)", "/bb_event", SECRET, 2)
        logger.info("GET /api/v1/ping?password=%s -> %d", SECRET, 401)
        logger.info("literal %d%% and a /v/prepare?q=%s tail", 50, "Quokka-4821")
    finally:
        logger.removeHandler(handler)
    assert buf.getvalue() == ("webhook registered at /bb_event?token=*** (attempt 2)\n"
                              "GET /api/v1/ping?password=*** -> 401\n"
                              "literal 50% and a /v/prepare?*** tail\n")
    captured = capsys.readouterr()
    assert "Logging error" not in captured.err and SECRET not in captured.err + captured.out
    # the record itself: no raw value left in msg or args
    f = r5.TokenMaskFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s", (SECRET,), None)
    assert f.filter(rec) is True and f.filter(rec) is True           # idempotent (logger + handler level)
    assert (rec.msg, rec.args) == ("token=***", ())
    # a record that cannot format is left to logging, not raised from the filter
    bad = logging.LogRecord("x", logging.INFO, __file__, 1, "%d items", ("not a number",), None)
    assert f.filter(bad) is True


def test_install_log_masking_is_idempotent_and_covers_uvicorn_loggers(r):
    r.install_log_masking()
    r.install_log_masking()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, r.TokenMaskFilter)]
        assert len(filters) == 1, name


def test_masked_log_config_puts_the_filter_on_every_handler(r):
    cfg = r.masked_log_config()
    assert cfg["filters"]["mask_token"]["()"] is r.TokenMaskFilter
    assert cfg["handlers"], "uvicorn's default handlers expected"
    for name, handler in cfg["handlers"].items():
        assert "mask_token" in handler["filters"], name
    # the config is accepted by logging and the resulting handlers mask
    logging.config.dictConfig(cfg)
    try:
        buf = io.StringIO()
        access = logging.getLogger("uvicorn.access")
        for h in access.handlers:
            h.stream = buf
        access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:5000", "GET",
                    f"/map?token={SECRET}", "1.1", 200)
    finally:
        # back to the stock config so nothing else in the session inherits ours
        from uvicorn.config import LOGGING_CONFIG
        logging.config.dictConfig(LOGGING_CONFIG)
    assert SECRET not in buf.getvalue()
    assert "/map?token=*** HTTP/1.1" in buf.getvalue()


def test_masked_log_config_hides_voice_queries_and_passwords_on_uvicorns_own_handlers(r5):
    """The same end-to-end check as above, for the two shapes step R5 added:
    through the logging config ``uvicorn.run`` is given, on uvicorn's loggers."""
    logging.config.dictConfig(r5.masked_log_config())
    try:
        buf = io.StringIO()
        access, error = logging.getLogger("uvicorn.access"), logging.getLogger("uvicorn.error")
        for h in (*access.handlers, *error.handlers, *logging.getLogger("uvicorn").handlers):
            h.stream = buf
        access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.2:5000", "GET",
                    f"/v/prepare?query={DICTATED}", "1.1", 200)
        error.error("upstream said: GET /api/v1/contact?password=%s failed", SECRET)
    finally:
        from uvicorn.config import LOGGING_CONFIG
        logging.config.dictConfig(LOGGING_CONFIG)
    out = buf.getvalue()
    assert '"GET /v/prepare?*** HTTP/1.1" 200' in out and "password=***" in out
    assert SECRET not in out
    for word in DICTATED_WORDS:
        assert word not in out, word


# ---------------------------------------------------------------------------
# MaskingStream (what __main__ wraps sys.stdout / sys.stderr with)
# ---------------------------------------------------------------------------

def test_masking_stream_masks_writes_and_delegates_the_rest(r):
    raw = io.StringIO()
    s = r.MaskingStream(raw)
    print(f"[facetime] launched for https://x.example/ws?token={SECRET}", file=s)
    s.writelines([f"a?token={SECRET}\n", "plain\n"])
    s.flush()
    assert SECRET not in raw.getvalue()
    assert raw.getvalue() == ("[facetime] launched for https://x.example/ws?token=***\n"
                              "a?token=***\nplain\n")
    assert s.getvalue() == raw.getvalue()          # attribute delegation
    assert s.isatty() is False


def test_masking_stream_hides_voice_queries_and_passwords_too(r5):
    raw = io.StringIO()
    s = r5.MaskingStream(raw)
    print(f'INFO:     10.0.0.2:0 - "GET /v/prepare?q={DICTATED} HTTP/1.1" 200 OK', file=s)
    print(f"[contacts] BlueBubbles returned HTTP 500: GET /api/v1/contact?password={SECRET}", file=s)
    assert raw.getvalue() == ('INFO:     10.0.0.2:0 - "GET /v/prepare?*** HTTP/1.1" 200 OK\n'
                              "[contacts] BlueBubbles returned HTTP 500: GET /api/v1/contact?password=***\n")


class RecordingStream:
    """A raw stream that records what reaches it: ("write", text) / ("flush",)."""

    def __init__(self):
        self.events: list[tuple] = []

    def write(self, s):
        self.events.append(("write", s))
        return len(s)

    def flush(self):
        self.events.append(("flush",))


def test_masking_stream_flushes_every_line_and_forwards_flush(r5):
    raw = RecordingStream()
    s = r5.MaskingStream(raw)
    s.write("no newline yet")
    assert raw.events == [("write", "no newline yet")]                    # a partial line stays buffered
    s.write(f", now one: /ws?token={SECRET}\n")
    assert raw.events[-2:] == [("write", ", now one: /ws?token=***\n"), ("flush",)]
    raw.events.clear()
    print("[check] relay doctor", file=s)                                 # print() writes the text, then the newline
    assert raw.events == [("write", "[check] relay doctor"), ("write", "\n"), ("flush",)]
    raw.events.clear()
    print("a\nb\nc", file=s, flush=True)                                   # the table is one multi-line print
    assert raw.events == [("write", "a\nb\nc"), ("flush",), ("write", "\n"), ("flush",), ("flush",)]
    raw.events.clear()
    s.flush()                                                             # explicit flush reaches the raw stream
    assert raw.events == [("flush",)]
    raw.events.clear()
    s.writelines(["one\n", "two"])
    assert raw.events == [("write", "one\n"), ("flush",), ("write", "two")]


def test_masking_stream_can_be_left_block_buffered_and_survives_a_stream_without_flush(r5):
    raw = RecordingStream()
    s = r5.MaskingStream(raw, line_buffered=False)
    print("a line", file=s)
    assert ("flush",) not in raw.events
    s.flush()
    assert raw.events[-1] == ("flush",)

    class WriteOnly:
        def __init__(self):
            self.text = ""

        def write(self, text):
            self.text += text
            return len(text)

    bare = WriteOnly()
    s = r5.MaskingStream(bare)
    print(f"x?token={SECRET}", file=s)                                    # must not raise for want of flush()
    s.flush()
    assert bare.text == "x?token=***\n"


def test_a_line_printed_through_the_wrapper_reaches_a_pipe_while_the_process_lives(r5, relay_module, tmp_path):
    """The launchd symptom, reproduced with a pipe: stdout that is not a
    terminal is block-buffered, so a short line stays in the process until it
    exits or the buffer fills. Through ``MaskingStream`` the line arrives at
    once: the child prints it and then blocks on stdin, and the parent reads
    it while the child is still running. (The lines the module printed at
    import, before the wrapper existed, come out with it, in order.)"""
    relay_dir = Path(relay_module.module.__file__).resolve().parent
    code = ("import sys, types; m = types.ModuleType('dotenv'); "
            "m.load_dotenv = lambda *a, **k: False; sys.modules['dotenv'] = m; "
            f"import {relay_module.name} as relay; "
            "sys.stdout = relay.MaskingStream(sys.stdout); "
            "print('MARKER doctor table would be here /x?token=synthetic-secret-value-0123'); "
            "sys.stdin.readline()")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ["HOME"],
           **RELAY_STUB_ENV, "IMSG_SELF": RELAY_STUB_SELF,
           "IMSG_CHATDB": str(tmp_path / "placeholder-chat.db"),
           "IMSG_STATE": str(tmp_path / "relay_state.json")}                # no PYTHONUNBUFFERED
    p = subprocess.Popen([sys.executable, "-c", code], cwd=relay_dir, env=env, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    seen: list[str] = []

    def read_until_marker():
        for line in p.stdout:
            seen.append(line)
            if line.startswith("MARKER"):
                return

    reader = threading.Thread(target=read_until_marker, daemon=True)
    reader.start()
    try:
        reader.join(timeout=45)
        alive_when_read = p.poll() is None
        assert not reader.is_alive(), f"no line arrived while the child was running: {seen!r}"
        assert alive_when_read, "the child had already exited, so this proved nothing"
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=15)
        err = p.stderr.read()
        p.stdout.close()
        p.stderr.close()
    assert p.returncode == 0, err[-1500:]
    assert seen[-1] == "MARKER doctor table would be here /x?token=***\n"   # masked on the way
    assert any(line.startswith("[auth] token required") for line in seen[:-1])   # the import's own lines, flushed with it


def test_launch_autoadmit_print_is_masked(r, monkeypatch, tmp_path, capsys):
    launched = []
    monkeypatch.setattr(r.subprocess, "Popen", lambda *a, **k: launched.append((a, k)))
    monkeypatch.setattr(r, "FT_AUTO_LOG", tmp_path / "ft-auto.log")
    monkeypatch.setattr(r, "FT_ADMIT_APP", tmp_path, raising=False)   # helper app "present"
    monkeypatch.delenv("FT_AUTOADMIT", raising=False)
    r._launch_autoadmit(f"https://facetime.example/join?token={SECRET}")
    out = capsys.readouterr().out
    assert launched and SECRET not in out
    assert "[facetime] auto-admit (outbound) launched for https://facetime.example/join?token=***" in out


# ---------------------------------------------------------------------------
# ?token= keeps working where it must
# ---------------------------------------------------------------------------

def test_bb_event_accepts_query_token_and_header(r, client):
    body = {"type": "something-else"}                     # acknowledged and ignored
    assert client.post("/bb_event", params={"token": STUB_TOKEN}, json=body).json() == {"ok": True}
    assert client.post("/bb_event", headers={"X-Imsg-Token": STUB_TOKEN}, json=body).json() == {"ok": True}
    assert client.post("/bb_event", json=body).status_code == 401
    assert client.post("/bb_event", params={"token": STUB_TOKEN + "x"}, json=body).status_code == 401


def test_websocket_accepts_query_token_and_header(r, client):
    with client.websocket_connect(f"/ws?token={STUB_TOKEN}") as ws:
        assert ws in r.hub.clients or len(r.hub.clients) >= 1
    with client.websocket_connect("/ws", headers={"X-Imsg-Token": STUB_TOKEN}):
        pass
    for bad in ("/ws", f"/ws?token={STUB_TOKEN}x", "/ws?token="):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(bad):
                pass
        # 1008 is the code in the ASGI close message, which is all the
        # in-process client sees; a real client gets an HTTP 403 (next test).
        assert exc.value.code == 1008, bad


@pytest.mark.filterwarnings("ignore::DeprecationWarning")      # uvicorn's own use of websockets.legacy
def test_a_rejected_websocket_is_an_http_403_at_the_handshake(r):
    """Over a real socket. The handler calls ``ws.close(code=1008)`` BEFORE
    accepting; uvicorn turns that into ``HTTP/1.1 403 Forbidden`` on the
    upgrade request. No WebSocket is opened, so no close frame and no 1008
    ever reach the client. Loopback only, an ephemeral port, ``lifespan="off"``
    (the relay's startup hooks never run), no logging reconfigured."""
    import uvicorn
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    server = uvicorn.Server(uvicorn.Config(r.app, host="127.0.0.1", port=0, lifespan="off",
                                           log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 20
        while not server.started and thread.is_alive() and time.time() < deadline:
            time.sleep(0.02)
        assert server.started, "uvicorn did not start on a loopback port"
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"ws://127.0.0.1:{port}"
        for bad in ("/ws", "/ws?token=not-the-token", "/ws?token="):
            with pytest.raises(InvalidStatus) as exc:
                with connect(base + bad, open_timeout=10):
                    pass
            assert exc.value.response.status_code == 403, bad
        with pytest.raises(InvalidStatus) as exc:
            with connect(base + "/ws", additional_headers={"X-Imsg-Token": "not-the-token"}, open_timeout=10):
                pass
        assert exc.value.response.status_code == 403
        # the right token, either way, completes the upgrade
        with connect(base + "/ws", additional_headers={"X-Imsg-Token": STUB_TOKEN}, open_timeout=10):
            pass
        with connect(f"{base}/ws?token={STUB_TOKEN}", open_timeout=10):
            pass
    finally:
        server.should_exit = True
        thread.join(timeout=20)
    assert not thread.is_alive()


def test_middleware_still_accepts_query_token_everywhere_else(r, client):
    assert client.get("/contacts", params={"token": STUB_TOKEN}).status_code == 200
    assert client.get("/contacts", params={"token": "nope"}).status_code == 401
