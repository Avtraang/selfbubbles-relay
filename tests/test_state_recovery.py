"""The state file: a read that fails for a moment is not damage.

Regression check of 2026-10-08: a transient read error on an intact state file
was treated like unparseable content, so the file was set aside and the relay
went on with an empty state (cursor, pins, read marks and push registrations
gone from the live state). Reading must fail loudly instead, and a failed save
must leave nothing behind.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import r  # noqa: F401  (fixture)

GOOD = {"last_rowid": 41, "push_tokens": ["synthetic-device-registration"], "pins": ["iMessage;-;+15550000001"]}


@pytest.fixture
def state(r, relay_module):
    path, bak = relay_module.state_path, relay_module.state_path.with_suffix(".bak")
    path.write_text(json.dumps(GOOD))
    bak.write_text(json.dumps({"last_rowid": 40}))
    yield path, bak
    for leftover in list(path.parent.glob("*.damaged-*")) + list(path.parent.glob("*.tmp")):
        leftover.unlink()


def _unreadable(monkeypatch, target: Path, times: int | None = None):
    """Make reading ``target`` fail with an I/O error, ``times`` times (None: every time)."""
    real, left = Path.read_text, {"n": times}

    def read_text(self, *a, **k):
        if self == target and (left["n"] is None or left["n"] > 0):
            if left["n"] is not None:
                left["n"] -= 1
            raise OSError(errno.EIO, "synthetic: input/output error")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)


def _leftovers(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if ".damaged-" in p.name or p.name.endswith(".tmp"))


def test_a_state_file_that_cannot_be_read_is_not_an_empty_state(r, state, monkeypatch):
    path, bak = state
    _unreadable(monkeypatch, path)
    with pytest.raises(OSError):
        r.load_state()                              # not {} and not the older backup


def test_saving_over_a_state_file_that_cannot_be_read_changes_nothing(r, state, monkeypatch, capsys):
    path, bak = state
    before = (path.read_bytes(), bak.read_bytes())
    _unreadable(monkeypatch, path)
    with pytest.raises(OSError):
        r.save_state(last_rowid=99)
    monkeypatch.undo()
    assert (path.read_bytes(), bak.read_bytes()) == before        # the intact file was neither replaced ...
    assert _leftovers(path) == []                                 # ... nor set aside, and nothing was left behind
    assert "[state]" not in capsys.readouterr().out
    assert r.load_state() == GOOD


def test_a_read_that_fails_once_is_tried_again(r, state, monkeypatch):
    path, bak = state
    _unreadable(monkeypatch, path, times=1)
    assert r.load_state() == GOOD
    r.save_state(last_rowid=42)
    assert json.loads(path.read_text())["last_rowid"] == 42 and _leftovers(path) == []


def test_unparseable_content_is_still_damage_and_the_backup_is_used(r, state, capsys):
    path, bak = state
    path.write_text("{ not json")
    assert r.load_state() == {"last_rowid": 40}                   # the backup, which parses
    bak.write_text("")
    r.save_state(last_rowid=7)                                    # neither parses now: kept aside, said once
    assert json.loads(path.read_text()) == {"last_rowid": 7}
    assert len([n for n in _leftovers(path) if ".damaged-" in n]) == 2
    out = capsys.readouterr().out
    assert out.count("is missing or not a state: reading") <= 1 and out.count("were unreadable: kept as") == 1


@pytest.mark.parametrize("step", ["fsync", "copyfile"])
def test_a_save_that_fails_leaves_the_live_file_and_no_temporary_files(r, state, monkeypatch, step):
    path, bak = state

    def boom(*a, **k):
        raise OSError(errno.ENOSPC, "synthetic: no space left on device")

    monkeypatch.setattr(r.os if step == "fsync" else r.shutil, step, boom)
    if step == "fsync":
        with pytest.raises(OSError):
            r.save_state(last_rowid=77)
        assert json.loads(path.read_text()) == GOOD               # nothing was replaced
    else:
        r.save_state(last_rowid=77)                               # the backup copy is best effort
        assert json.loads(path.read_text())["last_rowid"] == 77
    assert _leftovers(path) == []
