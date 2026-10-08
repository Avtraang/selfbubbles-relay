"""The relay-side glue in ``relay.py``, exercised on the shipped module against the goldens.

``test_relay_compat.py`` proves the database layer; this module proves the
pieces that live in ``relay.py`` itself (DESIGN.md section 7.4 gate 1 says
the test "imports relay with stub env"), each compared with the recorded JSON
in ``tests/golden/`` (what the frozen pre-extraction copy of relay.py produced
on the same synthetic database; see ``tests/record_golden.py``) by
``json.dumps`` byte equality:

* the ``chatdb_adapter.configure(...)`` call at import binds relay's OWN
  hooks (``resolve`` / ``att_public`` / ``person_key`` / ``group_title`` /
  ``SELF_RAW``) and the ``IMSG_CHATDB`` path;
* ``group_title`` (iterating ``participants()``) on every group shape, including
  the no-ORDER-BY ordering and the 4-name cap;
* ``last_message_previews`` (fed by ``lite_rows``, ``att_public`` applied before
  the mime branch), and a NULL-mime ``.heic`` still previews as a Photo;
* ``find_chat_for_addresses`` through the adapter reaches relay's
  ``group_title`` / ``person_key`` on every case;
* ``poll_loop`` survives ``CursorAhead``: it re-initialises the cursor from the
  exception, saves it, logs exactly once, and keeps delivering -- even when
  ``max_rowid()`` would raise (the DB is busy exactly while Messages rebuilds
  it) and even when saving the cursor fails.

Phase 2 + 3 (DESIGN.md section 7.3, the rows marked "Phase 2" / "Phase 3"):

* ``search_messages``: text-column and blob-only hits, every case variant, the
  attribute-key false positive (``NSString`` is in every typedstream blob), tapbacks
  excluded, ``limit`` and the exact ``limit * 2`` oversample, the ``len(q) < 2`` guard;
* ``link_image``: the bytes (recorded as sha256 + size) for every balloon with an
  embedded image (largest blob, first on a tie) and the three 404 details
  ``"no payload"`` / ``"unparseable payload"`` / ``"no embedded image"`` through
  ``HTTPException.detail``;
* ``thread_media``: attachments newest-first with ``att_public`` applied, links with
  the first-seen dedupe, ``"me"`` / resolved / ``None`` senders, empty and unknown chats;
* ``fetch_threads(200)``: with the recorded relay state seeded -- the first-run
  ``reads_baseline`` initialisation and the set path, pins in order, archived,
  auto-translate, forced unread, a group known to have no icon (and an expired
  entry), SMS / RCS / iMessage / unknown labels, limit and ordering, and a chat
  with no messages that must not appear;
* ``/attachment``: the ``(path, media_type, filename)`` of the ``FileResponse`` for
  non-HEIC files under ``tmp_path`` (never a HEIC: that branch runs ``sips``) and
  the two 404s, plus ``/thumbnail``'s two pre-``qlmanage`` 404s;
* ``contact_recency`` over ``one_to_one_activity()``, before and after a 1:1 chat
  whose ``chat_identifier`` is NULL;
* ``/health`` through the ASGI test client: the full dict (recorded key order)
  only with a valid token (header or ``?token=``), ``{"ok": true}`` otherwise,
  a ``?nonce=`` parameter ignored (the unauthenticated HMAC answer is gone:
  step R5), the middleware exemption, and a non-ASCII token on
  /health, the middleware and the WS gate being rejected (``token_matches``
  compares bytes; ``hmac.compare_digest(str, str)`` raises TypeError on
  non-ASCII, which would have been a 500).

The module under test is the one the conftest imports (``RELAY_MODULE``:
``relay`` by default, ``relay_new`` before a cutover) under a placeholder
environment; its chat.db and state paths are under tmp.  Synthetic database
only; the conftest refuses anything under ``~/Library``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.testclient import TestClient

import chatdb_adapter
from tests import compat_fixture as cf
from tests.compat_fixture import (
    ALICE_PHONE,
    BOB_PHONE,
    C1_GUID,
    C1E_GUID,
    C1S_GUID,
    CARL_PHONE,
    CNONE_GUID,
    CRCS_GUID,
    CSMS_GUID,
    EMPTY_GUID,
    FIND_CASES,
    FRANK_PHONE,
    G1_GUID,
    G2_GUID,
    G3_GUID,
    G5_GUID,
    NO_SUCH_GUID,
)
from tests.conftest import RELAY_STUB_ENV, RELAY_STUB_SELF, has_r5
from tests.fixtures import builders
from tests.test_relay_compat import (  # noqa: F401  (relay_state: autouse snapshot/restore)
    assert_golden,
    compat_db,
    relay_state,
)


# ---------------------------------------------------------------------------
# configure() wiring at import
# ---------------------------------------------------------------------------

def test_import_configured_adapter_with_relay_hooks(relay_module):
    r = relay_module.module
    hooks = relay_module.import_hooks
    assert hooks["resolve"] is r.resolve
    assert hooks["att_public"] is r.att_public
    assert hooks["person_key"] is r.person_key
    assert hooks["group_title"] is r.group_title          # defined before configure(): bound directly
    assert hooks["self_raw"] is r.SELF_RAW                # the live list, not a copy
    assert r.SELF_RAW == [RELAY_STUB_SELF]                # from the stub IMSG_SELF
    # Paths: the stub env's tmp files, never anything under ~/Library.
    assert hooks["chatdb_path"] == relay_module.chatdb_path.resolve()
    assert Path(r.CHATDB).resolve() == relay_module.chatdb_path.resolve()
    assert r.STATE_PATH.resolve() == relay_module.state_path.resolve()
    lib = (Path.home() / "Library").resolve()
    assert not hooks["chatdb_path"].is_relative_to(lib)
    assert not r.STATE_PATH.resolve().is_relative_to(lib)
    # The names every untouched call site imports are the adapter's.
    for name in ("db", "fetch_new", "fetch_edited", "max_rowid", "max_date_edited",
                 "fetch_thread_messages", "last_rowid_for", "find_chat_for_addresses",
                 "_norm_service", "chat_services", "lite_rows", "participants",
                 "apple_date_to_unix", "parse_attributed_body", "CursorAhead"):
        assert getattr(r, name) is getattr(chatdb_adapter, name), name
    assert r.LINK_BALLOON == chatdb_adapter.LINK_BALLOON
    # The transitional shims (MESSAGE_SELECT / row_to_msg) are gone from the
    # adapter since Phase 2 ported thread_media; the relay must not have
    # re-created them locally.
    for shim in ("MESSAGE_SELECT", "row_to_msg"):
        assert not hasattr(chatdb_adapter, shim), shim
        assert not hasattr(r, shim), f"{r.__name__} still defines {shim}"


# ---------------------------------------------------------------------------
# group_title / last_message_previews / find_chat through relay
# ---------------------------------------------------------------------------

def test_group_title_matches_golden(compat_db, relay_module):
    r = relay_module.module
    g = cf.load_golden("group_title")
    c = compat_db.chats
    conn = r.db()
    try:
        for key, display in cf.GROUP_TITLE_CASES:
            assert_golden(g[cf.group_title_key(key, display)],
                          r.group_title(conn, cf.chat_rowid(compat_db, key), display),
                          f"group_title({key}, {display!r})")
        # The shapes the fixture pins, on the shipped function.  No ORDER BY: rows
        # come back in chat_handle_join index order (handle ROWID), so Alice's
        # email handle sorts second and Dana is cut by the 4-name cap.
        assert r.group_title(conn, c["g1"], None) == "Alice, Bob"
        assert r.group_title(conn, c["g2"], "Synthetic Crew") == "Synthetic Crew"
        assert r.group_title(conn, c["g3"], None) == f"Alice, Alice, Bob, {CARL_PHONE}…"
        assert r.group_title(conn, c["g4"], None) == "Alice, Alice"
        assert r.group_title(conn, cf.MISSING_CHAT_ROWID, None) is None
    finally:
        conn.close()


def test_last_message_previews_matches_golden(compat_db, relay_module):
    r = relay_module.module
    m = compat_db.messages
    conn = r.db()
    try:
        n = r.last_message_previews(conn, compat_db.all_rowids)
        assert_golden(cf.load_golden("last_message_previews")["all"], n,
                      "relay.last_message_previews(all)")
        # att_public runs BEFORE the mime branch: a NULL-mime .heic is a Photo; the
        # guid whose transcode failed keeps its image/heic mime, so it is a Photo
        # too (only its advertised name / URL differ, which previews never show).
        assert n[m["att_only_heic"]]["body"] == "\U0001F4F7 Photo"
        assert n[m["failed_heic"]]["body"] == "\U0001F4F7 Photo"
        assert r.last_message_previews(conn, []) == {}
        assert r.last_message_previews(conn, [cf.MISSING_ROWID]) == {}
    finally:
        conn.close()


@pytest.mark.parametrize("case", list(FIND_CASES))
def test_find_chat_for_addresses_through_relay_hooks(compat_db, relay_module, case):
    r = relay_module.module
    addrs = FIND_CASES[case]
    conn = r.db()
    try:
        assert_golden(cf.load_golden("find_chat_for_addresses")[case],
                      r.find_chat_for_addresses(conn, addrs),
                      f"relay.find_chat_for_addresses({case})")
    finally:
        conn.close()


def test_fetch_new_through_relay(compat_db, relay_module):
    r = relay_module.module
    assert_golden(cf.load_golden("fetch_new")["from_zero"], r.fetch_new(0), "relay.fetch_new(0)")
    assert r.max_rowid() == cf.load_golden("scalars")["max_rowid"]


# ---------------------------------------------------------------------------
# poll_loop: CursorAhead recovery
# ---------------------------------------------------------------------------

async def _wait_until(pred, what: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


def _arm_poll_loop(r, monkeypatch, *, cursor_ahead_by: int, max_rowid_raises: bool):
    """Seed the state file with a cursor past MAX(ROWID) and stub the loop's
    side channels (contacts refresh, push, WebSocket broadcast).  Returns the
    recorded broadcasts.  With ``max_rowid_raises`` the module-level ``max_rowid``
    raises, proving the recovery never queries the database."""
    top = r.max_rowid()
    r.save_state(last_rowid=top + cursor_ahead_by, last_edit=r.max_date_edited())
    assert r.load_cursor() == top + cursor_ahead_by
    events: list[dict] = []

    async def record_broadcast(payload):
        events.append(payload)

    monkeypatch.setattr(r, "load_contacts", lambda: None)
    monkeypatch.setattr(r, "send_push", lambda msg: None)
    monkeypatch.setattr(r.hub, "broadcast", record_broadcast)
    if max_rowid_raises:
        def busy():
            raise RuntimeError("database is locked (simulated rebuild)")
        monkeypatch.setattr(r, "max_rowid", busy)
    return top, events


def _run_recovery_scenario(r, compat_db, events, top):
    """Start poll_loop, wait for the recovery, then prove it still delivers."""
    async def scenario():
        task = asyncio.create_task(r.poll_loop())
        try:
            await _wait_until(lambda: task.done() or r.load_cursor() == top, "cursor re-init")
            assert not task.done(), f"poll_loop exited: {task.exception()!r}"
            assert r.load_cursor() == top
            # New row after the "rebuild": the loop must pick it up and broadcast it.
            h = builders.handle_rowid(compat_db.writer, ALICE_PHONE, create=False)
            new_rowid = builders.add_message(compat_db.writer, compat_db.chats["c1"],
                                             text="synthetic message after rebuild", handle=h)
            await _wait_until(
                lambda: task.done() or any(e["type"] == "message" and e["data"]["rowid"] == new_rowid
                                           for e in events),
                "delivery after recovery")
            assert not task.done(), f"poll_loop exited: {task.exception()!r}"
            await _wait_until(lambda: task.done() or r.load_cursor() == new_rowid, "cursor saved")
            assert not task.done()
            return new_rowid
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return asyncio.run(scenario())


def test_poll_loop_recovers_from_cursor_ahead_without_querying(compat_db, relay_module,
                                                              monkeypatch, capsys):
    r = relay_module.module
    top, events = _arm_poll_loop(r, monkeypatch, cursor_ahead_by=7, max_rowid_raises=True)
    new_rowid = _run_recovery_scenario(r, compat_db, events, top)
    out = capsys.readouterr().out
    assert out.count("re-initialized cursor at ROWID") == 1          # logged once
    assert f"re-initialized cursor at ROWID {top}" in out
    assert "[poll] error" not in out                                  # nothing escaped to the catch-all
    assert r.load_cursor() == new_rowid
    delivered = [e for e in events if e["type"] == "message"]
    assert [e["data"]["rowid"] for e in delivered] == [new_rowid]     # no replay of old rows


def test_poll_loop_survives_save_failure_during_recovery(compat_db, relay_module,
                                                        monkeypatch, capsys):
    r = relay_module.module
    top, events = _arm_poll_loop(r, monkeypatch, cursor_ahead_by=3, max_rowid_raises=True)
    real_save = r.save_cursor
    failures = {"left": 1}

    def flaky_save(rowid):
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("state file unwritable (simulated)")
        real_save(rowid)

    monkeypatch.setattr(r, "save_cursor", flaky_save)
    # The in-memory cursor is re-initialised even though persisting it failed, so
    # the first new row is delivered and THAT save lands.  load_cursor() still
    # shows the stale value until then, so wait on delivery rather than the file.
    async def scenario():
        task = asyncio.create_task(r.poll_loop())
        try:
            await _wait_until(lambda: task.done() or failures["left"] == 0, "recovery attempt")
            assert not task.done(), f"poll_loop exited: {task.exception()!r}"
            h = builders.handle_rowid(compat_db.writer, ALICE_PHONE, create=False)
            new_rowid = builders.add_message(compat_db.writer, compat_db.chats["c1"],
                                             text="synthetic message after failed save", handle=h)
            await _wait_until(lambda: task.done() or r.load_cursor() == new_rowid, "cursor saved")
            assert not task.done(), f"poll_loop exited: {task.exception()!r}"
            return new_rowid
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    new_rowid = asyncio.run(scenario())
    out = capsys.readouterr().out
    assert out.count("error saving re-initialized cursor") == 1
    assert out.count("re-initialized cursor at ROWID") == 1
    assert [e["data"]["rowid"] for e in events if e["type"] == "message"] == [new_rowid]


# ---------------------------------------------------------------------------
# Phase 2: search_messages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q,limit", cf.SEARCH_CASES, ids=[cf.search_key(q, n) for q, n in cf.SEARCH_CASES])
def test_search_matches_golden(compat_db, relay_module, q, limit):
    r = relay_module.module
    assert_golden(cf.load_golden("search")["cases"][cf.search_key(q, limit)],
                  r.search_messages(q, limit=limit), f"search_messages({q!r}, limit={limit})")


def test_search_pins(compat_db, relay_module):
    """What the equal JSON actually says, on the shipped module."""
    r = relay_module.module
    m = compat_db.messages
    hits = r.search_messages("blob only")["results"]
    assert [h["rowid"] for h in hits] == [m["blob"]]
    assert hits[0]["snippet"] == "You: blob only text"
    assert list(hits[0]) == ["chat_guid", "chat_name", "rowid", "date", "snippet"]
    assert [h["rowid"] for h in r.search_messages("NSString")["results"]] == [m["needle"]]
    assert r.search_messages("kIMMessagePart") == {"results": []}
    assert r.search_messages("streamtyped") == {"results": []}
    tapbacks = {m[k] for k in m if k.startswith("tap")} | {m["sticker"]}
    assert r.search_messages("Loved") == {"results": []}         # incl. the balloon tapback
    for q in ("hello", "HELLO", "Hello From"):
        rows = [h["rowid"] for h in r.search_messages(q)["results"]]
        assert m["text"] in rows and not tapbacks & set(rows), q
    g1 = r.search_messages("g1 carl")["results"]
    assert g1 == [{"chat_guid": G1_GUID, "chat_name": "Alice, Bob", "rowid": m["g1_carl"],
                   "date": r.apple_date_to_unix(builders.BASE_DATE_NS
                                                + (m["g1_carl"] - 1) * builders.DATE_STEP_NS),
                   "snippet": f"{CARL_PHONE}: g1 carl (no join row)"}]   # unresolved handle, whole
    g2 = r.search_messages("g2 alice")["results"]
    assert g2[0]["chat_name"] == "Synthetic Crew" and g2[0]["snippet"] == "Alice: g2 alice"
    sysnote = r.search_messages("system note")["results"]
    assert sysnote[0]["snippet"] == "system note https://sys.example.invalid/n."   # no sender
    assert sysnote[0]["chat_name"] == FRANK_PHONE
    assert len(r.search_messages("example.invalid", limit=3)["results"]) == 3
    assert len(r.search_messages("example.invalid", limit=1)["results"]) == 1
    for q in ("a", " ", "", " x "):
        assert r.search_messages(q) == {"results": []}, repr(q)
    assert r.search_messages("  hello  ")["results"] == r.search_messages("hello")["results"]


def test_search_oversample_is_exactly_twice_the_limit(compat_db, relay_module):
    """SQL returns the newest ``limit * 2`` instr() hits; every typedstream blob
    contains "NSString", so the one real (older) hit is reachable only once
    ``2 * limit`` exceeds the number of newer blob rows.  The golden pins exactly
    where that line is."""
    r = relay_module.module
    g = cf.load_golden("search")["oversample"]
    needle = compat_db.messages["needle"]
    conn = r.db()
    try:
        newer_blobs = cf.newer_blob_rows(conn, compat_db)
    finally:
        conn.close()
    assert newer_blobs == g["newer_blobs"] >= 2
    for limit in range(1, newer_blobs + 2):
        n = r.search_messages("NSString", limit=limit)
        assert_golden(g["NSString"][str(limit)], n, f"search_messages('NSString', limit={limit})")
        expected = [needle] if 2 * limit > newer_blobs else []
        assert [h["rowid"] for h in n["results"]] == expected, limit


# ---------------------------------------------------------------------------
# Phase 2: link_image
# ---------------------------------------------------------------------------

def test_link_image_matches_golden(compat_db, relay_module):
    r = relay_module.module
    m = compat_db.messages
    g = cf.load_golden("link_image")
    for key, mime in cf.LINK_IMAGE_OK.items():
        resp = r.link_image(m[key])
        assert isinstance(resp, Response), key
        assert_golden(g[key], cf.link_image_outcome(r, m[key]), f"link_image({key})")
        assert g[key]["status"] == 200, key
        assert resp.media_type == mime == r.sniff_image(bytes(resp.body)[:12]), key
    assert len(r.link_image(m["link_two_blobs"]).body) == 4000      # largest; first on the tie
    for key, detail in cf.LINK_IMAGE_404.items():
        with pytest.raises(HTTPException) as ei:
            r.link_image(m[key])
        assert (ei.value.status_code, ei.value.detail) == (404, detail), key
        assert_golden(g[key], cf.link_image_outcome(r, m[key]), f"link_image({key})")
    with pytest.raises(HTTPException) as ei:
        r.link_image(cf.MISSING_ROWID)
    assert (ei.value.status_code, ei.value.detail) == (404, "no payload")
    assert_golden(g[cf.LINK_IMAGE_MISSING_KEY], cf.link_image_outcome(r, cf.MISSING_ROWID),
                  "link_image(missing rowid)")
    # every URL balloon in the fixture is covered above
    conn = r.db()
    try:
        balloons = {row[0] for row in conn.execute(
            "SELECT ROWID FROM message WHERE balloon_bundle_id = ?", (r.LINK_BALLOON,))}
    finally:
        conn.close()
    assert balloons <= {m[k] for k in (*cf.LINK_IMAGE_OK, *cf.LINK_IMAGE_404)}


# ---------------------------------------------------------------------------
# Phase 2: thread_media
# ---------------------------------------------------------------------------

def test_thread_media_matches_golden(compat_db, relay_module):
    r = relay_module.module
    g = cf.load_golden("thread_media")
    for guid in cf.thread_media_guids(compat_db):
        assert_golden(g[guid], r.thread_media(guid), f"thread_media({guid})")
    assert r.thread_media(EMPTY_GUID) == {"attachments": [], "links": []} == r.thread_media(NO_SUCH_GUID)

    c1 = r.thread_media(C1_GUID)
    atts = c1["attachments"]
    assert [a["date"] for a in atts] == sorted((a["date"] for a in atts), reverse=True)
    guids = [a["guid"] for a in atts]
    assert "ATT-PLUGIN" not in guids and "ATT-UNFLAGGED" in guids and "ATT-GONE" in guids
    assert list(atts[0]) == ["guid", "mime_type", "name", "url", "date"]
    by = {a["guid"]: a for a in atts}
    assert {k: by["ATT-HEIC-1"][k] for k in ("mime_type", "name", "url")} == \
        {"mime_type": "image/jpeg", "name": "IMG_0001.jpg", "url": "/attachment/ATT-HEIC-1?f=jpg"}
    assert {k: by["ATT-HEIC-FAILED"][k] for k in ("mime_type", "name", "url")} == \
        {"mime_type": "image/heic", "name": "c.heic", "url": "/attachment/ATT-HEIC-FAILED"}
    assert {k: by["ATT-NULL"][k] for k in ("mime_type", "name", "url")} == \
        {"mime_type": None, "name": None, "url": "/attachment/ATT-NULL"}
    # links: newest row first, first-seen dedupe, "me" for outgoing, resolved otherwise
    assert [(x["url"], x["sender"]) for x in c1["links"]] == [
        ("https://a.example.invalid/x", "me"), ("https://b.example.invalid/y", "me"),
        ("https://only.example.invalid/path?x=1", "Alice Anders"),
        ("https://decoy.example.invalid/", "Alice Anders"),
        ("https://two.example.invalid/b", "Alice Anders"),          # blob-only body
        ("https://photo.example.invalid/j", "me"),
        ("https://site.example.invalid/page", "Alice Anders"),
        ("https://blog.example.invalid/post", "me"),
        ("https://news.example.invalid/story", "Alice Anders"),     # blob-only body
    ]
    assert [x["date"] for x in c1["links"]] == sorted((x["date"] for x in c1["links"]), reverse=True)
    none = r.thread_media(CNONE_GUID)
    assert [(x["url"], x["sender"]) for x in none["links"]] == [("https://sys.example.invalid/n", None)]
    g5 = r.thread_media(G5_GUID)
    assert [(x["url"], x["sender"]) for x in g5["links"]] == [("https://g5.example.invalid/x", "me")]
    assert g5["attachments"] == []


# ---------------------------------------------------------------------------
# Phase 3: fetch_threads
# ---------------------------------------------------------------------------

def test_fetch_threads_first_run_initialises_baseline(compat_db, relay_module, capsys):
    r = relay_module.module
    g = cf.load_golden("fetch_threads")
    cf.seed_relay_thread_state(relay_module, cf.thread_state_first_run(time.time()))  # no reads / baseline
    n = r.fetch_threads(200)
    assert_golden(g["first_run"], n, "fetch_threads(200) with reads_baseline=None")
    top = compat_db.all_rowids[-1]
    assert r.load_state()["reads_baseline"] == top
    assert r.load_state()["pins"] == [G1_GUID, C1_GUID]                 # save_state merged, not replaced
    assert capsys.readouterr().out.count("[reads] baseline initialized at ROWID") == 1
    guids = [t["chat_guid"] for t in n]
    assert EMPTY_GUID not in guids and set(guids) == set(compat_db.chat_guids)
    assert [t["unread"] for t in n] == [1 if g_ == C1E_GUID else 0 for g_ in guids]   # forced only
    # the second call reads the saved baseline: same output, no second log line
    assert_golden(g["first_run_second_call"], r.fetch_threads(200), "fetch_threads(200) second call")
    assert cf.dumps(g["first_run_second_call"]) == cf.dumps(g["first_run"])
    assert "[reads]" not in capsys.readouterr().out


def test_fetch_threads_state_pins_archive_icons_labels(compat_db, relay_module, capsys):
    r = relay_module.module
    m = compat_db.messages
    g = cf.load_golden("fetch_threads")
    cf.seed_relay_thread_state(relay_module, cf.thread_state_with_reads(time.time(), m))
    n = r.fetch_threads(200)
    assert_golden(g["with_reads"], n, "fetch_threads(200) with reads set")
    assert "[reads]" not in capsys.readouterr().out
    assert r.load_state()["reads_baseline"] == m["link_scan"]           # untouched
    by = {t["chat_guid"]: t for t in n}
    assert list(n[0]) == ["chat_guid", "chat_name", "is_group", "handles", "icon_url", "last_date",
                          "last_rowid", "pinned", "pin_index", "archived", "auto_translate",
                          "preview", "unread", "service", "via_label", "send_warning"]

    # unread: incoming rows past the chat's own mark, else past the baseline; forced wins
    conn = r.db()
    try:
        def incoming_after(chat_key, mark):
            return conn.execute(
                """SELECT COUNT(*) FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id
                   WHERE cmj.chat_id = ? AND m.ROWID > ? AND m.is_from_me = 0""",
                (compat_db.chats[chat_key], mark)).fetchone()[0]
        assert by[C1_GUID]["unread"] == incoming_after("c1", m["text"]) > 1
        assert by[G1_GUID]["unread"] == incoming_after("g1", m["g1_bob"]) == 2
        assert by[CSMS_GUID]["unread"] == 0                               # mark past the newest row
        assert by[C1E_GUID]["unread"] == 1                                # forced: mark past newest
        for key, guid in (("c1s", C1S_GUID), ("crcs", CRCS_GUID), ("g5", G5_GUID), ("cnone", CNONE_GUID)):
            assert by[guid]["unread"] == incoming_after(key, m["link_scan"]), key
        assert by[G5_GUID]["unread"] == 1 and by[CNONE_GUID]["unread"] == 2
    finally:
        conn.close()

    # pins in list order; archived; auto-translate
    assert (by[G1_GUID]["pinned"], by[G1_GUID]["pin_index"]) == (True, 0)
    assert (by[C1_GUID]["pinned"], by[C1_GUID]["pin_index"]) == (True, 1)
    assert all((t["pinned"], t["pin_index"]) == (False, -1)
               for t in n if t["chat_guid"] not in (G1_GUID, C1_GUID))
    assert [t["chat_guid"] for t in n if t["archived"]] == [CSMS_GUID]
    assert [t["chat_guid"] for t in n if t["auto_translate"]] == [CRCS_GUID]

    # icons: known-missing group omits icon_url, an expired entry asks again, 1:1 never
    assert by[G2_GUID]["icon_url"] is None
    assert by[G3_GUID]["icon_url"] == "/chat_icon/" + quote(G3_GUID, safe="")
    assert by[G1_GUID]["icon_url"] == "/chat_icon/iMessage%3B%2B%3Bchat100"
    assert all(t["icon_url"] is None for t in n if not t["is_group"])

    # labels
    def labels(t):
        return {k: t[k] for k in ("service", "via_label", "send_warning")}
    assert r.TEXT_RELAY_LABEL == RELAY_STUB_ENV["TEXT_RELAY_LABEL"]
    assert labels(by[C1_GUID]) == {"service": "iMessage", "via_label": "iMessage", "send_warning": None}
    assert labels(by[C1S_GUID])["service"] == "iMessage"                  # stale SMS hint overridden
    assert labels(by[CSMS_GUID]) == {"service": "SMS",
                                     "via_label": f"SMS · sent through {r.TEXT_RELAY_LABEL}",
                                     "send_warning": r.TEXT_SEND_WARNING}
    assert labels(by[CRCS_GUID]) == {"service": "RCS",
                                     "via_label": f"RCS · sent through {r.TEXT_RELAY_LABEL}",
                                     "send_warning": r.TEXT_SEND_WARNING}
    assert labels(by[G5_GUID])["service"] == "SMS"
    assert labels(by[CNONE_GUID]) == {"service": None, "via_label": None, "send_warning": None}

    # titles / handles / previews
    assert by[C1_GUID]["chat_name"] == "Alice Anders" and by[C1_GUID]["handles"] == [ALICE_PHONE]
    assert by[CSMS_GUID]["chat_name"] == CARL_PHONE
    assert by[G2_GUID]["chat_name"] == "Synthetic Crew"
    assert by[G3_GUID]["chat_name"] == f"Alice, Alice, Bob, {CARL_PHONE}…"
    assert by[G1_GUID]["handles"] == [ALICE_PHONE, BOB_PHONE] and by[G1_GUID]["is_group"] is True
    assert by[G1_GUID]["preview"] == "Bob: g1 reply" and by[G2_GUID]["preview"] == "You: g2 me"
    assert by[G5_GUID]["preview"] == "You: g5 me https://g5.example.invalid/x"
    assert by[C1_GUID]["preview"] == "\U0001F4F7 Photo"                   # newest C1 row: ATT-GONE png
    assert by[CNONE_GUID]["preview"] == "system note https://sys.example.invalid/n."
    assert by[C1_GUID]["last_rowid"] == m["file_gone"]
    assert by[C1_GUID]["last_date"] == r.apple_date_to_unix(
        builders.BASE_DATE_NS + (m["file_gone"] - 1) * builders.DATE_STEP_NS)

    # ordering and limit
    assert [t["last_rowid"] for t in n] == sorted((t["last_rowid"] for t in n), reverse=True)
    assert_golden(g["with_reads_limit_3"], r.fetch_threads(3), "fetch_threads(3)")
    assert [t["chat_guid"] for t in r.fetch_threads(3)] == [t["chat_guid"] for t in n][:3]
    assert_golden(g["with_reads_limit_0"], r.fetch_threads(0), "fetch_threads(0)")
    assert r.fetch_threads(0) == []


# ---------------------------------------------------------------------------
# Phase 2: /attachment resolution (non-HEIC only) and /thumbnail's 404s
# ---------------------------------------------------------------------------

def test_attachment_resolution_matches_golden(compat_db, relay_module, tmp_path):
    r = relay_module.module
    g = cf.load_golden("attachment")
    files_dir = compat_db.files_dir
    for guid in cf.FILE_ATTACHMENTS:
        resp = r.attachment(guid)
        assert isinstance(resp, FileResponse), guid
        assert Path(resp.path).resolve().is_relative_to(tmp_path.resolve())
        assert not r._is_heic(resp.media_type, resp.filename) and not r._is_heic(None, resp.path)   # never the sips branch
        # The endpoint's FileResponse: raw path (relative to the files directory
        # in the golden), mime with the endpoint's default, the name.
        assert_golden(g[guid], cf.attachment_outcome(r, guid, files_dir), f"/attachment/{guid}")
        assert g[guid]["status"] == 200 and resp.path == str(files_dir / g[guid]["path"]), guid
    assert r.attachment("ATT-NONAME").filename == "noname.bin"            # basename fallback
    assert r.attachment("ATT-NONAME").media_type == "application/octet-stream"
    assert r.attachment("ATT-FILE-MP4").media_type == "video/mp4"
    for guid, detail in cf.ATTACHMENT_404.items():
        with pytest.raises(HTTPException) as ei:
            r.attachment(guid)
        assert (ei.value.status_code, ei.value.detail) == (404, detail), guid
        assert_golden(g[guid], cf.attachment_outcome(r, guid, files_dir), f"/attachment/{guid}")
    # /thumbnail shares the lookup; its two 404s fire before qlmanage could run
    for guid, detail in (("NO-SUCH-ATT", "unknown attachment"), ("ATT-NULL", "file missing on disk")):
        with pytest.raises(HTTPException) as ei:
            r.thumbnail(guid)
        assert (ei.value.status_code, ei.value.detail) == (404, detail), guid


# ---------------------------------------------------------------------------
# Phase 3 (optional row): contact_recency over one_to_one_activity()
# ---------------------------------------------------------------------------

def test_contact_recency_matches_golden(compat_db, relay_module):
    """Same dict (keys, values, insertion order) as the relay's own SQL produced,
    including after a 1:1 chat whose chat_identifier is NULL (the library skips
    the row; the relay's ``CONTACTS.get(norm_key(None))`` could never have named it)."""
    r = relay_module.module
    g = cf.load_golden("contact_recency")
    n = r.contact_recency()
    assert_golden(g["initial"], n, "contact_recency()")
    assert n and set(n) <= set(cf.SYNTHETIC_CONTACTS.values())       # only named contacts
    assert "Alice Anders" in n                                       # C1 / C1S / C1E are hers
    assert all(isinstance(v, int) and v > 0 for v in n.values())
    cf.add_null_identifier_chat(compat_db.writer)
    n2 = r.contact_recency()
    assert_golden(g["after_null_identifier_chat"], n2,
                  "contact_recency() with a NULL-identifier 1:1 chat")
    assert_golden(g["initial"], n2, "contact_recency() unchanged by the NULL-identifier chat")


# ---------------------------------------------------------------------------
# /health: no nonce path any more; the body is trimmed unless the token is valid
# ---------------------------------------------------------------------------

STUB_TOKEN = RELAY_STUB_ENV["IMSG_TOKEN"]          # a placeholder, never the real token


@pytest.fixture
def client(relay_module):
    """ASGI test client; no ``with``, so startup hooks (poll loop, FCM) never run."""
    return TestClient(relay_module.module.app)


def test_health_ignores_a_nonce_parameter(relay_module, client):
    """``/health?nonce=<x>`` used to answer ``{"ok": true, "hmac": HMAC-SHA256(token, x)}``
    to ANY caller: one answer was enough to test token guesses offline. The
    branch is gone (step R5); a nonce is ignored like any other unknown
    parameter, so nothing derived from the token ever leaves without the token."""
    r = relay_module.module
    assert r.IMSG_TOKEN == STUB_TOKEN
    if not has_r5(r):
        pytest.skip(f"{relay_module.name} still answers /health?nonce= (pre-R5)")
    import inspect
    assert "nonce" not in inspect.signature(r.health).parameters
    for nonce in ("synthetic-nonce", "", "x" * 300):
        for extra in ({}, {"headers": {"X-Imsg-Token": "wrong"}}):
            resp = client.get("/health", params={"nonce": nonce}, **extra)
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}, (nonce, extra)
            assert "hmac" not in resp.text
    # with the token the nonce changes nothing either: the usual full shape, no hmac
    plain = client.get("/health", headers={"X-Imsg-Token": STUB_TOKEN}).json()
    for kw in ({"headers": {"X-Imsg-Token": STUB_TOKEN}, "params": {"nonce": "synthetic-nonce"}},
               {"params": {"nonce": "synthetic-nonce", "token": STUB_TOKEN}}):
        body = client.get("/health", **kw).json()
        assert list(body) == list(plain) and "hmac" not in body


def test_health_plain_path_with_token_returns_full_shape(relay_module, client):
    r = relay_module.module
    for kw in ({"headers": {"X-Imsg-Token": STUB_TOKEN}}, {"params": {"token": STUB_TOKEN}}):
        resp = client.get("/health", **kw)
        assert resp.status_code == 200
        body = resp.json()
        # health.json is the newest shape (R2 keys, then R6's "capabilities"); a
        # relay that predates a step (relay.py until its swap) is compared
        # without that step's keys (tests.compat_fixture.R2_HEALTH_KEYS / R6_HEALTH_KEYS).
        assert list(body) == cf.expected_health_golden(r, cf.load_golden("health"))["full_shape"], kw
        assert body["ok"] is True
        assert body["cursor"] == r.load_cursor()
        assert body["contacts"] == len(r.CONTACTS)
        assert body["self"] == r.SELF_RAW == [RELAY_STUB_SELF]
        assert body["bb_reachable"] is False                   # BB_URL is a discard port


def test_health_golden_is_the_r2_shape_and_pre_r2_relays_compare_without_it(relay_module, client, tmp_path):
    """The golden was re-recorded in R2 (plan section 7, objection 4) and again
    in R6 (``capabilities``), and stays the newest shape on disk; the glue test
    and ``record_golden --check`` both compare a relay that predates a step
    against it minus that step's keys, so the default suite is green on the
    live relay.py before a swap AND on the new one after it. ``record_golden``
    (write mode) never downgrades the golden from an older source."""
    from types import SimpleNamespace

    from tests import record_golden as rg

    golden = cf.load_golden("health")
    assert list(cf.R2_HEALTH_KEYS) == ["engines", "features", "protocol"]
    assert list(cf.R6_HEALTH_KEYS) == ["capabilities"]
    assert golden["full_shape"][-4:] == [*cf.R2_HEALTH_KEYS, *cf.R6_HEALTH_KEYS]
    pre_r2, r2 = SimpleNamespace(), SimpleNamespace(_chain=lambda: [])
    r6 = SimpleNamespace(_chain=lambda: [], edit_message=lambda: None)
    assert not cf.has_r2_health(pre_r2) and cf.has_r2_health(r2) and cf.has_r2_health(r6)
    assert not cf.has_r6_health(pre_r2) and not cf.has_r6_health(r2) and cf.has_r6_health(r6)
    assert not cf.has_r6_health(SimpleNamespace(edit_message=lambda: None))     # R6 without R2 does not exist
    assert cf.expected_health_golden(r6, golden) is golden
    assert cf.expected_health_golden(r2, golden)["full_shape"] == \
        ["ok", "cursor", "contacts", "self", "bb_reachable", "engines", "features", "protocol"]
    assert cf.expected_health_golden(pre_r2, golden)["full_shape"] == \
        ["ok", "cursor", "contacts", "self", "bb_reachable"]
    assert cf.load_golden("health") == golden                              # never mutated

    # the module under test agrees with its own expectation
    body = client.get("/health", headers={"X-Imsg-Token": STUB_TOKEN}).json()
    assert list(body) == cf.expected_health_golden(relay_module.module, golden)["full_shape"]
    assert cf.has_r2_health(relay_module.module) == ("protocol" in body)
    assert cf.has_r6_health(relay_module.module) == ("capabilities" in body)

    # record_golden --check applies the same rule, in both directions
    path = cf.GOLDEN_DIR / "health.json"
    r2_text, pre_text = cf.golden_text(golden), cf.golden_text(cf.strip_r2_health(golden))
    same, line = rg.check_line("health", path, pre_text, has_r2_health=False)
    assert same and "pre-R2 source" in line and line.startswith("same")
    assert rg.check_line("health", path, r2_text, has_r2_health=True) == (True, f"same    {path}")
    assert rg.check_line("health", path, pre_text, has_r2_health=True)[0] is False
    assert rg.check_line("health", path, r2_text, has_r2_health=False)[0] is False   # mismatch still fails
    # ... and for the step in between: an R2..R5 relay answers without "capabilities"
    mid_text = cf.golden_text(cf.strip_r6_health(golden))
    assert mid_text not in (r2_text, pre_text)
    same, line = rg.check_line("health", path, mid_text, has_r2_health=True, has_r6_health=False)
    assert same and "pre-R6 source: compared without capabilities" in line
    assert rg.check_line("health", path, mid_text, has_r2_health=True, has_r6_health=True)[0] is False
    assert rg.check_line("health", path, r2_text, has_r2_health=True, has_r6_health=False)[0] is False
    assert rg.check_line("health", path, mid_text, has_r2_health=False, has_r6_health=False)[0] is False
    # other goldens are compared verbatim whatever the source
    scalars = cf.GOLDEN_DIR / "scalars.json"
    assert rg.check_line("scalars", scalars, scalars.read_text(encoding="utf-8"), False)[0] is True
    assert rg.check_line("scalars", scalars, "{}\n", False)[0] is False
    assert rg.check_line("scalars", scalars, "{}\n", True, False)[0] is False

    # write mode: a pre-R2 source keeps the R2 golden, an R2 source writes
    copy = tmp_path / "health.json"
    copy.write_text(r2_text, encoding="utf-8")
    assert rg.write_line("health", copy, pre_text, has_r2_health=False).startswith("kept")
    assert copy.read_text(encoding="utf-8") == r2_text
    assert rg.write_line("health", copy, mid_text, has_r2_health=True, has_r6_health=False).startswith("kept")
    assert copy.read_text(encoding="utf-8") == r2_text                     # a pre-R6 source keeps it too
    assert rg.write_line("health", copy, pre_text, has_r2_health=True).startswith("wrote")
    assert copy.read_text(encoding="utf-8") == pre_text
    assert rg.write_line("health", copy, r2_text, has_r2_health=True).startswith("wrote")
    assert copy.read_text(encoding="utf-8") == r2_text
    assert path.read_text(encoding="utf-8") == r2_text                     # the real one untouched


def test_health_plain_path_without_token_is_ok_only(relay_module, client):
    for kw in ({}, {"headers": {"X-Imsg-Token": "not-the-token"}}, {"params": {"token": ""}},
               {"params": {"token": STUB_TOKEN + "x"}}):
        resp = client.get("/health", **kw)
        assert resp.status_code == 200, kw                     # still open: the middleware exemption
        assert resp.json() == {"ok": True}, kw


def test_middleware_exempts_only_health(relay_module, client):
    assert client.get("/health").status_code == 200
    assert client.get("/contacts").status_code == 401            # anything else needs the token
    assert client.get("/contacts", headers={"X-Imsg-Token": "wrong"}).status_code == 401
    assert client.get("/contacts", headers={"X-Imsg-Token": STUB_TOKEN}).status_code == 200


NON_ASCII_TOKENS = ("é", "café-token", "\U0001F512")   # placeholders, never the real token


def test_token_matches_is_bytes_safe(relay_module):
    tm = relay_module.module.token_matches
    assert tm(STUB_TOKEN) is True
    for bad in (None, "", "wrong", STUB_TOKEN + "x", *NON_ASCII_TOKENS):
        assert tm(bad) is False, bad


def test_health_plain_path_non_ascii_token_is_ok_only(relay_module, client):
    for tok in NON_ASCII_TOKENS:
        resp = client.get("/health", params={"token": tok})
        assert resp.status_code == 200 and resp.json() == {"ok": True}, tok
    # a latin-1 header byte (what a raw client can send) must not 500 either
    resp = client.get("/health", headers={"X-Imsg-Token": "é".encode("latin-1")})
    assert resp.status_code == 200 and resp.json() == {"ok": True}


def test_middleware_rejects_non_ascii_token_with_401(relay_module, client):
    for tok in NON_ASCII_TOKENS:
        assert client.get("/contacts", params={"token": tok}).status_code == 401, tok
    assert client.get("/contacts",
                      headers={"X-Imsg-Token": "é".encode("latin-1")}).status_code == 401


def test_ws_gate_rejects_non_ascii_token(relay_module, client):
    from starlette.websockets import WebSocketDisconnect
    for tok in NON_ASCII_TOKENS:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws?token=" + quote(tok)):
                pass
        assert exc.value.code == 1008, tok
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers={"X-Imsg-Token": "wrong"}):
            pass
    assert exc.value.code == 1008
    # and the real token still gets through the gate
    with client.websocket_connect("/ws", headers={"X-Imsg-Token": STUB_TOKEN}) as ws:
        assert ws is not None
