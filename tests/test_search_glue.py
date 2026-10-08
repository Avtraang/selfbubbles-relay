"""The in-chat ``/search`` glue in the relay module (``relay_new`` until the cutover).

``GET /search`` gained an optional ``chat`` query parameter.  This file proves
the relay side of it on the synthetic compat database:

* ``chat=""`` (and no ``chat`` at all) is the global search, byte-identical to
  the recorded golden for every ``SEARCH_CASES`` entry;
* an iMessage guid returns only that chat's hits -- exactly the global hits
  with that ``chat_guid``, same shape and keys -- and ``limit`` caps them
  per chat (hits in other chats no longer consume the budget);
* an unknown guid, a chat with no messages and a SQL-looking guid (bound as a
  literal, never interpolated) all give ``{"results": []}``;
* a ``bp:`` guid goes to ``beeper.fetch_messages(chat, limit=500)`` (a fake,
  monkeypatched; the database is never opened): case-insensitive filter, newest
  first, capped at ``limit``, the library's 40/120 snippet window with the
  ``who:`` prefix, ``rowid`` 0 and the message ``guid`` in a new ``guid`` key;
* the ``len(q) < 2`` guard runs first on every path (the fake is never called);
* the ASGI route wires ``?chat=`` through, with the stub token.

Synthetic database only (the conftest refuses anything under ``~/Library``);
Beeper is replaced by a fake (``BEEPER_TOKEN`` is empty in the stub env, so the
real client is disabled anyway).  Every test skips cleanly when the selected
relay module (``RELAY_MODULE``) has no ``chat`` parameter -- i.e. ``relay.py``
before the cutover.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

import chatdb_adapter
from tests import compat_fixture as cf
from tests.compat_fixture import (
    C1_GUID,
    EMPTY_GUID,
    G1_GUID,
    G2_GUID,
    G4_GUID,
    NO_SUCH_GUID,
)
from tests.conftest import RELAY_STUB_ENV
from tests.test_relay_compat import (  # noqa: F401  (fixtures)
    assert_golden,
    compat_db,
    relay_state,
)

STUB_TOKEN = RELAY_STUB_ENV["IMSG_TOKEN"]          # a placeholder, never the real token
AUTH = {"X-Imsg-Token": STUB_TOKEN}
RESULT_KEYS = ["chat_guid", "chat_name", "rowid", "date", "snippet"]
BP_GUID = "bp:4242"
BP_OTHER_GUID = "bp:9"
SQL_LOOKING_GUIDS = [
    "' OR 1=1 --",
    "%",
    "iMessage;-;%",
    "iMessage;-;+15550000001' OR '1'='1",
    "x\" OR 1=1; DROP TABLE chat; --",
]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def r(relay_module):
    """The relay module; skips when ``search_messages`` has no ``chat`` parameter."""
    mod = relay_module.module
    params = inspect.signature(mod.search_messages).parameters
    if "chat" not in params:
        pytest.skip(f"{relay_module.name}: search_messages has no chat parameter")
    return mod


@pytest.fixture
def client(r):
    """ASGI test client; no ``with``, so startup hooks (poll loop, FCM) never run."""
    return TestClient(r.app)


def fake_message(guid: str, text: str, date: float, *, from_me: bool = False,
                 sender: str = "Gina Mobile") -> dict:
    """A row the shape ``beeper.msg_to_dict`` emits (the keys the search reads
    plus the ones the thread view carries); ``rowid`` is 0 for Beeper rows."""
    return {
        "rowid": 0, "guid": guid, "text": text, "date": date, "date_read": None,
        "date_edited": None, "is_from_me": from_me,
        "sender": "" if from_me else sender, "sender_handle": "+15550001234",
        "chat_guid": BP_GUID, "chat_name": "", "is_group": False,
        "has_attachments": False, "assoc_guid": None, "assoc_type": 0,
        "attachments": [], "link": None, "reply_to_guid": None, "reply_to": None,
        "network": "gmessages",
    }


LONG_BP_TEXT = ("x" * 60) + "needle in the middle of a long synthetic message " + ("y" * 100)

#: What the fake Beeper API holds for ``BP_GUID``, oldest first (the order
#: ``beeper.fetch_messages`` returns, mirroring chat.db).
BP_MESSAGES = [
    fake_message("bp-m1", "hello from gina", 1_704_067_200.0),
    fake_message("bp-m2", "nothing to see here", 1_704_067_201.0),
    fake_message("bp-m3", "HELLO again", 1_704_067_202.0, from_me=True),
    fake_message("bp-m4", "Well hello there", 1_704_067_203.0),
    fake_message("bp-m5", LONG_BP_TEXT, 1_704_067_204.0),
    fake_message("bp-m6", "", 1_704_067_205.0),                    # empty text
    {"guid": "bp-m7", "date": 1_704_067_206.0, "text": None},      # text None, sparse dict
]


@pytest.fixture
def fake_beeper(r, monkeypatch):
    """``beeper.fetch_messages`` replaced by an async fake that records its calls
    and serves ``BP_MESSAGES`` for ``BP_GUID`` only, with a cached title for it."""
    calls: list[tuple[str, int]] = []

    async def fetch_messages(chat_guid: str, limit: int = 50, cursor=None) -> list[dict]:
        calls.append((chat_guid, limit))
        return list(BP_MESSAGES) if chat_guid == BP_GUID else []

    monkeypatch.setattr(r.beeper, "fetch_messages", fetch_messages)
    monkeypatch.setitem(r.CHAT_TITLES, BP_GUID, "Gina (Google Messages)")
    return calls


@pytest.fixture
def no_db(r, monkeypatch):
    """``db()`` made to fail, so a ``bp:`` search that touched the database is caught."""
    monkeypatch.setattr(r, "db", lambda: pytest.fail("bp: search opened the database"))


# ---------------------------------------------------------------------------
# global search: unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q,limit", cf.SEARCH_CASES, ids=[cf.search_key(q, n) for q, n in cf.SEARCH_CASES])
def test_global_search_matches_golden(compat_db, r, q, limit):
    expected = cf.load_golden("search")["cases"][cf.search_key(q, limit)]
    assert_golden(expected, r.search_messages(q, limit=limit, chat=""),
                  f"search_messages({q!r}, limit={limit}, chat='')")
    assert_golden(expected, r.search_messages(q, limit=limit),
                  f"search_messages({q!r}, limit={limit})")


def test_chat_parameter_defaults_to_empty(r):
    assert inspect.signature(r.search_messages).parameters["chat"].default == ""


def test_adapter_passthrough_default_is_global(compat_db, r):
    """``search_rows`` without ``chat_guid`` is what the golden path always called."""
    conn = r.db()
    try:
        before = [h.rowid for h in chatdb_adapter.search_rows(conn, "alice", 30)]
        after = [h.rowid for h in chatdb_adapter.search_rows(conn, "alice", 30, chat_guid=None)]
    finally:
        conn.close()
    assert before == after
    assert before == [h["rowid"] for h in r.search_messages("alice")["results"]]


# ---------------------------------------------------------------------------
# iMessage chat: only that chat's hits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chat", [C1_GUID, G1_GUID, G2_GUID, G4_GUID])
def test_in_chat_search_is_the_chat_subset_of_the_global_hits(compat_db, r, chat):
    for q in ("alice", "hello", "example.invalid", "g1", "synthetic"):
        global_hits = r.search_messages(q, limit=30)["results"]
        assert len(global_hits) < 30, "widen the cap: this case must not be capped"
        got = r.search_messages(q, limit=30, chat=chat)["results"]
        assert got == [h for h in global_hits if h["chat_guid"] == chat], (q, chat)
        for h in got:
            assert h["chat_guid"] == chat
            assert list(h) == RESULT_KEYS


def test_in_chat_search_has_hits_and_names(compat_db, r):
    m = compat_db.messages
    g4 = r.search_messages("alice", chat=G4_GUID)["results"]
    assert [h["rowid"] for h in g4] == [m["g4_email"], m["g4_alice"]]       # newest first
    assert {h["chat_name"] for h in g4} == {"Alice, Alice"}
    assert all(h["snippet"].startswith("Alice: ") for h in g4)
    c1 = r.search_messages("hello", chat=C1_GUID)["results"]
    assert m["text"] in [h["rowid"] for h in c1]
    assert all(h["chat_guid"] == C1_GUID and h["chat_name"] == "Alice Anders" for h in c1)
    # A query that hits elsewhere only.
    assert r.search_messages("g1 carl", chat=C1_GUID) == {"results": []}
    assert r.search_messages("g1 carl", chat=G1_GUID)["results"] == \
        r.search_messages("g1 carl")["results"]


def test_in_chat_limit_applies_per_chat(compat_db, r):
    """The global cap can starve a chat (newer hits elsewhere eat the budget);
    scoped, ``limit`` counts that chat's hits only."""
    all_hits = r.search_messages("example.invalid", limit=30)["results"]
    chats = {h["chat_guid"] for h in all_hits}
    assert len(chats) >= 2
    newest_chat = all_hits[0]["chat_guid"]
    other = next(c for c in chats if c != newest_chat)
    assert all_hits[0]["chat_guid"] != other
    capped = r.search_messages("example.invalid", limit=1, chat=other)["results"]
    assert len(capped) == 1 and capped[0]["chat_guid"] == other
    assert capped == [h for h in all_hits if h["chat_guid"] == other][:1]
    for limit in (1, 2, 3):
        scoped = r.search_messages("alice", limit=limit, chat=G4_GUID)["results"]
        assert len(scoped) == min(limit, 2)
        assert scoped == r.search_messages("alice", chat=G4_GUID)["results"][:limit]


def test_in_chat_search_strips_and_is_case_insensitive(compat_db, r):
    a = r.search_messages("  HELLO  ", chat=C1_GUID)["results"]
    b = r.search_messages("hello", chat=C1_GUID)["results"]
    assert a and a == b


# ---------------------------------------------------------------------------
# unknown / empty / SQL-looking chat guids
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chat", [NO_SUCH_GUID, EMPTY_GUID, "nope", "iMessage;-;", "bp"])
def test_unknown_chat_is_empty(compat_db, r, chat):
    assert r.search_messages("alice", chat=chat) == {"results": []}
    assert r.search_messages("example.invalid", limit=30, chat=chat) == {"results": []}


@pytest.mark.parametrize("chat", SQL_LOOKING_GUIDS)
def test_sql_looking_chat_guid_is_literal(compat_db, r, chat):
    """No chat has such a guid, so a bound literal finds nothing; an interpolated
    one would match everything (``%``) or break the statement."""
    assert r.search_messages("alice", chat=chat) == {"results": []}
    assert r.search_messages("example.invalid", chat=chat) == {"results": []}
    # The database is intact afterwards.
    assert r.search_messages("alice")["results"]


# ---------------------------------------------------------------------------
# bp: chats through beeper.fetch_messages
# ---------------------------------------------------------------------------

def test_bp_search_filters_and_orders(r, fake_beeper, no_db):
    res = r.search_messages("hello", chat=BP_GUID)
    hits = res["results"]
    assert fake_beeper == [(BP_GUID, 500)]
    assert [h["guid"] for h in hits] == ["bp-m4", "bp-m3", "bp-m1"]      # newest first
    assert [h["snippet"] for h in hits] == ["Gina: Well hello there", "You: HELLO again",
                                            "Gina: hello from gina"]
    for h in hits:
        assert list(h) == RESULT_KEYS + ["guid"]
        assert h["rowid"] == 0
        assert h["chat_guid"] == BP_GUID
        assert h["chat_name"] == "Gina (Google Messages)"
    assert [h["date"] for h in hits] == [1_704_067_203.0, 1_704_067_202.0, 1_704_067_200.0]


def test_bp_search_is_case_insensitive_and_strips(r, fake_beeper, no_db):
    for q in ("HELLO", "Hello", "  hElLo  "):
        assert [h["guid"] for h in r.search_messages(q, chat=BP_GUID)["results"]] \
            == ["bp-m4", "bp-m3", "bp-m1"], q


def test_bp_search_caps_at_limit(r, fake_beeper, no_db):
    assert [h["guid"] for h in r.search_messages("hello", limit=1, chat=BP_GUID)["results"]] == ["bp-m4"]
    assert [h["guid"] for h in r.search_messages("hello", limit=2, chat=BP_GUID)["results"]] \
        == ["bp-m4", "bp-m3"]
    # The fake is always asked for 500, whatever the cap.
    assert {limit for _, limit in fake_beeper} == {500}


def test_bp_search_snippet_uses_the_library_window(r, fake_beeper, no_db):
    hits = r.search_messages("needle", chat=BP_GUID)["results"]
    assert [h["guid"] for h in hits] == ["bp-m5"]
    i = LONG_BP_TEXT.lower().find("needle")
    assert hits[0]["snippet"] == "Gina: " + chatdb_adapter.snippet(LONG_BP_TEXT, i)
    assert hits[0]["snippet"].startswith("Gina: …") and hits[0]["snippet"].endswith("…")
    # 40 before, 120 wide: the relay's window verbatim.
    assert hits[0]["snippet"] == "Gina: …" + LONG_BP_TEXT[i - 40:i - 40 + 120] + "…"


def test_bp_search_no_hits_and_unknown_bp_chat(r, fake_beeper, no_db):
    assert r.search_messages("zzz-no-such", chat=BP_GUID) == {"results": []}
    assert r.search_messages("hello", chat=BP_OTHER_GUID) == {"results": []}
    assert fake_beeper == [(BP_GUID, 500), (BP_OTHER_GUID, 500)]


def test_bp_search_without_cached_title(r, fake_beeper, no_db, monkeypatch):
    monkeypatch.delitem(r.CHAT_TITLES, BP_GUID)
    hits = r.search_messages("hello", limit=1, chat=BP_GUID)["results"]
    assert hits and hits[0]["chat_name"] == ""


# ---------------------------------------------------------------------------
# the len(q) < 2 guard, on every path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", ["a", " ", "", " x ", "\t"])
def test_short_query_guard_on_every_path(compat_db, r, q, monkeypatch):
    calls: list = []

    async def fetch_messages(chat_guid, limit=50, cursor=None):
        calls.append(chat_guid)
        return list(BP_MESSAGES)

    monkeypatch.setattr(r.beeper, "fetch_messages", fetch_messages)
    assert r.search_messages(q) == {"results": []}
    assert r.search_messages(q, chat="") == {"results": []}
    assert r.search_messages(q, chat=C1_GUID) == {"results": []}
    assert r.search_messages(q, chat=BP_GUID) == {"results": []}
    assert calls == []


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------

def test_route_wires_chat_through(compat_db, client, r, fake_beeper):
    base = client.get("/search", params={"q": "alice"}, headers=AUTH)
    assert base.status_code == 200
    assert base.json() == r.search_messages("alice")
    scoped = client.get("/search", params={"q": "alice", "chat": G4_GUID}, headers=AUTH)
    assert scoped.status_code == 200
    assert scoped.json() == r.search_messages("alice", chat=G4_GUID)
    assert {h["chat_guid"] for h in scoped.json()["results"]} == {G4_GUID}
    bp = client.get("/search", params={"q": "hello", "chat": BP_GUID, "limit": 2}, headers=AUTH)
    assert bp.status_code == 200
    assert [h["guid"] for h in bp.json()["results"]] == ["bp-m4", "bp-m3"]
    assert (BP_GUID, 500) in fake_beeper
