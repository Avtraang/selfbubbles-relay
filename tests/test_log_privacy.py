"""No relay log line carries a message's text (step R5, 2026-10-07).

Chat identifiers, recipient addresses, contact names, file names and search
terms are still logged (docs/privacy-and-logs.md lists every line); message
TEXT is not. Three places used to print it:

* ``engines.applescript._applescript``: ``[fallback] osascript error: <e>``
  printed the exception, and ``str()`` of a ``subprocess.TimeoutExpired`` is
  the whole command line, i.e. the chat guid and the message text (or the
  staged file's path). ``[fallback] osascript failed: <stderr>`` printed 200
  characters of Messages' own error text, which quotes the chat guid
  (``Can't get chat id "..."``). Now: the exception CLASS, or the return code
  and the first line of stderr with quoted strings and arguments removed.
* ``engines.chain.deliver``: ``[send] <engine> failed (<detail>)`` printed up
  to 200 characters of the upstream's error body. BlueBubbles (1.9.9) answers
  a failed send with the failed message itself under ``data``. Now: the status
  and the JSON body without ``data``. The HTTP answer to the client keeps the
  full detail; only the log line changed.
* the voice endpoints: ``[assist] '<dictated text>' -> <name>`` and
  ``[assist] send failed: <whole upstream body>``. Now: the length of the
  text, and the HTTP status.

Two more, from the review of that step:

* the AppleScript line again: Messages reports a file it cannot open in HFS
  spelling and outside any quotes (``File Macintosh HD:Users:<you>:...:<name>
  wasn't found``), which the search for the POSIX path did not find. The path
  is searched for in that spelling too, and in both Unicode normal forms.
* ``beeper.send``: ``[beeper] send failed HTTP <status>: <200 characters of
  Beeper's answer>``. Whether Beeper repeats the message there is not known.
  Now: the status and, when the answer is JSON with a short ``code``, that
  code; nothing else of the body.

(What uvicorn's access log records for the voice routes' query strings is in
``tests/test_auth_logging.py``.)

Every value here is synthetic and distinctive, so a leak cannot hide.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import unicodedata

import httpx
import pytest
from fastapi.testclient import TestClient

import engines.applescript as osa
from engines import Capability, DeliveryError, SendResult, deliver
from engines.applescript import FAILED as AS_FAILED
from engines.applescript import AppleScriptEngine, _AS_FILE, _AS_TEXT, _failure_words, _hfs_form
from engines.bluebubbles import BlueBubblesEngine
from engines.chain import LOG_BODY_CHARS, _log_detail
from tests.compat_fixture import ALICE_PHONE, NOBODY_PHONE
from tests.conftest import RELAY_STUB_ENV
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)

AUTH = {"X-Imsg-Token": RELAY_STUB_ENV["IMSG_TOKEN"]}

GUID = "iMessage;-;+15550004242"                              # a synthetic one-to-one chat
ANY_GUID = "any;-;quokka.sender@example.invalid"
TEXT = 'ZEBRA-QUOKKA-7731 meet "at the pier" at 9\\30, bring the BLUE-HERON folder'
STAGED = "/Users/synthetic/Library/Messages/RelayOutbox/1700000000000-OKAPI-PLAN-5519.pdf"
SECRETS = (GUID, "+15550004242", TEXT, "ZEBRA-QUOKKA-7731", "BLUE-HERON", "at the pier",
           "quokka.sender", STAGED, "OKAPI-PLAN-5519")


def sync(coro):
    return asyncio.run(coro)


def assert_clean(captured: str) -> None:
    for secret in SECRETS:
        assert secret not in captured, secret


# ---------------------------------------------------------------------------
# osascript: a timeout, another exception, a non-zero exit
# ---------------------------------------------------------------------------

def test_osascript_timeout_logs_the_class_and_nothing_of_the_command(monkeypatch, capsys):
    seen = []

    def timing_out(cmd, **kw):
        seen.append(cmd)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout"))

    monkeypatch.setattr(osa.subprocess, "run", timing_out)
    assert osa._applescript(_AS_TEXT, GUID, TEXT) is False
    assert osa._applescript(_AS_FILE, GUID, STAGED) is False
    # the exception really did carry both: this is what used to be printed
    leaked = str(subprocess.TimeoutExpired(cmd=seen[0], timeout=20))
    assert GUID in leaked and "ZEBRA-QUOKKA-7731" in leaked
    out, err = capsys.readouterr()
    assert out == "[fallback] osascript error: TimeoutExpired\n" * 2
    assert_clean(out + err)
    assert seen[0] == ["osascript", "-e", _AS_TEXT, GUID, TEXT]         # the call itself is unchanged


def test_osascript_other_exceptions_log_the_class_only(monkeypatch, capsys):
    def broken(cmd, **kw):
        raise OSError(f"synthetic failure while running {cmd!r}")

    monkeypatch.setattr(osa.subprocess, "run", broken)
    assert osa._applescript(_AS_TEXT, GUID, TEXT) is False
    out, err = capsys.readouterr()
    assert out == "[fallback] osascript error: OSError\n"
    assert_clean(out + err)


def _completed(rc: int, stderr: str):
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, stdout="", stderr=stderr)


def test_osascript_failure_logs_rc_and_the_first_line_without_arguments(monkeypatch, capsys):
    stderr = (f'63:120: execution error: Messages got an error: Can’t get chat id "{GUID}". (-1728)\n'
              f"a second line that repeats {GUID} and {TEXT}\n")
    monkeypatch.setattr(osa.subprocess, "run", _completed(1, stderr))
    assert osa._applescript(_AS_TEXT, GUID, TEXT) is False
    out, err = capsys.readouterr()
    assert out == ('[fallback] osascript failed: rc=1 63:120: execution error: Messages got an error: '
                   'Can’t get chat id "...". (-1728)\n')
    assert_clean(out + err)


@pytest.mark.parametrize("stderr", [
    # the text quoted with AppleScript's own escaping
    '0:9: execution error: Can’t make "ZEBRA-QUOKKA-7731 meet \\"at the pier\\" at 9\\\\30, '
    'bring the BLUE-HERON folder" into type text. (-1700)',
    # a quoted value that does not close on the first line
    f'0:9: execution error: Can’t make "{TEXT[:40]}',
    # the arguments bare, outside any quotes
    f"execution error: {GUID} would not take {TEXT} (-1)",
    # the consent error names nothing, and survives whole
    "execution error: Not authorized to send Apple events to Messages. (-1743)",
])
def test_osascript_failure_line_never_quotes_the_guid_or_the_text(monkeypatch, capsys, stderr):
    monkeypatch.setattr(osa.subprocess, "run", _completed(1, stderr))
    assert osa._applescript(_AS_TEXT, GUID, TEXT) is False
    out, err = capsys.readouterr()
    assert out.startswith("[fallback] osascript failed: rc=1 ") and out.count("\n") == 1
    assert_clean(out + err)
    if "-1743" in stderr:
        assert out == f"[fallback] osascript failed: rc=1 {stderr}\n"


def test_osascript_file_failure_hides_the_staged_path_and_file_name(monkeypatch, capsys):
    hfs = STAGED.strip("/").replace("/", ":")
    for stderr in (f"execution error: File {STAGED} wasn’t found. (-43)",
                   f'execution error: Can’t get alias "Macintosh HD:{hfs}". (-43)',
                   "execution error: 1700000000000-OKAPI-PLAN-5519.pdf is not readable (-43)"):
        monkeypatch.setattr(osa.subprocess, "run", _completed(1, stderr))
        assert osa._applescript(_AS_FILE, GUID, STAGED) is False
        out, err = capsys.readouterr()
        assert "(-43)" in out
        assert_clean(out + err)


def test_osascript_file_failure_hides_an_unquoted_hfs_path(monkeypatch, capsys):
    """Messages' own wording for a file it cannot open: the path in HFS
    spelling, volume name in front, NO quotes around it. The user's directory
    and the file name must go, the wording and the number stay."""
    hfs = "Users:synthetic:Library:Messages:RelayOutbox:1700000000000-OKAPI-PLAN-5519.pdf"
    assert _hfs_form(STAGED) == hfs
    stderr = f"0:86: execution error: File Macintosh HD:{hfs} wasn’t found. (-43)"
    monkeypatch.setattr(osa.subprocess, "run", _completed(1, stderr))
    assert osa._applescript(_AS_FILE, GUID, STAGED) is False
    out, err = capsys.readouterr()
    assert out == "[fallback] osascript failed: rc=1 0:86: execution error: File Macintosh HD:<arg> wasn’t found. (-43)\n"
    assert_clean(out + err)
    assert "synthetic" not in out and "RelayOutbox" not in out


def test_hfs_form_and_unicode_normal_forms():
    # ":" and "/" swap between the two spellings; the volume name is not part of it
    assert _hfs_form("/Users/synthetic/Outbox/a:b.pdf") == "Users:synthetic:Outbox:a/b.pdf"
    assert _hfs_form("Users/synthetic/x") == "Users:synthetic:x"
    # a name with a colon, as AppleScript shows it (a slash), bare and inside the HFS path
    staged = "/Users/synthetic/Library/Messages/RelayOutbox/1700000000000-QUOKKA:NOTES.txt"
    for stderr in ("execution error: File Macintosh HD:Users:synthetic:Library:Messages:RelayOutbox:"
                   "1700000000000-QUOKKA/NOTES.txt wasn’t found. (-43)",
                   "execution error: 1700000000000-QUOKKA/NOTES.txt is not readable (-43)"):
        words = _failure_words(stderr, (GUID, staged))
        assert "QUOKKA" not in words and "synthetic" not in words and "(-43)" in words, words
    # macOS hands file names back decomposed (NFD); the argument was composed (NFC), and the reverse
    composed = "/Users/synthetic/Library/Messages/RelayOutbox/1700000000000-CAF\u00c9-OKAPI.pdf"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    for arg, shown in ((composed, decomposed), (decomposed, composed)):
        for stderr in (f"execution error: File {shown} wasn’t found. (-43)",
                       f"execution error: File Macintosh HD:{_hfs_form(shown)} wasn’t found. (-43)",
                       f"execution error: {shown.rsplit('/', 1)[-1]} is not readable (-43)"):
            words = _failure_words(stderr, (GUID, arg))
            assert "OKAPI" not in words and "synthetic" not in words and "(-43)" in words, words


def test_osascript_called_process_error_is_logged_by_the_same_rule(monkeypatch, capsys):
    def raising(cmd, **kw):
        raise subprocess.CalledProcessError(
            3, cmd, stderr=f'execution error: Can’t get chat id "{GUID}". (-1728)\nmore: {TEXT}')

    monkeypatch.setattr(osa.subprocess, "run", raising)
    assert osa._applescript(_AS_TEXT, GUID, TEXT) is False
    out, err = capsys.readouterr()
    assert out == '[fallback] osascript failed: rc=3 execution error: Can’t get chat id "...". (-1728)\n'
    assert_clean(out + err)


def test_osascript_success_and_silent_failure(monkeypatch, capsys):
    monkeypatch.setattr(osa.subprocess, "run", _completed(0, f"a warning naming {GUID}"))
    assert osa._applescript(_AS_TEXT, GUID, TEXT) is True
    assert capsys.readouterr().out == ""                                 # a success logs nothing
    monkeypatch.setattr(osa.subprocess, "run", _completed(1, ""))
    assert osa._applescript(_AS_TEXT, GUID, TEXT) is False
    assert capsys.readouterr().out == "[fallback] osascript failed: rc=1\n"


def test_failure_words_unit():
    assert _failure_words(None, (GUID,)) == "" and _failure_words(b"", (GUID,)) == ""
    assert _failure_words(b'x "y" z', ()) == 'x "..." z'                 # bytes are decoded
    assert _failure_words("first\nsecond", ()) == "first"                # first line only
    assert len(_failure_words("e" * 500, ())) == 200                     # capped
    multi = "OKAPI line one\nHERON line two"
    assert _failure_words("error near OKAPI line one (-1)", (multi,)) == "error near <arg> (-1)"
    assert _failure_words("error near ab (-1)", ("ab",)) == "error near ab (-1)"   # too short to search for


def test_the_engine_reports_the_frozen_failure_and_logs_nothing_else(monkeypatch, capsys, tmp_path):
    """Through the engine, as the relay calls it: a timeout on both guid
    variants of an ``any;`` chat is the frozen ``AppleScript fallback failed``
    and two class-only lines."""
    monkeypatch.setattr(osa.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 20)))
    engine = AppleScriptEngine(outbox=tmp_path / "outbox")
    assert sync(engine.send_text(ANY_GUID, TEXT)) == SendResult(False, "applescript", AS_FAILED)
    res = sync(engine.send_attachment(GUID, "OKAPI-PLAN-5519.pdf", b"%PDF synthetic", "application/pdf"))
    assert res == SendResult(False, "applescript", AS_FAILED)
    out, err = capsys.readouterr()
    assert out == "[fallback] osascript error: TimeoutExpired\n" * 3
    assert_clean(out + err)


# ---------------------------------------------------------------------------
# the chain's hop log: an upstream body without its `data`
# ---------------------------------------------------------------------------

#: The shape BlueBubbles server 1.9.9 answers a failed send with: status,
#: message, error{type, message} and, under ``data``, the message it could
#: not send.
BB_SEND_ERROR = json.dumps({
    "status": 500, "message": "Message Send Error",
    "error": {"type": "iMessage Error",
              "message": "Failed to send message! See attached message error code."},
    "data": {"originalROWID": None, "guid": "relay-1700000000000", "text": TEXT,
             "handle": {"address": "+15550004242"}, "error": 22},
})


def test_log_detail_drops_the_data_member_of_an_upstream_json_body():
    res = SendResult(False, "bb", f"BlueBubbles failed (HTTP 500: {BB_SEND_ERROR[:200]})",
                     status=500, body=BB_SEND_ERROR)
    logged = _log_detail(res)
    assert logged == ('HTTP 500: {"status": 500, "message": "Message Send Error", "error": '
                      '{"type": "iMessage Error", "message": "Failed to send message! '
                      'See attached message error code."}}')
    assert_clean(logged)
    assert "ZEBRA-QUOKKA-7731" in res.body                               # the result itself is untouched
    # capped, like the old excerpt
    long = json.dumps({"status": 500, "message": "m" * 1000})
    assert len(_log_detail(SendResult(False, "bb", "d", status=500, body=long))) == len("HTTP 500: ") + LOG_BODY_CHARS


@pytest.mark.parametrize("body", [f"<html>{TEXT}</html>", json.dumps([TEXT]), json.dumps(TEXT), "", TEXT])
def test_log_detail_never_quotes_a_body_that_is_not_a_json_object(body):
    logged = _log_detail(SendResult(False, "bb", f"BlueBubbles failed (HTTP 502: {body[:200]})",
                                    status=502, body=body))
    assert logged == f"HTTP 502, body not quoted ({len(body)} characters, not a JSON object)"
    assert_clean(logged)


def test_log_detail_keeps_the_engines_own_words_when_there_is_no_upstream_body():
    assert _log_detail(SendResult(False, "applescript", AS_FAILED)) == AS_FAILED
    assert _log_detail(SendResult(False, "bb", "BlueBubbles failed (synthetic refused)")) == \
        "BlueBubbles failed (synthetic refused)"


def test_a_failed_bluebubbles_send_is_logged_without_the_message_and_answered_with_it(capsys):
    """End to end through the real engine and the chain: BlueBubbles answers
    the 1.9.9 error shape, AppleScript then fails too. The log has the status
    and BlueBubbles' error words; the 502 for the client is what it always was."""
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text=BB_SEND_ERROR))
    bb = BlueBubblesEngine("http://bb.invalid:1234", "stub-bb-password-not-real", transport=transport)
    script = AppleScriptEngine(runner=lambda script, *args: False)
    lines: list[str] = []
    with pytest.raises(DeliveryError) as info:
        sync(deliver([bb, script], Capability.TEXT, GUID, TEXT, log=lines.append))
    assert lines == [
        '[send] bluebubbles failed (HTTP 500: {"status": 500, "message": "Message Send Error", "error": '
        '{"type": "iMessage Error", "message": "Failed to send message! See attached message error code."}})'
        " — trying applescript",
        f"[send] applescript failed ({AS_FAILED})",
    ]
    assert_clean(lines[0])
    assert info.value.status == 502                                       # the answer is unchanged
    assert info.value.detail == f"BlueBubbles failed (HTTP 500: {BB_SEND_ERROR[:200]}); {AS_FAILED}"
    # a single engine: the upstream status and body pass through to the client, whole, as before
    with pytest.raises(DeliveryError) as info:
        sync(deliver([bb], Capability.TEXT, GUID, TEXT, log=lines.append))
    assert (info.value.status, info.value.detail) == (500, BB_SEND_ERROR)
    assert_clean(lines[-1])
    assert "stub-bb-password-not-real" not in "".join(lines)


# ---------------------------------------------------------------------------
# beeper.send: the status and Beeper's error code, never its answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status, body, logged", [
    # an error answer that repeats the message: none of it is logged
    (400, {"code": "INVALID_REQUEST", "message": f"cannot send {TEXT}", "details": {"text": TEXT}},
     "[beeper] send failed HTTP 400 (INVALID_REQUEST)"),
    (404, {"code": "not_found"}, "[beeper] send failed HTTP 404 (not_found)"),
    (500, {"message": f"failed: {TEXT}"}, "[beeper] send failed HTTP 500"),              # no code
    (500, {"code": f"{TEXT}"}, "[beeper] send failed HTTP 500"),                         # a "code" that is prose
    (500, {"code": 17}, "[beeper] send failed HTTP 500"),
    (502, f"<html>{TEXT}</html>", "[beeper] send failed HTTP 502"),                      # not JSON
    (500, [TEXT], "[beeper] send failed HTTP 500"),                                      # JSON, not an object
    (500, "", "[beeper] send failed HTTP 500"),
])
def test_a_failed_beeper_send_logs_the_status_and_code_only(monkeypatch, capsys, status, body, logged):
    import beeper

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(beeper.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(beeper, "_enabled", True)
    monkeypatch.setattr(beeper, "BEEPER_TOKEN", "stub-beeper-token-not-real")
    monkeypatch.setattr(beeper, "BEEPER_URL", "http://beeper.invalid:23373")
    assert sync(beeper.send("bp:401", TEXT)) is False
    out, err = capsys.readouterr()
    assert out == logged + "\n"
    assert_clean(out + err)
    assert "stub-beeper-token-not-real" not in out


# ---------------------------------------------------------------------------
# the voice endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def client(r5) -> TestClient:
    """ASGI test client; no ``with``, so startup hooks never run."""
    return TestClient(r5.app)


def test_assistant_prepare_logs_the_length_not_the_dictated_text(r5, client, compat_db, capsys):
    spoken = f"text Alice Anders that says {TEXT}"
    body = client.post("/assistant/prepare", json={"query": spoken}, headers=AUTH).json()
    assert (body["status"], body["chat_name"], body["text"]) == ("confirm", "Alice Anders", TEXT)
    out, err = capsys.readouterr()
    assert out == f"[assist] {len(TEXT)}-character message -> Alice Anders (confident)\n"
    assert_clean(out + err)
    r5.PENDING.clear()


def test_v_prepare_logs_the_length_not_the_dictated_text(r5, client, compat_db, capsys):
    resp = client.post("/v/prepare", json={"query": f"text Alice Anders {TEXT}"}, headers=AUTH)
    assert resp.text == f"Send {TEXT} to Alice Anders?"                   # the answer still reads it back
    out, err = capsys.readouterr()
    assert out == f"[assist] {len(TEXT)}-character message -> Alice Anders (confident)\n"
    assert_clean(out + err)
    r5.LAST_PENDING.clear()


def test_ambiguous_recipient_lines_carry_names_but_no_text(r5, client, monkeypatch, capsys):
    cands = [(0.7, "Alice Anders", [ALICE_PHONE]), (0.6, "Alicia Stand-In", ["+15550000011"])]
    monkeypatch.setattr(r5, "resolve_assistant", lambda q: ("suggest", cands, TEXT))
    body = client.post("/assistant/prepare", json={"query": "anything"}, headers=AUTH).json()
    assert body["status"] == "choose" and body["text"] == TEXT
    assert client.post("/v/prepare", json={"query": "anything"}, headers=AUTH).status_code == 200
    out, err = capsys.readouterr()
    line = f"[assist] {len(TEXT)}-character message -> ambiguous: ['Alice Anders', 'Alicia Stand-In']\n"
    assert out == line * 2
    assert_clean(out + err)
    r5.PENDING.clear(); r5.LAST_PENDING.clear()


def test_assistant_send_failures_log_the_status_not_the_upstream_body(r5, compat_db, monkeypatch, capsys):
    """A DeliveryError passes a single engine's upstream body through whole,
    and BlueBubbles' body carries the message: the log line is the status."""
    async def failing_text(chat_guid, text, reply_to_guid=None):
        raise DeliveryError(500, BB_SEND_ERROR)

    async def failing_deliver(*args, **kwargs):
        raise DeliveryError(500, BB_SEND_ERROR)

    monkeypatch.setattr(r5, "deliver_text", failing_text)
    monkeypatch.setattr(r5, "deliver", failing_deliver)
    known = {"kind": "confirm", "name": "Alice Anders", "addresses": [ALICE_PHONE], "text": TEXT}
    res = sync(r5.assistant_deliver(known, "yes"))                        # an existing chat
    assert res["status"] == "failed"
    stranger = {"kind": "confirm", "name": "Nobody Known", "addresses": [NOBODY_PHONE], "text": TEXT}
    res = sync(r5.assistant_deliver(stranger, "yes"))                     # no chat yet: the create path
    assert res["status"] == "failed"
    out, err = capsys.readouterr()
    assert out == "[assist] send failed: HTTP 500\n[assist] new-chat failed: HTTP 500\n"
    assert_clean(out + err)


def test_failure_status_words(r5):
    assert r5._failure_status(DeliveryError(502, TEXT)) == "HTTP 502"
    assert r5._failure_status(DeliveryError(501, "no configured engine can create a chat")) == "HTTP 501"
    assert r5._failure_status(RuntimeError(TEXT)) == "RuntimeError"
