"""iMessage send effects, relay side (parts 1a and 1b, 2026-10-10).

Seen: Messages keeps the effect a message was sent with (Slam, Confetti,
Invisible Ink, ...) in ``message.expressive_send_style_id``, and the relay
dropped it: the app could neither show an effect it received nor ask for one.

Changed: every message dict the relay serves carries ``effect``, Apple's id
from that column or ``null``, right after ``service``. An empty column is
``null`` too, and a Google Messages message is always ``null``. ``POST /send``
takes an optional ``effect``: one of the thirteen ids in ``engines.base.EFFECTS``
(anything else, and any effect in a ``bp:`` chat, is 422 before an engine is
asked). BlueBubbles gets it as ``effectId``; a fallback engine sends the text
plain, and the answer says which happened (``effect_sent``). The send-id
fingerprint takes the effect in, so one id cannot stand for the same text
with two effects. ``GET /health`` names ``effect`` among its ``capabilities``
exactly when BlueBubbles is in the chain for iMessage chats, so the app can
offer the picker only where an effect can be applied (as it offers Edit and
Undo Send, and relies on send ids, only where those are named).

Nothing talks to BlueBubbles, Messages or Beeper: the recording client and
osascript runner of ``test_send_path``, a scripted ``BBServer`` for the engine
on its own, and a synthetic chat.db under ``tmp_path``.
"""

from __future__ import annotations

import pytest

import chatdb_adapter
from engines import SendResult
from engines.applescript import _AS_TEXT, AppleScriptEngine
from engines.base import EFFECT_NOT_FOR_GOOGLE_MESSAGES, EFFECTS, UNKNOWN_EFFECT, effect_problem
from engines.beeper import BeeperEngine
from engines.bluebubbles import BlueBubblesEngine
from tests.fixtures import builders
from tests.test_engines import BBServer, Runner, sync
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_id import code, ids, send  # noqa: F401  (fixtures)
from tests.test_send_path import (  # noqa: F401  (fixtures)
    AUTH, CHAT, TEXT, FakeResponse, _bb_ok, bb, beeper_on, client, osa, r,
)

SLAM = "com.apple.MobileSMS.expressivesend.impact"
CONFETTI = "com.apple.messages.effect.CKConfettiEffect"
PHONE = CHAT[len("iMessage;-;"):]                   # the synthetic number inside CHAT


# ---------------------------------------------------------------------------
# the list
# ---------------------------------------------------------------------------

def test_the_thirteen_effects_are_apples_ids_with_their_ios_names():
    assert len(EFFECTS) == 13
    assert EFFECTS[SLAM] == "Slam" and EFFECTS[CONFETTI] == "Confetti"
    assert EFFECTS["com.apple.MobileSMS.expressivesend.invisibleink"] == "Invisible Ink"
    assert EFFECTS["com.apple.messages.effect.CKSparklesEffect"] == "Celebration"
    assert sum(i.startswith("com.apple.MobileSMS.expressivesend.") for i in EFFECTS) == 4     # bubble effects
    assert sum(i.startswith("com.apple.messages.effect.CK") for i in EFFECTS) == 9           # screen effects


def test_an_effect_passes_only_as_one_of_those_ids_and_only_into_an_imessage_chat():
    assert effect_problem(CHAT, SLAM) is None
    assert effect_problem(CHAT, "Slam") == UNKNOWN_EFFECT                   # the name is not the id
    assert effect_problem(CHAT, "") == UNKNOWN_EFFECT
    assert effect_problem(CHAT, "com.apple.messages.effect.CKNoSuchEffect") == UNKNOWN_EFFECT
    assert effect_problem("bp:42", SLAM) == EFFECT_NOT_FOR_GOOGLE_MESSAGES


# ---------------------------------------------------------------------------
# 1a: the message dict
# ---------------------------------------------------------------------------

@pytest.fixture
def effects_db(make_db, relay_module):
    """One chat with three messages: sent with Slam, sent plain (NULL column),
    and one whose column holds "" (seen on real rows); the relay's own hooks."""
    fx = make_db("macos27", name="effects.db")
    h = builders.add_handle(fx.writer, PHONE)
    c = builders.add_chat(fx.writer, CHAT, 45, PHONE, handles=[h])
    rows = {
        "slam": builders.add_message(fx.writer, c, text="synthetic slam", handle=h,
                                     expressive_send_style_id=SLAM),
        "plain": builders.add_message(fx.writer, c, text="synthetic plain", handle=h),
        "blank": builders.add_message(fx.writer, c, text="synthetic blank column", handle=h,
                                      expressive_send_style_id=""),
    }
    relay_module.configure(fx.path)
    return rows


def test_a_message_sent_with_an_effect_carries_apples_id(r, effects_db):
    by = {m["rowid"]: m for m in r.fetch_new(0)}
    assert by[effects_db["slam"]]["effect"] == SLAM


def test_a_message_sent_without_one_carries_null(r, effects_db):
    by = {m["rowid"]: m for m in r.fetch_new(0)}
    assert by[effects_db["plain"]]["effect"] is None


def test_an_empty_column_is_null_too(r, effects_db):
    by = {m["rowid"]: m for m in r.fetch_new(0)}
    assert by[effects_db["blank"]]["effect"] is None


def test_the_key_follows_service_on_every_path_a_message_leaves_by(r, client, effects_db):
    polled = r.fetch_new(0)                                            # the poll loop -> WebSocket "message"
    page = client.get(f"/thread/{CHAT}/messages", headers=AUTH).json()["messages"]
    one = chatdb_adapter.fetch_message(effects_db["slam"])             # a link-card upgrade -> "update"
    assert len(polled) == len(page) == 3 and one is not None
    for m in (*polled, *page, one):
        keys = list(m)
        assert keys[keys.index("service") + 1] == "effect" and keys[-1] == "effect"
    assert {m["rowid"]: m["effect"] for m in page} == {effects_db["slam"]: SLAM, effects_db["plain"]: None,
                                                        effects_db["blank"]: None}


def test_a_google_messages_message_has_the_key_and_it_is_null(r):
    msg = r.beeper.msg_to_dict({"id": "m1", "text": "synthetic", "isSender": True}, "bp:1", False)
    assert "service" not in msg                                        # no service key: the key goes last
    assert msg["effect"] is None and list(msg)[-1] == "effect"


# ---------------------------------------------------------------------------
# 1b: POST /send
# ---------------------------------------------------------------------------

def post(client, **body):
    return client.post("/send", json={"chat_guid": CHAT, "text": TEXT, **body}, headers=AUTH)


def test_a_valid_effect_reaches_bluebubbles_as_effect_id_and_the_answer_says_it_was_sent(r, bb, osa, client):
    bb.answers = [_bb_ok({"status": 200, "data": {"guid": "synthetic-bb-guid"}})]
    resp = post(client, effect=SLAM)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "bb", "bb": {"status": 200, "data": {"guid": "synthetic-bb-guid"}},
                           "effect_sent": True}
    call = bb.calls[0]
    assert call.path == "/api/v1/message/text" and call.param_keys == ["password"]
    assert set(call.json) == {"chatGuid", "message", "method", "tempGuid", "effectId"}
    assert (call.json["effectId"], call.json["method"], call.json["message"]) == (SLAM, "private-api", TEXT)
    assert osa.calls == []


def test_a_reply_with_an_effect_carries_both(bb, osa, client):
    bb.answers = [_bb_ok()]
    assert post(client, effect=CONFETTI, reply_to_guid="p:0/synthetic").json()["effect_sent"] is True
    assert set(bb.calls[0].json) == {"chatGuid", "message", "method", "tempGuid",
                                     "selectedMessageGuid", "partIndex", "effectId"}


def test_a_fallback_delivers_the_text_plain_and_the_answer_says_the_effect_was_not_sent(r, bb, osa, client):
    bb.answers = [FakeResponse(500, text="synthetic BB failure")]
    osa.results = [True]
    resp = post(client, effect=CONFETTI)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "via": "applescript", "effect_sent": False}
    assert bb.calls[0].json["effectId"] == CONFETTI                    # BlueBubbles was asked for it first
    assert osa.calls == [(r._AS_TEXT, (CHAT, TEXT))]                   # then the text, and nothing else


def test_with_no_bluebubbles_password_the_text_still_goes_out_and_the_effect_does_not(r, bb, osa, client, monkeypatch):
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    osa.results = [True]
    assert post(client, effect=SLAM).json() == {"ok": True, "via": "applescript", "effect_sent": False}
    assert bb.calls == [] and osa.calls == [(r._AS_TEXT, (CHAT, TEXT))]


def test_an_unknown_id_is_422_before_any_engine_is_asked(bb, osa, client):
    for bad in ("Slam", "com.apple.messages.effect.CKNoSuchEffect", "", "../../chat/new"):
        resp = post(client, effect=bad)
        assert (resp.status_code, resp.json()["detail"]) == (422, UNKNOWN_EFFECT), bad
    assert bb.calls == [] and osa.calls == []


def test_an_effect_in_a_google_messages_chat_is_422_before_beeper_is_asked(r, bb, osa, client, monkeypatch, beeper_on):
    asked = []

    async def fake_send(chat_guid, text, reply_to=None):
        asked.append(chat_guid)
        return True

    monkeypatch.setattr(r.beeper, "send", fake_send)
    resp = post(client, chat_guid=r.beeper.PREFIX + "42", effect=SLAM)
    assert (resp.status_code, resp.json()["detail"]) == (422, EFFECT_NOT_FOR_GOOGLE_MESSAGES)
    assert asked == [] and bb.calls == [] and osa.calls == []


def test_without_an_effect_the_answer_and_the_payload_are_as_they_were(bb, osa, client):
    bb.answers = [_bb_ok(), _bb_ok()]
    for body in ({}, {"effect": None}):
        resp = post(client, **body)
        assert resp.status_code == 200 and "effect_sent" not in resp.json(), body
    assert all("effectId" not in c.json for c in bb.calls) and len(bb.calls) == 2


# ---------------------------------------------------------------------------
# 1b: the send id stands for the effect too
# ---------------------------------------------------------------------------

def test_the_same_id_with_another_effect_or_none_is_another_message(client, bb, osa, compat_db, ids):
    bb.answers = [_bb_ok()]
    assert send(client, effect=SLAM).status_code == 200
    for other in ({"effect": CONFETTI}, {}):
        again = send(client, **other)
        assert (again.status_code, code(again)) == (409, "send_id_reused"), other
    assert len(bb.calls) == 1 and osa.calls == []


def test_the_same_id_with_the_same_effect_is_the_same_message_and_its_answer_keeps_effect_sent(client, bb, osa, compat_db, ids):
    bb.answers = [_bb_ok()]
    send(client, effect=SLAM)
    again = send(client, effect=SLAM)
    assert (again.status_code, again.json()) == (200, {"ok": True, "via": "bb", "effect_sent": True, "duplicate": True})
    assert len(bb.calls) == 1


def test_a_send_without_an_effect_fingerprints_as_before(client, bb, osa, compat_db, ids):
    """An id the relay wrote before effects existed must still be recognised:
    the effect joins the fingerprint only when one was asked for."""
    bb.answers = [_bb_ok()]
    send(client)
    assert send(client).json()["duplicate"] is True
    assert send(client, effect=None).json()["duplicate"] is True
    assert len(bb.calls) == 1


# ---------------------------------------------------------------------------
# the engines on their own
# ---------------------------------------------------------------------------

def test_the_bluebubbles_engine_adds_effect_id_only_when_asked():
    bbs = BBServer(answers=[(200, {"status": 200}), (200, {"status": 200})])
    engine = BlueBubblesEngine("http://bb.invalid:1234", "synthetic-bb-password", transport=bbs.transport())
    assert sync(engine.send_text(CHAT, TEXT, effect=SLAM)).ok
    assert sync(engine.send_text(CHAT, TEXT)).ok
    with_effect, without = bbs.seen
    assert with_effect.path == without.path == "/api/v1/message/text"
    assert with_effect.json["effectId"] == SLAM and with_effect.json["method"] == "private-api"
    assert "effectId" not in without.json


def test_the_other_engines_take_the_keyword_and_send_the_text_plain(tmp_path):
    run = Runner([True])
    osa = AppleScriptEngine(runner=run, outbox=tmp_path / "outbox")
    assert sync(osa.send_text(CHAT, TEXT, effect=SLAM)) == SendResult(True, "applescript")
    assert run.calls == [(_AS_TEXT, (CHAT, TEXT))]

    seen = []

    async def fake_send(chat_guid, text, reply_to=None):
        seen.append((chat_guid, text, reply_to))
        return True

    assert sync(BeeperEngine("stub", send=fake_send).send_text("bp:42", TEXT, effect=SLAM)).ok
    assert seen == [("bp:42", TEXT, None)]


# ---------------------------------------------------------------------------
# /health says where an effect can be applied
# ---------------------------------------------------------------------------

def test_the_relay_names_effect_among_its_capabilities_exactly_where_bluebubbles_sends_imessages(
        r, bb, osa, client, monkeypatch):
    """The app offers the effect picker only where the relay names ``effect``,
    as it offers Edit and Undo Send: a relay from before effects has no such
    entry (its ``/send`` ignores the key and sends plain), and a chain in which
    AppleScript alone sends iMessages would answer ``effect_sent: false`` to
    every request, so there is nothing to pick there."""
    def caps():
        return client.get("/health", headers=AUTH).json()["capabilities"]

    assert "effect" in caps()                                          # stub env: BB_PASSWORD set
    assert client.get("/health", headers=AUTH).json()["protocol"] == 1 # additive, like send_id: no bump
    monkeypatch.setattr(r, "SEND_ENGINES", "applescript")
    assert "effect" not in caps()                                      # BlueBubbles configured but not sending
    monkeypatch.setattr(r, "SEND_ENGINES", "")
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    assert "effect" not in caps()                                      # AppleScript alone
    monkeypatch.setattr(r.beeper, "BEEPER_TOKEN", "stub-beeper-token-not-real")
    assert "effect" not in caps()                                      # Google Messages has no effects
    # ... and where it is not named, an effect is still taken and the text goes out plain
    osa.results = [True]
    assert post(client, effect=SLAM).json() == {"ok": True, "via": "applescript", "effect_sent": False}
    assert bb.calls == [] and osa.calls == [(r._AS_TEXT, (CHAT, TEXT))]
