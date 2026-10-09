"""Reviewer B probes, second batch. test_RIGHT_* asserts the required outcome (a failure is a defect)."""

from __future__ import annotations

import itertools

import pytest

from tests.fixtures import builders
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, bb, client, osa, r  # noqa: F401  (fixtures)
from tests.test_identity_attack import (HOME, MOBILE, SAM, TEXT, _ok, _one, _sends, _voice, load_cards)

X = "+15550100301"
G_SENIOR, G_JUNIOR = "iMessage;+;chat9101", "iMessage;+;chat9102"


def _group(w, guid, ident, members, tag):
    chat = builders.add_chat(w, guid, 43, ident, handles=members)
    builders.add_message(w, chat, guid=f"B2-{tag}", text=f"synthetic to {tag}", is_from_me=1)


def _create(client, bb, *addresses):
    bb.answers = [_ok()]
    resp = client.post("/create_chat", json={"addresses": list(addresses), "text": TEXT}, headers=AUTH)
    return resp.status_code, _sends(bb)


# B1c. two cards whose DISPLAY NAMES DIFFER (digits / another script) but "sound" the same
@pytest.mark.parametrize("first, second", [("Noa לוי", "Noa כהן"),
                                           ("Front Desk 1", "Front Desk 2"),
                                           ("Sam (Work)", "Sam ❤")],
                         ids=["hebrew-surnames", "digits", "punctuation-emoji"])
def test_RIGHT_B1c_cards_with_different_names_are_two_people(r, client, bb, osa, compat_db, monkeypatch,
                                                            first, second):
    w = compat_db.writer
    _one(w, HOME, "first")
    _one(w, MOBILE, "second")
    load_cards(r, monkeypatch, [(first, [HOME]), (second, [HOME, MOBILE])])
    code, sends = _create(client, bb, HOME)
    print(f"\n[B1c {ascii(first)} / {ascii(second)}] name_key {r.name_key(first)!r} / {r.name_key(second)!r} | "
          f"person_key HOME={r.person_key(HOME)!r} MOBILE={r.person_key(MOBILE)!r} | /create_chat([HOME]) {code} sent {sends}")
    assert r.person_key(HOME) != r.person_key(MOBILE)
    assert sends == [("text", f"iMessage;-;{HOME}")]


# B1d. the same fold, through a group
def test_RIGHT_B1d_group_with_the_home_line_is_not_the_group_with_the_mobile(r, client, bb, osa, compat_db,
                                                                          monkeypatch):
    w = compat_db.writer
    _group(w, G_JUNIOR, "chat9102", [MOBILE, X], "junior-group")
    load_cards(r, monkeypatch, [("Chris Park", [HOME]), ("Chris Park", [HOME, MOBILE])])
    code, sends = _create(client, bb, HOME, X)
    print(f"\n[B1d group] /create_chat([HOME, X]) {code} sent {sends} (chat9102 = MOBILE + X)")
    assert sends == [("new", (HOME, X))]


# B2b. a namesake whose only number is ALSO on a later card with another name
@pytest.mark.parametrize("order", ["dana-last", "dana-first"])
def test_RIGHT_B2b_voice_refuses_namesakes_whatever_order_the_cards_come_in(r, client, bb, osa, compat_db,
                                                                          monkeypatch, order):
    _one(compat_db.writer, MOBILE, "chris-a")
    chris = [("Chris Park", [MOBILE]), ("Chris Park", [HOME])]
    dana = [("Dana Roe", [HOME, SAM])]
    load_cards(r, monkeypatch, chris + dana if order == "dana-last" else dana + chris)
    said, done = _voice(client, bb, f"text Chris Park {TEXT}")
    print(f"\n[B2b {order}] namesakes={getattr(r, 'namesake_names', lambda: 'n/a')()} | prepare -> {said!r} | "
          f"yes -> {done!r} | sent {_sends(bb)}")
    assert _sends(bb) == []


# B2c. the card that carries exactly the spoken name has a refused number; a similar name exists
def test_RIGHT_B2c_voice_does_not_offer_a_resembling_contact_for_a_known_name(r, client, bb, osa, compat_db,
                                                                             monkeypatch):
    _one(compat_db.writer, MOBILE, "hall")
    load_cards(r, monkeypatch, [("Gemma Hale", ["07700 900123"]), ("Gemma Hall", [MOBILE])])
    said, done = _voice(client, bb, f"text Gemma Hale {TEXT}")
    print(f"\n[B2c] prepare -> {said!r} | yes -> {done!r} | sent {_sends(bb)}")
    assert _sends(bb) == []


# B8. the same three same-name cards in every order: is "who is one person" the same?
def test_RIGHT_B8_identity_does_not_depend_on_card_order(r, compat_db, monkeypatch):
    cards = [("Chris Park", [HOME]), ("Chris Park", [MOBILE]), ("Chris Park", [HOME, MOBILE])]
    seen = {}
    for perm in itertools.permutations(cards):
        load_cards(r, monkeypatch, list(perm))
        same = r.person_key(HOME) == r.person_key(MOBILE)
        seen.setdefault((same, tuple(sorted(getattr(r, "CONTACT_SHARED", ())))), []).append(
            [len(c[1]) for c in perm])
    print(f"\n[B8] (HOME is MOBILE's person, shared keys) -> card orders by size: {seen}")
    assert len(seen) == 1


# B11. chain: A-B share x, B-C share y (three names)
def test_RIGHT_B11_chain_of_shared_numbers_keeps_three_people(r, client, bb, osa, compat_db, monkeypatch):
    a, b, c, x, y = "+15550100401", "+15550100402", "+15550100403", "+15550100411", "+15550100412"
    w = compat_db.writer
    for addr in (x, y, a, b, c):
        _one(w, addr, addr[-3:])
    cards = [("Ann Ash", [a, x]), ("Bo Birch", [x, b, y]), ("Cy Cole", [y, c])]
    for perm in itertools.permutations(cards):
        load_cards(r, monkeypatch, list(perm))
        keys = [r.person_key(k) for k in (a, b, c, x, y)]
        assert len(set(keys)) == 5, (perm, keys)
        for addr in (a, b, c, x, y):
            got = client.post("/match_chat", json={"addresses": [addr]}, headers=AUTH).json()
            assert got["chat_guid"] == f"iMessage;-;{addr}", (perm, addr, got)
    said, done = _voice(client, bb, f"text Bo Birch {TEXT}")
    print(f"\n[B11] voice to Bo -> {said!r} / {done!r} / {_sends(bb)}")
    assert _sends(bb) == [("text", f"iMessage;-;{b}")]
