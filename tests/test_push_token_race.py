"""Push registrations: a change to them reads and writes in one step.

Audit finding R2-F8 (2026-10-08), reproduced here before the fix. A push read
the registered devices once, asked Firebase about each, and then wrote back
"what I read, minus the ones Firebase called gone". A phone that registered in
between (the app registers at every start and now at every reconnect) was
written over: its registration was gone, and it got no notification until it
registered again. Two registrations at the same moment had the same shape: each
read the file, each wrote its own set.

Every change to the registrations now reads the file and writes it inside one
critical section, from what the file holds at that moment.

Firebase is a recording fake; nothing leaves the process.
"""

from __future__ import annotations

import json
import threading
import types

import pytest

from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)

DEAD, LIVE, NEW = "synthetic-registration-gone", "synthetic-registration-kept", "synthetic-registration-new"
REST = {"last_rowid": 7, "pins": ["iMessage;-;+15550000001"]}
MESSAGE = {"chat_guid": "iMessage;-;+15550000002", "chat_name": "", "sender": "Stand In", "text": "synthetic",
           "rowid": 9, "guid": "synthetic-guid", "is_group": False}


@pytest.fixture
def rig(compat_db, relay_module, monkeypatch):
    r = relay_module.module

    class Unregistered(Exception):
        pass

    w = types.SimpleNamespace(r=r, path=relay_module.state_path, asked=[], meanwhile=None, stuck=False)

    def send(m):
        w.asked.append(m.token)
        if m.token == DEAD:
            if w.meanwhile:                                # what happens while Firebase is being asked
                other = threading.Thread(target=w.meanwhile)
                other.start()
                other.join(5)
                w.stuck = other.is_alive()                 # a change that cannot run while a push is out
            raise Unregistered()

    fake_fb = types.SimpleNamespace(
        Message=lambda token=None, data=None, android=None: types.SimpleNamespace(token=token, data=dict(data)),
        AndroidConfig=lambda priority=None: None, UnregisteredError=Unregistered, send=send)
    monkeypatch.setattr(r, "fb_messaging", fake_fb, raising=False)
    monkeypatch.setattr(r, "FCM_READY", True)
    monkeypatch.setattr(r, "CHAT_TITLES", {})
    monkeypatch.setattr(r, "load_contacts", lambda: None)
    w.path.write_text(json.dumps({"push_tokens": [DEAD, LIVE], **REST}))
    return w


def held(w) -> dict:
    return json.loads(w.path.read_text())


def test_a_registration_that_arrives_while_a_dead_one_is_being_removed_is_kept(rig):
    rig.meanwhile = lambda: rig.r.register_push(rig.r.PushReq(token=NEW))
    rig.r.send_push(dict(MESSAGE))
    assert not rig.stuck
    assert sorted(rig.asked) == sorted([DEAD, LIVE])
    assert set(held(rig)["push_tokens"]) == {LIVE, NEW}


def test_the_same_holds_for_a_facetime_ring(rig):
    rig.meanwhile = lambda: rig.r.register_push(rig.r.PushReq(token=NEW))
    rig.r.send_facetime_push("incoming", "synthetic-call", "+15550000002", "Stand In", False)
    assert not rig.stuck
    assert set(held(rig)["push_tokens"]) == {LIVE, NEW}


def test_removing_a_dead_registration_touches_nothing_else_in_the_state(rig):
    rig.r.send_push(dict(MESSAGE))
    assert held(rig) == {"push_tokens": [LIVE], **REST}


def test_a_pin_made_while_a_dead_registration_is_being_removed_is_kept_too(rig):
    rig.meanwhile = lambda: rig.r.save_state(pins=["iMessage;-;+15550000003"])
    rig.r.send_push(dict(MESSAGE))
    assert not rig.stuck
    assert held(rig) == {"push_tokens": [LIVE], "last_rowid": 7, "pins": ["iMessage;-;+15550000003"]}


def test_two_registrations_at_the_same_moment_are_both_kept(rig, monkeypatch):
    """The first to read the file waits until the second has read it too, or, where
    the second cannot read before the first has written, a moment."""
    r, real, second_has_read, calls = rig.r, rig.r._read_state, threading.Event(), []

    def read_state():
        out = real()
        calls.append(threading.current_thread().name)
        if len(calls) == 1:
            second_has_read.wait(0.4)
        else:
            second_has_read.set()
        return out

    monkeypatch.setattr(r, "_read_state", read_state)
    answers = []
    threads = [threading.Thread(target=lambda t=t: answers.append(r.register_push(r.PushReq(token=t))), name=t)
               for t in (NEW, NEW + "-2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads)
    assert set(held(rig)["push_tokens"]) == {DEAD, LIVE, NEW, NEW + "-2"}
    assert sorted(a["count"] for a in answers) == [3, 4]


def test_registering_a_device_that_is_already_registered_writes_nothing(rig):
    before = rig.path.stat()
    answer = rig.r.register_push(rig.r.PushReq(token=LIVE))
    after = rig.path.stat()
    assert answer == {"ok": True, "count": 2}
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_a_new_registration_is_counted_and_written(rig):
    assert rig.r.register_push(rig.r.PushReq(token=NEW)) == {"ok": True, "count": 3}
    assert held(rig) == {"push_tokens": sorted([DEAD, LIVE, NEW]), **REST}


def test_a_push_with_no_dead_registration_writes_nothing(rig):
    rig.path.write_text(json.dumps({"push_tokens": [LIVE], **REST}))
    before = rig.path.stat()
    rig.r.send_push(dict(MESSAGE))
    after = rig.path.stat()
    assert rig.asked == [LIVE]
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
