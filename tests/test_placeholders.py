"""Shipped placeholders are never credentials (step R5, 2026-10-07).

``.env.example`` ships ``IMSG_TOKEN=change-me-to-a-long-random-string`` and the
launchd example ``CHANGE-ME-long-random-string`` / ``CHANGE-ME``. Both strings
are public. Before R5 the relay accepted either as its token, put a dead
``bluebubbles`` engine first in the chain for ``BB_PASSWORD=change-me``,
started the Beeper watcher with a bearer token that could not work and probed
Home Assistant with one at every start.

* ``placeholders.is_placeholder``: the rule itself (case-insensitive prefixes
  ``change-me``, ``changeme``, ``replace-with``, ``your-``; the exact
  ``CHANGE-ME``), and ``drop_placeholder``.
* ``engines.chain.build_chain`` and ``engines.features.derive_features``: a
  placeholder ``BB_PASSWORD`` / ``BEEPER_TOKEN`` / ``HA_TOKEN`` /
  ``MAPKIT_TOKEN`` counts as unset.
* ``beeper.py``: a placeholder token leaves the bridge disabled.
* the relay module, imported in a fresh interpreter with every example value
  still in place: empty credentials, the key names remembered for the doctor,
  the AppleScript-only chain, nothing advertised, ``/locations`` not configured.
* ``IMSG_TOKEN``: a placeholder is never accepted as the token. With no
  ``IMSG_ALLOW_NO_TOKEN=1`` the API is locked (401 everywhere but ``/health``,
  the WebSocket refused); with the flag it is the same as no token.

(``python relay.py`` refusing to start is in ``tests/test_doctor.py``, beside
the ``--check`` run it must not affect.)

Synthetic values only. The relay-module tests skip on a relay that predates R5.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from engines import build_chain, derive_features, engine_names
from placeholders import PLACEHOLDER_PREFIXES, drop_placeholder, is_placeholder
from tests.conftest import RELAY_STUB_ENV, RELAY_STUB_SELF
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)

STUB_TOKEN = RELAY_STUB_ENV["IMSG_TOKEN"]
AUTH = {"X-Imsg-Token": STUB_TOKEN}

#: The placeholder strings the two example files actually ship.
SHIPPED = ("change-me-to-a-long-random-string", "CHANGE-ME-long-random-string", "CHANGE-ME",
           "your-bluebubbles-server-password")


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    *SHIPPED, "change-me", "Change-Me", "CHANGE-ME", "changeme", "ChangeMe123", "change-me!",
    "replace-with-your-token", "REPLACE-WITH-SOMETHING", "your-token-here", "YOUR-PASSWORD",
    "  CHANGE-ME  ", "\tchange-me-to-a-long-random-string\n",
])
def test_placeholders_are_recognised(value):
    assert is_placeholder(value) is True
    assert drop_placeholder(value) == ""


@pytest.mark.parametrize("value", [
    "", "   ", "x", "stub-token-for-tests-not-real", "stub-bb-password-not-real", "demo",
    "my-change-me", "unchangeme", "yours", "your", "you-r-", "replacewith", "replace",
    "0123456789abcdef" * 4, "a-real-password", "please change-me",
])
def test_real_values_and_empty_ones_are_not_placeholders(value):
    assert is_placeholder(value) is False
    assert drop_placeholder(value) == value                  # returned exactly as given


def test_non_strings_are_never_placeholders():
    for value in (None, 0, 7, b"change-me", ["CHANGE-ME"], {"k": "change-me"}):
        assert is_placeholder(value) is False
    assert drop_placeholder(None) == ""
    assert PLACEHOLDER_PREFIXES == ("change-me", "changeme", "replace-with", "your-")
    assert all(p == p.lower() for p in PLACEHOLDER_PREFIXES)


def test_the_stub_values_the_suite_runs_under_are_not_placeholders():
    for key in ("IMSG_TOKEN", "BB_PASSWORD"):
        assert not is_placeholder(RELAY_STUB_ENV[key]), key


# ---------------------------------------------------------------------------
# the chain and the advertised features
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("placeholder", SHIPPED)
def test_a_placeholder_password_or_token_keeps_the_engine_out_of_the_chain(placeholder):
    assert engine_names(build_chain({"BB_PASSWORD": placeholder})) == ["applescript"]
    assert engine_names(build_chain({"BEEPER_TOKEN": placeholder})) == ["applescript"]
    assert engine_names(build_chain({"BB_PASSWORD": placeholder, "BEEPER_TOKEN": placeholder,
                                     "SEND_APPLESCRIPT_FALLBACK": "0"})) == []
    # naming the engine in SEND_ENGINES does not configure it either
    env = {"BB_PASSWORD": placeholder, "BEEPER_TOKEN": placeholder,
           "SEND_ENGINES": "beeper,bluebubbles,applescript"}
    assert engine_names(build_chain(env)) == ["applescript"]
    # one real value beside one placeholder: only the real one is in
    assert engine_names(build_chain({"BB_PASSWORD": "a-real-password", "BEEPER_TOKEN": placeholder})) == \
        ["bluebubbles", "applescript"]
    assert engine_names(build_chain({"BB_PASSWORD": placeholder, "BEEPER_TOKEN": "a-real-token"})) == \
        ["beeper", "applescript"]


@pytest.mark.parametrize("placeholder", SHIPPED)
def test_a_placeholder_credential_derives_no_feature(placeholder):
    off = {"facetime": False, "map": False, "translate": False, "voice": False}
    assert derive_features({"BB_PASSWORD": placeholder}) == off
    assert derive_features({"MAPKIT_TOKEN": placeholder, "HA_TOKEN": placeholder}) == off
    assert derive_features({"MAPKIT_TOKEN": "a-real-token", "HA_TOKEN": placeholder}) == off
    assert derive_features({"MAPKIT_TOKEN": placeholder, "HA_TOKEN": "a-real-token"}) == off
    assert derive_features({"MAPKIT_TOKEN": "m", "HA_TOKEN": "h"})["map"] is True
    # an explicit FEATURE_* still wins: it is a switch, not a credential
    assert derive_features({"BB_PASSWORD": placeholder, "FEATURE_FACETIME": "1"})["facetime"] is True


# ---------------------------------------------------------------------------
# beeper.py and the relay module, in a fresh interpreter
# ---------------------------------------------------------------------------

def _run(code: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    # IMESSAGE_CLI=0 (step R6): a Mac that has imessage-cli installed would
    # otherwise add its edit engine to the chain these tests pin.
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ["HOME"],
            "IMESSAGE_CLI": "0", **env}
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=full,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("token, enabled", [("CHANGE-ME", False), ("change-me", False),
                                            ("your-beeper-token", False), ("", False),
                                            ("stub-beeper-token-not-real", True)])
def test_beeper_module_treats_a_placeholder_token_as_unset(token, enabled):
    root = Path(__file__).resolve().parent.parent
    p = _run("import beeper; print(beeper.enabled(), bool(beeper.BEEPER_TOKEN))",
             {"BEEPER_TOKEN": token}, root)
    assert p.returncode == 0, p.stderr[-1500:]
    assert p.stdout.split() == [str(enabled), str(enabled)]


_IMPORT_PROBE = """
import sys, types, json
m = types.ModuleType('dotenv'); m.load_dotenv = lambda *a, **k: False; sys.modules['dotenv'] = m
import {module} as relay
print('PROBE' + json.dumps({{
    'token': relay.IMSG_TOKEN, 'bb': relay.BB_PASSWORD, 'ha': relay.HA_TOKEN, 'mapkit': relay.MAPKIT_TOKEN,
    'beeper': [relay.beeper.BEEPER_TOKEN, relay.beeper.enabled()],
    'keys': sorted(relay.PLACEHOLDER_KEYS), 'locked': relay.AUTH_LOCKED,
    'engines': relay.engine_names(relay._chain()), 'features': relay.FEATURES,
    'doctor': relay.doctor_table(relay.doctor_rows(probe=lambda *a, **k: None)),
}}))
"""


def test_relay_imported_with_every_example_placeholder_still_in_place(r5, relay_module, compat_db, tmp_path):
    relay_dir = Path(relay_module.module.__file__).resolve().parent
    env = {"IMSG_TOKEN": "CHANGE-ME-long-random-string", "BB_PASSWORD": "CHANGE-ME",
           "BEEPER_TOKEN": "CHANGE-ME", "HA_TOKEN": "CHANGE-ME", "MAPKIT_TOKEN": "CHANGE-ME",
           "HA_LOCATIONS": "Alice=device_tracker.alice_phone", "HA_URL": "http://127.0.0.1:9",
           "BB_URL": "http://127.0.0.1:9", "FCM_CREDS": "",     # the doctor's probe is stubbed below
           "IMSG_CHATDB": str(compat_db.path), "IMSG_STATE": str(tmp_path / "relay_state.json"),
           "IMSG_SELF": RELAY_STUB_SELF, "RELAY_DATA_DIR": str(tmp_path / "data")}
    p = _run(_IMPORT_PROBE.format(module=relay_module.name), env, relay_dir)
    assert p.returncode == 0, p.stderr[-2000:]
    got = json.loads(next(l for l in p.stdout.splitlines() if l.startswith("PROBE"))[5:])
    assert (got["token"], got["bb"], got["ha"], got["mapkit"]) == ("", "", "", "")
    assert got["beeper"] == ["", False]
    assert got["keys"] == ["BB_PASSWORD", "BEEPER_TOKEN", "HA_TOKEN", "IMSG_TOKEN", "MAPKIT_TOKEN"]
    assert got["locked"] is True
    assert got["engines"] == ["applescript"]
    assert got["features"] == {"facetime": False, "map": False, "translate": False, "voice": False}
    rows = {line.split("  ")[1].strip(): line for line in got["doctor"].splitlines()[3:]}
    assert "PLACEHOLDER" in rows["token (IMSG_TOKEN)"]
    for name in ("BlueBubbles", "Beeper (Google Messages)", "Home Assistant", "MapKit"):
        assert "placeholder — treated as unset" in rows[name], name
    assert "CHANGE-ME" not in got["doctor"]                  # status words, never the value
    assert "[auth] IMSG_TOKEN IS A PLACEHOLDER" in p.stdout
    assert "[beeper]" not in p.stdout                        # nothing started at import


def test_relay_import_with_real_values_is_unchanged(r5, relay_module):
    """The session's own import ran under the stub (real-looking) values."""
    assert r5.IMSG_TOKEN == STUB_TOKEN and r5.BB_PASSWORD == RELAY_STUB_ENV["BB_PASSWORD"]
    assert r5.PLACEHOLDER_KEYS == frozenset() and r5.AUTH_LOCKED is False
    assert r5.engine_names(r5._chain()) == ["bluebubbles", "applescript"]


# ---------------------------------------------------------------------------
# placeholders that gate a route
# ---------------------------------------------------------------------------

@pytest.fixture
def client(r5) -> TestClient:
    """ASGI test client; no ``with``, so startup hooks never run."""
    return TestClient(r5.app)


def test_locations_is_not_configured_while_the_ha_token_is_a_placeholder(r5, client, monkeypatch):
    # what the import leaves behind for HA_TOKEN=CHANGE-ME: an empty token
    monkeypatch.setattr(r5, "HA_TOKEN", drop_placeholder("CHANGE-ME"))
    monkeypatch.setattr(r5, "HA_LOCATIONS", "Alice=device_tracker.alice_phone")
    resp = client.get("/locations", headers=AUTH)
    assert resp.status_code == 503 and "HA not configured" in resp.json()["detail"]


def test_map_page_never_embeds_a_placeholder_token(r5, client, monkeypatch):
    monkeypatch.setattr(r5, "MAPKIT_TOKEN", drop_placeholder("CHANGE-ME"))
    page = client.get("/map", headers=AUTH).text
    assert "CHANGE-ME" not in page and 'done("")' in page


# ---------------------------------------------------------------------------
# IMSG_TOKEN: a placeholder is never the token
# ---------------------------------------------------------------------------

def _lock(r5, monkeypatch, allow: bool) -> None:
    """The module state an import leaves for a placeholder IMSG_TOKEN."""
    monkeypatch.setattr(r5, "IMSG_TOKEN", "")
    monkeypatch.setattr(r5, "PLACEHOLDER_KEYS", frozenset({"IMSG_TOKEN"}))
    monkeypatch.setattr(r5, "ALLOW_NO_TOKEN", allow)
    monkeypatch.setattr(r5, "AUTH_LOCKED", not allow)


def test_a_placeholder_token_locks_the_api(r5, client, monkeypatch):
    _lock(r5, monkeypatch, allow=False)
    for supplied in (None, *SHIPPED, STUB_TOKEN, ""):
        headers = {} if supplied is None else {"X-Imsg-Token": supplied}
        assert client.get("/contacts", headers=headers).status_code == 401, supplied
        assert client.post("/send", json={"chat_guid": "iMessage;-;+15550001234", "text": "x"},
                           headers=headers).status_code == 401, supplied
        assert client.get("/health", headers=headers).json() == {"ok": True}, supplied
        if supplied:
            assert client.get("/contacts", params={"token": supplied}).status_code == 401, supplied
    for url in ("/ws", "/ws?token=CHANGE-ME-long-random-string", "/ws?token=change-me-to-a-long-random-string"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url):
                pass
    assert r5.token_matches("CHANGE-ME-long-random-string") is False


def test_allow_no_token_makes_a_placeholder_the_same_as_no_token(r5, client, monkeypatch):
    _lock(r5, monkeypatch, allow=True)
    assert client.get("/contacts").status_code == 200                       # no authentication at all
    assert client.get("/contacts", headers={"X-Imsg-Token": "anything"}).status_code == 200
    assert client.get("/health").json() == {"ok": True}                     # and never the full body
    assert client.get("/health", headers={"X-Imsg-Token": "CHANGE-ME-long-random-string"}).json() == {"ok": True}
    with client.websocket_connect("/ws"):
        pass


def test_no_token_at_all_behaves_as_before_on_import(r5, client, monkeypatch):
    """An imported relay with IMSG_TOKEN unset is open, as it always was; only
    ``python relay.py`` refuses that configuration (tests/test_doctor.py)."""
    monkeypatch.setattr(r5, "IMSG_TOKEN", "")
    assert r5.AUTH_LOCKED is False and r5.PLACEHOLDER_KEYS == frozenset()
    assert client.get("/contacts").status_code == 200
    assert client.get("/health").json() == {"ok": True}
