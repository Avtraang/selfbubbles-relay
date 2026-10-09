"""Reviewer C: attacks on the state file handling. Synthetic data only; a FAILING test is a defect."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import threading
import types
from pathlib import Path

import pytest

from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import r  # noqa: F401  (fixture)

GOOD = {"last_rowid": 41, "push_tokens": ["synthetic-device-registration"], "pins": ["iMessage;-;+15550000001"]}


@pytest.fixture
def state(r, relay_module, relay_state):
    path, bak = relay_module.state_path, relay_module.state_path.with_suffix(".bak")
    path.write_text(json.dumps(GOOD))
    bak.write_text(json.dumps({"last_rowid": 40}))
    yield path, bak
    for p in (path, bak):
        with contextlib.suppress(Exception):
            if p.is_dir():
                p.rmdir()
            else:
                os.chmod(p, 0o600)
    for leftover in list(path.parent.glob("*.damaged-*")) + list(path.parent.glob("*.tmp")):
        leftover.unlink()


def _leftovers(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if ".damaged-" in p.name or p.name.endswith(".tmp"))


def _arm_reads(monkeypatch, target: Path):
    """Reads of ``target`` fail while arm['n'] > 0 (each failure uses one up)."""
    real, arm = Path.read_text, {"n": 0}

    def read_text(self, *a, **k):
        if self == target and arm["n"] > 0:
            arm["n"] -= 1
            raise OSError(errno.EIO, "synthetic: input/output error")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    return arm


def _fail_final_rename(monkeypatch, live: Path):
    real = Path.replace

    def replace(self, target):
        if Path(target) == live:
            raise OSError(errno.EIO, "synthetic: rename failed")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", replace)


def _run_poll(r, until, seconds=4.0):
    async def run():
        task = asyncio.create_task(r.poll_loop())
        for _ in range(int(seconds / 0.01)):
            await asyncio.sleep(0.01)
            if until() or task.done():
                break
        outcome = None
        if task.done() and not task.cancelled():
            outcome = task.exception()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return outcome

    return asyncio.run(run())


def _quiet_poll(r, monkeypatch, max_rowid):
    monkeypatch.setattr(r, "max_rowid", lambda: max_rowid)
    monkeypatch.setattr(r, "max_date_edited", lambda: 5)
    monkeypatch.setattr(r, "load_contacts", lambda: None)
    monkeypatch.setattr(r, "fetch_edited", lambda mark: ([], mark))
    monkeypatch.setattr(r, "enrich_links", lambda msgs: [])
    monkeypatch.setattr(r, "schedule_link_resolves", lambda need: None)


# S1 ---------------------------------------------------------------------------------------------
def test_S1_receive_loop_start_during_a_read_error_does_not_jump_past_the_stored_cursor(r, state, monkeypatch):
    path, bak = state
    _quiet_poll(r, monkeypatch, max_rowid=9000)          # 42..9000 arrived while the relay was down
    polled: list[int] = []
    monkeypatch.setattr(r, "fetch_new", lambda cursor: polled.append(cursor) or [])
    arm = _arm_reads(monkeypatch, path)
    arm["n"] = 2                                         # the read load_cursor makes, and its one retry
    real_sleep = asyncio.sleep

    async def short(d, *a, **k):
        return await real_sleep(min(d, 0.02))

    monkeypatch.setattr(asyncio, "sleep", short)

    async def run():
        task = asyncio.create_task(r._supervised("poll", r.poll_loop))   # as the startup hook runs it
        for _ in range(300):
            await real_sleep(0.01)
            if polled:
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    stored = json.loads(path.read_text())["last_rowid"]
    print(f"\nS1 first poll from cursor={polled[:1]} stored last_rowid={stored}")
    assert polled and polled[0] <= 41, f"receive loop started at {polled[:1]}: messages 42..9000 are skipped"
    assert stored <= 41, f"the intact file's cursor 41 was overwritten with {stored}"


def test_S1b_load_cursor_on_an_unreadable_file_is_not_zero(r, state, monkeypatch):
    path, bak = state
    arm = _arm_reads(monkeypatch, path)
    arm["n"] = 10 ** 6
    try:
        got = r.load_cursor()
    except OSError:
        return
    assert got != 0, "load_cursor() answered 0 (what a first run answers) for an intact file holding 41"


# S2 / S3 ----------------------------------------------------------------------------------------
def test_S2_a_save_whose_final_rename_fails_leaves_no_temporary_file(r, state, monkeypatch):
    path, bak = state
    _fail_final_rename(monkeypatch, path)
    with pytest.raises(OSError):
        r.save_state(last_rowid=77)
    monkeypatch.undo()
    assert json.loads(path.read_text()) == GOOD
    print(f"\nS2 leftovers={_leftovers(path)}")
    assert _leftovers(path) == []


def test_S3_the_good_backup_survives_a_save_that_fails_while_the_live_file_is_damaged(r, state, monkeypatch, capsys):
    path, bak = state
    path.write_text("{ not json")                        # real damage: the backup is the only state left
    assert r.load_state() == {"last_rowid": 40}
    _fail_final_rename(monkeypatch, path)                # the save dies at its last step (or the process does)
    with pytest.raises(OSError):
        r.save_state(pins=[])
    monkeypatch.undo()
    after = r.load_state()
    print(f"\nS3 live={path.read_text()!r} backup={bak.read_text()!r} load_state()={after!r}")
    assert after == {"last_rowid": 40}, "the only good copy (the backup) was overwritten with the damaged file"


# S4 ---------------------------------------------------------------------------------------------
def test_S4_one_short_read_of_the_intact_file_does_not_roll_the_state_back(r, state, monkeypatch, capsys):
    path, bak = state
    real, left = Path.read_text, {"n": 1}

    def read_text(self, *a, **k):
        text = real(self, *a, **k)
        if self == path and left["n"]:
            left["n"] -= 1
            return text[: len(text) // 2]                # synthetic partial read, no error raised
        return text

    monkeypatch.setattr(Path, "read_text", read_text)
    r.save_state(forced_unread=[])
    monkeypatch.undo()
    live = json.loads(path.read_text())
    print(f"\nS4 live after one short read + save: {live} said={'[state]' in capsys.readouterr().out}")
    assert live.get("push_tokens") == GOOD["push_tokens"] and live.get("last_rowid") == 41


# S5 ---------------------------------------------------------------------------------------------
def test_S5_saves_racing_in_threads_lose_nothing_and_readers_never_see_less(r, state):
    path, bak = state
    bad: list = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                st = r.load_state()
            except Exception as e:                       # noqa: BLE001
                bad.append(repr(e))
                continue
            if st.get("push_tokens") != GOOD["push_tokens"]:
                bad.append(st)

    def writer(i):
        for j in range(25):
            r.save_state(**{f"k{i}_{j}": j})

    readers = [threading.Thread(target=reader) for _ in range(2)]
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()
    final = r.load_state()
    missing = [f"k{i}_{j}" for i in range(8) for j in range(25) if f"k{i}_{j}" not in final]
    print(f"\nS5 missing={len(missing)} reader_anomalies={len(bad)} leftovers={_leftovers(path)}")
    assert not missing and not bad and _leftovers(path) == []
    assert final["last_rowid"] == 41 and final["pins"] == GOOD["pins"]


# S6 ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("how", ["chmod000", "directory", "backup_unreadable_live_damaged", "vanished_live"])
def test_S6_real_filesystem_conditions(r, state, how, capsys):
    path, bak = state
    if how == "chmod000":
        if os.geteuid() == 0:
            pytest.skip("root reads anything")
        os.chmod(path, 0)
        with pytest.raises(OSError):
            r.load_state()
        with pytest.raises(OSError):
            r.save_state(last_rowid=99)
        os.chmod(path, 0o600)
        assert json.loads(path.read_text()) == GOOD and _leftovers(path) == []
    elif how == "directory":
        path.unlink()
        path.mkdir()
        with pytest.raises(OSError):
            r.load_state()
        with pytest.raises(OSError):
            r.save_state(last_rowid=99)
        assert path.is_dir() and json.loads(bak.read_text()) == {"last_rowid": 40} and _leftovers(path) == []
    elif how == "backup_unreadable_live_damaged":
        if os.geteuid() == 0:
            pytest.skip("root reads anything")
        path.write_text("{ not json")
        os.chmod(bak, 0)
        with pytest.raises(OSError):
            r.load_state()
        with pytest.raises(OSError):
            r.save_state(last_rowid=99)
        os.chmod(bak, 0o600)
        assert json.loads(bak.read_text()) == {"last_rowid": 40} and _leftovers(path) == []
    else:
        path.unlink()                                    # live file gone, backup present
        got = r.load_state()
        print(f"\nS6 vanished live file -> load_state()={got}")
        assert got == {"last_rowid": 40}
        r.save_state(pins=[])
        assert json.loads(path.read_text()) == {"last_rowid": 40, "pins": []} and _leftovers(path) == []


def test_S6b_first_run_no_files_at_all(r, state):
    path, bak = state
    path.unlink()
    bak.unlink()
    assert r.load_state() == {} and r.load_cursor() == 0
    r.save_state(last_rowid=5)
    assert json.loads(path.read_text()) == {"last_rowid": 5} and not bak.exists() and _leftovers(path) == []


# S7 ---------------------------------------------------------------------------------------------
def test_S7_a_read_error_while_pushing_does_not_push_or_broadcast_a_message_twice(r, state, monkeypatch, capsys):
    path, bak = state
    bak.write_text(json.dumps(GOOD))                     # as live: the backup is one save behind, same registrations
    _quiet_poll(r, monkeypatch, max_rowid=43)
    arm = _arm_reads(monkeypatch, path)
    sent: list[str] = []
    broadcasts: list[str] = []

    class Unregistered(Exception):
        pass

    def fb_send(m):
        sent.append(m.data["guid"])
        if len(sent) == 1:
            arm["n"] = 2                                 # the next read of the state file and its retry fail

    monkeypatch.setattr(r, "fb_messaging", types.SimpleNamespace(
        Message=lambda token=None, data=None, android=None: types.SimpleNamespace(token=token, data=dict(data)),
        AndroidConfig=lambda priority=None: None, UnregisteredError=Unregistered, send=fb_send), raising=False)
    monkeypatch.setattr(r, "FCM_READY", True)
    monkeypatch.setattr(r, "CHAT_TITLES", {})
    monkeypatch.setattr(r, "ARCHIVED", set())

    def msg(rowid):
        return {"rowid": rowid, "guid": f"synthetic-{rowid}", "text": "synthetic", "attachments": [], "assoc_type": 0,
                "is_from_me": False, "chat_guid": "iMessage;-;+15550000001", "chat_name": "",
                "sender": "Synthetic Sender", "is_group": False, "has_attachments": False}

    polls: list[int] = []

    def fetch_new(cursor):
        polls.append(cursor)
        return [msg(n) for n in (42, 43) if n > cursor]

    monkeypatch.setattr(r, "fetch_new", fetch_new)

    async def record(payload):
        if payload["type"] == "message":
            broadcasts.append(payload["data"]["guid"])

    monkeypatch.setattr(r.hub, "broadcast", record)
    _run_poll(r, lambda: len(polls) >= 5)
    out = capsys.readouterr().out
    print(f"\nS7 pushed={sent} broadcast={broadcasts} polls={polls[:5]} poll_error={'[poll] error' in out}")
    assert sent == ["synthetic-42", "synthetic-43"]
    assert broadcasts == ["synthetic-42", "synthetic-43"]
