"""The voice confirmation sends on a plain yes and on nothing else.

Second pass (regression check of 2026-10-08). The first fix cancelled on a list
of refusal words, and a reviewer got through it with refusals that were not on
the list ("that isn't right", "I can't send that", "ok hold on"), with ordinary
words that resemble a candidate's name in the which-one round ("nah" against
Noah), and with two cards whose names differ only in spacing or an accent.
The rule tested here is the other way round: every word of the answer must be
part of a yes, and a name must be said, not resembled.

In-process and synthetic: the real routes, the real send chain into the
suite's recording BlueBubbles client, contact cards through load_contacts().
"""

from __future__ import annotations

import asyncio

import pytest

from tests import compat_fixture as cf
from tests.test_recipient_safety import TEXT, _confirm, _ok, _prepare, _sends, load_cards
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, bb, client, osa, r  # noqa: F401  (fixtures)

A, B = "+15550100101", "+15550100102"

# Every one of these was sent by the previous fix (or is a close relative of one that was).
NOT_A_YES = [
    "that isn't right", "that isnt right", "I can't send that", "you shouldn't send it", "cannot confirm",
    "send it to Jon Smith instead", "ok hold on", "yeah nah", "ok but change it first", "you sure",
    "are you sure", "send it tomorrow", "ok so who is that", "sure? I didn't say that", "ok wait",
    "yes... actually no", "right, wrong person", "correct me if I'm wrong", "it's not ok", "okay but not him",
    "send to", "yes or no", "maybe", "I guess", "right away sir", "do it later", "sure thing buddy",
    "yes да", "ok ‏לא", "yes 2", "",
]


@pytest.mark.parametrize("answer", NOT_A_YES)
def test_anything_but_a_plain_yes_cancels(client, bb, osa, compat_db, answer):
    assert _prepare(client, f"text Alice Anders {TEXT}") == f"Send {TEXT} to Alice Anders?"
    assert _confirm(client, answer) == "Cancelled."
    assert bb.calls == [] and osa.calls == []
    assert _confirm(client, "yes") == "There's nothing waiting to send."
    assert bb.calls == [] and osa.calls == []


PLAIN_YES = ["yes", "Yes.", "YES!", "yeah", "yep", "yup", "ok", "okay", "sure", "confirm", "correct", "right",
             "send", "send it", "do it", "yes please", "yes send it", "ok send it", "yeah send it", "yes do it",
             "that's right", "that's correct", "ok thanks", "yes thank you", "sure, send it"]


@pytest.mark.parametrize("answer", PLAIN_YES)
def test_a_plain_yes_still_sends(client, bb, osa, compat_db, answer):
    bb.answers = [_ok()]
    assert _prepare(client, f"text Alice Anders {TEXT}") == f"Send {TEXT} to Alice Anders?"
    assert _confirm(client, answer) == "Sent to Alice Anders."
    (kind, target), = _sends(bb)
    assert kind == "text" and (cf.ALICE_PHONE in target or cf.ALICE_EMAIL in target)


@pytest.mark.parametrize("answer", ["that isn't right", "I can't send that", "ok hold on", "you sure", ""])
def test_the_json_pair_follows_the_same_rule(client, bb, osa, compat_db, answer):
    first = client.post("/assistant/prepare", json={"query": f"text Alice Anders {TEXT}"}, headers=AUTH).json()
    assert first["status"] == "confirm" and first["token"]
    second = client.post("/assistant/confirm", json={"token": first["token"], "answer": answer}, headers=AUTH).json()
    assert (second["ok"], second["status"]) == (False, "cancelled")
    assert bb.calls == [] and osa.calls == []
    again = client.post("/assistant/confirm", json={"token": first["token"], "answer": "yes"}, headers=AUTH).json()
    assert again["status"] == "expired" and bb.calls == []


def test_the_json_pair_sends_on_a_plain_yes(client, bb, osa, compat_db):
    bb.answers = [_ok()]
    first = client.post("/assistant/prepare", json={"query": f"text Alice Anders {TEXT}"}, headers=AUTH).json()
    second = client.post("/assistant/confirm", json={"token": first["token"], "answer": "yes"}, headers=AUTH).json()
    assert (second["ok"], second["status"]) == (True, "sent") and len(_sends(bb)) == 1


# ---------------------------------------------------------------------------
# the which-one round: a name is said, not resembled
# ---------------------------------------------------------------------------

def _choose(first: str, second: str = "Olga Other") -> dict:
    return {"kind": "choose", "text": TEXT, "candidates": [(0.70, first, [A]), (0.68, second, [B])]}


@pytest.mark.parametrize("answer, lookalike", [
    ("nah", "Noah Fixture"), ("yes", "Wes Fixture"), ("yeah", "Leah Fixture"), ("sure", "Sue Fixture"),
    ("um", "Uma Fixture"), ("what", "Walt Fixture"), ("huh", "Hugh Fixture"), ("sorry", "Cory Fixture"),
    ("hold on", "Holden Fixture"), ("no way", "Noah Wayne"), ("okay", "Kay Fixture"), ("right", "Wright Fixture")])
def test_an_ordinary_word_is_not_taken_for_a_name_it_resembles(r, bb, osa, compat_db, answer, lookalike):
    res = asyncio.run(r.assistant_deliver(_choose(lookalike), answer))
    assert res["status"] == "cancelled"
    assert bb.calls == [] and osa.calls == []


@pytest.mark.parametrize("answer, first, second, sent_to", [
    ("Noah Fixture", "Noah Fixture", "Nora Fixture", A), ("nora fixture", "Noah Fixture", "Nora Fixture", B),
    ("Nora Fixture.", "Noah Fixture", "Nora Fixture", B), ("Maria Cancel", "Maria Cancel", "Mario Fixture", A),
    ("No Frills Market", "Olga Other", "No Frills Market", B), ("Zoë Fixture", "Zoe Fixture", "Olga Other", A)])
def test_a_full_name_that_is_said_is_chosen(r, bb, osa, compat_db, answer, first, second, sent_to):
    bb.answers = [_ok()]
    res = asyncio.run(r.assistant_deliver(_choose(first, second), answer))
    assert res["status"] == "sent"
    assert _sends(bb) == [("new", (sent_to,))]


@pytest.mark.parametrize("answer, first, second", [
    ("Fixture", "Noah Fixture", "Nora Fixture"),            # a word both names have
    ("Noah", "Noah Fixture", "Nora Fixture"),               # part of a name: also an ordinary word, as often as not
    ("skip", "Skip Fixture", "Olga Other"), ("other", "Noah Fixture", "Olga Other"),
    ("Sam", "Sam", "Sam Quinn"), ("stop", "Stop", "Olga Other"), ("cancel", "Cancel", "Olga Other"),
    ("Chris Park", "Chris Park 2", "Chris Park"),            # two names that are one when spoken
    ("Chris Park", "Chris-Park", "Chris Park"),
    ("Noah or Nora", "Noah Fixture", "Nora Fixture"),
    ("not Noah", "Noah Fixture", "Nora Fixture"),
    ("Noah Smith", "Noah Fixture", "Nora Fixture")])         # a word no candidate has
def test_an_answer_that_does_not_name_exactly_one_candidate_cancels(r, bb, osa, compat_db, answer, first, second):
    res = asyncio.run(r.assistant_deliver(_choose(first, second), answer))
    assert res["status"] == "cancelled"
    assert bb.calls == [] and osa.calls == []


# ---------------------------------------------------------------------------
# namesakes whose names differ only in spacing, punctuation or an accent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("first, second, spoken", [
    ("Chris Park", "Chris  Park", "Chris Park"), ("Chris Park", "Chris Park ", "Chris Park"),
    ("Chris Park", "Chris Park.", "Chris Park"), ("Zoe Fixture", "Zoë Fixture", "Zoe Fixture"),
    ("Chris Park", "chris park", "Chris Park"), ("Chris Park", "Chris-Park", "Chris Park")])
def test_voice_refuses_two_cards_whose_names_sound_the_same(r, client, bb, osa, compat_db, monkeypatch,
                                                            first, second, spoken):
    load_cards(r, monkeypatch, [(first, [A]), (second, [B])])
    said = _prepare(client, f"text {spoken} {TEXT}")
    assert said.startswith("You have more than one contact called ")
    assert _confirm(client, "yes") == "There's nothing waiting to send."
    assert bb.calls == [] and osa.calls == []
