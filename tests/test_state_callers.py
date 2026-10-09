"""Reviewer C: what every caller of load_state does while the state file stays unreadable.
These document behaviour (they print); only the asserts marked DEFECT are findings."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, r  # noqa: F401

GOOD = {"last_rowid": 41, "push_tokens": ["synthetic-device-registration"], "pins": ["iMessage;-;+15550000001"],
        "reads_baseline": 1, "last_edit": 5}


@pytest.fixture
def unreadable(r, relay_module, relay_state, compat_db, monkeypatch):
    path, bak = relay_module.state_path, relay_module.state_path.with_suffix(".bak")
    path.write_text(json.dumps(GOOD))
    bak.write_text(json.dumps(GOOD))
    real = Path.read_text

    def read_text(self, *a, **k):
        if self == path:
            raise OSError(errno.EIO, "synthetic: input/output error")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    yield path, bak
    monkeypatch.undo()
    assert json.loads(path.read_text()) == GOOD, "the intact file was changed"
    assert not [p.name for p in path.parent.iterdir() if ".damaged-" in p.name or p.name.endswith(".tmp")]


def test_requests_while_the_state_file_is_unreadable(r, unreadable):
    client = TestClient(r.app, raise_server_exceptions=False)
    probes = [("GET", "/threads", None), ("GET", "/health", None),
              ("POST", "/register_push", {"token": "synthetic-second-registration"})]
    for route in r.app.routes:
        path, methods = getattr(route, "path", ""), getattr(route, "methods", set()) or set()
        if any(w in path for w in ("pin", "archive", "unread", "translate", "read")) and "{" not in path:
            for m in sorted(methods - {"HEAD", "OPTIONS"}):
                probes.append((m, path, {"chat_guid": "iMessage;-;+15550000001", "rowid": 1, "on": True,
                                         "pinned": True, "archived": True, "enabled": True, "unread": True}))
    seen = []
    for method, path, body in probes:
        resp = client.request(method, path, json=body, headers=AUTH)
        seen.append(f"{method} {path} -> {resp.status_code}"
                    + (f" cursor={resp.json().get('cursor')}" if path == "/health" and resp.status_code == 200 else ""))
    print("\nCALLERS " + " | ".join(seen))
    print(f"CALLERS in-memory PINS after the failed requests: {len(r.PINS)} entries")


def test_startup_hook_while_the_state_file_is_unreadable(r, unreadable, monkeypatch):
    spawned = []
    monkeypatch.setattr(r, "_tighten_files", lambda: None)
    monkeypatch.setattr(r, "init_fcm", lambda: None)
    monkeypatch.setattr(r, "_spawn", lambda name, start: spawned.append(name))
    try:
        asyncio.run(r._startup())
        outcome = "returned"
    except OSError as e:
        outcome = f"raised {type(e).__name__}"
    print(f"\nSTARTUP hook {outcome}; loops spawned={spawned}")
    assert outcome.startswith("raised") and spawned == []


def test_receive_loop_while_the_state_file_stays_unreadable(r, unreadable, monkeypatch, capsys):
    monkeypatch.setattr(r, "max_rowid", lambda: 9000)
    monkeypatch.setattr(r, "load_contacts", lambda: None)
    polled = []
    monkeypatch.setattr(r, "fetch_new", lambda cursor: polled.append(cursor) or [])
    real_sleep = asyncio.sleep

    async def short(d, *a, **k):
        return await real_sleep(min(d, 0.02))

    monkeypatch.setattr(asyncio, "sleep", short)

    async def run():
        task = asyncio.create_task(r._supervised("poll", r.poll_loop))
        await real_sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    out = capsys.readouterr().out
    print(f"\nPOLL restarts={out.count('starting it again')} polled={polled[:3]} "
          f"first_line={[l for l in out.splitlines() if l.startswith('[poll]')][:1]}")
    assert polled == []                                   # it never reads messages with a made-up cursor


def test_send_push_while_the_state_file_is_unreadable(r, unreadable, monkeypatch):
    monkeypatch.setattr(r, "FCM_READY", True)
    try:
        r.send_push({"chat_guid": "iMessage;-;+15550000001", "text": "synthetic", "sender": "Synthetic", "rowid": 1})
        outcome = "returned (no push)"
    except OSError as e:
        outcome = f"raised {type(e).__name__}"
    print(f"\nSEND_PUSH {outcome}")
