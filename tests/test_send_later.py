"""Send Later, relay side (2026-10-11).

Seen: the app could send a text now or not at all; a message meant for the
morning had to be remembered by the owner. The relay had everything a
scheduled send needs (the send path, the send ids that keep one id to one
message, the state file, the WebSocket and the push lane) and no place to
keep a text until its time.

Changed: ``POST /scheduled`` takes a text, a chat, a unix time at least a
minute and at most 30 days ahead, and the send id the delivery will run
under; ``GET``, ``PATCH``, ``DELETE`` and ``POST .../send_now`` list, change,
cancel and deliver. A background loop (``scheduled.Scheduler.run``) sleeps
until the earliest item is due or 30 s, wakes at once on a change, and
delivers through ``deliver_text`` under the item's ``client_id`` via
``_send_once``, so a relay that stopped in the middle cannot send it twice:
on the next start the send-id file says what became of it. An item due while
the relay was down goes out up to six hours late and fails after that with a
fixed reason. A send the send-id file could not record (it cannot be read or
written: certainly not sent, most likely passing) does not fail the item,
which a failed state would leave without a way to retry: it is pending again
and tried at the next look, for as long as the late window lasts. Every
change is a ``{"type": "scheduled"}`` frame; a finished item is one push with
``kind`` ``scheduled``. Finished items stay listed for a day. ``/health``
names ``schedule`` among its features; ``IMSG_SCHEDULE=0`` turns it off and
the routes answer 404.

The scheduler runs on a clock the test moves and a sleep hook that moves it:
nothing here waits for real, apart from one test that shows ``wake()`` ends
the real wait at once (bounded by a second, not by what the loop would have
slept). BlueBubbles and osascript are the suite's recording fakes; the send
ids live in a file under ``tmp_path``; every value is synthetic.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import types

import pytest

import scheduled
from engines.base import EFFECT_NOT_FOR_GOOGLE_MESSAGES, UNKNOWN_EFFECT
from scheduled import (BAD_CLIENT_ID, CHAT_EMPTY, IDS_UNAVAILABLE, NEW_ID_FOR_FAILED_ONLY, NOT_PENDING_CANCEL, NOT_PENDING_EDIT,
                       NOT_PENDING_SEND, NOT_RUNNING, OUTCOME_UNKNOWN, PUBLIC_KEYS, TEXT_EMPTY, TOO_FAR, TOO_SOON,
                       UNKNOWN_ITEM)
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_id import Died, ids, restart  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, CHAT, TEXT, FakeResponse, _bb_ok, bb, client, osa, r  # noqa: F401

SLAM = "com.apple.MobileSMS.expressivesend.impact"
ID = "synthetic-later-0001"
OTHER_ID = "synthetic-later-0002"
FRESH_ID = "synthetic-later-anyway-0003"
MSG_GUID = "p:0/11111111-2222-3333-4444-555555555555"
START = 1_800_000_000                                    # a unix second; what it is does not matter
HOUR = 3600


class SteppedClock:
    """Unix seconds that only the test moves. ``sleep`` is the loop's wait:
    the time passes at once and the length asked for is kept."""

    def __init__(self) -> None:
        self.now, self.slept = float(START), []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_sched(r, clock, *, sleep="stepped") -> scheduled.Scheduler:
    """The relay's scheduler on the test's clock: the very hooks relay.py gives
    its own (they look the relay's globals up at call time, so the suite's
    fakes for the engines, the send ids and the hub hold), the time replaced."""
    live = r.SCHEDULER
    return scheduled.Scheduler(
        load_state=live.load_state, change_state=live.change_state, deliver=live.deliver,
        send_once=live.send_once, fingerprint=live.fingerprint, id_ok=live.id_ok, broadcast=live.broadcast,
        push=live.push, chat_name=live.chat_name, enabled=live.enabled, clock=clock,
        sleep=clock.sleep if sleep == "stepped" else None)


@pytest.fixture
def clock() -> SteppedClock:
    return SteppedClock()


@pytest.fixture
def sched(r, clock, ids, monkeypatch) -> scheduled.Scheduler:
    s = make_sched(r, clock)
    monkeypatch.setattr(r, "SCHEDULER", s)
    return s


@pytest.fixture
def events(r, monkeypatch) -> list:
    seen: list = []

    async def record(payload):
        seen.append(copy.deepcopy(payload))

    monkeypatch.setattr(r.hub, "broadcast", record)
    return seen


@pytest.fixture
def pushes(r, monkeypatch) -> list:
    """Firebase replaced by a recorder, one registered device, the chat listed under a name."""
    sent: list = []

    class Unregistered(Exception):
        pass

    fake = types.SimpleNamespace(
        Message=lambda token=None, data=None, android=None: types.SimpleNamespace(token=token, data=dict(data)),
        AndroidConfig=lambda priority=None: None, UnregisteredError=Unregistered,
        send=lambda m: sent.append(m.data))
    monkeypatch.setattr(r, "fb_messaging", fake, raising=False)
    monkeypatch.setattr(r, "FCM_READY", True)
    monkeypatch.setattr(r, "MESSAGE_PUSHES", r.PushLane(r.PUSH_QUEUE_MAX))
    monkeypatch.setattr(r, "CHAT_TITLES", {CHAT: "Synthetic Person"})
    r.save_state(push_tokens=["synthetic-device-registration"])
    return sent


def at(clock, seconds: int) -> int:
    return int(clock.now) + seconds


def post(client, clock, **body):
    base = {"chat_guid": CHAT, "text": TEXT, "send_at": at(clock, 120), "client_id": ID}
    return client.post("/scheduled", json={**base, **body}, headers=AUTH)


def listed(client, chat=None):
    return client.get("/scheduled", params={"chat_guid": chat} if chat else None, headers=AUTH).json()["items"]


def tick(r, sched):
    """One look by the loop, and the push lane drained so a test can read what it sent."""
    async def go():
        due = await sched.tick()
        for _ in range(300):
            if r.MESSAGE_PUSHES.idle():
                break
            await asyncio.sleep(0.005)
        return due

    return asyncio.run(go())


def stored(relay_module) -> dict:
    return json.loads(relay_module.state_path.read_text()).get("scheduled", {})


def schedule_lines(capsys) -> list:
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("[schedule]")]


# ---------------------------------------------------------------------------
# create and list
# ---------------------------------------------------------------------------

def test_a_scheduled_text_is_kept_as_pending_and_listed_by_time(client, clock, sched, events):
    later = post(client, clock, send_at=at(clock, 300), client_id=OTHER_ID).json()["item"]
    resp = post(client, clock)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "item"} and body["ok"] is True
    item = body["item"]
    assert list(item) == list(PUBLIC_KEYS)
    assert item["chat_guid"] == CHAT and item["text"] == TEXT and item["send_at"] == at(clock, 120)
    assert item["created_at"] == START and item["client_id"] == ID
    assert (item["reply_to_guid"], item["effect"], item["state"]) == (None, None, "pending")
    assert (item["sent_at"], item["sent_via"], item["error"]) == (None, None, None)
    assert listed(client) == [item, later]                                   # by send_at, not by creation
    assert listed(client, chat=CHAT) == [item, later] and listed(client, chat="iMessage;-;+15550009999") == []
    assert events[-1] == {"type": "scheduled", "chat_guid": CHAT, "id": item["id"], "state": "created"}


def test_a_reply_with_an_effect_is_kept_as_asked(client, clock, sched):
    item = post(client, clock, reply_to_guid=MSG_GUID, effect=SLAM).json()["item"]
    assert (item["reply_to_guid"], item["effect"]) == (MSG_GUID, SLAM)


@pytest.mark.parametrize("body, detail", [
    ({"text": "   "}, TEXT_EMPTY),
    ({"text": ""}, TEXT_EMPTY),
    ({"chat_guid": " "}, CHAT_EMPTY),
    ({"send_at": 59}, TOO_SOON),                     # relative to the clock: see below
    ({"send_at": 30 * 86400 + 1}, TOO_FAR),
    ({"effect": "Slam"}, UNKNOWN_EFFECT),            # the name is not the id
    ({"effect": ""}, UNKNOWN_EFFECT),
    ({"chat_guid": "bp:42", "effect": SLAM}, EFFECT_NOT_FOR_GOOGLE_MESSAGES),
    ({"client_id": "short"}, BAD_CLIENT_ID),
    ({"client_id": "has a space in it"}, BAD_CLIENT_ID),
])
def test_what_cannot_be_scheduled_is_422_with_a_short_reason_and_nothing_is_kept(client, clock, sched, body, detail):
    if "send_at" in body:
        body = {**body, "send_at": at(clock, body["send_at"])}
    resp = post(client, clock, **body)
    assert (resp.status_code, resp.json()["detail"]) == (422, detail), body
    assert listed(client) == []


def test_the_edges_of_the_window_are_allowed(client, clock, sched):
    assert post(client, clock, send_at=at(clock, 60)).status_code == 200
    assert post(client, clock, send_at=at(clock, 30 * 86400), client_id=OTHER_ID).status_code == 200


def test_the_send_id_is_required(client, clock, sched):
    resp = client.post("/scheduled", json={"chat_guid": CHAT, "text": TEXT, "send_at": at(clock, 120)}, headers=AUTH)
    assert resp.status_code == 422                                           # the body's shape, before the relay's rules
    assert listed(client) == []


def test_the_same_send_id_again_answers_the_item_that_exists_and_adds_nothing(client, clock, sched):
    first = post(client, clock).json()["item"]
    again = post(client, clock, text="another synthetic text", send_at=at(clock, 900))
    assert again.status_code == 200
    assert again.json() == {"ok": True, "item": first, "duplicate": True}    # the first, as it was
    assert listed(client) == [first]


def test_a_repeat_is_a_duplicate_even_once_its_time_is_too_close_or_it_was_sent(r, client, clock, sched, bb, osa):
    """The app repeats the same body after a lost answer. By then the time may
    be less than a minute away, or the item already sent; the time rule must
    not answer that repeat with 422, which reads as "nothing stored" and would
    lead to a second schedule under a fresh id (review of 2026-10-13)."""
    first = post(client, clock).json()["item"]                              # due in 2 minutes
    clock.advance(90)                                                        # 30 s away now: too soon for a new one
    again = post(client, clock, send_at=first["send_at"])
    assert (again.status_code, again.json().get("duplicate"), again.json()["item"]["id"]) == (200, True, first["id"])
    bb.answers = [_bb_ok()]
    clock.advance(60)
    tick(r, sched)
    assert listed(client)[0]["state"] == "sent"
    late = post(client, clock, send_at=first["send_at"])                     # in the past now
    assert (late.status_code, late.json().get("duplicate"), late.json()["item"]["state"]) == (200, True, "sent")
    assert len(bb.calls) == 1 and len(listed(client)) == 1


def test_the_routes_require_the_token(client, clock, sched):
    assert client.post("/scheduled", json={}).status_code == 401
    assert client.get("/scheduled").status_code == 401
    assert client.patch("/scheduled/x", json={}).status_code == 401
    assert client.delete("/scheduled/x").status_code == 401
    assert client.post("/scheduled/x/send_now").status_code == 401


# ---------------------------------------------------------------------------
# change and cancel
# ---------------------------------------------------------------------------

def test_a_pending_item_can_be_changed_field_by_field(client, clock, sched, events):
    item = post(client, clock).json()["item"]
    url = f"/scheduled/{item['id']}"
    resp = client.patch(url, json={"text": "changed synthetic text", "send_at": at(clock, 600), "effect": SLAM},
                        headers=AUTH)
    assert resp.status_code == 200
    changed = resp.json()["item"]
    assert (changed["text"], changed["send_at"], changed["effect"]) == ("changed synthetic text", at(clock, 600), SLAM)
    assert changed["created_at"] == item["created_at"] and changed["client_id"] == ID
    assert events[-1] == {"type": "scheduled", "chat_guid": CHAT, "id": item["id"], "state": "edited"}
    # a key left out keeps its value; a null effect clears it
    assert client.patch(url, json={"text": "once more"}, headers=AUTH).json()["item"]["effect"] == SLAM
    assert client.patch(url, json={"effect": None}, headers=AUTH).json()["item"]["effect"] is None
    assert listed(client)[0]["text"] == "once more"


def test_a_text_changed_just_before_it_goes_out_is_not_refused_for_being_due_soon(client, clock, sched):
    item = post(client, clock).json()["item"]
    clock.advance(100)                                                       # due in 20 s
    resp = client.patch(f"/scheduled/{item['id']}", json={"text": "last words"}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["item"]["text"] == "last words"
    # ... but a new time has to obey the window
    resp = client.patch(f"/scheduled/{item['id']}", json={"send_at": at(clock, 30)}, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (422, TOO_SOON)


@pytest.mark.parametrize("body, detail", [
    ({"text": " "}, TEXT_EMPTY),
    ({"send_at": 10}, TOO_SOON),
    ({"send_at": 31 * 86400}, TOO_FAR),
    ({"effect": "com.apple.messages.effect.CKNoSuchEffect"}, UNKNOWN_EFFECT),
])
def test_a_change_is_checked_like_a_new_item(client, clock, sched, body, detail):
    item = post(client, clock).json()["item"]
    if "send_at" in body:
        body = {**body, "send_at": at(clock, body["send_at"])}
    resp = client.patch(f"/scheduled/{item['id']}", json=body, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (422, detail), body
    assert listed(client) == [item]                                          # unchanged


def test_an_unknown_id_is_404_on_every_route_that_takes_one(client, clock, sched):
    for resp in (client.patch("/scheduled/nosuchitem", json={"text": "x"}, headers=AUTH),
                 client.delete("/scheduled/nosuchitem", headers=AUTH),
                 client.post("/scheduled/nosuchitem/send_now", headers=AUTH)):
        assert (resp.status_code, resp.json()["detail"]) == (404, UNKNOWN_ITEM)


def test_cancelling_removes_a_pending_item(client, clock, sched, events, relay_module):
    item = post(client, clock).json()["item"]
    resp = client.delete(f"/scheduled/{item['id']}", headers=AUTH)
    assert (resp.status_code, resp.json()) == (200, {"ok": True})
    assert listed(client) == [] and stored(relay_module) == {}
    assert events[-1] == {"type": "scheduled", "chat_guid": CHAT, "id": item["id"], "state": "cancelled"}
    assert client.delete(f"/scheduled/{item['id']}", headers=AUTH).status_code == 404


def test_a_failed_item_can_be_sent_again_or_let_go_but_not_changed(r, client, clock, sched, bb, osa):
    """The app's failed row offers Retry and Delete (review of 2026-10-11: the
    first contract said 409 for both, while telling the owner to do exactly
    that). A retry is the same send id again: a send that certainly failed may
    be repeated, one whose outcome is unknown is refused (the test above)."""
    bb.answers = [FakeResponse(500, text="synthetic BB failure"), FakeResponse(500, text="synthetic BB failure")]
    osa.results = [False, False]
    one = post(client, clock).json()["item"]
    two = post(client, clock, client_id=OTHER_ID).json()["item"]
    clock.advance(120)
    tick(r, sched)
    assert [it["state"] for it in listed(client)] == ["failed", "failed"]
    # not changed
    resp = client.patch(f"/scheduled/{one['id']}", json={"text": "other words"}, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (409, NOT_PENDING_EDIT)
    # a plain retry runs under the same id; an engine's 5xx leaves the outcome open (the relay's
    # own rule for /send too), so the id file refuses it before any engine is asked
    bb.answers = [_bb_ok()]
    again = client.post(f"/scheduled/{one['id']}/send_now", headers=AUTH).json()["item"]
    assert (again["state"], again["error"]) == ("failed", OUTCOME_UNKNOWN)
    assert len(bb.calls) == 2
    # "send anyway": a new id, so a new message, and BlueBubbles answers this time
    final = client.post(f"/scheduled/{one['id']}/send_now", json={"client_id": FRESH_ID}, headers=AUTH).json()["item"]
    assert (final["state"], final["sent_via"], final["error"], final["client_id"]) == ("sent", "bb", None, FRESH_ID)
    assert len(bb.calls) == 3                                   # two failed tries, one send anyway
    # let go
    assert client.delete(f"/scheduled/{two['id']}", headers=AUTH).json() == {"ok": True}
    assert [it["id"] for it in listed(client)] == [one["id"]]
    # and a sent one is final for both
    for resp, detail in ((client.delete(f"/scheduled/{one['id']}", headers=AUTH), NOT_PENDING_CANCEL),
                         (client.post(f"/scheduled/{one['id']}/send_now", headers=AUTH), NOT_PENDING_SEND)):
        assert (resp.status_code, resp.json()["detail"]) == (409, detail)


def test_a_finished_item_can_neither_be_changed_nor_cancelled_nor_sent_again(client, clock, sched, bb, osa):
    bb.answers = [_bb_ok()]
    item = post(client, clock).json()["item"]
    url = f"/scheduled/{item['id']}"
    assert client.post(url + "/send_now", headers=AUTH).json()["item"]["state"] == "sent"
    for resp, detail in ((client.patch(url, json={"text": "too late"}, headers=AUTH), NOT_PENDING_EDIT),
                         (client.delete(url, headers=AUTH), NOT_PENDING_CANCEL),
                         (client.post(url + "/send_now", headers=AUTH), NOT_PENDING_SEND)):
        assert (resp.status_code, resp.json()["detail"]) == (409, detail)
    assert len(bb.calls) == 1


# ---------------------------------------------------------------------------
# send now
# ---------------------------------------------------------------------------

def test_send_now_delivers_through_the_send_path_under_the_items_send_id(client, clock, sched, bb, osa, ids):
    bb.answers = [_bb_ok({"status": 200, "data": {"guid": "synthetic-bb-guid"}})]
    item = post(client, clock, reply_to_guid=MSG_GUID, effect=SLAM).json()["item"]
    clock.advance(5)
    resp = client.post(f"/scheduled/{item['id']}/send_now", headers=AUTH)
    assert resp.status_code == 200
    final = resp.json()["item"]
    assert (final["state"], final["sent_via"], final["sent_at"], final["error"]) == ("sent", "bb", START + 5, None)
    assert list(final) == list(PUBLIC_KEYS) and final["text"] == TEXT
    call = bb.calls[0]
    assert call.path == "/api/v1/message/text" and call.param_keys == ["password"]
    assert (call.json["chatGuid"], call.json["message"], call.json["effectId"], call.json["selectedMessageGuid"]) == \
        (CHAT, TEXT, SLAM, MSG_GUID)
    assert osa.calls == []
    # the send id is the one the app chose, with /send's own fingerprint: the
    # same text through /send under that id is the same message, not a second one
    assert ids.look(ID)["state"] == "delivered"
    again = client.post("/send", json={"chat_guid": CHAT, "text": TEXT, "reply_to_guid": MSG_GUID, "effect": SLAM,
                                       "client_id": ID}, headers=AUTH)
    assert again.json().get("duplicate") is True and len(bb.calls) == 1
    assert listed(client)[0] == final


def test_send_now_on_a_fallback_says_which_engine_sent_it(r, client, clock, sched, bb, osa):
    bb.answers = [FakeResponse(500, text="synthetic BB failure")]
    osa.results = [True]
    item = post(client, clock, effect=SLAM).json()["item"]
    final = client.post(f"/scheduled/{item['id']}/send_now", headers=AUTH).json()["item"]
    assert (final["state"], final["sent_via"]) == ("sent", "applescript")   # the text went plain: no effect
    assert bb.calls[0].json["effectId"] == SLAM and osa.calls == [(r._AS_TEXT, (CHAT, TEXT))]


# ---------------------------------------------------------------------------
# the loop: delivery at send_at
# ---------------------------------------------------------------------------

def test_the_loop_delivers_at_send_at_and_not_before(r, client, clock, sched, bb, osa, events, capsys):
    bb.answers = [_bb_ok()]
    item = post(client, clock, send_at=at(clock, 120)).json()["item"]
    assert tick(r, sched) == 120 and bb.calls == []
    clock.advance(119)
    assert tick(r, sched) == 1 and bb.calls == []
    clock.advance(1)
    assert tick(r, sched) is None                                            # nothing pending any more
    assert [c.json["message"] for c in bb.calls] == [TEXT]
    final = listed(client)[0]
    assert (final["state"], final["sent_via"], final["sent_at"]) == ("sent", "bb", START + 120)
    assert [e["state"] for e in events if e["type"] == "scheduled"] == ["created", "sending", "sent"]
    assert all(e["chat_guid"] == CHAT and e["id"] == item["id"] for e in events)
    lines = schedule_lines(capsys)
    assert f"[schedule] {item['id']}: created, due in 2 min" in lines
    assert f"[schedule] {item['id']}: sending" in lines and f"[schedule] {item['id']}: sent via bb" in lines
    assert not any(TEXT in line or CHAT in line for line in lines)


def test_a_delivery_that_fails_is_failed_with_a_reason_that_is_not_the_text(r, client, clock, sched, bb, osa, events, capsys):
    bb.answers = [FakeResponse(500, text="synthetic BB failure")]
    osa.results = [False]
    item = post(client, clock).json()["item"]
    clock.advance(120)
    tick(r, sched)
    final = listed(client)[0]
    assert (final["state"], final["sent_at"], final["sent_via"]) == ("failed", None, None)
    assert final["error"] == "the message could not be sent (HTTP 502)"
    assert TEXT not in final["error"] and CHAT not in final["error"]
    assert [e["state"] for e in events if e["type"] == "scheduled"] == ["created", "sending", "failed"]
    lines = schedule_lines(capsys)
    assert f"[schedule] {item['id']}: failed (the message could not be sent (HTTP 502))" in lines
    assert not any(TEXT in line or CHAT in line for line in lines)


def test_the_loop_sleeps_until_the_earliest_item_or_thirty_seconds(r, client, clock, sched, bb, osa):
    bb.answers = [_bb_ok(), _bb_ok()]
    assert post(client, clock, send_at=at(clock, 75)).status_code == 200
    assert post(client, clock, send_at=at(clock, 100), client_id=OTHER_ID).status_code == 200

    class Enough(Exception):
        pass

    async def sleep(seconds):
        await clock.sleep(seconds)
        if len(clock.slept) == 5:
            raise Enough

    sched._sleep = sleep
    with pytest.raises(Enough):
        asyncio.run(sched.run())
    assert clock.slept == [30, 30, 15, 25, 30]           # 75 capped at 30 twice, then exactly until due; 25 to the next; then the tick
    assert [c.json["message"] for c in bb.calls] == [TEXT, TEXT]
    assert [it["state"] for it in listed(client)] == ["sent", "sent"]


def test_a_change_wakes_the_loop(client, clock, sched, monkeypatch):
    wakes = []
    monkeypatch.setattr(sched, "wake", lambda: wakes.append(1))
    item = post(client, clock).json()["item"]
    client.patch(f"/scheduled/{item['id']}", json={"text": "x"}, headers=AUTH)
    client.delete(f"/scheduled/{item['id']}", headers=AUTH)
    assert len(wakes) == 3


def test_wake_ends_the_real_wait_at_once(r, clock, ids):
    """The one test on the real clock: the loop's wait would be 30 s, and
    ``wake()`` ends it now. Bounded by a second, which is a failure, not a
    measurement."""
    s = make_sched(r, clock, sleep=None)
    ticks = []
    real = s.tick

    async def counting():
        ticks.append(1)
        return await real()

    s.tick = counting

    async def until(pred):
        for _ in range(400):
            if pred():
                return
            await asyncio.sleep(0.0025)
        raise AssertionError("the loop did not come round")

    async def go():
        task = asyncio.create_task(s.run())
        await until(lambda: len(ticks) == 1)
        s.wake()
        await until(lambda: len(ticks) == 2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(go())


# ---------------------------------------------------------------------------
# a relay that stopped: the send-id file is the truth
# ---------------------------------------------------------------------------

def test_a_relay_that_stopped_in_the_middle_of_a_delivery_never_sends_it_again(r, client, clock, sched, bb, osa, ids,
                                                                              monkeypatch, relay_module):
    bb.answers = [Died()]
    item = post(client, clock).json()["item"]
    clock.advance(120)
    with pytest.raises(Died):                            # the process is gone; BlueBubbles may have had the message
        asyncio.run(sched.tick())
    assert stored(relay_module)[item["id"]]["state"] == "sending"
    assert json.loads(ids.path.read_text())[ID]["state"] == "started"
    # the next start: a new process, the files, nothing else
    restart(r, monkeypatch, ids)
    fresh = make_sched(r, clock)
    monkeypatch.setattr(r, "SCHEDULER", fresh)
    osa.results = [True]                                 # AppleScript would work: it must not be asked
    tick(r, fresh)
    final = listed(client)[0]
    assert (final["state"], final["error"]) == ("failed", OUTCOME_UNKNOWN)
    assert len(bb.calls) == 1 and osa.calls == []
    # and send now cannot force it: the retry runs under the same id, which the id file refuses
    again = client.post(f"/scheduled/{item['id']}/send_now", headers=AUTH)
    assert (again.status_code, again.json()["item"]["state"], again.json()["item"]["error"]) == (200, "failed", OUTCOME_UNKNOWN)
    assert len(bb.calls) == 1 and osa.calls == []
    # "send anyway": the owner accepts that it may arrive twice, so a new id, so a new message
    bb.answers = [_bb_ok()]
    anyway = client.post(f"/scheduled/{item['id']}/send_now", json={"client_id": FRESH_ID}, headers=AUTH).json()["item"]
    assert (anyway["state"], anyway["sent_via"], anyway["client_id"]) == ("sent", "bb", FRESH_ID)
    assert len(bb.calls) == 2
    # a new id is for a failed item only
    fresh = post(client, clock, client_id="pending-item-send-id").json()["item"]
    resp = client.post(f"/scheduled/{fresh['id']}/send_now", json={"client_id": "another-fresh-id"}, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (409, NEW_ID_FOR_FAILED_ONLY)
    assert client.post(f"/scheduled/{fresh['id']}/send_now", json={"client_id": "x"}, headers=AUTH).status_code == 422


def test_a_delivery_the_id_file_knows_as_delivered_is_recorded_as_sent_without_sending(r, client, clock, sched, bb, osa,
                                                                                     ids, monkeypatch, relay_module):
    """Stopped between the send ids' "delivered" and the state file's "sent":
    the item reads as sending, the id says it went, and it is not sent again."""
    bb.answers = [_bb_ok()]
    item = post(client, clock).json()["item"]
    clock.advance(120)
    tick(r, sched)
    r._change_state(lambda st: st["scheduled"][item["id"]].update(state="sending", sent_at=None, sent_via=None))
    restart(r, monkeypatch, ids)
    fresh = make_sched(r, clock)
    tick(r, fresh)
    final = listed(client)[0]
    assert (final["state"], final["sent_via"]) == ("sent", "bb") and len(bb.calls) == 1


# ---------------------------------------------------------------------------
# the send ids cannot be read or written: not sent, and not given up on
# ---------------------------------------------------------------------------

@pytest.fixture
def ids_down(ids, monkeypatch) -> list:
    """The send-id file unreadable for as long as the list holds True (/send
    answers 503 ``send_ids_unavailable`` then, and sends nothing)."""
    broken = [True]
    real = ids.look

    def look(sid):
        if broken[0]:
            raise PermissionError("synthetic: not readable right now")
        return real(sid)

    monkeypatch.setattr(ids, "look", look)
    return broken


def test_a_send_the_ids_cannot_record_leaves_the_item_pending_and_the_next_look_tries_again(
        r, client, clock, sched, bb, osa, ids_down, events, pushes, capsys):
    bb.answers = [_bb_ok()]
    item = post(client, clock).json()["item"]
    clock.advance(120)
    tick(r, sched)
    held = listed(client)[0]
    assert (held["state"], held["error"]) == ("pending", None) and bb.calls == [] and osa.calls == []
    assert pushes == []                                                      # nothing finished: nothing to tell
    assert [e["state"] for e in events if e["type"] == "scheduled"] == ["created", "sending", "pending"]
    assert f"[schedule] {item['id']}: not sent, the send ids are not available; pending again" in schedule_lines(capsys)
    # still pending, so the owner may change or cancel it meanwhile ...
    assert client.patch(f"/scheduled/{item['id']}", json={"text": "changed"}, headers=AUTH).status_code == 200
    # ... and once the ids are back the loop sends it, once
    ids_down[0] = False
    clock.advance(30)
    tick(r, sched)
    final = listed(client)[0]
    assert (final["state"], final["sent_via"]) == ("sent", "bb") and [c.json["message"] for c in bb.calls] == ["changed"]
    assert [p["state"] for p in pushes] == ["sent"]


@pytest.mark.parametrize("look_takes", [0, 31])
def test_an_item_the_ids_held_past_six_hours_fails_with_that_reason_not_the_relay_was_down(
        r, client, clock, sched, bb, osa, ids, ids_down, events, pushes, look_takes, monkeypatch):
    """Both ways past the edge: the loop's look finds the item too late to try
    (``look_takes`` 0), or the ids' failure itself took the item past it."""
    if look_takes:
        slow = ids.look

        def look(sid):
            clock.advance(look_takes)
            return slow(sid)

        monkeypatch.setattr(ids, "look", look)
    post(client, clock)
    clock.advance(120 + 6 * HOUR - 10)                   # ten seconds inside the window
    tick(r, sched)
    clock.advance(30)
    tick(r, sched)
    final = listed(client)[0]
    assert (final["state"], final["error"]) == ("failed", IDS_UNAVAILABLE)
    assert bb.calls == [] and osa.calls == []
    assert [p["state"] for p in pushes] == ["failed"]
    assert [e["state"] for e in events if e["type"] == "scheduled"][-1] == "failed"


def test_a_new_time_for_a_held_item_forgets_what_held_it(r, client, clock, sched, bb, osa, ids_down):
    item = post(client, clock).json()["item"]
    clock.advance(120)
    tick(r, sched)
    assert client.patch(f"/scheduled/{item['id']}", json={"send_at": at(clock, HOUR)}, headers=AUTH).status_code == 200
    clock.advance(8 * HOUR)                              # the relay was down past the new time
    tick(r, sched)
    assert listed(client)[0]["error"] == NOT_RUNNING


def test_send_now_while_the_ids_are_unavailable_answers_as_send_does_and_leaves_the_item_pending(
        client, clock, sched, bb, osa, ids_down):
    bb.answers = [_bb_ok()]
    item = post(client, clock).json()["item"]
    resp = client.post(f"/scheduled/{item['id']}/send_now", headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]["code"]) == (503, "send_ids_unavailable")
    assert listed(client)[0]["state"] == "pending" and bb.calls == []
    ids_down[0] = False
    assert client.post(f"/scheduled/{item['id']}/send_now", headers=AUTH).json()["item"]["state"] == "sent"
    assert len(bb.calls) == 1


# ---------------------------------------------------------------------------
# the late rules
# ---------------------------------------------------------------------------

def test_an_item_due_while_the_relay_was_down_is_sent_up_to_six_hours_late(r, client, clock, sched, bb, osa):
    bb.answers = [_bb_ok()]
    post(client, clock, send_at=at(clock, HOUR))
    clock.advance(HOUR + 5 * HOUR)                       # five hours late when the relay comes back
    tick(r, make_sched(r, clock))
    assert listed(client)[0]["state"] == "sent" and len(bb.calls) == 1


def test_an_item_more_than_six_hours_late_fails_with_the_fixed_reason_and_nothing_is_sent(r, client, clock, sched, bb,
                                                                                       osa, events, pushes):
    item = post(client, clock, send_at=at(clock, HOUR)).json()["item"]
    clock.advance(HOUR + 7 * HOUR)
    tick(r, make_sched(r, clock))
    final = listed(client)[0]
    assert (final["state"], final["error"], final["sent_via"]) == ("failed", "the relay was not running at the time", None)
    assert final["error"] == NOT_RUNNING
    assert bb.calls == [] and osa.calls == []
    assert [e["state"] for e in events if e["type"] == "scheduled"] == ["created", "failed"]      # never "sending"
    assert [p["state"] for p in pushes] == ["failed"] and pushes[0]["id"] == item["id"]


def test_send_now_ignores_the_late_rule_because_the_owner_asked(client, clock, sched, bb, osa):
    bb.answers = [_bb_ok()]
    item = post(client, clock, send_at=at(clock, HOUR)).json()["item"]
    clock.advance(HOUR + 7 * HOUR)
    final = client.post(f"/scheduled/{item['id']}/send_now", headers=AUTH).json()["item"]
    assert final["state"] == "sent" and len(bb.calls) == 1


def test_the_late_rule_also_holds_for_an_item_left_as_sending_whose_id_was_never_begun(r, client, clock, sched, bb, osa,
                                                                                    relay_module):
    """Stopped between the state file's "sending" and the send ids' "started":
    the id file knows nothing, so the send would be made, and the late rule
    decides as it does for a pending item."""
    bb.answers = [_bb_ok()]
    too_late = post(client, clock, send_at=at(clock, HOUR), client_id=ID).json()["item"]
    in_time = post(client, clock, send_at=at(clock, 7 * HOUR), client_id=OTHER_ID).json()["item"]
    for item in (too_late, in_time):
        r._change_state(lambda st, i=item["id"]: st["scheduled"][i].update(state="sending"))
    clock.advance(8 * HOUR)                              # seven hours late, and one
    tick(r, make_sched(r, clock))
    by_id = {it["client_id"]: it for it in listed(client)}
    assert (by_id[ID]["state"], by_id[ID]["error"]) == ("failed", NOT_RUNNING)
    assert (by_id[OTHER_ID]["state"], by_id[OTHER_ID]["sent_via"]) == ("sent", "bb")
    assert len(bb.calls) == 1


# ---------------------------------------------------------------------------
# retention, the push, the state file
# ---------------------------------------------------------------------------

def test_a_finished_item_stays_listed_for_a_day_and_is_then_dropped(r, client, clock, sched, bb, osa, relay_module, capsys):
    bb.answers = [_bb_ok()]
    item = post(client, clock).json()["item"]
    clock.advance(120)
    tick(r, sched)
    clock.advance(24 * HOUR - 1)
    tick(r, sched)
    assert [it["id"] for it in listed(client)] == [item["id"]]
    clock.advance(1)
    assert listed(client) == []                                              # not shown even before the loop looks
    assert item["id"] in stored(relay_module)
    tick(r, sched)
    assert stored(relay_module) == {}
    assert "[schedule] dropped 1 finished item(s) older than a day" in schedule_lines(capsys)


def test_a_finished_item_is_one_push_with_kind_scheduled(r, client, clock, sched, bb, osa, pushes):
    bb.answers = [_bb_ok()]
    long_text = "x" * 61 + " more synthetic words"
    item = post(client, clock, text=long_text).json()["item"]
    clock.advance(120)
    tick(r, sched)
    assert pushes == [{"kind": "scheduled", "chat_guid": CHAT, "id": item["id"], "state": "sent",
                       "chat_name": "Synthetic Person", "text_preview": "x" * 60}]
    tick(r, sched)
    assert len(pushes) == 1                                                  # once, not at every look


def test_a_failed_item_is_one_push_too_and_a_received_message_still_pushes_as_before(r, client, clock, sched, bb, osa,
                                                                                    pushes):
    bb.answers = [FakeResponse(500, text="synthetic BB failure")]
    osa.results = [False]
    item = post(client, clock).json()["item"]
    clock.advance(120)
    tick(r, sched)
    assert [(p["kind"], p["state"], p["id"]) for p in pushes] == [("scheduled", "failed", item["id"])]
    r.send_push({"chat_guid": CHAT, "chat_name": "", "sender": "Synthetic Person", "text": "synthetic received",
                 "rowid": 7, "guid": "synthetic-row-guid"})
    assert pushes[-1]["text"] == "synthetic received" and "kind" not in pushes[-1]


def test_items_live_in_the_state_file_and_a_fresh_scheduler_lists_them(r, client, clock, sched, relay_module):
    one = post(client, clock).json()["item"]
    two = post(client, clock, send_at=at(clock, 500), client_id=OTHER_ID, effect=SLAM).json()["item"]
    kept = stored(relay_module)
    assert set(kept) == {one["id"], two["id"]} and kept[two["id"]]["text"] == TEXT
    fresh = make_sched(r, clock)
    assert asyncio.run(fresh.listing(None)) == [one, two] == listed(client)


# ---------------------------------------------------------------------------
# the feature flag
# ---------------------------------------------------------------------------

def test_health_names_schedule_and_off_the_routes_are_gone(r, client, clock, sched, monkeypatch, compat_db):
    assert client.get("/health", headers=AUTH).json()["features"]["schedule"] is True
    item = post(client, clock).json()["item"]
    monkeypatch.setitem(r.FEATURES, "schedule", False)
    assert client.get("/health", headers=AUTH).json()["features"]["schedule"] is False
    for resp in (post(client, clock, client_id=OTHER_ID), client.get("/scheduled", headers=AUTH),
                 client.patch(f"/scheduled/{item['id']}", json={"text": "x"}, headers=AUTH),
                 client.delete(f"/scheduled/{item['id']}", headers=AUTH),
                 client.post(f"/scheduled/{item['id']}/send_now", headers=AUTH)):
        assert resp.status_code == 404
    # ... and the loop is not started
    spawned = []
    monkeypatch.setattr(r, "_tighten_files", lambda: None)
    monkeypatch.setattr(r, "init_fcm", lambda: None)
    monkeypatch.setattr(r, "_spawn", lambda name, start: spawned.append(name))
    asyncio.run(r._startup())
    assert "schedule" not in spawned and "poll" in spawned
    monkeypatch.setitem(r.FEATURES, "schedule", True)
    spawned.clear()
    asyncio.run(r._startup())
    assert spawned[-1] == "schedule"


def test_the_feature_is_derived_from_imsg_schedule():
    from engines.features import derive_features
    assert derive_features({})["schedule"] is True
    for off in ("0", "false", "NO", " off "):
        assert derive_features({"IMSG_SCHEDULE": off})["schedule"] is False, off
    assert derive_features({"IMSG_SCHEDULE": "1"})["schedule"] is True
