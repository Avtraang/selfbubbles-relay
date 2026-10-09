"""Google Messages: no text is lost around a dropped connection, a restart or a thread-list request.

Audit findings R3-F2, R4-F2 and R2-F7 (2026-10-08), each reproduced here before
the fix:

* R3-F2: every listing of the Beeper chats moved the watcher's "newest message
  I know of" mark. The app asks for the thread list all the time, and the
  watcher itself lists when it meets a chat it has not seen. A text whose chat
  was listed before its event was read was then taken for history: no
  notification, and a frame the app's open conversation ignores. The first
  text of a new conversation was always lost this way.
* R4-F2: a text that arrived while the relay's connection to Beeper was down
  (a dropped socket, a restart of the relay) was never announced. Nothing read
  what had been missed, and the listing after the reconnect moved the mark
  past it.
* R2-F7: a session that Beeper ended cleanly was followed by a new connection
  at once, with no pause.

What stays as it was (the rules of 2026-10-02/03, "K12"): a message replayed
after a reconnect, the newest message of a chat sent again, history filled in
for an old chat, and a `message.updated` are never announced.

Nothing talks to Beeper: its REST answers and its WebSocket are scripted.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from datetime import datetime, timezone

import pytest

ACCOUNT = "sh-gmessages_synthetic/1"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class Stop(BaseException):
    """Ends watch(), which catches Exception only."""


class Drop(Exception):
    """A dropped socket."""


def chat(room: str, local: str, last: float, kind: str = "single", title: str = "Stand In", unread=None) -> dict:
    """`unread` is Beeper's unreadCount for the chat; None leaves the field out, as if Beeper did not say."""
    c = {"id": room, "localChatID": local, "accountID": ACCOUNT, "title": title, "type": kind,
         "lastActivity": iso(last), "participants": {"items": [{"fullName": title}]}}
    if unread is not None:
        c["unreadCount"] = unread
    return c


def message(room: str, mid: str, ts: float, me: bool = False) -> dict:
    return {"id": mid, "chatID": room, "accountID": ACCOUNT, "timestamp": iso(ts),
            "text": f"synthetic {mid}", "isSender": me, "senderName": "" if me else "Stand In"}


def upsert(*msgs: dict) -> dict:
    return {"type": "message.upserted", "chatID": msgs[0]["chatID"], "entries": list(msgs)}


def updated(msg: dict) -> dict:
    return {"type": "message.updated", "chatID": msg["chatID"], "entries": [msg]}


@pytest.fixture
def world(relay_module, monkeypatch):
    """Beeper as a script: `chats` is its chat list, `messages[local id]` what it holds per chat."""
    bp = relay_module.module.beeper
    w = types.SimpleNamespace(bp=bp, chats=[], messages={}, rest=[], sleeps=[], fail_reads=set(), fail_listing=False)

    async def fake_get(client, path, **params):
        w.rest.append(path)
        if path == "/v1/chats":
            if w.fail_listing:
                raise RuntimeError("synthetic: Beeper does not answer")
            return {"items": list(w.chats)}
        local = path.split("/")[3]
        if local in w.fail_reads:
            raise RuntimeError("synthetic: Beeper does not answer")
        held = sorted(w.messages.get(local, []), key=lambda m: m["timestamp"], reverse=True)
        return {"items": held[: params.get("limit", 50)]}             # newest first, as Beeper answers

    async def no_portal_types():
        return None

    monkeypatch.setattr(bp, "_enabled", True)
    monkeypatch.setattr(bp, "_get", fake_get)
    monkeypatch.setattr(bp, "refresh_portal_types", no_portal_types)
    for name in ("_chat_last", "_chatid_to_local", "_seen_msg_ids", "_chat_meta"):
        monkeypatch.setattr(bp, name, {}, raising=False)
    monkeypatch.setattr(bp, "_kind_asked", set(), raising=False)
    for name, value in (("_seeded", False), ("_mark", 0.0)):          # a freshly started process
        monkeypatch.setattr(bp, name, value, raising=False)

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds, *a, **k):                           # pauses are recorded, not waited for
        w.sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(bp.asyncio, "sleep", fast_sleep)
    return w


class FakeWS:
    def __init__(self, frames, how_it_ends):
        self._frames, self._end = list(frames), how_it_ends

    async def send(self, _):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            if self._end == "stop":
                raise Stop()
            if self._end == "close":
                raise StopAsyncIteration                               # Beeper ended the session cleanly
            raise Drop("synthetic drop")
        frame = self._frames.pop(0)
        if callable(frame):                                            # something that happens at this point
            frame = frame()
            if asyncio.iscoroutine(frame):
                frame = await frame
            if frame is None:
                return json.dumps({"type": "noop"})
        return json.dumps(frame)


def run(world, monkeypatch, sessions, before=None, ends=None, on_message=None, restart=False, since=None):
    """Run watch() over scripted sessions (a list of frames each). `before[n]` runs just before
    connection n; `ends[n]` is how session n ends ("drop" by default, the last one stops the test).
    `restart=True` starts the watcher the way a restarted relay does: with the mark the previous run
    saved (`since`, None on a first run ever) and a callback that is told every new mark."""
    before, ends = before or {}, ends or {}
    got, marks = [], []

    async def collect(d, is_new):
        got.append((d["chat_guid"], d["guid"], is_new, d["is_from_me"]))

    state = types.SimpleNamespace(connects=0)

    def connect(url, **kw):
        state.connects += 1
        n = state.connects

        class CM:
            async def __aenter__(self):
                if n > len(sessions):
                    raise Stop()
                if n in before:
                    before[n]()
                return FakeWS(sessions[n - 1], ends.get(n, "stop" if n == len(sessions) else "drop"))

            async def __aexit__(self, *exc):
                return False

        return CM()

    mod = types.ModuleType("websockets")
    mod.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", mod)

    async def main():
        try:
            await world.bp.watch(on_message or collect, **({"since": since, "on_mark": marks.append} if restart else {}))
        except Stop:
            pass

    asyncio.run(main())
    return types.SimpleNamespace(got=got, marks=marks, connects=state.connects)


def news(result) -> list[str]:
    return [mid for _, mid, is_new, _ in result.got if is_new]


# ---------------------------------------------------------------------------
# R3-F2: a listing is not the watcher having seen a message
# ---------------------------------------------------------------------------

def test_the_first_text_of_a_new_conversation_is_announced(world, monkeypatch):
    """The watcher meets an unknown chat and lists to learn it; the listing already shows the text."""
    now = time.time()
    world.chats = [chat("!old:b", "1", now - 3600)]

    def arrives():
        world.chats.append(chat("!new:b", "2", now - 1))
        return upsert(message("!new:b", "m-first", now - 1))

    out = run(world, monkeypatch, [[arrives]])
    assert out.got == [("bp:2", "m-first", True, False)]


def test_a_thread_list_request_between_two_texts_does_not_hide_the_second(world, monkeypatch):
    """The app answers every new message by asking for the thread list (GET /threads)."""
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]

    async def the_app_lists_threads():
        world.chats = [chat("!a:b", "1", now - 1)]                     # Beeper already holds the second text
        await world.bp.fetch_threads(200)

    out = run(world, monkeypatch, [[upsert(message("!a:b", "m1", now - 2)), the_app_lists_threads,
                                    upsert(message("!a:b", "m2", now - 1))]])
    assert news(out) == ["m1", "m2"]


def test_a_text_in_another_chat_is_not_hidden_by_a_thread_list_request_either(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600), chat("!c:d", "2", now - 7200)]

    async def the_app_lists_threads():
        world.chats = [chat("!a:b", "1", now - 2), chat("!c:d", "2", now - 1)]
        await world.bp.fetch_threads(200)

    out = run(world, monkeypatch, [[upsert(message("!a:b", "m1", now - 2)), the_app_lists_threads,
                                    upsert(message("!c:d", "m2", now - 1))]])
    assert news(out) == ["m1", "m2"]


# ---------------------------------------------------------------------------
# What must stay history (K12)
# ---------------------------------------------------------------------------

def test_history_is_still_not_news(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 60)]
    frames = [
        upsert(message("!a:b", "newest-again", now - 60)),             # the chat's newest message, sent again
        upsert(message("!a:b", "older", now - 86400)),                 # backfill
        upsert(message("!dormant:b", "months-old", now - 90 * 86400)),  # a chat outside the listing
        updated(message("!a:b", "receipt", now - 5)),                  # a read receipt
        upsert(message("!a:b", "live", now - 1)),
        upsert(message("!a:b", "live", now - 1)),                      # the same event twice
    ]
    out = run(world, monkeypatch, [frames])
    assert news(out) == ["live"]
    assert len(out.got) == 6                                           # the rest is passed on as updates


def test_a_replay_after_a_reconnect_is_not_announced_twice(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    m = message("!a:b", "m1", now - 5)

    def beeper_has_it():
        world.chats = [chat("!a:b", "1", now - 5)]
        world.messages["1"] = [m]

    out = run(world, monkeypatch, [[upsert(m)], [upsert(m)]], before={2: beeper_has_it})
    assert news(out) == ["m1"]


# ---------------------------------------------------------------------------
# R4-F2: what arrived while nobody was listening
# ---------------------------------------------------------------------------

def test_a_text_that_arrived_while_the_socket_was_down_is_announced_after_the_reconnect(world, monkeypatch):
    """Beeper does not send the missed event again: the watcher has to read what it missed."""
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]

    def gap():
        world.chats = [chat("!a:b", "1", now - 2, unread=1)]
        world.messages["1"] = [message("!a:b", "m-gap", now - 2)]

    out = run(world, monkeypatch, [[], []], before={2: gap})
    assert out.got == [("bp:1", "m-gap", True, False)]


def test_the_missed_text_is_announced_once_when_beeper_also_replays_it(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    m = message("!a:b", "m-gap", now - 2)

    def gap():
        world.chats = [chat("!a:b", "1", now - 2, unread=1)]
        world.messages["1"] = [m]

    out = run(world, monkeypatch, [[], [upsert(m)]], before={2: gap})
    assert news(out) == ["m-gap"]
    assert [g[2] for g in out.got] == [True, False]


def test_everything_missed_in_one_outage_is_announced_oldest_first(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600), chat("!c:d", "2", now - 7200)]

    def gap():
        world.chats = [chat("!a:b", "1", now - 10, unread=2), chat("!c:d", "2", now - 20, unread=0),
                       chat("!n:e", "3", now - 30, unread=1)]
        world.messages = {"1": [message("!a:b", "g1", now - 40), message("!a:b", "mine", now - 25, me=True),
                                message("!a:b", "g4", now - 10), message("!a:b", "before", now - 3600)],
                          "2": [message("!c:d", "only-mine", now - 20, me=True)],   # sent from the phone: nothing unread there
                          "3": [message("!n:e", "g2", now - 30)]}                   # a conversation that began meanwhile

    out = run(world, monkeypatch, [[], []], before={2: gap})
    # In the unread chats everything missed is passed on in order, my own text included (it is no notification).
    assert [(g[1], g[3]) for g in out.got] == [("g1", False), ("g2", False), ("mine", True), ("g4", False)]
    assert all(g[2] for g in out.got)


def test_a_missed_text_older_than_the_age_limit_is_not_announced(world, monkeypatch):
    """The documented limit: a message that surfaces more than six hours late is shown, not announced."""
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 30 * 3600)]

    def gap():
        world.chats = [chat("!a:b", "1", now - 7 * 3600, unread=1)]
        world.messages["1"] = [message("!a:b", "late", now - 7 * 3600)]

    out = run(world, monkeypatch, [[], []], before={2: gap})
    assert news(out) == []


def test_only_chats_with_news_are_read_after_a_reconnect(world, monkeypatch):
    now = time.time()
    world.chats = [chat(f"!r{i}:b", str(i), now - 3600 - i) for i in range(1, 61)]

    def gap():
        world.chats[6] = chat("!r7:b", "7", now - 3, unread=1)
        world.messages["7"] = [message("!r7:b", "m-gap", now - 3)]

    out = run(world, monkeypatch, [[], [], []], before={2: gap})
    reads = [p for p in world.rest if p.endswith("/messages")]
    assert news(out) == ["m-gap"]
    assert reads == ["/v1/chats/7/messages"]                           # one chat had news, once


def test_a_reconnect_with_nothing_missed_reads_no_chat_and_announces_nothing(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600), chat("!c:d", "2", now - 60)]
    out = run(world, monkeypatch, [[], [], []])
    assert out.got == [] and not [p for p in world.rest if p.endswith("/messages")]


def test_a_read_that_fails_does_not_lose_the_text_if_beeper_sends_it_again(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    m = message("!a:b", "m-gap", now - 2)

    def gap():
        world.chats = [chat("!a:b", "1", now - 2, unread=1)]
        world.messages["1"] = [m]
        world.fail_reads.add("1")

    out = run(world, monkeypatch, [[], [upsert(m)]], before={2: gap})
    assert news(out) == ["m-gap"]


def test_a_listing_that_fails_after_a_reconnect_is_tried_again_at_the_next_one(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]

    def gap_and_no_answer():
        world.chats = [chat("!a:b", "1", now - 2, unread=1)]
        world.messages["1"] = [message("!a:b", "m-gap", now - 2)]
        world.fail_listing = True

    def answers_again():
        world.fail_listing = False

    out = run(world, monkeypatch, [[], [], []], before={2: gap_and_no_answer, 3: answers_again})
    assert news(out) == ["m-gap"]


# ---------------------------------------------------------------------------
# R4-F2 across a restart of the relay: the mark it saved
# ---------------------------------------------------------------------------

def test_a_text_that_arrived_while_the_relay_was_restarting_is_announced(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3, unread=1), chat("!c:d", "2", now - 500, unread=4)]
    world.messages = {"1": [message("!a:b", "m-gap", now - 3), message("!a:b", "seen-before", now - 400)],
                      "2": [message("!c:d", "also-seen", now - 500)]}
    out = run(world, monkeypatch, [[]], restart=True, since=now - 60)            # the previous run had seen up to a minute ago
    assert out.got == [("bp:1", "m-gap", True, False)]
    assert out.marks and out.marks[-1] == pytest.approx(now - 3, abs=0.01)


def test_a_restart_announces_nothing_the_previous_run_had_already_seen(world, monkeypatch):
    now = time.time()
    m = message("!a:b", "m-last", now - 3)
    world.chats = [chat("!a:b", "1", now - 3)]
    world.messages = {"1": [m]}
    seen = world.bp._ts_to_unix(iso(now - 3))                          # exactly the last message it announced
    out = run(world, monkeypatch, [[upsert(m)]], restart=True, since=seen)
    assert news(out) == []
    assert not [p for p in world.rest if p.endswith("/messages")]


def test_a_first_run_with_no_saved_mark_takes_what_is_listed_for_history(world, monkeypatch):
    """Nothing to compare with: announcing everything Beeper holds would be the 2026-10-02 bug."""
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3)]
    world.messages = {"1": [message("!a:b", "already-there", now - 3)]}
    out = run(world, monkeypatch, [[]], restart=True, since=None)
    assert out.got == []
    assert out.marks and out.marks[-1] == pytest.approx(now - 3, abs=0.01)   # and from now on there is a mark


def test_the_mark_follows_what_was_announced(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    out = run(world, monkeypatch, [[upsert(message("!a:b", "m1", now - 2)), upsert(message("!a:b", "m2", now - 1))]],
                  restart=True, since=now - 3600)
    assert news(out) == ["m1", "m2"]
    assert out.marks == sorted(out.marks) and out.marks[-1] == pytest.approx(now - 1, abs=0.01)
    assert world.bp.seen_until() == pytest.approx(now - 1, abs=0.01)


# ---------------------------------------------------------------------------
# R2-F7 and the loop itself
# ---------------------------------------------------------------------------

def test_a_session_that_beeper_ends_cleanly_is_followed_by_a_pause(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    out = run(world, monkeypatch, [[], [], [], []], ends={1: "close", 2: "close", 3: "close"})
    assert out.connects == 4
    assert len(world.sleeps) == 3 and all(s >= 1 for s in world.sleeps)


def test_a_dropped_session_is_followed_by_a_pause_too(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    out = run(world, monkeypatch, [[], [], []])
    assert out.connects == 3 and len(world.sleeps) == 2


def test_one_message_the_relay_cannot_handle_does_not_end_the_session(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    got = []

    async def on_message(d, is_new):
        if d["guid"] == "m1":
            raise RuntimeError("synthetic: the relay could not pass this one on")
        got.append((d["guid"], is_new))

    out = run(world, monkeypatch, [[upsert(message("!a:b", "m1", now - 2)), upsert(message("!a:b", "m2", now - 1))]],
              on_message=on_message)
    assert got == [("m2", True)] and out.connects == 1 and world.sleeps == []


# ---------------------------------------------------------------------------
# The relay's side: it keeps the mark across its own restarts
# ---------------------------------------------------------------------------

from tests.test_relay_compat import relay_state  # noqa: E402,F401  (fixture: the state file is put back after each test)


@pytest.fixture
def r(relay_module, monkeypatch):
    mod = relay_module.module
    monkeypatch.setattr(mod, "_BEEPER_MARK", {"want": 0.0, "saved": 0.0, "task": None}, raising=False)
    return mod


def test_the_relay_hands_the_watcher_the_mark_it_saved(r, monkeypatch):
    seen = {}

    async def fake_watch(on_message, since=None, on_mark=None):
        seen.update(on_message=on_message, since=since, on_mark=on_mark)

    monkeypatch.setattr(r.beeper, "watch", fake_watch)
    r.save_state(beeper_seen=1759900000.25)
    asyncio.run(r._watch_beeper())
    assert seen == {"on_message": r._on_beeper_message, "since": 1759900000.25, "on_mark": r._note_beeper_mark}


@pytest.mark.parametrize("stored", [None, "yesterday", -5, 0, True, [1.0]])
def test_a_missing_or_meaningless_saved_mark_means_a_first_run(r, stored):
    r.save_state(beeper_seen=stored)
    assert r._beeper_since() is None


def test_a_state_file_that_cannot_be_read_means_a_first_run_not_a_crash(r, monkeypatch):
    def unreadable():
        raise OSError("synthetic")

    monkeypatch.setattr(r, "load_state", unreadable)
    assert r._beeper_since() is None


def test_a_new_mark_is_saved_behind_the_watcher_and_the_newest_wins(r):
    async def scenario():
        for when in (100.0, 300.0, 200.0):          # the call returns at once; nothing is awaited here
            r._note_beeper_mark(when)
        await r._BEEPER_MARK["task"]

    asyncio.run(scenario())
    assert r.load_state()["beeper_seen"] == 300.0


def test_a_mark_that_cannot_be_saved_stops_nothing_and_the_next_one_is_saved(r, monkeypatch):
    real, fail = r.save_state, {"on": True}

    def flaky(**fields):
        if fail["on"]:
            raise OSError("synthetic: disk full")
        return real(**fields)

    monkeypatch.setattr(r, "save_state", flaky)

    async def scenario():
        r._note_beeper_mark(100.0)
        await r._BEEPER_MARK["task"]                 # the failure is logged, not raised
        fail["on"] = False
        r._note_beeper_mark(150.0)
        await r._BEEPER_MARK["task"]

    asyncio.run(scenario())
    assert r.load_state()["beeper_seen"] == 150.0


def test_a_chat_listed_without_an_activity_time_keeps_the_fifteen_minute_rule(world, monkeypatch):
    """A conversation Beeper has just created is listed without a time. It must not get a mark of
    zero: then anything up to six hours old that Beeper fills in would be announced."""
    now = time.time()
    empty = chat("!new:b", "9", now)
    empty["lastActivity"] = None
    world.chats = [chat("!a:b", "1", now - 3600), empty]
    out = run(world, monkeypatch, [[upsert(message("!new:b", "filled-in", now - 2 * 3600)),
                                    upsert(message("!new:b", "live", now - 1))]])
    assert news(out) == ["live"]


# ---------------------------------------------------------------------------
# Only what is still unread is announced (review of 2026-10-08)
# ---------------------------------------------------------------------------
# After a long gap (the Mac asleep, Beeper closed) the catch-up used to announce
# up to six hours of texts at once, also those read on the phone long before.
# Beeper's chat list says how many messages of a chat are unread: only those
# are announced, and where it does not say, nothing is.

def _one_outage(world, monkeypatch, gap, replay=()):
    """A first session that drops, `gap()` while the socket is down, and a second session with `replay`."""
    return run(world, monkeypatch, [[], list(replay)], before={2: gap})


def test_a_missed_text_in_a_chat_already_read_elsewhere_is_not_announced(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    m = message("!a:b", "m-gap", now - 60)

    def gap():
        world.chats = [chat("!a:b", "1", now - 60, unread=0)]       # read on the phone meanwhile
        world.messages["1"] = [m]

    out = _one_outage(world, monkeypatch, gap, replay=[upsert(m)])
    assert news(out) == []
    assert out.got == [("bp:1", "m-gap", False, False)]            # and Beeper sending it again does not announce it either
    assert not [p for p in world.rest if p.endswith("/messages")]  # a read chat is not even read


@pytest.mark.parametrize("unread", [None, "3", True, -1, 2.5, [1]])
def test_where_beeper_does_not_say_what_is_unread_nothing_is_announced(world, monkeypatch, capsys, unread):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    m = message("!a:b", "m-gap", now - 60)

    def gap():
        c = chat("!a:b", "1", now - 60)
        if unread is not None:
            c["unreadCount"] = unread
        world.chats = [c]
        world.messages["1"] = [m]

    out = _one_outage(world, monkeypatch, gap, replay=[upsert(m)])
    assert news(out) == []
    assert "without an unread count" in capsys.readouterr().out     # said in the log, once per connect


def test_no_more_are_announced_than_are_unread_and_those_are_the_newest(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3600)]
    missed = [message("!a:b", f"m{i}", now - 60 + i) for i in range(1, 6)]

    def gap():
        world.chats = [chat("!a:b", "1", now - 55, unread=2)]       # three of the five were read on the phone
        world.messages["1"] = list(missed)

    out = _one_outage(world, monkeypatch, gap, replay=[upsert(m) for m in missed])
    assert news(out) == ["m4", "m5"]
    assert [g[1] for g in out.got if not g[2]] == ["m1", "m2", "m3", "m4", "m5"]    # the replay announces none of them


def test_an_unread_count_that_reaches_back_before_the_gap_announces_only_what_was_missed(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 600, unread=2)]          # two texts unread already, announced in their time
    world.messages["1"] = [message("!a:b", "old1", now - 700), message("!a:b", "old2", now - 600)]

    def gap():
        world.chats = [chat("!a:b", "1", now - 30, unread=3)]
        world.messages["1"].append(message("!a:b", "m-gap", now - 30))

    out = _one_outage(world, monkeypatch, gap)
    assert news(out) == ["m-gap"]


def test_a_wrong_unread_count_cannot_become_a_burst(world, monkeypatch, capsys):
    """However many chats Beeper calls unread: one connect announces at most CATCH_UP_ANNOUNCE_MAX, the newest."""
    now = time.time()
    limit = world.bp.CATCH_UP_ANNOUNCE_MAX
    n = limit + 4
    monkeypatch.setattr(world.bp, "CATCH_UP_CHATS", n + 10)
    world.chats = [chat(f"!r{i}:b", str(i), now - 3600 - i) for i in range(1, n + 1)]
    missed = {i: message(f"!r{i}:b", f"m{i}", now - 100 + i) for i in range(1, n + 1)}

    def gap():
        world.chats = [chat(f"!r{i}:b", str(i), now - 100 + i, unread=500) for i in range(1, n + 1)]
        world.messages = {str(i): [missed[i]] for i in range(1, n + 1)}

    out = _one_outage(world, monkeypatch, gap, replay=[upsert(missed[i]) for i in range(1, n + 1)])
    assert news(out) == [f"m{i}" for i in range(5, n + 1)]          # the newest `limit`; the four oldest gave way
    assert len(news(out)) == limit
    assert "over the limit" in capsys.readouterr().out


def test_after_a_restart_too_only_what_is_unread_is_announced(world, monkeypatch):
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 3, unread=0), chat("!c:d", "2", now - 4, unread=1)]
    world.messages = {"1": [message("!a:b", "read-already", now - 3)], "2": [message("!c:d", "unread", now - 4)]}
    out = run(world, monkeypatch, [[]], restart=True, since=now - 60)
    assert news(out) == ["unread"]


# ---------------------------------------------------------------------------
# A saved mark that is ahead of this Mac's clock (review of 2026-10-08)
# ---------------------------------------------------------------------------
# Google Messages times come from the phone. Its clock was seen three seconds
# ahead of the Mac's, so the mark saved for the last text can lie in the Mac's
# future. The mark used to be cut down to "now" at start: a restart quicker
# than that lead then took the last text for one that arrived meanwhile and
# announced it again.

def test_a_restart_inside_the_phones_clock_lead_does_not_announce_the_last_text_again(world, monkeypatch):
    now = time.time()
    last = world.bp._ts_to_unix(iso(now + 3))                      # announced by the previous run, dated by the phone
    world.chats = [chat("!a:b", "1", now + 3, unread=1)]            # still unread
    world.messages = {"1": [message("!a:b", "m-last", now + 3)]}
    out = run(world, monkeypatch, [[upsert(message("!a:b", "m-last", now + 3))]], restart=True, since=last)
    assert news(out) == []
    assert not [p for p in world.rest if p.endswith("/messages")]


def test_a_text_that_arrived_after_a_mark_ahead_of_the_clock_is_still_announced(world, monkeypatch):
    now = time.time()
    last = world.bp._ts_to_unix(iso(now + 3))
    world.chats = [chat("!a:b", "1", now + 4, unread=2)]            # one more text, a second later by the same clock
    world.messages = {"1": [message("!a:b", "m-last", now + 3), message("!a:b", "m-gap", now + 4)]}
    out = run(world, monkeypatch, [[]], restart=True, since=last)
    assert news(out) == ["m-gap"]


def test_a_saved_mark_far_in_the_future_is_not_trusted(world, monkeypatch, capsys):
    """A mark an hour ahead is not a clock that runs fast. Trusted, it would hide everything until then."""
    now = time.time()
    world.chats = [chat("!a:b", "1", now - 120)]
    out = run(world, monkeypatch, [[upsert(message("!a:b", "live", now - 1))]], restart=True, since=now + 3600)
    assert news(out) == ["live"]
    assert out.marks and all(m < now + 60 for m in out.marks)       # and the mark that is saved from here on is sane
    assert "ahead of this Mac's clock" in capsys.readouterr().out
