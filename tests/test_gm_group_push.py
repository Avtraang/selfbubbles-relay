"""A Google Messages group is pushed as a group.

Regression check of 2026-10-08 (original finding G5-F1 for this transport): a
push for a Google Messages group carried no group marker and an empty title, so
until some client had fetched the thread list the phone showed it as a one-to-one
chat with the sender, and a reply typed into the notification went to everyone.

The rig is the auditors': the shipped relay with a recording Firebase, one
synthetic registered device, an empty title cache as in a freshly started
process, and Beeper's chat list and WebSocket replaced by fakes. Nothing leaves
the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)

ROOM = "!cake:beeper.local"


def old_app_says_group(push: dict) -> bool:
    """An app from before the is_group field: a chat name that is not the sender's."""
    name, sender = push.get("chat_name", "") or "", push.get("sender", "") or ""
    return bool(name.strip()) and name != sender


def new_app(push: dict) -> tuple[bool, str]:
    """PushService.kt pushIsGroup / pushChatTitle: (is a group, the title shown)."""
    name, sender, field = push.get("chat_name", "") or "", push.get("sender", "") or "", push.get("is_group")
    group = True if field == "1" else False if field == "0" else (bool(name.strip()) and name != sender)
    return group, ((name or "Group chat") if group else (name or sender))


@pytest.fixture
def rig(compat_db, relay_module, monkeypatch):
    r = relay_module.module
    sent: list[dict] = []

    class Unregistered(Exception):
        pass

    fake_fb = types.SimpleNamespace(
        Message=lambda token=None, data=None, android=None: types.SimpleNamespace(
            token=token, data=dict(data), android=android),
        AndroidConfig=lambda priority=None: types.SimpleNamespace(priority=priority),
        UnregisteredError=Unregistered,
        send=lambda m: sent.append(m.data),
    )
    monkeypatch.setattr(r, "fb_messaging", fake_fb, raising=False)
    monkeypatch.setattr(r, "FCM_READY", True)
    monkeypatch.setattr(r, "CHAT_TITLES", {})            # a freshly started relay: nothing listed yet
    monkeypatch.setattr(r, "load_contacts", lambda: None)

    async def record(payload):
        copy.deepcopy(payload)

    monkeypatch.setattr(r.hub, "broadcast", record)
    return types.SimpleNamespace(r=r, sent=sent)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _chat(kind: str, title: str | None, *others: str) -> dict:
    now = datetime.now(timezone.utc)
    return {"id": ROOM, "localChatID": "401", "accountID": "sh-gmessages_synthetic/1", "type": kind,
            "title": title,
            "participants": {"items": [{"fullName": n} for n in others] + [{"fullName": "Me", "isSelf": True}]},
            "lastActivity": _iso(now - timedelta(hours=1)), "preview": {"text": "older"}}


def _pushes(rig, monkeypatch, chat: dict, senders: list[str]) -> list[dict]:
    """Start the Beeper watcher as the relay's startup does, feed it one new message per sender, and
    return what was pushed. No thread list is ever fetched by a client."""
    r, bp = rig.r, rig.r.beeper
    now = datetime.now(timezone.utc)

    async def fake_get(client, path, **params):
        assert path == "/v1/chats"
        return {"items": [chat]}

    async def no_portal_types():
        return None

    monkeypatch.setattr(bp, "_enabled", True)
    monkeypatch.setattr(bp, "_get", fake_get)
    monkeypatch.setattr(bp, "refresh_portal_types", no_portal_types)
    for name in ("_chat_last", "_chatid_to_local", "_seen_msg_ids", "_chat_meta"):
        monkeypatch.setattr(bp, name, {}, raising=False)
    r.save_state(last_rowid=r.max_rowid(), last_edit=r.max_date_edited(),
                 push_tokens=["synthetic-device-registration"])

    def event(mid: str, sender: str, when: datetime) -> str:
        return json.dumps({"type": "message.upserted", "chatID": ROOM, "entries": [{
            "id": mid, "chatID": ROOM, "accountID": "sh-gmessages_synthetic/1", "senderName": sender,
            "senderID": "@x:beeper.local", "text": f"synthetic text from {sender}", "timestamp": _iso(when),
            "isSender": False}]})

    async def scenario():
        q: asyncio.Queue = asyncio.Queue()

        class FakeWS:
            async def send(self, _):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await q.get()

        class Connect:
            async def __aenter__(self):
                return FakeWS()

            async def __aexit__(self, *exc):
                return False

        fake_ws_mod = types.ModuleType("websockets")
        fake_ws_mod.connect = lambda *a, **k: Connect()
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_mod)
        task = asyncio.create_task(bp.watch(r._on_beeper_message))
        try:
            for i, sender in enumerate(senders):
                await q.put(event(f"m{i}", sender, now + timedelta(seconds=i)))
                deadline = time.monotonic() + 10
                while len(rig.sent) < i + 1:
                    assert time.monotonic() < deadline, f"timed out waiting for Beeper push {i + 1}"
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    return rig.sent


def test_a_named_group_is_pushed_as_a_group_before_any_thread_list(rig, monkeypatch):
    first, second = _pushes(rig, monkeypatch, _chat("group", "Cake Committee", "Bob Brown", "Carol Clark"),
                            ["Bob Brown", "Carol Clark"])
    for push, sender in ((first, "Bob Brown"), (second, "Carol Clark")):
        assert push["chat_guid"] == "bp:401" and push["sender"] == sender
        assert push["is_group"] == "1"
        assert push["chat_name"] == "Cake Committee"
        assert new_app(push) == (True, "Cake Committee")
        assert old_app_says_group(push)                  # a phone that has not been updated sees a group too


def test_an_unnamed_group_is_titled_with_its_members_never_with_the_sender(rig, monkeypatch):
    (push,) = _pushes(rig, monkeypatch, _chat("group", None, "Bob Brown", "Carol Clark"), ["Bob Brown"])
    assert push["is_group"] == "1"
    assert push["chat_name"] == "Bob Brown, Carol Clark" and push["chat_name"] != push["sender"]
    assert new_app(push) == (True, "Bob Brown, Carol Clark") and old_app_says_group(push)


def test_a_one_to_one_chat_is_pushed_as_one(rig, monkeypatch):
    (push,) = _pushes(rig, monkeypatch, _chat("single", "Bob Brown", "Bob Brown"), ["Bob Brown"])
    assert push["is_group"] == "0"
    assert new_app(push) == (False, "Bob Brown") and not old_app_says_group(push)
