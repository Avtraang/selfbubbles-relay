"""Step R6, the engine half: unsend through BlueBubbles, edit through
``imessage-cli``, and the chain that keeps the two apart.

Measured on the author's Mac (macOS 27.0), once each, with a real message:

* BlueBubbles server 1.9.9 unsends (``POST /api/v1/message/<guid>/unsend``,
  ``{"partIndex": 0}`` -> 200 ``Message unsent!``) and its edit call does
  nothing;
* ``imessage-cli`` 0.24.2 edits and its ``undo-send`` does nothing, while
  printing ``ok``.

So ``BlueBubblesEngine`` has ``UNSEND`` and never ``EDIT``,
``ImessageCliEngine`` has ``EDIT`` and nothing else, and neither is believed:
the routes confirm in ``chat.db`` (``tests/test_edit_unsend_routes.py``).

Nothing here reaches BlueBubbles (``httpx.MockTransport``) or the real tool:
every binary an engine is given is the fake of ``tests/fake_imessage_cli.py``,
written into ``tmp_path``. The values are synthetic and distinctive, so a leak
into a failure detail or a log line cannot hide.
"""

from __future__ import annotations

import asyncio
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import chatdb_adapter
import engines.imessage_cli as cli
from engines import (Capability, DeliveryError, EngineError, SendEngine, SendResult, Unsupported,
                     build_chain, deliver, engine_names, first_with, imessage_capabilities)
from engines.applescript import AppleScriptEngine
from engines.beeper import BeeperEngine
from engines.bluebubbles import (BAD_MESSAGE_GUID, BlueBubblesEngine, _call_segment, _path_segment,
                                 _spellings)
from engines.chain import DEFAULT_ORDER, IMESSAGE_EXTRAS, IMESSAGE_PROBE_GUID
from engines.imessage_cli import ImessageCliEngine, find_binary
from tests.fake_imessage_cli import assert_fake, make_fake_cli
from tests.fixtures import builders
from tests.fixtures.typedstream_writer import encode_attributed_body

BB_URL = "http://bb.invalid:1234"
PASSWORD = "stub-bb-password-not-real"
CHAT = "any;-;+15550004242"                                   # synthetic one-to-one chat
GROUP = "any;+;chat900900900"
BP_CHAT = "bp:42"
GUID = "0FADE0FF-CAFE-4BAD-8ACE-1234FACE5678"                 # synthetic message guid
TEXT = 'ZEBRA-QUOKKA-7731 meet "at the pier" at 9\\30'
SECRETS = (CHAT, "+15550004242", GUID, "0FADE0FF", TEXT, "ZEBRA-QUOKKA-7731", "at the pier", PASSWORD,
           BB_URL, "bb.invalid")


def sync(coro):
    return asyncio.run(coro)


def assert_clean(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, secret


@pytest.fixture(autouse=True)
def one_messages_instance(monkeypatch):
    """The engine counts Messages.app processes around a call (``pgrep``) and
    asks macOS whether this process has the Accessibility grant; here the
    count is a constant and the answer is yes, so no test depends on what is
    running or on what the machine has granted, and the real question is
    never put."""
    monkeypatch.setattr(cli, "count_messages_instances", lambda: 1)
    monkeypatch.setattr(cli, "accessibility_trusted", lambda: True)


# ---------------------------------------------------------------------------
# BlueBubbles: unsend
# ---------------------------------------------------------------------------

class BB:
    """``httpx.MockTransport`` handler: records the request line as ``httpx``
    puts it on the wire, the parameter NAMES and the JSON body."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen: list[tuple[str, str, list[str], object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json
        body = request.read()
        self.seen.append((request.method, request.url.raw_path.decode().split("?", 1)[0],
                          sorted(k for k, _ in request.url.params.multi_items()),
                          json.loads(body) if body else None))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(request)
        status, payload = answer
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    def engine(self) -> BlueBubblesEngine:
        return BlueBubblesEngine(BB_URL + "/", PASSWORD, transport=httpx.MockTransport(self))


def test_unsend_posts_the_guid_as_one_segment_with_the_part_index(monkeypatch):
    timeouts = []
    real = httpx.AsyncClient

    class Spy(real):
        def __init__(self, *a, **kw):
            timeouts.append(kw.get("timeout"))
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Spy)
    bb = BB((200, {"status": 200, "message": "Message unsent!"}), (200, {"status": 200}))
    engine = bb.engine()
    assert sync(engine.unsend(CHAT, GUID)) == SendResult(True, "bb")
    assert sync(engine.unsend(CHAT, GUID, part_index=3)) == SendResult(True, "bb")
    assert bb.seen == [("POST", f"/api/v1/message/{GUID}/unsend", ["password"], {"partIndex": 0}),
                       ("POST", f"/api/v1/message/{GUID}/unsend", ["password"], {"partIndex": 3})]
    assert timeouts == [15, 15]
    # the chat guid is not sent anywhere: BlueBubbles finds the chat itself
    assert CHAT not in str(bb.seen)


def test_unsend_http_errors_are_results_with_the_upstream_status_and_body():
    body = ('{"status":400,"message":"You\'ve made a bad request! Please check your request params & body",'
            '"error":{"type":"Validation Error","message":"Selected message does not exist!"}}')
    bb = BB((400, body), (500, "synthetic BB failure"))
    engine = bb.engine()
    res = sync(engine.unsend(CHAT, GUID))
    assert (res.ok, res.via, res.status, res.body) == (False, "bb", 400, body)
    assert res.detail == f"BlueBubbles failed (HTTP 400: {body[:200]})"
    res = sync(engine.unsend(CHAT, GUID))
    assert (res.ok, res.status, res.body) == (False, 500, "synthetic BB failure")
    assert res.detail == "BlueBubbles failed (HTTP 500: synthetic BB failure)"


@pytest.mark.parametrize("error", [httpx.ConnectError(f"refused {BB_URL}/api?password={PASSWORD} SECRET-DETAIL"),
                                   httpx.ReadTimeout("synthetic read timeout: SECRET-DETAIL"),
                                   OSError(f"synthetic: {PASSWORD}"), RuntimeError("SECRET-DETAIL")],
                         ids=lambda e: type(e).__name__)
def test_unsend_transport_errors_are_reported_by_class_name_only(error):
    res = sync(BB(error).engine().unsend(CHAT, GUID))
    assert res == SendResult(False, "bb", f"BlueBubbles failed ({type(error).__name__})")
    assert res.status is None and res.body is None
    assert "SECRET-DETAIL" not in res.detail
    assert_clean(res.detail)


def test_unsend_failure_details_never_carry_the_url_or_the_password():
    """An upstream that quotes the request it got (a proxy's error page does)."""
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=f"Bad gateway for {request.url} (password={PASSWORD})")

    res = sync(BB(echo).engine().unsend(CHAT, GUID))
    assert (res.ok, res.status) == (False, 502)
    assert PASSWORD not in res.detail and PASSWORD not in res.body
    assert BB_URL not in res.detail and BB_URL not in res.body
    assert res.body.startswith("Bad gateway for ***") and "password=***" in res.body
    assert GUID not in res.detail and GUID not in res.body          # nor the message guid from the path


#: A password with characters a URL has to encode: on the wire it is not the string that was typed.
AWKWARD_PASSWORD = "stub pazz/w0rd&x=1+é#not-real"


def test_a_quoted_url_never_gives_the_password_away_in_any_encoding():
    """An upstream that answers JSON quoting the request URL (the chain logs a
    JSON error body, and the client gets it whole): the password is in it
    percent-encoded, and the message guid is in its path."""
    seen = {}

    def echo(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(502, json={"status": 502, "message": f"no route for {request.url}",
                                         "raw": request.url.raw_path.decode(),
                                         "typed": f"password={AWKWARD_PASSWORD}"})

    engine = BlueBubblesEngine(BB_URL, AWKWARD_PASSWORD, transport=httpx.MockTransport(echo))
    res = sync(engine.unsend(CHAT, GUID))
    assert (res.ok, res.status) == (False, 502)
    assert AWKWARD_PASSWORD not in seen["url"] and "stub" in seen["url"]     # the premise: it travels encoded
    lines: list[str] = []
    with pytest.raises(DeliveryError) as info:
        sync(deliver([engine], Capability.UNSEND, CHAT, GUID, part_index=0, log=lines.append))
    for text in (res.detail, res.body, info.value.detail, *lines):
        for spelling in _spellings(AWKWARD_PASSWORD):
            assert spelling not in text, spelling
        for piece in ("stub", "pazz", "w0rd", "not-real", GUID, "0FADE0FF", BB_URL, "bb.invalid"):
            assert piece not in text, piece
    assert res.body.count("password=***") == 3 and "/api/v1/message/***/unsend" in res.body
    assert len(lines) == 1 and lines[0].startswith("[send] bluebubbles failed (HTTP 502: ")


def test_the_spellings_of_a_secret_and_what_is_too_short_to_search_for():
    assert _spellings("plain") == {"plain"}
    assert _spellings("a b/c") >= {"a b/c", "a%20b%2Fc", "a+b%2Fc", "a%20b%2fc", "a+b%2fc"}
    assert "" not in _spellings("")
    engine = BlueBubblesEngine(BB_URL, PASSWORD)
    # a short identifier is left alone: replacing "text" wherever it occurs would shred the answer
    assert engine._scrub("the text of it", "text") == "the text of it"
    assert engine._scrub(f"about {GUID} at {BB_URL}/x", GUID) == "about *** at ***/x"
    assert engine._scrub("x?PASSWORD=abc%20def&k=1 and password=tail") == "x?PASSWORD=***&k=1 and password=***"


#: Guids that are not a message: path navigation, separators, a query, a fragment, a line break.
HOSTILE_GUIDS = ("../../message/text", "..", ".", "...", "", "a/b", "a/../../chat/new", "x/../../../facetime/session",
                 "a?x=1", "a#frag", "a\nb", "a b", "%2e%2e", "..%2F..%2Fmessage%2Ftext", "text", "x" * 300)


@pytest.mark.parametrize("guid", HOSTILE_GUIDS)
def test_no_guid_reaches_a_path_other_than_the_unsend_endpoint(guid):
    bb = BB((200, {"status": 200}))
    engine = bb.engine()
    if not guid.strip("."):
        with pytest.raises(EngineError) as info:
            sync(engine.unsend(CHAT, guid))
        assert (info.value.detail, info.value.status) == (BAD_MESSAGE_GUID, 400)
        assert bb.seen == []                                       # refused before anything is sent
        return
    assert sync(engine.unsend(CHAT, guid)).ok
    (method, path, params, body), = bb.seen
    assert method == "POST" and params == ["password"] and body == {"partIndex": 0}
    assert path.startswith("/api/v1/message/") and path.endswith("/unsend"), path
    segment = path[len("/api/v1/message/"):-len("/unsend")]
    assert segment and "/" not in segment and segment not in (".", ".."), path


def test_the_segment_helper_is_shared_with_the_facetime_calls():
    assert BAD_MESSAGE_GUID == "invalid message guid"
    assert _path_segment(GUID, BAD_MESSAGE_GUID) == GUID            # a real guid goes in unchanged
    assert _path_segment("p:0/" + GUID, BAD_MESSAGE_GUID) == "p%3A0%2F" + GUID
    assert _call_segment("synthetic-call") == "synthetic-call"      # the R5 helper still answers the same
    with pytest.raises(EngineError) as info:
        _call_segment("..")
    assert info.value.detail == "invalid FaceTime call id"


def test_a_refused_guid_through_the_chain_is_a_400_and_bluebubbles_cannot_edit():
    bb = BB()
    lines: list[str] = []
    with pytest.raises(DeliveryError) as info:
        sync(deliver([bb.engine()], Capability.UNSEND, CHAT, "..", part_index=0, log=lines.append))
    assert (info.value.status, info.value.detail) == (400, BAD_MESSAGE_GUID)
    assert bb.seen == []
    # EDIT is not a BlueBubbles capability: the chain never asks it ...
    with pytest.raises(DeliveryError) as info:
        sync(deliver([bb.engine()], Capability.EDIT, CHAT, GUID, TEXT, part_index=0))
    assert (info.value.status, info.value.detail) == \
        (501, "no configured engine can edit messages in this chat")
    # ... and a direct caller gets a clear refusal, with nothing sent
    with pytest.raises(Unsupported) as unsupported:
        sync(bb.engine().edit(CHAT, GUID, TEXT))
    assert unsupported.value.detail == "bluebubbles cannot edit messages"
    assert bb.seen == []


# ---------------------------------------------------------------------------
# imessage-cli: finding the binary
# ---------------------------------------------------------------------------

def _executable(path: Path, mode: int = 0o755) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(mode)
    return str(path)


def test_discovery_order_explicit_then_the_two_homebrew_places_then_the_path(tmp_path):
    first = _executable(tmp_path / "opt" / "imessage-cli")
    second = _executable(tmp_path / "usr-local" / "imessage-cli")
    on_path = _executable(tmp_path / "elsewhere" / "imessage-cli")
    explicit = _executable(tmp_path / "mine" / "my-cli")
    which_calls = []

    def which(name):
        which_calls.append(name)
        return on_path

    kw = {"candidates": (first, second), "which": which}
    assert find_binary(explicit, **kw) == explicit                   # IMESSAGE_CLI wins
    assert find_binary(None, **kw) == first == find_binary("", **kw) == find_binary("   ", **kw)
    os.remove(first)
    assert find_binary(None, **kw) == second                        # the first one that exists
    os.chmod(second, 0o644)
    assert which_calls == []                                         # the PATH is the last resort
    assert find_binary(None, **kw) == on_path                       # not executable: skipped
    assert which_calls == ["imessage-cli"]
    assert find_binary(None, candidates=(first, second), which=lambda name: None) is None
    # a directory of that name is not a binary
    (tmp_path / "dir" / "imessage-cli").mkdir(parents=True)
    assert find_binary(None, candidates=(str(tmp_path / "dir" / "imessage-cli"),), which=lambda n: None) is None


@pytest.mark.parametrize("setting", ["0", "off", "OFF", " Off ", " 0 "])
def test_imessage_cli_zero_or_off_disables_the_engine_whatever_is_installed(tmp_path, setting):
    installed = _executable(tmp_path / "imessage-cli")
    assert find_binary(setting, candidates=(installed,), which=lambda name: installed) is None
    assert not ImessageCliEngine(find_binary(setting, candidates=(installed,))).configured()


def test_an_explicit_path_that_is_not_an_executable_file_is_not_replaced_by_another(tmp_path, monkeypatch):
    installed = _executable(tmp_path / "imessage-cli")
    kw = {"candidates": (installed,), "which": lambda name: installed}
    assert find_binary(str(tmp_path / "missing"), **kw) is None      # naming a binary means that binary
    plain = tmp_path / "not-executable"
    plain.write_text("x")
    assert find_binary(str(plain), **kw) is None
    assert find_binary(str(tmp_path), **kw) is None                  # a directory
    # ~ is expanded, a relative path is taken from the working directory, and the answer is absolute
    assert find_binary("relative/imessage-cli", **kw) is None
    monkeypatch.setenv("HOME", str(tmp_path))
    assert find_binary("~/imessage-cli", **kw) == installed
    monkeypatch.chdir(tmp_path)
    assert find_binary("./imessage-cli", **kw) == installed and os.path.isabs(find_binary("./imessage-cli", **kw))


def test_the_defaults_are_homebrews_two_locations_and_nothing_is_run():
    assert cli.DEFAULT_PATHS == ("/opt/homebrew/bin/imessage-cli", "/usr/local/bin/imessage-cli")
    assert cli.OFF_VALUES == {"0", "off"}
    assert ImessageCliEngine(None).configured() is False and ImessageCliEngine("").configured() is False
    assert ImessageCliEngine(None).ping() is False
    res = sync(ImessageCliEngine(None).edit(CHAT, GUID, TEXT))
    assert res == SendResult(False, "imessage-cli", "imessage-cli is not installed")


# ---------------------------------------------------------------------------
# imessage-cli: the engine against the fake binary
# ---------------------------------------------------------------------------

def _engine(fake, tmp_path, **kw) -> ImessageCliEngine:
    assert_fake(fake.binary, tmp_path)
    return ImessageCliEngine(str(fake.binary), tmp_path / "data" / "imessage-cli", **kw)


def test_engine_identity_and_the_one_capability(tmp_path):
    engine = _engine(make_fake_cli(tmp_path), tmp_path)
    assert (engine.name, engine.via) == ("imessage-cli", "imessage-cli")
    assert engine.capabilities == frozenset({Capability.EDIT})
    assert engine.facetime is None and engine.configured() and engine.ping()
    for guid in (CHAT, GROUP, "iMessage;-;+15550004242", "SMS;-;+15550004242"):
        assert engine.handles(guid)
    assert not engine.handles(BP_CHAT)
    # undo-send printed "ok" and did nothing when it was tried: never offered
    for call, cap in ((engine.unsend(CHAT, GUID), "unsend messages"), (engine.send_text(CHAT, TEXT), "send text"),
                      (engine.send_attachment(CHAT, "a", b"x", "text/plain"), "send attachments"),
                      (engine.react(CHAT, GUID, "love"), "react"), (engine.create_chat(["+15550004242"], TEXT),
                                                                    "create a chat"),
                      (engine.chat_icon(CHAT), "fetch chat icons")):
        with pytest.raises(Unsupported) as info:
            sync(call)
        assert info.value.detail == f"imessage-cli cannot {cap}"
    with pytest.raises(Unsupported):
        engine.contacts()


def test_edit_runs_the_measured_command_and_reports_ok(tmp_path, capsys):
    fake = make_fake_cli(tmp_path)
    engine = _engine(fake, tmp_path)
    assert sync(engine.edit(CHAT, GUID, TEXT)) == SendResult(True, "imessage-cli")
    call, = fake.calls
    assert call["argv"] == ["--json", "--no-events", "--data-dir", str(tmp_path / "data" / "imessage-cli"),
                            "edit", CHAT, GUID, TEXT]
    assert engine.command(CHAT, GUID, TEXT) == [str(fake.binary), *call["argv"]]
    assert call["stdin"] == ""                                       # closed: nothing to read, no hang
    assert (tmp_path / "data" / "imessage-cli").is_dir()             # the tool's own state directory
    out, err = capsys.readouterr()
    assert out == "" and err == ""                                   # a success logs nothing


def test_the_tool_gets_four_environment_variables_and_none_of_the_relays(tmp_path, monkeypatch):
    for key, value in (("IMSG_TOKEN", "stub-token-in-the-environment"), ("BB_PASSWORD", PASSWORD),
                       ("BEEPER_TOKEN", "stub-beeper"), ("HA_TOKEN", "stub-ha"), ("IMESSAGE_CLI", "0"),
                       ("LANG", "de_DE.UTF-8")):
        monkeypatch.setenv(key, value)
    assert set(cli.child_environment()) == {"HOME", "PATH", "LANG", "TMPDIR"}
    assert cli.child_environment({}) == {"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                         "LANG": "en_US.UTF-8", "TMPDIR": cli.tempfile.gettempdir()}
    assert cli.child_environment({"HOME": "/h", "PATH": "/p", "LANG": "x", "TMPDIR": "/t", "IMSG_TOKEN": "s"}) == \
        {"HOME": "/h", "PATH": "/p", "LANG": "x", "TMPDIR": "/t"}
    fake = make_fake_cli(tmp_path)
    assert sync(_engine(fake, tmp_path).edit(CHAT, GUID, TEXT)).ok
    env = fake.calls[0]["env"]
    assert env["HOME"] == os.environ["HOME"] and env["PATH"] == os.environ["PATH"]
    assert env["LANG"] == "de_DE.UTF-8"
    for key in ("IMSG_TOKEN", "BB_PASSWORD", "BEEPER_TOKEN", "HA_TOKEN", "IMESSAGE_CLI"):
        assert key not in env, key
    assert "stub-token-in-the-environment" not in str(env) and PASSWORD not in str(env)


@pytest.mark.parametrize("text", [
    "- milk\n- eggs",                                   # starts with a hyphen, two lines
    "-5 degrees",
    "--not an option, a sentence",
    "-",
    "--",
    'he said "hi" and \'bye\' `x` $HOME $(touch INJECTED) ; touch INJECTED | cat && true',
    "line one\nline two\r\n\ttabbed",
    "emoji \U0001F389\U0001F468‍\U0001F469‍\U0001F467 and café, naïve, 日本語",
    "  leading and trailing spaces  ",
    "back\\slash and a 'single' quote",
    "x" * 10000,
], ids=["hyphen-list", "minus-five", "double-hyphen-sentence", "single-hyphen", "double-hyphen", "shell",
        "newlines", "unicode", "spaces", "backslash", "long"])
def test_the_text_arrives_as_one_argument_exactly_as_given(tmp_path, monkeypatch, text):
    monkeypatch.chdir(tmp_path)                                      # where a shell would have created INJECTED
    fake = make_fake_cli(tmp_path)
    assert sync(_engine(fake, tmp_path).edit(CHAT, GUID, text)) == SendResult(True, "imessage-cli")
    argv = fake.calls[0]["argv"]
    assert argv[-4:] == ["edit", CHAT, GUID, text]
    assert len(argv) == 8                                            # one element, never split
    assert not (tmp_path / "INJECTED").exists() and not (Path(fake.calls[0]["cwd"]) / "INJECTED").exists()


@pytest.mark.parametrize("text", ["--json", "--verbose", "--stay-open", "--no-events", "--format=json",
                                  "--data-dir=/tmp/x y", "-h", "--help", "--anything-else", "-x",
                                  "-h=anything", "-k=1", "---", "-----", "--- note ---", "---> look",
                                  "---\nsecond line"])
def test_a_text_the_tool_would_read_as_an_option_is_refused_before_it_runs(tmp_path, text):
    """0.24.2 has no end-of-options marker (``--`` is passed on as a value) and
    takes an argument that is exactly one of its options as that option,
    wherever it stands (``-h=anything`` prints its help); an argument behind
    three or more hyphens is rejected by its parser with exit status 64
    (seen with ``imessage-cli version ---``). Such a text would edit nothing,
    or something else."""
    fake = make_fake_cli(tmp_path)
    res = sync(_engine(fake, tmp_path).edit(CHAT, GUID, text))
    assert res == SendResult(False, "imessage-cli",
                             "imessage-cli cannot take a text that reads as one of its options")
    assert fake.calls == []                                          # the tool was never started


@pytest.mark.parametrize("text", ["", "a\x00b", "\x00", None, 5, b"bytes",
                                  "a\x1b[2Jb", "back\x08space", "del\x7f", "bell\x07", "form\x0cfeed"])
def test_a_text_that_cannot_be_an_argument_is_refused_too(tmp_path, text):
    """Nothing to hand over, a NUL, or a control character: what a key code
    does when the tool enters it into Messages was never tried. (Tab, line
    feed and carriage return are text; the test above passes all three.)"""
    fake = make_fake_cli(tmp_path)
    res = sync(_engine(fake, tmp_path).edit(CHAT, GUID, text))
    assert res == SendResult(False, "imessage-cli", "imessage-cli cannot take this text")
    assert fake.calls == []


def test_a_text_the_system_cannot_encode_is_a_failure_not_a_crash(tmp_path):
    fake = make_fake_cli(tmp_path)
    res = sync(_engine(fake, tmp_path).edit(CHAT, GUID, "half a surrogate pair \ud83d"))
    assert res == SendResult(False, "imessage-cli", "imessage-cli could not be started")
    assert fake.calls == []


def test_reads_as_option():
    for text in ("-h", "-x", "--json", "--no-use-secondary-instance", "--format=yaml", "--format=a b\nc", "--9",
                 "-h=x", "-h=a b\nc", "-k=1", "---", "----", "--- note ---", "---> look", "---x"):
        assert cli.reads_as_option(text), text
    for text in ("-", "--", "- milk", "-5", "-hey", "-_-", "--json please", "-- json", "--a b=c",
                 "hello", " --json", "--json ", "--json\nmore", "—json", "--=x", "-- -", "a ---", "- --- -"):
        assert not cli.reads_as_option(text), text


@pytest.mark.parametrize("guid", ["latest", "LATEST", "latest-1", "latest-999999", "last-message", "lastMessage",
                                  "latestMessage", "--json", "-h", "-" + GUID, "", "a b", "a\nb", "x" * 129,
                                  GUID + "\x00", "café", "a;b", '"' + GUID + '"'])
def test_a_message_id_that_is_an_alias_or_not_a_guid_is_refused(tmp_path, guid):
    """``latest`` and its relatives mean "the newest message" to the tool."""
    fake = make_fake_cli(tmp_path)
    res = sync(_engine(fake, tmp_path).edit(CHAT, guid, TEXT))
    assert res == SendResult(False, "imessage-cli", cli.BAD_MESSAGE_ID)
    assert fake.calls == []


@pytest.mark.parametrize("chat", ["+15550004242", "someone@example.test", "chat900900900", "--json", "-h",
                                  "-any;-;+15550004242", "", "any;-;", "any;x;+15550004242", "any;-;a\nb",
                                  "any;-;a\x00b", BP_CHAT, "bp:any;-;+15550004242", ";-;+15550004242"])
def test_a_chat_id_that_is_not_a_full_chat_guid_is_refused(tmp_path, chat):
    """A bare address would make the tool look for a chat by itself."""
    fake = make_fake_cli(tmp_path)
    res = sync(_engine(fake, tmp_path).edit(chat, GUID, TEXT))
    assert res == SendResult(False, "imessage-cli", cli.BAD_CHAT_ID)
    assert fake.calls == []


def test_real_guid_shapes_are_accepted_and_only_the_first_part_can_be_edited(tmp_path):
    fake = make_fake_cli(tmp_path)
    engine = _engine(fake, tmp_path)
    for chat in (CHAT, GROUP, "iMessage;-;someone@example.test", "iMessage;+;chat123"):
        for guid in (GUID, GUID.lower(), "p:0/" + GUID, "SYN-MSG-000001"):
            assert cli.argument_problem(chat, guid, TEXT) is None, (chat, guid)
    assert sync(engine.edit(CHAT, GUID, TEXT, part_index=0)).ok
    for part in (1, 63, -1):
        assert sync(engine.edit(CHAT, GUID, TEXT, part_index=part)) == \
            SendResult(False, "imessage-cli", "imessage-cli can only edit the first part of a message")
    assert len(fake.calls) == 1


def test_exit_zero_without_the_ok_line_is_a_failure(tmp_path, capsys):
    """Exit status 0 is not success: the run that prints no ok line, the help
    text a ``-h`` gets, and the ok line anywhere but on stdout."""
    fake = make_fake_cli(tmp_path, "no-ok")
    for mode in ("no-ok", "help", "ok-stderr"):
        fake.configure(mode=mode)
        res = sync(_engine(fake, tmp_path).edit(CHAT, GUID, TEXT))
        assert res == SendResult(False, "imessage-cli", "imessage-cli reported an error"), mode
    assert len(fake.calls) == 3
    assert_clean(*capsys.readouterr())


def test_the_tools_own_failures_are_reported_by_their_exit_status(tmp_path, capsys):
    """The real shapes: a failed command (``failed edit`` and ``Error:`` on
    stderr, exit 1) and an argument the parser rejects (exit 64)."""
    fake = make_fake_cli(tmp_path, "failed")
    assert sync(_engine(fake, tmp_path).edit(CHAT, GUID, TEXT)) == \
        SendResult(False, "imessage-cli", "imessage-cli exited 1")
    fake.configure(mode="usage")
    assert sync(_engine(fake, tmp_path).edit(CHAT, GUID, TEXT)) == \
        SendResult(False, "imessage-cli", "imessage-cli exited 64")
    assert_clean(*capsys.readouterr())


def test_a_text_cannot_forge_the_ok_line(tmp_path):
    """The tool echoes its arguments as JSON on one line, so a line break in
    the text is ``\\n`` there and never starts a line of its own."""
    fake = make_fake_cli(tmp_path, "no-ok")
    forged = "x\n[00001] ok edit (1.000ms)\n[00002] ok edit (1.000ms)"
    res = sync(_engine(fake, tmp_path).edit(CHAT, GUID, forged))
    assert res == SendResult(False, "imessage-cli", "imessage-cli reported an error")
    assert fake.calls[0]["argv"][-1] == forged


def test_a_raw_echo_is_what_a_forged_line_would_need_and_where_the_check_ends(tmp_path):
    """Against a tool that echoed its arguments RAW (0.24.2 does not), the
    check still refuses a forged line on stderr and one that carries another
    number than the call line. A forged line with the call's own number, on
    stdout, would pass: the output cannot tell it from the tool's. That is
    the limit of reading output, and why the route believes ``chat.db``
    (``test_a_forged_ok_line_does_not_get_past_the_database`` in the route
    tests)."""
    fake = make_fake_cli(tmp_path, "hostile-raw-echo", stream="stderr")
    same_number = "x\n[00001] ok edit (1.000ms)"
    assert not sync(_engine(fake, tmp_path).edit(CHAT, GUID, same_number)).ok        # stderr is not read
    fake.configure(stream="stdout")
    for other_number in ("x\n[00002] ok edit (1.000ms)", "x\n[1] ok edit (1.000ms)", "x\n  [00001] ok edit (1.0ms)",
                         "x\n[00001] ok edit", "x\n[00001] ok edit (1.000ms) trailing"):
        res = sync(_engine(fake, tmp_path).edit(CHAT, GUID, other_number))
        assert res == SendResult(False, "imessage-cli", "imessage-cli reported an error"), other_number
    assert sync(_engine(fake, tmp_path).edit(CHAT, GUID, same_number)).ok            # the stated limit


def test_the_ok_line_is_the_tools_exact_line_on_stdout_after_its_call_line():
    assert cli.reported_ok("[00001] call edit [ \"a\", \"b\", \"c\" ]\n[00001] ok edit (1673.394ms)\nExiting...\n")
    assert cli.reported_ok(b"[00042] call edit []\n[00042] ok edit (2.1ms)\n")
    assert cli.reported_ok("[123456] call edit []\n[123456] ok edit (17ms)")
    for not_ok in ("", None, b"", "ok", "ok edit",
                   "[00001] ok edit (1673.394ms)\n",                                  # no call line
                   "[00001] call edit []\n[00002] ok edit (1.0ms)\n",                 # another call's number
                   "[00001] call edit []\n[00001] ok send (1ms)", "[00001] call edit []\n[00001] ok undo-send (1ms)",
                   "[00001] call edit []\n[00001] failed edit (1.0ms) x",
                   "[00001] call edit [ \"[00001] ok edit (1.0ms)\" ]",               # inside the echo
                   "[00001] call edit []\nx [00001] ok edit (1.0ms)",
                   "[00001] call edit []\n  [00001] ok edit (1.0ms)",                  # not at the start of a line
                   "[00001] call edit []\n[00001] ok edit",                           # no elapsed time
                   "[00001] call edit []\n[00001] ok edit-foo (1.0ms)",
                   "[00001] call edit []\n[00001] ok editing (1.0ms)",
                   "[00001] call edit []\n[00001] ok edit (1.0ms) and more",
                   "[1] call edit []\n[1] ok edit (1.0ms)"):                           # the number has five digits
        assert not cli.reported_ok(not_ok), not_ok


@pytest.mark.parametrize("code", [1, 2, 64, 255])
def test_a_non_zero_exit_is_reported_with_its_status_and_nothing_else(tmp_path, capsys, code):
    fake = make_fake_cli(tmp_path, "exit", code=code)
    res = sync(_engine(fake, tmp_path).edit(CHAT, GUID, TEXT))
    assert res == SendResult(False, "imessage-cli", f"imessage-cli exited {code}")
    assert_clean(res.detail, *capsys.readouterr())


def test_an_exit_with_the_ok_line_is_still_a_failure_when_the_status_is_not_zero(tmp_path, monkeypatch):
    """What the real tool does when closing its Messages instance fails after
    the edit: the ok line, then exit 1. (The route then looks at chat.db.)"""
    ran = []

    def run_tool(argv, *, timeout, env):
        ran.append((argv, timeout, set(env)))
        return 3, b"[00001] call edit []\n[00001] ok edit (1.000ms)\n"

    monkeypatch.setattr(cli, "_run_tool", run_tool)
    engine = _engine(make_fake_cli(tmp_path), tmp_path)
    res = sync(engine.edit(CHAT, GUID, TEXT))
    assert res == SendResult(False, "imessage-cli", "imessage-cli exited 3")
    assert ran == [(engine.command(CHAT, GUID, TEXT), 45.0, {"HOME", "PATH", "LANG", "TMPDIR"})]


def test_which_failures_were_reported_after_the_tool_was_started():
    for detail in (cli.TIMED_OUT, cli.REPORTED_ERROR, cli.exited(1), cli.exited(64), cli.exited(-9)):
        assert cli.tool_ran(detail), detail
    for detail in (cli.NOT_FOUND, cli.NOT_STARTED, cli.NEEDS_ACCESSIBILITY, cli.BAD_MESSAGE_ID, cli.BAD_CHAT_ID,
                   cli.BAD_TEXT, cli.OPTION_TEXT, cli.ONLY_FIRST_PART, "", "imessage-cli exited", "x; imessage-cli exited 1",
                   "BlueBubbles failed (ReadTimeout)"):
        assert not cli.tool_ran(detail), detail


def test_the_output_is_read_from_a_file_so_a_helper_holding_it_open_cannot_stall_the_call(tmp_path):
    """The tool opens a second Messages instance. Read through a pipe, a
    process it left running would keep the pipe open and the call would wait
    out the whole timeout for an edit that had finished."""
    fake = make_fake_cli(tmp_path, "helper", seconds=4)
    started = time.monotonic()
    res = sync(_engine(fake, tmp_path, timeout=20).edit(CHAT, GUID, TEXT))
    assert res == SendResult(True, "imessage-cli")
    assert time.monotonic() - started < 3.5                          # it did not wait for the helper to end
    assert len(fake.spans) == 1


def test_running_the_tool_uses_no_pipe_and_kills_only_the_tool_on_a_timeout(tmp_path, monkeypatch):
    seen = {}
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, argv, **kw):
            seen.update(kw, argv=argv)
            super().__init__(argv, **kw)

        def kill(self):
            seen.setdefault("killed", []).append(self.pid)
            super().kill()

    monkeypatch.setattr(cli.subprocess, "Popen", Spy)
    killpg = []
    monkeypatch.setattr(os, "killpg", lambda *a: killpg.append(a))
    fake = make_fake_cli(tmp_path, "sleep", seconds=30)
    with pytest.raises(subprocess.TimeoutExpired):
        cli._run_tool([str(fake.binary), "edit", CHAT, GUID, "x"], timeout=0.4, env=cli.child_environment())
    assert seen["stdin"] == subprocess.DEVNULL and seen["stderr"] == subprocess.DEVNULL
    assert seen["stdout"] not in (subprocess.PIPE, subprocess.DEVNULL, None)         # a file of its own
    assert not seen.get("shell") and not seen.get("start_new_session")
    assert len(seen["killed"]) == 1 and killpg == []                 # the tool itself, no process group
    fake.configure(mode="ok")
    code, stdout = cli._run_tool([str(fake.binary), "edit", CHAT, GUID, "x"], timeout=20,
                                 env=cli.child_environment())
    assert code == 0 and cli.reported_ok(stdout) and stdout.endswith(b"Exiting...\n")


def test_a_timeout_is_reported_without_the_command_line(tmp_path, capsys):
    fake = make_fake_cli(tmp_path, "sleep", seconds=30)
    engine = _engine(fake, tmp_path, timeout=0.5)
    res = sync(engine.edit(CHAT, GUID, TEXT))
    assert res == SendResult(False, "imessage-cli", "imessage-cli timed out")
    assert fake.spans == []                                          # it did not get to finish: it was stopped
    out, err = capsys.readouterr()
    assert_clean(res.detail, out, err)
    assert cli.EDIT_TIMEOUT == 45.0 and ImessageCliEngine("/x")._timeout == 45.0


def test_what_the_timeout_exception_would_have_leaked(tmp_path):
    """Why the exception text is never used: it is the whole command line."""
    engine = _engine(make_fake_cli(tmp_path), tmp_path)
    leaked = str(subprocess.TimeoutExpired(cmd=engine.command(CHAT, GUID, TEXT), timeout=45))
    assert CHAT in leaked and GUID in leaked and "ZEBRA-QUOKKA-7731" in leaked


def test_a_binary_that_cannot_be_started_is_a_failure_not_a_crash(tmp_path, capsys):
    gone = tmp_path / "gone" / "imessage-cli"
    res = sync(ImessageCliEngine(str(gone), tmp_path / "data").edit(CHAT, GUID, TEXT))
    assert res == SendResult(False, "imessage-cli", "imessage-cli could not be started")
    # ... nor is a data directory that cannot be made
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    fake = make_fake_cli(tmp_path)
    res = sync(ImessageCliEngine(str(fake.binary), blocker / "under-a-file").edit(CHAT, GUID, TEXT))
    assert res == SendResult(False, "imessage-cli", "imessage-cli could not be started")
    assert fake.calls == []
    assert_clean(*capsys.readouterr())


@pytest.mark.parametrize("mode, extra", [("no-ok", {}), ("failed", {}), ("exit", {"code": 1}),
                                         ("usage", {}), ("hostile-raw-echo", {"stream": "stdout"}),
                                         ("hostile-raw-echo", {"stream": "stderr"}), ("sleep", {"seconds": 30})])
def test_what_the_tool_prints_never_reaches_the_detail_or_the_log(tmp_path, capsys, mode, extra):
    """Every failure mode echoes the guid and the text; none of it comes back."""
    fake = make_fake_cli(tmp_path, mode, **extra)
    lines: list[str] = []
    engine = _engine(fake, tmp_path, timeout=0.5 if mode == "sleep" else 45, log=lines.append)
    with pytest.raises(DeliveryError) as info:
        sync(deliver([engine], Capability.EDIT, CHAT, GUID, TEXT, part_index=0, log=lines.append))
    assert info.value.status == 502
    assert info.value.detail in ("imessage-cli reported an error", "imessage-cli exited 1",
                                 "imessage-cli exited 64", "imessage-cli timed out")
    assert lines == [f"[send] imessage-cli failed ({info.value.detail})"]     # the chain's one hop line
    out, err = capsys.readouterr()
    assert_clean(info.value.detail, *lines, out, err)


def test_the_callable_data_dir_is_resolved_at_call_time(tmp_path):
    fake = make_fake_cli(tmp_path)
    where = {"dir": tmp_path / "first"}
    engine = ImessageCliEngine(str(fake.binary), lambda: where["dir"] / "imessage-cli")
    assert sync(engine.edit(CHAT, GUID, TEXT)).ok
    where["dir"] = tmp_path / "second"
    assert sync(engine.edit(CHAT, GUID, TEXT)).ok
    assert [c["argv"][3] for c in fake.calls] == [str(tmp_path / "first" / "imessage-cli"),
                                                  str(tmp_path / "second" / "imessage-cli")]
    assert oct((tmp_path / "second" / "imessage-cli").stat().st_mode & 0o777) == "0o700"


def test_a_data_directory_that_was_already_there_is_closed_to_others(tmp_path):
    """``mkdir(mode=0o700)`` only sets the mode of a folder it creates. What
    the tool keeps there has not been examined, so one that exists with a
    wider mode is tightened before the tool is given it."""
    data = tmp_path / "data" / "imessage-cli"
    data.mkdir(parents=True)
    os.chmod(data, 0o755)
    os.chmod(data.parent, 0o755)
    fake = make_fake_cli(tmp_path)
    assert sync(ImessageCliEngine(str(fake.binary), data).edit(CHAT, GUID, TEXT)).ok
    assert oct(data.stat().st_mode & 0o777) == "0o700"
    assert oct(data.parent.stat().st_mode & 0o777) == "0o755"       # the parent is the relay's data dir: left alone
    assert cli.prepare_data_dir(data) and len(fake.calls) == 1


def test_a_data_directory_that_is_a_link_or_not_a_directory_is_refused(tmp_path, capsys):
    fake = make_fake_cli(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    os.chmod(elsewhere, 0o755)
    link = tmp_path / "data" / "imessage-cli"
    link.parent.mkdir()
    link.symlink_to(elsewhere, target_is_directory=True)
    res = sync(ImessageCliEngine(str(fake.binary), link).edit(CHAT, GUID, TEXT))
    assert res == SendResult(False, "imessage-cli", "imessage-cli could not be started")
    assert fake.calls == []                                          # the tool was never given the folder
    assert oct(elsewhere.stat().st_mode & 0o777) == "0o755"         # ... and what the link points at is untouched
    assert not cli.prepare_data_dir(link)
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    assert not cli.prepare_data_dir(a_file) and not cli.prepare_data_dir(a_file / "below")
    assert_clean(*capsys.readouterr())


def test_a_data_directory_of_another_user_is_refused(tmp_path, monkeypatch):
    data = tmp_path / "data" / "imessage-cli"
    data.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(cli.os, "getuid", lambda: os.stat(data).st_uid + 1)
    assert not cli.prepare_data_dir(data)


def test_without_a_data_directory_the_tool_is_not_run(tmp_path):
    """No fallback to a shared temporary folder: the relay always names one."""
    fake = make_fake_cli(tmp_path)
    engine = ImessageCliEngine(str(fake.binary))
    assert engine.data_dir() is None
    assert sync(engine.edit(CHAT, GUID, TEXT)) == \
        SendResult(False, "imessage-cli", "imessage-cli could not be started")
    assert fake.calls == []


# ---------------------------------------------------------------------------
# imessage-cli: the Accessibility grant is checked before the tool is started
# ---------------------------------------------------------------------------

def test_without_the_accessibility_grant_the_tool_is_never_started(tmp_path, capsys):
    """Started without the grant, the real tool asks for it (the system prompt,
    its own window) and waits two minutes: a request that hangs to the
    timeout, and permission windows on the Mac, for every edit."""
    fake = make_fake_cli(tmp_path)
    started = time.monotonic()
    res = sync(_engine(fake, tmp_path, trusted=lambda: False).edit(CHAT, GUID, TEXT))
    assert res == SendResult(False, "imessage-cli",
                             "imessage-cli needs the Accessibility grant for the relay's Python")
    assert fake.calls == [] and time.monotonic() - started < 2
    assert not (tmp_path / "data" / "imessage-cli").exists()        # nothing was prepared either
    lines: list[str] = []
    with pytest.raises(DeliveryError) as info:
        sync(deliver([_engine(fake, tmp_path, trusted=lambda: False)], Capability.EDIT, CHAT, GUID, TEXT,
                     part_index=0, log=lines.append))
    assert (info.value.status, info.value.detail) == (502, cli.NEEDS_ACCESSIBILITY)
    assert lines == [f"[send] imessage-cli failed ({cli.NEEDS_ACCESSIBILITY})"]
    assert_clean(*lines, *capsys.readouterr())


def test_a_grant_that_is_there_or_cannot_be_told_lets_the_tool_run(tmp_path, monkeypatch):
    fake = make_fake_cli(tmp_path)

    def broken():
        raise OSError("synthetic")

    for answer in (lambda: True, lambda: None, broken):
        assert sync(_engine(fake, tmp_path, trusted=answer).edit(CHAT, GUID, TEXT)).ok
    assert len(fake.calls) == 3
    # the default is the module's question, looked up when the edit runs
    monkeypatch.setattr(cli, "accessibility_trusted", lambda: False)
    assert sync(_engine(fake, tmp_path).edit(CHAT, GUID, TEXT)).detail == cli.NEEDS_ACCESSIBILITY
    assert len(fake.calls) == 3


def test_the_accessibility_question_only_reads_and_never_raises(monkeypatch):
    """``AXIsProcessTrusted`` (no options, so no prompt), asked of the system
    framework. The framework is a stand-in here: the suite does not put the
    question to the real one."""
    monkeypatch.undo()                                               # the real function, not the autouse answer
    import ctypes
    loaded = []

    class Framework:
        def __init__(self, answer):
            self.AXIsProcessTrusted = lambda: answer

    def cdll(path, *a, **kw):
        loaded.append(path)
        return cdll.framework

    monkeypatch.setattr(ctypes, "CDLL", cdll)
    cdll.framework = Framework(1)
    assert cli.accessibility_trusted() is True
    cdll.framework = Framework(0)
    assert cli.accessibility_trusted() is False
    assert loaded == ["/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"] * 2
    assert cdll.framework.AXIsProcessTrusted.restype is ctypes.c_bool
    assert cdll.framework.AXIsProcessTrusted.argtypes == []

    def missing(path, *a, **kw):
        raise OSError("no such framework")

    monkeypatch.setattr(ctypes, "CDLL", missing)
    assert cli.accessibility_trusted() is None                      # not macOS: unknown, not "no"
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: object())
    assert cli.accessibility_trusted() is None


# ---------------------------------------------------------------------------
# imessage-cli: which version is installed (read from the path, nothing run)
# ---------------------------------------------------------------------------

def test_the_version_is_read_from_the_homebrew_folder_the_link_points_into(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: ran.append(a))
    assert cli.CHECKED_VERSION == "0.24.2"
    for folder, version in (("0.24.2", "0.24.2"), ("0.24.5", "0.24.5"), ("0.24.2_1", "0.24.2"), ("1.0.0-beta.2", "1.0.0-beta.2")):
        real = tmp_path / "homebrew" / "Cellar" / "imessage-cli" / folder / "bin" / "imessage-cli"
        real.parent.mkdir(parents=True)
        real.write_text("#!/bin/sh\n")
        link = tmp_path / "homebrew" / "bin" / f"imessage-cli-{folder}"
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(real)
        assert cli.installed_version(str(link)) == version == cli.installed_version(str(real)), folder
    for unknown in (None, "", str(tmp_path / "fake-cli" / "imessage-cli"), "/usr/local/bin/imessage-cli-not-there",
                    str(tmp_path / "homebrew" / "Cellar" / "other-tool" / "1.0" / "bin" / "imessage-cli"),
                    "a\x00b"):
        assert cli.installed_version(unknown) is None, unknown
    assert ran == []


def test_one_call_at_a_time_even_from_two_engine_objects(tmp_path):
    """The relay builds a new engine object for every request; the tool still
    works one Messages window."""
    fake = make_fake_cli(tmp_path, "sleep", seconds=0.4)

    async def both():
        a, b, c = (_engine(fake, tmp_path) for _ in range(3))
        return await asyncio.gather(a.edit(CHAT, GUID, "first"), b.edit(CHAT, GUID, "second"),
                                    c.edit(GROUP, GUID, "third"))

    results = sync(both())
    assert all(r.ok for r in results)
    spans = sorted(fake.spans)
    assert len(spans) == 3
    for (_, finished), (started, _) in zip(spans, spans[1:]):
        assert started >= finished, spans                            # no two runs overlap


@pytest.mark.filterwarnings("ignore::DeprecationWarning")      # uvloop's own, on Python 3.14
def test_the_engine_runs_under_uvloop_which_is_what_uvicorn_gives_the_relay(tmp_path):
    uvloop = pytest.importorskip("uvloop")
    fake = make_fake_cli(tmp_path, "sleep", seconds=0.2)

    async def both():
        return await asyncio.gather(_engine(fake, tmp_path).edit(CHAT, GUID, "first"),
                                    _engine(fake, tmp_path).edit(CHAT, GUID, "second"))

    loop = uvloop.new_event_loop()
    try:
        results = loop.run_until_complete(both())
    finally:
        loop.close()
    assert [r.ok for r in results] == [True, True]
    (_, first_done), (second_started, _) = sorted(fake.spans)
    assert second_started >= first_done


def test_a_loop_that_cannot_be_weakly_referenced_still_gets_one_lock(monkeypatch):
    class NoWeakrefs(dict):
        def get(self, key):
            raise TypeError("cannot create weak reference")

    monkeypatch.setattr(cli, "_LOCKS", NoWeakrefs())
    monkeypatch.setattr(cli, "_LOCKS_BY_ID", {})

    async def twice():
        return cli._lock(), cli._lock()

    first, second = sync(twice())
    assert first is second and isinstance(first, asyncio.Lock)


# ---------------------------------------------------------------------------
# imessage-cli: the Messages.app instance count
# ---------------------------------------------------------------------------

def _counting(values):
    values = list(values)
    return lambda: values.pop(0)


def test_an_extra_messages_instance_is_logged_once_and_nothing_is_killed(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(os, "kill", lambda *a: killed.append(a))
    monkeypatch.setattr(os, "killpg", lambda *a: killed.append(a))
    fake = make_fake_cli(tmp_path)
    lines: list[str] = []
    # before, after, after again (the recount): still one more than before
    engine = _engine(fake, tmp_path, count=_counting([1, 2, 2]), log=lines.append, recount_delay=0.01)
    assert sync(engine.edit(CHAT, GUID, TEXT)).ok
    assert lines == ["[imessage-cli] 1 extra Messages instance(s) left running"]
    lines.clear()
    engine = _engine(fake, tmp_path, count=_counting([1, 4, 3]), log=lines.append, recount_delay=0.01)
    assert sync(engine.edit(CHAT, GUID, TEXT)).ok
    assert lines == ["[imessage-cli] 2 extra Messages instance(s) left running"]
    assert killed == []
    # the same after a failure
    lines.clear()
    fake.configure(mode="exit", code=1)
    engine = _engine(fake, tmp_path, count=_counting([0, 1, 1]), log=lines.append, recount_delay=0.01)
    assert not sync(engine.edit(CHAT, GUID, TEXT)).ok
    assert lines == ["[imessage-cli] 1 extra Messages instance(s) left running"]
    assert killed == []


@pytest.mark.parametrize("counts", [[1, 1], [2, 1], [1, 2, 1], [None], [1, None], [1, 2, None], [0, 0]],
                         ids=["same", "fewer", "gone-on-recount", "unknown-before", "unknown-after",
                              "unknown-on-recount", "none-running"])
def test_no_line_when_the_count_did_not_grow_or_cannot_be_told(tmp_path, counts):
    lines: list[str] = []
    engine = _engine(make_fake_cli(tmp_path), tmp_path, count=_counting(counts), log=lines.append,
                     recount_delay=0.01)
    assert sync(engine.edit(CHAT, GUID, TEXT)).ok
    assert lines == []


def test_a_counter_that_raises_is_the_same_as_not_knowing(tmp_path):
    def broken():
        raise OSError("synthetic")

    lines: list[str] = []
    engine = _engine(make_fake_cli(tmp_path), tmp_path, count=broken, log=lines.append)
    assert sync(engine.edit(CHAT, GUID, TEXT)).ok and lines == []


def test_the_default_counter_only_lists_processes(monkeypatch):
    monkeypatch.undo()                                               # the real function, not the constant
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw.get("stdin")))
        return run.answer

    monkeypatch.setattr(cli.subprocess, "run", run)
    run.answer = subprocess.CompletedProcess([], 0, stdout="501\n77012\n", stderr="")
    assert cli.count_messages_instances() == 2
    run.answer = subprocess.CompletedProcess([], 1, stdout="", stderr="")            # pgrep: no such process
    assert cli.count_messages_instances() == 0
    run.answer = subprocess.CompletedProcess([], 2, stdout="", stderr="pgrep: bad usage")
    assert cli.count_messages_instances() is None
    assert seen == [(["/usr/bin/pgrep", "-x", "Messages"], subprocess.DEVNULL)] * 3

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(cli.subprocess, "run", missing)
    assert cli.count_messages_instances() is None


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------

def test_build_chain_adds_the_edit_engine_last_and_only_when_it_is_handed_one(tmp_path):
    fake = make_fake_cli(tmp_path)
    edit_engine = _engine(fake, tmp_path)
    assert DEFAULT_ORDER == ("beeper", "bluebubbles", "applescript", "imessage-cli")
    env = {"BEEPER_TOKEN": "t", "BB_PASSWORD": "x"}
    chain = build_chain(env, imessage_cli=edit_engine)
    assert engine_names(chain) == ["beeper", "bluebubbles", "applescript", "imessage-cli"]
    assert chain[-1] is edit_engine
    assert engine_names(build_chain({}, imessage_cli=edit_engine)) == ["applescript", "imessage-cli"]
    assert engine_names(build_chain({"SEND_APPLESCRIPT_FALLBACK": "0"}, imessage_cli=edit_engine)) == ["imessage-cli"]
    # not handed one, or one without a binary: the chain every caller got before step R6,
    # whether or not the tool is installed on the machine that runs this
    assert engine_names(build_chain(env)) == ["beeper", "bluebubbles", "applescript"]
    assert engine_names(build_chain(env, imessage_cli=ImessageCliEngine(None))) == \
        ["beeper", "bluebubbles", "applescript"]
    assert engine_names(build_chain({})) == ["applescript"]
    assert engine_names(build_chain({"IMESSAGE_CLI": str(fake.binary)})) == ["applescript"]   # not read from env


def test_send_engines_may_place_the_edit_engine_but_cannot_leave_it_out(tmp_path):
    """``SEND_ENGINES`` orders the engines that send. A list written before
    the edit engine existed must not switch editing off without a word, and
    cannot change any delivery by having it: the engine joins at the end
    unless the list gave it a place. ``IMESSAGE_CLI=0`` is its switch."""
    edit_engine = _engine(make_fake_cli(tmp_path), tmp_path)
    env = {"BB_PASSWORD": "x"}
    names = lambda send_engines, **kw: engine_names(  # noqa: E731
        build_chain({**env, "SEND_ENGINES": send_engines}, imessage_cli=edit_engine, **kw))
    assert names("imessage-cli,bluebubbles") == ["imessage-cli", "bluebubbles"]
    assert names("bluebubbles, applescript, imessage-cli") == ["bluebubbles", "applescript", "imessage-cli"]
    assert names("bluebubbles,applescript") == ["bluebubbles", "applescript", "imessage-cli"]
    assert names("applescript") == ["applescript", "imessage-cli"]
    assert names("imessage-cli,imessage-cli") == ["imessage-cli"]
    # what the list does decide is untouched: who sends, and in which order
    pinned = build_chain({**env, "SEND_ENGINES": "applescript,bluebubbles"}, imessage_cli=edit_engine)
    assert [e.name for e in pinned if Capability.TEXT in e.capabilities] == ["applescript", "bluebubbles"]
    assert first_with(pinned, Capability.EDIT, CHAT) is edit_engine
    # off means off, with or without a list
    off = ImessageCliEngine(find_binary("0", candidates=(str(edit_engine.binary),)))
    assert engine_names(build_chain({**env, "SEND_ENGINES": "bluebubbles,applescript"}, imessage_cli=off)) == \
        ["bluebubbles", "applescript"]
    assert engine_names(build_chain({**env, "SEND_ENGINES": "bluebubbles,applescript"})) == \
        ["bluebubbles", "applescript"]                               # no engine handed in: the chain it always was
    # named but not installed: left out, like any engine that is not configured
    assert engine_names(build_chain({**env, "SEND_ENGINES": "bluebubbles,imessage-cli"})) == ["bluebubbles"]
    with pytest.raises(ValueError) as info:
        build_chain({"SEND_ENGINES": "imessage_cli"})
    assert "known: beeper, bluebubbles, applescript, imessage-cli" in str(info.value)


def test_each_change_goes_to_its_own_engine_and_nothing_else_reaches_the_edit_engine(tmp_path):
    fake = make_fake_cli(tmp_path)
    bb = BB((200, {"status": 200, "message": "Message unsent!"}), (200, {"status": 200, "data": {}}))
    osa_calls = []
    chain = build_chain({"BB_PASSWORD": PASSWORD, "BB_URL": BB_URL},
                        applescript=AppleScriptEngine(runner=lambda *a: osa_calls.append(a) or True,
                                                      outbox=tmp_path / "outbox"),
                        bluebubbles_transport=httpx.MockTransport(bb),
                        imessage_cli=_engine(fake, tmp_path))
    assert engine_names(chain) == ["bluebubbles", "applescript", "imessage-cli"]
    assert first_with(chain, Capability.EDIT, CHAT).name == "imessage-cli"
    assert first_with(chain, Capability.UNSEND, CHAT).name == "bluebubbles"
    assert first_with(chain, Capability.EDIT, BP_CHAT) is None and first_with(chain, Capability.UNSEND, BP_CHAT) is None

    res = sync(deliver(chain, Capability.EDIT, CHAT, GUID, TEXT, part_index=0))
    assert res == SendResult(True, "imessage-cli")
    assert bb.seen == [] and len(fake.calls) == 1                    # BlueBubbles' edit is never tried
    res = sync(deliver(chain, Capability.UNSEND, CHAT, GUID, part_index=0))
    assert res == SendResult(True, "bb")
    assert len(fake.calls) == 1                                      # the tool's undo-send is never tried
    assert sync(deliver(chain, Capability.TEXT, CHAT, TEXT)).via == "bb"
    assert len(fake.calls) == 1 and osa_calls == []                  # existing deliveries are what they were
    assert [path for _, path, _, _ in bb.seen] == [f"/api/v1/message/{GUID}/unsend", "/api/v1/message/text"]

    for cap in (Capability.EDIT, Capability.UNSEND):
        with pytest.raises(DeliveryError) as info:
            sync(deliver(chain, cap, BP_CHAT, GUID, *([TEXT] if cap is Capability.EDIT else []), part_index=0))
        assert info.value.status == 501
    with pytest.raises(DeliveryError) as info:
        sync(deliver(chain[1:], Capability.UNSEND, CHAT, GUID, part_index=0))
    assert (info.value.status, info.value.detail) == (501, "no configured engine can unsend messages in this chat")
    with pytest.raises(DeliveryError) as info:
        sync(deliver(chain[:2], Capability.EDIT, CHAT, GUID, TEXT, part_index=0))
    assert (info.value.status, info.value.detail) == (501, "no configured engine can edit messages in this chat")


def test_every_engine_satisfies_the_protocol_and_refuses_what_it_cannot_do(tmp_path):
    """``SendEngine`` is runtime-checkable and gained ``unsend`` and ``edit``:
    each engine has both, and all but the one that holds the capability
    raise ``Unsupported`` from them rather than ``AttributeError``."""
    engines_ = (AppleScriptEngine(), BeeperEngine("t"), BlueBubblesEngine(BB_URL, PASSWORD),
                _engine(make_fake_cli(tmp_path), tmp_path), ImessageCliEngine(None))
    for engine in engines_:
        assert isinstance(engine, SendEngine), engine.name
    for engine, words in ((AppleScriptEngine(), "applescript"), (BeeperEngine("t"), "beeper")):
        assert not {Capability.UNSEND, Capability.EDIT} & engine.capabilities
        with pytest.raises(Unsupported) as info:
            sync(engine.unsend(CHAT, GUID))
        assert info.value.detail == f"{words} cannot unsend messages"
        with pytest.raises(Unsupported) as info:
            sync(engine.edit(CHAT, GUID, TEXT, part_index=0))
        assert info.value.detail == f"{words} cannot edit messages"


def test_imessage_capabilities_names_what_a_chain_can_do_beyond_sending(tmp_path):
    edit_engine = _engine(make_fake_cli(tmp_path), tmp_path)
    assert [c.value for c in IMESSAGE_EXTRAS] == ["react", "reply", "create_chat", "unsend", "edit"]
    assert not BeeperEngine("t").handles(IMESSAGE_PROBE_GUID) and AppleScriptEngine().handles(IMESSAGE_PROBE_GUID)
    caps = lambda env, **kw: imessage_capabilities(build_chain(env, **kw))  # noqa: E731
    assert caps({}) == []                                            # AppleScript alone: plain sending
    assert caps({"BB_PASSWORD": "x"}) == ["create_chat", "react", "reply", "unsend"]
    assert caps({"BB_PASSWORD": "x"}, imessage_cli=edit_engine) == ["create_chat", "edit", "react", "reply", "unsend"]
    assert caps({}, imessage_cli=edit_engine) == ["edit"]
    # Beeper can reply, but only in Google Messages chats: that is not an iMessage capability
    assert caps({"BEEPER_TOKEN": "t"}) == []
    assert caps({"BEEPER_TOKEN": "t", "SEND_APPLESCRIPT_FALLBACK": "0"}) == []
    assert caps({"BB_PASSWORD": "x", "SEND_ENGINES": "applescript"}, imessage_cli=edit_engine) == ["edit"]
    assert caps({"BB_PASSWORD": "x", "SEND_ENGINES": "applescript"}) == []
    assert imessage_capabilities([]) == []


# ---------------------------------------------------------------------------
# chatdb_adapter.change_target: what the routes read before and after
# ---------------------------------------------------------------------------

def _summary(**keys) -> bytes:
    return plistlib.dumps(keys, fmt=plistlib.FMT_BINARY)


def _history(entries: int) -> dict:
    """``ec`` for part 0 with ``entries`` items: the original and ``entries - 1`` edits."""
    return {"0": [{"d": 700000000.0 + i, "t": b"synthetic typedstream"} for i in range(entries)]}


_ADAPTER_GLOBALS = ("_CDB", "_resolve", "_att_public", "_person_key", "_group_title", "_self_raw")
_UNSET = object()


@pytest.fixture
def bind_adapter():
    """``bind(path)`` points the shared adapter module at a synthetic database
    with plain hooks (no relay module involved); whatever it was bound to
    before, the relay's own hooks included, is put back afterwards."""
    saved = {name: getattr(chatdb_adapter, name, _UNSET) for name in _ADAPTER_GLOBALS}

    def bind(path) -> None:
        chatdb_adapter.configure(chatdb_path=str(path), resolve=lambda h: h,
                                 att_public=lambda g, m, n: (m, n, f"/attachment/{g}"),
                                 person_key=lambda a: a, group_title=lambda c, r, d: d, self_raw=[])

    yield bind
    for name, value in saved.items():
        if value is _UNSET:
            if hasattr(chatdb_adapter, name):
                delattr(chatdb_adapter, name)
        else:
            setattr(chatdb_adapter, name, value)


@pytest.fixture
def adapter_db(make_db, bind_adapter):
    """A macos27 database with the ``message_summary_info`` column, bound to
    the adapter."""
    fx = make_db("macos27", name="change.db")
    w = fx.writer
    w.execute("ALTER TABLE message ADD COLUMN message_summary_info BLOB")
    chat = builders.add_chat(w, CHAT, 45, "+15550004242", handles=["+15550004242"])
    other = builders.add_chat(w, GROUP, 43, "chat900900900", handles=["+15550004242", "+15550005555"])
    rows = {
        "plain": builders.add_message(w, chat, guid=GUID, text=TEXT, is_from_me=1, date_ns=700_000_000_000_000_000),
        "theirs": builders.add_message(w, chat, guid="THEIRS", text="hello", handle="+15550004242"),
        "blob": builders.add_message(w, chat, guid="BLOB", body=encode_attributed_body("blob only"), is_from_me=1),
        "sms": builders.add_message(w, chat, guid="SMS", text="a text", is_from_me=1, service="SMS"),
        "unsent": builders.add_message(w, chat, guid="UNSENT", text=None, is_from_me=1, date_edited=5),
        "edited": builders.add_message(w, chat, guid="EDITED", text="v3", is_from_me=1, date_edited=7),
        "photo": builders.add_message(w, chat, guid="PHOTO", text="￼", is_from_me=1, has_att=1),
        "elsewhere": builders.add_message(w, other, guid="ELSEWHERE", text="in the group", is_from_me=1),
        "junk": builders.add_message(w, chat, guid="JUNK", text="junk summary", is_from_me=1),
        "odd": builders.add_message(w, chat, guid="ODD", text="odd summary", is_from_me=1),
        "tapback": builders.add_message(w, chat, guid="TAPBACK", text="Loved “hello”", is_from_me=1,
                                        assoc_guid="p:0/THEIRS", assoc_type=2000),
        "event": builders.add_message(w, other, guid="EVENT", text=None, is_from_me=1, item_type=2,
                                      group_title="renamed"),
    }
    for key, blob in (("unsent", _summary(rp=[0], ust=True)),
                      ("edited", _summary(ec=_history(3), ep=[0], otr={"0": {"lo": 0, "le": 2}})),
                      ("junk", b"this is not a property list"),
                      ("odd", plistlib.dumps(["a", "list"], fmt=plistlib.FMT_BINARY))):
        w.execute("UPDATE message SET message_summary_info = ? WHERE ROWID = ?", (blob, rows[key]))
    bind_adapter(fx.path)
    return fx, rows


def test_change_target_finds_a_message_only_in_the_chat_it_belongs_to(adapter_db):
    fx, rows = adapter_db
    t = chatdb_adapter.change_target(CHAT, GUID)
    assert (t.rowid, t.guid, t.chat_guid, t.is_from_me, t.text, t.service) == \
        (rows["plain"], GUID, CHAT, True, TEXT, "iMessage")
    assert (t.date, t.date_edited, t.date_retracted, t.summary) == (700_000_000_000_000_000, 0, 0, {})
    assert t.has_text and not t.is_retracted() and t.edit_count() == 0 and t.retracted_parts == ()
    assert chatdb_adapter.change_target(GROUP, GUID) is None         # the right guid, another chat
    assert chatdb_adapter.change_target(CHAT, "ELSEWHERE") is None
    assert chatdb_adapter.change_target(GROUP, "ELSEWHERE").chat_guid == GROUP
    for chat, guid in ((CHAT, "NO-SUCH-GUID"), ("any;-;+15550000000", GUID), ("", GUID), (CHAT, ""),
                       (CHAT, GUID.lower()), (CHAT, "%"), (CHAT, "' OR 1=1 --")):
        assert chatdb_adapter.change_target(chat, guid) is None, (chat, guid)


def test_change_target_reads_the_other_facts(adapter_db):
    theirs = chatdb_adapter.change_target(CHAT, "THEIRS")
    assert theirs.is_from_me is False
    assert chatdb_adapter.change_target(CHAT, "BLOB").text == "blob only"      # text column NULL: the blob
    assert chatdb_adapter.change_target(CHAT, "SMS").service == "SMS"
    photo = chatdb_adapter.change_target(CHAT, "PHOTO")
    assert photo.text == "￼" and not photo.has_text and not photo.is_retracted()   # empty, but never changed


def test_change_target_tells_a_message_from_a_tapback_and_a_group_event(adapter_db):
    """The owner's own rows too: a reaction the owner gave and a group the
    owner renamed are ``is_from_me`` rows of the message table."""
    plain = chatdb_adapter.change_target(CHAT, GUID)
    assert (plain.associated_message_type, plain.item_type, plain.is_message) == (0, 0, True)
    tapback = chatdb_adapter.change_target(CHAT, "TAPBACK")
    assert (tapback.is_from_me, tapback.associated_message_type, tapback.is_message) == (True, 2000, False)
    event = chatdb_adapter.change_target(GROUP, "EVENT")
    assert (event.is_from_me, event.item_type, event.is_message) == (True, 2, False)
    make = lambda **kw: chatdb_adapter.ChangeTarget(  # noqa: E731
        rowid=1, guid="G", chat_guid=CHAT, is_from_me=True, date=1, date_edited=0, date_retracted=0,
        text="x", service="iMessage", summary={}, **kw)
    assert make().is_message                                         # the two fields default to "a message"
    assert not make(associated_message_type=3).is_message and not make(item_type=1).is_message


def test_change_target_reads_retraction_and_edit_history_from_the_summary(adapter_db):
    unsent = chatdb_adapter.change_target(CHAT, "UNSENT")
    assert unsent.text is None and unsent.date_edited == 5 and unsent.retracted_parts == (0,)
    assert unsent.is_retracted() and unsent.is_retracted(0)
    assert not unsent.is_retracted(1)                                # Messages listed part 0 only
    edited = chatdb_adapter.change_target(CHAT, "EDITED")
    assert edited.edit_count() == 2                                  # three entries: the original and two edits
    assert edited.edit_count(1) == 0 and not edited.is_retracted()
    assert set(edited.summary) == {"ec", "ep", "otr"}


@pytest.mark.parametrize("entries, edits", [(0, 0), (1, 0), (2, 1), (5, 4), (6, 5), (7, 6)])
def test_edit_count_takes_the_first_history_entry_for_the_original(entries, edits):
    make = lambda summary: chatdb_adapter.ChangeTarget(  # noqa: E731
        rowid=1, guid="G", chat_guid=CHAT, is_from_me=True, date=1, date_edited=1, date_retracted=0,
        text="x", service="iMessage", summary=summary)
    assert make({"ec": _history(entries)}).edit_count() == edits
    assert make({"ec": {0: _history(entries)["0"]}}).edit_count() == edits      # an integer key reads the same
    assert make({}).edit_count() == 0 and make({"ec": {}}).edit_count() == 0
    assert make({"ec": {"1": [1, 2, 3]}}).edit_count() == 0 and make({"ec": {"1": [1, 2, 3]}}).edit_count(1) == 2
    for unreadable in (None, {"ec": "x"}, {"ec": ["a"]}, {"ec": {"0": "x"}}):
        assert make(unreadable).edit_count() is None, unreadable


def test_is_retracted_without_a_list_of_parts_goes_by_the_emptied_text():
    def make(**kw):
        base = dict(rowid=1, guid="G", chat_guid=CHAT, is_from_me=True, date=1, date_edited=0,
                    date_retracted=0, text="x", service="iMessage", summary={})
        return chatdb_adapter.ChangeTarget(**{**base, **kw})

    assert make(text=None, date_edited=9).is_retracted()             # macOS 27: text emptied, edit mark set
    assert make(text="", date_retracted=9, summary=None).is_retracted()
    assert make(text=" ￼ ", date_edited=9).is_retracted()
    assert not make(text=None).is_retracted()                        # a photo that was never touched
    assert not make(text="still here", date_edited=9).is_retracted()           # an edit, not an unsend
    assert make(text="caption", date_edited=9, summary={"rp": [1]}).is_retracted(1)
    assert not make(text="caption", date_edited=9, summary={"rp": [1]}).is_retracted(0)
    assert make(summary={"rp": [0, True, "1", 2.0, 3]}).retracted_parts == (0, 3)    # integers only
    assert make(summary={"rp": "x"}).retracted_parts == () and make(summary=None).retracted_parts == ()


def test_a_summary_that_is_not_a_property_list_dictionary_is_unreadable_not_an_error(adapter_db):
    for guid in ("JUNK", "ODD"):
        t = chatdb_adapter.change_target(CHAT, guid)
        assert t is not None and t.summary is None
        assert t.edit_count() is None and t.retracted_parts == () and not t.is_retracted()


def test_change_target_without_the_summary_column_and_on_older_schemas(make_db, bind_adapter):
    """The fixture profiles have no ``message_summary_info`` (the library does
    not read it); ``macos14`` also lacks ``date_retracted``. The statement
    renders what is missing as NULL and says the history is unreadable."""
    for profile in ("macos14", "macos15", "macos26", "macos27"):
        fx = make_db(profile)
        w = fx.writer
        chat = builders.add_chat(w, CHAT, 45, "+15550004242", handles=["+15550004242"])
        builders.add_message(w, chat, guid=GUID, text=TEXT, is_from_me=1)
        builders.add_message(w, chat, guid="UNSENT", text=None, is_from_me=1, date_edited=5)
        bind_adapter(fx.path)
        t = chatdb_adapter.change_target(CHAT, GUID)
        assert (t.text, t.is_from_me, t.service, t.date_retracted) == (TEXT, True, "iMessage", 0), profile
        assert t.summary is None and t.edit_count() is None and not t.is_retracted(), profile
        assert chatdb_adapter.change_target(CHAT, "UNSENT").is_retracted(), profile


def test_change_target_writes_nothing_and_opens_read_only(adapter_db):
    fx, _ = adapter_db
    before = fx.writer.execute("SELECT count(*), max(ROWID), total(date_edited) FROM message").fetchone()
    for _ in range(3):
        chatdb_adapter.change_target(CHAT, GUID)
    assert fx.writer.execute("SELECT count(*), max(ROWID), total(date_edited) FROM message").fetchone() == before
    conn = chatdb_adapter.db()
    try:
        with pytest.raises(Exception) as info:
            conn.execute("UPDATE message SET text = 'x'")
        assert "readonly" in str(info.value).lower() or "read-only" in str(info.value).lower() \
            or "query_only" in str(info.value).lower()
    finally:
        conn.close()
    assert sys.modules["chatdb_adapter"].MAX_PART_INDEX == 63
