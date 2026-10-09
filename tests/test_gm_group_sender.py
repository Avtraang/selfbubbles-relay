"""Google Messages: in a group, each message says it belongs to a group.

Seen on the owner's phone on 2026-10-09: in a Google Messages group of three
or more, nothing showed who had written a message. The app puts the sender's
name above a bubble only for a message marked `is_group`, and the relay marked
every Google Messages message as not in a group: the three places that build
such a message each passed a fixed False, whatever Beeper's chat listing said
about the chat. The thread list had it right, so the header showed the group.

A one-to-one chat stays unmarked: a name above every bubble of a chat with one
person would be noise.

Nothing talks to Beeper: its REST answers and its WebSocket are scripted (the
script is the one of test_gm_watch_recovery).
"""

from __future__ import annotations

import asyncio

from tests.test_gm_watch_recovery import chat, iso, message, run, upsert, world  # noqa: F401  (world is a fixture)

NOW = 1_800_000_000.0
ADA, BEN = "Ada Stand-In", "Ben Stand-In"


def group(room: str, local: str, last: float, names=(ADA, BEN)) -> dict:
    c = chat(room, local, last, kind="group", title="")
    c["participants"] = {"items": [{"fullName": n} for n in names] + [{"fullName": "Owner Stand-In", "isSelf": True}]}
    return c


def said(room: str, mid: str, ts: float, who: str) -> dict:
    m = message(room, mid, ts)
    m["senderName"], m["senderID"] = who, f"@{who.split()[0].lower()}:synthetic"
    return m


def a_group_with_three_messages(world) -> None:
    world.chats = [group("!g:synthetic", "G1", NOW)]
    world.messages["G1"] = [said("!g:synthetic", "m1", NOW - 30, ADA), said("!g:synthetic", "m2", NOW - 20, BEN),
                            message("!g:synthetic", "m3", NOW - 10, me=True)]


def test_in_a_group_every_message_is_marked_and_names_its_sender(world):
    a_group_with_three_messages(world)
    asyncio.run(world.bp.fetch_threads())                             # the app lists its chats, then opens one
    msgs = asyncio.run(world.bp.fetch_messages("bp:G1"))
    assert [m["guid"] for m in msgs] == ["m1", "m2", "m3"]
    assert [m["is_group"] for m in msgs] == [True, True, True]
    assert [m["sender"] for m in msgs] == [ADA, BEN, ""]              # the owner's own message names nobody


def test_a_one_to_one_chat_is_not_marked_as_a_group(world):
    world.chats = [chat("!s:synthetic", "S1", NOW)]
    world.messages["S1"] = [message("!s:synthetic", "m1", NOW - 10), message("!s:synthetic", "m2", NOW - 5, me=True)]
    asyncio.run(world.bp.fetch_threads())
    assert [m["is_group"] for m in asyncio.run(world.bp.fetch_messages("bp:S1"))] == [False, False]


def test_a_group_opened_before_any_chat_listing_is_still_a_group(world):
    """A relay that has just started has listed nothing: the chat's kind is asked for, once."""
    a_group_with_three_messages(world)
    msgs = asyncio.run(world.bp.fetch_messages("bp:G1"))
    assert [m["is_group"] for m in msgs] == [True, True, True]
    assert world.rest.count("/v1/chats") == 1
    asyncio.run(world.bp.fetch_messages("bp:G1"))                     # known now: no second listing
    assert world.rest.count("/v1/chats") == 1


def test_a_chat_the_listing_does_not_hold_is_asked_about_once_not_at_every_read(world):
    world.messages["X9"] = [message("!x:synthetic", "m1", NOW - 10)]
    for _ in range(3):
        assert [m["is_group"] for m in asyncio.run(world.bp.fetch_messages("bp:X9"))] == [False]
    assert world.rest.count("/v1/chats") == 1


def test_a_listing_that_fails_does_not_keep_the_messages_from_being_read(world):
    a_group_with_three_messages(world)
    world.fail_listing = True
    msgs = asyncio.run(world.bp.fetch_messages("bp:G1"))
    assert [m["guid"] for m in msgs] == ["m1", "m2", "m3"]
    assert [m["is_group"] for m in msgs] == [False, False, False]     # not known: as before the fix
    world.fail_listing = False
    asyncio.run(world.bp.fetch_threads())                             # any later listing settles it
    assert [m["is_group"] for m in asyncio.run(world.bp.fetch_messages("bp:G1"))] == [True, True, True]


def test_a_message_that_arrives_live_in_a_group_is_marked_too(world, monkeypatch):
    world.chats = [group("!g:synthetic", "G1", NOW - 60)]
    arriving = said("!g:synthetic", "m9", NOW, BEN)
    handed = []

    async def keep(d, is_new):
        handed.append(d)

    run(world, monkeypatch, [[upsert(arriving)]], on_message=keep)
    assert [(d["guid"], d["is_group"], d["sender"]) for d in handed] == [("m9", True, BEN)]


def test_a_message_that_arrives_live_in_a_one_to_one_chat_is_not_marked(world, monkeypatch):
    world.chats = [chat("!s:synthetic", "S1", NOW - 60)]
    handed = []

    async def keep(d, is_new):
        handed.append(d)

    run(world, monkeypatch, [[upsert(message("!s:synthetic", "m9", NOW))]], on_message=keep)
    assert [(d["guid"], d["is_group"]) for d in handed] == [("m9", False)]


def test_what_is_read_after_a_reconnect_is_marked_the_same_way(world):
    a_group_with_three_messages(world)
    world.chats.append(chat("!s:synthetic", "S1", NOW))
    world.messages["S1"] = [message("!s:synthetic", "n1", NOW - 10)]
    asyncio.run(world.bp.fetch_threads())
    assert [m["is_group"] for m in asyncio.run(world.bp._recent("bp:G1", 10))] == [True, True, True]
    assert [m["is_group"] for m in asyncio.run(world.bp._recent("bp:S1", 10))] == [False]
