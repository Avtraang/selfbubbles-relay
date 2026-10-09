"""Reviewer B probes, third batch: the owner's own card, and numbers of the wrong length."""

from __future__ import annotations

import pytest

from tests import compat_fixture as cf
from tests.fixtures import builders
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, bb, client, osa, r  # noqa: F401  (fixtures)
from tests.test_identity_attack import HOME, SAM, TEXT, _ok, _sends, _voice, load_cards

X, Y = "+15550100301", "+15550100302"
OWN_MAIL = "owner@example.invalid"


def _group(w, guid, ident, members, tag):
    chat = builders.add_chat(w, guid, 43, ident, handles=members)
    builders.add_message(w, chat, guid=f"B3-{tag}", text=f"synthetic to {tag}", is_from_me=1)


def _create(client, bb, *addresses):
    bb.answers = [_ok()]
    resp = client.post("/create_chat", json={"addresses": list(addresses), "text": TEXT}, headers=AUTH)
    return resp.status_code, _sends(bb)


# B1e. the owner's card and a relative's card with the same name, the relative only on the home line
def test_RIGHT_B1e_a_namesake_of_the_owner_is_not_the_owner(r, client, bb, osa, compat_db, monkeypatch):
    r.SELF_RAW[:] = [cf.SELF_PHONE]
    w = compat_db.writer
    _group(w, "iMessage;+;chat9201", "chat9201", [HOME, X], "home-and-x")
    load_cards(r, monkeypatch, [("Me Owner", [cf.SELF_PHONE, HOME]), ("Me Owner", [HOME])])
    code, sends = _create(client, bb, X)
    print(f"\n[B1e] person_key(HOME)={r.person_key(HOME)!r} person_key(SELF)={r.person_key(cf.SELF_PHONE)!r} | "
          f"/create_chat([X]) {code} sent {sends} (chat9201 = HOME + X)")
    assert sends == [("new", (X,))]


# B12. the owner's phone is also on the spouse's card: is the owner's e-mail still the owner?
def test_RIGHT_B12_owners_other_identity_is_still_the_owner(r, client, bb, osa, compat_db, monkeypatch):
    # The relay cannot tell which of two cards holding the owner's number is the owner's own, so it no
    # longer takes the rest of either card for the owner: IMSG_SELF has to list every one of the owner's
    # addresses (a cutover check). With both listed, the group is found.
    r.SELF_RAW[:] = [cf.SELF_PHONE, OWN_MAIL]
    w = compat_db.writer
    _group(w, "iMessage;+;chat9202", "chat9202", [OWN_MAIL, X, Y], "me-x-y")
    load_cards(r, monkeypatch, [("Me Owner", [cf.SELF_PHONE, OWN_MAIL]), ("Sam Quinn", [SAM, cf.SELF_PHONE])])
    code, sends = _create(client, bb, X, Y)
    print(f"\n[B12] person_key(OWN_MAIL)={r.person_key(OWN_MAIL)!r} person_key(SELF)={r.person_key(cf.SELF_PHONE)!r} | "
          f"/create_chat([X, Y]) {code} sent {sends} (chat9202 = owner's e-mail + X + Y)")
    assert sends == [("text", "iMessage;+;chat9202")]


# B13. a spoken number one digit short, and a message that starts with a digit
def test_RIGHT_B13_spoken_number_does_not_borrow_a_digit_from_the_message(r, client, bb, osa, compat_db):
    said, done = _voice(client, bb, "text 555 010 014 2 tickets please")
    print(f"\n[B13] prepare -> {said!r} | yes -> {done!r} | sent {_sends(bb)}")
    assert _sends(bb) == []


# B14. "+1" numbers that are not eleven digits
@pytest.mark.parametrize("typed", ["+1 555 010 014", "+1 555 010 0142 2", "+1 555 010 0142 / 0143"])
def test_RIGHT_B14_plus_one_number_of_the_wrong_length_is_refused(r, client, bb, osa, compat_db, typed):
    code, sends = _create(client, bb, typed)
    print(f"\n[B14 {typed!r}] /create_chat {code} sent {sends}")
    assert sends == []


# B1f. as B1e, with a group of three: HOME (the owner's namesake) + X + Y
def test_RIGHT_B1f_message_for_x_and_y_is_not_sent_into_the_group_with_the_namesake(r, client, bb, osa, compat_db,
                                                                                 monkeypatch):
    r.SELF_RAW[:] = [cf.SELF_PHONE]
    w = compat_db.writer
    _group(w, "iMessage;+;chat9203", "chat9203", [HOME, X, Y], "home-x-y")
    load_cards(r, monkeypatch, [("Me Owner", [cf.SELF_PHONE, HOME]), ("Me Owner", [HOME])])
    code, sends = _create(client, bb, X, Y)
    print(f"\n[B1f] person_key(HOME)={r.person_key(HOME)!r} person_key(SELF)={r.person_key(cf.SELF_PHONE)!r} | "
          f"/create_chat([X, Y]) {code} sent {sends} (chat9203 = HOME + X + Y)")
    assert sends == [("new", (X, Y))]


# B1g. control for B1f: the same cards under two DIFFERENT names
def test_RIGHT_B1g_control_different_names(r, client, bb, osa, compat_db, monkeypatch):
    r.SELF_RAW[:] = [cf.SELF_PHONE]
    w = compat_db.writer
    _group(w, "iMessage;+;chat9204", "chat9204", [HOME, X, Y], "home-x-y")
    load_cards(r, monkeypatch, [("Me Owner", [cf.SELF_PHONE, HOME]), ("Pa Owner", [HOME])])
    code, sends = _create(client, bb, X, Y)
    print(f"\n[B1g control] /create_chat([X, Y]) {code} sent {sends}")
    assert sends == [("new", (X, Y))]


# B13b. a complete spoken number and a message that starts with a digit
def test_INFO_B13b_full_number_then_a_digit(r, client, bb, osa, compat_db):
    said, done = _voice(client, bb, "text 555 010 0142 2 tickets please")
    print(f"\n[B13b] prepare -> {said!r} | yes -> {done!r} | sent {_sends(bb)}")
    said, done = _voice(client, bb, "text 555 0142 100 dollars is fine")
    print(f"[B13c] prepare -> {said!r} | yes -> {done!r} | sent {_sends(bb)}")
