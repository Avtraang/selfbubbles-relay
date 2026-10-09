"""One app that stops taking frames does not hold up the others, or receiving itself.

Every new message is broadcast to the connected apps before anything else is
done with it, one client after the other, and each send was waited for without
a limit. A phone that has gone out of reach without closing its connection
stops taking data; once its buffer is full the send to it never returns, and
with it the loop that reads new messages stood still (no frame for any other
app, no notification, no next message) until the connection was finally given
up on.

Synthetic clients, the relay's own Hub.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.test_send_path import r  # noqa: F401  (fixture)

LIMIT = 0.3          # seconds a client gets in these tests (the relay gives five)


class Client:
    def __init__(self, mode: str = "ok"):
        self.mode, self.got, self.closed, self.at = mode, [], None, []

    async def send_json(self, payload):
        if self.mode == "hang":
            await asyncio.Event().wait()                  # takes nothing more, never fails
        if self.mode == "dead":
            raise RuntimeError("synthetic: connection lost")
        self.got.append(payload["n"])
        self.at.append(time.monotonic())

    async def close(self, code: int = 1000):
        if self.mode == "hang":
            await asyncio.Event().wait()                  # closing it hangs just the same
        self.closed = code


@pytest.fixture
def hub(r, monkeypatch):
    monkeypatch.setattr(r, "HUB_SEND_SECONDS", LIMIT, raising=False)
    return r.Hub()


def test_a_client_that_takes_nothing_more_does_not_hold_up_the_others(hub):
    stuck, fine, also_fine = Client("hang"), Client(), Client()
    hub.clients.update({stuck, fine, also_fine})

    async def scenario():
        t0 = time.monotonic()
        await asyncio.wait_for(hub.broadcast({"n": 1}), 2.0)
        first = time.monotonic() - t0
        await asyncio.wait_for(hub.broadcast({"n": 2}), 2.0)
        return t0, first, time.monotonic() - t0

    t0, first, both = asyncio.run(scenario())
    assert fine.got == [1, 2] and also_fine.got == [1, 2]
    assert fine.at[0] - t0 < LIMIT / 2 and also_fine.at[0] - t0 < LIMIT / 2   # at once, not after the stuck one's limit
    assert LIMIT <= first < LIMIT + 1.5                              # the caller waits for the limit, once
    assert stuck not in hub.clients                                  # and the second frame does not wait at all
    assert both - first < LIMIT / 2


def test_a_client_whose_connection_is_gone_is_dropped_as_before(hub):
    gone, fine = Client("dead"), Client()
    hub.clients.update({gone, fine})
    asyncio.run(hub.broadcast({"n": 1}))
    assert fine.got == [1] and hub.clients == {fine}


def test_a_dropped_client_is_closed_so_that_its_app_reconnects(hub, r):
    """An app that is only slow must not sit on a connection nothing is sent to any more."""
    slow = Client("ok")
    real = slow.send_json

    async def slow_once(payload):
        await asyncio.sleep(LIMIT * 3)                    # longer than the limit
        await real(payload)

    slow.send_json = slow_once
    hub.clients.add(slow)

    async def scenario():
        await hub.broadcast({"n": 1})
        for _ in range(300):
            if slow.closed is not None:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert slow not in hub.clients and slow.closed is not None and slow.got == []


def test_frames_still_reach_every_client_in_order(hub):
    a, b = Client(), Client()
    hub.clients.update({a, b})

    async def scenario():
        for n in range(1, 21):
            await hub.broadcast({"n": n})

    asyncio.run(scenario())
    assert a.got == b.got == list(range(1, 21))


def test_a_broadcast_to_nobody_is_nothing(hub):
    asyncio.run(hub.broadcast({"n": 1}))
    assert hub.clients == set()
