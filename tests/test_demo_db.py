"""``tools/make_demo_db.py``: the synthetic chat.db for screenshots and demos.

* the shape it promises: eight chats mixing iMessage and SMS (two groups, one
  named), about sixty messages over the last fourteen calendar days, threaded
  replies, tapbacks, one edited row, one PNG attachment on disk and two URL
  balloons; ROWID order follows date order; every reply / tapback target exists
  and is older than the row pointing at it; scene times are local wall-clock
  times, also across a daylight-saving change;
* idempotence: the same ``seed`` and ``now`` (and output path and time zone)
  give the same rows; a rebuild replaces the file (no ``-wal`` / ``-shm`` left
  behind), also when the path contains ``#``, ``?``, ``%`` or a space; a file
  this script did not make is refused, and so is any path under ``~/Library``
  or inside the relay checkout (lexically, before anything is opened);
* the relay reads it through the shipped wiring: ``relay.fetch_threads`` (the
  thread list the app shows) and ``chatdb_adapter.fetch_thread_messages`` (one
  thread), plus the ``/attachment`` and ``/link_image`` endpoint functions on the
  photo and the Apple News-style card;
* the names server (``--serve-contacts``): the relay's own BlueBubbles engine
  reads the six fictional people from it, the thread list is then titled by
  name, and every write is refused, so nothing can be sent;
* the printed recipe: parsed by a real shell it yields exactly ``relay_env``,
  paths with spaces included; it leaves no real integration configured and no
  engine that could hand a message to Messages; a relay imported with it reads
  none of the values a hostile ``.env`` offers;
* the CLI: ``main()`` prints the recipe and exits 0, in-process and as a
  subprocess, twice, with a new random token each time.

Everything is built under ``tmp_path``.  The conftest's ``sqlite3.connect``
guard keeps ``~/Library`` unreachable in the pytest process; the subprocess
tests rely on the tool's own path check and on ``IMSG_CHATDB`` naming the
synthetic file.  No test reads the real ``.env`` or runs ``osascript``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import shlex
import signal
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.responses import FileResponse, Response
from fastapi.testclient import TestClient

import chatdb_adapter
from engines.bluebubbles import BlueBubblesEngine
from engines.chain import build_chain, engine_names
from engines.features import derive_features
from tests.test_relay_compat import relay_state  # noqa: F401  (autouse: snapshot/restore relay globals + state file)
from tools import make_demo_db as demo

#: A fixed anchor so the rows (and the assertions about them) do not depend on
#: the wall clock: 2026-10-06 18:00 local time, as the CLI's ``--now`` would parse it.
NOW_ISO = "2026-10-06T18:00:00"
NOW = demo._parse_now(NOW_ISO)
DAY = 86_400


#: A stand-in demo token for the tests that need a fixed one (the tool makes a random
#: one).  Deliberately not random-looking, so no secret scanner mistakes it for a real one.
TOKEN = "t" * 32

#: The names the relay should show once it has read the names server, newest thread first.
THREAD_TITLES = ["Alex Rivera", "Weekend plan", "Sam Okafor", "Morgan Chen", "Riley Park",
                 "Priya, Morgan", "Priya Natarajan", "Jordan Lee"]


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _one(conn: sqlite3.Connection, sql: str, *args) -> object:
    return conn.execute(sql, args).fetchone()[0]


@pytest.fixture
def built(tmp_path):
    """One demo database under ``tmp_path`` plus a read-only connection to it."""
    info = demo.build_demo_db(tmp_path / "demo" / "demo.db", seed=3, now=NOW)
    conn = _ro(info.path)
    try:
        yield info, conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------

def test_counts_match_the_script_and_the_promise(built):
    info, c = built
    assert info.path.is_file() and info.path.parent == info.files_dir.parent
    assert _one(c, "SELECT count(*) FROM chat") == info.chat_count == len(demo.CHATS) == 8
    assert 6 <= info.chat_count <= 8
    n = _one(c, "SELECT count(*) FROM message")
    assert n == info.message_count == sum(len(s.turns) for s in demo.SCRIPT)
    assert 55 <= n <= 70, n                                        # "about sixty"
    assert _one(c, "SELECT count(*) FROM chat_message_join") == n   # every row is in exactly one chat
    assert _one(c, "SELECT count(*) FROM message WHERE associated_message_type BETWEEN 2000 AND 2999") \
        == info.tapback_count >= 2
    assert _one(c, "SELECT count(*) FROM message WHERE thread_originator_guid IS NOT NULL") \
        == info.reply_count >= 3
    assert _one(c, "SELECT count(*) FROM message WHERE date_edited > 0") == info.edited_count == 1
    assert _one(c, "SELECT count(*) FROM attachment") == info.attachment_count == 1
    assert _one(c, "SELECT count(*) FROM message_attachment_join") == 1
    assert _one(c, "SELECT count(*) FROM message WHERE balloon_bundle_id = ?", demo.LINK_BALLOON) \
        == info.link_count == 2
    # the marker that lets a rebuild recognise its own output
    assert dict(c.execute(f"SELECT key, value FROM {demo.MARKER_TABLE}").fetchall())["seed"] == "3"


def test_chats_mix_imessage_and_sms_groups_and_an_email_handle(built):
    info, c = built
    chats = {r["guid"]: r for r in c.execute("SELECT * FROM chat")}
    assert set(chats) == set(demo.CHATS) == set(info.chat_rowids)
    assert {r["service_name"] for r in chats.values()} == {"iMessage", "SMS"}
    assert {r["service"] for r in c.execute("SELECT DISTINCT service FROM message")} == {"iMessage", "SMS"}
    groups = [g for g, r in chats.items() if r["style"] == 43]
    assert len(groups) == 2 and chats[demo.WEEKEND_GUID]["display_name"] == "Weekend plan"
    assert chats[demo.MOVERS_GUID]["display_name"] is None            # untitled group: relay names it
    assert all(chats[g]["style"] == 45 for g in chats if g not in groups)
    assert "@" in demo.MORGAN and chats[demo.MORGAN_GUID]["chat_identifier"] == demo.MORGAN
    # every chat has messages; the members of every chat are joined
    for guid, rowid in info.chat_rowids.items():
        assert info.message_rowids[guid], guid
        members = {r[0] for r in c.execute(
            "SELECT h.id FROM chat_handle_join j JOIN handle h ON h.ROWID = j.handle_id WHERE j.chat_id = ?",
            (rowid,))}
        assert members == set(demo.CHATS[guid].members), guid
    # the SMS chat's messages are SMS rows from an SMS handle
    svc = {r[0] for r in c.execute(
        "SELECT m.service FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID "
        "WHERE j.chat_id = ?", (info.chat_rowids[demo.JORDAN_GUID],))}
    assert svc == {"SMS"}
    assert _one(c, "SELECT service FROM handle WHERE id = ?", demo.JORDAN) == "SMS"
    # only fictional identities: +1555 numbers and example.com addresses
    for (hid,) in c.execute("SELECT id FROM handle"):
        assert hid.startswith("+1555") or hid.endswith("@example.com"), hid
    assert demo.SELF_NUMBER not in {r[0] for r in c.execute("SELECT id FROM handle")}


def test_dates_span_two_weeks_ending_now_in_rowid_order(built):
    info, c = built
    rows = c.execute("SELECT ROWID, date FROM message ORDER BY ROWID").fetchall()
    dates = [chatdb_adapter.apple_date_to_unix(r["date"]) for r in rows]
    assert dates == sorted(dates)                                   # ROWID order is date order
    # the script covers SPAN_DAYS calendar days, today included: the oldest scene is SPAN_DAYS - 1 back
    assert demo.SPAN_DAYS == max(s.days_ago for s in demo.SCRIPT) + 1 == 14
    assert NOW - demo.SPAN_DAYS * DAY < dates[0] <= NOW - (demo.SPAN_DAYS - 2) * DAY
    assert dt.date.fromtimestamp(dates[0]) == dt.date.fromtimestamp(NOW) - dt.timedelta(days=demo.SPAN_DAYS - 1)
    assert NOW - 1 * DAY < dates[-1] <= NOW                         # ends "now" (a few minutes old at most)
    assert dates[-1] >= NOW - 15 * 60
    days = {dt.date.fromtimestamp(d) for d in dates}
    assert 10 <= len(days) <= demo.SPAN_DAYS                        # spread over the fortnight, not clustered
    # the join's message_date is the row's date
    assert _one(c, "SELECT count(*) FROM chat_message_join j JOIN message m ON m.ROWID = j.message_id "
                   "WHERE j.message_date <> m.date") == 0


def test_replies_and_tapbacks_point_at_older_rows_in_the_same_chat(built):
    info, c = built
    by_guid = {r["guid"]: r for r in c.execute(
        "SELECT m.guid, m.ROWID AS rowid, j.chat_id FROM message m JOIN chat_message_join j "
        "ON j.message_id = m.ROWID")}
    replies = c.execute("SELECT guid, thread_originator_guid AS t, thread_originator_part AS p "
                        "FROM message WHERE thread_originator_guid IS NOT NULL").fetchall()
    assert len(replies) == info.reply_count
    for r in replies:
        target = by_guid[r["t"]]
        assert target["rowid"] < by_guid[r["guid"]]["rowid"] and target["chat_id"] == by_guid[r["guid"]]["chat_id"]
        assert r["p"] == "0:0:0"
    taps = c.execute("SELECT guid, associated_message_guid AS a, associated_message_type AS t, text "
                     "FROM message WHERE associated_message_type <> 0").fetchall()
    assert len(taps) == info.tapback_count
    for r in taps:
        assert r["a"].startswith("p:0/") and 2000 <= r["t"] <= 2005
        target = by_guid[r["a"][4:]]
        assert target["rowid"] < by_guid[r["guid"]]["rowid"] and target["chat_id"] == by_guid[r["guid"]]["chat_id"]
        assert r["text"].split(" ")[0] in {v.split(" ")[0] for v in demo.TAPBACK_VERBS}


def test_attachment_is_a_real_png_on_disk(built):
    info, c = built
    a = c.execute("SELECT * FROM attachment").fetchone()
    assert a["guid"] == demo.ATTACHMENT_GUID and a["mime_type"] == "image/png"
    assert a["transfer_name"] == demo.ATTACHMENT_NAME and a["uti"] == "public.png"
    # stored as "~/..." under the home directory, absolute otherwise; the relay expands either
    path = Path(a["filename"]).expanduser()
    assert path == info.attachment_path and path.is_absolute() and path.parent == info.files_dir
    data = path.read_bytes()
    assert a["total_bytes"] == len(data)
    width, height = _png_dimensions(data)
    assert (width, height) == (320, 240)
    # the owning message is an attachment-only row (text NULL, flag set)
    m = c.execute("SELECT m.text, m.cache_has_attachments FROM message m JOIN message_attachment_join j "
                  "ON j.message_id = m.ROWID").fetchone()
    assert m["text"] is None and m["cache_has_attachments"] == 1


def test_attachment_opens_with_pillow(built):
    """Its own test, so a venv without Pillow skips only this."""
    pil = pytest.importorskip("PIL.Image", reason="pillow is optional")
    info, _ = built
    with pil.open(info.attachment_path) as img:
        img.load()
        assert img.size == (320, 240) and img.mode == "RGB"


def test_attachment_path_is_home_relative_under_the_home_directory(tmp_path, monkeypatch, relay_module):
    """Under the home directory the row says ``~/...`` (as real chat.db rows do),
    so the database does not carry the account name; the relay expands it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Path.home() == tmp_path
    info = demo.build_demo_db(tmp_path / "selfbubbles-demo" / "chat.db", seed=1, now=NOW)
    conn = _ro(info.path)
    try:
        stored = _one(conn, "SELECT filename FROM attachment")
    finally:
        conn.close()
    assert stored == f"~/selfbubbles-demo/chat_files/{demo.ATTACHMENT_NAME}"
    relay_module.configure(info.path)
    resp = relay_module.module.attachment(demo.ATTACHMENT_GUID)
    assert isinstance(resp, FileResponse) and Path(resp.path) == info.attachment_path


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Decode the PNG by hand (signature, IHDR, inflatable IDAT, IEND) and return its size."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks, idat = 8, [], b""
    while pos < len(data):
        (length,), tag = struct.unpack(">I", data[pos:pos + 4]), data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        assert struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0] == zlib.crc32(tag + body) & 0xFFFFFFFF
        chunks.append(tag)
        if tag == b"IDAT":
            idat += body
        pos += 12 + length
    assert chunks[0] == b"IHDR" and chunks[-1] == b"IEND"
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    assert (depth, colour) == (8, 2)
    assert len(zlib.decompress(idat)) == height * (1 + 3 * width)
    return width, height


def test_make_png_sizes_and_the_hero_threshold():
    hero = demo.make_png(64, 40, 4, compress=0)
    assert _png_dimensions(hero) == (64, 40) and len(hero) > 3000     # the library's embedded-image floor
    assert demo.make_png(8, 8, 1) == demo.make_png(8, 8, 1) != demo.make_png(8, 8, 2)


# ---------------------------------------------------------------------------
# idempotence and guards
# ---------------------------------------------------------------------------

def _rows(path: Path) -> dict[str, list[tuple]]:
    conn = _ro(path)
    try:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in ("handle", "chat", "message", "attachment", "chat_message_join",
                          "chat_handle_join", "message_attachment_join")}
    finally:
        conn.close()


def test_rebuild_replaces_the_file_and_reproduces_the_rows(tmp_path):
    out = tmp_path / "demo.db"
    first = demo.build_demo_db(out, seed=5, now=NOW)
    rows_1 = _rows(out)
    # a stale sidecar and a changed row must not survive the rebuild
    w = sqlite3.connect(out)
    w.execute("UPDATE message SET text = 'tampered' WHERE ROWID = 1")
    w.commit()
    w.close()
    out.with_name("demo.db-wal").write_bytes(b"")
    second = demo.build_demo_db(out, seed=5, now=NOW)
    # the builder's writer was the last connection: closing it removed the sidecars
    # (a read-only reader opened later leaves empty ones behind, as on a real chat.db)
    assert not out.with_name("demo.db-wal").exists() and not out.with_name("demo.db-shm").exists()
    assert _rows(out) == rows_1
    assert second.message_count == first.message_count and second.chat_rowids == first.chat_rowids
    assert first.attachment_path.read_bytes() == demo.make_png(320, 240, 5)
    # a different seed moves the jitter
    demo.build_demo_db(out, seed=6, now=NOW)
    assert _rows(out)["message"] != rows_1["message"]
    assert _rows(out)["chat"] == rows_1["chat"]


def _dates(path: Path) -> list[int]:
    conn = _ro(path)
    try:
        return [r[0] for r in conn.execute("SELECT date FROM message ORDER BY ROWID")]
    finally:
        conn.close()


@pytest.fixture
def pacific_time():
    """Run the test in America/Los_Angeles, whatever the machine's zone is."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_a_different_now_moves_the_dates(tmp_path, pacific_time):
    """Times are fixed at build time.  "now" on another day moves every date by
    that day; later the same day only the scene anchored to "now" moves, because
    the other scenes sit at fixed wall-clock times.  (Pinned to one zone: no
    daylight-saving change falls inside this fortnight there.)"""
    out = tmp_path / "demo.db"
    now = demo._parse_now(NOW_ISO)
    demo.build_demo_db(out, seed=5, now=now)
    base = _dates(out)
    demo.build_demo_db(out, seed=5, now=now + DAY)
    assert [b - a for a, b in zip(base, _dates(out))] == [DAY * 1_000_000_000] * len(base)
    demo.build_demo_db(out, seed=5, now=now + 3600)
    moved = [i for i, (a, b) in enumerate(zip(base, _dates(out))) if a != b]
    anchored = len(demo.SCRIPT[-1].turns)
    assert demo.SCRIPT[-1].hour is None and moved == list(range(len(base) - anchored, len(base)))


def test_scene_times_are_wall_clock_times_across_a_dst_change(pacific_time):
    """``--now 2026-11-05T12:00:00`` is four days after the US change of
    1 November 2026: the scenes before it must still start at their own
    hour:minute (a fixed UTC offset would put them an hour late)."""
    now = demo._parse_now("2026-11-05T12:00:00")
    today = dt.date.fromtimestamp(now)
    starts = {id(scene): stamp for stamp, scene, turn in demo._schedule(now, random.Random(1))
              if turn is scene.turns[0]}
    dst = set()
    for scene in demo.SCRIPT:
        if scene.hour is None:
            continue
        local = dt.datetime.fromtimestamp(starts[id(scene)])
        assert (local.hour, local.minute) == (scene.hour, scene.minute), scene.days_ago
        assert local.date() == today - dt.timedelta(days=scene.days_ago)
        dst.add(time.localtime(starts[id(scene)]).tm_isdst)
    assert dst == {0, 1}                                            # the change is inside the window


@pytest.mark.parametrize("folder", ["demo #1", "what?", "demo%20x", "with space"])
def test_rebuild_finds_its_own_marker_when_the_path_has_uri_characters(tmp_path, folder):
    out = tmp_path / folder / "chat.db"
    demo.build_demo_db(out, seed=1, now=NOW)
    assert demo.is_demo_database(out)
    demo.build_demo_db(out, seed=1, now=NOW)                        # not refused as "not made by this script"
    assert sorted(p.name for p in tmp_path.iterdir()) == [folder]   # and no stray file beside the folder


def test_refuses_to_replace_a_file_it_did_not_make(tmp_path):
    out = tmp_path / "other.db"
    out.write_bytes(b"not a sqlite database at all")
    with pytest.raises(demo.NotADemoDatabase):
        demo.build_demo_db(out, seed=1, now=NOW)
    assert out.read_bytes() == b"not a sqlite database at all"
    # a real-shaped chat.db without the marker is refused too
    w = sqlite3.connect(out.with_name("foreign.db"))
    w.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT)")
    w.commit()
    w.close()
    assert not demo.is_demo_database(out.with_name("foreign.db"))
    with pytest.raises(demo.NotADemoDatabase):
        demo.build_demo_db(out.with_name("foreign.db"), seed=1, now=NOW)


def test_refuses_paths_under_library_without_opening_anything():
    real = Path.home() / "Library" / "Messages" / "chat.db"
    with pytest.raises(demo.UnsafeOutputPath):
        demo.check_output_path(real)
    with pytest.raises(demo.UnsafeOutputPath):
        demo.check_output_path(Path("~/Library/Messages/chat.db"))
    with pytest.raises(demo.UnsafeOutputPath):
        demo.build_demo_db(real, seed=1, now=NOW)       # raises before the conftest guard would


def test_refuses_paths_inside_the_relay_checkout(tmp_path, monkeypatch):
    """The checkout's ``.gitignore`` covers only the usual demo file names, not
    a database, and the state file beside the database would hold a real push
    token: the example path of an earlier draft (``demo/chat.db``) is refused,
    and nothing is created."""
    checkout = Path(__file__).resolve().parent.parent
    assert demo.ROOT == checkout
    for inside in (checkout / "demo" / "chat.db", checkout / "chat.db", checkout):
        with pytest.raises(demo.UnsafeOutputPath, match="relay checkout"):
            demo.check_output_path(inside)                          # lexical: nothing is created
    assert demo.check_output_path(checkout.parent / "selfbubbles-demo" / "chat.db")   # a sibling is fine
    # the build itself, against a stand-in checkout so a regression cannot litter the real one
    stand_in = tmp_path / "checkout"
    monkeypatch.setattr(demo, "ROOT", stand_in)
    with pytest.raises(demo.UnsafeOutputPath):
        demo.build_demo_db(stand_in / "demo" / "chat.db", seed=1, now=NOW)
    assert not stand_in.exists()


# ---------------------------------------------------------------------------
# through the relay's own reader
# ---------------------------------------------------------------------------

def test_relay_lists_the_threads(built, relay_module, capsys):
    info, _ = built
    r = relay_module.module
    r.CONTACTS.clear()                                  # no BlueBubbles: raw handles, as in a demo without it
    relay_module.configure(info.path)
    threads = r.fetch_threads(200)
    assert "[reads] baseline initialized" in capsys.readouterr().out
    assert r.load_state()["reads_baseline"] == max(max(v) for v in info.message_rowids.values())
    assert relay_module.state_path.exists() and relay_module.state_path != info.state_path

    assert [t["chat_guid"] for t in threads] == sorted(
        info.message_rowids, key=lambda g: max(info.message_rowids[g]), reverse=True)
    by_guid = {t["chat_guid"]: t for t in threads}
    assert set(by_guid) == set(demo.CHATS)
    for t in threads:
        assert t["preview"], t["chat_guid"]
        assert t["last_rowid"] == max(info.message_rowids[t["chat_guid"]])
        assert t["unread"] == 0 and t["pinned"] is False and t["archived"] is False
        assert t["last_date"] <= NOW
    weekend = by_guid[demo.WEEKEND_GUID]
    assert weekend["chat_name"] == "Weekend plan" and weekend["is_group"] is True
    assert set(weekend["handles"]) == set(demo.CHATS[demo.WEEKEND_GUID].members)
    assert weekend["icon_url"].startswith("/chat_icon/")
    assert weekend["preview"].endswith("Now we are talking")          # "<sender>: text" in a group
    movers = by_guid[demo.MOVERS_GUID]
    assert movers["chat_name"] == f"{demo.PRIYA}, {demo.MORGAN}"      # untitled group -> members (no contacts)
    alex = by_guid[demo.ALEX_GUID]
    assert alex["chat_name"] == demo.ALEX and alex["is_group"] is False
    assert alex["service"] == "iMessage" and alex["via_label"] == "iMessage" and alex["send_warning"] is None
    assert alex["preview"] == "On it."
    jordan = by_guid[demo.JORDAN_GUID]
    assert jordan["service"] == "SMS" and jordan["via_label"].startswith("SMS · sent through")
    assert jordan["send_warning"]
    assert by_guid[demo.MORGAN_GUID]["handles"] == [demo.MORGAN]


def test_relay_reads_one_thread_with_photo_reply_tapback_and_edit(built, relay_module):
    info, _ = built
    r = relay_module.module
    r.CONTACTS.clear()
    relay_module.configure(info.path)

    msgs = chatdb_adapter.fetch_thread_messages(demo.ALEX_GUID, 200, None)
    assert [m["rowid"] for m in msgs] == info.message_rowids[demo.ALEX_GUID]   # oldest first, all of them
    assert all(m["chat_guid"] == demo.ALEX_GUID and m["is_group"] is False and m["service"] == "iMessage"
               for m in msgs)
    assert {m["sender"] for m in msgs if not m["is_from_me"]} == {demo.ALEX}
    assert all(m["sender"] is None for m in msgs if m["is_from_me"])

    photo = [m for m in msgs if m["has_attachments"]]
    assert len(photo) == 1
    photo = photo[0]
    assert photo["text"] is None and photo["is_from_me"] == 0
    assert photo["attachments"] == [{"guid": demo.ATTACHMENT_GUID, "mime_type": "image/png",
                                     "name": demo.ATTACHMENT_NAME, "url": f"/attachment/{demo.ATTACHMENT_GUID}"}]
    reply = [m for m in msgs if m["reply_to_guid"] == photo["guid"]]
    assert len(reply) == 1 and reply[0]["text"] == "Told you. That broth."
    assert reply[0]["reply_to"] == {"text": "Attachment", "sender": demo.ALEX}
    tap = [m for m in msgs if m["assoc_type"]]
    assert len(tap) == 1 and tap[0]["assoc_type"] == 2000 and tap[0]["assoc_guid"] == f"p:0/{photo['guid']}"
    assert tap[0]["text"] == "Loved an image" and tap[0]["is_from_me"] == 1
    other_replies = [m for m in msgs if m["reply_to_guid"] and m is not reply[0]]
    assert other_replies and all(m["reply_to"]["text"] for m in other_replies)
    assert all(m["date_edited"] is None for m in msgs)               # the edited row is in another chat
    dates = [m["date"] for m in msgs]
    assert dates == sorted(dates) and dates[-1] <= NOW

    # the one edited message
    morgan = chatdb_adapter.fetch_thread_messages(demo.MORGAN_GUID, 200, None)
    edited = [m for m in morgan if m["date_edited"]]
    assert len(edited) == 1 and edited[0]["is_from_me"] == 1 and edited[0]["date_edited"] > edited[0]["date"]

    # /attachment serves the PNG from the files directory
    resp = r.attachment(demo.ATTACHMENT_GUID)
    assert isinstance(resp, FileResponse)
    assert Path(resp.path) == info.attachment_path and resp.media_type == "image/png"
    assert resp.filename == demo.ATTACHMENT_NAME


def test_relay_decodes_the_link_cards_offline(built, relay_module):
    info, _ = built
    r = relay_module.module
    relay_module.configure(info.path)

    sam = chatdb_adapter.fetch_thread_messages(demo.SAM_GUID, 200, None)
    news = [m for m in sam if m["link"]]
    assert len(news) == 1
    news = news[0]
    assert news["text"] == demo.APPLE_NEWS_URL and news["is_from_me"] == 0
    assert news["link"] == {"url": demo.APPLE_NEWS_URL,
                            "title": "Council approves protected bike lanes on Harbor Street",
                            "summary": "After four years of hearings, the city will build 3.2 miles of "
                                       "separated lanes between the harbor and the rail station.",
                            "site": "Harbor Gazette",
                            "image": f"/link_image/{news['rowid']}"}
    # the hero image is embedded in payload_data: served without any network
    resp = r.link_image(news["rowid"])
    assert isinstance(resp, Response) and resp.media_type == "image/png"
    assert _png_dimensions(resp.body) == (64, 40)
    liked = [m for m in sam if m["assoc_type"]]
    assert len(liked) == 1 and liked[0]["assoc_guid"] == f"p:0/{news['guid']}" and liked[0]["assoc_type"] == 2001

    weekend = chatdb_adapter.fetch_thread_messages(demo.WEEKEND_GUID, 200, None)
    trail = [m for m in weekend if m["link"]]
    assert len(trail) == 1 and trail[0]["is_from_me"] == 1
    assert trail[0]["link"]["url"] == demo.TRAIL_URL and trail[0]["link"]["site"] == "example.com"
    assert trail[0]["link"]["image"] is None                         # no hero: a plain card
    replies = [m for m in weekend if m["reply_to_guid"]]
    assert len(replies) == 2 and all(m["reply_to"]["sender"] for m in replies)


# ---------------------------------------------------------------------------
# the names server (--serve-contacts)
# ---------------------------------------------------------------------------

@pytest.fixture
def names_server():
    """The tool's names server on a free loopback port, in a thread; yields its URL."""
    server = demo.make_contacts_server(0)
    assert server.server_address[0] == "127.0.0.1"                  # never beyond this machine
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_names_server_serves_the_six_people_and_refuses_everything_else(names_server, capfd):
    bb = BlueBubblesEngine(names_server, demo.DEMO_CONTACTS_PASSWORD)   # the relay's own client
    assert bb.ping() is True
    records = bb.contacts()
    assert records == demo.contacts_payload()
    assert sorted(r["displayName"] for r in records) == sorted(name for name, _ in demo.PEOPLE.values())
    assert {a["address"] for r in records for a in r["phoneNumbers"] + r["emails"]} == set(demo.PEOPLE)
    marker = {"password": "a-password-that-must-not-be-logged"}
    with httpx.Client(timeout=5) as c:
        assert c.get(f"{names_server}/api/v1/chat/x/icon", params=marker).status_code == 404
        assert c.get(f"{names_server}/api/v1/message/count", params=marker).status_code == 404
        for method in ("post", "put", "patch", "delete"):
            r = getattr(c, method)(f"{names_server}/api/v1/message/text", params=marker)
            assert r.status_code == 501 and "read-only" in r.json()["message"], method
    out, err = capfd.readouterr()
    assert out == "" and err == ""                                  # silent: no request line, no password


def test_relay_shows_the_fictional_names_and_cannot_send(built, relay_module, names_server,
                                                        monkeypatch, tmp_path):
    """The recipe's engine settings on the relay module, the way it reads them
    at import: names come from the names server, and no send, tapback, new
    chat or FaceTime call gets past it.  ``osascript`` is replaced by a
    recorder that must stay empty."""
    info, _ = built
    r = relay_module.module
    env = demo.relay_env(info, TOKEN)
    monkeypatch.setattr(r, "BB_URL", names_server)
    monkeypatch.setattr(r, "BB_PASSWORD", env["BB_PASSWORD"])
    monkeypatch.setattr(r, "SEND_ENGINES", env["SEND_ENGINES"])
    monkeypatch.setattr(r, "SEND_APPLESCRIPT_FALLBACK", env["SEND_APPLESCRIPT_FALLBACK"])
    monkeypatch.setattr(r.beeper, "BEEPER_TOKEN", env["BEEPER_TOKEN"])
    osascript, rig = [], []
    monkeypatch.setattr(r, "_applescript", lambda *a: osascript.append(a) or False)
    monkeypatch.setattr(r, "_launch_autoadmit", lambda *a, **k: rig.append(a))
    monkeypatch.setattr(r, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(r, "ICON_CACHE", tmp_path / "icons")
    relay_module.configure(info.path)
    assert r.engine_names(r._chain()) == ["bluebubbles"]

    r.CONTACTS.clear()
    r.load_contacts()
    assert len(r.CONTACTS) == len(demo.PEOPLE) == 6
    threads = r.fetch_threads(200)
    assert [t["chat_name"] for t in threads] == THREAD_TITLES
    by_guid = {t["chat_guid"]: t for t in threads}
    assert by_guid[demo.WEEKEND_GUID]["preview"] == "Alex: Now we are talking"
    assert by_guid[demo.MOVERS_GUID]["chat_name"] == "Priya, Morgan"          # untitled group: first names
    assert by_guid[demo.MORGAN_GUID]["chat_name"] == "Morgan Chen"            # the e-mail handle resolves too
    weekend = chatdb_adapter.fetch_thread_messages(demo.WEEKEND_GUID, 200, None)
    assert {m["sender"] for m in weekend if not m["is_from_me"]} == {"Alex Rivera", "Sam Okafor",
                                                                    "Priya Natarajan"}
    client = TestClient(r.app)                                      # no ``with``: startup hooks never run
    auth = {"X-Imsg-Token": r.IMSG_TOKEN}
    found = client.get("/contacts/search", params={"q": "ri"}, headers=auth).json()["results"]
    assert sorted(c["name"] for c in found) == ["Alex Rivera", "Priya Natarajan", "Riley Park"]

    refused = [
        client.post("/send", json={"chat_guid": demo.ALEX_GUID, "text": "hello"}, headers=auth),
        client.post("/send", json={"chat_guid": "iMessage;-;+15550109999", "text": "hello"}, headers=auth),
        client.post("/react", json={"chat_guid": demo.ALEX_GUID, "message_guid": "DEMO-MSG-0001",
                                    "reaction": "love"}, headers=auth),
        client.post("/create_chat", json={"addresses": ["+15550109999"], "text": "hello"}, headers=auth),
        client.post("/create_chat", json={"addresses": [demo.ALEX], "text": "hello"}, headers=auth),
        client.post("/send_attachment", data={"chat_guid": demo.ALEX_GUID},
                    files={"file": ("a.png", demo.make_png(8, 8), "image/png")}, headers=auth),
        client.post("/ft_link", headers=auth),
    ]
    for resp in refused:
        assert resp.status_code == 501, (resp.request.url.path, resp.status_code)
        assert "read-only" in resp.text
    assert client.get(f"/chat_icon/{quote(demo.WEEKEND_GUID, safe='')}", headers=auth).status_code == 404
    assert osascript == [] and rig == []
    assert not (tmp_path / "outbox").exists()


def test_serve_contacts_refuses_a_port_that_is_taken(names_server, capsys):
    port = int(names_server.rsplit(":", 1)[1])
    assert demo.port_in_use(port)
    assert demo.serve_contacts(port) == 2
    assert f"port {port}" in capsys.readouterr().err
    assert demo.main(["--serve-contacts", str(port)]) == 2
    assert "--contacts-port" in capsys.readouterr().err


def test_serve_contacts_cli_serves_until_interrupted():
    script = Path(demo.__file__)
    # A process that was started with SIGINT ignored (a background job of a
    # non-interactive shell, for one) hands that on to its children, and the
    # child's Python then never raises KeyboardInterrupt. A handler, unlike
    # "ignore", is reset to the default by exec: install one while the child
    # starts, so the test does not depend on how pytest itself was launched.
    inherited = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        p = subprocess.Popen([sys.executable, str(script), "--serve-contacts", "0"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL if inherited is None else inherited)
    try:
        first = p.stdout.readline()
        url = re.search(r"http://127\.0\.0\.1:\d+", first).group(0)
        assert "6 fictional contacts" in first
        assert BlueBubblesEngine(url, demo.DEMO_CONTACTS_PASSWORD).ping() is True
        p.send_signal(signal.SIGINT)
        assert p.wait(timeout=20) == 0
        assert p.stderr.read() == ""
    finally:
        if p.poll() is None:
            p.kill()
        p.stdout.close()
        p.stderr.close()


# ---------------------------------------------------------------------------
# the printed recipe
# ---------------------------------------------------------------------------

def _step2(text: str) -> list[str]:
    """The lines of the recipe's relay command, as printed."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("  IMSG_CHATDB="))
    end = lines.index("  venv/bin/python relay.py", start)
    return lines[start:end + 1]


def _environment_a_shell_gives(step2: list[str], cwd: Path) -> dict[str, str]:
    """Paste the command into ``/bin/sh`` with the relay replaced by a program
    that dumps its environment: what the relay would really be started with."""
    dump = shlex.join([sys.executable, "-c", "import json, os, sys; json.dump(dict(os.environ), sys.stdout)"])
    script = "\n".join(step2[:-1] + ["  " + dump]) + "\n"
    p = subprocess.run(["/bin/sh", "-c", script], cwd=cwd, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.parametrize("folder", ["plain", "with space", "demo #1"])
def test_recipe_survives_the_paste_and_configures_no_real_integration(tmp_path, folder):
    info = demo.build_demo_db(tmp_path / folder / "chat.db", seed=1, now=NOW)
    text = demo.run_instructions(info, TOKEN)
    env = demo.relay_env(info, TOKEN)
    assert demo.relay_command(env) == [line.strip() for line in _step2(text)]

    got = _environment_a_shell_gives(_step2(text), tmp_path)
    assert {k: got[k] for k in env} == env                           # every variable, value for value
    assert got["IMSG_CHATDB"] == str(info.path) and got["IMSG_STATE"] == str(info.state_path)
    assert info.state_path == info.path.with_name("chat_relay_state.json")
    assert got["TEXT_RELAY_LABEL"] == demo.DEMO_PHONE_LABEL and got["IMSG_SELF"] == demo.SELF_NUMBER
    assert got["IMSG_PORT"] == str(demo.DEMO_RELAY_PORT) not in ("8700", "8701")
    assert got["APPLE_NEWS_PREVIEWS"] == "0"
    assert got["IMESSAGE_CLI"] == "0"                 # step R6: an installed imessage-cli stays out of the demo
    # set, and empty: python-dotenv (override=False) then leaves them alone
    for key in ("BEEPER_TOKEN", "FCM_CREDS", "HA_TOKEN", "MAPKIT_TOKEN", "SEND_ENGINES"):
        assert got[key] == "", key

    # the relay's own readers of that environment: one engine, bound to the
    # names server; no AppleScript; nothing advertised
    chain = build_chain(got)
    assert engine_names(chain) == ["bluebubbles"]
    assert chain[0].url == f"http://127.0.0.1:{demo.DEMO_CONTACTS_PORT}"
    assert derive_features(got) == {"facetime": False, "map": False, "translate": False, "voice": False}
    # and without the names server's password there is no engine at all
    assert build_chain({**got, "BB_PASSWORD": ""}) == []
    # dropping the two send lines is what would bring AppleScript back
    assert "applescript" in engine_names(build_chain({**got, "SEND_APPLESCRIPT_FALLBACK": "1"}))

    assert f"--serve-contacts {demo.DEMO_CONTACTS_PORT}" in text
    assert f"curl -s -H 'X-Imsg-Token: {TOKEN}' http://127.0.0.1:{demo.DEMO_RELAY_PORT}/threads" in text
    assert "change-me" not in text and "NOTE:" not in text


_PROBE = """
import json, sys
import dotenv
_real = dotenv.load_dotenv
dotenv.load_dotenv = lambda *a, **k: _real(sys.argv[1], override=k.get("override", False))
import {module} as relay
out = {{"label": relay.TEXT_RELAY_LABEL, "self": relay.SELF_RAW, "port": relay.PORT,
       "engines": relay.engine_names(relay._chain()), "features": relay.FEATURES,
       "beeper": relay.beeper.enabled(), "fcm": bool(relay.FCM_CREDS), "ha": bool(relay.HA_TOKEN),
       "mapkit": bool(relay.MAPKIT_TOKEN), "bb_url": relay.BB_URL, "state": str(relay.STATE_PATH),
       "chatdb": relay.CHATDB, "token": relay.IMSG_TOKEN, "news": relay.LINK_ENRICHER.enabled}}
if sys.argv[2] == "doctor":
    out["doctor"] = relay.doctor_table()
print("PROBE" + json.dumps(out))
"""

#: What a real install's ``.env`` could hold.  Placeholders only; every URL is a discard port.
_HOSTILE_DOTENV = """\
IMSG_TOKEN=hostile-token
IMSG_SELF=+15550009999
IMSG_PORT=8700
TEXT_RELAY_LABEL="Hostile Phone (0199)"
APPLE_NEWS_PREVIEWS=1
BB_URL=http://127.0.0.1:9
BB_PASSWORD=hostile-bb-password
BEEPER_TOKEN=hostile-beeper-token
FCM_CREDS=/nonexistent/hostile-key.json
HA_TOKEN=hostile-ha-token
HA_URL=http://127.0.0.1:9
HA_LOCATIONS=Someone=device_tracker.someone
MAPKIT_TOKEN=hostile-mapkit-token
SEND_ENGINES=applescript
SEND_APPLESCRIPT_FALLBACK=1
FEATURE_FACETIME=1
FEATURE_MAP=1
FEATURE_TRANSLATE=1
FEATURE_VOICE=1
"""


def _import_relay_with(env: dict[str, str], dotenv_file: Path, relay_module, mode: str) -> dict:
    """Import the relay in a fresh interpreter with ``env`` and with python-dotenv
    pointed at ``dotenv_file`` (its real ``override=False`` logic; the real
    ``.env`` is never opened), and report what the relay then holds."""
    relay_dir = Path(relay_module.module.__file__).resolve().parent
    code = _PROBE.format(module=relay_module.name)
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ["HOME"], **env}
    p = subprocess.run([sys.executable, "-c", code, str(dotenv_file), mode], cwd=relay_dir, env=full,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-2000:]
    assert "hostile" not in p.stdout.split("PROBE", 1)[0]            # nothing printed at import either
    return json.loads(p.stdout.split("PROBE", 1)[1])


def test_recipe_wins_over_a_hostile_dotenv(built, relay_module, names_server, tmp_path):
    """The reason for every empty value in the recipe.  With a ``.env`` full of
    a real install's settings, a relay started with the printed environment
    still holds only the demo's; started with the five variables an earlier
    draft printed, it takes the Beeper token, the BlueBubbles password, the
    phone label, the map tokens and the AppleScript engine from the file."""
    pytest.importorskip("dotenv", reason="python-dotenv is optional for the relay")
    info, _ = built
    dotenv_file = tmp_path / "hostile.env"
    dotenv_file.write_text(_HOSTILE_DOTENV)
    port = int(names_server.rsplit(":", 1)[1])
    env = demo.relay_env(info, TOKEN, contacts_port=port)
    # not part of the recipe: keep the doctor's probes on this machine and its files under tmp_path
    local = {"MARIAN_URL": names_server, "OLLAMA_URL": names_server, "RELAY_DATA_DIR": str(tmp_path / "data")}

    got = _import_relay_with({**env, **local}, dotenv_file, relay_module, "doctor")
    doctor = got.pop("doctor")
    assert got == {
        "label": demo.DEMO_PHONE_LABEL, "self": [demo.SELF_NUMBER], "port": demo.DEMO_RELAY_PORT,
        "engines": ["bluebubbles"],
        "features": {"facetime": False, "map": False, "translate": False, "voice": False},
        "beeper": False, "fcm": False, "ha": False, "mapkit": False, "bb_url": names_server,
        "state": str(info.state_path), "chatdb": str(info.path), "token": TOKEN, "news": False,
    }
    assert "hostile" not in doctor.lower()
    rows = {}
    for line in doctor.splitlines()[3:]:
        name, status = (re.split(r" {2,}", line.strip()) + [""])[:2]
        rows[name] = status
    # the rows the tool tells the reader to check before a screenshot
    assert rows["chat.db"] == "readable"
    assert rows["BlueBubbles"] == "password set, server reachable"
    assert rows["Beeper (Google Messages)"] == "disabled"
    assert rows["FCM push"] == "disabled"
    assert rows["Home Assistant"] == "disabled"
    assert rows["MapKit"] == "not set"
    assert rows["send engines"] == "bluebubbles"
    assert rows["features advertised"] == "(none)"
    text = demo.run_instructions(info, TOKEN)
    for name in ("BlueBubbles", "Beeper", "FCM push", "Home Assistant", "MapKit", "send engines",
                 "features advertised"):
        full = next(n for n in rows if n.startswith(name))
        assert f'{name} "{rows[full]}"' in text.replace("\n", " "), name

    # the control: the five variables of the earlier draft, same .env. (IMESSAGE_CLI=0 is the
    # test's, not the draft's: where imessage-cli is installed its edit engine joins the chain
    # whatever SEND_ENGINES says (step R6), and "engines" below is about what the .env leaks.)
    draft = {k: env[k] for k in ("IMSG_CHATDB", "IMSG_STATE", "IMSG_SELF", "IMSG_TOKEN", "APPLE_NEWS_PREVIEWS")}
    leaked = _import_relay_with({**draft, **local, "IMESSAGE_CLI": "0"}, dotenv_file, relay_module, "values")
    assert leaked["beeper"] is True and leaked["ha"] is True and leaked["mapkit"] is True and leaked["fcm"] is True
    assert leaked["label"] == "Hostile Phone (0199)" and leaked["bb_url"] == "http://127.0.0.1:9"
    assert leaked["engines"] == ["applescript"] and leaked["port"] == 8700
    assert leaked["features"] == {"facetime": True, "map": True, "translate": True, "voice": True}
    assert not info.state_path.exists()                             # importing wrote no state


def test_every_build_prints_a_new_random_token(built):
    info, _ = built
    first, second = demo.run_instructions(info), demo.run_instructions(info)
    tokens = [re.search(r"IMSG_TOKEN=([0-9a-f]+) ", text).group(1) for text in (first, second)]
    assert tokens[0] != tokens[1] and all(len(t) == 32 for t in tokens)
    assert first.count(tokens[0]) == 2                               # the command and the check
    assert first.replace(tokens[0], "T") == second.replace(tokens[1], "T")


def test_busy_ports_are_called_out(built, names_server, tmp_path, capsys):
    info, _ = built
    busy = int(names_server.rsplit(":", 1)[1])
    assert demo.port_in_use(busy)
    with demo.socket.socket() as s:                                  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert not demo.port_in_use(free)
    relay_note = demo.run_instructions(info, TOKEN, busy_ports=(demo.DEMO_RELAY_PORT,))
    assert f"NOTE: something already listens on port {demo.DEMO_RELAY_PORT}" in relay_note
    assert "--relay-port N" in relay_note and "--contacts-port N" not in relay_note
    names_note = demo.run_instructions(info, TOKEN, busy_ports=(demo.DEMO_CONTACTS_PORT,))
    assert f"NOTE: something already listens on port {demo.DEMO_CONTACTS_PORT}" in names_note
    assert "--contacts-port N" in names_note and "--relay-port N" not in names_note
    # main() does the looking, and the port options reach every line that names a port
    assert demo.main([str(tmp_path / "ports.db"), "--now", NOW_ISO,
                      "--relay-port", str(busy), "--contacts-port", str(free)]) == 0
    text = capsys.readouterr().out
    assert f"NOTE: something already listens on port {busy}" in text and text.count("NOTE:") == 1
    assert f"IMSG_PORT={busy} " in text and f"http://127.0.0.1:{busy}/threads" in text
    assert f"BB_URL=http://127.0.0.1:{free} " in text and f"--serve-contacts {free}" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_main_prints_how_to_run_the_relay(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(demo.secrets, "token_hex", lambda n=16: "ab" * n)     # the one random part
    monkeypatch.setattr(demo, "port_in_use", lambda port: False)
    out = tmp_path / "cli.db"
    assert demo.main([str(out), "--seed", "2", "--now", NOW_ISO]) == 0
    text = capsys.readouterr().out
    assert f"Wrote {out.resolve()}" in text
    assert f"IMSG_CHATDB={shlex.quote(str(out.resolve()))} " in text
    assert f"IMSG_STATE={shlex.quote(str(out.resolve().with_name('cli_relay_state.json')))} " in text
    assert f"IMSG_SELF={demo.SELF_NUMBER}" in text and f"IMSG_TOKEN={'ab' * 16} " in text
    assert "APPLE_NEWS_PREVIEWS=0" in text and "venv/bin/python relay.py" in text
    assert "BEEPER_TOKEN= FCM_CREDS= HA_TOKEN= MAPKIT_TOKEN= " in text
    assert "SEND_ENGINES= SEND_APPLESCRIPT_FALLBACK=0 IMESSAGE_CLI=0 " in text and "BB_PASSWORD=demo " in text
    assert "relay_state.json" in text and "BlueBubbles" in text
    assert "8 chats" in text and "2 link cards" in text
    assert f"over the last {demo.SPAN_DAYS} calendar days, ending 2026-10-06 18:00" in text
    for name, _ in demo.PEOPLE.values():
        assert name in text
    # idempotent: a second run over the same file succeeds and says the same
    assert demo.main([str(out), "--seed", "2", "--now", NOW_ISO]) == 0
    assert capsys.readouterr().out == text


def test_main_reports_refusals_on_stderr(tmp_path, capsys, monkeypatch):
    out = tmp_path / "foreign.db"
    out.write_bytes(b"junk")
    assert demo.main([str(out)]) == 2
    err = capsys.readouterr().err
    assert "not a database made by this script" in err
    assert demo.main([str(Path.home() / "Library" / "Messages" / "chat.db")]) == 2
    assert "~/Library" in capsys.readouterr().err
    monkeypatch.setattr(demo, "ROOT", tmp_path / "checkout")        # a stand-in, never the real checkout
    assert demo.main([str(tmp_path / "checkout" / "demo" / "chat.db")]) == 2
    assert "relay checkout" in capsys.readouterr().err and not (tmp_path / "checkout").exists()
    with pytest.raises(SystemExit):
        demo.main([str(tmp_path / "x.db"), "--now", "yesterday"])
    with pytest.raises(SystemExit):
        demo.main([])                                               # neither a path nor --serve-contacts
    with pytest.raises(SystemExit):
        demo.main([str(tmp_path / "x.db"), "--serve-contacts"])     # the two modes do not mix
    assert not (tmp_path / "x.db").exists()


def test_cli_subprocess_twice(tmp_path):
    script = Path(demo.__file__)
    assert script == Path(__file__).resolve().parent.parent / "tools" / "make_demo_db.py"
    out = tmp_path / "sub.db"
    tokens = []
    for _ in range(2):
        p = subprocess.run([sys.executable, str(script), str(out), "--seed", "9", "--now", NOW_ISO],
                           capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stderr
        assert f"IMSG_CHATDB={shlex.quote(str(out))} " in p.stdout and f"IMSG_SELF={demo.SELF_NUMBER}" in p.stdout
        tokens.append(re.search(r"IMSG_TOKEN=([0-9a-f]{32}) ", p.stdout).group(1))
    assert tokens[0] != tokens[1]                                   # never a constant
    assert demo.is_demo_database(out)
    conn = _ro(out)
    try:
        assert _one(conn, "SELECT count(*) FROM chat") == 8
        assert 55 <= _one(conn, "SELECT count(*) FROM message") <= 70
    finally:
        conn.close()
