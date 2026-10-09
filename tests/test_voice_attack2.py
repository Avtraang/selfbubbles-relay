"""Reviewer A2 (third look at the voice confirmation). Every test asserts the
SAFE behaviour the owner requires, so a failure here is a defect.
In-process, synthetic names and 555 / documentation numbers only."""

from __future__ import annotations

import pytest

from tests.test_recipient_safety import TEXT, _confirm, _ok, _prepare, _sends, load_cards
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, bb, client, osa, r  # noqa: F401  (fixtures)

A, B, C = "+15550100101", "+15550100102", "+15550100103"
REFUSAL = "You have more than one contact called "

# --- 3. two cards a listener cannot tell apart ------------------------------------------------
# (what speech-to-text writes, the card the owner means). Both are read back by the phone with the same sound.
SOUND_ALIKE = [
    ("Lee Fixture", "Leigh Fixture"), ("Ashley Park", "Ashleigh Park"), ("Kaylee Park", "Kayleigh Park"),
    ("Sam Thompson", "Sam Thomson"), ("Pat Rogers", "Pat Rodgers"), ("Malcolm Fixture", "Malcom Fixture"),
    ("Lindsay Park", "Linsey Park"), ("Sam Samson", "Sam Sampson"), ("Bridget Park", "Brigit Park"),
    ("Pat Doherty", "Pat Dougherty"), ("Sam Simpson", "Sam Simson"), ("Pat Holmes", "Pat Homes"),
    ("Lee", "Leigh"), ("Ashley", "Ashleigh"),
]


@pytest.mark.parametrize("heard, meant", SOUND_ALIKE)
def test_cards_that_sound_the_same_are_refused(r, client, bb, osa, compat_db, monkeypatch, heard, meant):
    bb.answers = [_ok()]
    load_cards(r, monkeypatch, [(heard, [A]), (meant, [B])])
    said = _prepare(client, f"text {heard} {TEXT}")      # the owner said the name of `meant`
    after = _confirm(client, "yes")                      # ...and hears that same sound read back
    assert said.startswith(REFUSAL) and _sends(bb) == [], (said, after, _sends(bb))


# --- 1. an answer that is not a clear yes -------------------------------------------------------
@pytest.mark.parametrize("answer", ["sure？", "ok？", "right﹖", "right⁇", "¿ok", "ok؟"])
def test_a_question_cancels_whatever_question_mark_it_is_written_with(client, bb, osa, compat_db, answer):
    bb.answers = [_ok()]
    assert _prepare(client, f"text Alice Anders {TEXT}") == f"Send {TEXT} to Alice Anders?"
    said = _confirm(client, answer)
    assert (said, _sends(bb)) == ("Cancelled.", []), (answer, said, _sends(bb))


def test_empty_answer_in_a_form_body_decides_over_the_query_string(client, bb, osa, compat_db):
    bb.answers = [_ok()]
    assert _prepare(client, f"text Alice Anders {TEXT}") == f"Send {TEXT} to Alice Anders?"
    resp = client.post("/v/confirm?answer=yes", data={"answer": ""}, headers=AUTH)   # body: answer=
    assert (resp.text, _sends(bb)) == ("Cancelled.", []), (resp.text, _sends(bb))


def test_empty_answer_in_a_form_body_decides_on_the_json_pair_too(client, bb, osa, compat_db):
    bb.answers = [_ok()]
    first = client.post("/assistant/prepare", json={"query": f"text Alice Anders {TEXT}"}, headers=AUTH).json()
    second = client.post(f"/assistant/confirm?token={first['token']}&answer=yes", data={"answer": ""},
                         headers=AUTH).json()
    # nothing is sent: the empty answer in the body is not replaced by the yes in the URL (the token in the
    # URL is not read either once the body speaks, so the answer is "expired"; "cancelled" would do as well)
    assert second["status"] in ("cancelled", "expired") and _sends(bb) == [], (second, _sends(bb))


# --- a spoken number: the digits of the message are not part of it ------------------------------
@pytest.mark.parametrize("sentence, number_said, wrong", [
    ("text +44 7700 900123 2 tickets please", "+447700900123", "+4477009001232"),
    ("text +49 30 901820 10 people are coming", "+4930901820", "+493090182010"),
    ("text +972 52 555 1234 5 minutes", "+972525551234", "+9725255512345"),
])
def test_a_plus_number_does_not_take_digits_of_the_message(client, bb, osa, compat_db, sentence, number_said, wrong):
    bb.answers = [_ok()]
    said = _prepare(client, sentence)
    after = _confirm(client, "yes")
    assert _sends(bb) in ([], [("new", (number_said,))]), (said, after, _sends(bb))


# --- 4. the full name that was said, not a shorter name that is also a card ---------------------
def test_a_full_name_is_not_sent_to_the_card_that_is_only_its_first_word(r, client, bb, osa, compat_db, monkeypatch):
    bb.answers = [_ok()]
    load_cards(r, monkeypatch, [("Dan", [A]), ("Dan Fixture", [B])])
    said = _prepare(client, f"text Dan Fixture {TEXT}")
    after = _confirm(client, "yes")
    assert _sends(bb) in ([], [("new", (B,))]), (said, after, _sends(bb))


def test_namesakes_said_in_full_are_refused_also_when_a_shorter_card_exists(r, client, bb, osa, compat_db, monkeypatch):
    bb.answers = [_ok()]
    load_cards(r, monkeypatch, [("Dan", [A]), ("Dan Fixture", [B]), ("Dan Fixture", [C])])
    said = _prepare(client, f"text Dan Fixture {TEXT}")
    after = _confirm(client, "yes")
    assert said.startswith(REFUSAL) and _sends(bb) == [], (said, after, _sends(bb))


# --- 2. the which-one round through the routes (expected to hold) -------------------------------
def test_which_one_round_sends_only_to_the_full_name_said(r, client, bb, osa, compat_db, monkeypatch):
    load_cards(r, monkeypatch, [("Mary Fixture", [A]), ("Mary Ann Other", [B])])
    for answer, expect in [("Mary", []), ("yes", []), ("the second one", []), ("Mary Ann", []), ("Other", []),
                           ("Mary Fixture Other", []), ("Mary Ann Other", [("new", (B,))])]:
        bb.calls.clear()
        bb.answers = [_ok()]
        said = _prepare(client, f"text Mary {TEXT}")
        assert said == (f"Did you mean Mary Fixture or Mary Ann Other? The message is: {TEXT}. "
                        "Say the full name, or say cancel."), said
        after = _confirm(client, answer)
        assert _sends(bb) == expect, (answer, after, _sends(bb))
        assert _confirm(client, "yes") == "There's nothing waiting to send."


def test_which_one_round_sends_the_text_that_was_dictated(r, client, bb, osa, compat_db, monkeypatch):
    bb.answers = [_ok()]
    load_cards(r, monkeypatch, [("Mary Fixture", [A]), ("Mary Ann Other", [B])])
    said = _prepare(client, f"text Mary Ann {TEXT}")
    after = _confirm(client, "Mary Ann Other")
    texts = [c.json.get("message") for c in bb.calls if c.path == "/api/v1/chat/new"]
    # Where the name ends and the message begins cannot be known ("Mary" + "Ann ..." or "Mary Ann" + "..."),
    # so the question reads the message back, and what is sent is exactly what was read: nothing else.
    read_back = said.split("The message is: ", 1)[1].split(". Say the full name", 1)[0]
    assert texts in ([], [read_back]), (said, after, texts)


# --- 5. the single slot: an answer belongs to the prompt it was given for -----------------------
@pytest.mark.xfail(strict=True, reason="known limit: the plain-text voice pair has one pending slot and no token, "
                                       "so an answer cannot be tied to the prompt it was given for. Needs a "
                                       "change of the voice protocol; not part of this update.")
def test_a_late_yes_for_the_first_prompt_does_not_send_the_second_message(client, bb, osa, compat_db):
    bb.answers = [_ok()]
    assert _prepare(client, f"text Alice Anders {TEXT}") == f"Send {TEXT} to Alice Anders?"
    assert _prepare(client, "text Bob Brown a second synthetic text") == "Send a second synthetic text to Bob Brown?"
    said = _confirm(client, "yes")          # the delayed answer to the Alice prompt arrives now
    assert _sends(bb) == [] or said == "Sent to Alice Anders.", (said, _sends(bb))
