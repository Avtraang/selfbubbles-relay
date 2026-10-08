"""The Apple News link-preview glue in the relay module (``relay_new`` until the cutover).

``tests/test_link_enrich.py`` proves the module; this file proves the relay
side: ``enrich_links`` / ``schedule_link_resolves``, the overlay in the
``/thread/{guid}/messages`` endpoint, the ``update`` broadcast after a
background resolve, ``GET /link_preview_image/{key}`` and the
``APPLE_NEWS_PREVIEWS=0`` switch.

No network and no real database: the relay's ``LINK_ENRICHER`` is replaced by
an ``Enricher`` over a fake ``fetch`` with its cache under ``tmp_path``, and
the messages come from the synthetic compat database (plus rows added to that
per-test copy) or are plain dicts.  Every test skips cleanly when the selected
relay module (``RELAY_MODULE``) has no ``LINK_ENRICHER`` -- i.e. ``relay.py``
before the cutover.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

from link_enrich import Enricher, FetchError
from tests.compat_fixture import ALICE_PHONE, C1_GUID, G1_GUID, THREAD_MESSAGE_GUIDS
from tests import compat_fixture as cf
from tests.conftest import RELAY_STUB_ENV
from tests.fixtures import builders
from tests.test_link_enrich import (
    APPLE,
    PNG,
    PUB,
    FakeFetch,
    apple_page,
    key_of,
    standard_routes,
)
from tests.test_link_enrich import relay_msg as plain_msg
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)

STUB_TOKEN = RELAY_STUB_ENV["IMSG_TOKEN"]          # a placeholder, never the real token
AUTH = {"X-Imsg-Token": STUB_TOKEN}
IMAGE_URL = "https://cdn.example-news.com/a.png"
IMAGE_KEY = key_of(IMAGE_URL)
IMAGE_ROUTE = f"/link_preview_image/{IMAGE_KEY}"
APPLE2 = "https://apple.news/SecondSyntheticLink"
APPLE3 = "https://apple.news/ThirdSyntheticLink"


@pytest.fixture
def glue(relay_module, tmp_path, monkeypatch):
    """The relay module with a fake-backed enricher, empty scheduler state and a
    recording hub.  Skips when the module has no link glue (``relay.py`` today)."""
    r = relay_module.module
    if not hasattr(r, "LINK_ENRICHER"):
        pytest.skip(f"{relay_module.name} has no Apple News link glue")
    fake = FakeFetch(standard_routes())
    enricher = Enricher(tmp_path / "link_cache", fetch=fake)
    events: list[dict] = []

    async def record_broadcast(payload):
        events.append(payload)

    monkeypatch.setattr(r, "LINK_ENRICHER", enricher)
    monkeypatch.setattr(r, "_LINK_WAITERS", {})
    monkeypatch.setattr(r, "_LINK_QUEUE", [])
    monkeypatch.setattr(r, "_LINK_TASKS", set())
    monkeypatch.setattr(r.hub, "broadcast", record_broadcast)

    class Glue:
        pass

    g = Glue()
    g.r, g.fake, g.enricher, g.events, g.tmp = r, fake, enricher, events, tmp_path
    return g


@pytest.fixture
def client(relay_module):
    """ASGI test client; no ``with``, so startup hooks (poll loop, FCM) never run."""
    return TestClient(relay_module.module.app)


async def _drain(r, timeout: float = 3.0) -> None:
    """Wait until every scheduled resolve has finished."""
    deadline = time.monotonic() + timeout
    while r._LINK_TASKS or r._LINK_QUEUE or r._LINK_WAITERS:
        if time.monotonic() > deadline:
            raise AssertionError("link resolves did not finish")
        await asyncio.sleep(0.01)


def _add_apple_message(compat_db, chat_key: str = "c1", text: str = APPLE) -> int:
    h = builders.handle_rowid(compat_db.writer, ALICE_PHONE, create=False)
    return builders.add_message(compat_db.writer, compat_db.chats[chat_key], text=text, handle=h)


def _overlaid(link: dict) -> None:
    assert link == {"url": APPLE, "title": "Big story here", "summary": "Summary text",
                    "site": "Example News", "image": IMAGE_ROUTE, "resolved_url": PUB}
    assert list(link)[-1] == "resolved_url"


# ---------------------------------------------------------------------------
# enrich_links
# ---------------------------------------------------------------------------

def test_fixture_database_has_no_apple_news_message(compat_db, glue):
    """The golden database carries no apple.news link, so the overlay cannot have
    changed any recorded output; the apple.news rows below are added per test."""
    r = glue.r
    everything = r.fetch_new(0)
    assert everything and "apple.news" not in json.dumps(everything)
    assert r.enrich_links(everything) == []


def test_enrich_links_leaves_other_messages_byte_identical(compat_db, glue):
    r = glue.r
    asyncio.run(glue.enricher.resolve(APPLE))            # a warm cache must not matter either
    msgs = r.fetch_new(0)
    before = cf.dumps(msgs)
    assert r.enrich_links(msgs) == []
    assert cf.dumps(msgs) == before
    assert cf.dumps(msgs) == cf.dumps(cf.load_golden("fetch_new")["from_zero"])
    plain = [plain_msg("hello", None), plain_msg(None, None),
             plain_msg("https://example.com/a", {"url": "https://example.com/a", "title": "T",
                                                 "summary": None, "site": None, "image": None}),
             plain_msg("https://apple.news.evil.example/A", None)]
    snapshot = copy.deepcopy(plain)
    assert r.enrich_links(plain) == []
    assert json.dumps(plain) == json.dumps(snapshot)


def test_thread_endpoint_unchanged_for_every_golden_thread(compat_db, glue, client):
    for guid in THREAD_MESSAGE_GUIDS:
        resp = client.get(f"/thread/{guid}/messages", params={"limit": 50}, headers=AUTH)
        assert resp.status_code == 200
        assert cf.dumps(resp.json()["messages"]) == cf.dumps(glue.r.fetch_thread_messages(guid, 50, None)), guid
    assert glue.fake.calls == [] and glue.events == []
    assert not glue.r._LINK_WAITERS and not glue.r._LINK_QUEUE


def test_enrich_links_returns_url_and_rowid_for_uncached(glue):
    r = glue.r
    a, b, c = plain_msg(APPLE, None), plain_msg("hi", None), plain_msg(f"see {APPLE2}.", None)
    a["rowid"], b["rowid"], c["rowid"] = 11, 12, 13
    before = json.dumps([a, b, c])
    assert r.enrich_links([a, b, c]) == [(APPLE, 11), (APPLE2, 13)]
    assert json.dumps([a, b, c]) == before               # nothing cached: nothing overlaid
    assert glue.fake.calls == []                         # enrich_links never touches the network


def test_enrich_links_overlays_from_cache(glue):
    r = glue.r
    asyncio.run(glue.enricher.resolve(APPLE))
    bare = plain_msg(APPLE, None)
    card = plain_msg(APPLE, {"url": APPLE, "title": "Apple's own title", "summary": None,
                             "site": None, "image": None})
    assert r.enrich_links([bare, card]) == []
    _overlaid(bare["link"])
    assert bare["text"] == APPLE                          # the app hides text == link.url
    assert card["link"] == {"url": APPLE, "title": "Apple's own title", "summary": "Summary text",
                            "site": "Example News", "image": IMAGE_ROUTE, "resolved_url": PUB}


def test_enrich_links_survives_a_failing_overlay(glue, monkeypatch, capsys):
    r = glue.r

    def boom(msg):
        raise RuntimeError("synthetic failure with secret text")

    monkeypatch.setattr(glue.enricher, "apply", boom)
    msg = plain_msg(APPLE, None)
    assert r.enrich_links([msg]) == []
    out = capsys.readouterr().out
    assert "overlay failed (RuntimeError)" in out and "secret" not in out and "apple.news/" not in out


# ---------------------------------------------------------------------------
# /thread/{guid}/messages
# ---------------------------------------------------------------------------

def test_thread_messages_returns_overlaid_card_from_seeded_cache(compat_db, glue, client):
    asyncio.run(glue.enricher.resolve(APPLE))
    calls_after_seed = len(glue.fake.calls)
    rowid = _add_apple_message(compat_db)
    resp = client.get(f"/thread/{C1_GUID}/messages", headers=AUTH)
    assert resp.status_code == 200
    by = {m["rowid"]: m for m in resp.json()["messages"]}
    _overlaid(by[rowid]["link"])
    assert by[rowid]["text"] == APPLE
    assert len(glue.fake.calls) == calls_after_seed      # served from cache: no fetch, nothing queued
    assert not glue.r._LINK_WAITERS and glue.events == []
    # every other message in the thread is exactly what the adapter returns
    plain = {m["rowid"]: m for m in glue.r.fetch_thread_messages(C1_GUID, 50, None)}
    for rid, m in by.items():
        if rid != rowid:
            assert cf.dumps(m) == cf.dumps(plain[rid]), rid
    # and the image the card points at is served
    img = client.get(by[rowid]["link"]["image"], headers=AUTH)
    assert img.status_code == 200 and img.content == PNG


def test_thread_messages_schedules_then_update_carries_the_card(compat_db, glue):
    r = glue.r
    rowid = _add_apple_message(compat_db)

    async def scenario():
        first = await r.thread_messages(C1_GUID)
        msg = next(m for m in first["messages"] if m["rowid"] == rowid)
        assert msg["link"] is None                        # not cached yet: today's card
        assert r._LINK_WAITERS == {APPLE: {rowid}}
        await _drain(r)
        return await r.thread_messages(C1_GUID)

    second = asyncio.run(scenario())
    assert [e["type"] for e in glue.events] == ["update"]
    assert glue.events[0]["data"]["rowid"] == rowid
    _overlaid(glue.events[0]["data"]["link"])
    _overlaid(next(m for m in second["messages"] if m["rowid"] == rowid)["link"])
    assert glue.fake.urls() == [APPLE, PUB, IMAGE_URL]    # one resolve in total


def test_beeper_threads_are_not_enriched(glue, monkeypatch):
    r = glue.r
    asyncio.run(glue.enricher.resolve(APPLE))
    gm = plain_msg(APPLE, None)

    async def fake_fetch_messages(chat_guid, limit):
        return [gm]

    monkeypatch.setattr(r.beeper, "fetch_messages", fake_fetch_messages)
    before = json.dumps(gm)
    out = asyncio.run(r.thread_messages("bp:synthetic-room"))
    assert json.dumps(out["messages"][0]) == before and not r._LINK_WAITERS


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------

def test_unresolved_link_is_scheduled_exactly_once_while_in_flight(compat_db, glue):
    r = glue.r
    one = _add_apple_message(compat_db)
    two = _add_apple_message(compat_db, "g1")

    async def scenario():
        glue.fake.gate = asyncio.Event()                  # hold the resolve in flight
        for _ in range(4):
            await r.thread_messages(C1_GUID)
            await r.thread_messages(G1_GUID)
            r.schedule_link_resolves([(APPLE, one)])
            await asyncio.sleep(0.01)
        assert len(r._LINK_TASKS) == 1 and r._LINK_QUEUE == []
        assert r._LINK_WAITERS == {APPLE: {one, two}}
        assert glue.fake.urls() == [APPLE]                # one fetch sequence, still at step one
        assert glue.events == []
        glue.fake.gate.set()
        await _drain(r)

    asyncio.run(scenario())
    assert glue.fake.urls() == [APPLE, PUB, IMAGE_URL]
    # one update per waiting rowid, each the overlaid message
    assert [e["type"] for e in glue.events] == ["update", "update"]
    assert [e["data"]["rowid"] for e in glue.events] == sorted([one, two])
    for e in glue.events:
        _overlaid(e["data"]["link"])
        assert e["data"]["text"] == APPLE
    assert {e["data"]["chat_guid"] for e in glue.events} == {C1_GUID, G1_GUID}


def test_at_most_two_resolves_at_a_time(glue):
    r = glue.r
    for url in (APPLE2, APPLE3):
        glue.fake.routes[url] = ("text/html", apple_page(None))
    running_peak = 0

    async def scenario():
        nonlocal running_peak
        glue.fake.gate = asyncio.Event()
        r.schedule_link_resolves([(APPLE, None), (APPLE2, None), (APPLE3, None), (APPLE2, None)])
        await asyncio.sleep(0.02)
        running_peak = len(r._LINK_TASKS)
        assert r._LINK_QUEUE == [APPLE3]                  # the third waits for a free slot
        assert sorted(glue.fake.urls()) == sorted([APPLE, APPLE2])
        glue.fake.gate.set()
        await _drain(r)

    asyncio.run(scenario())
    assert running_peak == r.LINK_MAX_RESOLVES == 2
    assert [u for u in glue.fake.urls() if u.startswith("https://apple.news/")].count(APPLE3) == 1
    assert glue.fake.urls().count(APPLE2) == 1
    assert glue.events == []                              # no rowids were waiting


def test_failed_resolve_broadcasts_nothing(compat_db, glue, capsys):
    r = glue.r
    rowid = _add_apple_message(compat_db)
    glue.fake.routes[APPLE] = FetchError("timeout")

    async def scenario():
        await r.thread_messages(C1_GUID)
        await _drain(r)
        # a negative entry that is not due is not scheduled again
        await r.thread_messages(C1_GUID)
        assert not r._LINK_WAITERS and not r._LINK_TASKS

    asyncio.run(scenario())
    assert glue.events == []
    assert glue.fake.urls() == [APPLE]
    assert rowid and glue.enricher.lookup(APPLE) is None
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("[links]")]
    assert len(lines) == 1 and lines[0].endswith("apple.news unresolved (apple.news: timeout)")
    assert "apple.news/" not in out


def test_link_without_publisher_keeps_todays_card(compat_db, glue, capsys):
    r = glue.r
    _add_apple_message(compat_db)
    glue.fake.routes[APPLE] = ("text/html", apple_page(None))

    async def scenario():
        await r.thread_messages(C1_GUID)
        await _drain(r)

    asyncio.run(scenario())
    assert glue.events == []
    assert "apple.news unresolved (no publisher URL)" in capsys.readouterr().out


def test_success_log_line_is_content_free(compat_db, glue, capsys):
    r = glue.r
    _add_apple_message(compat_db, text=f"private words {APPLE}")

    async def scenario():
        await r.thread_messages(C1_GUID)
        await _drain(r)

    asyncio.run(scenario())
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("[links]")]
    assert len(lines) == 1
    assert lines[0].endswith("apple.news -> www.example-news.com (image=yes)")
    assert "private words" not in out and "apple.news/" not in out and "/2026/story" not in out


def test_vanished_row_is_skipped(glue):
    r = glue.r

    async def scenario():
        r.schedule_link_resolves([(APPLE, cf.MISSING_ROWID)])
        await _drain(r)

    r.chatdb_adapter.configure(chatdb_path=str(glue.tmp / "absent.db"), resolve=r.resolve,
                               att_public=r.att_public, person_key=r.person_key,
                               group_title=r.group_title, self_raw=r.SELF_RAW)
    asyncio.run(scenario())                               # the re-read fails: logged, not raised
    assert glue.events == []
    assert glue.enricher.lookup(APPLE) is not None


def test_fetch_message_adapter(compat_db, glue):
    r = glue.r
    rowid = compat_db.messages["text"]
    expected = next(m for m in r.fetch_new(0) if m["rowid"] == rowid)
    assert cf.dumps(r.chatdb_adapter.fetch_message(rowid)) == cf.dumps(expected)
    assert r.chatdb_adapter.fetch_message(cf.MISSING_ROWID) is None


# ---------------------------------------------------------------------------
# poll_loop: the message is not delayed; the card arrives as an update
# ---------------------------------------------------------------------------

def test_poll_loop_broadcasts_message_then_update(compat_db, glue, monkeypatch):
    r = glue.r
    monkeypatch.setattr(r, "load_contacts", lambda: None)
    pushed: list[dict] = []
    monkeypatch.setattr(r, "send_push", lambda msg: pushed.append(copy.deepcopy(msg)))
    r.save_state(last_rowid=r.max_rowid(), last_edit=r.max_date_edited())

    async def scenario():
        glue.fake.gate = asyncio.Event()
        task = asyncio.create_task(r.poll_loop())
        try:
            await asyncio.sleep(0.05)
            rowid = _add_apple_message(compat_db)
            deadline = time.monotonic() + 3
            while not any(e["type"] == "message" for e in glue.events):
                assert not task.done() and time.monotonic() < deadline
                await asyncio.sleep(0.01)
            # delivered and pushed while the resolve is still held in flight
            assert [e["type"] for e in glue.events] == ["message"]
            assert glue.events[0]["data"]["rowid"] == rowid and glue.events[0]["data"]["link"] is None
            while not pushed:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            glue.fake.gate.set()
            while len(glue.events) < 2:
                assert not task.done() and time.monotonic() < deadline
                await asyncio.sleep(0.01)
            return rowid
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    rowid = asyncio.run(scenario())
    assert [(e["type"], e["data"]["rowid"]) for e in glue.events] == [("message", rowid), ("update", rowid)]
    _overlaid(glue.events[1]["data"]["link"])
    assert [p["rowid"] for p in pushed] == [rowid]


# ---------------------------------------------------------------------------
# GET /link_preview_image/{key}
# ---------------------------------------------------------------------------

def test_link_preview_image_serves_cached_file_with_immutable_header(glue, client):
    asyncio.run(glue.enricher.resolve(APPLE))
    resp = client.get(IMAGE_ROUTE, headers=AUTH)
    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert resp.headers["content-type"] == "image/png"
    assert client.get(IMAGE_ROUTE, params={"token": STUB_TOKEN}).status_code == 200


@pytest.mark.parametrize("key", [
    "0" * 32,                       # well-formed, not cached
    "ZZZ", "abc", "g" * 32, "A" * 32, "0" * 65,
    "../index.json", "..%2Findex.json", "%2E%2E%2Findex.json", "index.json", ".index-x.tmp",
    IMAGE_KEY + ".png", IMAGE_KEY + "/..", IMAGE_KEY + "%00",
])
def test_link_preview_image_404_for_unknown_and_malformed_keys(glue, client, key):
    asyncio.run(glue.enricher.resolve(APPLE))             # index.json and one image exist
    assert (glue.tmp / "link_cache" / "index.json").is_file()
    resp = client.get(f"/link_preview_image/{key}", headers=AUTH)
    assert resp.status_code == 404, key
    assert b"resolved_url" not in resp.content


def test_link_preview_image_is_token_protected(glue, client):
    asyncio.run(glue.enricher.resolve(APPLE))
    assert glue.r.IMSG_TOKEN == STUB_TOKEN
    assert client.get(IMAGE_ROUTE).status_code == 401
    assert client.get(IMAGE_ROUTE, headers={"X-Imsg-Token": "wrong"}).status_code == 401
    assert client.get("/link_preview_image/" + "0" * 32).status_code == 401   # no oracle without the token
    assert client.get(IMAGE_ROUTE, headers=AUTH).status_code == 200


# ---------------------------------------------------------------------------
# APPLE_NEWS_PREVIEWS=0
# ---------------------------------------------------------------------------

def test_module_enricher_follows_the_environment(relay_module):
    r = relay_module.module
    if not hasattr(r, "LINK_ENRICHER"):
        pytest.skip(f"{relay_module.name} has no Apple News link glue")
    # the stub environment does not set APPLE_NEWS_PREVIEWS: on by default, cache beside the module
    assert r.LINK_ENRICHER.enabled is True
    assert r.LINK_ENRICHER.cache_dir.name == "link_cache"
    src = open(r.__file__, encoding="utf-8").read()
    assert 'enabled=os.environ.get("APPLE_NEWS_PREVIEWS", "1").strip() != "0"' in src


def test_disabled_feature_neither_overlays_nor_schedules(compat_db, glue, client, monkeypatch):
    r = glue.r
    asyncio.run(glue.enricher.resolve(APPLE))             # even a warm cache is ignored
    calls = len(glue.fake.calls)
    off = Enricher(glue.tmp / "link_cache", enabled=False, fetch=glue.fake)
    monkeypatch.setattr(r, "LINK_ENRICHER", off)
    rowid = _add_apple_message(compat_db)

    msg = plain_msg(APPLE, None)
    before = json.dumps(msg)
    assert r.enrich_links([msg]) == [] and json.dumps(msg) == before

    resp = client.get(f"/thread/{C1_GUID}/messages", headers=AUTH)
    got = next(m for m in resp.json()["messages"] if m["rowid"] == rowid)
    assert got["link"] is None

    async def scenario():
        out = await r.thread_messages(C1_GUID)
        await asyncio.sleep(0.02)
        return out

    asyncio.run(scenario())
    assert not r._LINK_WAITERS and not r._LINK_QUEUE and not r._LINK_TASKS
    assert len(glue.fake.calls) == calls and glue.events == []


# ---------------------------------------------------------------------------
# Regression tests for the review findings
# ---------------------------------------------------------------------------

def test_update_never_overtakes_its_own_message_in_a_batch(compat_db, glue, monkeypatch):
    """A batch of [plain text, apple.news link] with a slow push and an instant
    resolve: the link's "update" must come after its "message"."""
    r = glue.r
    monkeypatch.setattr(r, "load_contacts", lambda: None)
    monkeypatch.setattr(r, "send_push", lambda msg: time.sleep(0.25))
    r.save_state(last_rowid=r.max_rowid(), last_edit=r.max_date_edited())
    first = _add_apple_message(compat_db, text="look at this")
    second = _add_apple_message(compat_db)

    async def scenario():
        task = asyncio.create_task(r.poll_loop())
        try:
            deadline = time.monotonic() + 5
            while len(glue.events) < 3:
                assert not task.done() and time.monotonic() < deadline
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert [(e["type"], e["data"]["rowid"]) for e in glue.events] == [
        ("message", first), ("message", second), ("update", second)]
    assert glue.events[1]["data"]["link"] is None
    _overlaid(glue.events[2]["data"]["link"])


def test_edited_message_update_precedes_the_resolved_update(compat_db, glue, monkeypatch):
    r = glue.r
    monkeypatch.setattr(r, "load_contacts", lambda: None)
    rowid = _add_apple_message(compat_db)
    r.save_state(last_rowid=r.max_rowid(), last_edit=r.max_date_edited())
    order: list[str] = []
    real_schedule, real_broadcast = r.schedule_link_resolves, r.hub.broadcast

    def schedule(need):
        order.append("schedule")
        real_schedule(need)

    async def broadcast(payload):
        order.append(payload["type"])
        await real_broadcast(payload)

    plain = r.fetch_message(rowid) if hasattr(r, "fetch_message") else r.chatdb_adapter.fetch_message(rowid)
    calls = 0

    def fetch_edited(mark):
        nonlocal calls
        calls += 1
        return ([copy.deepcopy(plain)], mark + 1) if calls == 1 else ([], mark)

    monkeypatch.setattr(r, "schedule_link_resolves", schedule)
    monkeypatch.setattr(r.hub, "broadcast", broadcast)
    monkeypatch.setattr(r, "fetch_edited", fetch_edited)

    async def scenario():
        task = asyncio.create_task(r.poll_loop())
        try:
            deadline = time.monotonic() + 5
            while len(glue.events) < 2:
                assert not task.done() and time.monotonic() < deadline
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert order[:3] == ["update", "schedule", "update"]
    assert glue.events[0]["data"]["link"] is None
    _overlaid(glue.events[1]["data"]["link"])


def test_url_queued_while_finished_workers_still_hold_their_slots_is_not_stranded(glue, monkeypatch):
    r = glue.r
    resolved: list[str] = []

    class Instant:
        async def resolve(self, url):
            resolved.append(url)
            return None

        def reason(self, url):
            return "stub"

    monkeypatch.setattr(r, "LINK_ENRICHER", Instant())

    async def scenario():
        loop = asyncio.get_running_loop()
        r.schedule_link_resolves([(APPLE, 1), (APPLE2, 2)])
        # runs after both workers have returned but before their done-callbacks
        loop.call_soon(r.schedule_link_resolves, [(APPLE3, 3)])
        await asyncio.sleep(0.3)
        return list(r._LINK_QUEUE), dict(r._LINK_WAITERS), set(r._LINK_TASKS)

    queue, waiters, tasks = asyncio.run(scenario())
    assert (queue, waiters, tasks) == ([], {}, set())
    assert sorted(resolved) == sorted([APPLE, APPLE2, APPLE3])


def test_fragment_variants_of_one_link_are_scheduled_once(glue):
    r = glue.r
    msgs = [plain_msg(f"{APPLE}#{i}", None) for i in range(5)]
    for i, m in enumerate(msgs):
        m["rowid"] = 9000 + i
    need = r.enrich_links(msgs)
    assert {u for u, _ in need} == {APPLE}

    async def scenario():
        r.schedule_link_resolves(need)
        assert list(r._LINK_WAITERS) == [APPLE] and r._LINK_QUEUE == []
        await _drain(r)

    asyncio.run(scenario())
    assert glue.fake.urls().count(APPLE) == 1 and len(glue.fake.calls) == 3
