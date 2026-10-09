"""B2: deterministic probes. In-process, synthetic numbers (555-01xx, drama ranges), nothing leaves the process."""
from __future__ import annotations

from typing import Any

import pytest

from tests import compat_fixture as cf
from tests.fixtures import builders
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, FakeResponse, bb, client, osa, r  # noqa: F401  (fixtures)

TEXT = "synthetic hello"
HOME, PAT, AMC, ANN2 = "+15550100300", "+15550100301", "+15550100302", "+15550100303"
BOB, CAROL, SPOUSE = "+15550100311", "+15550100312", "+15550100313"


def _ok() -> FakeResponse:
    return FakeResponse(200, {"status": 200, "data": {"guid": "synthetic-bb-guid"}})


def _sends(bb) -> list[tuple]:
    out = []
    for c in bb.calls:
        if c.path == "/api/v1/message/text":
            out.append(("text", c.json["chatGuid"]))
        elif c.path == "/api/v1/chat/new":
            out.append(("new", tuple(c.json["addresses"])))
    return out


def load_cards(r, monkeypatch, cards) -> None:
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

        def get(self, url: str, params: Any = None, **_: Any):
            return FakeResponse(200, {"status": 200, "data": records})

    monkeypatch.setattr(r.httpx, "Client", _Client)
    r.load_contacts()


def _one(w, addr: str, tag: str) -> None:
    chat = builders.add_chat(w, f"iMessage;-;{addr}", 45, addr, handles=[addr])
    builders.add_message(w, chat, guid=f"B2F-{tag}", text=f"synthetic to {tag}", is_from_me=1)


def _voice(client, bb, sentence: str, answer: str = "yes"):
    bb.calls.clear()
    bb.answers = [_ok()]
    said = client.post("/v/prepare", data={"query": sentence}, headers=AUTH).text
    heard = client.post("/v/confirm", data={"answer": answer}, headers=AUTH).text
    return said, heard, _sends(bb)


# ---------------------------------------------------------------------------------------------------
# F1. A card whose only address is also on a card that is loaded LATER has no entry in the voice index.
#     The name that was said then resolves to a name that resembles it, and "yes" sends to that other person.
# ---------------------------------------------------------------------------------------------------
HIDDEN = [("Ann", [HOME]), ("Ann Marie Cole", [AMC]), ("Pat Quinn", [PAT, HOME])]


@pytest.mark.parametrize("order", ["ann-first", "ann-last"])
def test_F1_voice_card_with_only_a_shared_address(r, client, bb, osa, compat_db, monkeypatch, order):
    w = compat_db.writer
    for a, tag in ((HOME, "home"), (PAT, "pat"), (AMC, "amc")):
        _one(w, a, tag)
    cards = HIDDEN if order == "ann-first" else [HIDDEN[2], HIDDEN[1], HIDDEN[0]]
    load_cards(r, monkeypatch, cards)
    said, heard, sends = _voice(client, bb, f"text Ann {TEXT}")
    offered = client.get("/contacts/search", params={"q": "ann"}, headers=AUTH).json()["results"]
    print(f"\nF1 [{order}] index names {sorted(r.name_index())} | prepare {said!r} | yes -> {heard!r} | sends {sends} "
          f"| typeahead 'ann' {offered}")
    assert sends in ([], [("text", f"iMessage;-;{HOME}")]), "a message for Ann went to somebody who is not Ann"


def test_F1b_voice_bob_stone_becomes_bo(r, client, bb, osa, compat_db, monkeypatch):
    w = compat_db.writer
    for a, tag in ((HOME, "home"), (PAT, "bo-own")):
        _one(w, a, tag)
    load_cards(r, monkeypatch, [("Bob Stone", [HOME]), ("Bo", [PAT, HOME])])
    said, heard, sends = _voice(client, bb, f"text Bob Stone {TEXT}")
    print(f"\nF1b prepare {said!r} | yes -> {heard!r} | sends {sends}")
    assert sends in ([], [("text", f"iMessage;-;{HOME}")])


# ---------------------------------------------------------------------------------------------------
# F2. Default country other than 1 whose international prefix is not "00": "00" is cut off anyway and the
#     next digit is read as the start of the country code.
# ---------------------------------------------------------------------------------------------------
OWN_PREFIX = [("852", "001 65 6555 0123", "+6565550123"),      # Hong Kong dials 001: a Singapore number
              ("65", "001 45 5550 1234", "+4555501234"),       # Singapore dials 001: a Danish number
              ("82", "002 44 7700 900123", "+447700900123"),   # Korea, carrier 002: a UK number
              ("234", "009 1 225 555 0123", "+12255550123"),   # Nigeria dials 009: a US number
              ("55", "0021 1 225 555 0123", "+12255550123"),   # Brazil, carrier 21: a US number
              ("81", "010 65 6555 0123", "+6565550123")]       # Japan dials 010: a Singapore number


def test_F2_own_international_prefix_table(r, monkeypatch):
    wrong = []
    for default, written, meant in OWN_PREFIX:
        monkeypatch.setattr(r, "DEFAULT_COUNTRY_CODE", default)
        try:
            got = r.normalize_address(written)
        except r.BadAddress:
            got = "refused"
        print(f"F2 default {default}: {written!r} (meant {meant}) -> {got}")
        if got not in ("refused", meant):
            wrong.append((default, written, meant, got))
    assert not wrong


def test_F2_end_to_end_default_852(r, client, bb, osa, compat_db, monkeypatch):
    monkeypatch.setattr(r, "DEFAULT_COUNTRY_CODE", "852")
    stranger = "+16565550123"                                   # 555-01xx: fictional
    _one(compat_db.writer, stranger, "us-stranger")
    load_cards(r, monkeypatch, [("Mei Tan", ["001 65 6555 0123"])])
    offered = client.get("/contacts/search", params={"q": "mei"}, headers=AUTH).json()["results"]
    bb.answers = [_ok()]
    for row in offered:
        client.post("/create_chat", json={"addresses": [row["address"]], "text": TEXT}, headers=AUTH)
    app_sends = _sends(bb)
    said, heard, voice_sends = _voice(client, bb, f"text Mei Tan {TEXT}")
    print(f"\nF2 e2e default 852, card 'Mei Tan' = '001 65 6555 0123': offered {offered} | app sends {app_sends} "
          f"| voice {said!r} -> {heard!r} sends {voice_sends}")
    assert not any(s == ("text", f"iMessage;-;{stranger}") for s in app_sends + voice_sends)


# ---------------------------------------------------------------------------------------------------
# F3. Spoken number with "+": the first number of the message is glued onto it (no shape check with "+").
# ---------------------------------------------------------------------------------------------------
def test_F3_voice_plus_number_swallows_the_message_digits(r, client, bb, osa, compat_db):
    said, heard, sends = _voice(client, bb, "text +49 30 5550100 2 tickets please")
    print(f"\nF3 prepare {said!r} | yes -> {heard!r} | sends {sends}")
    assert sends in ([], [("new", ("+49305550100",))])


# ---------------------------------------------------------------------------------------------------
# F4. The owner's own number on somebody else's card and on no card of the owner's.
# ---------------------------------------------------------------------------------------------------
def test_F4_owner_number_on_another_card(r, client, bb, osa, compat_db, monkeypatch):
    w = compat_db.writer
    chat = builders.add_chat(w, "iMessage;+;chatB2F4", 43, "chatB2F4", handles=[BOB, CAROL, SPOUSE])
    builders.add_message(w, chat, guid="B2F-F4", text="synthetic group", is_from_me=1)
    load_cards(r, monkeypatch, [("Bob Brook", [BOB]), ("Carol Reyes", [CAROL]), ("Robin Home", [SPOUSE, cf.SELF_PHONE])])
    bb.answers = [_ok()]
    resp = client.post("/create_chat", json={"addresses": [BOB, CAROL], "text": TEXT}, headers=AUTH)
    print(f"\nF4 /create_chat([Bob, Carol]) -> {resp.status_code} sends {_sends(bb)} "
          f"| person_key(spouse)={r.person_key(SPOUSE)!r} person_key(self)={r.person_key(cf.SELF_PHONE)!r}")
    assert _sends(bb) != [("text", "iMessage;+;chatB2F4")], "a message for Bob and Carol went into the group with Robin"


# ---------------------------------------------------------------------------------------------------
# F5. Arabic-Indic digits: the bracketed trunk zero is not dropped (ASCII and full-width are).
# ---------------------------------------------------------------------------------------------------
def test_F5_arabic_indic_bracketed_zero(r):
    ascii_form = "+44 (0)7700 900123"
    arabic = ascii_form.translate({ord(c): 0x0660 + int(c) for c in "0123456789"})
    a, b = r.normalize_address(ascii_form), r.normalize_address(arabic)
    print(f"\nF5 {ascii_form!r} -> {a} | {arabic!r} -> {b} | keys {r.norm_key(ascii_form)!r} {r.norm_key(arabic)!r}")
    assert a == b
