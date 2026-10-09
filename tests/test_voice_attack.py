"""Reviewer A: attacks on the voice confirmation. Every test asserts the SAFE
behaviour the owner requires, so a failure here is a defect in the fix.
In-process, synthetic contacts and 555 numbers only."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests import compat_fixture as cf
from tests.test_recipient_safety import TEXT, _confirm, _ok, _prepare, _sends, load_cards
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, bb, client, osa, r  # noqa: F401  (fixtures)

A, B = "+15550100101", "+15550100102"

# --- yes/no round: answers made ONLY of allowed tokens that are not a clear yes ---
POLITE_REFUSAL = ["that's ok", "that's okay", "That's OK, thanks", "that's okay, thank you", "that's ok thank you"]
CORRECTION = ["correct that", "correct it", "correct that please", "ok correct that", "ok, correct it please",
              "ok now do that right", "do it right", "send it right"]
QUESTION = ["right now?", "that right?", "send that?", "do you send it now?", "sure?", "ok?", "send it now?",
            "sure that's right?"]
SARCASM = ["yeah right", "sure sure sure", "yeah yeah sure"]


def _must_cancel(client, bb, osa, answer):
    bb.answers = [_ok()]
    assert _prepare(client, f"text Alice Anders {TEXT}") == f"Send {TEXT} to Alice Anders?"
    said = _confirm(client, answer)
    assert (said, _sends(bb)) == ("Cancelled.", []), f"answer {answer!r} -> {said!r} sends={_sends(bb)}"


@pytest.mark.parametrize("answer", POLITE_REFUSAL)
def test_polite_refusal_cancels(client, bb, osa, compat_db, answer):
    _must_cancel(client, bb, osa, answer)


@pytest.mark.parametrize("answer", CORRECTION)
def test_correction_cancels(client, bb, osa, compat_db, answer):
    _must_cancel(client, bb, osa, answer)


@pytest.mark.parametrize("answer", QUESTION)
def test_question_cancels(client, bb, osa, compat_db, answer):
    _must_cancel(client, bb, osa, answer)


@pytest.mark.parametrize("answer", SARCASM)
def test_sarcasm_cancels(client, bb, osa, compat_db, answer):
    _must_cancel(client, bb, osa, answer)


# --- transport oddities for "answer" on the JSON pair ---
@pytest.mark.parametrize("body, query", [
    ({"answer": ["no thanks", "yes"][1:]}, ""),          # a list holding yes
    ({"answer": {"ok": "sure"}}, ""),                     # an object
    ({"answer": ""}, "&answer=yes"),                      # empty spoken answer, stale query parameter
    ({"answer": None}, "&answer=yes"),
    ({"answer": False}, "&answer=yes"),
])
def test_json_answer_that_is_not_a_spoken_yes_cancels(client, bb, osa, compat_db, body, query):
    bb.answers = [_ok()]
    first = client.post("/assistant/prepare", json={"query": f"text Alice Anders {TEXT}"}, headers=AUTH).json()
    body = dict(body, token=first["token"])
    second = client.post("/assistant/confirm?x=1" + query, json=body, headers=AUTH).json()
    assert (second["status"], _sends(bb)) == ("cancelled", []), second


# --- which-one round ---
def _choose(*names):
    addrs = [A, B, "+15550100103"]
    return {"kind": "choose", "text": TEXT,
            "candidates": [(0.70 - i / 100, n, [addrs[i]]) for i, n in enumerate(names)]}


@pytest.mark.parametrize("answer", ["that's ok", "that's okay, thanks", "correct that", "right now?"])
def test_single_candidate_round_not_a_yes_cancels(r, bb, osa, compat_db, answer):
    bb.answers = [_ok()]
    res = asyncio.run(r.assistant_deliver(_choose("Noah Fixture"), answer))
    assert (res["status"], _sends(bb)) == ("cancelled", []), (answer, res, _sends(bb))


@pytest.mark.parametrize("answer, names", [
    ("Wei", ("李 Wei", "Wei Chen")),                 # half of a mixed-script name counts as "said in full"
    ("Sam", ("Sam", "Sam Quinn")),                   # a first name both candidates have
    ("skip", ("Skip Fixture", "Olga Other")),        # an ordinary word that is a name word
    ("other", ("Noah Fixture", "Olga Other")),
    ("nope", ("Nope", "Olga Other")),                # a refusal word that is a whole name
    ("cancel", ("Cancel", "Olga Other")),
    ("stop", ("Stop", "Olga Other")),
])
def test_which_one_round_ambiguous_or_refusing_answer_cancels(r, bb, osa, compat_db, answer, names):
    bb.answers = [_ok()]
    res = asyncio.run(r.assistant_deliver(_choose(*names), answer))
    assert (res["status"], _sends(bb)) == ("cancelled", []), (answer, names, res, _sends(bb))


# --- namesakes that sound the same but are written with different spacing/punctuation inside a word ---
@pytest.mark.parametrize("first, second, spoken", [
    ("Sam O'Brien", "Sam OBrien", "Sam O'Brien"),
    ("Sam O'Brien", "Sam OBrien", "Sam OBrien"),
    ("Mary Ann Lee", "Maryann Lee", "Mary Ann Lee"),
    ("Mary Ann Lee", "Maryann Lee", "Maryann Lee"),
    ("Sam McDonald", "Sam Mc Donald", "Sam McDonald"),
    ("Jean-Luc Fixture", "Jeanluc Fixture", "Jean-Luc Fixture"),
])
def test_cards_that_sound_the_same_are_refused(r, client, bb, osa, compat_db, monkeypatch, first, second, spoken):
    bb.answers = [_ok()]
    load_cards(r, monkeypatch, [(first, [A]), (second, [B])])
    said = _prepare(client, f"text {spoken} {TEXT}")
    after = _confirm(client, "yes")
    assert said.startswith("You have more than one contact called ") and _sends(bb) == [], (said, after, _sends(bb))


# --- state across requests: a prepare that fails must not leave the previous message armed ---
def test_failed_prepare_does_not_leave_the_previous_message_armed(r, bb, osa, compat_db, monkeypatch):
    bb.answers = [_ok()]
    quiet = TestClient(r.app, raise_server_exceptions=False)
    assert _prepare(quiet, f"text Alice Anders {TEXT}") == f"Send {TEXT} to Alice Anders?"

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:                     # one transient read error on chat.db
        m.setattr(r, "one_to_one_activity", locked)
        resp = quiet.post("/v/prepare", data={"query": "text Bob Brown a different synthetic text"}, headers=AUTH)
        assert resp.status_code == 500
    said = _confirm(quiet, "yes")                        # the caller is answering the Bob request
    assert (said, _sends(bb)) == ("There's nothing waiting to send.", []), (said, _sends(bb))


def test_cancel_then_yes_and_double_confirm_send_nothing_more(client, bb, osa, compat_db):
    bb.answers = [_ok(), _ok()]
    _prepare(client, f"text Alice Anders {TEXT}")
    assert _confirm(client, "yes") == "Sent to Alice Anders."
    assert _confirm(client, "yes") == "There's nothing waiting to send."
    assert len(_sends(bb)) == 1


# --- a namesake whose card holds only a number the relay refuses to guess at ---
@pytest.mark.parametrize("unusable", ["555-0102", "020 7946 0018"])
def test_namesake_whose_only_number_is_refused_is_still_a_namesake(r, client, bb, osa, compat_db, monkeypatch, unusable):
    bb.answers = [_ok()]
    load_cards(r, monkeypatch, [("Chris Park", [A]), ("Chris Park", [unusable])])
    said = _prepare(client, f"text Chris Park {TEXT}")
    after = _confirm(client, "yes")
    assert said.startswith("You have more than one contact called ") and _sends(bb) == [], (said, after, _sends(bb))


# --- two cards with one name, the second holding only the address they share ---
def test_same_name_cards_sharing_a_landline_are_not_one_person(r, client, bb, osa, compat_db, monkeypatch):
    bb.answers = [_ok()]
    landline = "+15550100199"
    load_cards(r, monkeypatch, [("Sam Quinn", [A, landline]), ("Sam Quinn", [landline])])
    said = _prepare(client, f"text Sam Quinn {TEXT}")
    after = _confirm(client, "yes")
    assert said.startswith("You have more than one contact called ") and _sends(bb) == [], (said, after, _sends(bb))


# --- beyond the stated rule: two spellings of one spoken name (speech-to-text picks the spelling) ---
@pytest.mark.parametrize("first, second, spoken", [
    ("Sarah Park", "Sara Park", "Sarah Park"), ("Jon Smith", "John Smith", "John Smith")])
def test_homophone_cards_are_refused(r, client, bb, osa, compat_db, monkeypatch, first, second, spoken):
    bb.answers = [_ok()]
    load_cards(r, monkeypatch, [(first, [A]), (second, [B])])
    said = _prepare(client, f"text {spoken} {TEXT}")
    assert said.startswith("You have more than one contact called "), said


# --- an answer cut off after its first words (interruption) ---
@pytest.mark.parametrize("answer", ["ok do", "yes that's", "ok that", "sure you", "ok it's", "right that's"])
def test_cut_off_answer_cancels(client, bb, osa, compat_db, answer):
    _must_cancel(client, bb, osa, answer)


# --- the single-suggestion round reached through the real routes ---
def test_route_single_suggestion_polite_refusal_cancels(r, client, bb, osa, compat_db, monkeypatch):
    bb.answers = [_ok()]
    load_cards(r, monkeypatch, [("Noah Fixture", [A])])
    said = _prepare(client, f"text Nora {TEXT}")
    after = _confirm(client, "that's ok")
    assert (said.startswith("Did you mean Noah Fixture?"), after.endswith("Cancelled."), _sends(bb)) == (True, True, []), (said, after, _sends(bb))
