"""Behaviour the adapter adds on top of the byte-identical Phase 1 surface.

* ``fetch_new`` raises ``CursorAhead`` when the cursor is past ``MAX(ROWID)``
  (a rebuilt database); the relay's poll loop re-initialises on it.  The
  relay's original SQL silently returned ``[]`` forever.
* The hooks handed to ``configure()`` are looked up at call time (late-bound),
  so relay.py may configure before every hook body is final.

The hooks here are minimal stand-ins (no contacts, no HEIC advertising); the
relay's real hooks are exercised in ``test_relay_compat.py`` / ``test_relay_glue.py``.
"""

from __future__ import annotations

import pytest

import chatdb_adapter
from imessage_chatdb import CursorAhead
from tests.fixtures import builders

PHONE = "+15550000001"
GUID = f"iMessage;-;{PHONE}"


def _resolve(handle):
    return handle


def _att_public(guid, mime, name):
    return mime, name, f"/attachment/{guid}"


def _person_key(addr):
    return addr


def _group_title(conn, chat_rowid, display_name):
    return display_name


def _configure(path, **over):
    kw = dict(chatdb_path=str(path), resolve=_resolve, att_public=_att_public,
              person_key=_person_key, group_title=_group_title, self_raw=[])
    kw.update(over)
    return chatdb_adapter.configure(**kw)


@pytest.fixture
def small_db(make_db):
    fx = make_db("macos27", name="extras.db")
    h = builders.add_handle(fx.writer, PHONE)
    c = builders.add_chat(fx.writer, GUID, 45, PHONE, handles=[h])
    rowids = [builders.add_message(fx.writer, c, text=f"synthetic {i}", handle=h) for i in range(3)]
    fx.rowids = rowids
    return fx


def test_fetch_new_raises_cursor_ahead_when_db_was_rebuilt(small_db):
    _configure(small_db.path)
    top = chatdb_adapter.max_rowid()
    assert top == small_db.rowids[-1]
    assert chatdb_adapter.fetch_new(top) == []                 # at the top: nothing new, no error
    with pytest.raises(CursorAhead) as ei:
        chatdb_adapter.fetch_new(top + 7)
    assert (ei.value.cursor_rowid, ei.value.max_rowid) == (top + 7, top)
    # The poll loop's recovery: re-initialise at max_rowid() and carry on.
    cursor = chatdb_adapter.max_rowid()
    assert cursor == top and chatdb_adapter.fetch_new(cursor) == []


def test_hooks_are_late_bound(small_db):
    calls = []

    def resolve_v1(h):
        calls.append(("v1", h))
        return "Name One" if h else h

    def resolve_v2(h):
        calls.append(("v2", h))
        return "Name Two" if h else h

    _configure(small_db.path, resolve=resolve_v1)
    assert chatdb_adapter.fetch_new(0)[0]["sender"] == "Name One"
    _configure(small_db.path, resolve=resolve_v2)             # configure again: new hook wins
    assert chatdb_adapter.fetch_new(0)[0]["sender"] == "Name Two"
    assert {c[0] for c in calls} == {"v1", "v2"}


def test_db_connections_are_read_only(small_db):
    for ro in (False, True):
        _configure(small_db.path, readonly_uri=ro)
        conn = chatdb_adapter.db()
        try:
            with pytest.raises(Exception):
                conn.execute("INSERT INTO handle (id) VALUES ('+15550000002')")
        finally:
            conn.close()
