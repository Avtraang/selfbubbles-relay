"""Reviewer B probes: who is one person, and which number.

Every test named test_RIGHT_* asserts the outcome the owner requires, so a FAILURE is a
demonstrated defect. test_INFO_* only print. Synthetic data only (555 numbers, .invalid).
Runs unchanged on HEAD and on HEAD~1 (own copy of the card loader).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fixtures import builders
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, FakeResponse, bb, client, osa, r  # noqa: F401  (fixtures)

TEXT = "synthetic hello"
HOME = "+15550100100"
MOBILE = "+15550100201"
SAM = "+15550100202"
PAT_MAIL = "pat@example.invalid"
UK = "+447700900123"


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
    builders.add_message(w, chat, guid=f"B-{tag}", text=f"synthetic to {tag}", is_from_me=1)


def _voice(client, bb, query: str) -> tuple[str, str]:
    said = client.post("/v/prepare", data={"query": query}, headers=AUTH).text
    bb.answers = [_ok()]
    done = client.post("/v/confirm", data={"answer": "yes"}, headers=AUTH).text
    return said, done


def _offered(client, name: str) -> list[str]:
    hits = client.get("/contacts/search", params={"q": name.lower()}, headers=AUTH).json()["results"]
    return [h["address"] for h in hits if h["name"] == name]


# ---------------------------------------------------------------------------
# B1. the fold: two cards with one name, one card's addresses a subset of the other's.
#     Chris senior is reachable only on the home line; Chris junior lists home + mobile.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ["senior-first", "junior-first"])
def test_RIGHT_B1_app_message_to_the_home_line_is_not_sent_to_the_mobile(r, client, bb, osa, compat_db,
                                                                       monkeypatch, order):
    w = compat_db.writer
    _one(w, HOME, "senior")
    _one(w, MOBILE, "junior")                               # the mobile chat is the newer one
    cards = [("Chris Park", [HOME]), ("Chris Park", [HOME, MOBILE])]
    load_cards(r, monkeypatch, cards if order == "senior-first" else cards[::-1])
    bb.answers = [_ok()]
    resp = client.post("/create_chat", json={"addresses": [HOME], "text": TEXT}, headers=AUTH)
    sends = _sends(bb)
    print(f"\n[B1 app {order}] person_key(HOME)={r.person_key(HOME)!r} person_key(MOBILE)={r.person_key(MOBILE)!r} "
          f"namesakes={getattr(r, 'namesake_names', lambda: 'n/a')()} | /create_chat([HOME]) {resp.status_code} sent {sends}")
    assert sends == [("text", f"iMessage;-;{HOME}")]


def test_RIGHT_B1_voice_does_not_pick_one_of_two_same_name_cards(r, client, bb, osa, compat_db, monkeypatch):
    w = compat_db.writer
    _one(w, HOME, "senior")
    _one(w, MOBILE, "junior")
    load_cards(r, monkeypatch, [("Chris Park", [HOME]), ("Chris Park", [HOME, MOBILE])])
    said, done = _voice(client, bb, f"text Chris Park {TEXT}")
    print(f"\n[B1 voice] prepare -> {said!r} | yes -> {done!r} | sent {_sends(bb)}")
    assert _sends(bb) in ([], [("text", f"iMessage;-;{HOME}")])


# ---------------------------------------------------------------------------
# B2. a namesake whose only number the relay refuses is not in the map at all:
#     is the voice path still told that there are two people of that name?
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("second", ["(555) 010-0199 x123", "07700 900123", "44 7700 900123", "555-0142"])
def test_RIGHT_B2_voice_refuses_namesakes_when_one_card_number_is_refused(r, client, bb, osa, compat_db,
                                                                         monkeypatch, second):
    _one(compat_db.writer, MOBILE, "chris-a")
    load_cards(r, monkeypatch, [("Chris Park", [MOBILE]), ("Chris Park", [second])])
    said, done = _voice(client, bb, f"text Chris Park {TEXT}")
    print(f"\n[B2 second card {second!r}] namesakes={getattr(r, 'namesake_names', lambda: 'n/a')()} | prepare -> {said!r} | "
          f"yes -> {done!r} | sent {_sends(bb)}")
    assert _sends(bb) == []


# ---------------------------------------------------------------------------
# B3. "+44 (0)7700 900123": the bracketed trunk zero is not part of the number
# ---------------------------------------------------------------------------

def test_RIGHT_B3_bracketed_trunk_zero_is_dropped_or_refused(r):
    out = {}
    for typed in ["+44 (0)7700 900123", "+44 (0) 20 7946 0958", "+49 (0)30 901820", "+61 (0)412 345 678"]:
        try:
            out[typed] = r.normalize_address(typed)
        except r.BadAddress:
            out[typed] = "REFUSED"
    print(f"\n[B3 normalize] {out}")
    assert out["+44 (0)7700 900123"] in ("+447700900123", "REFUSED")
    assert out["+44 (0) 20 7946 0958"] in ("+442079460958", "REFUSED")


def test_RIGHT_B3_card_with_bracketed_zero_reaches_its_own_chat(r, client, bb, osa, compat_db, monkeypatch):
    _one(compat_db.writer, UK, "nigel")
    load_cards(r, monkeypatch, [("Nigel Brook", ["+44 (0)7700 900123"])])
    offered = _offered(client, "Nigel Brook")
    said, done = _voice(client, bb, f"text Nigel Brook {TEXT}")
    print(f"\n[B3 card] thread name for {UK} = {r.resolve(UK)!r} | offered {offered} | prepare -> {said!r} | "
          f"yes -> {done!r} | sent {_sends(bb)}")
    assert _sends(bb) in ([], [("text", f"iMessage;-;{UK}")])


# ---------------------------------------------------------------------------
# B4. does a contact still name its chat wherever it did before the key change?
# ---------------------------------------------------------------------------

NAMED_BEFORE = [("uk-national", "07700 900123", "+447700900123"),
                ("uk-cc-no-plus", "44 7700 900123", "+447700900123"),
                ("il-cc-no-plus", "972 52 555 1234", "+972525551234"),
                ("de-national", "0151 23456789", "+4915123456789"),
                ("us-plain", "(555) 010-0142", "+15550100142"),
                ("us-db-no-plus", "+1 555 010 0142", "15550100142"),
                ("us-db-ten", "+1 555 010 0142", "5550100142"),
                ("intl-db-no-plus", "+44 7700 900123", "447700900123")]


@pytest.mark.parametrize("tag, card, handle", NAMED_BEFORE, ids=[c[0] for c in NAMED_BEFORE])
def test_RIGHT_B4_contact_still_names_its_chat(r, compat_db, monkeypatch, tag, card, handle):
    load_cards(r, monkeypatch, [("Gemma Hale", [card])])
    print(f"\n[B4 {tag}] card {card!r}, chat handle {handle!r}: norm_key={r.norm_key(handle)!r} "
          f"thread name={r.resolve(handle)!r}")
    # Decided, not overlooked: a card number the relay cannot read as a phone number (a national number of
    # another country, a country code without "+") names no chat any more, and neither does a database handle
    # without "+". Naming it by its last ten digits is the guess that sent messages to the wrong person. The
    # relay says at load how many card numbers it could not use, so the cards can be written with "+".
    unreadable = {"uk-national", "uk-cc-no-plus", "il-cc-no-plus", "de-national", "intl-db-no-plus"}
    assert r.resolve(handle) == (handle if tag in unreadable else "Gemma Hale")


# ---------------------------------------------------------------------------
# B5. a card whose OWN address is an e-mail and whose phone is the couple's shared line
# ---------------------------------------------------------------------------

def test_RIGHT_B5_voice_message_for_pat_is_not_addressed_to_the_shared_line(r, client, bb, osa, compat_db,
                                                                          monkeypatch):
    w = compat_db.writer
    _one(w, PAT_MAIL, "pat")
    _one(w, HOME, "home")
    load_cards(r, monkeypatch, [("Sam Quinn", [SAM, HOME]), ("Pat Quinn", [PAT_MAIL, HOME])])
    idx = r.name_index().get("pat quinn")
    said, done = _voice(client, bb, f"text Pat Quinn {TEXT}")
    print(f"\n[B5] name_index['pat quinn']={idx} shared={sorted(getattr(r, 'CONTACT_SHARED', []))} | "
          f"prepare -> {said!r} | yes -> {done!r} | sent {_sends(bb)}")
    assert _sends(bb) in ([], [("text", f"iMessage;-;{PAT_MAIL}")])


# ---------------------------------------------------------------------------
# B6. other default countries: one national mobile and landline each, and the
#     country code written without "+"
# ---------------------------------------------------------------------------

NATIONAL = [("44", "07700 900123", "+447700900123"), ("44", "020 7946 0958", "+442079460958"),
            ("49", "0151 23456789", "+4915123456789"), ("49", "030 901820", "+4930901820"),
            ("33", "06 12 34 56 78", "+33612345678"), ("33", "01 42 68 53 00", "+33142685300"),
            ("39", "347 123 4567", "+393471234567"), ("39", "06 6982 0000", "+390669820000"),
            ("7", "8 (912) 345-67-89", "+79123456789"), ("7", "8 (495) 123-45-67", "+74951234567"),
            ("52", "55 1234 5678", "+525512345678"), ("52", "81 1234 5678", "+528112345678"),
            ("91", "098765 43210", "+919876543210"), ("91", "022 2345 6789", "+912223456789"),
            ("972", "052-555-1234", "+972525551234"), ("972", "03-555-1234", "+97235551234"),
            ("61", "0412 345 678", "+61412345678"), ("61", "(02) 9876 5432", "+61298765432")]

WITH_CC = [("44", "44 7700 900123", "+447700900123"), ("49", "49 151 23456789", "+4915123456789"),
           ("33", "33 6 12 34 56 78", "+33612345678"), ("39", "39 347 123 4567", "+393471234567"),
           ("7", "7 912 345-67-89", "+79123456789"), ("52", "52 55 1234 5678", "+525512345678"),
           ("91", "91 98765 43210", "+919876543210"), ("972", "972 52-555-1234", "+972525551234"),
           ("61", "61 412 345 678", "+61412345678"),
           ("61", "0011 44 7700 900123", "+447700900123"),      # Australia's international prefix
           ("7", "8 10 44 7700 900123", "+447700900123"),       # Russia's international prefix
           ("52", "044 55 1234 5678", "+525512345678")]         # Mexico's old mobile prefix


def _norm(r, typed: str) -> str:
    try:
        return r.normalize_address(typed)
    except r.BadAddress:
        return "REFUSED"


@pytest.mark.parametrize("cc, typed, e164", NATIONAL, ids=[f"{c[0]}:{c[1]}" for c in NATIONAL])
def test_RIGHT_B6_national_number_of_the_default_country(r, monkeypatch, cc, typed, e164):
    monkeypatch.setattr(r, "DEFAULT_COUNTRY_CODE", cc)
    got = _norm(r, typed)
    print(f"\n[B6 national cc={cc}] {typed!r} -> {got}")
    assert got == e164


@pytest.mark.parametrize("cc, typed, e164", WITH_CC, ids=[f"{c[0]}:{c[1]}" for c in WITH_CC])
def test_RIGHT_B6_country_code_without_plus_is_right_or_refused(r, monkeypatch, cc, typed, e164):
    monkeypatch.setattr(r, "DEFAULT_COUNTRY_CODE", cc)
    got = _norm(r, typed)
    print(f"\n[B6 with-cc cc={cc}] {typed!r} -> {got}")
    assert got in (e164, "REFUSED")


# ---------------------------------------------------------------------------
# B7. default country 1: what a North American types every day, and odd inputs
# ---------------------------------------------------------------------------

EVERYDAY = ["555-010-0142", "(555) 010-0142", "(555)010-0142", "1 (555) 010-0142", "+1 555 010 0142",
            "555.010.0142", "1-555-010-0142", "1.555.010.0142", "+1.555.010.0142", "+1-555-010-0142",
            "tel:555-010-0142", "tel:+1-555-010-0142", "sms:+15550100142", "sms://+15550100142",
            "5550100142", "15550100142", "  555 010 0142  ", "+1 (555) 010-0142", "001 555 010 0142",
            "‪(555) 010-0142‬", "555 010 0142", "555‑010‑0142",
            "+1 555 010 0142", "1 555 010 0142", "555 010 0142", "555/010-0142"]


@pytest.mark.parametrize("typed", EVERYDAY, ids=[ascii(t) for t in EVERYDAY])
def test_RIGHT_B7_everyday_north_american_forms(r, typed):
    got = _norm(r, typed)
    print(f"\n[B7 everyday] {ascii(typed)} -> {got}")
    assert got == "+15550100142"


ODD = ["+1 555 010 0142 2", "+1 555 010 014", "+555 010 0142", "0011 44 7700 900123", "<pat@example.invalid>",
       "mailto:pat@example.invalid?subject=x", "pat@example.invalid\nwork", "+1 555 010 0142\n12",
       "555 010 0142 / 555 010 0143", "1 555 010 0142 1", "+15550100142,,22", "+1 555 010 0142 (mobile)",
       "+1 555 010 0142 - 12", "00 1234 5678", "7700 900123", "98765 43210", "911", "555010", "+1 055 010 0142",
       "+0 555 010 0142", "p:+15550100142", "e:pat@example.invalid", "+15550100142@s.example.invalid",
       "urn:biz:synthetic", "chat123456789012345678", "VERIFY22395", "1410200500", "+1 555 010 0142 / 0143"]


def test_INFO_B7_odd_inputs(r):
    print("\n[B7 odd]")
    for typed in ODD:
        print(f"   {ascii(typed):45s} -> {_norm(r, typed):22s} key={r.norm_key(typed)!r}")


def test_RIGHT_B7_two_values_in_one_field_are_not_glued(r):
    assert _norm(r, "pat@example.invalid\nwork") in ("pat@example.invalid", "REFUSED")
    assert _norm(r, "+1 555 010 0142\n12") in ("+15550100142", "REFUSED")
    assert _norm(r, "+1 555 010 0142 / 0143") in ("+15550100142", "REFUSED")
