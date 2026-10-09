"""V3 regression probe: which number a message is addressed to (R11-F2 / R11-F4).

Every test asserts the SAFE outcome, so a failure here is a demonstrated defect.
Synthetic data only: 555 numbers, documentation ranges, invented names.
"""

from __future__ import annotations

import pytest

from tests import compat_fixture as cf
from tests.fixtures import builders
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, bb, client, osa, r  # noqa: F401  (fixtures)
from tests.test_recipient_safety import _confirm, _ok, _prepare, _sends, load_cards

TEXT = "synthetic hello"


def _offered(client, name: str) -> list[str]:
    hits = client.get("/contacts/search", params={"q": name.lower()}, headers=AUTH).json()["results"]
    return [h["address"] for h in hits if h["name"] == name]


def _create(client, addr: str):
    return client.post("/create_chat", json={"addresses": [addr], "text": TEXT}, headers=AUTH)


def _match(client, addr: str) -> dict:
    return client.post("/match_chat", json={"addresses": [addr]}, headers=AUTH).json()


# --------------------------------------------------------------------------
# 0. the original scenario, all three paths, more countries than the shipped tests
# --------------------------------------------------------------------------

ABROAD = [("Noa Levi", "+972 52-555-1234", "+972525551234"),
          ("Pierre Dupont", "+33 1 99 00 12 34", "+33199001234"),
          ("Mia Clarke", "+44 7700 900123", "+447700900123"),
          ("Hans Meier", "+49 30 23125 000", "+493023125000"),
          ("Lupe Rios", "+52 55 5550 1234", "+525555501234"),
          ("Kylie Marsh", "+61 491 570 156", "+61491570156"),
          ("Shared Pref", "+972 52-555-9876X-SHARED-PHOTO-DISPLAY-PREF", "+972525559876"),
          ("Paren Plus", "+1 (555) 010-0142", "+15550100142")]


@pytest.mark.parametrize("name, card, e164", ABROAD, ids=[c[0] for c in ABROAD])
def test_original_cards_typeahead_create_and_voice(client, r, bb, osa, compat_db, monkeypatch,
                                                   name, card, e164):
    load_cards(r, monkeypatch, [(n, [c]) for n, c, _ in ABROAD])
    assert _offered(client, name) == [e164]
    bb.answers = [_ok(), _ok()]
    assert _create(client, _offered(client, name)[0]).status_code == 200
    assert _prepare(client, f"text {name} {TEXT}") == f"Send {TEXT} to {name}?"
    assert _confirm(client, "yes") == f"Sent to {name}."
    assert _sends(bb) == [("new", (e164,)), ("new", (e164,))]


# --------------------------------------------------------------------------
# 1. a card the relay refuses to read is offered as its ten-digit KEY, and the
#    key is then read as a North American number
# --------------------------------------------------------------------------

REFUSED_CARDS = [("Gemma Hale", "020 7946 0958"),         # UK national format, 11 digits
                 ("Noa Nopluss", "972 52 555 1234"),       # country code, no "+"
                 ("Otto Ext", "(555) 010-0199 x123")]      # extension


@pytest.mark.parametrize("name, card", REFUSED_CARDS, ids=[c[0] for c in REFUSED_CARDS])
def test_refused_card_is_not_sent_to_a_rebuilt_number_from_the_app(client, r, bb, osa, compat_db,
                                                                  monkeypatch, name, card):
    load_cards(r, monkeypatch, [(name, [card])])
    with pytest.raises(r.BadAddress):
        r.normalize_address(card)                    # the relay itself refuses this card
    bb.answers = [_ok()]
    for addr in _offered(client, name):              # what the app's compose screen posts back
        resp = _create(client, addr)
        print(f"\nV3-1 card {card!r}: offered {addr!r} -> /create_chat {resp.status_code}, sends {_sends(bb)}")
    assert _sends(bb) == []


@pytest.mark.parametrize("name, card", REFUSED_CARDS, ids=[c[0] for c in REFUSED_CARDS])
def test_refused_card_is_not_sent_to_by_voice(client, r, bb, osa, compat_db, monkeypatch, name, card):
    load_cards(r, monkeypatch, [(name, [card])])
    bb.answers = [_ok()]
    said = _prepare(client, f"text {name} {TEXT}")
    heard = _confirm(client, "yes")
    print(f"\nV3-2 card {card!r}: prepare {said!r}, confirm {heard!r}, sends {_sends(bb)}")
    assert _sends(bb) == []


# --------------------------------------------------------------------------
# 2. "+" that is not the first character: the country code is read as NANP digits
# --------------------------------------------------------------------------

HIDDEN_PLUS = [("tel:+6591234567", "+65"), ("(+65) 9123 4567", "+65"),
               ("‎+65 9123 4567", "+65"), ("‪+45 12 34 56 78‬", "+45"),
               ("tel:+4712345678", "+47")]


@pytest.mark.parametrize("typed, cc", HIDDEN_PLUS, ids=[ascii(t[0]) for t in HIDDEN_PLUS])
def test_a_plus_behind_a_prefix_keeps_its_country_or_is_refused(client, r, bb, osa, compat_db, typed, cc):
    bb.answers = [_ok()]
    resp = _create(client, typed)
    print(f"\nV3-3 typed {ascii(typed)}: /create_chat {resp.status_code}, sends {_sends(bb)}")
    assert resp.status_code == 400 or _sends(bb)[0][1][0].startswith(cc)


def test_a_card_with_parenthesised_plus_is_not_offered_as_north_american(client, r, compat_db, monkeypatch):
    load_cards(r, monkeypatch, [("Sing Lee", ["(+65) 9123 4567"])])
    offered = _offered(client, "Sing Lee")
    print(f"\nV3-4 card '(+65) 9123 4567': offered {offered}")
    assert not any(a.startswith("+1") for a in offered)


# --------------------------------------------------------------------------
# 3. extensions and letters
# --------------------------------------------------------------------------

@pytest.mark.parametrize("card, wrong", [("+49 30 23125 x789", "+493023125789"),
                                         ("+1 555 010 0177 ext. 22", "+1555010017722"),
                                         ("+44 20 7946 0958;12", "+44207946095812")])
def test_an_extension_is_not_glued_onto_the_number(client, r, compat_db, monkeypatch, card, wrong):
    load_cards(r, monkeypatch, [("Otto Ext", [card])])
    offered = _offered(client, "Otto Ext")
    print(f"\nV3-5 card {card!r}: offered {offered}")
    assert wrong not in offered


@pytest.mark.parametrize("card", ["1-800-FLOWERS", "555-010-HELP"])
def test_a_vanity_number_is_not_a_short_code(client, r, bb, osa, compat_db, monkeypatch, card):
    load_cards(r, monkeypatch, [("Flora Shop", [card])])
    offered = _offered(client, "Flora Shop")
    bb.answers = [_ok()]
    for addr in offered:
        _create(client, addr)
    print(f"\nV3-6 card {card!r}: offered {offered}, sends {_sends(bb)}")
    assert _sends(bb) == []


# --------------------------------------------------------------------------
# 4. typed input
# --------------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["06 12 34 56 78", "0412 345 678", "123 456 7890", "1 055 501 0123"])
def test_ten_digits_that_cannot_be_north_american_are_refused(r, typed):
    try:
        out = r.normalize_address(typed)
    except r.BadAddress:
        return
    print(f"\nV3-7 typed {typed!r} -> {out!r}")
    pytest.fail(f"{typed!r} -> {out!r}")


def test_non_ascii_digits_do_not_reach_the_engine(r):
    out = r.normalize_address("+٩٧٢٥٢٥٥٥١٢٣٤")
    print(f"\nV3-8 arabic-indic digits -> {ascii(out)}")
    assert out.isascii()


def _old(addr: str) -> str:
    """normalize_address as it was before the fix (commit 5ecd650)."""
    addr = addr.strip()
    if "@" in addr:
        return addr.lower()
    digits = "".join(ch for ch in addr if ch.isdigit())
    if not digits:
        return addr
    if addr.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits


NA_INPUTS = ["310-555-0123", "(310) 555-0123", "310.555.0123", "3105550123", "1-310-555-0123",
             "1 (310) 555-0123", "+1 310 555 0123", "+1-310-555-0123", "13105550123",
             "tel:+13105550123", "tel:3105550123", "‪+1 (310) 555-0123‬",
             "310 555 0123", " +1 310 555 0123", "32665", "911",
             "Alice@Example.INVALID", "555-0123", "44 20 7946 0958", "tel:+442079460958",
             "310 555 0123 x5", "+1 310 555 0123 x5", "AMAZON"]


def test_table_old_against_new_for_a_north_american_user(r):
    refused = []
    for typed in NA_INPUTS:
        try:
            new = r.normalize_address(typed)
        except r.BadAddress:
            new = "REFUSED"
            refused.append(typed)
        print(f"\nV3-9 {ascii(typed):42} old {_old(typed)!r:22} new {new!r}")
    print(f"\nV3-9 refused now: {[ascii(t) for t in refused]}")


def test_russian_trunk_prefix_with_default_country_7(r, monkeypatch):
    monkeypatch.setattr(r, "DEFAULT_COUNTRY_CODE", "7")
    out = r.normalize_address("8 (912) 555-01-23")
    print(f"\nV3-10 default 7, typed '8 (912) 555-01-23' -> {out!r}")
    assert out == "+79125550123"


# --------------------------------------------------------------------------
# 5. BadAddress never becomes a 500
# --------------------------------------------------------------------------

NASTY = ["Mom", "555 0123", "+12", "tel:", "x" * 300, "‪", "0", "00", "011", "+", "++1",
         "1" * 40, "+" + "1" * 40, "@", "07911 123456"]


@pytest.mark.parametrize("bad", NASTY, ids=[ascii(b)[:20] for b in NASTY])
def test_no_route_answers_500_or_sends(client, bb, osa, compat_db, bad):
    codes = [_create(client, bad).status_code,
             client.post("/match_chat", json={"addresses": [bad]}, headers=AUTH).status_code,
             client.post("/match_chat", json={"addresses": [cf.ALICE_PHONE, bad]}, headers=AUTH).status_code,
             client.post("/create_chat", json={"addresses": [cf.ALICE_PHONE, bad], "text": TEXT},
                         headers=AUTH).status_code,
             client.post("/v/prepare", data={"query": f"text {bad} hi"}, headers=AUTH).status_code,
             client.post("/assistant/prepare", json={"query": f"text {bad} hi"}, headers=AUTH).status_code]
    print(f"\nV3-11 {ascii(bad)[:24]}: {codes} sends {_sends(bb)}")
    assert all(c < 500 for c in codes)
    if bad != "@":
        assert bb.calls == [] and osa.calls == []


# --------------------------------------------------------------------------
# 6. the comparison key is still the last ten digits
# --------------------------------------------------------------------------

US_STRANGER = "+12525551234"          # North Carolina; same last ten digits as +972 52 555 1234


@pytest.fixture
def stranger_chat(compat_db):
    w = compat_db.writer
    h = builders.add_handle(w, US_STRANGER)
    chat = builders.add_chat(w, f"iMessage;-;{US_STRANGER}", 45, US_STRANGER, handles=[h])
    builders.add_message(w, chat, guid="V3-US-1", text="synthetic wrong-number text", handle=h)
    return f"iMessage;-;{US_STRANGER}"


def test_contact_abroad_is_not_matched_to_a_us_chat_with_the_same_last_ten(client, r, bb, osa, monkeypatch,
                                                                          stranger_chat):
    load_cards(r, monkeypatch, [("Noa Levi", ["+972 52-555-1234"])])
    addr, = _offered(client, "Noa Levi")
    assert addr == "+972525551234"
    m = _match(client, addr)                       # what ComposeScreen and FaceTimeScreen ask first
    bb.answers = [_ok()]
    resp = _create(client, addr)
    print(f"\nV3-12 picked Noa {addr}: match {m.get('found')} {m.get('chat_guid')} name {m.get('chat_name')!r}; "
          f"create {resp.json()}; sends {_sends(bb)}; resolve(US) = {r.resolve(US_STRANGER)!r}")
    assert _sends(bb) == [("new", ("+972525551234",))]


def test_voice_to_contact_abroad_does_not_go_into_the_us_chat(client, r, bb, osa, monkeypatch, stranger_chat):
    load_cards(r, monkeypatch, [("Noa Levi", ["+972 52-555-1234"])])
    bb.answers = [_ok()]
    said = _prepare(client, f"text Noa Levi {TEXT}")
    heard = _confirm(client, "yes")
    print(f"\nV3-13 voice: {said!r} / {heard!r}; sends {_sends(bb)}")
    assert _sends(bb) == [("new", ("+972525551234",))]


def test_two_cards_with_the_same_last_ten_digits(client, r, compat_db, monkeypatch):
    load_cards(r, monkeypatch, [("Ann Carolina", ["+1 252 555 1234"]), ("Noa Levi", ["+972 52-555-1234"])])
    a, n = _offered(client, "Ann Carolina"), _offered(client, "Noa Levi")
    print(f"\nV3-14 two cards: Ann offered {a}, Noa offered {n}, names {sorted(set(r.CONTACTS.values()))}")
    assert a == ["+12525551234"] and n == ["+972525551234"]
