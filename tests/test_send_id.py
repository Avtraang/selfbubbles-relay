"""One send is one message, however often its request arrives.

The gap this closes ("K7"): the relay could not tell a repeated request from a
second message. The app never repeats a send by itself, but after a send that
ended without an answer the owner had "Send again", and a text that had in
fact gone out went out twice.

A text send may now carry an id the client chose (`client_id`). The relay
writes the id down before any engine is asked, and what became of the send
when it ends: delivered, certainly not sent (the relay answered 4xx or 501:
refused before any engine could have sent), or unknown (anything else, or the
relay stopped in the middle). A request whose id is known is answered from
that: delivered -> "ok" again, marked as a duplicate, nothing sent; unknown ->
409, nothing sent, ever, under this id; certainly not sent -> it may be sent.
The ids are kept in a file, so all of this holds across a restart.

Two routes, tested apart: /send (a text into an existing conversation) and
/create_chat (the first text of a new one). Voice and attachments carry no
id and are not protected.

In-process and synthetic: the real routes and send chain, into the suite's
recording BlueBubbles client and osascript runner.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from tests.test_recipient_safety import _ok, _sends
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import (AUTH, C1S_GUID, CHAT, FIND_CASES, FakeResponse, bb, client, osa, r)  # noqa: F401

TEXT = "synthetic text for the send id tests"
ID = "synthetic-send-0001"
UNKNOWN = "send_outcome_unknown"
NEW_GUID = "iMessage;-;synthetic-new"


class Died(BaseException):
    """The relay's process ends while an engine is at work (not an Exception: nothing may tidy up after it)."""


@pytest.fixture
def ids(r, monkeypatch, tmp_path):
    """A send-id store of its own for the test, in a file of its own."""
    store = r.SendIds(tmp_path / "send_ids.json")
    monkeypatch.setattr(r, "SEND_IDS", store)
    monkeypatch.setattr(r, "_SENDS_IN_FLIGHT", {})
    return store


def restart(r, monkeypatch, ids):
    """What a new process has: the file, and nothing else."""
    fresh = r.SendIds(ids.path)
    monkeypatch.setattr(r, "SEND_IDS", fresh)
    monkeypatch.setattr(r, "_SENDS_IN_FLIGHT", {})
    return fresh


def send(client, cid=ID, text=TEXT, chat=CHAT, **more):
    body = {"chat_guid": chat, "text": text, **more}
    if cid is not None:
        body["client_id"] = cid
    return client.post("/send", json=body, headers=AUTH)


def create(client, cid=ID, addresses=None, text=TEXT):
    body = {"addresses": addresses if addresses is not None else FIND_CASES["no_match_unknown"], "text": text}
    if cid is not None:
        body["client_id"] = cid
    return client.post("/create_chat", json=body, headers=AUTH)


def created():
    return FakeResponse(200, {"status": 200, "data": {"guid": NEW_GUID}})


def code(resp):
    detail = resp.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


# ===========================================================================
# /send: a text into an existing conversation
# ===========================================================================

def test_a_first_send_with_an_id_is_an_ordinary_send(client, bb, osa, compat_db, ids):
    bb.answers = [_ok()]
    resp = send(client)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True and resp.json()["via"] == "bb" and "duplicate" not in resp.json()
    assert _sends(bb) == [("text", CHAT)] and osa.calls == []


def test_the_answer_was_lost_and_the_request_comes_again_nothing_is_sent_twice(client, bb, osa, compat_db, ids):
    bb.answers = [_ok()]
    send(client)                                        # delivered; suppose the app never heard
    again = send(client)
    assert (again.status_code, again.json()) == (200, {"ok": True, "via": "bb", "duplicate": True})
    assert send(client).json()["duplicate"] is True     # and as often as it is asked
    assert len(_sends(bb)) == 1 and osa.calls == []


def test_a_send_that_certainly_failed_may_be_sent_again_under_its_id(r, client, bb, osa, compat_db, ids, monkeypatch):
    monkeypatch.setattr(r, "SEND_APPLESCRIPT_FALLBACK", "0")           # one engine: its refusal is the relay's answer
    bb.answers = [FakeResponse(400, text="synthetic BB rejection"), _ok()]
    assert send(client).status_code == 400                             # refused: nothing went out
    again = send(client)
    assert again.status_code == 200 and "duplicate" not in again.json()
    assert len(_sends(bb)) == 2


@pytest.mark.parametrize("no_answer", [httpx.ReadTimeout("synthetic: no answer in time"),
                                       httpx.RemoteProtocolError("synthetic: the answer was cut off")])
def test_an_unknown_outcome_is_never_followed_by_another_send(client, bb, osa, compat_db, ids, no_answer):
    bb.answers = [no_answer]
    assert send(client).status_code == 502                             # may have been sent: nobody knows
    for _ in range(3):
        again = send(client)
        assert (again.status_code, code(again)) == (409, UNKNOWN)
    assert len(_sends(bb)) == 1 and osa.calls == []


def test_every_engine_reporting_a_failure_counts_as_unknown_too(client, bb, osa, compat_db, ids):
    """A 502 from the relay is what the app already calls "may have been sent"; the id keeps to the same rule."""
    bb.answers = [FakeResponse(500, text="synthetic BB error")]
    osa.results = [False]
    assert send(client).status_code == 502
    again = send(client)
    assert (again.status_code, code(again)) == (409, UNKNOWN)
    assert len(bb.calls) == 1 and len(osa.calls) == 1


def test_the_id_is_on_disk_before_any_engine_is_asked(r, client, bb, osa, compat_db, ids, monkeypatch):
    seen = {}
    real = r.deliver

    async def watching(*a, **k):
        seen["file"] = json.loads(ids.path.read_text())
        return await real(*a, **k)

    monkeypatch.setattr(r, "deliver", watching)
    bb.answers = [_ok()]
    send(client)
    assert seen["file"][ID]["state"] == "started"
    assert json.loads(ids.path.read_text())[ID]["state"] == "delivered"


def test_after_a_restart_a_delivered_send_is_still_known(r, client, bb, osa, compat_db, ids, monkeypatch):
    bb.answers = [_ok()]
    send(client)
    restart(r, monkeypatch, ids)
    again = send(client)
    assert (again.status_code, again.json().get("duplicate")) == (200, True)
    assert len(_sends(bb)) == 1


def test_a_relay_that_stopped_in_the_middle_of_a_send_never_sends_it_again(r, client, bb, osa, compat_db, ids, monkeypatch):
    bb.answers = [Died()]
    with pytest.raises(Died):                           # the route itself: the test client would wrap what ends it
        asyncio.run(r.send(r.SendReq(chat_guid=CHAT, text=TEXT, client_id=ID)))
    # the process is gone; BlueBubbles may have had the message
    assert json.loads(ids.path.read_text())[ID]["state"] == "started"
    restart(r, monkeypatch, ids)
    for _ in range(2):
        again = send(client)
        assert (again.status_code, code(again)) == (409, UNKNOWN)
    assert len(bb.calls) == 1 and osa.calls == []


def test_after_a_restart_an_unknown_outcome_is_still_unknown(r, client, bb, osa, compat_db, ids, monkeypatch):
    bb.answers = [httpx.ReadTimeout("synthetic")]
    send(client)
    restart(r, monkeypatch, ids)
    again = send(client)
    assert (again.status_code, code(again)) == (409, UNKNOWN)
    assert len(bb.calls) == 1


def test_after_a_restart_a_certain_failure_may_still_be_sent(r, client, bb, osa, compat_db, ids, monkeypatch):
    monkeypatch.setattr(r, "SEND_APPLESCRIPT_FALLBACK", "0")
    bb.answers = [FakeResponse(400, text="synthetic BB rejection"), _ok()]
    send(client)
    restart(r, monkeypatch, ids)
    assert send(client).status_code == 200 and len(_sends(bb)) == 2


@pytest.mark.parametrize("other", [{"text": TEXT + " changed"}, {"chat": C1S_GUID}, {"reply_to_guid": "p:0/synthetic"}])
def test_an_id_stands_for_one_message_only(client, bb, osa, compat_db, ids, other):
    bb.answers = [_ok()]
    send(client)
    again = send(client, **other)
    assert (again.status_code, code(again)) == (409, "send_id_reused")
    assert len(_sends(bb)) == 1


def test_without_an_id_nothing_has_changed(client, bb, osa, compat_db, ids):
    """An app from before the id, voice, anything else: every request is a message, as it always was."""
    bb.answers = [_ok(), _ok()]
    first, second = send(client, cid=None), send(client, cid=None)
    assert first.json() == second.json() and "duplicate" not in first.json()
    assert len(_sends(bb)) == 2
    assert not ids.path.exists()


@pytest.mark.parametrize("bad", ["short", "x" * 65, "has space in it", "slash/in/it", "", 12345678, ["a-list-of-one"]])
def test_something_that_is_not_an_id_is_refused_before_anything_is_sent(client, bb, osa, compat_db, ids, bad):
    resp = send(client, cid=bad)
    assert resp.status_code in (400, 422)
    assert bb.calls == [] and osa.calls == [] and not ids.path.exists()


def test_the_file_holds_no_text_and_no_recipient(client, bb, osa, compat_db, ids):
    bb.answers = [_ok()]
    send(client)
    stored = ids.path.read_text()
    assert TEXT not in stored and CHAT not in stored and "5550001234" not in stored
    assert set(json.loads(stored)[ID]) <= {"fp", "state", "at", "via"}
    assert oct(ids.path.stat().st_mode & 0o777) == "0o600"


def test_two_requests_with_one_id_at_the_same_moment_are_one_send(r, compat_db, ids, monkeypatch):
    gate, calls = asyncio.Event(), []

    async def slow_deliver(chain, cap, chat_guid, *a, **k):
        calls.append(chat_guid)
        await gate.wait()
        return r.SendResult(True, "bb", payload={})

    monkeypatch.setattr(r, "deliver", slow_deliver)

    async def scenario():
        req = r.SendReq(chat_guid=CHAT, text=TEXT, client_id=ID)
        one = asyncio.create_task(r.send(req))
        two = asyncio.create_task(r.send(r.SendReq(chat_guid=CHAT, text=TEXT, client_id=ID)))
        for _ in range(400):                            # until one is at the engine; the other waits for it
            if calls:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)                       # and room for the other to get there too, if it could
        in_the_engine = len(calls)
        gate.set()
        return in_the_engine, await one, await two

    in_the_engine, first, second = asyncio.run(scenario())
    assert in_the_engine == 1 and len(calls) == 1
    assert sorted([first.get("duplicate", False), second.get("duplicate", False)]) == [False, True]


def test_the_second_of_two_at_once_does_not_send_when_the_first_ends_unknown(r, compat_db, ids, monkeypatch):
    gate, calls = asyncio.Event(), []

    async def no_answer(chain, cap, chat_guid, *a, **k):
        calls.append(chat_guid)
        await gate.wait()
        raise r.DeliveryError(502, r.MAYBE_SENT)

    monkeypatch.setattr(r, "deliver", no_answer)

    async def scenario():
        tasks = [asyncio.create_task(r.send(r.SendReq(chat_guid=CHAT, text=TEXT, client_id=ID))) for _ in range(3)]
        await asyncio.sleep(0.05)
        gate.set()
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = asyncio.run(scenario())
    assert len(calls) == 1
    assert sorted(type(x).__name__ for x in results) == ["DeliveryError", "HTTPException", "HTTPException"]
    assert all(x.status_code == 409 and x.detail["code"] == UNKNOWN for x in results if isinstance(x, HTTPException))


def test_the_relay_says_that_it_keeps_send_ids(client, compat_db, ids):
    assert "send_id" in client.get("/health", headers=AUTH).json()["capabilities"]


# ---- the store itself ----

def test_an_id_is_kept_for_48_hours_and_then_forgotten(r, tmp_path):
    now = [1_000_000.0]
    store = r.SendIds(tmp_path / "ids.json", clock=lambda: now[0])
    store.begin("id-number-one", "fp1")
    store.finish("id-number-one", "delivered", via="bb")
    now[0] += 47 * 3600
    assert store.look("id-number-one")["state"] == "delivered"
    now[0] += 2 * 3600
    store.begin("id-number-two", "fp2")                 # the next write tidies up
    assert store.look("id-number-one") is None
    assert r.SEND_ID_KEEP_SECONDS == 48 * 3600


def test_no_more_than_2000_ids_are_kept_and_the_oldest_go(r, tmp_path):
    now = [1_000_000.0]
    store = r.SendIds(tmp_path / "ids.json", clock=lambda: now[0])
    store._known = {f"old-id-{i:05d}": {"fp": "f", "state": "delivered", "at": now[0] + i} for i in range(r.SEND_ID_MAX)}
    now[0] += r.SEND_ID_MAX + 1
    store.begin("the-newest-id", "fp")
    assert len(json.loads(store.path.read_text())) == r.SEND_ID_MAX == 2000
    assert store.look("old-id-00000") is None and store.look("old-id-00001") is not None
    assert store.look("the-newest-id")["state"] == "started"


def test_a_file_that_cannot_be_read_stops_the_send_instead_of_risking_a_second_one(r, client, bb, osa, compat_db, ids, monkeypatch):
    bb.answers = [_ok()]
    send(client)
    fresh = restart(r, monkeypatch, ids)

    def unreadable(*a, **k):
        raise PermissionError("synthetic: not readable right now")

    monkeypatch.setattr(type(fresh.path), "read_text", unreadable)
    resp = send(client)
    assert (resp.status_code, code(resp)) == (503, "send_ids_unavailable")
    assert len(_sends(bb)) == 1


def test_a_file_that_is_not_a_list_of_ids_is_put_aside_and_said(r, client, bb, osa, compat_db, ids, monkeypatch, capsys):
    ids.path.write_text("{ this is not json")
    restart(r, monkeypatch, ids)
    bb.answers = [_ok()]
    assert send(client).status_code == 200              # nothing is known any more: an ordinary send
    assert [p.name for p in ids.path.parent.iterdir() if ".damaged-" in p.name]
    assert "[send]" in capsys.readouterr().out


# ===========================================================================
# /create_chat: the first text of a new conversation
# ===========================================================================

def test_a_new_conversation_started_twice_under_one_id_is_started_once(client, bb, osa, compat_db, ids):
    bb.answers = [created()]
    first = create(client)
    assert (first.status_code, first.json()) == (200, {"ok": True, "chat_guid": NEW_GUID})
    again = create(client)
    assert again.status_code == 200 and again.json()["ok"] is True and again.json()["duplicate"] is True
    assert _sends(bb) == [("new", tuple(r_norm(FIND_CASES["no_match_unknown"])))] and osa.calls == []


def r_norm(addrs):
    import relay
    return [relay.normalize_address(a) for a in addrs]


def test_a_first_text_into_a_conversation_that_exists_is_sent_once(client, bb, osa, compat_db, ids):
    bb.answers = [_ok()]
    first = create(client, addresses=FIND_CASES["one_to_one_phone"])
    assert first.json() == {"ok": True, "chat_guid": C1S_GUID}
    again = create(client, addresses=FIND_CASES["one_to_one_phone"])
    assert again.json() == {"ok": True, "chat_guid": C1S_GUID, "duplicate": True}     # and it says where the chat is
    assert _sends(bb) == [("text", C1S_GUID)]


def test_a_new_conversation_whose_outcome_is_unknown_is_never_started_again(client, bb, osa, compat_db, ids):
    bb.answers = [httpx.ReadTimeout("synthetic: no answer in time")]
    assert create(client).status_code == 502
    for _ in range(2):
        again = create(client)
        assert (again.status_code, code(again)) == (409, UNKNOWN)
    assert len(bb.calls) == 1 and osa.calls == []


def test_a_new_conversation_that_was_certainly_refused_may_be_tried_again(client, bb, osa, compat_db, ids):
    bb.answers = [FakeResponse(400, text="synthetic BB rejection"), created()]
    assert create(client).status_code == 400
    again = create(client)
    assert again.status_code == 200 and "duplicate" not in again.json()
    assert len(bb.calls) == 2


def test_a_restart_after_a_new_conversation_was_started_does_not_start_it_again(r, client, bb, osa, compat_db, ids, monkeypatch):
    bb.answers = [created()]
    create(client)
    restart(r, monkeypatch, ids)
    again = create(client)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert len(bb.calls) == 1


def test_a_relay_that_stopped_while_starting_a_conversation_never_starts_it_again(r, client, bb, osa, compat_db, ids, monkeypatch):
    bb.answers = [Died()]
    with pytest.raises(Died):
        asyncio.run(r.create_chat(r.CreateChatReq(addresses=FIND_CASES["no_match_unknown"], text=TEXT, client_id=ID)))
    restart(r, monkeypatch, ids)
    again = create(client)
    assert (again.status_code, code(again)) == (409, UNKNOWN)
    assert len(bb.calls) == 1


def test_the_same_people_in_another_order_are_the_same_conversation(client, bb, osa, compat_db, ids):
    a, b = "+15550107001", "+15550107002"
    bb.answers = [created()]
    create(client, addresses=[a, b])
    again = create(client, addresses=[b, a])
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert len(bb.calls) == 1


@pytest.mark.parametrize("other", [{"addresses": ["+15550107009"]}, {"text": TEXT + " changed"}])
def test_an_id_stands_for_one_new_conversation_only(client, bb, osa, compat_db, ids, other):
    bb.answers = [created()]
    create(client)
    again = create(client, **other)
    assert (again.status_code, code(again)) == (409, "send_id_reused")
    assert len(bb.calls) == 1


def test_a_recipient_that_is_no_address_is_refused_before_the_id_is_written(client, bb, osa, compat_db, ids):
    resp = create(client, addresses=["not an address at all"])
    assert resp.status_code == 400 and not ids.path.exists() and bb.calls == []
    bb.answers = [created()]
    assert create(client).status_code == 200            # the corrected request under the same id is the first send


def test_two_requests_at_once_start_one_conversation(r, compat_db, ids, monkeypatch):
    gate, calls = asyncio.Event(), []

    async def slow_deliver(chain, cap, chat_guid, *a, **k):
        calls.append(cap)
        await gate.wait()
        return r.SendResult(True, "bb", payload=NEW_GUID)

    monkeypatch.setattr(r, "deliver", slow_deliver)

    async def scenario():
        make = lambda: r.CreateChatReq(addresses=FIND_CASES["no_match_unknown"], text=TEXT, client_id=ID)
        tasks = [asyncio.create_task(r.create_chat(make())) for _ in range(2)]
        await asyncio.sleep(0.05)
        gate.set()
        return await asyncio.gather(*tasks)

    replies = asyncio.run(scenario())
    assert len(calls) == 1
    assert sorted(x.get("duplicate", False) for x in replies) == [False, True]


def test_a_send_and_a_new_conversation_cannot_share_an_id(client, bb, osa, compat_db, ids):
    bb.answers = [_ok()]
    send(client)
    again = create(client, addresses=FIND_CASES["one_to_one_phone"])
    assert (again.status_code, code(again)) == (409, "send_id_reused")
    assert len(bb.calls) == 1
