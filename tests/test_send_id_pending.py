"""The same send arriving twice is delivered twice: the relay has no send id yet (K7).

Not fixed here. This is the reproduction, kept as an expected failure so the
gap stays visible in every run until the design is agreed (a client-chosen id
on /send, /send_attachment and /create_chat, remembered by the relay with the
outcome). What it would make safe is "Send again" on a text whose first send
ended without an answer: today the owner has to look at the chat first,
because a second POST is a second message.
"""

from __future__ import annotations

import pytest

from tests.test_recipient_safety import _ok, _sends
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, CHAT, bb, client, osa, r  # noqa: F401  (fixtures)


@pytest.mark.xfail(strict=True, reason="K7: no send id on the relay yet; the design awaits the owner's review")
def test_the_same_send_arriving_twice_is_delivered_once(client, bb, osa, compat_db):
    bb.answers = [_ok(), _ok()]
    body = {"chat_guid": CHAT, "text": "synthetic text", "client_id": "synthetic-send-0001"}
    first = client.post("/send", json=body, headers=AUTH)
    second = client.post("/send", json=body, headers=AUTH)      # "Send again" after an answer that never arrived
    assert first.status_code == 200 and second.status_code == 200
    assert len(_sends(bb)) == 1 and osa.calls == []
