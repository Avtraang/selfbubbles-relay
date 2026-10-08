"""``relay.py --check`` and the portability knobs (plan 2026-10-06, step R3).

* ``doctor_rows`` / ``doctor_table``: one row per dependency, statuses only.
  Under the conftest's placeholder environment the table must not contain
  the placeholder token, the placeholder password, or any value of the
  stub environment that is not a plain on/off flag.
* chat.db: NOT FOUND for the placeholder path, readable once the adapter is
  pointed at the synthetic database, and NOT READABLE with the Full Disk
  Access hint naming ``sys.executable`` when an existing file cannot be
  opened (the symptom of a missing grant).
* the network: ``probe`` is injected, so no test dials out; the BlueBubbles
  ping goes to the stub's discard port.
* paths: ``ICON_CACHE`` is ``__file__``-relative; ``RELAY_DATA_DIR`` moves
  the FaceTime log (default: the checkout); ``_launch_autoadmit`` runs the
  script beside relay.py, appends to the data-dir log and passes
  ``RELAY_DATA_DIR`` down; the five helpers live in ``facetime/``.
* ``--check`` end to end: the module run as ``__main__`` with the flag prints
  the table and exits 0 without starting a server. ``dotenv`` is stubbed in
  that subprocess too, so the real ``.env`` is never read; every probe URL
  points at a discard port and ``IMSG_CHATDB`` at the synthetic database.

Step R5 (2026-10-07) added, and these tests skip on a relay without them:

* a ``listening on`` row (``IMSG_BIND``:``IMSG_PORT``) with a hint when the
  relay binds every interface, in any spelling the socket layer reads as
  ``0.0.0.0`` (``bind_kind``), and ``NOT AN IP ADDRESS`` for a value that is
  not an address at all;
* the token row's ``NOT SET`` / ``PLACEHOLDER`` states, and the same module
  run WITHOUT ``--check``: it prints why and exits 78 instead of serving,
  unless ``IMSG_ALLOW_NO_TOKEN=1`` AND the bind address is loopback;
* the one ``[auth]`` line on stdout, which never says the relay is running
  without authentication when the start is about to be refused;
* ``placeholder — treated as unset`` for a BlueBubbles password, Beeper token,
  Home Assistant token or MapKit token that still holds an example's value;
* a ``chat.db`` row that survives macOS refusing even the ``stat``: NOT
  READABLE with the Full Disk Access hint, never a traceback or NOT FOUND.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import RELAY_STUB_ENV, RELAY_STUB_SELF, has_r5, has_r6
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)

NO_NETWORK_CALLS: list = []


def no_probe(url, headers=None, timeout=None):
    """Records that a probe was attempted (never the headers) and answers 'down'."""
    NO_NETWORK_CALLS.append(url.split("?")[0])
    return None


@pytest.fixture
def r(relay_module):
    """The relay under test; skips when it predates the doctor (pre-R3)."""
    mod = relay_module.module
    if not hasattr(mod, "doctor_rows"):
        pytest.skip(f"{relay_module.name} has no doctor (pre-R3)")
    return mod


@pytest.fixture
def table(r):
    rows = r.doctor_rows(probe=no_probe)
    return rows, r.doctor_table(rows)


def test_doctor_lists_every_component(r, table):
    rows, text = table
    names = [name for name, _, _ in rows]
    expected = ["chat.db", "token (IMSG_TOKEN)", "BlueBubbles", "Beeper (Google Messages)",
                "FCM push", "Home Assistant", "MapKit", "translation", "send engines",
                "features advertised", "data dir", "FaceTime auto-admit"]
    if has_r6(r):                       # step R6: who can change a sent message, after the chain
        expected.insert(expected.index("send engines") + 1, "edit / unsend")
    if has_r5(r):                       # step R5: where the server binds, after the token
        expected.insert(2, "listening on")
    assert names == expected
    assert text.startswith("[check] relay doctor\n")
    for name in names:
        assert name in text


def test_doctor_prints_no_secret_values(r, table):
    rows, text = table
    assert "stub-token-for-tests-not-real" not in text
    assert "stub-bb-password-not-real" not in text
    assert RELAY_STUB_SELF not in text
    for key, value in RELAY_STUB_ENV.items():
        if value and value not in ("0", "1"):
            assert value not in text, key
    assert r.IMSG_TOKEN not in text and r.BB_PASSWORD not in text
    # URLs stay out too: only status words and the two allowed paths
    assert r.BB_URL not in text and r.MARIAN_URL not in text and r.OLLAMA_URL not in text


def test_doctor_statuses_under_stub_env(r, table):
    rows, _ = table
    by = {name: (status, hint) for name, status, hint in rows}
    assert by["token (IMSG_TOKEN)"] == ("set", "")
    assert by["BlueBubbles"][0] == "password set, server unreachable"      # discard port
    assert by["Beeper (Google Messages)"][0] == "disabled"
    assert by["FCM push"][0] == "disabled"
    assert by["Home Assistant"][0] == "disabled"                            # HA_TOKEN unset
    assert by["MapKit"][0] == "not set"
    assert by["translation"][0] == "marian unreachable, ollama unreachable (OLLAMA_MODEL default)"
    assert by["send engines"][0] == "bluebubbles, applescript"
    assert by["features advertised"][0] == "facetime, voice"
    assert by["data dir"][0] == f"{r.DATA_DIR} (writable)"
    if r.FT_ADMIT_APP.exists():                                             # the author's checkout
        assert by["FaceTime auto-admit"][0] == "on, script found, helper app found"
    else:                                                                   # every other checkout
        assert by["FaceTime auto-admit"][0] == "off (helper app missing), script found, helper app missing"
    # the probes that ran went to marian and ollama only (no HA token -> no HA probe)
    assert NO_NETWORK_CALLS[-2:] == [f"{r.MARIAN_URL}/", f"{r.OLLAMA_URL}/api/tags"]


def test_doctor_chatdb_not_found_for_the_placeholder(r, relay_module):
    # The adapter is session-wide and other tests re-point it at their
    # databases, so bind it back to the import-time placeholder first.
    relay_module.configure(relay_module.chatdb_path)
    assert not relay_module.chatdb_path.exists()
    rows = r.doctor_rows(probe=no_probe)
    name, status, hint = rows[0]
    assert (name, status) == ("chat.db", "NOT FOUND")
    assert "IMSG_CHATDB" in hint


def test_doctor_chatdb_readable_on_the_synthetic_db(r, relay_module, compat_db):
    relay_module.configure(compat_db.path)
    name, status, hint = r._chatdb_row()
    assert (name, status, hint) == ("chat.db", "readable", "")


def test_doctor_chatdb_unreadable_names_the_interpreter(r, monkeypatch, tmp_path):
    existing = tmp_path / "chat.db"
    existing.write_bytes(b"")                                   # exists, cannot be opened
    monkeypatch.setattr(r, "CHATDB", str(existing))

    def denied():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(r, "db", denied)
    name, status, hint = r._chatdb_row()
    assert name == "chat.db" and status == "NOT READABLE (OperationalError)"
    assert "Full Disk Access" in hint and sys.executable in hint
    if os.path.realpath(sys.executable) != sys.executable:
        assert os.path.realpath(sys.executable) in hint


def test_file_state_tells_missing_from_denied(r5, tmp_path, monkeypatch):
    present = tmp_path / "there.db"
    present.write_bytes(b"")
    assert r5._file_state(present) == "present"
    assert r5._file_state(tmp_path / "absent.db") == "missing"
    assert r5._file_state(present / "under-a-file.db") == "missing"       # ENOTDIR

    def denied(path, *a, **k):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(r5.os, "stat", denied)
    assert r5._file_state(present) == "denied"
    assert r5._file_state(tmp_path / "absent.db") == "denied"             # cannot tell: not "missing"


def test_doctor_chatdb_row_survives_a_denied_stat(r5, monkeypatch, tmp_path):
    """Without Full Disk Access macOS refuses the open AND the stat of
    ``chat.db`` (EPERM). The row used to call ``Path.exists()`` inside its
    ``except`` block: on Python 3.12/3.13 that re-raises the PermissionError (a
    traceback instead of a table), on 3.14 it answers False (a wrong NOT
    FOUND). It must be NOT READABLE with the grant hint."""
    target = tmp_path / "Messages" / "chat.db"
    target.parent.mkdir()
    target.write_bytes(b"")
    monkeypatch.setattr(r5, "CHATDB", str(target))
    monkeypatch.setattr(r5.chatdb_adapter, "_cdb",
                        lambda: (_ for _ in ()).throw(RuntimeError("synthetic: adapter unavailable")))

    def db_denied():
        raise sqlite3.OperationalError("unable to open database file")

    real_stat = os.stat

    def stat_denied(path, *a, **k):
        if os.fspath(path) == str(target):
            raise PermissionError(1, "Operation not permitted", str(target))
        return real_stat(path, *a, **k)

    monkeypatch.setattr(r5, "db", db_denied)
    monkeypatch.setattr(os, "stat", stat_denied)
    with pytest.raises(PermissionError):
        os.stat(target)                                   # the fake is in force
    name, status, hint = r5._chatdb_row()                 # ... and the row does not raise
    assert (name, status) == ("chat.db", "NOT READABLE (OperationalError)")
    assert "Full Disk Access" in hint and sys.executable in hint
    assert str(target) not in hint                        # the database path stays out of the table
    # the whole table still renders
    text = r5.doctor_table(r5.doctor_rows(probe=no_probe))
    assert "NOT READABLE (OperationalError)" in text and "Traceback" not in text


def test_doctor_listening_row(r5, monkeypatch):
    by = {n: (s, h) for n, s, h in r5.doctor_rows(probe=no_probe)}
    assert r5.BIND == "0.0.0.0"                           # IMSG_BIND unset: every interface, as always
    status, hint = by["listening on"]
    assert status == f"0.0.0.0:{r5.PORT}"
    assert "IMSG_BIND=127.0.0.1" in hint and "every network interface" in hint
    for bind, shown, hinted in (("127.0.0.1", f"127.0.0.1:{r5.PORT}", False),
                                ("192.168.0.10", f"192.168.0.10:{r5.PORT}", False),
                                ("::1", f"[::1]:{r5.PORT}", False),
                                ("localhost", f"localhost:{r5.PORT}", False),
                                ("::", f"[::]:{r5.PORT}", True),
                                # other spellings the socket layer binds to every interface
                                ("0", f"0:{r5.PORT}", True),
                                ("0x0", f"0x0:{r5.PORT}", True),
                                ("00.0.0.0", f"00.0.0.0:{r5.PORT}", True),
                                ("::0", f"[::0]:{r5.PORT}", True)):
        monkeypatch.setattr(r5, "BIND", bind)
        status, hint = {n: (s, h) for n, s, h in r5.doctor_rows(probe=no_probe)}["listening on"]
        assert status == shown and bool(hint) is hinted, bind
        assert ("every network interface" in hint) is hinted, bind


def _star_is_the_wildcard_address() -> bool:
    """Does this C library read the host ``*`` as "no host given"?

    glibc's ``getaddrinfo`` does, and with ``AI_PASSIVE`` that is the wildcard
    address: on Linux ``IMSG_BIND=*`` binds every interface. macOS, where the
    relay runs, refuses ``*`` as not numeric. ``bind_kind`` reports what the
    socket layer does, so what these tests expect for ``*`` follows this
    answer (asked with the flags ``bind_kind`` uses: no lookup, no packet)."""
    try:
        socket.getaddrinfo("*", 0, type=socket.SOCK_STREAM,
                           flags=socket.AI_PASSIVE | socket.AI_NUMERICHOST)
    except OSError:
        return False
    return True


STAR_IS_WILDCARD = _star_is_the_wildcard_address()


@pytest.mark.parametrize("bind", [
    "127.0.0.1:8700", "[::1]",
    pytest.param("*", marks=pytest.mark.skipif(
        STAR_IS_WILDCARD, reason="this C library reads the host '*' as the wildcard address (glibc); "
                                 "on macOS, where the relay runs, it is not an address")),
    "127.0.0.1 # loopback", "relay.example.test"])
def test_doctor_listening_row_flags_a_value_that_is_not_an_address(r5, monkeypatch, bind):
    """uvicorn would exit on most of these (and resolve the last one when it
    starts); the row says what is wrong instead of showing the value with an
    empty hint. The value itself is not echoed."""
    monkeypatch.setattr(r5, "BIND", bind)
    rows = r5.doctor_rows(probe=no_probe)
    status, hint = {n: (s, h) for n, s, h in rows}["listening on"]
    assert status == f"NOT AN IP ADDRESS, port {r5.PORT}"
    assert "IMSG_BIND must be an IP address such as 127.0.0.1" in hint
    assert bind not in r5.doctor_table(rows)


def test_bind_kind(r5):
    kinds = {"all": ("0.0.0.0", "0", "0x0", "00.0.0.0", "::", "::0", "0:0:0:0:0:0:0:0"),
             "loopback": ("127.0.0.1", "127.1", "127.0.0.53", "::1", "localhost", "LOCALHOST", " localhost "),
             "address": ("192.168.0.10", "10.0.0.2", "fe80::1"),
             "name": ("127.0.0.1:8700", "[::1]", "*", "relay.example.test", "not an address", "\u00e9")}
    if STAR_IS_WILDCARD:                # glibc: there "*" is the wildcard address, so every interface
        kinds["name"] = tuple(v for v in kinds["name"] if v != "*")
        kinds["all"] += ("*",)
    for kind, values in kinds.items():
        for value in values:
            assert r5.bind_kind(value) == kind, value


def test_doctor_token_row_states(r5, monkeypatch):
    def row():
        return {n: (s, h) for n, s, h in r5.doctor_rows(probe=no_probe)}["token (IMSG_TOKEN)"]

    assert row() == ("set", "")
    monkeypatch.setattr(r5, "IMSG_TOKEN", "")
    monkeypatch.setattr(r5, "ALLOW_NO_TOKEN", False)
    status, hint = row()
    assert status == "NOT SET" and "refuses to start" in hint and "IMSG_ALLOW_NO_TOKEN=1" in hint
    monkeypatch.setattr(r5, "PLACEHOLDER_KEYS", frozenset({"IMSG_TOKEN"}))
    status, hint = row()
    assert status == "PLACEHOLDER" and "refuses to start" in hint and "openssl rand -hex 32" in hint
    # the flag counts only on a loopback bind; anywhere else the relay still refuses
    monkeypatch.setattr(r5, "ALLOW_NO_TOKEN", True)
    for bind in ("0.0.0.0", "192.168.0.10", "::", "0", "relay.example.test"):
        monkeypatch.setattr(r5, "BIND", bind)
        status, hint = row()
        assert status == "PLACEHOLDER" and "refuses to start" in hint and "loopback" in hint, bind
        assert "WITHOUT authentication" not in hint, bind
    monkeypatch.setattr(r5, "BIND", "127.0.0.1")
    status, hint = row()
    assert status == "PLACEHOLDER" and "WITHOUT authentication" in hint and "placeholder is ignored" in hint
    monkeypatch.setattr(r5, "PLACEHOLDER_KEYS", frozenset())
    status, hint = row()
    assert status == "NOT SET" and "WITHOUT authentication" in hint and "placeholder" not in hint
    # the startup table goes through the masking stream: the hint must come out as written
    assert r5.mask_token(hint) == hint and hint.startswith("IMSG_ALLOW_NO_TOKEN is 1: ")


def test_doctor_says_placeholder_treated_as_unset(r5, monkeypatch):
    """What the relay holds after an import with the example's values still in
    place (the module read them as empty and remembers the key names)."""
    keys = frozenset({"BB_PASSWORD", "BEEPER_TOKEN", "HA_TOKEN", "MAPKIT_TOKEN"})
    monkeypatch.setattr(r5, "PLACEHOLDER_KEYS", keys)
    monkeypatch.setattr(r5, "BB_PASSWORD", "")
    monkeypatch.setattr(r5, "HA_TOKEN", "")
    monkeypatch.setattr(r5, "MAPKIT_TOKEN", "")
    assert not r5.beeper.enabled()
    rows = r5.doctor_rows(probe=no_probe)
    by = {n: (s, h) for n, s, h in rows}
    assert r5.PLACEHOLDER_STATUS == "placeholder — treated as unset"
    assert by["BlueBubbles"][0] == "placeholder — treated as unset, server unreachable"
    assert by["Beeper (Google Messages)"][0] == "placeholder — treated as unset"
    assert by["Home Assistant"][0] == "placeholder — treated as unset"
    assert by["MapKit"][0] == "placeholder — treated as unset"
    for name, key in (("BlueBubbles", "BB_PASSWORD"), ("Beeper (Google Messages)", "BEEPER_TOKEN"),
                      ("Home Assistant", "HA_TOKEN"), ("MapKit", "MAPKIT_TOKEN")):
        assert by[name][1].startswith(f"{key} still holds the example's placeholder"), name
    assert by["send engines"][0] == "applescript"
    text = r5.doctor_table(rows)
    assert "CHANGE-ME" not in text and "change-me" not in text       # the status, never the value
    # a real value wins over a stale key name: the rows are the ordinary ones again
    monkeypatch.setattr(r5, "BB_PASSWORD", "stub-bb-password-not-real")
    monkeypatch.setattr(r5, "MAPKIT_TOKEN", "stub-mapkit-token-not-real")
    by = {n: (s, h) for n, s, h in r5.doctor_rows(probe=no_probe)}
    assert by["BlueBubbles"][0] == "password set, server unreachable"
    assert by["MapKit"] == ("token set", "")


def test_doctor_home_assistant_statuses(r, monkeypatch):
    monkeypatch.setattr(r, "HA_TOKEN", "stub-ha-token-not-real")
    monkeypatch.setattr(r, "HA_LOCATIONS", "A=device_tracker.a,B=device_tracker.b")
    seen = []

    def probe(url, headers=None, timeout=None):
        seen.append(url)
        return probe.status

    for status, expect in ((200, "reachable, token accepted, 2 location(s) configured"),
                           (401, "reachable, token REJECTED, 2 location(s) configured"),
                           (None, "UNREACHABLE, 2 location(s) configured")):
        probe.status = status
        rows = r.doctor_rows(probe=probe)
        by = {n: s for n, s, _ in rows}
        assert by["Home Assistant"] == expect, status
    assert seen[0] == f"{r.HA_URL}/api/"
    assert "stub-ha-token-not-real" not in r.doctor_table(rows)


def test_doctor_fcm_rows(r, monkeypatch, tmp_path):
    monkeypatch.setattr(r, "FCM_CREDS", str(tmp_path / "missing.json"))
    by = {n: (s, h) for n, s, h in r.doctor_rows(probe=no_probe)}
    assert by["FCM push"][0].startswith("credentials file MISSING")
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setattr(r, "FCM_CREDS", str(creds))
    monkeypatch.setattr(r, "firebase_admin", object())      # "installed"
    by = {n: (s, h) for n, s, h in r.doctor_rows(probe=no_probe)}
    assert by["FCM push"] == ("credentials file found", "")
    assert str(creds) not in r.doctor_table(r.doctor_rows(probe=no_probe))


def test_doctor_no_engines_warns(r, monkeypatch):
    monkeypatch.setattr(r, "BB_PASSWORD", "")
    monkeypatch.setattr(r, "SEND_APPLESCRIPT_FALLBACK", "0")
    by = {n: (s, h) for n, s, h in r.doctor_rows(probe=no_probe)}
    assert by["send engines"][0] == "NONE" and "501" in by["send engines"][1]
    assert by["BlueBubbles"][0].startswith("no password (AppleScript only)")


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def test_paths_are_relative_to_the_checkout_by_default(r):
    relay_dir = Path(r.__file__).resolve().parent
    assert r.RELAY_DIR == relay_dir
    assert r.DATA_DIR == relay_dir                                   # RELAY_DATA_DIR unset
    assert r.ICON_CACHE == relay_dir / "icons"
    assert r.FT_ADMIT_SCRIPT == relay_dir / "ft-autoadmit.sh"
    assert r.FT_AUTO_LOG == relay_dir / "ft-auto.log"
    assert r.FT_ADMIT_APP == relay_dir / "FaceTimeAdmit.app"
    assert r.FT_ADMIT_SCRIPT.is_file()
    for helper in ("clickcg.py", "findadmit.py", "findblue.py", "hold.py", "jiggle.py"):
        assert (relay_dir / "facetime" / helper).is_file(), helper
        assert not (relay_dir / helper).exists(), helper
    assert not (relay_dir / "mousepos.py").exists()


def test_autoadmit_script_uses_the_facetime_helpers(r):
    text = r.FT_ADMIT_SCRIPT.read_text()
    assert 'BASE="${RELAY_DATA_DIR:-$HERE}"' in text
    assert '$BASE/clickcg.py' not in text and '$BASE/findadmit.py' not in text
    assert '$HELPERS/findadmit.py' in text and '$HELPERS/clickcg.py' in text
    assert "$HOME/imsg-relay" not in text


def test_launch_autoadmit_runs_the_script_with_the_data_dir(r, monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(r.subprocess, "Popen", lambda args, **kw: launched.append((args, kw)))
    monkeypatch.setattr(r, "DATA_DIR", tmp_path)
    monkeypatch.setattr(r, "FT_AUTO_LOG", tmp_path / "ft-auto.log")
    monkeypatch.setattr(r, "FT_ADMIT_APP", tmp_path)                 # helper app "present"
    monkeypatch.delenv("FT_AUTOADMIT", raising=False)
    r._launch_autoadmit("https://facetime.example/join", incoming=True)
    (args, kw), = launched
    assert args == ["/bin/bash", str(r.FT_ADMIT_SCRIPT), "--incoming", "https://facetime.example/join"]
    assert kw["env"]["RELAY_DATA_DIR"] == str(tmp_path)
    assert kw["start_new_session"] is True
    assert (tmp_path / "ft-auto.log").exists()
    kw["stdout"].close()
    launched.clear()
    monkeypatch.setenv("FT_AUTOADMIT", "0")
    r._launch_autoadmit("https://facetime.example/join")
    assert launched == []


def test_autoadmit_is_off_by_construction_without_the_helper_app(r, monkeypatch, tmp_path, capsys):
    """A stranger's checkout has no FaceTimeAdmit.app (git-ignored, never
    rebuilt) and no FT_AUTOADMIT in the env: minting a FaceTime link must not
    fire the author's display-specific rig (plan section 7, risk 6: off by
    default). FT_AUTOADMIT=1 cannot force it on without the app; with the app
    in place the rig is on and FT_AUTOADMIT=0 still wins."""
    launched = []
    monkeypatch.setattr(r.subprocess, "Popen", lambda *a, **k: launched.append((a, k)))
    monkeypatch.setattr(r, "DATA_DIR", tmp_path)
    monkeypatch.setattr(r, "FT_AUTO_LOG", tmp_path / "ft-auto.log")
    monkeypatch.setattr(r, "FT_ADMIT_APP", tmp_path / "FaceTimeAdmit.app")   # absent
    monkeypatch.delenv("FT_AUTOADMIT", raising=False)                        # the stranger's env
    assert r.autoadmit_state() == (False, "helper app missing")
    r._launch_autoadmit("https://facetime.example/join")
    r._launch_autoadmit("https://facetime.example/join", incoming=True)
    monkeypatch.setenv("FT_AUTOADMIT", "1")
    r._launch_autoadmit("https://facetime.example/join")
    assert launched == [] and not (tmp_path / "ft-auto.log").exists()
    out = capsys.readouterr().out
    assert "[facetime] auto-admit skipped: helper app missing (FT_ADMIT_APP)" in out
    assert "facetime.example" not in out                                     # the link stays out of the log
    by = {n: (s, h) for n, s, h in r.doctor_rows(probe=no_probe)}
    assert by["FaceTime auto-admit"][0] == "off (helper app missing), script found, helper app missing"
    assert "FT_ADMIT_APP" in by["FaceTime auto-admit"][1] and "off by construction" in by["FaceTime auto-admit"][1]

    (tmp_path / "FaceTimeAdmit.app").mkdir()                                 # the author's checkout
    monkeypatch.delenv("FT_AUTOADMIT", raising=False)
    assert r.autoadmit_state() == (True, "")
    by = {n: (s, h) for n, s, h in r.doctor_rows(probe=no_probe)}
    assert by["FaceTime auto-admit"] == ("on, script found, helper app found",
                                         "the auto-admit rig is display-specific; FT_AUTOADMIT=0 turns it off")
    r._launch_autoadmit("https://facetime.example/join")
    assert len(launched) == 1
    launched[0][1]["stdout"].close()

    monkeypatch.setenv("FT_AUTOADMIT", "0")                                  # explicit off still wins
    assert r.autoadmit_state() == (False, "FT_AUTOADMIT=0")
    r._launch_autoadmit("https://facetime.example/join")
    assert len(launched) == 1
    by = {n: (s, h) for n, s, h in r.doctor_rows(probe=no_probe)}
    assert by["FaceTime auto-admit"] == ("off (FT_AUTOADMIT=0), script found, helper app found", "")
    assert "skipped" not in capsys.readouterr().out                          # 0 is silent, as before


# ---------------------------------------------------------------------------
# --check end to end
# ---------------------------------------------------------------------------

#: Replaces ``uvicorn.run`` in the child: reports how it was called and what
#: ``sys.stdout`` was at that moment, then returns instead of serving.
_FAKE_UVICORN = ("import uvicorn; uvicorn.run = lambda app, **kw: print("
                 "'UVICORN-RUN host=%s port=%s log_config=%s stdout=%s stderr=%s' % ("
                 "kw.get('host'), kw.get('port'), type(kw.get('log_config')).__name__, "
                 "type(sys.stdout).__name__, type(sys.stderr).__name__)); ")


def run_as_main(relay_module, compat_db, tmp_path, *argv, fake_uvicorn=False, **overrides):
    """Run the relay module as ``__main__`` in a fresh interpreter, the way
    ``python relay.py [--check]`` does, under the stub environment plus
    ``overrides`` (``None`` removes a key). ``dotenv`` is stubbed before the
    module runs, exactly as the conftest does, so the real ``.env`` is never
    read; ``IMSG_PORT`` is the discard port. With ``fake_uvicorn`` the child's
    ``uvicorn.run`` only reports its arguments, so a start that is NOT refused
    can be observed without a server."""
    relay_dir = Path(relay_module.module.__file__).resolve().parent
    script = f"{relay_module.name}.py"
    env = dict(os.environ)
    for key in ("FT_AUTOADMIT", "IMSG_ALLOW_NO_TOKEN", "IMSG_BIND", "PYTHONUNBUFFERED"):
        env.pop(key, None)
    env.update(RELAY_STUB_ENV, IMSG_CHATDB=str(compat_db.path), IMSG_SELF=RELAY_STUB_SELF,
               IMSG_STATE=str(tmp_path / "relay_state.json"), RELAY_DATA_DIR=str(tmp_path / "data"),
               MARIAN_URL="http://127.0.0.1:9", OLLAMA_URL="http://127.0.0.1:9",
               HA_TOKEN="", MAPKIT_TOKEN="", FCM_CREDS="", BEEPER_TOKEN="",
               IMSG_PORT="9")                                   # never bound: the server must not start
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    code = ("import sys, types, runpy; m = types.ModuleType('dotenv'); "
            "m.load_dotenv = lambda *a, **k: False; sys.modules['dotenv'] = m; "
            + (_FAKE_UVICORN if fake_uvicorn else "") +
            f"sys.argv = [{script!r}, *{list(argv)!r}]; runpy.run_path({script!r}, run_name='__main__')")
    return subprocess.run([sys.executable, "-c", code], cwd=relay_dir, env=env,
                          capture_output=True, text=True, timeout=60)


def test_check_flag_prints_the_table_and_exits_without_serving(r, relay_module, compat_db, tmp_path):
    p = run_as_main(relay_module, compat_db, tmp_path, "--check")
    assert p.returncode == 0, p.stderr[-2000:]
    out = p.stdout
    assert "[check] relay doctor" in out
    assert "chat.db" in out and "readable" in out.split("chat.db", 1)[1].splitlines()[0]
    assert "Uvicorn running" not in out + p.stderr
    for secret in (RELAY_STUB_ENV["IMSG_TOKEN"], RELAY_STUB_ENV["BB_PASSWORD"], RELAY_STUB_SELF):
        assert secret not in out.split("[check] relay doctor", 1)[1]
    assert str(tmp_path / "data") in out                        # the data dir row


SHIPPED_TOKEN_PLACEHOLDERS = ("change-me-to-a-long-random-string",      # .env.example
                              "CHANGE-ME-long-random-string")           # the launchd example


@pytest.mark.parametrize("token, word, why", [
    ("", "NOT SET", "IMSG_TOKEN is not set"),
    *[(p, "PLACEHOLDER", "IMSG_TOKEN is still the placeholder from the example file")
      for p in SHIPPED_TOKEN_PLACEHOLDERS],
])
def test_started_without_a_real_token_the_relay_exits_78_and_check_still_runs(
        r5, relay_module, compat_db, tmp_path, token, word, why):
    # python relay.py: a clear message on stderr, EX_CONFIG, no table, no server
    p = run_as_main(relay_module, compat_db, tmp_path, IMSG_TOKEN=token)
    assert p.returncode == 78 == r5.EX_CONFIG, (p.stdout[-800:], p.stderr[-800:])
    assert f"[auth] refusing to start: {why}." in p.stderr
    assert "openssl rand -hex 32" in p.stderr and "IMSG_ALLOW_NO_TOKEN=1" in p.stderr
    assert "Traceback" not in p.stderr
    assert "Uvicorn running" not in p.stdout + p.stderr and "[check] relay doctor" not in p.stdout
    if token:
        assert token not in p.stdout + p.stderr
    # stderr: three [auth] lines, the first of which is the refusal. stdout
    # (relay.log under launchd): ONE [auth] line that states the configuration
    # and does not say the relay is answering without authentication.
    auth_err = [line for line in p.stderr.splitlines() if line.startswith("[auth]")]
    assert len(auth_err) == 3 and auth_err[0].startswith("[auth] refusing to start: ")
    assert not any(line.startswith("[auth] refusing to start") for line in auth_err[1:])
    auth_out = [line for line in p.stdout.splitlines() if line.startswith("[auth]")]
    assert len(auth_out) == 1
    if token:
        assert auth_out[0].startswith("[auth] IMSG_TOKEN IS A PLACEHOLDER — every request is refused")
    else:
        assert auth_out[0].startswith("[auth] NO TOKEN SET — relay.py does not start like this")
    assert "WITHOUT authentication" not in p.stdout and "safe on LAN" not in p.stdout
    # python relay.py --check: still prints the table, with the finding in the token row
    p = run_as_main(relay_module, compat_db, tmp_path, "--check", IMSG_TOKEN=token)
    assert p.returncode == 0, p.stderr[-2000:]
    row = next(line for line in p.stdout.splitlines() if line.strip().startswith("token (IMSG_TOKEN)"))
    assert word in row and "refuses to start" in row
    if token:
        assert token not in p.stdout + p.stderr


@pytest.mark.parametrize("bind, host", [(None, "0.0.0.0"), ("127.0.0.1", "127.0.0.1")])
def test_started_with_a_token_the_relay_binds_imsg_bind_and_prints_the_table_first(
        r5, relay_module, compat_db, tmp_path, bind, host):
    """``python relay.py`` with a real token: the doctor table, then
    ``uvicorn.run(app, host=IMSG_BIND, port=IMSG_PORT, log_config=<masked>)``.
    No ``IMSG_BIND`` (or an empty one) is ``0.0.0.0``, what the relay always
    did. Both streams are the masking wrappers by then."""
    p = run_as_main(relay_module, compat_db, tmp_path, fake_uvicorn=True, IMSG_BIND=bind)
    assert p.returncode == 0, p.stderr[-2000:]
    lines = p.stdout.splitlines()
    run_line = next(i for i, line in enumerate(lines) if line.startswith("UVICORN-RUN"))
    table = next(i for i, line in enumerate(lines) if line == "[check] relay doctor")
    assert table < run_line                                              # printed before the server starts
    assert lines[run_line] == f"UVICORN-RUN host={host} port=9 log_config=dict stdout=MaskingStream stderr=MaskingStream"
    row = next(line for line in lines if line.strip().startswith("listening on"))
    assert f"{host}:9" in row
    assert ("IMSG_BIND=127.0.0.1" in row) is (host == "0.0.0.0")         # the hint, only for every interface
    assert "refusing to start" not in p.stderr


def test_allow_no_token_starts_the_relay_without_authentication_and_says_so(r5, relay_module, compat_db, tmp_path):
    token = "CHANGE-ME-long-random-string"                 # the unset case differs only in the row's word
    p = run_as_main(relay_module, compat_db, tmp_path, fake_uvicorn=True,
                    IMSG_TOKEN=token, IMSG_ALLOW_NO_TOKEN="1", IMSG_BIND="127.0.0.1")
    assert p.returncode == 0, p.stderr[-2000:]
    assert "UVICORN-RUN host=127.0.0.1" in p.stdout
    assert ("[auth] NO TOKEN SET — IMSG_ALLOW_NO_TOKEN is 1: every request is answered WITHOUT authentication"
            in p.stdout)
    row = next(line for line in p.stdout.splitlines() if line.strip().startswith("token (IMSG_TOKEN)"))
    assert "PLACEHOLDER" in row and "IMSG_ALLOW_NO_TOKEN is 1: running WITHOUT authentication" in row
    assert "***" not in row                                # the startup table is printed through the masking stream
    assert "refusing to start" not in p.stderr
    assert token not in p.stdout + p.stderr
    # anything but "1" is not the flag
    p = run_as_main(relay_module, compat_db, tmp_path, fake_uvicorn=True, IMSG_TOKEN="", IMSG_ALLOW_NO_TOKEN="true",
                    IMSG_BIND="127.0.0.1")
    assert p.returncode == 78 and "UVICORN-RUN" not in p.stdout


@pytest.mark.parametrize("bind", [None, "192.168.0.10"])       # the other spellings: the unit test below
def test_allow_no_token_is_honoured_only_on_a_loopback_bind(r5, relay_module, compat_db, tmp_path, bind):
    """``IMSG_ALLOW_NO_TOKEN=1 python relay.py`` with no ``IMSG_BIND`` would be
    an unauthenticated relay on every interface (the built-in bind address).
    The flag lifts the refusal only together with a loopback bind."""
    p = run_as_main(relay_module, compat_db, tmp_path, fake_uvicorn=True,
                    IMSG_TOKEN="", IMSG_ALLOW_NO_TOKEN="1", IMSG_BIND=bind)
    assert p.returncode == 78, (p.stdout[-800:], p.stderr[-800:])
    assert "UVICORN-RUN" not in p.stdout and "[check] relay doctor" not in p.stdout
    auth_err = [line for line in p.stderr.splitlines() if line.startswith("[auth]")]
    assert len(auth_err) == 3
    assert auth_err[0].startswith("[auth] refusing to start: IMSG_ALLOW_NO_TOKEN=1 needs a loopback bind")
    assert "IMSG_BIND=127.0.0.1" in auth_err[1] and "openssl rand -hex 32" in auth_err[2]
    assert "WITHOUT authentication" not in p.stdout       # nothing in relay.log claims it is running open
    assert "[auth] NO TOKEN SET — relay.py does not start like this" in p.stdout
    # the doctor says the same without starting
    p = run_as_main(relay_module, compat_db, tmp_path, "--check",
                    IMSG_TOKEN="", IMSG_ALLOW_NO_TOKEN="1", IMSG_BIND=bind)
    assert p.returncode == 0, p.stderr[-2000:]
    row = next(line for line in p.stdout.splitlines() if line.strip().startswith("token (IMSG_TOKEN)"))
    assert "NOT SET" in row and "refuses to start" in row and "loopback" in row


@pytest.mark.parametrize("bind", ["::1", "localhost"])          # 127.0.0.1 is the test above
def test_allow_no_token_starts_on_any_loopback_spelling(r5, relay_module, compat_db, tmp_path, bind):
    p = run_as_main(relay_module, compat_db, tmp_path, fake_uvicorn=True,
                    IMSG_TOKEN="", IMSG_ALLOW_NO_TOKEN="1", IMSG_BIND=bind)
    assert p.returncode == 0, p.stderr[-2000:]
    assert f"UVICORN-RUN host={bind} " in p.stdout and "refusing to start" not in p.stderr


def test_startup_refusal_is_lifted_by_a_real_token_or_the_explicit_flag(r5, monkeypatch):
    assert r5.IMSG_TOKEN == RELAY_STUB_ENV["IMSG_TOKEN"]
    assert r5.startup_refusal() is None                                    # a real token
    monkeypatch.setattr(r5, "IMSG_TOKEN", "")
    monkeypatch.setattr(r5, "ALLOW_NO_TOKEN", False)
    assert "IMSG_TOKEN is not set" in r5.startup_refusal()
    assert r5.auth_status_line().startswith("[auth] NO TOKEN SET — relay.py does not start like this")
    monkeypatch.setattr(r5, "PLACEHOLDER_KEYS", frozenset({"IMSG_TOKEN"}))
    assert "still the placeholder" in r5.startup_refusal()
    monkeypatch.setattr(r5, "ALLOW_NO_TOKEN", True)                        # IMSG_ALLOW_NO_TOKEN=1 ...
    assert r5.BIND == "0.0.0.0"
    refusal = r5.startup_refusal()                                         # ... on every interface: still refused
    assert refusal.startswith("[auth] refusing to start: IMSG_ALLOW_NO_TOKEN=1 needs a loopback bind")
    assert refusal.count("\n") == 2 and all(line.startswith("[auth] ") for line in refusal.splitlines())
    assert "WITHOUT authentication" not in r5.auth_status_line()
    for bind in ("192.168.0.10", "::", "0", "relay.example.test"):
        monkeypatch.setattr(r5, "BIND", bind)
        assert r5.startup_refusal() is not None, bind
    for bind in ("127.0.0.1", "::1", "localhost"):                         # ... on loopback: lifted
        monkeypatch.setattr(r5, "BIND", bind)
        assert r5.startup_refusal() is None, bind
    assert r5.auth_status_line().startswith("[auth] NO TOKEN SET — IMSG_ALLOW_NO_TOKEN is 1: ")
    assert "WITHOUT authentication" in r5.auth_status_line()
    # a real token starts on any bind address, flag or no flag
    monkeypatch.setattr(r5, "IMSG_TOKEN", RELAY_STUB_ENV["IMSG_TOKEN"])
    monkeypatch.setattr(r5, "BIND", "0.0.0.0")
    assert r5.startup_refusal() is None and r5.auth_status_line() == "[auth] token required"


def test_importing_the_module_never_exits_whatever_the_token(r5, relay_module, compat_db, tmp_path):
    """``uvicorn relay:app`` and the tests import the module: no exit, with no
    token, with a placeholder, and with the flag. What the import holds: a
    placeholder is never the token; without the flag it locks the API."""
    relay_dir = Path(relay_module.module.__file__).resolve().parent
    code = ("import sys, types, json; m = types.ModuleType('dotenv'); "
            "m.load_dotenv = lambda *a, **k: False; sys.modules['dotenv'] = m; "
            f"import {relay_module.name} as relay; "
            "print('STATE' + json.dumps([relay.IMSG_TOKEN, relay.AUTH_LOCKED, relay.ALLOW_NO_TOKEN, "
            "sorted(relay.PLACEHOLDER_KEYS), relay.startup_refusal() is None])); "
            "print('BIND' + relay.BIND)")
    base = dict(os.environ)
    for key in ("IMSG_ALLOW_NO_TOKEN", "IMSG_BIND"):
        base.pop(key, None)
    base.update(RELAY_STUB_ENV, IMSG_CHATDB=str(compat_db.path), IMSG_SELF=RELAY_STUB_SELF,
                IMSG_STATE=str(tmp_path / "relay_state.json"),
                HA_TOKEN="", MAPKIT_TOKEN="", FCM_CREDS="", BEEPER_TOKEN="")
    import json
    refused = "NO TOKEN SET — relay.py does not start like this"
    open_ = "NO TOKEN SET — IMSG_ALLOW_NO_TOKEN is 1: every request is answered WITHOUT authentication"
    # the last element of each state: would `python relay.py` start with this configuration?
    cases = [({"IMSG_TOKEN": ""}, ["", False, False, [], False], refused),
             ({"IMSG_TOKEN": "CHANGE-ME-long-random-string"},
              ["", True, False, ["IMSG_TOKEN"], False], "IMSG_TOKEN IS A PLACEHOLDER"),
             # the flag, but on the built-in bind address (every interface): still not started
             ({"IMSG_TOKEN": "change-me-to-a-long-random-string", "IMSG_ALLOW_NO_TOKEN": "1"},
              ["", False, True, ["IMSG_TOKEN"], False], refused),
             ({"IMSG_TOKEN": "", "IMSG_ALLOW_NO_TOKEN": "1"}, ["", False, True, [], False], refused),
             # the flag on loopback: started, and the line says what that means
             ({"IMSG_TOKEN": "change-me-to-a-long-random-string", "IMSG_ALLOW_NO_TOKEN": "1",
               "IMSG_BIND": "127.0.0.1"}, ["", False, True, ["IMSG_TOKEN"], True], open_),
             ({"IMSG_TOKEN": "", "IMSG_ALLOW_NO_TOKEN": "1", "IMSG_BIND": "127.0.0.1"},
              ["", False, True, [], True], open_),
             ({"IMSG_TOKEN": "stub-token-for-tests-not-real", "IMSG_ALLOW_NO_TOKEN": "yes"},
              ["stub-token-for-tests-not-real", False, False, [], True], "token required")]
    for extra, expected, auth_line in cases:
        p = subprocess.run([sys.executable, "-c", code], cwd=relay_dir, env={**base, **extra},
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, (extra, p.stderr[-1500:])
        state = json.loads(next(l for l in p.stdout.splitlines() if l.startswith("STATE"))[5:])
        assert state == expected, extra
        auth_lines = [l for l in p.stdout.splitlines() if l.startswith("[auth]")]
        assert len(auth_lines) == 1 and auth_lines[0].startswith(f"[auth] {auth_line}"), extra
        assert "safe on LAN" not in p.stdout
        assert f"BIND{extra.get('IMSG_BIND', '0.0.0.0')}" in p.stdout.splitlines()   # unset: every interface
    # IMSG_BIND as the module reads it: empty or blank is the built-in default
    for bind, expected in (("", "0.0.0.0"), ("  ", "0.0.0.0"), ("127.0.0.1", "127.0.0.1"),
                           (" 192.168.0.10 ", "192.168.0.10"), ("::1", "::1")):
        p = subprocess.run([sys.executable, "-c", code], cwd=relay_dir, env={**base, "IMSG_BIND": bind},
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, (bind, p.stderr[-1500:])
        assert f"BIND{expected}" in p.stdout.splitlines(), bind


def test_http_probe_retries_once_after_a_miss(r, monkeypatch):
    """A service that misses one probe right after a restart is still
    reported by its real status; two misses are a None, and never a third try."""
    import httpx as _httpx

    calls = []

    class FlakyClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            calls.append(url)
            if len(calls) < fail_first:
                raise _httpx.ConnectError("synthetic refused")
            return _httpx.Response(200)

    monkeypatch.setattr(r.time, "sleep", lambda s: None)
    monkeypatch.setattr(r.httpx, "Client", FlakyClient)

    fail_first = 2          # first call misses, second answers
    assert r._http_probe("http://192.168.0.2:8123/api/") == 200
    assert len(calls) == 2

    calls.clear(); fail_first = 99
    assert r._http_probe("http://192.168.0.2:8123/api/") is None
    assert len(calls) == 2
