"""V2 regression probes for relay step 1 ("who is one person").

Every test asserts the RIGHT outcome, so a FAILURE here is a demonstrated defect.
All data is synthetic (555 range); contacts are built by the real load_contacts()
from contact CARDS answered by a fake BlueBubbles; sends go into the suite's
recording BlueBubbles client. Nothing leaves the process.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests import compat_fixture as cf
from tests.fixtures import builders
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, FakeResponse, bb, client, osa, r  # noqa: F401  (fixtures)

TEXT = "synthetic hello"
PAT, SAM = "+15550100201", "+15550100202"
HOME_LOW = "+15550100100"       # shared landline that sorts BELOW both mobiles
HOME_HIGH = "+15550100999"      # shared landline that sorts ABOVE both mobiles
HOME_SELF = "+15550000100"      # shared landline below the owner's own number too
X, Y = "+15550100301", "+15550100302"     # two unrelated people without cards
G_SMALL, G_BIG, G_SAM = "iMessage;+;chat9001", "iMessage;+;chat9002", "iMessage;+;chat9003"


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


def load_cards(r, monkeypatch, cards: list[tuple[str, list[str]]]) -> None:
    """Same as tests/test_recipient_safety.load_cards (copied so this file also runs on the old code)."""
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


def _chat(w, guid: str, style: int, ident: str, members: list[str], tag: str) -> None:
    chat = builders.add_chat(w, guid, style, ident, handles=members)
    builders.add_message(w, chat, guid=f"V2-{tag}", text=f"synthetic to {tag}", is_from_me=1)


def _one(w, addr: str, tag: str) -> None:
    _chat(w, f"iMessage;-;{addr}", 45, addr, [addr], tag)


def _match(client, *addresses: str) -> dict:
    resp = client.post("/match_chat", json={"addresses": list(addresses)}, headers=AUTH)
    assert resp.status_code == 200
    return resp.json()


def _create(client, bb, *addresses: str) -> list[tuple]:
    bb.answers = [_ok()]
    resp = client.post("/create_chat", json={"addresses": list(addresses), "text": TEXT}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return _sends(bb)


COUPLE_LOW = [("Pat Quinn", [PAT, HOME_LOW]), ("Sam Quinn", [SAM, HOME_LOW])]


# 1. two DIFFERENT names, each card lists the home landline, the landline is the smallest key
def test_RIGHT_couple_sharing_their_lowest_number_stay_two_people(r, client, bb, osa, compat_db, monkeypatch):
    w = compat_db.writer
    _one(w, PAT, "pat")
    _one(w, SAM, "sam")                                   # Sam's chat is the newer one
    load_cards(r, monkeypatch, COUPLE_LOW)
    m, sends = _match(client, PAT), _create(client, bb, PAT)
    print(f"\n[V2-1 couple] person_key(PAT)={r.person_key(PAT)!r} person_key(SAM)={r.person_key(SAM)!r} "
          f"| /match_chat([PAT]) -> {m.get('chat_guid')} | /create_chat([PAT]) sent {sends}")
    assert r.person_key(PAT) != r.person_key(SAM)
    assert m["chat_guid"].endswith(PAT)
    assert sends == [("text", f"iMessage;-;{PAT}")]


# 1b. the voice path: "text Sam", read back as Sam, while Pat's chat is the newer one
def test_RIGHT_voice_to_sam_is_sent_to_sam(r, client, bb, osa, compat_db, monkeypatch):
    w = compat_db.writer
    _one(w, SAM, "sam")
    _one(w, PAT, "pat")                                   # Pat's chat is the newer one
    load_cards(r, monkeypatch, COUPLE_LOW)
    said = client.post("/v/prepare", data={"query": f"text Sam Quinn {TEXT}"}, headers=AUTH).text
    bb.answers = [_ok()]
    done = client.post("/v/confirm", data={"answer": "yes"}, headers=AUTH).text
    print(f"\n[V2-1b voice] prepare -> {said!r} | confirm yes -> {done!r} | sent {_sends(bb)}")
    assert _sends(bb) in ([("text", f"iMessage;-;{SAM}")], [])


# 2. the ORIGINAL scenario (one name, two cards) with the same shared landline
def test_RIGHT_namesakes_sharing_their_lowest_number_stay_two_people(r, client, bb, osa, compat_db, monkeypatch):
    w = compat_db.writer
    _one(w, PAT, "chris-a")
    _one(w, SAM, "chris-b")
    load_cards(r, monkeypatch, [("Chris Park", [PAT, HOME_LOW]), ("Chris Park", [SAM, HOME_LOW])])
    sends = _create(client, bb, PAT)
    print(f"\n[V2-2 namesakes] /create_chat([A]) sent {sends}")
    assert sends == [("text", f"iMessage;-;{PAT}")]


def test_RIGHT_voice_still_refuses_namesakes_that_share_a_number(r, client, bb, osa, compat_db, monkeypatch):
    w = compat_db.writer
    _one(w, PAT, "chris-a")
    _one(w, SAM, "chris-b")
    load_cards(r, monkeypatch, [("Chris Park", [PAT, HOME_LOW]), ("Chris Park", [SAM, HOME_LOW])])
    said = client.post("/v/prepare", data={"query": f"text Chris Park {TEXT}"}, headers=AUTH).text
    bb.answers = [_ok()]
    done = client.post("/v/confirm", data={"answer": "yes"}, headers=AUTH).text
    print(f"\n[V2-2 voice] namesake_names()={r.namesake_names()} | prepare -> {said!r} | "
          f"confirm yes -> {done!r} | sent {_sends(bb)}")
    assert said.startswith("You have more than one contact called Chris Park")
    assert bb.calls == []


# 3. groups: {Pat, X} against {Pat, Sam, X}
def test_RIGHT_group_without_sam_is_not_the_group_with_sam(r, client, bb, osa, compat_db, monkeypatch):
    w = compat_db.writer
    _chat(w, G_SMALL, 43, "chat9001", [PAT, X], "small")
    _chat(w, G_BIG, 43, "chat9002", [PAT, SAM, X], "big")          # newer
    load_cards(r, monkeypatch, COUPLE_LOW)
    m, sends = _match(client, PAT, X), _create(client, bb, PAT, X)
    print(f"\n[V2-3 group] /match_chat([PAT, X]) -> {m.get('chat_guid')} | /create_chat([PAT, X]) sent {sends} "
          f"(chat9001 = Pat+X, chat9002 = Pat+Sam+X)")
    assert sends == [("text", G_SMALL)]


# 4. the owner's own card shares its smallest number with the spouse's card
def test_RIGHT_spouse_sharing_the_owners_lowest_number_is_not_the_owner(r, client, bb, osa, compat_db, monkeypatch):
    r.SELF_RAW[:] = [cf.SELF_PHONE]
    w = compat_db.writer
    _chat(w, G_SAM, 43, "chat9003", [SAM, X, Y], "with-sam")
    load_cards(r, monkeypatch, [("Me Owner", [cf.SELF_PHONE, HOME_SELF]), ("Sam Quinn", [SAM, HOME_SELF])])
    m, sends = _match(client, X, Y), _create(client, bb, X, Y)
    print(f"\n[V2-4 self] person_key(SAM)={r.person_key(SAM)!r} person_key(SELF)={r.person_key(cf.SELF_PHONE)!r} "
          f"| /match_chat([X, Y]) -> {m.get('chat_guid')} | /create_chat([X, Y]) sent {sends} (chat9003 = Sam+X+Y)")
    assert r.person_key(SAM) != r.person_key(cf.SELF_PHONE)
    assert m == {"found": False}
    assert sends == [("new", (X, Y))]


# 5. control: the shared number is NOT the smallest key
def test_RIGHT_couple_sharing_a_number_that_is_not_the_lowest(r, client, bb, osa, compat_db, monkeypatch):
    w = compat_db.writer
    _one(w, HOME_HIGH, "home")
    _one(w, PAT, "pat")
    _one(w, SAM, "sam")
    load_cards(r, monkeypatch, [("Pat Quinn", [PAT, HOME_HIGH]), ("Sam Quinn", [SAM, HOME_HIGH])])
    home = _match(client, HOME_HIGH)
    print(f"\n[V2-5 control] person_key PAT={r.person_key(PAT)!r} SAM={r.person_key(SAM)!r} "
          f"HOME={r.person_key(HOME_HIGH)!r} | /match_chat([HOME]) -> {home.get('chat_guid')}")
    assert r.person_key(PAT) != r.person_key(SAM)
    assert _match(client, PAT)["chat_guid"].endswith(PAT)
    assert _match(client, SAM)["chat_guid"].endswith(SAM)
    assert _create(client, bb, PAT) == [("text", f"iMessage;-;{PAT}")]


# 6. one person on two cards (two accounts), the second card a subset of the first
def test_RIGHT_a_duplicate_card_is_still_one_person(r, client, bb, osa, compat_db, monkeypatch):
    load_cards(r, monkeypatch, [("Dana Dupe", [PAT, HOME_HIGH]), ("Dana Dupe", [HOME_HIGH])])
    said = client.post("/v/prepare", data={"query": f"text Dana Dupe {TEXT}"}, headers=AUTH).text
    print(f"\n[V2-6 duplicate] person_key(mobile)={r.person_key(PAT)!r} person_key(home)={r.person_key(HOME_HIGH)!r} "
          f"| voice prepare -> {said!r}")
    # Second pass: only two cards with exactly the same addresses are one card. A card that lists part of
    # another's addresses may be another person of that name (a parent and a child on the home line), so the
    # two stay apart, the shared number belongs to neither, and the voice path refuses the name.
    assert r.person_key(PAT) != r.person_key(HOME_HIGH)
    assert said.startswith("You have more than one contact called Dana Dupe")
    load_cards(r, monkeypatch, [("Dana Dupe", [PAT, HOME_HIGH]), ("Dana Dupe", [HOME_HIGH, PAT])])
    assert r.person_key(PAT) == r.person_key(HOME_HIGH) and r.namesake_names() == set()
    return


# 7. odd addresses and keys
def test_INFO_odd_address_keys(r, compat_db):
    odd = [None, "", "   ", "---", "+", "@", " Alice@Example.INVALID ", "AMAZON", " amazon ",
           "VERIFY22395", "22395", "+447723456789", "+17723456789"]
    print("\n[V2-7 keys] " + " | ".join(f"{a!r}->{r.person_key(a)!r}" for a in odd))
    assert r.person_key(None) == r.person_key("") == r.person_key("   ") == "raw:"
    assert r.person_key("---") != r.person_key("+") != r.person_key("")
    assert r.person_key(" Alice@Example.INVALID ") == r.person_key(cf.ALICE_EMAIL)


def test_RIGHT_a_number_abroad_is_not_the_us_number_with_the_same_last_ten_digits(r, client, bb, osa, compat_db):
    _one(compat_db.writer, "+17723456789", "us-number")
    m, sends = _match(client, "+44 7723 456789"), _create(client, bb, "+44 7723 456789")
    print(f"\n[V2-7 last-ten] /match_chat(['+44 7723 456789']) -> {m.get('chat_guid')} | /create_chat sent {sends}")
    assert sends == [("new", ("+447723456789",))]


def test_RIGHT_a_short_code_is_not_a_named_sender_that_ends_in_the_same_digits(r, client, bb, osa, compat_db):
    _chat(compat_db.writer, "SMS;-;VERIFY22395", 45, "VERIFY22395", ["VERIFY22395"], "alnum")
    m = _match(client, "22395")
    print(f"\n[V2-7 short code] /match_chat(['22395']) -> {m.get('chat_guid')}")
    assert m == {"found": False}
