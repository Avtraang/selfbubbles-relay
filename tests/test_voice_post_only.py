"""The plain-text voice routes act on POST and on nothing else.

A GET is what anything that loads an address makes: the image of a received
link preview, a page open in a browser, a link someone taps. `/v/prepare` and
`/v/confirm` used to act on one, so an address was enough to arm a message
and a second one to send it. The app's own voice screen posts a form
(`Imsg.kt`: `voicePrepare`, `voiceConfirm`), and so does everything else that
is meant to call these routes.

What is pinned here: a GET (or any other method) arms nothing, confirms
nothing and does not use up the message that is waiting; the three ways an
automation app may post (a form body, a JSON body, the query string of the
POST) keep working exactly as before.

In-process and synthetic: the real routes, the real send chain into the
suite's recording BlueBubbles client.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_recipient_safety import TEXT, _confirm, _ok, _prepare, _sends
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, STUB_TOKEN, bb, client, osa, r  # noqa: F401  (fixtures)

SENTENCE = f"text Alice Anders {TEXT}"
ASKS = f"Send {TEXT} to Alice Anders?"
NOTHING = "There's nothing waiting to send."


@pytest.fixture(autouse=True)
def _nothing_waiting(r):
    """The pending slot is one per process: no test starts or ends with a message in it."""
    r.LAST_PENDING.clear()
    yield
    r.LAST_PENDING.clear()


def test_a_get_to_prepare_is_refused_and_arms_nothing(client, bb, osa, compat_db):
    resp = client.get("/v/prepare", params={"q": SENTENCE}, headers=AUTH)
    assert resp.status_code == 405
    assert "POST" in resp.headers.get("allow", "")
    assert TEXT not in resp.text
    assert _confirm(client, "yes") == NOTHING
    assert bb.calls == [] and osa.calls == []


def test_a_get_to_confirm_is_refused_and_sends_nothing(client, bb, osa, compat_db):
    bb.answers = [_ok()]
    assert _prepare(client, SENTENCE) == ASKS
    resp = client.get("/v/confirm", params={"a": "yes"}, headers=AUTH)
    assert resp.status_code == 405
    assert bb.calls == [] and osa.calls == []


def test_a_refused_get_does_not_use_up_the_waiting_message(client, bb, osa, compat_db):
    """The person at the phone still gets to answer: a stray GET is not a "no" either."""
    bb.answers = [_ok()]
    assert _prepare(client, SENTENCE) == ASKS
    assert client.get("/v/confirm", params={"a": "no"}, headers=AUTH).status_code == 405
    assert client.get("/v/prepare", params={"q": "text Bob Brown something else"}, headers=AUTH).status_code == 405
    assert _confirm(client, "yes") == "Sent to Alice Anders."
    assert len(_sends(bb)) == 1 and osa.calls == []


def test_a_get_carrying_the_token_in_its_address_is_refused_too(client, bb, osa, compat_db):
    """The shape a crafted link has: everything in the address, no header."""
    bb.answers = [_ok()]
    assert client.get("/v/prepare", params={"q": SENTENCE, "token": STUB_TOKEN}).status_code == 405
    assert client.get("/v/confirm", params={"a": "yes", "token": STUB_TOKEN}).status_code == 405
    assert _confirm(client, "yes") == NOTHING
    assert bb.calls == [] and osa.calls == []


@pytest.mark.parametrize("method", ["HEAD", "PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("path, params", [("/v/prepare", {"q": SENTENCE}), ("/v/confirm", {"a": "yes"})])
def test_no_other_method_acts(client, bb, osa, compat_db, method, path, params):
    bb.answers = [_ok()]
    _prepare(client, SENTENCE)
    assert client.request(method, path, params=params, headers=AUTH).status_code == 405
    assert bb.calls == [] and osa.calls == []


def test_a_get_without_the_token_learns_nothing_and_arms_nothing(client, bb, osa, compat_db):
    resp = client.get("/v/prepare", params={"q": SENTENCE})
    assert resp.status_code in (401, 403, 405)
    assert TEXT not in resp.text
    assert _confirm(client, "yes") == NOTHING
    assert bb.calls == [] and osa.calls == []


# ---------------------------------------------------------------------------
# What has to keep working: every way a POST may carry the two fields.
# ---------------------------------------------------------------------------

def test_the_form_post_the_app_sends_still_works(client, bb, osa, compat_db):
    """OkHttp's FormBody: application/x-www-form-urlencoded, fields `query` and `answer`."""
    bb.answers = [_ok()]
    form = {"Content-Type": "application/x-www-form-urlencoded", **AUTH}
    resp = client.post("/v/prepare", content="query=" + SENTENCE.replace(" ", "+"), headers=form)
    assert (resp.status_code, resp.text) == (200, ASKS)
    resp = client.post("/v/confirm", content="answer=yes", headers=form)
    assert (resp.status_code, resp.text) == (200, "Sent to Alice Anders.")
    assert len(_sends(bb)) == 1 and osa.calls == []


def test_a_post_with_the_fields_in_its_query_string_still_works(client, bb, osa, compat_db):
    """How an automation app that cannot build a body posts."""
    bb.answers = [_ok()]
    resp = client.post("/v/prepare", params={"q": SENTENCE}, headers=AUTH)
    assert (resp.status_code, resp.text) == (200, ASKS)
    resp = client.post("/v/confirm", params={"a": "yes"}, headers=AUTH)
    assert (resp.status_code, resp.text) == (200, "Sent to Alice Anders.")
    assert len(_sends(bb)) == 1 and osa.calls == []


def test_a_post_with_a_json_body_still_works(client, bb, osa, compat_db):
    bb.answers = [_ok()]
    resp = client.post("/v/prepare", json={"query": SENTENCE}, headers=AUTH)
    assert (resp.status_code, resp.text) == (200, ASKS)
    resp = client.post("/v/confirm", json={"answer": "no"}, headers=AUTH)
    assert (resp.status_code, resp.text) == (200, "Cancelled.")
    assert bb.calls == [] and osa.calls == []


@pytest.mark.parametrize("path", ["/assistant/prepare", "/assistant/confirm"])
def test_the_json_voice_routes_never_acted_on_a_get(client, bb, osa, compat_db, path):
    assert client.get(path, params={"query": SENTENCE, "answer": "yes"}, headers=AUTH).status_code == 405
    assert bb.calls == [] and osa.calls == []


def test_the_route_table_says_post():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    row = next(line for line in readme.splitlines() if "`/v/prepare`, `/v/confirm`" in line)
    assert row.startswith("| POST |"), row
    assert "one server-side pending slot" in row
