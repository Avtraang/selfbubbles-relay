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
import time

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


# ---------------------------------------------------------------------------
# what a group is listed as
# ---------------------------------------------------------------------------
# Seen on the owner's data on 2026-10-08: six groups that have no name of their
# own were listed under the name of one member, exactly as a one-to-one chat
# with that member is, while their notifications already showed the members.

def listed(world) -> dict:
    return {t["chat_guid"]: t["chat_name"] for t in asyncio.run(world.bp.fetch_threads())}


def test_a_group_without_a_name_is_listed_under_its_members_not_one_of_them(world):
    world.chats = [group("!g:synthetic", "G1", NOW)]
    assert listed(world) == {"bp:G1": f"{ADA}, {BEN}"}


def test_a_group_with_a_name_keeps_it(world):
    c = group("!g:synthetic", "G1", NOW)
    c["title"] = "Synthetic Committee"
    world.chats = [c]
    assert listed(world) == {"bp:G1": "Synthetic Committee"}


def test_a_large_group_without_a_name_lists_four_members_and_counts_the_rest(world):
    world.chats = [group("!g:synthetic", "G1", NOW, names=[f"Member {n}" for n in "ABCDEF"])]
    assert listed(world) == {"bp:G1": "Member A, Member B, Member C, Member D +2"}


def test_a_group_whose_members_have_no_names_is_a_group_chat(world):
    c = group("!g:synthetic", "G1", NOW)
    c["participants"] = {"items": [{}, {}, {"isSelf": True}]}
    world.chats = [c]
    assert listed(world) == {"bp:G1": "Group chat"}


def test_a_one_to_one_chat_is_still_listed_under_the_persons_name(world):
    world.chats = [chat("!s:synthetic", "S1", NOW, title="Stand In")]
    assert listed(world) == {"bp:S1": "Stand In"}


def test_the_list_and_the_notification_use_the_same_name_for_a_group(world):
    world.chats = [group("!g:synthetic", "G1", NOW)]
    assert listed(world)["bp:G1"] == world.bp.chat_meta("bp:G1")[0]


# ---------------------------------------------------------------------------
# asking the listing what kind of chat this is: shared, bounded, tried again
# ---------------------------------------------------------------------------
# Independent review of 2026-10-09, two findings in the lookup above, each
# reproduced here first. A second read of the same unlisted group that arrived
# while the first was still asking was told "not a group", and its page was the
# one the app kept. And the read waited for the listing however long Beeper
# took (up to 15 s, longer than the app waits for a page), then never asked
# again once a listing had failed.

def gated_listing(world, monkeypatch) -> asyncio.Event:
    """Beeper's chat listing answers only once the returned event is set. Call inside the running loop."""
    gate, real = asyncio.Event(), world.bp._get

    async def get(client, path, **params):
        if path == "/v1/chats":
            await gate.wait()
        return await real(client, path, **params)

    monkeypatch.setattr(world.bp, "_get", get)
    return gate


async def settle(world) -> None:
    for _ in range(25):
        await asyncio.sleep(0)
    task = world.bp._kind_listing
    if task is not None:
        await task


def test_two_reads_of_an_unlisted_group_at_the_same_moment_are_both_marked(world, monkeypatch):
    a_group_with_three_messages(world)

    async def main():
        gate = gated_listing(world, monkeypatch)
        first = asyncio.create_task(world.bp.fetch_messages("bp:G1"))
        second = asyncio.create_task(world.bp.fetch_messages("bp:G1"))
        for _ in range(25):
            await asyncio.sleep(0)                                    # both wait for the listing now
        gate.set()
        return await first, await second

    one, two = asyncio.run(main())
    assert [m["is_group"] for m in one] == [True, True, True]
    assert [m["is_group"] for m in two] == [True, True, True]
    assert world.rest.count("/v1/chats") == 1                         # one listing served both


def test_a_listing_that_is_slow_does_not_hold_the_messages_back(world, monkeypatch):
    a_group_with_three_messages(world)
    monkeypatch.setattr(world.bp, "KIND_LOOKUP_SECONDS", 0.05)

    async def main():
        gate = gated_listing(world, monkeypatch)
        started = time.monotonic()
        early = await world.bp.fetch_messages("bp:G1")
        waited = time.monotonic() - started
        gate.set()                                                    # the listing lands behind the read
        await settle(world)
        return early, waited, await world.bp.fetch_messages("bp:G1")

    early, waited, later = asyncio.run(main())
    assert [m["guid"] for m in early] == ["m1", "m2", "m3"]
    assert [m["is_group"] for m in early] == [False, False, False]    # not known yet: as a one-to-one chat's
    assert waited < 1.0
    assert [m["is_group"] for m in later] == [True, True, True]       # what the listing said is there for the next read
    assert world.rest.count("/v1/chats") == 1


def test_a_chat_the_listing_did_not_show_is_asked_about_again_after_a_while(world, monkeypatch):
    a_group_with_three_messages(world)
    clock = [5_000.0]
    monkeypatch.setattr(world.bp, "_now", lambda: clock[0])
    world.fail_listing = True
    for _ in range(3):
        assert [m["is_group"] for m in asyncio.run(world.bp.fetch_messages("bp:G1"))] == [False, False, False]
    assert world.rest.count("/v1/chats") == 1                         # not at every read
    world.fail_listing = False
    clock[0] += world.bp.KIND_ASK_AGAIN_SECONDS - 1
    assert [m["is_group"] for m in asyncio.run(world.bp.fetch_messages("bp:G1"))] == [False, False, False]
    assert world.rest.count("/v1/chats") == 1
    clock[0] += 2
    assert [m["is_group"] for m in asyncio.run(world.bp.fetch_messages("bp:G1"))] == [True, True, True]
    assert world.rest.count("/v1/chats") == 2


def test_the_wait_for_the_listing_is_shorter_than_the_apps_wait_for_a_page(world):
    assert 0 < world.bp.KIND_LOOKUP_SECONDS <= 5                      # the app gives a page ten seconds
    assert world.bp.KIND_ASK_AGAIN_SECONDS >= 30
