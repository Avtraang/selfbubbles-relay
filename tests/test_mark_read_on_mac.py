"""A chat the phone opens is marked read in Messages on the Mac too.

Asked for on 2026-10-09: opening a conversation on the phone took its
notification away in the app only; Messages on the Mac kept the chat unread.
POST /read now also asks BlueBubbles (``POST /api/v1/chat/<guid>/read``, the
Private API's ``markChatRead``) after the relay's own read mark is saved.
Best effort: a refusal or a failure is one log line and the phone's answer is
"ok" regardless. A Google Messages chat is marked read in Beeper, as before.

Nothing talks to BlueBubbles: its answers are scripted (``bb``).
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import pytest

from engines import EngineError, SendResult
from engines.bluebubbles import BAD_CHAT_GUID, BlueBubblesEngine
from tests.test_engines import BBServer, sync
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, CHAT, FakeResponse, bb, client, r  # noqa: F401  (fixtures)

PASSWORD = "synthetic-bb-password"


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

@pytest.fixture
def bbs() -> BBServer:
    return BBServer()


@pytest.fixture
def engine(bbs) -> BlueBubblesEngine:
    return BlueBubblesEngine("http://bb.invalid:1234", PASSWORD, transport=bbs.transport())


def test_mark_read_posts_to_the_chats_read_route_with_the_password_and_no_body(engine, bbs):
    bbs.answers = [(200, {"status": 200, "message": "Successfully marked chat as read!"})]
    assert sync(engine.mark_read(CHAT)) == SendResult(True, "bb")
    s = bbs.seen[0]
    assert (s.method, s.path, s.param_keys, s.json) == ("POST", f"/api/v1/chat/{CHAT}/read", ["password"], None)
    assert s.raw_path.decode().startswith(f"/api/v1/chat/{quote(CHAT, safe='')}/read?")   # one encoded segment


def test_the_guid_is_one_path_segment(engine, bbs):
    bbs.answers = [(200, {})]
    sync(engine.mark_read("iMessage;+;chat123/../../admin"))
    raw = bbs.seen[0].raw_path.decode()
    assert raw.startswith("/api/v1/chat/iMessage%3B%2B%3Bchat123%2F..%2F..%2Fadmin/read?")


def test_an_empty_guid_is_refused_before_anything_is_sent(engine, bbs):
    with pytest.raises(EngineError) as e:
        sync(engine.mark_read(""))
    assert BAD_CHAT_GUID in str(e.value)
    assert bbs.seen == []


def test_a_refusal_is_a_result_without_the_password_in_it(engine, bbs):
    bbs.answers = [(400, {"status": 400, "message": f"no such chat (password={PASSWORD})"})]
    res = sync(engine.mark_read(CHAT))
    assert res.ok is False and res.status == 400
    assert PASSWORD not in (res.detail or "") and PASSWORD not in (res.body or "")


def test_a_transport_failure_is_a_result_named_by_its_class(engine, bbs):
    bbs.answers = [(0, ConnectionError(f"synthetic: refused at http://bb.invalid:1234?password={PASSWORD}"))]
    res = sync(engine.mark_read(CHAT))
    assert res == SendResult(False, "bb", "BlueBubbles failed (ConnectionError)")   # the class alone: no URL, no password
    assert PASSWORD not in res.detail


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------

def read(client, chat=CHAT, rowid=5):
    return client.post("/read", json={"chat_guid": chat, "rowid": rowid}, headers=AUTH)


def test_opening_a_chat_marks_it_read_on_the_mac_too(r, bb, client, monkeypatch):
    monkeypatch.setattr(r, "BB_PASSWORD", PASSWORD)
    bb.answers = [FakeResponse(200, {"status": 200})]
    assert read(client).json() == {"ok": True}
    assert [(c.method, c.path, c.param_keys, c.json) for c in bb.calls] == \
           [("POST", f"/api/v1/chat/{quote(CHAT, safe='')}/read", ["password"], None)]   # one encoded segment
    assert int(r.load_state()["reads"][CHAT]) == 5                       # the relay's own mark, as before


def test_the_relays_mark_is_saved_before_the_mac_is_asked_and_kept_when_the_mac_refuses(r, bb, client, monkeypatch, capsys):
    monkeypatch.setattr(r, "BB_PASSWORD", PASSWORD)
    bb.answers = [FakeResponse(500, "synthetic BlueBubbles failure")]
    assert read(client).json() == {"ok": True}
    assert int(r.load_state()["reads"][CHAT]) == 5
    out = capsys.readouterr().out
    assert "[reads] Messages on the Mac was not told that a chat was read (HTTP 500)" in out
    assert CHAT not in out


def test_a_bluebubbles_that_does_not_answer_is_one_line_and_the_phone_still_gets_ok(r, bb, client, monkeypatch, capsys):
    monkeypatch.setattr(r, "BB_PASSWORD", PASSWORD)
    bb.answers = [ConnectionError("synthetic: refused")]
    assert read(client).json() == {"ok": True}
    out = capsys.readouterr().out
    assert "[reads] Messages on the Mac was not told that a chat was read (BlueBubbles failed (ConnectionError))" in out


def test_without_a_bluebubbles_password_the_mac_is_not_asked(r, bb, client, monkeypatch):
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    assert read(client).json() == {"ok": True}
    assert bb.calls == []
    assert int(r.load_state()["reads"][CHAT]) == 5


def test_the_setting_switches_it_off(r, bb, client, monkeypatch):
    monkeypatch.setattr(r, "BB_PASSWORD", PASSWORD)
    monkeypatch.setattr(r, "BB_MARK_READ", False)
    assert read(client).json() == {"ok": True}
    assert bb.calls == []


def test_a_google_messages_chat_goes_to_beeper_not_to_the_mac(r, bb, client, monkeypatch):
    monkeypatch.setattr(r, "BB_PASSWORD", PASSWORD)
    asked = []

    async def beeper_mark_read(chat_guid):
        asked.append(chat_guid)
        return True

    monkeypatch.setattr(r.beeper, "mark_read", beeper_mark_read)
    assert read(client, chat="bp:401", rowid=0).json() == {"ok": True}
    assert asked == ["bp:401"] and bb.calls == []


def test_a_mac_that_takes_too_long_does_not_hold_the_phone(r, monkeypatch, capsys):
    monkeypatch.setattr(r, "BB_PASSWORD", PASSWORD)
    monkeypatch.setattr(r, "MARK_READ_ON_MAC_SECONDS", 0.05)

    class Slow:
        def configured(self):
            return True

        async def mark_read(self, chat_guid):
            await asyncio.sleep(5)

    monkeypatch.setattr(r, "_bluebubbles", lambda: Slow())
    asyncio.run(asyncio.wait_for(r.mark_read_on_mac(CHAT), 1))
    assert "[reads] Messages on the Mac did not answer within 0 s" in capsys.readouterr().out


def test_a_guid_that_cannot_be_a_path_segment_is_not_sent_and_not_logged(r, bb, client, monkeypatch, capsys):
    monkeypatch.setattr(r, "BB_PASSWORD", PASSWORD)
    assert read(client, chat="...").json() == {"ok": True}
    assert bb.calls == []
    assert "[reads]" not in capsys.readouterr().out
