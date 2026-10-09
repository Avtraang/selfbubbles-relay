"""A notification that is slow to send does not hold up receiving.

Audit finding R3-F3 (and the /bb_event part of R9-F5), reproduced here before
the fix. The relay sent each push while it was handling the message: the
iMessage loop and the Google Messages watcher both waited for Firebase before
they looked at the next message, with the library's own two-minute timeout,
and the FaceTime webhook sent on the event loop itself, which stops everything
the relay does. One push that hung delayed every message behind it, the frames
for an open app included.

Also pinned: a row that was passed on is not passed on again because a later
step of the same round failed (the position in the database used to be saved
once per round, so the whole round was repeated).

Firebase is a script here: with the gate closed it does not answer.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import threading
import time
import types

import pytest

from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import r  # noqa: F401  (fixture)

CHAT = "iMessage;-;+15550001234"          # synthetic
HANG_SECONDS = 3.0                        # how long the scripted Firebase holds a send when nobody opens the gate


@pytest.fixture
def fcm(r, monkeypatch):
    f = types.SimpleNamespace(sent=[], frames=[], gate=threading.Event(), broadcast_fails=collections.Counter())
    f.gate.set()

    class Unregistered(Exception):
        pass

    def send(m):
        f.gate.wait(HANG_SECONDS)
        f.sent.append(m.data.get("guid") or m.data.get("ft_event"))

    fake = types.SimpleNamespace(
        Message=lambda token=None, data=None, android=None: types.SimpleNamespace(token=token, data=dict(data)),
        AndroidConfig=lambda priority=None, **kw: None,
        UnregisteredError=Unregistered,
        send=send,
    )
    monkeypatch.setattr(r, "fb_messaging", fake, raising=False)
    monkeypatch.setattr(r, "FCM_READY", True)
    monkeypatch.setattr(r, "CHAT_TITLES", {})
    monkeypatch.setattr(r, "load_contacts", lambda: None)
    monkeypatch.setattr(r, "enrich_links", lambda msgs: [])
    if hasattr(r, "PushLane"):                               # nothing left waiting from another test
        monkeypatch.setattr(r, "MESSAGE_PUSHES", r.PushLane(r.PUSH_QUEUE_MAX))
        monkeypatch.setattr(r, "CALL_PUSHES", r.PushLane(20))
    r.save_state(push_tokens=["synthetic-device-registration"])

    async def record(payload):
        key = payload["data"].get("guid") or payload["data"].get("event")
        if f.broadcast_fails[key] > 0:
            f.broadcast_fails[key] -= 1
            raise RuntimeError("synthetic: this frame could not be passed on")
        f.frames.append((payload["type"], key))

    monkeypatch.setattr(r.hub, "broadcast", record)
    yield f
    f.gate.set()


async def settled(r, timeout: float = 5.0) -> None:
    """Wait until no push is waiting or being sent."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if r.MESSAGE_PUSHES.idle() and r.CALL_PUSHES.idle():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("pushes were still waiting")


def gm(mid: str) -> dict:
    return {"chat_guid": "bp:401", "chat_name": "", "sender": "Stand In", "text": f"synthetic {mid}", "rowid": 0,
            "guid": mid, "is_from_me": False, "attachments": [], "is_group": False}


def row(rowid: int, guid: str, from_me: bool = False) -> dict:
    return {"rowid": rowid, "guid": guid, "text": f"synthetic {guid}", "attachments": [], "has_attachments": False,
            "is_from_me": from_me, "assoc_type": 0, "chat_guid": CHAT, "chat_name": "", "sender": "Stand In",
            "is_group": False, "link": None}


def run_poll(r, fcm, monkeypatch, rows, until, seconds: float = 2.0, then=None):
    """Run the receive loop over scripted rows until `until()` holds (or `seconds` pass); `then()` runs
    in the same event loop before the loop is stopped."""
    base = r.max_rowid()
    r.save_cursor(base)
    for i, x in enumerate(rows, 1):
        x["rowid"] = base + i
    monkeypatch.setattr(r, "POLL_SECONDS", 0.005)
    monkeypatch.setattr(r, "fetch_new", lambda cursor: [x for x in rows if x["rowid"] > cursor])
    monkeypatch.setattr(r, "fetch_edited", lambda mark: ([], mark))
    out = types.SimpleNamespace(base=base, reached=False)

    async def scenario():
        task = asyncio.create_task(r.poll_loop())
        end = time.monotonic() + seconds
        while time.monotonic() < end and not task.done():
            if until():
                out.reached = True
                break
            await asyncio.sleep(0.005)
        out.frames, out.sent = list(fcm.frames), list(fcm.sent)
        settle = time.monotonic() + 2.0                   # the position is saved right behind each frame
        while out.reached and r.load_cursor() < base + len(rows) and time.monotonic() < settle:
            await asyncio.sleep(0.005)
        out.cursor = r.load_cursor()
        if then is not None:
            await then()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return out, scenario


# ---------------------------------------------------------------------------
# Google Messages
# ---------------------------------------------------------------------------

def test_a_google_messages_push_that_hangs_does_not_hold_up_the_next_text(r, fcm):
    fcm.gate.clear()

    async def scenario():
        t0 = time.monotonic()
        await asyncio.wait_for(r._on_beeper_message(gm("m1"), True), 1.0)
        await asyncio.wait_for(r._on_beeper_message(gm("m2"), True), 1.0)
        took = time.monotonic() - t0
        seen = (list(fcm.frames), list(fcm.sent))
        fcm.gate.set()
        await settled(r)
        return took, seen

    took, (frames, sent_while_held) = asyncio.run(scenario())
    assert took < 0.5
    assert frames == [("message", "m1"), ("message", "m2")] and sent_while_held == []
    assert fcm.sent == ["m1", "m2"]                       # both notified, in order, once Firebase answers


def test_a_push_that_fails_outright_does_not_stop_the_one_behind_it(r, fcm, monkeypatch, capsys):
    real = r.send_push

    def send_push(msg):
        if msg["guid"] == "m1":
            raise RuntimeError("synthetic: the push code itself failed")
        return real(msg)

    monkeypatch.setattr(r, "send_push", send_push)

    async def scenario():
        await r._on_beeper_message(gm("m1"), True)
        await r._on_beeper_message(gm("m2"), True)
        await settled(r)

    asyncio.run(scenario())
    assert fcm.frames == [("message", "m1"), ("message", "m2")] and fcm.sent == ["m2"]
    assert "synthetic" not in capsys.readouterr().out


def test_waiting_pushes_are_bounded_and_the_oldest_give_way(r, fcm, monkeypatch, capsys):
    monkeypatch.setattr(r.MESSAGE_PUSHES, "limit", 5)
    fcm.gate.clear()

    async def scenario():
        for i in range(1, 10):
            await r._on_beeper_message(gm(f"m{i}"), True)
            await asyncio.sleep(0.01)                     # m1 is taken up and hangs; the rest wait
        waiting = len(r.MESSAGE_PUSHES.waiting)
        fcm.gate.set()
        await settled(r)
        return waiting

    waiting = asyncio.run(scenario())
    assert waiting == 5
    assert fcm.sent == ["m1", "m5", "m6", "m7", "m8", "m9"]
    assert len(fcm.frames) == 9                           # every message still reached the open app
    assert capsys.readouterr().out.count("gave way") == 3


# ---------------------------------------------------------------------------
# iMessage (the database loop)
# ---------------------------------------------------------------------------

def test_an_imessage_push_that_hangs_does_not_hold_up_the_rows_behind_it(r, compat_db, fcm, monkeypatch):
    rows = [row(0, "g1"), row(0, "g2"), row(0, "g3")]
    fcm.gate.clear()

    async def release():
        fcm.gate.set()
        await settled(r)

    out, scenario = run_poll(r, fcm, monkeypatch, rows, until=lambda: len(fcm.frames) >= 3, seconds=1.5, then=release)
    asyncio.run(scenario())
    assert out.reached, f"only {out.frames} were passed on while the first push hung"
    assert out.frames == [("message", "g1"), ("message", "g2"), ("message", "g3")] and out.sent == []
    assert out.cursor == out.base + 3                     # and the position was saved past all three
    assert fcm.sent == ["g1", "g2", "g3"]                 # notified, in order, once Firebase answered


def test_the_pushes_of_one_round_go_out_in_order(r, compat_db, fcm, monkeypatch):
    rows = [row(0, "g1"), row(0, "mine", from_me=True), row(0, "g2"), row(0, "g3")]
    out, scenario = run_poll(r, fcm, monkeypatch, rows, until=lambda: len(fcm.sent) >= 3)
    asyncio.run(scenario())
    assert fcm.sent == ["g1", "g2", "g3"]                 # nothing for the row that is my own
    assert [k for _, k in fcm.frames] == ["g1", "mine", "g2", "g3"]


def test_a_row_that_was_passed_on_is_not_passed_on_again_when_the_one_behind_it_fails(r, compat_db, fcm, monkeypatch):
    rows = [row(0, "g1"), row(0, "g2")]
    fcm.broadcast_fails["g2"] = 2                         # two rounds in which g2 cannot be passed on
    out, scenario = run_poll(r, fcm, monkeypatch, rows, until=lambda: ("message", "g2") in fcm.frames and len(fcm.sent) >= 2)
    asyncio.run(scenario())
    assert out.reached
    assert fcm.frames == [("message", "g1"), ("message", "g2")]       # g1 once, although its round failed twice
    assert fcm.sent == ["g1", "g2"]
    assert out.cursor == out.base + 2


# ---------------------------------------------------------------------------
# FaceTime (the BlueBubbles webhook)
# ---------------------------------------------------------------------------

class _Webhook:
    def __init__(self, status: str):
        self._body = {"type": "ft-call-status-changed",
                      "data": {"status": status, "uuid": "CALL-0001", "address": "+15550001234", "is_video": True}}

    async def json(self):
        return self._body


def test_a_facetime_push_that_hangs_does_not_stop_the_relay(r, fcm):
    fcm.gate.clear()

    async def scenario():
        task = asyncio.create_task(r.bb_event(_Webhook(r.FT_STATUS_INCOMING)))
        t0 = time.monotonic()
        await asyncio.sleep(0.05)                         # whatever else the relay has to do meanwhile
        lag = time.monotonic() - t0
        ring_shown = ("facetime", "incoming") in fcm.frames
        await asyncio.wait_for(task, 1.0)                 # the webhook is answered without waiting for Firebase
        answered_while_held = fcm.sent == []
        fcm.gate.set()
        await settled(r)
        return lag, ring_shown and answered_while_held

    lag, ring_shown = asyncio.run(scenario())
    assert lag < 0.5, f"the relay did nothing else for {lag:.1f} s"
    assert ring_shown                                     # an open app rings without waiting for Firebase
    assert fcm.sent == ["incoming"]


# ---------------------------------------------------------------------------
# Firebase's own timeout
# ---------------------------------------------------------------------------

def test_firebase_is_given_a_short_timeout(r, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(r, "FCM_CREDS", str(tmp_path / "none.json"))
    monkeypatch.setattr(r, "FCM_READY", False)
    monkeypatch.setattr(r.fb_credentials, "Certificate", lambda path: object())
    monkeypatch.setattr(r.firebase_admin, "initialize_app",
                        lambda cred, options=None, **kw: seen.update(options=options))
    r.init_fcm()
    assert seen["options"] and 1 <= seen["options"]["httpTimeout"] <= 15
    assert r.firebase_admin._http_client.DEFAULT_TIMEOUT_SECONDS == 120      # what it was left at before
