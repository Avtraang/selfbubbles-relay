"""Who a message goes to.

Regression tests for the hardening audit of 2026-10-08: the voice confirmation
that sent on a refusal, two contacts with one name treated as one person,
contact numbers rebuilt as North American, typed numbers read as North
American, and a recipient that is no address matching an unrelated chat.

Everything is in-process and synthetic: contacts come from the real
``load_contacts()`` with the BlueBubbles contact list answered by a fake, sends
go through the real chain into the suite's recording BlueBubbles client, and
chat.db is the synthetic compat database. Numbers are from the 555 range or
from documentation ranges.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests import compat_fixture as cf
from tests.fixtures import builders
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, FakeResponse, bb, client, osa, r  # noqa: F401  (fixtures)

TEXT = "synthetic hello"
CHRIS_A, CHRIS_B = "+15550100101", "+15550100102"


def _ok() -> FakeResponse:
    return FakeResponse(200, {"status": 200, "data": {"guid": "synthetic-bb-guid"}})


def _sends(bb) -> list[tuple]:
    """("text", chat guid) or ("new", addresses) for every send the relay asked BlueBubbles for."""
    out = []
    for c in bb.calls:
        if c.path == "/api/v1/message/text":
            out.append(("text", c.json["chatGuid"]))
        elif c.path == "/api/v1/chat/new":
            out.append(("new", tuple(c.json["addresses"])))
    return out


def load_cards(r, monkeypatch, cards: list[tuple[str, list[str]]]) -> None:
    """Fill the contact map the way production does, from contact CARDS."""
    records = [{"displayName": name,
                "phoneNumbers": [{"address": a} for a in addrs if "@" not in a],
                "emails": [{"address": a} for a in addrs if "@" in a]} for name, addrs in cards]

    class _Client:
        def __init__(self, **_: Any):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def get(self, url: str, params: Any = None):
            return FakeResponse(200, {"status": 200, "data": records})

    monkeypatch.setattr(r.httpx, "Client", _Client)
    r.load_contacts()


def _prepare(client, sentence: str) -> str:
    resp = client.post("/v/prepare", data={"query": sentence}, headers=AUTH)
    assert resp.status_code == 200
    return resp.text


def _confirm(client, answer: str) -> str:
    resp = client.post("/v/confirm", data={"answer": answer}, headers=AUTH)
    assert resp.status_code == 200
    return resp.text


# ---------------------------------------------------------------------------
# the voice confirmation fails closed
# ---------------------------------------------------------------------------

REFUSED_OR_UNCLEAR = [
    "no", "No.", "nope", "cancel", "stop", "never mind",
    "don't send it", "do not send", "please don't send that", "don't do it", "dont send",
    "that's not right", "not right", "incorrect", "that's incorrect", "not correct",
    "not sure", "not okay", "wrong person", "wait", "nobody",
    "нет", "לא", "不", "2", "?", "", "banana", "look at that",
]


@pytest.mark.parametrize("answer", REFUSED_OR_UNCLEAR)
def test_voice_refusal_or_unclear_answer_sends_nothing(client, bb, osa, compat_db, answer):
    assert _prepare(client, f"text Alice Anders {TEXT}") == f"Send {TEXT} to Alice Anders?"
    assert _confirm(client, answer) == "Cancelled."
    assert bb.calls == [] and osa.calls == []
    # the message is gone with the refusal: a later yes has nothing to send
    assert _confirm(client, "yes") == "There's nothing waiting to send."
    assert bb.calls == [] and osa.calls == []


@pytest.mark.parametrize("answer", ["yes", "Yes.", "yeah", "yep", "ok", "okay", "sure", "send it",
                                    "yes please", "correct", "that's right", "do it", "confirm"])
def test_voice_yes_sends_to_the_person_that_was_read_back(client, bb, osa, compat_db, answer):
    bb.answers = [_ok()]
    assert _prepare(client, f"text Alice Anders {TEXT}") == f"Send {TEXT} to Alice Anders?"
    assert _confirm(client, answer) == "Sent to Alice Anders."
    (kind, target), = _sends(bb)
    assert kind == "text" and (cf.ALICE_PHONE in target or cf.ALICE_EMAIL in target)
    assert osa.calls == []


def test_voice_confirm_without_any_answer_sends_nothing(client, bb, osa, compat_db):
    _prepare(client, f"text Alice Anders {TEXT}")
    resp = client.post("/v/confirm", headers=AUTH)
    assert (resp.status_code, resp.text) == (200, "Cancelled.")
    assert bb.calls == [] and osa.calls == []


def _choose() -> dict:
    return {"kind": "choose", "text": TEXT,
            "candidates": [(0.70, "Alice Anders", [cf.ALICE_PHONE]), (0.68, "Bob Brown", [cf.BOB_PHONE])]}


@pytest.mark.parametrize("answer", ["yes", "ok", "send it", "2", "neither", "none of them", "not Alice",
                                    "the other one", "", "нет"])
def test_voice_which_one_round_never_chooses_for_the_owner(r, bb, osa, compat_db, answer):
    res = asyncio.run(r.assistant_deliver(_choose(), answer))
    assert res["status"] == "cancelled"
    assert bb.calls == [] and osa.calls == []


def test_voice_which_one_round_sends_to_the_name_that_was_said(r, bb, osa, compat_db):
    bb.answers = [_ok()]
    res = asyncio.run(r.assistant_deliver(_choose(), "Bob Brown"))
    assert (res["status"], res["speak"]) == ("sent", "Sent to Bob Brown.")
    (kind, target), = _sends(bb)
    assert kind == "text" and cf.BOB_PHONE in target


def test_voice_single_suggestion_takes_a_plain_yes(r, bb, osa, compat_db):
    bb.answers = [_ok()]
    one = {"kind": "choose", "text": TEXT, "candidates": [(0.70, "Bob Brown", [cf.BOB_PHONE])]}
    res = asyncio.run(r.assistant_deliver(one, "yes"))
    assert res["status"] == "sent" and cf.BOB_PHONE in _sends(bb)[0][1]


# ---------------------------------------------------------------------------
# two contacts with one name are two people
# ---------------------------------------------------------------------------

@pytest.fixture
def namesakes(r, monkeypatch, compat_db):
    """Two cards called Chris Park, each with a one-to-one chat; B's is the newer one."""
    w = compat_db.writer
    chat_a = builders.add_chat(w, f"iMessage;-;{CHRIS_A}", 45, CHRIS_A, handles=[CHRIS_A])
    chat_b = builders.add_chat(w, f"iMessage;-;{CHRIS_B}", 45, CHRIS_B, handles=[CHRIS_B])
    builders.add_message(w, chat_a, guid="NS-A-1", text="to the first Chris", is_from_me=1)
    builders.add_message(w, chat_b, guid="NS-B-1", text="to the second Chris", is_from_me=1)
    load_cards(r, monkeypatch, [("Chris Park", [CHRIS_A]), ("Chris Park", [CHRIS_B]),
                                ("Alice Anders", [cf.ALICE_PHONE, cf.ALICE_EMAIL])])


def _match(client, *addresses: str) -> dict:
    resp = client.post("/match_chat", json={"addresses": list(addresses)}, headers=AUTH)
    assert resp.status_code == 200
    return resp.json()


def test_namesakes_are_matched_by_their_own_address(client, namesakes):
    assert _match(client, CHRIS_A)["chat_guid"].endswith(CHRIS_A)      # not B's newer chat
    assert _match(client, CHRIS_B)["chat_guid"].endswith(CHRIS_B)
    hits = client.get("/contacts/search", params={"q": "chris"}, headers=AUTH).json()["results"]
    assert sorted(h["address"] for h in hits) == [CHRIS_A, CHRIS_B]


def test_a_message_for_one_namesake_is_not_sent_into_the_others_chat(client, bb, osa, namesakes):
    bb.answers = [_ok()]
    resp = client.post("/create_chat", json={"addresses": [CHRIS_A], "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["chat_guid"].endswith(CHRIS_A)
    assert _sends(bb) == [("text", f"iMessage;-;{CHRIS_A}")]


def test_the_phone_and_the_email_of_one_card_are_still_one_person(r, client, namesakes):
    by_phone, by_email = _match(client, cf.ALICE_PHONE), _match(client, cf.ALICE_EMAIL)
    assert by_phone["found"] and by_phone["chat_guid"] == by_email["chat_guid"]
    assert r.person_key(cf.ALICE_PHONE) == r.person_key(cf.ALICE_EMAIL)
    assert r.person_key(CHRIS_A) != r.person_key(CHRIS_B)


def test_voice_does_not_guess_between_namesakes(client, bb, osa, namesakes):
    said = _prepare(client, f"text Chris Park {TEXT}")
    assert said == "You have more than one contact called Chris Park. Send that one from the app."
    assert _confirm(client, "yes") == "There's nothing waiting to send."
    assert bb.calls == [] and osa.calls == []


# ---------------------------------------------------------------------------
# a contact's number is the number on the card
# ---------------------------------------------------------------------------

ABROAD = [("Noa Levi", "+972 52-555-1234", "+972525551234"),
          ("Pierre Dupont", "+33 1 99 00 12 34", "+33199001234"),
          ("Mia Clarke", "+44 7700 900123", "+447700900123"),
          ("Uma Stone", "(555) 000-4321", "+15550004321")]


@pytest.fixture
def abroad(r, monkeypatch, compat_db):
    load_cards(r, monkeypatch, [(name, [card]) for name, card, _ in ABROAD])


@pytest.mark.parametrize("name, card, e164", ABROAD, ids=[c[0] for c in ABROAD])
def test_typeahead_offers_the_cards_own_number(client, abroad, name, card, e164):
    hits = client.get("/contacts/search", params={"q": name.lower()}, headers=AUTH).json()["results"]
    assert [h["address"] for h in hits if h["name"] == name] == [e164]


def test_a_first_message_to_a_contact_abroad_goes_to_their_number(client, bb, osa, abroad):
    bb.answers = [_ok()]
    # the address the app sends is the one the typeahead offered
    (hit,) = client.get("/contacts/search", params={"q": "noa levi"}, headers=AUTH).json()["results"]
    resp = client.post("/create_chat", json={"addresses": [hit["address"]], "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200
    assert _sends(bb) == [("new", ("+972525551234",))]


def test_voice_to_a_contact_abroad_uses_their_number(client, bb, osa, abroad):
    bb.answers = [_ok()]
    assert _prepare(client, f"text Noa Levi {TEXT}") == f"Send {TEXT} to Noa Levi?"
    assert _confirm(client, "yes") == "Sent to Noa Levi."
    assert _sends(bb) == [("new", ("+972525551234",))]


# ---------------------------------------------------------------------------
# a typed number is not guessed at
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed, sendable", [
    ("+33 6 12 34 56 78", "+33612345678"), ("(310) 555-0123", "+13105550123"),
    ("1 310 555 0123", "+13105550123"), ("0033 6 12 34 56 78", "+33612345678"),
    ("011 44 20 7946 0958", "+442079460958"), ("32665", "32665"),
    (" Alice@Example.INVALID ", "alice@example.invalid")])
def test_normalize_address_with_the_default_country(r, typed, sendable):
    assert r.DEFAULT_COUNTRY_CODE == "1"
    assert r.normalize_address(typed) == sendable


@pytest.mark.parametrize("typed", ["Mom", "", "   ", "555 0123", "347 812 345", "07911 123456",
                                   "55 1234 5678 9", "+12", "+1 234 567 890 123 456 7"])
def test_normalize_address_refuses_what_it_would_have_to_guess(r, typed):
    with pytest.raises(r.BadAddress):
        r.normalize_address(typed)


@pytest.mark.parametrize("code, typed, sendable", [
    ("44", "07911 123456", "+447911123456"), ("44", "020 7946 0958", "+442079460958"),
    ("44", "+1 310 555 0123", "+13105550123"), ("39", "06 1234 5678", "+390612345678"),
    ("39", "347 812 3456", "+393478123456"), ("52", "55 1234 5678", "+525512345678")])
def test_normalize_address_with_another_default_country(r, monkeypatch, code, typed, sendable):
    monkeypatch.setattr(r, "DEFAULT_COUNTRY_CODE", code)
    assert r.normalize_address(typed) == sendable


@pytest.fixture
def digitless_chat(compat_db):
    """A chat whose identifier has no digits (an alphanumeric sender), newest in the database."""
    w = compat_db.writer
    chat = builders.add_chat(w, "SMS;-;AMAZON", 45, "AMAZON", handles=["AMAZON"])
    builders.add_message(w, chat, guid="DL-1", text="your parcel", is_from_me=1)


@pytest.mark.parametrize("recipient", ["Mom", "the office", "555 0123"])
def test_a_recipient_that_is_no_address_matches_nothing_and_sends_nothing(client, bb, osa, digitless_chat,
                                                                          recipient):
    assert _match(client, recipient) == {"found": False}
    resp = client.post("/create_chat", json={"addresses": [recipient], "text": TEXT}, headers=AUTH)
    assert resp.status_code == 400 and recipient not in resp.text
    assert bb.calls == [] and osa.calls == []


def test_an_identifier_without_digits_is_only_equal_to_itself(r, digitless_chat):
    assert r.person_key("AMAZON") == r.person_key(" amazon ")
    assert r.person_key("AMAZON") != r.person_key("Mom")
    assert r.person_key("Mom") != r.person_key("")
