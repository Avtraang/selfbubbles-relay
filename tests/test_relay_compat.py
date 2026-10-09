"""Gate 1 of DESIGN.md section 7.4, kept after the cutover (section 7.5 step 7):
the relay's chat.db JSON must not change.

The synthetic database and every case table live in ``tests/compat_fixture.py``;
``tests/golden/*.json`` holds what the frozen pre-extraction copy of relay.py
(``legacy_chatdb``, deleted once the extraction was finished) produced for those
cases, recorded once with ``tests/record_golden.py``.  Every test here runs the
shipped path on the same database and asserts that
``json.dumps(new, ensure_ascii=False, sort_keys=False)`` equals the dump of the
recorded value -- key order included.

The shipped path is ``chatdb_adapter`` (the imessage-chatdb-backed module that
``relay.py`` imports) for the database layer, configured with the relay's OWN
hooks (``resolve`` / ``att_public`` / ``person_key`` / ``group_title`` /
``SELF_RAW``) exactly as relay.py configures it at import, plus
``relay.last_message_previews`` for the one Phase-1 function whose body stays in
the relay.  ``relay`` is imported once per session by the conftest's
``relay_module`` fixture (``RELAY_MODULE``, default ``relay``) under a stub
environment (placeholder token, ``.env`` never read, chat.db/state paths under
tmp).  The relay-side glue (``group_title``, the ``configure(...)`` wiring,
``poll_loop``, the Phase 2 + 3 endpoints) is covered in ``test_relay_glue.py``.

Only synthetic handles (``+1555...``, ``*@example.invalid``) and synthetic names
appear here; no real database is ever opened (the conftest refuses anything
under ``~/Library``).  Re-record the goldens only for a deliberate, versioned
JSON change: ``venv/bin/python -m tests.record_golden`` (``--check`` compares).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import chatdb_adapter
from tests import compat_fixture as cf
from tests.compat_fixture import (
    ALICE_PHONE,
    C1_GUID,
    CARL_PHONE,
    FIND_CASES,
    LONG_TEXT,
    NO_SUCH_GUID,
)
from tests.conftest import UnsafeDatabasePath, assert_safe_db_path
from tests.fixtures import builders


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _snapshot_thread_state(module) -> dict:
    return {name: type(getattr(module, name))(getattr(module, name))
            for name in cf.THREAD_STATE_GLOBALS}


def _restore_thread_state(module, saved: dict) -> None:
    for name, value in saved.items():
        live = getattr(module, name)
        if isinstance(live, list):
            live[:] = value
        else:
            live.clear(); live.update(value)


@pytest.fixture(autouse=True)
def relay_state(relay_module):
    """Snapshot/restore relay's mutable globals (``CONTACTS`` / ``SELF_RAW`` /
    ``FAILED_HEIC``, which ``last_message_previews`` / ``att_public`` read, and
    PINS / ARCHIVED / AUTO_TRANSLATE / FORCED_UNREAD / NO_ICON, which
    ``fetch_threads`` reads) plus the tmp state file around each test."""
    r = relay_module.module
    saved = (dict(r.CONTACTS), list(r.SELF_RAW), set(r.FAILED_HEIC))
    saved_cards = (dict(r.CONTACT_CANON), dict(r.CONTACT_ADDRS), dict(r.CONTACT_CARD), set(r.CONTACT_SHARED))
    saved_threads = _snapshot_thread_state(r)
    state_file = relay_module.state_path
    state_bak = state_file.with_suffix(".bak")
    saved_files = {p: (p.read_bytes() if p.exists() else None) for p in (state_file, state_bak)}
    yield
    r.set_contacts(saved[0], *saved_cards)
    r.SELF_RAW[:] = saved[1]
    r.FAILED_HEIC.clear(); r.FAILED_HEIC.update(saved[2])
    _restore_thread_state(r, saved_threads)
    for p, data in saved_files.items():
        if data is None:
            p.unlink(missing_ok=True)
        else:
            p.write_bytes(data)


@pytest.fixture
def compat_db(make_db, relay_module, tmp_path):
    """The populated synthetic database, with relay seeded (synthetic contacts,
    self identity, failed-HEIC guid) and the adapter configured with relay's own
    hooks -- the call relay.py makes at import -- so what runs is the shipped wiring."""
    fx = make_db("macos27", name="compat.db")
    info = cf.populate_compat_db(fx, tmp_path / "files")
    cf.seed_relay_state(relay_module.module)
    relay_module.configure(fx.path)
    return info


class Impl(SimpleNamespace):
    """The shipped functions under test, bound to one implementation."""


@pytest.fixture
def impl(compat_db, relay_module) -> Impl:
    """``chatdb_adapter`` for the database layer and, for the one Phase-1 function
    whose body lives in the relay itself, ``relay``'s own ``last_message_previews``
    (fed by ``lite_rows``; ``att_public`` applied before the mime branch)."""
    return Impl(
        db=chatdb_adapter.db,
        fetch_new=chatdb_adapter.fetch_new,
        fetch_edited=chatdb_adapter.fetch_edited,
        max_rowid=chatdb_adapter.max_rowid,
        max_date_edited=chatdb_adapter.max_date_edited,
        fetch_thread_messages=chatdb_adapter.fetch_thread_messages,
        last_message_previews=relay_module.module.last_message_previews,
        find_chat_for_addresses=chatdb_adapter.find_chat_for_addresses,
        chat_services=chatdb_adapter.chat_services,
        last_rowid_for=chatdb_adapter.last_rowid_for,
    )


def assert_golden(expected, actual, label: str) -> None:
    """``expected`` is a loaded golden case; the comparison is on the JSON bytes."""
    a, b = cf.dumps(expected), cf.dumps(actual)
    if a != b:
        # Point at the first differing position; synthetic data only, so the
        # excerpt is safe to show.
        i = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        pytest.fail(f"{label}: JSON differs from the golden at offset {i}\n"
                    f"  golden: ...{a[max(0, i - 80):i + 120]}...\n"
                    f"  new:    ...{b[max(0, i - 80):i + 120]}...")


# ---------------------------------------------------------------------------
# the Gate 1 comparisons
# ---------------------------------------------------------------------------

def test_fetch_new_from_zero(impl, compat_db):
    n = impl.fetch_new(0)
    assert len(n) == len(compat_db.all_rowids)
    assert_golden(cf.load_golden("fetch_new")["from_zero"], n, "fetch_new(0)")


def test_fetch_new_mid_cursor(impl, compat_db):
    cursor = compat_db.messages["link_scan"]
    n = impl.fetch_new(cursor)
    assert n and n[0]["rowid"] > cursor
    assert_golden(cf.load_golden("fetch_new")["mid_cursor"], n, f"fetch_new({cursor})")


def test_fetch_new_at_max_is_empty(impl):
    n = impl.fetch_new(impl.max_rowid())
    assert_golden(cf.load_golden("fetch_new")["at_max"], n, "fetch_new(max)")
    assert n == []


def test_fetch_edited_from_zero(impl, compat_db):
    n = impl.fetch_edited(0)
    msgs, mark = n
    assert [x["rowid"] for x in msgs] == [compat_db.messages["edited_at_insert"],
                                          compat_db.messages["text"]]
    assert mark == builders.BASE_DATE_NS + 20_000 and isinstance(mark, int)
    assert_golden(cf.load_golden("fetch_edited")["from_zero"], n, "fetch_edited(0)")


def test_fetch_edited_from_mid_mark(impl):
    g = cf.load_golden("fetch_edited")
    mark = builders.BASE_DATE_NS + 10_000
    n = impl.fetch_edited(mark)
    assert len(n[0]) == 1
    assert_golden(g["from_mid_mark"], n, f"fetch_edited({mark})")
    top = n[1]
    assert_golden(g["from_top"], impl.fetch_edited(top), "fetch_edited(top)")


def test_max_rowid_and_edit_mark(impl, compat_db):
    g = cf.load_golden("scalars")
    assert impl.max_rowid() == g["max_rowid"] == compat_db.all_rowids[-1]
    assert impl.max_date_edited() == g["max_date_edited"] == builders.BASE_DATE_NS + 20_000
    assert isinstance(impl.max_date_edited(), int)


@pytest.mark.parametrize("guid", cf.THREAD_MESSAGE_GUIDS)
def test_fetch_thread_messages(impl, guid):
    n = impl.fetch_thread_messages(guid, 50, None)
    assert_golden(cf.load_golden("fetch_thread_messages")["limit_50"][guid], n,
                  f"fetch_thread_messages({guid}, 50, None)")
    if guid == NO_SUCH_GUID:
        assert n == []


def test_fetch_thread_messages_before_rowid(impl, compat_db):
    before = compat_db.messages["link_scan"]
    n = impl.fetch_thread_messages(C1_GUID, 50, before)
    assert n and all(x["rowid"] < before for x in n)
    assert_golden(cf.load_golden("fetch_thread_messages")["c1_limit_50_before_link_scan"], n,
                  f"fetch_thread_messages(C1, 50, {before})")


def test_fetch_thread_messages_limit_window(impl, compat_db):
    g = cf.load_golden("fetch_thread_messages")
    before = compat_db.messages["urls"]
    n = impl.fetch_thread_messages(C1_GUID, 3, before)
    assert len(n) == 3 and [x["rowid"] for x in n] == sorted(x["rowid"] for x in n)
    assert_golden(g["c1_limit_3_before_urls"], n, "fetch_thread_messages(C1, 3, before)")
    assert_golden(g["c1_limit_3_before_0"], impl.fetch_thread_messages(C1_GUID, 3, 0),
                  "before_rowid=0 is 'no bound'")


def test_last_message_previews_all(impl, compat_db):
    conn = impl.db()
    try:
        rowids = compat_db.all_rowids
        n = impl.last_message_previews(conn, rowids)
        assert len(n) == len(rowids)
        assert_golden(cf.load_golden("last_message_previews")["all"], n,
                      "last_message_previews(all)")
    finally:
        conn.close()


def test_last_message_previews_subsets(impl, compat_db):
    g = cf.load_golden("last_message_previews")
    conn = impl.db()
    try:
        for label, rowids in cf.preview_cases(compat_db).items():
            if label == "all":
                continue
            assert_golden(g[label], impl.last_message_previews(conn, rowids),
                          f"last_message_previews({label})")
        assert impl.last_message_previews(conn, []) == {}
    finally:
        conn.close()


@pytest.mark.parametrize("case", list(FIND_CASES))
def test_find_chat_for_addresses(impl, case):
    addrs = FIND_CASES[case]
    conn = impl.db()
    try:
        n = impl.find_chat_for_addresses(conn, addrs)
    finally:
        conn.close()
    assert_golden(cf.load_golden("find_chat_for_addresses")[case], n,
                  f"find_chat_for_addresses({case})")
    expected_guid = cf.FIND_EXPECTED_GUID.get(case)
    if expected_guid is None:
        assert n is None
    else:
        assert n["chat_guid"] == expected_guid


def test_chat_services(impl, compat_db):
    g = cf.load_golden("chat_services")
    cases = cf.chat_services_cases(compat_db)
    conn = impl.db()
    try:
        n = impl.chat_services(conn, cases["all"])
        assert n[compat_db.chats["c1s"]] == "iMessage"        # stale SMS hint overridden
        assert n[compat_db.chats["csms"]] == "SMS"
        assert n[compat_db.chats["crcs"]] == "RCS"            # blank newest service skipped
        assert n[compat_db.chats["c1"]] == "iMessage"         # iMessageLite / None rows
        for label, rids in cases.items():
            assert_golden(g[label], impl.chat_services(conn, rids), f"chat_services({label})")
        assert impl.chat_services(conn, []) == {}
    finally:
        conn.close()


def test_chat_services_failure_returns_empty(impl, compat_db, capsys):
    """The relay wrapper's try/except: a dead connection yields {} and a log line."""
    rids = list(compat_db.chats.values())
    conn = impl.db()
    conn.close()
    assert impl.chat_services(conn, rids) == {}
    out = capsys.readouterr().out
    assert out.count("[service] chat service lookup failed:") == 1


def test_last_rowid_for(impl, compat_db):
    g = cf.load_golden("last_rowid_for")
    conn = impl.db()
    try:
        for name, rid in compat_db.chats.items():
            assert impl.last_rowid_for(conn, rid) == g[name] > 0, name
        assert impl.last_rowid_for(conn, cf.MISSING_CHAT_ROWID) == g["missing"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# what the equal JSON actually says: the design's cases, on the shipped path
# ---------------------------------------------------------------------------

def test_fixture_covers_design_cases(impl, compat_db):
    m = compat_db.messages
    by = {x["rowid"]: x for x in impl.fetch_new(0)}

    assert by[m["text"]]["text"] == "hello from alice"
    assert by[m["blob"]]["text"] == "blob only text"
    assert by[m["empty_blob"]]["text"] == "empty column, blob body"
    assert by[m["mutable_blob"]]["text"] == "mutable skeleton éè \U0001F60A"
    assert by[m["att_only_heic"]]["text"] is None

    # replies
    assert by[m["reply_to_att"]]["reply_to"] == {"text": "Attachment", "sender": "Alice Anders"}
    assert by[m["reply_to_long"]]["reply_to"] == {"text": LONG_TEXT[:120], "sender": "You"}
    assert by[m["reply_to_blob"]]["reply_to"] == {"text": "blob only text", "sender": "You"}
    assert by[m["reply_to_ws"]]["reply_to"] == {"text": "photo caption here",
                                                "sender": "Alice Anders"}
    assert by[m["reply_dangling"]]["reply_to"] is None
    assert by[m["g1_reply"]]["reply_to"] == {"text": "g1 carl (no join row)", "sender": CARL_PHONE}

    # link precedence: embedded blob > imageMetadata > string scan
    assert by[m["link_embedded"]]["link"]["image"] == f"/link_image/{m['link_embedded']}"
    assert by[m["link_embedded"]]["link"]["site"] == "News Site"
    assert by[m["link_embedded"]]["attachments"] == []          # plugin payload filtered
    assert by[m["link_meta"]]["link"]["image"] == "https://img.example.invalid/hero2.jpg"
    assert by[m["link_scan"]]["link"]["image"] == "https://cdn.example.invalid/pic.png"
    assert by[m["link_title_only"]]["link"]["url"] is None
    assert by[m["link_title_only"]]["link"]["title"] == "Just a title"
    assert by[m["link_empty"]]["link"] is None
    assert by[m["link_garbage"]]["link"] is None
    assert by[m["payload_no_balloon"]]["link"] is None
    assert by[m["link_jpeg"]]["link"]["image"] == f"/link_image/{m['link_jpeg']}"
    assert by[m["link_two_blobs"]]["link"]["image"] == f"/link_image/{m['link_two_blobs']}"
    assert by[m["link_decoy"]]["link"] == {"url": "https://decoy.example.invalid/", "title": "Decoy",
                                           "summary": None, "site": None, "image": None}
    for k in ("link_no_objects", "link_list_plist", "link_empty_payload", "link_only"):
        assert by[m[k]]["link"] is None, k
    assert by[m["no_sender_url"]]["sender"] is None and by[m["no_sender_url"]]["is_from_me"] is False

    # attachments: NULL-mime HEIC advertised as JPEG, failed HEIC left alone, hidden PNG flows
    assert by[m["att_only_heic"]]["attachments"] == [{
        "guid": "ATT-HEIC-1", "mime_type": "image/jpeg", "name": "IMG_0001.jpg",
        "url": "/attachment/ATT-HEIC-1?f=jpg"}]
    assert by[m["failed_heic"]]["attachments"] == [{
        "guid": "ATT-HEIC-FAILED", "mime_type": "image/heic", "name": "c.heic",
        "url": "/attachment/ATT-HEIC-FAILED"}]
    assert [a["guid"] for a in by[m["hidden_png"]]["attachments"]] == ["ATT-PNG-HIDDEN"]
    assert by[m["null_att"]]["attachments"] == [{"guid": "ATT-NULL", "mime_type": None,
                                                 "name": None, "url": "/attachment/ATT-NULL"}]
    assert by[m["flag_no_rows"]]["has_attachments"] is True
    assert by[m["flag_no_rows"]]["attachments"] == []
    assert by[m["att_flag_off"]]["attachments"] == []            # flag off: not looked up
    assert len(by[m["two_atts"]]["attachments"]) == 2

    # tapbacks and services
    assert by[m["tap2006"]]["assoc_type"] == 2006
    assert by[m["sticker"]]["assoc_type"] == 1000
    assert {by[m[k]]["service"] for k in ("lite", "svc_none", "sms_in", "rcs_in")} == \
        {"iMessageLite", None, "SMS", "RCS"}
    assert by[m["text"]]["date_edited"] == \
        chatdb_adapter.apple_date_to_unix(builders.BASE_DATE_NS + 20_000)
    assert by[m["text"]]["date_read"] is not None

    # chat naming: 1:1 resolved, groups raw
    assert by[m["text"]]["chat_name"] == "Alice Anders" and by[m["text"]]["is_group"] is False
    assert by[m["sms_in"]]["chat_name"] == CARL_PHONE and by[m["sms_in"]]["sender"] == CARL_PHONE
    assert by[m["g1_alice"]]["chat_name"] == "chat100" and by[m["g1_alice"]]["is_group"] is True
    assert by[m["g2_alice"]]["chat_name"] == "Synthetic Crew"


def test_fixture_previews_cover_every_branch(impl, compat_db):
    m = compat_db.messages
    conn = impl.db()
    try:
        p = impl.last_message_previews(conn, compat_db.all_rowids)
    finally:
        conn.close()
    assert p[m["att_only_heic"]]["body"] == "\U0001F4F7 Photo"      # NULL mime .heic -> Photo
    assert p[m["video"]]["body"] == "\U0001F3A5 Video"
    assert p[m["pdf"]]["body"] == "\U0001F4CE doc.pdf"
    assert p[m["null_att"]]["body"] == "\U0001F4CE Attachment"
    assert p[m["flag_no_rows"]]["body"] == "\U0001F4CE Attachment"
    assert p[m["tap2000"]]["body"] == "Loved a message"
    assert p[m["tap2005"]]["body"] == "Questioned a message"
    assert p[m["tap2006"]]["body"].startswith("Reacted ")          # 2006 falls through to text
    assert p[m["tap3002"]]["body"] == "Removed a reaction"
    assert p[m["sticker"]]["body"] == "\U0001F4F7 Photo"           # 1000 -> attachment branch
    assert p[m["ws"]]["body"] == "photo caption here"
    assert p[m["text"]] == {"body": "hello from alice", "is_from_me": False, "sender": ALICE_PHONE}
    assert p[m["blob"]]["is_from_me"] is True and p[m["blob"]]["sender"] is None


# ---------------------------------------------------------------------------
# the guard rails
# ---------------------------------------------------------------------------

def test_conftest_refuses_library_paths(tmp_path, relay_module):
    real = Path.home() / "Library" / "Messages" / "chat.db"
    with pytest.raises(UnsafeDatabasePath):
        assert_safe_db_path(real, tmp_path)
    with pytest.raises(UnsafeDatabasePath):
        assert_safe_db_path(Path("~/Library/Messages/chat.db"), tmp_path)
    with pytest.raises(UnsafeDatabasePath):
        assert_safe_db_path(Path("/var/tmp/elsewhere.db"), tmp_path)
    # plain connect, bytes, PathLike and the library's file: URI are all refused
    for target in (str(real), bytes(real), real, f"file:{real}?mode=ro"):
        with pytest.raises(UnsafeDatabasePath):
            sqlite3.connect(target)
    # the adapter's db() goes through the same guard, with the plain path and
    # with the file: URI (readonly_uri) -- the relay's default IMSG_CHATDB is the
    # real path, so this is the open that must never succeed here
    r = relay_module.module
    try:
        for readonly_uri in (False, True):
            chatdb_adapter.configure(chatdb_path=str(real), resolve=r.resolve,
                                     att_public=r.att_public, person_key=r.person_key,
                                     group_title=r.group_title, self_raw=r.SELF_RAW,
                                     readonly_uri=readonly_uri)
            with pytest.raises(UnsafeDatabasePath):
                chatdb_adapter.db()
    finally:
        relay_module.configure(relay_module.chatdb_path)        # back to the tmp placeholder
    # and the library's opener goes through the same guard
    import imessage_chatdb
    with pytest.raises(UnsafeDatabasePath):
        imessage_chatdb.open_connection(real)
    with pytest.raises(UnsafeDatabasePath):
        imessage_chatdb.open_connection(real, readonly_uri=False)
    # in-memory and tmp_path databases still work
    sqlite3.connect(":memory:").close()
    sqlite3.connect(tmp_path / "ok.db").close()


def test_fixture_db_is_under_tmp_path(compat_db, tmp_path):
    assert compat_db.path.resolve().is_relative_to(tmp_path.resolve())
    assert not compat_db.path.resolve().is_relative_to((Path.home() / "Library").resolve())


def test_golden_files_are_complete_and_synthetic():
    """Every golden the recorder writes exists and names nothing but synthetic data."""
    for name in cf.GOLDEN_NAMES:
        text = (cf.GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")
        assert cf.golden_text(cf.load_golden(name)) == text, name      # canonical on-disk form
        for needle in ("/Users/", "/private/", "@gmail", "~/Library"):
            assert needle not in text, (name, needle)
        for addr in {w for w in text.replace('"', " ").split() if "@" in w}:
            assert addr.endswith("@example.invalid"), (name, addr)
