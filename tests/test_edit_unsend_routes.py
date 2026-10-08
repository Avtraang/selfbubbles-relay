"""Step R6, the relay half: ``POST /unsend`` and ``POST /edit``.

Both routes look the message up in ``chat.db`` BEFORE any engine is asked and
again afterwards, because neither engine's "ok" can be believed on its own
(BlueBubbles' edit and ``imessage-cli``'s undo-send both report success and
change nothing on macOS 27). Pinned here:

* the shared checks and their order: unknown guid, wrong chat or a row that
  is not a message (404, one answer for all three), not the owner's (403), a
  Google Messages chat or a message that is not an iMessage (409, in words
  the app reads as a plain refusal), already unsent (409), a part index
  outside 0..63 (422);
* per route: Apple's two minutes for an unsend and fifteen for an edit, by
  the database's own date, and a date that lies ahead (409); for an edit, an
  empty, over-long or control-character text (422), the same text again
  (200 ``unchanged``, no engine asked), and five edits already made (409);
* edits one at a time, each checked again when its turn has come: the same
  edit sent twice runs the tool once, an edit whose fifteen minutes ended
  while it waited is a 409, and a queue that is full is a 409 too;
* the engine step: 501 in the chain's words when no engine in the chain can
  do it, and for nothing else (the app stops offering the action when it
  reads one); an engine that was asked and failed is a 502 with one of the
  relay's own fixed details (``imessage-cli``'s short classification,
  ``BlueBubbles refused the request``, ``BlueBubbles could not unsend the
  message``), never BlueBubbles' status and never its body;
* the confirmation: engine ok and the database unchanged is ``502 the Mac
  did not apply the change``; engine ok and the database changed is
  ``{"ok": true, "via": ...}`` (with ``"text_differs": true`` when the edit
  landed with another text); an engine that failed after it was started and
  a database that shows the change all the same is that success too;
* ``/health.capabilities`` and the doctor's ``edit / unsend`` row;
* that none of the log lines carries a message's text, a chat identifier, a
  message guid or the BlueBubbles password.

BlueBubbles is the recording fake of ``tests/test_send_path.py``;
``imessage-cli`` is the fake of ``tests/fake_imessage_cli.py``, a script under
``tmp_path``. The database is a synthetic one built for each test. The whole
file skips on a relay module without step R6 (``relay.py`` until the swap).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import plistlib
import sqlite3
import threading
import time
import zlib
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import engines.imessage_cli as cli
from engines import SendResult
from imessage_chatdb import unix_to_apple
from tests.conftest import RELAY_STUB_ENV
from tests.fake_imessage_cli import assert_fake, make_fake_cli
from tests.fixtures import builders
from tests.fixtures.typedstream_writer import encode_attributed_body
from tests.test_relay_compat import relay_state  # noqa: F401  (autouse snapshot/restore)
from tests.test_send_path import BBRecorder, FakeResponse

AUTH = {"X-Imsg-Token": RELAY_STUB_ENV["IMSG_TOKEN"]}

PHONE = "+15550004242"
CHAT = f"any;-;{PHONE}"                                        # the relay's own guid form
OTHER_CHAT = "any;-;quokka.sender@example.test"
GROUP = "any;+;chat900900900"
BP_CHAT = "bp:42"
OLD_TEXT = "ZEBRA-QUOKKA-7731 meet at the pier"
NEW_TEXT = 'BLUE-HERON-4410 meet "at the pier" at 9\\30, bring it'

UNKNOWN = {"detail": "unknown message"}
NOT_YOURS = {"detail": "only your own messages can be changed"}
UNSUPPORTED = {"detail": "this chat cannot edit or unsend"}
ALREADY = {"detail": "already unsent"}
NOT_APPLIED = {"detail": "the Mac did not apply the change"}
REFUSED = {"detail": "BlueBubbles refused the request"}                 # it answered 4xx
COULD_NOT = {"detail": "BlueBubbles could not unsend the message"}      # no answer or a 5xx, and no change
EDIT_FAILED = {"detail": "the edit engine failed"}
NO_UNSEND_ENGINE = {"detail": "no configured engine can unsend messages in this chat"}
NO_EDIT_ENGINE = {"detail": "no configured engine can edit messages in this chat"}
NOT_SENT_YET = {"detail": "this message has not been sent yet"}
BUSY = {"detail": "another edit is still running on the Mac, try again in a moment"}
UNCHANGED = {"ok": True, "via": None, "unchanged": True}

#: What a log line must never carry. ``guids`` of the fixture are added per test.
SECRETS = (PHONE, CHAT, OTHER_CHAT, "quokka.sender", GROUP, "chat900900900", OLD_TEXT, NEW_TEXT,
           "ZEBRA-QUOKKA", "BLUE-HERON", "at the pier", RELAY_STUB_ENV["BB_PASSWORD"],
           RELAY_STUB_ENV["IMSG_TOKEN"])


def guid_for(name: str) -> str:
    """A synthetic message guid, UUID-shaped like chat.db's; the same for the
    same name in every run."""
    return f"5EED{zlib.crc32(name.encode()) % 0xFFFF:04X}-0000-4000-8000-{name.upper().ljust(12, '0')[:12]}"


def _summary(**keys) -> bytes:
    return plistlib.dumps(keys, fmt=plistlib.FMT_BINARY)


def _history(entries: int) -> dict:
    return {"0": [{"d": 700000000.0 + i, "t": b"synthetic"} for i in range(entries)]}


def _ago(seconds: float) -> int:
    """The raw chat.db date of a message sent ``seconds`` ago."""
    return unix_to_apple(time.time() - seconds)


#: name -> (chat, age in seconds, add_message keywords, message_summary_info or None)
MESSAGES = {
    "fresh":       (CHAT, 30, dict(text=OLD_TEXT, is_from_me=1), None),
    "fresh2":      (CHAT, 40, dict(text="second fresh one", is_from_me=1), None),
    "min3":        (CHAT, 180, dict(text=OLD_TEXT, is_from_me=1), None),        # too old to unsend, not to edit
    "min16":       (CHAT, 960, dict(text=OLD_TEXT, is_from_me=1), None),        # too old for both
    "undated":     (CHAT, None, dict(text=OLD_TEXT, is_from_me=1), None),       # date 0
    "theirs":      (CHAT, 20, dict(text="from the other side", is_from_me=0, handle=PHONE), None),
    "sms":         (CHAT, 20, dict(text="a text message", is_from_me=1, service="SMS"), None),
    "rcs":         (CHAT, 20, dict(text="an RCS message", is_from_me=1, service="RCS"), None),
    "noservice":   (CHAT, 20, dict(text="service unknown", is_from_me=1, service=None), None),
    "satellite":   (CHAT, 20, dict(text="sent by satellite", is_from_me=1, service="iMessageLite"), None),
    "unsent":      (CHAT, 25, dict(text=None, is_from_me=1, date_edited=_ago(5)), _summary(rp=[0])),
    "unsent_bare": (CHAT, 25, dict(text=None, is_from_me=1, date_edited=_ago(5)), None),
    "theirs_gone": (CHAT, 25, dict(text=None, is_from_me=0, handle=PHONE, date_edited=_ago(5)), _summary(rp=[0])),
    "edited4":     (CHAT, 60, dict(text="fourth version", is_from_me=1, date_edited=_ago(9)),
                    _summary(ec=_history(5), ep=[0])),
    "edited5":     (CHAT, 60, dict(text="fifth version", is_from_me=1, date_edited=_ago(9)),
                    _summary(ec=_history(6), ep=[0])),
    "junk":        (CHAT, 30, dict(text="unreadable history", is_from_me=1), b"not a property list"),
    "blob":        (CHAT, 30, dict(text=None, body=encode_attributed_body(OLD_TEXT), is_from_me=1), None),
    "photo":       (CHAT, 30, dict(text="￼", is_from_me=1, has_att=1), None),
    "elsewhere":   (OTHER_CHAT, 30, dict(text=OLD_TEXT, is_from_me=1), None),
    "group":       (GROUP, 30, dict(text=OLD_TEXT, is_from_me=1), None),
    # a date ahead of the clock: by a day (Send Later keeps the send time there), and by seconds (two clocks)
    "future":      (CHAT, -86400, dict(text=OLD_TEXT, is_from_me=1), None),
    "soon":        (CHAT, -20, dict(text=OLD_TEXT, is_from_me=1), None),
    # the owner's own rows that are not messages: a reaction the owner gave, a group the owner renamed
    "tapback":     (CHAT, 10, dict(text="Loved an earlier message", is_from_me=1, assoc_type=2000,
                                   assoc_guid="p:0/5EED0000-0000-4000-8000-000000000000"), None),
    "event":       (GROUP, 10, dict(text=None, is_from_me=1, item_type=2, group_title="renamed"), None),
}


@pytest.fixture
def r(r6):
    return r6


@pytest.fixture
def db(r, make_db, relay_module, monkeypatch, tmp_path):
    """The synthetic database (macos27 plus ``message_summary_info``), the
    adapter bound to it with the relay's own hooks, the confirmation waits cut
    to fractions of a second, the Messages instance count a constant and the
    Accessibility question answered yes without being put to the system."""
    monkeypatch.setattr(r, "CHANGE_CONFIRM_SECONDS", 0.4)
    monkeypatch.setattr(r, "CHANGE_CONFIRM_INTERVAL", 0.02)
    monkeypatch.setattr(r, "CHANGE_RECHECK_SECONDS", 0.15)
    monkeypatch.setattr(r, "CHANGE_SETTLE_SECONDS", 0.06)
    monkeypatch.setattr(r, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cli, "count_messages_instances", lambda: 1)
    monkeypatch.setattr(cli, "accessibility_trusted", lambda: True)
    fx = make_db("macos27", name="changes.db")
    w = fx.writer
    w.execute("ALTER TABLE message ADD COLUMN message_summary_info BLOB")
    chats = {CHAT: builders.add_chat(w, CHAT, 45, PHONE, handles=[PHONE]),
             OTHER_CHAT: builders.add_chat(w, OTHER_CHAT, 45, "quokka.sender@example.test",
                                           handles=["quokka.sender@example.test"]),
             GROUP: builders.add_chat(w, GROUP, 43, "chat900900900", handles=[PHONE, "+15550005555"])}
    rowids, guids = {}, {}
    for name, (chat, age, kw, summary) in MESSAGES.items():
        guids[name] = guid_for(name)
        rowids[name] = builders.add_message(w, chats[chat], guid=guids[name],
                                            date_ns=0 if age is None else _ago(age), **kw)
        if summary is not None:
            w.execute("UPDATE message SET message_summary_info = ? WHERE ROWID = ?", (summary, rowids[name]))
    relay_module.configure(fx.path)

    def execute(sql: str, params: tuple = ()) -> list:
        """One statement on a connection of its own: the BlueBubbles fake's
        ``effect`` runs on the test client's thread, where the fixture's
        writer connection may not be used."""
        conn = sqlite3.connect(fx.path, isolation_level=None, timeout=5)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def row(name: str) -> dict:
        (got,) = execute("SELECT text, attributedBody, date_edited, message_summary_info FROM message "
                         "WHERE ROWID = ?", (rowids[name],))
        return {"text": got[0], "body": got[1], "date_edited": got[2], "summary": got[3]}

    def mark_unsent(name: str, part: int = 0) -> None:
        """What Messages wrote for the real unsend: text emptied, the edit
        mark set, the part listed under ``rp`` (date_retracted untouched)."""
        execute("UPDATE message SET text = NULL, attributedBody = NULL, date_edited = ?, "
                "message_summary_info = ? WHERE ROWID = ?", (_ago(0), _summary(rp=[part]), rowids[name]))

    return SimpleNamespace(fx=fx, w=w, guids=guids, rowids=rowids, row=row, mark_unsent=mark_unsent,
                           execute=execute, path=fx.path)


class BBWithEffect(BBRecorder):
    """The recording BlueBubbles fake, plus ``effect``: called after a 2xx
    answer, so a test can make the database show what a real unsend writes.
    ``effect_on_failure`` is called when the scripted answer is an error
    status or a transport failure: BlueBubbles carried the unsend out and
    its answer was lost, or was a 5xx."""

    effect = None
    effect_on_failure = None

    def client_factory(self):
        base, outer = super().client_factory(), self

        class _Client(base):
            async def _call(self, method, url, **kw):
                try:
                    response = await super()._call(method, url, **kw)
                except AssertionError:
                    raise
                except Exception:
                    if outer.effect_on_failure is not None:
                        outer.effect_on_failure(outer.calls[-1])
                    raise
                if outer.effect is not None and response.status_code < 400:
                    outer.effect(outer.calls[-1])
                if outer.effect_on_failure is not None and response.status_code >= 400:
                    outer.effect_on_failure(outer.calls[-1])
                return response

        return _Client


@pytest.fixture
def bb(r, monkeypatch) -> BBWithEffect:
    rec = BBWithEffect()
    monkeypatch.setattr(r.httpx, "AsyncClient", rec.client_factory())
    return rec


@pytest.fixture
def fake_cli(r, monkeypatch, tmp_path, db):
    """The fake ``imessage-cli`` wired in as the relay's edit engine. Its
    ``apply`` mode writes the edit into the synthetic database."""
    fake = make_fake_cli(tmp_path, "apply", db=str(db.path))
    assert_fake(fake.binary, tmp_path)
    # what the module holds after an import with IMESSAGE_CLI=<that path>
    monkeypatch.setattr(r, "IMESSAGE_CLI", str(fake.binary))
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", str(fake.binary))
    return fake


@pytest.fixture
def osa(r, monkeypatch, tmp_path) -> list:
    """``osascript`` must never run for these routes."""
    calls: list = []
    monkeypatch.setattr(r, "_applescript", lambda *a: calls.append(a) or False)
    monkeypatch.setattr(r, "OUTBOX", tmp_path / "outbox")
    return calls


@pytest.fixture
def client(r) -> TestClient:
    """ASGI test client; no ``with``, so startup hooks (poll loop, FCM) never run."""
    return TestClient(r.app)


def _unsend(client, chat, guid, **extra):
    return client.post("/unsend", json={"chat_guid": chat, "guid": guid, **extra}, headers=AUTH)


def _edit(client, chat, guid, text=NEW_TEXT, **extra):
    return client.post("/edit", json={"chat_guid": chat, "guid": guid, "text": text, **extra}, headers=AUTH)


def _call(route, client, chat, guid, **extra):
    return _unsend(client, chat, guid, **extra) if route == "unsend" else _edit(client, chat, guid, **extra)


def assert_clean(db, *texts: str) -> None:
    for text in texts:
        for secret in (*SECRETS, *db.guids.values()):
            assert secret not in text, secret


ROUTES = ("unsend", "edit")


# ---------------------------------------------------------------------------
# the suite's own safety: the real tool is nowhere near
# ---------------------------------------------------------------------------

def test_the_stub_environment_switches_the_edit_engine_off(r):
    assert RELAY_STUB_ENV["IMESSAGE_CLI"] == "0"
    assert r.IMESSAGE_CLI == "0" and r.IMESSAGE_CLI_BIN is None
    assert "imessage-cli" not in r.engine_names(r._chain())
    assert not r._imessage_cli().configured()


def test_the_fake_is_under_tmp_path_and_so_is_the_tools_data_directory(r, fake_cli, tmp_path):
    engine = r._imessage_cli()
    assert engine.binary == str(fake_cli.binary) and str(tmp_path) in engine.binary
    assert engine.data_dir() == tmp_path / "data" / "imessage-cli"
    assert r.engine_names(r._chain()) == ["bluebubbles", "applescript", "imessage-cli"]


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
def test_both_routes_need_the_token(route, db, bb, fake_cli, osa, client):
    body = {"chat_guid": CHAT, "guid": db.guids["fresh"], "text": NEW_TEXT}
    for headers in ({}, {"X-Imsg-Token": "wrong"}):
        resp = client.post(f"/{route}", json=body, headers=headers)
        assert (resp.status_code, resp.text) == (401, "unauthorized")
    assert bb.calls == [] and fake_cli.calls == [] and osa == []
    assert db.row("fresh")["text"] == OLD_TEXT


@pytest.mark.parametrize("route", ROUTES)
def test_a_body_without_the_fields_is_a_422(route, db, bb, fake_cli, client):
    for body in ({}, {"chat_guid": CHAT}, {"guid": db.guids["fresh"], "text": NEW_TEXT},
                 {"chat_guid": CHAT, "guid": db.guids["fresh"], "text": NEW_TEXT, "part_index": "x"},
                 {"chat_guid": CHAT, "guid": 5, "text": NEW_TEXT}):
        assert client.post(f"/{route}", json=body, headers=AUTH).status_code == 422, body
    assert client.post("/edit", json={"chat_guid": CHAT, "guid": db.guids["fresh"]}, headers=AUTH).status_code == 422
    assert bb.calls == [] and fake_cli.calls == []


# ---------------------------------------------------------------------------
# the shared checks, in order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
def test_unknown_guid_and_wrong_chat_are_the_same_404(route, db, bb, fake_cli, osa, client):
    cases = [(CHAT, guid_for("nosuchmessage")),            # no such guid
             (CHAT, db.guids["elsewhere"]),                # a real guid, of another chat
             (OTHER_CHAT, db.guids["fresh"]),
             ("any;-;+15550000000", db.guids["fresh"]),    # no such chat
             (CHAT, ""), ("", db.guids["fresh"]), (CHAT, db.guids["fresh"].lower()),
             (CHAT, "../../message/text"), (CHAT, "latest"), (CHAT, "--json")]
    for chat, guid in cases:
        resp = _call(route, client, chat, guid)
        assert (resp.status_code, resp.json()) == (404, UNKNOWN), (chat, guid)
    assert bb.calls == [] and fake_cli.calls == [] and osa == []


@pytest.mark.parametrize("route", ROUTES)
def test_a_row_that_is_not_a_message_is_the_same_404(route, r, db, bb, fake_cli, osa, client):
    """A tapback the owner gave and a group the owner renamed are the owner's
    own rows of the message table, ten seconds old, on iMessage: every other
    check would pass. Messages offers neither change for them, and no engine
    is pointed at one."""
    for chat, name in ((CHAT, "tapback"), (GROUP, "event")):
        target = r.chatdb_adapter.change_target(chat, db.guids[name])
        assert target is not None and target.is_from_me and not target.is_message
        resp = _call(route, client, chat, db.guids[name])
        assert (resp.status_code, resp.json()) == (404, UNKNOWN), name
    assert bb.calls == [] and fake_cli.calls == [] and osa == []


@pytest.mark.parametrize("route", ROUTES)
def test_someone_elses_message_is_a_403(route, db, bb, fake_cli, client):
    for name in ("theirs", "theirs_gone"):                 # 403 comes before "already unsent"
        resp = _call(route, client, CHAT, db.guids[name])
        assert (resp.status_code, resp.json()) == (403, NOT_YOURS), name
    assert bb.calls == [] and fake_cli.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_a_message_that_is_not_an_imessage_is_a_409(route, db, bb, fake_cli, client):
    """Not a 501: that one says "no engine can do this at all", and the app
    stops offering the action for the session when it reads it."""
    for name in ("sms", "rcs", "noservice"):
        resp = _call(route, client, CHAT, db.guids[name])
        assert (resp.status_code, resp.json()) == (409, UNSUPPORTED), name
    assert bb.calls == [] and fake_cli.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_a_google_messages_chat_is_a_409_whatever_the_guid(route, r, db, bb, fake_cli, client, monkeypatch):
    """A ``bp:`` chat is answered before the lookup: its messages are not in
    chat.db, so each of them would otherwise read as "unknown message"."""
    for token in ("", "stub-beeper-token-not-real"):
        monkeypatch.setattr(r.beeper, "BEEPER_TOKEN", token)
        for guid in (db.guids["fresh"], "12345", ""):
            resp = _call(route, client, BP_CHAT, guid)
            assert (resp.status_code, resp.json()) == (409, UNSUPPORTED), (token, guid)
    assert bb.calls == [] and fake_cli.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_an_unsent_message_is_a_409(route, db, bb, fake_cli, client):
    for name in ("unsent", "unsent_bare"):                 # with the rp list, and by the emptied text alone
        resp = _call(route, client, CHAT, db.guids[name])
        assert (resp.status_code, resp.json()) == (409, ALREADY), name
    # 409 comes before the part index is looked at
    resp = _call(route, client, CHAT, db.guids["unsent_bare"], part_index=99)
    assert (resp.status_code, resp.json()) == (409, ALREADY)
    assert bb.calls == [] and fake_cli.calls == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("part", [-1, 64, 1000, -64])
def test_a_part_index_outside_0_to_63_is_a_422(route, part, db, bb, fake_cli, client):
    resp = _call(route, client, CHAT, db.guids["fresh"], part_index=part)
    assert (resp.status_code, resp.json()) == (422, {"detail": "part_index must be between 0 and 63"})
    assert bb.calls == [] and fake_cli.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_the_checks_come_in_the_documented_order(route, db, bb, fake_cli, client):
    bad_part = {"part_index": 99}
    assert _call(route, client, CHAT, guid_for("nosuch"), **bad_part).status_code == 404     # 404 before 422
    assert _call(route, client, CHAT, db.guids["theirs"], **bad_part).status_code == 403     # 403 before 422
    resp = _call(route, client, CHAT, db.guids["sms"], **bad_part)                           # "cannot" before 422
    assert (resp.status_code, resp.json()) == (409, UNSUPPORTED)
    assert _call(route, client, CHAT, db.guids["min16"], **bad_part).status_code == 422      # 422 before "too late"
    assert bb.calls == [] and fake_cli.calls == []


def test_part_63_is_inside_the_range_and_reaches_bluebubbles(db, bb, fake_cli, client):
    bb.answers = [FakeResponse(200, {"status": 200, "message": "Message unsent!"})]
    bb.effect = lambda call: db.mark_unsent("fresh", 63)
    resp = _unsend(client, CHAT, db.guids["fresh"], part_index=63)
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "bb"})
    assert bb.calls[0].json == {"partIndex": 63}


@pytest.mark.parametrize("route", ROUTES)
def test_a_database_that_cannot_be_read_asks_no_engine(route, r, db, bb, fake_cli, client, monkeypatch, capsys):
    def busy(chat_guid, guid):
        raise RuntimeError(f"synthetic: database is locked ({chat_guid} {guid})")

    monkeypatch.setattr(r.chatdb_adapter, "change_target", busy)
    resp = _call(route, client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (503, {"detail": "the Messages database could not be read"})
    assert bb.calls == [] and fake_cli.calls == []
    out = capsys.readouterr().out
    assert out == "[change] chat.db could not be read (RuntimeError)\n"
    assert_clean(db, out)


# ---------------------------------------------------------------------------
# /unsend
# ---------------------------------------------------------------------------

def test_unsend_asks_bluebubbles_and_answers_once_the_database_shows_it(r, db, bb, fake_cli, osa, client, capsys):
    bb.answers = [FakeResponse(200, {"status": 200, "message": "Message unsent!"})]
    bb.effect = lambda call: db.mark_unsent("fresh")
    resp = _unsend(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "bb"})
    call, = bb.calls
    assert (call.method, call.path, call.param_keys) == \
        ("POST", f"/api/v1/message/{db.guids['fresh']}/unsend", ["password"])
    assert call.url.startswith(r.BB_URL + "/") and call.json == {"partIndex": 0} and call.timeout == 15
    assert fake_cli.calls == [] and osa == []              # the tool's undo-send is never used
    out, err = capsys.readouterr()
    assert out == "[unsend] bb: confirmed in chat.db\n"
    assert_clean(db, out, err)
    # and now it is gone: asking again is a 409 without another call
    resp = _unsend(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (409, ALREADY)
    assert len(bb.calls) == 1


def test_unsend_that_bluebubbles_accepts_but_messages_ignores_is_a_502(db, bb, fake_cli, client, capsys):
    """The false success: 200 from the engine, nothing in the database."""
    bb.answers = [FakeResponse(200, {"status": 200, "message": "Message unsent!"})]
    started = time.monotonic()
    resp = _unsend(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (502, NOT_APPLIED)
    assert time.monotonic() - started >= 0.4               # it did wait for the change
    assert len(bb.calls) == 1 and db.row("fresh")["text"] == OLD_TEXT
    out, err = capsys.readouterr()
    assert out == "[unsend] bb: reported ok, but chat.db did not change\n"
    assert_clean(db, out, err)


def test_unsend_of_a_photo_is_not_confirmed_by_text_that_was_empty_all_along(db, bb, client):
    bb.answers = [FakeResponse(200, {"status": 200}), FakeResponse(200, {"status": 200})]
    resp = _unsend(client, CHAT, db.guids["photo"])
    assert (resp.status_code, resp.json()) == (502, NOT_APPLIED)
    bb.effect = lambda call: db.mark_unsent("photo")
    resp = _unsend(client, CHAT, db.guids["photo"])
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "bb"})


def test_unsend_is_confirmed_by_any_of_the_three_marks(r, db, bb, client):
    """The part listed under ``rp`` (macOS 27), ``date_retracted`` moving
    (what the library documents for macOS 26), or the text gone from a row
    whose edit mark moved (a database without the summary)."""
    effects = {
        "fresh": lambda call: db.execute("UPDATE message SET message_summary_info = ? WHERE ROWID = ?",
                                         (_summary(rp=[0]), db.rowids["fresh"])),
        "fresh2": lambda call: db.execute("UPDATE message SET date_retracted = ? WHERE ROWID = ?",
                                          (_ago(0), db.rowids["fresh2"])),
        "blob": lambda call: db.execute("UPDATE message SET attributedBody = NULL, date_edited = ? "
                                        "WHERE ROWID = ?", (_ago(0), db.rowids["blob"])),
    }
    for name, effect in effects.items():
        bb.answers = [FakeResponse(200, {"status": 200})]
        bb.effect = effect
        resp = _unsend(client, CHAT, db.guids[name])
        assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "bb"}), name
    # an EDIT arriving in that window is not an unsend
    bb.answers = [FakeResponse(200, {"status": 200})]
    bb.effect = lambda call: db.execute("UPDATE message SET text = 'edited elsewhere', date_edited = ? "
                                        "WHERE ROWID = ?", (_ago(0), db.rowids["group"]))
    assert _unsend(client, GROUP, db.guids["group"]).status_code == 502


def test_unsend_later_than_two_minutes_is_a_409(db, bb, fake_cli, client):
    for name in ("min3", "min16", "undated"):
        resp = _unsend(client, CHAT, db.guids[name])
        assert (resp.status_code, resp.json()) == \
            (409, {"detail": "too late to unsend (Apple allows 2 minutes)"}), name
    assert bb.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_a_message_dated_in_the_future_is_a_409_of_its_own(route, db, bb, fake_cli, client):
    """A negative age is not "recent": the row has not gone out yet."""
    resp = _call(route, client, CHAT, db.guids["future"])
    assert (resp.status_code, resp.json()) == (409, NOT_SENT_YET)
    assert bb.calls == [] and fake_cli.calls == []
    # the other checks still come first
    assert _call(route, client, CHAT, db.guids["future"], part_index=99).status_code == 422


def test_a_date_a_few_seconds_ahead_is_two_clocks_not_the_future(r, db, bb, fake_cli, client):
    assert r.FUTURE_TOLERANCE_SECONDS == 60
    bb.answers = [FakeResponse(200, {"status": 200})]
    bb.effect = lambda call: db.mark_unsent("soon")
    assert _unsend(client, CHAT, db.guids["soon"]).json() == {"ok": True, "via": "bb"}
    db.execute("UPDATE message SET text = ?, date_edited = 0, message_summary_info = NULL WHERE ROWID = ?",
               (OLD_TEXT, db.rowids["soon"]))
    assert _edit(client, CHAT, db.guids["soon"]).json() == {"ok": True, "via": "imessage-cli"}


def test_the_windows_are_apples(r):
    assert (r.UNSEND_WINDOW_SECONDS, r.EDIT_WINDOW_SECONDS, r.MAX_EDITS, r.MAX_EDIT_CHARS) == (120, 900, 5, 10000)
    assert r.CHANGE_CONFIRM_SECONDS == 8.0                 # the module's own value, outside the db fixture
    assert (r.CHANGE_RECHECK_SECONDS, r.CHANGE_SETTLE_SECONDS) == (2.0, 1.0)
    assert (r.EDIT_QUEUE_LIMIT, r.EDIT_QUEUE_SECONDS) == (3, 20.0)
    # the longest an edit can take, waiting included, stays inside the 75 s the app waits for the answer
    assert r.EDIT_QUEUE_SECONDS + cli.EDIT_TIMEOUT + r.CHANGE_CONFIRM_SECONDS <= 75
    target = SimpleNamespace(date=unix_to_apple(time.time() - 100))
    assert 99 < r._age_seconds(target) < 110
    assert r._age_seconds(SimpleNamespace(date=0)) is None


def test_unsend_that_bluebubbles_refuses_is_a_502_in_the_relays_own_words(db, bb, client, capsys):
    """BlueBubbles' 400 and its body (as server 1.9.9 words them) stay on the
    Mac: handed on, the app would take the status for the relay's own."""
    body = ('{"status":400,"message":"You\'ve made a bad request! Please check your request params & body",'
            '"error":{"type":"Validation Error","message":"Selected message does not exist!"}}')
    bb.answers = [FakeResponse(400, text=body)]
    resp = _unsend(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (502, REFUSED)
    for piece in ("bad request", "Validation Error", "Selected message", "400"):
        assert piece not in resp.text, piece
    out, err = capsys.readouterr()
    assert out.startswith("[unsend] bluebubbles failed (HTTP 400: ") and out.count("\n") == 1
    assert_clean(db, out, err)


@pytest.mark.parametrize("error", [httpx.ConnectError("synthetic refused: SECRET-DETAIL"),
                                   httpx.ReadTimeout("synthetic read timeout: SECRET-DETAIL")],
                         ids=lambda e: type(e).__name__)
def test_unsend_with_bluebubbles_unreachable_is_a_502_with_a_short_detail(db, bb, client, capsys, error):
    bb.answers = [error]
    resp = _unsend(client, CHAT, db.guids["fresh"])
    name = type(error).__name__
    assert (resp.status_code, resp.json()) == (502, COULD_NOT)
    out, err = capsys.readouterr()
    assert out == f"[unsend] bluebubbles failed (BlueBubbles failed ({name}))\n"
    assert "SECRET-DETAIL" not in out + resp.text and name not in resp.text
    assert_clean(db, out, err)


@pytest.mark.parametrize("answer", [httpx.ReadTimeout("synthetic read timeout"),
                                    FakeResponse(500, text="synthetic BB failure"),
                                    FakeResponse(504, text="synthetic gateway timeout")],
                         ids=["no-answer", "500", "504"])
def test_unsend_that_failed_but_is_in_the_database_is_answered_as_done(db, bb, client, capsys, answer):
    """BlueBubbles carried the unsend out and its answer was lost or was an
    error: the database says what happened, not the engine."""
    bb.answers = [answer]
    bb.effect_on_failure = lambda call: db.mark_unsent("fresh")
    resp = _unsend(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "bb"})
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert len(lines) == 2 and lines[0].startswith("[unsend] bluebubbles failed (")
    assert lines[1] == "[unsend] bb: failed, but chat.db shows the change"
    assert_clean(db, out, err)


def test_unsend_refused_by_bluebubbles_is_not_looked_up_again(r, db, bb, client, monkeypatch):
    """A 4xx is BlueBubbles refusing before it touched Messages: answered
    as ``502 BlueBubbles refused the request``, with no second reading of
    the database."""
    looked = []
    real = r._await_change
    monkeypatch.setattr(r, "_await_change", lambda *a: looked.append(a) or real(*a))
    bb.answers = [FakeResponse(400, text='{"status":400,"error":{"message":"Selected message does not exist!"}}')]
    bb.effect_on_failure = lambda call: db.mark_unsent("fresh")     # even if the row did change
    resp = _unsend(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (502, REFUSED)
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    assert _unsend(client, CHAT, db.guids["fresh2"]).status_code == 501
    assert looked == []


def test_unsend_without_bluebubbles_is_a_501_in_the_chains_words(r, db, bb, fake_cli, osa, client, monkeypatch):
    monkeypatch.setattr(r, "BB_PASSWORD", "")              # the chain: applescript, imessage-cli
    assert r.engine_names(r._chain()) == ["applescript", "imessage-cli"]
    resp = _unsend(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == \
        (501, {"detail": "no configured engine can unsend messages in this chat"})
    assert bb.calls == [] and fake_cli.calls == [] and osa == []


# ---------------------------------------------------------------------------
# an engine's failure in the relay's own words (what the app's classifier reads)
# ---------------------------------------------------------------------------
# The app (EditUnsend.kt, changeOutcomeFor) takes every status of these two
# routes for the relay's own verdict: 401 "the relay rejected the token", a
# JSON 403 "only your own messages", a JSON 404 "the relay doesn't know this
# message" or, with the detail "Not Found", "update the relay", a JSON 501
# "cannot", which it remembers for the session, and a 502 that says "did not
# apply" for the one certain failure among the 502s.

#: What the scripted BlueBubbles says. No answer of the relay may carry it, and no line it prints.
UPSTREAM_MARK = "WOMBAT-UPSTREAM-BODY-5150"

UPSTREAM_STATUSES = (400, 401, 403, 404, 405, 409, 422, 429, 500, 502, 503)


@pytest.mark.parametrize("status", UPSTREAM_STATUSES)
def test_no_bluebubbles_status_or_body_is_handed_on_by_unsend(status, r, db, bb, client, capsys, monkeypatch):
    """Whatever BlueBubbles answers, the relay answers 502 with one of its two
    fixed details: "refused the request" for a 4xx (nothing was changed, the
    database is not read again), "could not unsend the message" for a 5xx
    (after the database was read again and showed no change). The body is
    BlueBubbles' own: once as plain text, once as its JSON with the words
    under ``data``, where server 1.9.9 returns the message a call was about."""
    looked = []
    real = r._await_change
    monkeypatch.setattr(r, "_await_change", lambda *a: looked.append(a) or real(*a))
    expected = REFUSED if status < 500 else COULD_NOT
    plain = f"{UPSTREAM_MARK} is what BlueBubbles said"
    as_json = json.dumps({"status": status, "message": "synthetic upstream failure",
                          "data": {"text": UPSTREAM_MARK}})
    logged = {plain: f"HTTP {status}, body not quoted ({len(plain)} characters, not a JSON object)",
              as_json: 'HTTP %d: {"status": %d, "message": "synthetic upstream failure"}' % (status, status)}
    for body in (plain, as_json):
        bb.answers = [FakeResponse(status, text=body)]
        resp = _unsend(client, CHAT, db.guids["fresh"])
        assert (resp.status_code, resp.json()) == (502, expected), body
        assert resp.headers["content-type"] == "application/json"
        assert UPSTREAM_MARK not in resp.text and "synthetic upstream failure" not in resp.text
        assert "did not apply" not in resp.text            # that 502 is the confirmation's alone
        out, err = capsys.readouterr()
        assert UPSTREAM_MARK not in out + err
        # the one line is the chain's hop log, as it was: the status, and of a JSON body what is not ``data``
        assert out == f"[unsend] bluebubbles failed ({logged[body]})\n"
        assert_clean(db, out, err, resp.text)
    assert len(bb.calls) == 2 and db.row("fresh")["text"] == OLD_TEXT
    assert len(looked) == (0 if status < 500 else 2)       # a refusal is not looked up again, a 5xx is


def test_a_501_is_answered_only_when_no_engine_in_the_chain_can_do_it(r, db, bb, fake_cli, client, monkeypatch):
    """The app stops offering Edit or Undo Send for a whole kind of chat, for
    the rest of its session, when it reads a JSON 501. So a 501 is the chain's
    "no configured engine can ..." and nothing else: not a chat or a message
    that cannot take the change, not an engine that failed, and not
    BlueBubbles answering 501 itself."""
    def capabilities():
        return client.get("/health", headers=AUTH).json()["capabilities"]

    answers: dict[str, int] = {}

    def note(label: str, resp) -> None:
        answers[label] = resp.status_code

    # every engine is there: whatever is refused, and whatever fails, is not a 501
    assert {"edit", "unsend"} <= set(capabilities())
    for route in ROUTES:
        note(f"{route} bp", _call(route, client, BP_CHAT, db.guids["fresh"]))
        for name in ("sms", "rcs", "noservice", "theirs", "unsent", "min16", "future", "tapback"):
            note(f"{route} {name}", _call(route, client, CHAT, db.guids[name]))
        note(f"{route} unknown", _call(route, client, CHAT, guid_for("nosuchmessage")))
        note(f"{route} part", _call(route, client, CHAT, db.guids["fresh"], part_index=99))
    for status in (*UPSTREAM_STATUSES, 501, 504):
        bb.answers = [FakeResponse(status, text=f"{UPSTREAM_MARK} {status}")]
        note(f"unsend upstream {status}", _unsend(client, CHAT, db.guids["fresh"]))
    bb.answers = [httpx.ConnectError("synthetic refused"), FakeResponse(200, {"status": 200})]
    note("unsend unreachable", _unsend(client, CHAT, db.guids["fresh"]))
    note("unsend not applied", _unsend(client, CHAT, db.guids["fresh"]))
    for mode in ("exit", "no-ok", "ok"):
        fake_cli.configure(mode=mode, code=1)
        note(f"edit tool {mode}", _edit(client, CHAT, db.guids["fresh"]))
    note("edit option text", _edit(client, CHAT, db.guids["fresh"], text="--json"))
    note("edit second part", _edit(client, CHAT, db.guids["fresh"], part_index=1))
    monkeypatch.setattr(cli, "accessibility_trusted", lambda: False)
    note("edit no grant", _edit(client, CHAT, db.guids["fresh"]))
    monkeypatch.setattr(cli, "accessibility_trusted", lambda: True)
    assert len(answers) == 43 and 501 not in answers.values(), answers
    assert set(answers.values()) == {403, 404, 409, 422, 502}
    # BlueBubbles' own 501 is a 5xx like its others: the relay's 502, and none of its words
    bb.answers = [FakeResponse(501, text=f"{UPSTREAM_MARK} not implemented")]
    resp = _unsend(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (502, COULD_NOT) and UPSTREAM_MARK not in resp.text

    # no engine that can unsend: 501, in the chain's words, as JSON; /health agrees
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    assert capabilities() == ["edit"]
    resp = _unsend(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (501, NO_UNSEND_ENGINE)
    assert resp.headers["content-type"] == "application/json"
    # ... and still only for a message an engine would have been asked about
    resp = _unsend(client, BP_CHAT, "12345")
    assert (resp.status_code, resp.json()) == (409, UNSUPPORTED)
    assert _unsend(client, CHAT, db.guids["sms"]).json() == UNSUPPORTED
    assert _unsend(client, CHAT, db.guids["theirs"]).status_code == 403
    assert _unsend(client, CHAT, db.guids["min16"]).status_code == 409
    fake_cli.configure(mode="apply")
    assert _edit(client, CHAT, db.guids["fresh2"]).status_code == 200        # the other action is untouched

    # no engine that can edit: the same, the other way round
    monkeypatch.setattr(r, "BB_PASSWORD", RELAY_STUB_ENV["BB_PASSWORD"])
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", None)
    assert "edit" not in capabilities() and "unsend" in capabilities()
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (501, NO_EDIT_ENGINE)
    assert resp.headers["content-type"] == "application/json"
    resp = _edit(client, BP_CHAT, "12345")
    assert (resp.status_code, resp.json()) == (409, UNSUPPORTED)
    assert _edit(client, CHAT, db.guids["rcs"]).json() == UNSUPPORTED
    assert _edit(client, CHAT, db.guids["min16"]).status_code == 409
    assert len(fake_cli.calls) == 4                                             # exit, no-ok, ok, and fresh2


class _UpstreamEditEngine:
    """An edit engine that talks HTTP and fails with its upstream's status and
    body, or raises. None exists (``imessage-cli`` is a local tool); the route
    must not depend on that."""

    name = via = "stand-in"
    capabilities = None                                    # set per test: Capability comes from the relay module
    facetime = None

    def __init__(self, failure):
        self.failure = failure

    def handles(self, chat_guid: str) -> bool:
        return True

    async def edit(self, chat_guid: str, message_guid: str, text: str, part_index: int = 0) -> SendResult:
        if isinstance(self.failure, BaseException):
            raise self.failure
        return SendResult(False, self.via, f"stand-in failed (HTTP {self.failure}: {UPSTREAM_MARK})",
                          status=self.failure, body=f"{UPSTREAM_MARK} upstream body")


@pytest.mark.parametrize("failure", [400, 401, 403, 404, 409, 500, 501, 502, RuntimeError(f"boom {UPSTREAM_MARK}")],
                         ids=lambda f: str(f) if isinstance(f, int) else "raises")
def test_no_upstream_status_or_body_is_handed_on_by_edit(failure, r, db, client, capsys, monkeypatch):
    engine = _UpstreamEditEngine(failure)
    engine.capabilities = frozenset({r.Capability.EDIT})
    monkeypatch.setattr(r, "_chain", lambda: [engine])
    looked = []
    real = r._await_change
    monkeypatch.setattr(r, "_await_change", lambda *a: looked.append(a) or real(*a))
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (502, EDIT_FAILED)
    assert UPSTREAM_MARK not in resp.text and "boom" not in resp.text
    assert len(looked) == 1                                # it may have reached Messages: the database was read
    out, err = capsys.readouterr()
    if isinstance(failure, int):                           # (what an engine raises is the chain's hop line, as ever)
        assert UPSTREAM_MARK not in out + err
        assert out == (f"[edit] stand-in failed (HTTP {failure}, body not quoted "
                       f"({len(UPSTREAM_MARK) + len(' upstream body')} characters, not a JSON object))\n")
    assert_clean(db, out, err, resp.text)


def test_the_phrases_the_edit_route_answers_as_they_are_are_the_tools_own(r):
    """Every failure phrase of the edit engine, and no other text, is a 502
    detail as it stands; and none of the 502 details an engine's failure can
    get reads as the confirmation's "did not apply"."""
    phrases = {value for name, value in vars(cli).items()
               if name.isupper() and isinstance(value, str) and value.startswith("imessage-cli ")}
    assert phrases == r._CLI_DETAILS and len(phrases) == 10
    fixed = (*sorted(phrases), cli.exited(1), cli.exited(64), cli.exited(-9))
    for detail in fixed:
        assert r._edit_failure_detail(r.DeliveryError(502, detail)) == detail
    for status, detail in ((502, "imessage-cli failed (boom)"), (502, f"{cli.TIMED_OUT}; {cli.NOT_STARTED}"),
                           (403, cli.TIMED_OUT), (500, "upstream"), (502, "imessage-cli exited 1 or so"), (502, "")):
        assert r._edit_failure_detail(r.DeliveryError(status, detail)) == r.EDIT_FAILED == EDIT_FAILED["detail"]
    assert (r.UNSEND_REFUSED, r.UNSEND_FAILED) == (REFUSED["detail"], COULD_NOT["detail"])
    assert "did not apply" in r.CHANGE_NOT_APPLIED
    for detail in (*fixed, r.UNSEND_REFUSED, r.UNSEND_FAILED, r.EDIT_FAILED):
        assert "did not apply" not in detail.lower(), detail
    assert r.CHANGE_UNKNOWN.strip().lower() != "not found"   # FastAPI's words for a route that does not exist


# ---------------------------------------------------------------------------
# /edit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store", ["blob", "column"])
def test_edit_runs_the_tool_and_answers_once_the_database_shows_it(r, db, bb, fake_cli, osa, client, capsys,
                                                                   tmp_path, store):
    fake_cli.configure(store=store)
    before = db.row("fresh")
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})
    call, = fake_cli.calls
    assert call["argv"] == ["--json", "--no-events", "--data-dir", str(tmp_path / "data" / "imessage-cli"),
                            "edit", CHAT, db.guids["fresh"], NEW_TEXT]
    assert bb.calls == [] and osa == []                    # BlueBubbles' edit is never used
    after = db.row("fresh")
    assert after["date_edited"] > (before["date_edited"] or 0)
    assert (after["text"] is None) == (store == "blob")    # Messages keeps an edited text in the blob
    assert r.chatdb_adapter.change_target(CHAT, db.guids["fresh"]).text == NEW_TEXT
    out, err = capsys.readouterr()
    assert out == "[edit] imessage-cli: confirmed in chat.db\n"
    assert_clean(db, out, err)
    for key in ("IMSG_TOKEN", "BB_PASSWORD"):              # the relay's secrets stayed home
        assert key not in call["env"]
    assert RELAY_STUB_ENV["IMSG_TOKEN"] not in str(call["env"])


def test_edit_reaches_the_app_through_the_existing_edit_poll(r, db, fake_cli, client):
    """Nothing new is broadcast by the route: the row's edit mark moved, and
    ``fetch_edited`` (the poll loop's reader) hands it out as it always did."""
    mark = r.max_date_edited()
    assert _edit(client, CHAT, db.guids["fresh"]).status_code == 200
    edited, new_mark = r.fetch_edited(mark)
    assert [m["guid"] for m in edited] == [db.guids["fresh"]] and new_mark > mark
    assert edited[0]["text"] == NEW_TEXT and edited[0]["date_edited"] is not None


def test_edit_that_the_tool_reports_but_messages_ignores_is_a_502(db, bb, fake_cli, client, capsys):
    """The false success: exit 0 and the ok line, nothing in the database."""
    fake_cli.configure(mode="ok")
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (502, NOT_APPLIED)
    assert len(fake_cli.calls) == 1 and db.row("fresh")["text"] == OLD_TEXT
    out, err = capsys.readouterr()
    assert out == "[edit] imessage-cli: reported ok, but chat.db did not change\n"
    assert_clean(db, out, err)


def test_edit_that_landed_with_another_text_is_done_and_says_so(db, fake_cli, client, capsys):
    """Messages entered something else than it was given (a text replacement,
    autocorrect): the edit is on every device. "Not applied" would be wrong,
    and a retry would spend another of the five edits."""
    fake_cli.configure(stored_text="On my way! see you there")
    resp = _edit(client, CHAT, db.guids["fresh"], text="omw see you there")
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli", "text_differs": True})
    out, err = capsys.readouterr()
    assert out == "[edit] imessage-cli: applied, but chat.db holds another text than the one asked for\n"
    assert_clean(db, out, err)
    assert "On my way" not in out and "omw" not in out


def test_edit_is_not_confirmed_without_an_edit_mark_or_by_a_row_that_did_not_get_a_new_text(r, db, fake_cli, client):
    # the tool says ok and the edit mark did not move: nothing was applied, whatever the row says
    fake_cli.configure(mode="ok")
    resp = _edit(client, CHAT, db.guids["fresh2"], text="a new text")
    assert (resp.status_code, resp.json()) == (502, NOT_APPLIED)
    # the edit mark moved and the text is the one it had: not this edit
    fake_cli.configure(mode="apply", stored_text=OLD_TEXT)
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (502, NOT_APPLIED)
    # the edit mark moved and the text is gone: an unsend arrived, not an edit
    fake_cli.configure(mode="ok", stored_text=None)
    before = r.chatdb_adapter.change_target(CHAT, db.guids["blob"])
    verdict = r._edited_since(before, NEW_TEXT)
    db.mark_unsent("blob")
    assert verdict(r.chatdb_adapter.change_target(CHAT, db.guids["blob"])) is None


def test_the_verdict_on_an_edit(r, db):
    before = r.chatdb_adapter.change_target(CHAT, db.guids["fresh"])
    verdict = r._edited_since(before, "It's here")
    later = lambda **kw: SimpleNamespace(**{  # noqa: E731
        "date_edited": before.date_edited + 1, "text": "It’s here", "is_retracted": lambda part=0: False, **kw})
    assert verdict(later()) == r.CONFIRMED == "confirmed"
    assert verdict(later(text="It is here")) == r.DIFFERS == "differs"
    assert verdict(later(text=OLD_TEXT)) is None                    # the text it had
    assert verdict(later(text=None)) is None and verdict(later(text="￼")) is None
    assert verdict(later(text="other", is_retracted=lambda part=0: True)) is None
    assert verdict(later(date_edited=before.date_edited)) is None   # no edit mark, even with the right text
    assert verdict(later(date_edited=before.date_edited, text="other")) is None


def test_a_row_on_its_way_to_the_wanted_text_is_waited_for(r, db, monkeypatch):
    """DIFFERS is only the answer when the row has held the other text for
    CHANGE_SETTLE_SECONDS; a CONFIRMED reading inside that time wins."""
    answers = iter([r.DIFFERS, r.DIFFERS, r.CONFIRMED])
    assert r._await_change(CHAT, db.guids["fresh"], lambda now: next(answers), 5) == r.CONFIRMED
    monkeypatch.setattr(r, "CHANGE_SETTLE_SECONDS", 0.05)
    started = time.monotonic()
    assert r._await_change(CHAT, db.guids["fresh"], lambda now: r.DIFFERS, 5) == r.DIFFERS
    assert time.monotonic() - started < 2                            # not the whole five seconds
    assert r._await_change(CHAT, db.guids["fresh"], lambda now: None, 0.1) is None
    assert r._await_change(CHAT, guid_for("nosuchmessage"), lambda now: r.CONFIRMED, 0.1) is None


@pytest.mark.parametrize("asked, stored", [
    ("It's here", "It’s here"),                       # smart quote
    ('say "hi" -- now...', "say “hi” — now…"),
    ("trailing space ", "trailing space"),
    ("café", "café"),                           # the same letter, composed and decomposed
    ("with a photo", "￼with a photo"),                # the attachment placeholder stays in the row
])
def test_edit_is_confirmed_when_messages_only_restyled_the_text(r, db, fake_cli, client, asked, stored):
    assert r._text_key(asked) == r._text_key(stored)
    fake_cli.configure(stored_text=stored)
    resp = _edit(client, CHAT, db.guids["fresh"], text=asked)
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})


def test_text_key_folds_typography_and_nothing_else(r):
    assert r._text_key(None) == "" == r._text_key("  ")
    for a, b in (("Hello", "hello"), ("a b", "a  b"), ("one", "one."), ("a\nb", "a b"), ("x", "y")):
        assert r._text_key(a) != r._text_key(b), (a, b)


def test_edit_later_than_fifteen_minutes_is_a_409_and_three_minutes_is_fine(db, bb, fake_cli, client):
    for name in ("min16", "undated"):
        resp = _edit(client, CHAT, db.guids[name])
        assert (resp.status_code, resp.json()) == \
            (409, {"detail": "too late to edit (Apple allows 15 minutes)"}), name
    assert fake_cli.calls == []
    resp = _edit(client, CHAT, db.guids["min3"])           # too late to unsend, not to edit
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})


@pytest.mark.parametrize("text", ["", " ", "\n\t  ", "\u00a0", "\ufffc", " \ufffc\ufffc ", "\u200b\u200b",
                                  "\ufeff", "\u200d \u2060", "\u00a0\u3000", "\r\n", "\u00ad"],
                         ids=lambda t: "-".join(f"{ord(c):04x}" for c in t) or "empty")
def test_edit_to_an_empty_text_is_a_422(text, db, fake_cli, client):
    """Nothing to see: white space, the attachment placeholder, zero-width
    and other invisible characters, alone or together."""
    resp = _edit(client, CHAT, db.guids["fresh"], text=text)
    assert (resp.status_code, resp.json()) == (422, {"detail": "text must not be empty"})
    assert fake_cli.calls == []


@pytest.mark.parametrize("text", ["a\x1b[2Jb", "back\x08space", "del\x7f here", "bell\x07", "nul\x00"])
def test_edit_to_a_text_with_a_control_character_is_a_422(text, db, fake_cli, client):
    """The tool enters the text into Messages through Accessibility; what a
    key code does there was never tried."""
    resp = _edit(client, CHAT, db.guids["fresh"], text=text)
    assert (resp.status_code, resp.json()) == (422, {"detail": "text must not contain control characters"})
    assert fake_cli.calls == [] and db.row("fresh")["text"] == OLD_TEXT


def test_edit_hands_over_the_text_trimmed_with_plain_line_ends_and_no_placeholder(r, db, fake_cli, client):
    """The tool trims the text at both ends before it enters it; the relay
    does the same first, so what it compares is what Messages gets. Tabs and
    line breaks inside the text stay; ``\\r\\n`` and ``\\r`` become ``\\n``."""
    resp = _edit(client, CHAT, db.guids["fresh"], text="  \ufffcfirst line\r\n\tsecond\rthird \n ")
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})
    assert fake_cli.calls[0]["argv"][-1] == "first line\n\tsecond\nthird"
    assert r.chatdb_adapter.change_target(CHAT, db.guids["fresh"]).text == "first line\n\tsecond\nthird"
    assert r._plain_text(None) == "" and r._plain_text(" a\u200db ") == "a\u200db"     # a joiner inside stays


def test_edit_text_is_capped_at_10000_characters(db, fake_cli, client):
    resp = _edit(client, CHAT, db.guids["fresh"], text="x" * 10001)
    assert (resp.status_code, resp.json()) == (422, {"detail": "text is longer than 10000 characters"})
    assert fake_cli.calls == []
    resp = _edit(client, CHAT, db.guids["fresh"], text="\U0001F389" * 10000)       # characters, not bytes
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})
    assert fake_cli.calls[0]["argv"][-1] == "\U0001F389" * 10000
    # "too late" is answered before the text is looked at
    assert _edit(client, CHAT, db.guids["min16"], text="").status_code == 409


def test_edit_to_the_same_text_is_answered_without_asking_any_engine(db, bb, fake_cli, client, capsys):
    for name in ("fresh", "blob"):                         # text column, and blob only
        resp = _edit(client, CHAT, db.guids[name], text=OLD_TEXT)
        assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": None, "unchanged": True}), name
    assert fake_cli.calls == [] and bb.calls == []
    assert db.row("fresh")["date_edited"] in (0, None)
    assert capsys.readouterr().out == ""
    # a caption beside a photo is stored behind the attachment's placeholder
    db.execute("UPDATE message SET text = ? WHERE ROWID = ?", ("\ufffcthe caption", db.rowids["photo"]))
    for text in ("the caption", "\ufffcthe caption"):
        resp = _edit(client, CHAT, db.guids["photo"], text=text)
        assert resp.json() == {"ok": True, "via": None, "unchanged": True}, text
    assert fake_cli.calls == []
    # white space at the ends is no difference: the tool trims it, so nothing would be edited
    for text in (OLD_TEXT + " ", "  " + OLD_TEXT, OLD_TEXT + "\n", "\t" + OLD_TEXT + " \r\n"):
        assert _edit(client, CHAT, db.guids["fresh"], text=text).json() == UNCHANGED, repr(text)
    assert fake_cli.calls == []
    # inside the text, identical means identical: one more space is an edit
    resp = _edit(client, CHAT, db.guids["fresh"], text=OLD_TEXT.replace(" ", "  ", 1))
    assert resp.json() == {"ok": True, "via": "imessage-cli"} and len(fake_cli.calls) == 1
    # the shortcut comes after the checks: an old message is still "too late"
    assert _edit(client, CHAT, db.guids["min16"], text=OLD_TEXT).status_code == 409


def test_edit_stops_at_five_edits_when_the_history_can_be_read(db, fake_cli, client):
    resp = _edit(client, CHAT, db.guids["edited5"])        # original + five edits in the history
    assert (resp.status_code, resp.json()) == \
        (409, {"detail": "this message has been edited 5 times already"})
    assert fake_cli.calls == []
    resp = _edit(client, CHAT, db.guids["edited4"])        # original + four: a fifth is allowed
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})
    # the same text again is "unchanged" even at the limit: nothing would be edited
    resp = _edit(client, CHAT, db.guids["edited5"], text="fifth version")
    assert resp.json() == {"ok": True, "via": None, "unchanged": True}


def test_edit_skips_the_count_when_the_history_cannot_be_read(r, db, fake_cli, client):
    target = r.chatdb_adapter.change_target(CHAT, db.guids["junk"])
    assert target.summary is None and target.edit_count() is None
    resp = _edit(client, CHAT, db.guids["junk"])
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})


def test_edit_on_a_database_without_the_summary_column(r, make_db, relay_module, fake_cli, client, monkeypatch):
    """The fixture profiles as they ship: no ``message_summary_info`` at all.
    The five-edit check cannot be made and is skipped; the rest works."""
    fx = make_db("macos27", name="no-summary.db")
    chat = builders.add_chat(fx.writer, CHAT, 45, PHONE, handles=[PHONE])
    guid = guid_for("nosummary")
    builders.add_message(fx.writer, chat, guid=guid, text=OLD_TEXT, is_from_me=1, date_ns=_ago(30))
    gone = guid_for("nosummarygone")
    builders.add_message(fx.writer, chat, guid=gone, text=None, is_from_me=1, date_ns=_ago(30), date_edited=_ago(3))
    relay_module.configure(fx.path)
    fake_cli.configure(db=str(fx.path), store="column")
    assert r.chatdb_adapter.change_target(CHAT, guid).edit_count() is None
    resp = _edit(client, CHAT, guid)
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})
    assert (_edit(client, CHAT, gone).status_code, _unsend(client, CHAT, gone).json()) == (409, ALREADY)


@pytest.mark.parametrize("mode, extra, detail", [("exit", {"code": 1}, "imessage-cli exited 1"),
                                                 ("usage", {}, "imessage-cli exited 64"),
                                                 ("no-ok", {}, "imessage-cli reported an error"),
                                                 ("help", {}, "imessage-cli reported an error"),
                                                 ("ok-stderr", {}, "imessage-cli reported an error"),
                                                 ("failed", {}, "imessage-cli exited 1")])
def test_edit_engine_failures_are_a_502_with_the_short_classification(db, bb, fake_cli, client, capsys,
                                                                      mode, extra, detail):
    fake_cli.configure(mode=mode, **extra)
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (502, {"detail": detail})
    assert len(fake_cli.calls) == 1 and bb.calls == []
    out, err = capsys.readouterr()
    assert out == f"[edit] imessage-cli failed ({detail})\n"           # the tool echoed guid and text: not here
    assert_clean(db, out, err, resp.text)


def test_edit_that_times_out_is_a_502(r, db, fake_cli, client, capsys, monkeypatch):
    fake_cli.configure(mode="sleep", seconds=30)
    real = r.ImessageCliEngine
    monkeypatch.setattr(r, "ImessageCliEngine", lambda binary, data_dir: real(binary, data_dir, timeout=0.5))
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (502, {"detail": "imessage-cli timed out"})
    out, err = capsys.readouterr()
    assert out == "[edit] imessage-cli failed (imessage-cli timed out)\n"
    assert_clean(db, out, err)


@pytest.mark.parametrize("then, extra, detail", [("exit", {"code": 1}, "imessage-cli exited 1"),
                                                 ("no-ok", {}, "imessage-cli reported an error"),
                                                 ("ok-stderr", {}, "imessage-cli reported an error")])
def test_edit_made_by_a_run_that_then_failed_is_answered_as_done(db, bb, fake_cli, client, capsys,
                                                                 then, extra, detail):
    """The tool prints its ok line and THEN closes the Messages instance it
    opened; a failure there is exit 1 with the edit on every device. And an
    upgrade that changed the wording of the ok line would otherwise turn
    every edit into "reported an error". The database decides."""
    fake_cli.configure(then=then, **extra)                  # mode "apply": the edit is written, then this
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})
    assert len(fake_cli.calls) == 1 and bb.calls == []
    out, err = capsys.readouterr()
    assert out.splitlines() == [f"[edit] imessage-cli failed ({detail})",
                                "[edit] imessage-cli: failed, but chat.db shows the change"]
    assert_clean(db, out, err)
    # asking again is "unchanged": no second edit is spent
    assert _edit(client, CHAT, db.guids["fresh"]).json() == UNCHANGED and len(fake_cli.calls) == 1


def test_edit_made_by_a_run_that_then_hung_is_answered_as_done(r, db, fake_cli, client, capsys, monkeypatch):
    fake_cli.configure(then="sleep", seconds=30)
    real = r.ImessageCliEngine
    monkeypatch.setattr(r, "ImessageCliEngine", lambda binary, data_dir: real(binary, data_dir, timeout=0.6))
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})
    assert capsys.readouterr().out.splitlines() == ["[edit] imessage-cli failed (imessage-cli timed out)",
                                                    "[edit] imessage-cli: failed, but chat.db shows the change"]


def test_edit_that_failed_and_landed_with_another_text_says_both(db, fake_cli, client):
    fake_cli.configure(then="exit", code=1, stored_text="On my way! see you there")
    resp = _edit(client, CHAT, db.guids["fresh"], text="omw see you there")
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli", "text_differs": True})


def test_a_refusal_before_the_tool_was_started_is_not_looked_up_again(r, db, fake_cli, client, monkeypatch):
    """Only a failure reported after the tool ran can have left an edit behind."""
    looked = []
    real = r._await_change
    monkeypatch.setattr(r, "_await_change", lambda *a: looked.append(a) or real(*a))
    assert _edit(client, CHAT, db.guids["fresh"], text="--json").status_code == 502
    assert _edit(client, CHAT, db.guids["fresh"], part_index=1).status_code == 502
    monkeypatch.setattr(cli, "accessibility_trusted", lambda: False)
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == \
        (502, {"detail": "imessage-cli needs the Accessibility grant for the relay's Python"})
    found = r.IMESSAGE_CLI_BIN
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", None)
    assert _edit(client, CHAT, db.guids["fresh"]).status_code == 501
    assert looked == [] and fake_cli.calls == []
    # ... and a failure after it ran is looked up, once, for the short time
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", found)
    monkeypatch.setattr(cli, "accessibility_trusted", lambda: True)
    fake_cli.configure(mode="exit", code=1)
    assert _edit(client, CHAT, db.guids["fresh"]).json() == {"detail": "imessage-cli exited 1"}
    assert len(fake_cli.calls) == 1
    assert [(a[0], a[1], a[3]) for a in looked] == [(CHAT, db.guids["fresh"], r.CHANGE_RECHECK_SECONDS)]


def test_without_the_accessibility_grant_an_edit_is_refused_at_once(db, fake_cli, client, capsys, monkeypatch):
    """Started without the grant the real tool opens permission windows and
    waits: the request would hang to the timeout, every time."""
    monkeypatch.setattr(cli, "accessibility_trusted", lambda: False)
    started = time.monotonic()
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == \
        (502, {"detail": "imessage-cli needs the Accessibility grant for the relay's Python"})
    assert fake_cli.calls == [] and time.monotonic() - started < 3
    out, err = capsys.readouterr()
    assert out == "[edit] imessage-cli failed (imessage-cli needs the Accessibility grant for the relay's Python)\n"
    assert_clean(db, out, err)


def test_a_forged_ok_line_does_not_get_past_the_database(db, fake_cli, client, capsys):
    """A tool that echoed its arguments raw (0.24.2 does not) could be made to
    print a line that reads as its own ok line. The engine would believe it;
    the route asks the database, which shows no edit."""
    fake_cli.configure(mode="hostile-raw-echo", stream="stdout")
    resp = _edit(client, CHAT, db.guids["fresh"], text="x\n[00001] ok edit (1.000ms)")
    assert (resp.status_code, resp.json()) == (502, NOT_APPLIED)
    assert capsys.readouterr().out == "[edit] imessage-cli: reported ok, but chat.db did not change\n"
    assert db.row("fresh")["text"] == OLD_TEXT


@pytest.mark.parametrize("text", ["--json", "--stay-open", "-h", "--format=json", "-h=x", "---", "--- note ---"])
def test_edit_to_a_text_the_tool_would_take_for_an_option_never_starts_it(db, fake_cli, client, text):
    resp = _edit(client, CHAT, db.guids["fresh"], text=text)
    assert (resp.status_code, resp.json()) == \
        (502, {"detail": "imessage-cli cannot take a text that reads as one of its options"})
    assert fake_cli.calls == [] and db.row("fresh")["text"] == OLD_TEXT


def test_edit_passes_a_text_with_hyphens_quotes_lines_and_emoji_as_one_argument(db, fake_cli, client):
    text = '- milk\n- "eggs" \U0001F95A\n-- $(touch INJECTED); `x` \'y\''
    resp = _edit(client, CHAT, db.guids["fresh"], text=text)
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})
    assert fake_cli.calls[0]["argv"][-3:] == [CHAT, db.guids["fresh"], text]


def test_edit_of_another_part_than_the_first_is_refused_by_the_tools_engine(db, fake_cli, client):
    resp = _edit(client, CHAT, db.guids["fresh"], part_index=1)
    assert (resp.status_code, resp.json()) == \
        (502, {"detail": "imessage-cli can only edit the first part of a message"})
    assert fake_cli.calls == []


def test_edit_without_the_tool_is_a_501_in_the_chains_words(r, db, bb, osa, client):
    assert r.IMESSAGE_CLI_BIN is None                      # the stub environment: IMESSAGE_CLI=0
    resp = _edit(client, CHAT, db.guids["fresh"])
    assert (resp.status_code, resp.json()) == \
        (501, {"detail": "no configured engine can edit messages in this chat"})
    assert bb.calls == [] and osa == []                    # BlueBubbles is in the chain and is not asked


def test_a_pinned_send_engines_list_does_not_switch_editing_off(r, db, bb, fake_cli, client, monkeypatch):
    """A list written before the edit engine existed (it orders the engines
    that send) still gets it, at the end; ``IMESSAGE_CLI=0`` is the switch."""
    monkeypatch.setattr(r, "SEND_ENGINES", "bluebubbles,applescript")
    assert r.engine_names(r._chain()) == ["bluebubbles", "applescript", "imessage-cli"]
    assert _edit(client, CHAT, db.guids["fresh"]).json() == {"ok": True, "via": "imessage-cli"}
    monkeypatch.setattr(r, "SEND_ENGINES", "imessage-cli,bluebubbles")
    assert r.engine_names(r._chain()) == ["imessage-cli", "bluebubbles"]
    assert _edit(client, CHAT, db.guids["fresh2"]).json() == {"ok": True, "via": "imessage-cli"}
    assert bb.calls == [] and len(fake_cli.calls) == 2
    monkeypatch.setattr(r, "SEND_ENGINES", "bluebubbles,applescript")
    monkeypatch.setattr(r, "IMESSAGE_CLI", "0")
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", r.find_imessage_cli("0"))
    assert r.engine_names(r._chain()) == ["bluebubbles", "applescript"]
    assert _edit(client, CHAT, db.guids["blob"]).status_code == 501
    assert len(fake_cli.calls) == 2


def test_a_group_chat_and_a_satellite_message_are_imessage_too(db, bb, fake_cli, client):
    resp = _edit(client, GROUP, db.guids["group"])
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "imessage-cli"})
    assert fake_cli.calls[0]["argv"][-3] == GROUP
    assert _edit(client, CHAT, db.guids["satellite"]).status_code == 200           # iMessageLite


# ---------------------------------------------------------------------------
# one edit at a time, and each checked again when its turn has come
# ---------------------------------------------------------------------------
# The route coroutine is called directly, several times on ONE event loop, so
# a test can decide what happens while an edit waits. (The test client gives
# every request a loop of its own; the socket tests below go through uvicorn.)

EDITED = {"ok": True, "via": "imessage-cli"}


def _request(r, db, name: str, text: str = NEW_TEXT, chat: str = CHAT):
    return r.EditReq(chat_guid=chat, guid=db.guids[name], text=text)


async def _answer(r, req) -> tuple[int, dict]:
    """The route's answer, or its refusal, as ``(status, body)``."""
    try:
        return 200, await r.edit_message(req)
    except HTTPException as e:
        return e.status_code, {"detail": e.detail}
    except r.DeliveryError as e:
        return e.status, {"detail": e.detail}


async def _until(condition, what: str) -> None:
    deadline = time.monotonic() + 15
    while not condition():
        assert time.monotonic() < deadline, f"never happened: {what}"
        await asyncio.sleep(0.01)


def _run(scenario):
    return asyncio.run(asyncio.wait_for(scenario, 40))


def test_the_same_edit_sent_twice_runs_the_tool_once(r, db, fake_cli):
    """A retry after the app gave up waiting, or a double tap: the second
    request finds its text already there when its turn comes."""
    fake_cli.configure(delay=0.3)

    async def scenario():
        return await asyncio.gather(*(_answer(r, _request(r, db, "fresh")) for _ in range(3)))

    answers = _run(scenario())
    assert sorted(answers, key=str) == sorted([(200, EDITED), (200, UNCHANGED), (200, UNCHANGED)], key=str)
    assert len(fake_cli.calls) == 1 and r._EDITS_WAITING == 0
    assert r.chatdb_adapter.change_target(CHAT, db.guids["fresh"]).text == NEW_TEXT


def test_an_edit_whose_fifteen_minutes_ended_while_it_waited_is_refused_and_never_run(r, db, fake_cli):
    fake_cli.configure(delay=0.5)

    async def scenario():
        running = asyncio.create_task(_answer(r, _request(r, db, "fresh")))
        await _until(lambda: fake_cli.calls, "the first edit's tool run")
        waiting = asyncio.create_task(_answer(r, _request(r, db, "fresh2")))
        await _until(lambda: r._EDITS_WAITING == 1, "the second edit waiting for its turn")
        # it passed every check when it arrived; by the time its turn comes the message is too old
        db.execute("UPDATE message SET date = ? WHERE ROWID = ?",
                   (_ago(r.EDIT_WINDOW_SECONDS + 5), db.rowids["fresh2"]))
        return await running, await waiting

    first, second = _run(scenario())
    assert first == (200, EDITED)
    assert second == (409, {"detail": "too late to edit (Apple allows 15 minutes)"})
    assert len(fake_cli.calls) == 1 and r._EDITS_WAITING == 0
    assert db.row("fresh2")["text"] == "second fresh one"


def test_an_edit_that_waited_is_checked_again_for_the_five_edits_and_for_an_unsend(r, db, fake_cli):
    fake_cli.configure(delay=0.5)

    async def scenario():
        running = asyncio.create_task(_answer(r, _request(r, db, "fresh")))
        await _until(lambda: fake_cli.calls, "the first edit's tool run")
        fifth = asyncio.create_task(_answer(r, _request(r, db, "edited4")))
        gone = asyncio.create_task(_answer(r, _request(r, db, "fresh2")))
        await _until(lambda: r._EDITS_WAITING == 2, "two edits waiting")
        db.execute("UPDATE message SET message_summary_info = ? WHERE ROWID = ?",
                   (_summary(ec=_history(6), ep=[0]), db.rowids["edited4"]))      # the fifth edit, made elsewhere
        db.mark_unsent("fresh2")                                                   # unsent from another device
        return await running, await fifth, await gone

    first, fifth, gone = _run(scenario())
    assert first == (200, EDITED)
    assert fifth == (409, {"detail": "this message has been edited 5 times already"})
    assert gone == (409, ALREADY)
    assert len(fake_cli.calls) == 1 and r._EDITS_WAITING == 0


def test_a_refusal_does_not_wait_for_the_edit_that_is_running(r, db, fake_cli):
    fake_cli.configure(delay=0.6)

    async def scenario():
        running = asyncio.create_task(_answer(r, _request(r, db, "fresh")))
        await _until(lambda: fake_cli.calls, "the first edit's tool run")
        refusals = [await _answer(r, _request(r, db, name)) for name in ("theirs", "min16", "sms")]
        same = await _answer(r, _request(r, db, "fresh2", text="second fresh one"))
        return refusals, same, running.done(), await running

    refusals, same, was_done, first = _run(scenario())
    assert [status for status, _ in refusals] == [403, 409, 409]
    assert refusals[2] == (409, UNSUPPORTED)
    assert same == (200, UNCHANGED)
    assert was_done is False and first == (200, EDITED)       # all four were answered while the tool still ran
    assert len(fake_cli.calls) == 1


def test_a_full_queue_is_refused_as_busy_and_nothing_piles_up(r, db, fake_cli, monkeypatch):
    """One edit runs, EDIT_QUEUE_LIMIT wait, the next is told to try again:
    requests cannot be parked to drive Messages.app one after another."""
    monkeypatch.setattr(r, "EDIT_QUEUE_LIMIT", 1)
    fake_cli.configure(delay=0.5)

    async def scenario():
        running = asyncio.create_task(_answer(r, _request(r, db, "fresh")))
        await _until(lambda: fake_cli.calls, "the first edit's tool run")
        waiting = asyncio.create_task(_answer(r, _request(r, db, "fresh2")))
        await _until(lambda: r._EDITS_WAITING == 1, "the second edit waiting for its turn")
        refused = [await _answer(r, _request(r, db, "blob")), await _answer(r, _request(r, db, "group", chat=GROUP))]
        return await running, await waiting, refused

    first, second, refused = _run(scenario())
    assert first == (200, EDITED) and second == (200, EDITED)
    assert refused == [(409, BUSY), (409, BUSY)]
    assert len(fake_cli.calls) == 2 and r._EDITS_WAITING == 0
    assert db.row("blob")["date_edited"] in (0, None) and db.row("group")["text"] == OLD_TEXT


def test_an_edit_does_not_wait_for_its_turn_for_ever(r, db, fake_cli, monkeypatch):
    monkeypatch.setattr(r, "EDIT_QUEUE_SECONDS", 0.2)
    fake_cli.configure(delay=1.0)

    async def scenario():
        running = asyncio.create_task(_answer(r, _request(r, db, "fresh")))
        await _until(lambda: fake_cli.calls, "the first edit's tool run")
        started = time.monotonic()
        waited = await _answer(r, _request(r, db, "fresh2"))
        return waited, time.monotonic() - started, running.done(), await running

    waited, took, was_done, first = _run(scenario())
    assert waited == (409, BUSY) and 0.15 < took < 0.9 and was_done is False
    assert first == (200, EDITED)
    assert len(fake_cli.calls) == 1 and r._EDITS_WAITING == 0

    async def afterwards():                                    # the turn is free again: nothing was left held
        return await _answer(r, _request(r, db, "fresh2"))

    fake_cli.configure(delay=0)
    assert _run(afterwards()) == (200, EDITED)


def test_the_busy_wording_is_a_409_the_app_reads_as_a_plain_refusal():
    """The app sorts a 409 by its words: "too late", "already unsent" and
    "edited ... times" each have a meaning of their own there. "This chat
    cannot edit or unsend" is a 409 as well, and none of the three."""
    for words in (BUSY["detail"], NOT_SENT_YET["detail"], UNSUPPORTED["detail"]):
        said = words.lower()
        assert "too late" not in said and "already unsent" not in said
        assert not ("edited" in said and "times" in said)


# ---------------------------------------------------------------------------
# over a real socket, on the event loop uvicorn gives the relay
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _served(r):
    """The relay's app behind uvicorn on a loopback port: yields ``post(path,
    body, headers=AUTH) -> (status, json)``. ``lifespan="off"`` (the relay's
    startup hooks never run), no logging reconfigured."""
    import uvicorn

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

        def post(path: str, body: dict, headers: dict | None = AUTH) -> tuple[int, dict]:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers or {}, timeout=30) as c:
                resp = c.post(path, json=body)
                return resp.status_code, (resp.json() if resp.headers.get("content-type", "").startswith(
                    "application/json") else {"text": resp.text})

        yield post
    finally:
        server.should_exit = True
        thread.join(timeout=20)
    assert not thread.is_alive()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")      # uvicorn's and uvloop's own
def test_eight_requests_for_one_edit_over_a_socket_run_the_tool_once(r, db, fake_cli, capsys):
    """Eight POSTs of the same edit at once, as a client that retries would
    send them. Before the route took a turn per edit all eight ran the tool,
    one after another: eight edits against Apple's five, the last of them
    seconds after the first had answered."""
    fake_cli.configure(delay=0.4)
    body = {"chat_guid": CHAT, "guid": db.guids["fresh"], "text": NEW_TEXT}
    answers: list[tuple[int, dict]] = []
    with _served(r) as post:
        threads = [threading.Thread(target=lambda: answers.append(post("/edit", body))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert len(answers) == 8 and len(fake_cli.calls) == 1
    assert answers.count((200, EDITED)) == 1
    # the others waited and found the text there, or found the queue full
    assert all(a in ((200, EDITED), (200, UNCHANGED), (409, BUSY)) for a in answers), answers
    assert answers.count((200, UNCHANGED)) >= 1
    assert r._EDITS_WAITING == 0
    out, err = capsys.readouterr()
    assert out == "[edit] imessage-cli: confirmed in chat.db\n"
    assert_clean(db, out, err)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")      # uvicorn's and uvloop's own
def test_both_routes_over_a_real_socket_and_three_edits_at_once(r, db, bb, fake_cli, capsys):
    """The in-process test client runs every request on a fresh asyncio loop.
    The relay runs under uvicorn (uvloop where it is installed), where the
    worker-thread steps and the one-edit-at-a-time turn have to hold while
    requests overlap. Loopback only, an ephemeral port."""
    fake_cli.configure(delay=0.3)
    answers: dict[str, tuple[int, dict]] = {}
    with _served(r) as post:
        def edit(name: str) -> None:
            answers[name] = post("/edit", {"chat_guid": CHAT, "guid": db.guids[name],
                                           "text": f"{NEW_TEXT} ({name})"})

        edits = [threading.Thread(target=edit, args=(name,)) for name in ("fresh", "fresh2", "blob")]
        for t in edits:
            t.start()
        for t in edits:
            t.join(timeout=30)
        bb.answers = [FakeResponse(200, {"status": 200, "message": "Message unsent!"})]
        bb.effect = lambda call: db.mark_unsent("group")
        answers["unsend"] = post("/unsend", {"chat_guid": GROUP, "guid": db.guids["group"]})
        assert post("/edit", {"chat_guid": CHAT, "guid": db.guids["fresh"], "text": "x"}, headers=None)[0] == 401
    for name in ("fresh", "fresh2", "blob"):
        assert answers[name] == (200, {"ok": True, "via": "imessage-cli"}), name
        assert r.chatdb_adapter.change_target(CHAT, db.guids[name]).text == f"{NEW_TEXT} ({name})"
    assert answers["unsend"] == (200, {"ok": True, "via": "bb"})
    spans = sorted(fake_cli.spans)
    assert len(spans) == 3
    for (_, finished), (started, _) in zip(spans, spans[1:]):
        assert started >= finished, spans                  # three requests at once, one tool run at a time
    out, err = capsys.readouterr()
    assert sorted(out.splitlines()) == ["[edit] imessage-cli: confirmed in chat.db"] * 3 + \
        ["[unsend] bb: confirmed in chat.db"]
    assert_clean(db, out, err)


# ---------------------------------------------------------------------------
# logs: one line per outcome, naming the action, the engine and the outcome
# ---------------------------------------------------------------------------

def test_no_log_line_of_either_route_names_a_chat_a_message_or_its_text(r, db, bb, fake_cli, client, capsys,
                                                                        monkeypatch):
    bb.answers = [FakeResponse(200, {"status": 200}),                              # unsend, confirmed
                  FakeResponse(200, {"status": 200}),                              # unsend, not confirmed
                  FakeResponse(500, text=f"boom {CHAT} {OLD_TEXT}"),               # an upstream that quotes
                  httpx.ConnectError(f"refused {CHAT}")]
    bb.effect = lambda call: db.mark_unsent("fresh") if len(bb.calls) == 1 else None
    statuses = [_unsend(client, CHAT, db.guids["fresh"]).status_code,
                _unsend(client, CHAT, db.guids["fresh2"]).status_code,
                _unsend(client, CHAT, db.guids["blob"]).status_code,
                _unsend(client, GROUP, db.guids["group"]).status_code,
                _edit(client, CHAT, db.guids["fresh2"]).status_code]
    fake_cli.configure(mode="exit", code=1)
    statuses.append(_edit(client, CHAT, db.guids["blob"]).status_code)
    fake_cli.configure(mode="ok")
    statuses.append(_edit(client, CHAT, db.guids["blob"]).status_code)
    for name in ("theirs", "sms", "unsent", "min16"):
        statuses.append(_edit(client, CHAT, db.guids[name]).status_code)
        statuses.append(_unsend(client, CHAT, db.guids[name]).status_code)
    assert statuses == [200, 502, 502, 502, 200, 502, 502, 403, 403, 409, 409, 409, 409, 409, 409]
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "[unsend] bb: confirmed in chat.db",
        "[unsend] bb: reported ok, but chat.db did not change",
        "[unsend] bluebubbles failed (HTTP 500, body not quoted (%d characters, not a JSON object))"
        % len(f"boom {CHAT} {OLD_TEXT}"),
        "[unsend] bluebubbles failed (BlueBubbles failed (ConnectError))",
        "[edit] imessage-cli: confirmed in chat.db",
        "[edit] imessage-cli failed (imessage-cli exited 1)",
        "[edit] imessage-cli: reported ok, but chat.db did not change",
    ]                                                      # the refusals log nothing at all
    assert_clean(db, out, err)


def test_the_hop_log_wrapper_drops_the_chat_guid_the_chain_appends(r, capsys):
    log = r._change_log("edit")
    log(f"[send] delivered via imessage-cli -> {CHAT}")
    log("[send] bluebubbles failed (x) — trying imessage-cli")
    assert capsys.readouterr().out == ("[edit] delivered via imessage-cli\n"
                                       "[edit] bluebubbles failed (x) — trying imessage-cli\n")


def test_the_hop_log_blanks_the_requests_own_identifiers(r, capsys):
    guid = guid_for("fresh")
    log = r._change_log("unsend", guid, CHAT, "short", "")
    log(f"[send] bluebubbles failed (HTTP 502: no route for /api/v1/message/{guid}/unsend in {CHAT} "
        f"or {quote(CHAT, safe='')}, short of it)")
    out = capsys.readouterr().out
    assert out == ("[unsend] bluebubbles failed (HTTP 502: no route for /api/v1/message/***/unsend in *** "
                   "or ***, short of it)\n")                  # an identifier of a few letters is left alone


def test_an_upstream_error_that_quotes_the_request_does_not_put_it_in_the_log(r, db, bb, client, capsys):
    """BlueBubbles' JSON error body is logged (without its ``data``) and is
    not handed to the client at all; a proxy in front of it may quote the URL
    it was asked for."""
    guid = db.guids["fresh"]
    quoted = json.dumps({"status": 400, "message": f"no route for {r.BB_URL}/api/v1/message/{guid}/unsend"
                                                   f"?password={RELAY_STUB_ENV['BB_PASSWORD']} ({CHAT})"})
    bb.answers = [FakeResponse(400, text=quoted)]
    resp = _unsend(client, CHAT, guid)
    assert (resp.status_code, resp.json()) == (502, REFUSED)
    assert guid not in resp.text and RELAY_STUB_ENV["BB_PASSWORD"] not in resp.text
    assert "no route" not in resp.text and "/api/v1/message" not in resp.text
    out, err = capsys.readouterr()
    assert out.startswith("[unsend] bluebubbles failed (HTTP 400: ") and "/api/v1/message/***/unsend" in out
    assert_clean(db, out, err)


# ---------------------------------------------------------------------------
# /health.capabilities
# ---------------------------------------------------------------------------

def test_health_capabilities_follow_the_chain(r, db, fake_cli, client, monkeypatch):
    def caps():
        return client.get("/health", headers=AUTH).json()["capabilities"]

    assert caps() == ["create_chat", "edit", "react", "reply", "unsend"]           # BlueBubbles + the tool
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", None)
    assert caps() == ["create_chat", "react", "reply", "unsend"]
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    assert caps() == []                                                            # AppleScript alone
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", str(fake_cli.binary))
    assert caps() == ["edit"]
    monkeypatch.setattr(r.beeper, "BEEPER_TOKEN", "stub-beeper-token-not-real")
    assert caps() == ["edit"]                                                      # Beeper's reply is not iMessage's
    monkeypatch.setattr(r, "SEND_ENGINES", "applescript")
    assert caps() == ["edit"]                                                      # the list orders the senders only
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", None)
    assert caps() == []


def test_health_is_additive_and_says_nothing_new_without_the_token(r, client):
    body = client.get("/health", headers=AUTH).json()
    assert list(body) == ["ok", "cursor", "contacts", "self", "bb_reachable", "engines", "features",
                          "protocol", "capabilities"]
    assert body["protocol"] == 1                                                   # additive: no bump
    assert body["engines"] == ["bluebubbles", "applescript"]
    assert body["capabilities"] == sorted(body["capabilities"])
    assert set(body["capabilities"]) <= {"react", "reply", "create_chat", "unsend", "edit"}
    for kw in ({}, {"headers": {"X-Imsg-Token": "wrong"}}):
        assert client.get("/health", **kw).json() == {"ok": True}


# ---------------------------------------------------------------------------
# the doctor's "edit / unsend" row
# ---------------------------------------------------------------------------

def _no_probe(url, headers=None, timeout=None):
    return None


def _row(r) -> tuple[str, str]:
    by = {name: (status, hint) for name, status, hint in r.doctor_rows(probe=_no_probe)}
    return by["edit / unsend"]


def test_doctor_row_states(r, db, fake_cli, monkeypatch, tmp_path):
    status, hint = _row(r)
    assert status == "edit: imessage-cli found | unsend: BlueBubbles"
    assert hint.startswith(f"{fake_cli.binary} drives Messages.app") and "Accessibility and Automation" in hint
    assert "`imessage-cli authorize` shows them, and asks for a missing one, for the program that runs it" in hint
    assert r.mask_token(hint) == hint                                  # prints as written at startup

    # a pinned SEND_ENGINES list changes nothing here: the edit engine joins all the same
    monkeypatch.setattr(r, "SEND_ENGINES", "bluebubbles,applescript")
    assert _row(r) == (status, hint)
    monkeypatch.setattr(r, "SEND_ENGINES", "")

    for off in ("0", "off", "OFF"):                                     # switched off on purpose: no hint
        monkeypatch.setattr(r, "IMESSAGE_CLI", off)
        monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", r.find_imessage_cli(off))
        assert _row(r) == ("edit: off (IMESSAGE_CLI) | unsend: BlueBubbles", ""), off

    monkeypatch.setattr(r, "IMESSAGE_CLI", "")                          # unset, and nothing installed
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", None)
    status, hint = _row(r)
    assert status == "edit: not available (imessage-cli not found) | unsend: BlueBubbles"
    assert hint == "to edit sent messages: brew install beeper/tap/imessage-cli"

    missing = str(tmp_path / "SECRET-FOLDER" / "imessage-cli")
    monkeypatch.setattr(r, "IMESSAGE_CLI", missing)
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", r.find_imessage_cli(missing))
    status, hint = _row(r)
    assert status == "edit: not available (IMESSAGE_CLI names no executable file) | unsend: BlueBubbles"
    assert hint == "point IMESSAGE_CLI at the imessage-cli binary, or remove the key to search for it"
    assert "SECRET-FOLDER" not in status + hint                         # a path that names no binary is not echoed

    monkeypatch.setattr(r, "BB_PASSWORD", "")
    monkeypatch.setattr(r, "IMESSAGE_CLI", "")
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", None)
    status, hint = _row(r)
    assert status == "edit: not available (imessage-cli not found) | unsend: not available"
    assert hint == ("to edit sent messages: brew install beeper/tap/imessage-cli; "
                    "unsend needs BlueBubbles with the Private API (BB_PASSWORD)")


def test_doctor_row_says_when_the_accessibility_grant_is_missing(r, db, fake_cli, monkeypatch):
    """Asked of macOS for the doctor's own process, which under launchd is the
    relay's Python; the question never prompts. Unknown (``None``) is not
    reported as missing."""
    asked = []

    def answer(value):
        return lambda: asked.append(value) or value

    monkeypatch.setattr(cli, "accessibility_trusted", answer(False))
    status, hint = _row(r)
    assert status == "edit: imessage-cli found, Accessibility NOT GRANTED | unsend: BlueBubbles"
    assert hint.startswith("grant Accessibility to the Python that runs the relay (System Settings > Privacy & "
                           "Security) and restart it: until then /edit is refused without starting the tool")
    assert "this row shows Terminal's grant, not the LaunchAgent's" in hint
    assert f"; {fake_cli.binary} drives Messages.app" in hint
    for value in (None, True):
        monkeypatch.setattr(cli, "accessibility_trusted", answer(value))
        status, hint = _row(r)
        assert status == "edit: imessage-cli found | unsend: BlueBubbles" and "NOT GRANTED" not in hint
    assert asked == [False, None, True]
    # the question is not put at all where there is no edit engine to ask it for
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", None)
    _row(r)
    assert asked == [False, None, True]
    assert fake_cli.calls == []


def test_doctor_row_shows_the_installed_version_and_says_when_it_is_not_the_checked_one(r, db, monkeypatch,
                                                                                         tmp_path):
    """Homebrew's link points into a folder named after the version; a ``brew
    upgrade`` swaps the binary behind the same path, and the engine's rules
    were checked against one version."""
    assert r.IMESSAGE_CLI_CHECKED == "0.24.2"

    def install(version: str) -> str:
        real = tmp_path / "homebrew" / "Cellar" / "imessage-cli" / version / "bin" / "imessage-cli"
        real.parent.mkdir(parents=True)
        real.write_text("#!/bin/sh\nexit 0\n")
        real.chmod(0o755)
        link = tmp_path / "homebrew" / "bin" / "imessage-cli"
        link.parent.mkdir(exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(real)
        assert_fake(link, tmp_path)
        return str(link)

    binary = install("0.24.2")
    monkeypatch.setattr(r, "IMESSAGE_CLI", binary)
    monkeypatch.setattr(r, "IMESSAGE_CLI_BIN", binary)
    status, hint = _row(r)
    assert status == "edit: imessage-cli 0.24.2 found | unsend: BlueBubbles"
    assert "checked with" not in hint and hint.startswith(f"{binary} drives Messages.app")
    install("0.24.5")
    status, hint = _row(r)
    assert status == "edit: imessage-cli 0.24.5 found | unsend: BlueBubbles"
    assert hint.endswith("; the edit engine was checked with imessage-cli 0.24.2: make one edit of a test "
                         "message with this version (`brew pin imessage-cli` keeps a version)")
    assert str(tmp_path / "homebrew" / "Cellar") not in status + hint   # the link's place, not where it points


def test_doctor_runs_nothing_and_prints_no_other_path(r, db, fake_cli, monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(cli, "_run_tool", lambda *a, **k: ran.append(a))
    rows = r.doctor_rows(probe=_no_probe)
    text = r.doctor_table(rows)
    assert ran == [] and fake_cli.calls == []                           # the binary is looked for, never started
    names = [name for name, _, _ in rows]
    assert names[names.index("send engines") + 1] == "edit / unsend"
    assert "send engines" in text and "bluebubbles, applescript, imessage-cli" in text
    line = next(l for l in text.splitlines() if l.strip().startswith("edit / unsend"))
    assert str(fake_cli.binary) in line
    assert str(tmp_path / "data" / "imessage-cli") not in text          # the tool's state directory is not listed
    assert not (tmp_path / "data" / "imessage-cli").exists()            # ... and not created by looking


# ---------------------------------------------------------------------------
# the other routes are what they were
# ---------------------------------------------------------------------------

def test_the_edit_engine_is_never_asked_by_any_other_route(r, db, bb, fake_cli, osa, client):
    bb.answers = [FakeResponse(200, {"status": 200, "data": {"guid": "synthetic"}}),
                  FakeResponse(200, {"status": 200}), FakeResponse(200, {"status": 200})]
    resp = client.post("/send", json={"chat_guid": CHAT, "text": "plain"}, headers=AUTH)
    assert resp.json() == {"ok": True, "via": "bb", "bb": {"status": 200, "data": {"guid": "synthetic"}}}
    resp = client.post("/react", json={"chat_guid": CHAT, "message_guid": db.guids["theirs"],
                                       "reaction": "love"}, headers=AUTH)
    assert resp.json() == {"ok": True}
    resp = client.post("/send_attachment", data={"chat_guid": CHAT},
                       files={"file": ("a.png", b"\x89PNG", "image/png")}, headers=AUTH)
    assert resp.json() == {"ok": True}
    assert [c.path for c in bb.calls] == ["/api/v1/message/text", "/api/v1/message/react",
                                          "/api/v1/message/attachment"]
    assert fake_cli.calls == [] and osa == []
    paths = {route.path for route in r.app.routes}
    assert {"/unsend", "/edit", "/react", "/send", "/send_attachment", "/health"} <= paths
