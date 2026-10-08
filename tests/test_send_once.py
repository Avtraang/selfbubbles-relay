"""One send is one delivery, and receiving keeps going.

Regression tests for the hardening audit of 2026-10-08 (second relay step):
a BlueBubbles that took a message and gave no answer is not followed by a
second delivery through AppleScript; the receive loop is started again when
it ends; a contact list that cannot be loaded does not stop the database from
being read; the state file is never replaced by an empty one in silence; a
push says whether its chat is a group.

In-process and synthetic throughout: the suite's recording BlueBubbles client
and osascript recorder, the synthetic compat database, a state file under
the test's temporary directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import httpx
import pytest

from engines.chain import MAYBE_SENT
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, CHAT, TEXT, FakeResponse, _attachment, bb, client, osa, r  # noqa: F401

NO_ANSWER = [httpx.ReadTimeout("synthetic: no answer in time"),
             httpx.ReadError("synthetic: connection dropped"),
             httpx.RemoteProtocolError("synthetic: closed without a response"),
             httpx.WriteTimeout("synthetic: stalled while sending")]
NEVER_ARRIVED = [httpx.ConnectError("synthetic: refused"), httpx.ConnectTimeout("synthetic: no route")]


@pytest.mark.parametrize("failure", NO_ANSWER, ids=lambda e: type(e).__name__)
def test_text_is_not_sent_again_when_bluebubbles_took_it_and_gave_no_answer(bb, osa, client, capsys, failure):
    bb.answers = [failure]
    osa.results = [True]                                   # AppleScript would work: it must not be asked
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (502, {"detail": MAYBE_SENT})
    assert len(bb.calls) == 1 and osa.calls == []
    out = capsys.readouterr().out
    assert "not trying another engine" in out and TEXT not in out


@pytest.mark.parametrize("failure", NO_ANSWER, ids=lambda e: type(e).__name__)
def test_file_is_not_sent_again_when_bluebubbles_took_it_and_gave_no_answer(bb, osa, client, failure):
    bb.answers = [failure]
    osa.results = [True]
    resp = client.post("/send_attachment", data={"chat_guid": CHAT}, files=_attachment(), headers=AUTH)
    assert (resp.status_code, resp.json()) == (502, {"detail": MAYBE_SENT})
    assert len(bb.calls) == 1 and osa.calls == []


@pytest.mark.parametrize("failure", NEVER_ARRIVED, ids=lambda e: type(e).__name__)
def test_the_fallback_still_runs_when_bluebubbles_was_never_reached(r, bb, osa, client, failure):
    bb.answers = [failure]
    osa.results = [True]
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "applescript"})
    assert osa.calls == [(r._AS_TEXT, (CHAT, TEXT))]


def test_the_fallback_still_runs_when_bluebubbles_says_it_failed(r, bb, osa, client):
    bb.answers = [FakeResponse(500, text="synthetic: Private API helper is not connected")]
    osa.results = [True]
    resp = client.post("/send", json={"chat_guid": CHAT, "text": TEXT}, headers=AUTH)
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "via": "applescript"})


def test_local_services_are_never_reached_through_a_proxy(r):
    from engines.bluebubbles import BlueBubblesEngine
    kw = BlueBubblesEngine.__dict__["_client_kw"](next(e for e in r._chain() if e.name == "bluebubbles"), 5)
    assert kw["trust_env"] is False


# ---------------------------------------------------------------------------
# the receive loop
# ---------------------------------------------------------------------------

def test_a_background_loop_that_ends_or_raises_is_started_again(r, monkeypatch, capsys):
    monkeypatch.setattr(r, "LOOP_RESTART_SECONDS", 0.0)
    runs = []

    async def loop():
        runs.append(len(runs))
        if len(runs) == 1:
            raise OSError("synthetic: the log could not be written")
        if len(runs) == 2:
            return                                         # a loop that simply ends
        await asyncio.Event().wait()                       # third start: stays up

    async def scenario():
        task = asyncio.create_task(r._supervised("poll", loop))
        for _ in range(2000):
            if len(runs) >= 3:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert runs == [0, 1, 2]
    out = capsys.readouterr().out
    assert "[poll] stopped (OSError)" in out and "[poll] ended" in out and "synthetic" not in out


def test_a_contact_list_that_cannot_be_loaded_does_not_stop_receiving(r, compat_db, monkeypatch, capsys):
    monkeypatch.setattr(r, "POLL_SECONDS", 0.001)
    reads, loads = [], []

    def broken_contacts():
        loads.append(time.monotonic())
        raise ValueError("synthetic: a 200 answer that is not the contact JSON")

    def fetch_new(cursor):
        reads.append(cursor)
        return []

    monkeypatch.setattr(r, "load_contacts", broken_contacts)
    monkeypatch.setattr(r, "fetch_new", fetch_new)
    monkeypatch.setattr(r, "fetch_edited", lambda mark: ([], mark))

    async def scenario():
        task = asyncio.create_task(r.poll_loop())
        for _ in range(5000):
            if len(reads) >= 5 or task.done():
                break
            await asyncio.sleep(0.001)
        assert not task.done(), f"the loop ended: {task.exception()!r}"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(reads) >= 5                                 # the database was read on every round
    assert len(loads) == 1                                 # and the failed load waits for its retry time
    assert r.CONTACT_RETRY_SECONDS < r.CONTACT_REFRESH_SECONDS
    out = capsys.readouterr().out
    assert out.count("[contacts] refresh failed: ValueError") == 1 and "synthetic" not in out


# ---------------------------------------------------------------------------
# the state file
# ---------------------------------------------------------------------------

def test_saving_keeps_a_backup_and_never_removes_the_live_file(r, relay_module):
    path, bak = relay_module.state_path, relay_module.state_path.with_suffix(".bak")
    r.save_state(first=1)
    r.save_state(second=2)
    assert json.loads(path.read_text())["second"] == 2 and "second" not in json.loads(bak.read_text())
    assert json.loads(bak.read_text())["first"] == 1
    assert not path.with_suffix(".tmp").exists()


def test_two_unreadable_state_files_are_kept_aside_and_reported(r, relay_module, capsys):
    path, bak = relay_module.state_path, relay_module.state_path.with_suffix(".bak")
    path.write_text("{ not json")
    bak.write_text("")
    assert r.load_state() == {}
    r.save_state(last_rowid=7)
    assert json.loads(path.read_text()) == {"last_rowid": 7}
    kept = sorted(p.name for p in path.parent.glob("*.damaged-*"))
    assert len(kept) == 2 and any(n.startswith(path.name) for n in kept)
    out = capsys.readouterr().out
    assert out.count("[state]") == 1 and "unreadable" in out
    r.save_state(last_rowid=8)                             # a healthy file again: nothing more is said
    assert "[state]" not in capsys.readouterr().out
    for leftover in path.parent.glob("*.damaged-*"):       # the state folder is shared by the session
        leftover.unlink()


def test_a_missing_state_file_is_a_first_run_not_damage(r, relay_module, capsys):
    for p in (relay_module.state_path, relay_module.state_path.with_suffix(".bak")):
        p.unlink(missing_ok=True)
    r.save_state(last_rowid=3)
    assert json.loads(relay_module.state_path.read_text()) == {"last_rowid": 3}
    assert "[state]" not in capsys.readouterr().out
    assert not list(relay_module.state_path.parent.glob("*.damaged-*"))


# ---------------------------------------------------------------------------
# the push says whether its chat is a group
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg, flag", [
    ({"chat_guid": "iMessage;+;chat123", "is_group": True}, "1"),
    ({"chat_guid": "iMessage;-;+15550000001", "is_group": False}, "0"),
    ({"chat_guid": "any;-;+15550000001"}, "0"),
    ({"chat_guid": "bp:!room:example.invalid", "is_group": False}, None),
    ({"chat_guid": "bp:!room:example.invalid", "is_group": True}, None)])
def test_push_group_flag(r, msg, flag):
    assert r.push_group_flag(msg) == flag
