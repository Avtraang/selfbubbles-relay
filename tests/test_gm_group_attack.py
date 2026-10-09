"""Reviewer C: attacks on the Google Messages group push. Synthetic data only; a FAILING test is a defect.
Self-contained (the rig is copied from tests/test_gm_group_push.py) so it also runs on the previous commit."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import errno
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)

ROOM = "!cake:beeper.local"
ACCOUNT = "sh-gmessages_synthetic/1"


def old_app_says_group(push: dict) -> bool:
    name, sender = push.get("chat_name", "") or "", push.get("sender", "") or ""
    return bool(name.strip()) and name != sender


def new_app(push: dict) -> tuple[bool, str]:
    name, sender, field = push.get("chat_name", "") or "", push.get("sender", "") or "", push.get("is_group")
    group = True if field == "1" else False if field == "0" else (bool(name.strip()) and name != sender)
    return group, ((name or "Group chat") if group else (name or sender))


@pytest.fixture
def rig(compat_db, relay_module, relay_state, monkeypatch):
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
    monkeypatch.setattr(r, "CHAT_TITLES", {})
    monkeypatch.setattr(r, "load_contacts", lambda: None)

    async def record(payload):
        copy.deepcopy(payload)

    monkeypatch.setattr(r.hub, "broadcast", record)
    return types.SimpleNamespace(r=r, sent=sent, state_path=relay_module.state_path)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _chat(kind, title, others: list[dict], room=ROOM, local="401") -> dict:
    now = datetime.now(timezone.utc)
    c = {"id": room, "localChatID": local, "accountID": ACCOUNT, "title": title,
         "participants": {"items": list(others) + [{"fullName": "Me", "isSelf": True}]},
         "lastActivity": _iso(now - timedelta(hours=1)), "preview": {"text": "older"}}
    if kind is not None:
        c["type"] = kind
    return c


def _named(*names):
    return [{"fullName": n} for n in names]


def _pushes(rig, monkeypatch, listing, senders, client_lists_threads=False, expect=None, mid_hook=None,
            settle=0.3):
    """listing(call_number) -> the items /v1/chats answers with. One new message per sender."""
    r, bp = rig.r, rig.r.beeper
    now = datetime.now(timezone.utc)
    calls = {"n": 0}

    async def fake_get(client, path, **params):
        assert path == "/v1/chats"
        calls["n"] += 1
        return {"items": listing(calls["n"])}

    async def no_portal_types():
        return None

    monkeypatch.setattr(bp, "_enabled", True)
    monkeypatch.setattr(bp, "_get", fake_get)
    monkeypatch.setattr(bp, "refresh_portal_types", no_portal_types)
    for name in ("_chat_last", "_chatid_to_local", "_seen_msg_ids", "_chat_meta"):
        monkeypatch.setattr(bp, name, {}, raising=False)
    tokens = ["synthetic-device-registration"]
    r.save_state(last_rowid=r.max_rowid(), last_edit=r.max_date_edited(), push_tokens=tokens)
    r.save_state(push_tokens=tokens)                 # as live: the backup holds the registrations too
    expect = len(senders) if expect is None else expect

    def event(mid, sender, when):
        return json.dumps({"type": "message.upserted", "chatID": ROOM, "entries": [{
            "id": mid, "chatID": ROOM, "accountID": ACCOUNT, "senderName": sender,
            "senderID": "@x:beeper.local", "text": f"synthetic text from {sender}", "timestamp": _iso(when),
            "isSender": False}]})

    real_sleep = asyncio.sleep

    async def short_sleep(d, *a, **k):               # the watcher's 5 s reconnect pause, shortened
        return await real_sleep(min(d, 0.02))

    monkeypatch.setattr(asyncio, "sleep", short_sleep)

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
            await real_sleep(0.1)                    # let the watcher connect and seed
            if client_lists_threads:
                await r.threads(200)                 # what GET /threads does when the app opens
            for i, sender in enumerate(senders):
                if mid_hook:
                    mid_hook(i)
                await q.put(event(f"m{i}", sender, now + timedelta(seconds=i)))
                await real_sleep(settle)
            deadline = time.monotonic() + 3
            while len(rig.sent) < expect and time.monotonic() < deadline:
                await real_sleep(0.01)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    return rig.sent


def _show(tag, push):
    print(f"\n{tag} chat_guid={push.get('chat_guid')} is_group={push.get('is_group')!r} "
          f"chat_name={push.get('chat_name')!r} sender={push.get('sender')!r} "
          f"new_app={new_app(push)} old_app_group={old_app_says_group(push)}")


# G1: the listing does not show the chat (fails, or the chat is outside what it returns) ---------------------
@pytest.mark.parametrize("why", ["listing_empty", "listing_other_chats_only"])
def test_G1_a_group_the_listing_has_not_shown_is_not_pushed_as_one_to_one(rig, monkeypatch, why):
    others = [_chat("single", f"Synthetic {i}", _named(f"Synthetic {i}"), room=f"!o{i}:beeper.local", local=str(900 + i))
              for i in range(200)]
    listing = (lambda n: []) if why == "listing_empty" else (lambda n: others)
    sent = _pushes(rig, monkeypatch, listing, ["Bob Brown"])
    assert sent, "no push at all"
    _show(f"G1[{why}]", sent[0])
    assert new_app(sent[0])[0] is True, "a group message is shown as a one-to-one chat with its sender"


# G2: unnamed group, after the app has fetched /threads once (the normal state) --------------------------------
def test_G2_an_unnamed_group_is_not_titled_with_the_sender_once_a_client_listed_threads(rig, monkeypatch):
    chat = _chat("group", None, _named("Bob Brown", "Carol Clark"))
    sent = _pushes(rig, monkeypatch, lambda n: [chat], ["Bob Brown"], client_lists_threads=True)
    assert sent
    _show("G2", sent[0])
    assert sent[0]["chat_name"] != sent[0]["sender"], "the group's pushed title is the sender's own name"
    assert old_app_says_group(sent[0])


# G3: unnamed group where only one other member has a name ------------------------------------------------------
def test_G3_an_unnamed_group_with_one_named_member_is_not_titled_with_the_sender(rig, monkeypatch):
    chat = _chat("group", None, [{"fullName": "Bob Brown"}, {"phoneNumber": "+15550000002"}])
    sent = _pushes(rig, monkeypatch, lambda n: [chat], ["Bob Brown"])
    assert sent
    _show("G3", sent[0])
    assert sent[0]["chat_name"] != sent[0]["sender"], "the group's pushed title is the sender's own name"
    assert old_app_says_group(sent[0])


# G4: no type field and a short participant list: must not be flagged NOT a group ------------------------------
def test_G4_a_titled_group_without_a_type_field_is_not_flagged_one_to_one(rig, monkeypatch):
    chat = _chat(None, "Cake Committee", _named("Bob Brown"))
    sent = _pushes(rig, monkeypatch, lambda n: [chat], ["Bob Brown"], client_lists_threads=True)
    assert sent
    _show("G4", sent[0])
    assert new_app(sent[0])[0] is True, "is_group '0' overrides the title rule that used to show this as a group"


# G5: the group is renamed / the meta goes stale ---------------------------------------------------------------
def test_G5_a_renamed_group_is_pushed_with_its_new_title(rig, monkeypatch):
    old = _chat("group", "Cake Committee", _named("Bob Brown", "Carol Clark"))
    new = _chat("group", "Bake Sale", _named("Bob Brown", "Carol Clark"))
    sent = _pushes(rig, monkeypatch, lambda n: [old] if n == 1 else [new], ["Bob Brown"])
    assert sent
    _show("G5", sent[0])
    # Accepted limit: the title is the one Beeper's listing gave last (it is refreshed by a thread list or a
    # reconnect). What matters for the reply hazard holds: it is a group, under a group's title.
    assert sent[0]["is_group"] == "1" and sent[0]["chat_name"] in ("Bake Sale", "Cake Committee")


# G6: titles: long, with commas, blank --------------------------------------------------------------------------
@pytest.mark.parametrize("title", ["x" * 5000, "Brown, Bob", "   "])
def test_G6_group_titles(rig, monkeypatch, title):
    chat = _chat("group", title, _named("Bob Brown", "Carol Clark"))
    sent = _pushes(rig, monkeypatch, lambda n: [chat], ["Bob Brown"])
    assert sent
    push = sent[0]
    size = len(json.dumps(push).encode())
    print(f"\nG6 title_len={len(title)} is_group={push.get('is_group')!r} chat_name_len={len(push['chat_name'])} "
          f"payload_bytes={size} new_app_group={new_app(push)[0]} old_app_group={old_app_says_group(push)} "
          f"shown_title_blank={not new_app(push)[1].strip()}")
    assert push.get("is_group") == "1"
    assert size <= 4096, "push data larger than FCM's 4096-byte data limit: FCM rejects it and the push is lost"
    assert new_app(push)[1].strip(), "the group is shown with a blank title"


# G7: a state file read error while a Google Messages push is being sent ---------------------------------------
def test_G7_a_state_read_error_during_a_gm_push_does_not_lose_the_push_or_drop_the_socket(rig, monkeypatch, capsys):
    chat = _chat("group", "Cake Committee", _named("Bob Brown", "Carol Clark", "Dave Davis"))
    real, arm = Path.read_text, {"n": 0}

    def read_text(self, *a, **k):
        if self == rig.state_path and arm["n"] > 0:
            arm["n"] -= 1
            raise OSError(errno.EIO, "synthetic: input/output error")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)

    def hook(i):
        if i == 1:
            arm["n"] = 2                             # the read push_tokens() makes for message 1, and its retry

    sent = _pushes(rig, monkeypatch, lambda n: [chat], ["Bob Brown", "Carol Clark", "Dave Davis"], mid_hook=hook,
                   expect=3)
    out = capsys.readouterr().out
    print(f"\nG7 pushed_senders={[p['sender'] for p in sent]} socket_dropped={'websocket error' in out}")
    assert [p["sender"] for p in sent] == ["Bob Brown", "Carol Clark", "Dave Davis"]
    assert "websocket error" not in out


# G8: 1:1 chats -------------------------------------------------------------------------------------------------
def test_G8_a_one_to_one_whose_title_is_not_the_senders_name(rig, monkeypatch):
    chat = _chat("single", "Bob (work)", _named("Bob Brown"))
    sent = _pushes(rig, monkeypatch, lambda n: [chat], ["Bob Brown"], client_lists_threads=True)
    assert sent
    _show("G8", sent[0])
    assert sent[0].get("is_group") == "0" and new_app(sent[0])[0] is False
