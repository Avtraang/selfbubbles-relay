"""The CAF voice-message glue in the relay module (``relay_new`` until the cutover).

iPhone voice messages are ``Audio Message.caf`` rows with a NULL mime that
Android cannot play.  The relay advertises them as ``audio/mp4`` (``att_meta`` /
``att_public``, ``?f=m4a`` URL) and ``GET /attachment/{guid}`` transcodes them
with ``afconvert`` into ``AUDIO_CACHE`` once, exactly the way the HEIC rule
works.  This file proves that side:

* ``_is_caf`` / ``att_meta`` / ``att_public`` for a CAF with and without a mime,
  the name mapping, the ``FAILED_CAF`` fallback and non-CAF audio untouched;
* ``/attachment`` for a REAL synthetic CAF (a 1-second WAV from the ``wave``
  module converted with ``afconvert -f caff``): 200, ``audio/mp4``, an ISO BMFF
  ``ftyp`` box, a ``.m4a`` Content-Disposition, the second request served from
  the cache, ``Range`` -> 206, a missing file -> 404, a corrupt CAF -> the
  original bytes with the guid in ``FAILED_CAF`` and a content-free log line;
* ``last_message_previews`` / ``thread_media`` / the thread messages all pick
  the rule up through ``att_public``.

No real database (the synthetic compat database plus rows added to the
per-test copy), no real cache directory (``AUDIO_CACHE`` is redirected under
``tmp_path``).  Every test skips cleanly when the selected relay module
(``RELAY_MODULE``) has no ``_is_caf`` -- i.e. ``relay.py`` before the cutover.
The tests that use the ``audio`` fixture also skip where ``afconvert`` is not
on ``PATH`` (it ships with macOS only, so: the Linux CI jobs): the fixture's
CAF is made with it and the relay transcodes with it.
"""

from __future__ import annotations

import math
import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from tests.compat_fixture import ALICE_PHONE, C1_GUID
from tests.conftest import RELAY_STUB_ENV
from tests.fixtures import builders
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)

STUB_TOKEN = RELAY_STUB_ENV["IMSG_TOKEN"]          # a placeholder, never the real token
AUTH = {"X-Imsg-Token": STUB_TOKEN}
VOICE = "\U0001F3A4 Voice message"
CAF_NAME = "Audio Message.caf"
M4A_NAME = "Audio Message.m4a"
IMMUTABLE = "private, max-age=31536000, immutable"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def r(relay_module, tmp_path, monkeypatch):
    """The relay module with ``AUDIO_CACHE`` under ``tmp_path`` and an empty
    ``FAILED_CAF``; skips when the module has no CAF glue (``relay.py`` today)."""
    mod = relay_module.module
    if not hasattr(mod, "_is_caf"):
        pytest.skip(f"{relay_module.name} has no CAF voice-message glue")
    cache = tmp_path / "audio_cache"
    cache.mkdir()
    monkeypatch.setattr(mod, "AUDIO_CACHE", cache)
    mod.FAILED_CAF.clear()
    yield mod
    mod.FAILED_CAF.clear()


def write_caf(path: Path) -> Path:
    """A real 1-second mono 24 kHz CAF (LEI16) at ``path``, via the ``wave``
    module and ``afconvert -f caff``."""
    wav = path.with_suffix(".wav")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 10)))
                               for i in range(24000)))
    subprocess.run(["afconvert", "-f", "caff", "-d", "LEI16", str(wav), str(path)],
                   check=True, capture_output=True, timeout=60)
    assert path.read_bytes()[:4] == b"caff"
    return path


@pytest.fixture
def audio(compat_db, r, tmp_path):
    """Audio rows added to the per-test compat database, all in chat C1:
    a real CAF with a NULL mime, a real CAF with ``audio/x-caf``, a CAF whose
    file is missing, a corrupt CAF, and a plain ``.m4a`` / ``.mp3``.

    Skips without ``afconvert`` (macOS only): ``write_caf`` needs it here and
    ``GET /attachment`` needs it in the relay."""
    if shutil.which("afconvert") is None:
        pytest.skip("afconvert is not on PATH (it ships with macOS only): "
                    "no CAF fixture and no m4a transcode without it")
    files = tmp_path / "audio_files"
    files.mkdir()
    caf = write_caf(files / "ok.caf")
    bad = files / "bad.caf"
    bad.write_bytes(b"this is not a CAF file\n")
    m4a = files / "clip.m4a"
    m4a.write_bytes(b"\x00\x00\x00\x1cftypM4A " + b"\x00" * 40)
    mp3 = files / "song.mp3"
    mp3.write_bytes(b"ID3" + b"\x00" * 40)

    w = compat_db.writer
    c1 = compat_db.chats["c1"]
    h = builders.handle_rowid(w, ALICE_PHONE, create=False)
    rows: dict[str, int] = {}

    def add(guid, mime, tname, fname, *, is_from_me=0):
        rowid = builders.add_message(w, c1, handle=h, has_att=1, guid=f"G-{guid}",
                                     is_from_me=is_from_me)
        builders.add_attachment(w, rowid, guid, mime, tname, fname)
        rows[guid] = rowid

    add("ATT-CAF-NULL", None, CAF_NAME, str(caf))
    add("ATT-CAF-MIME", "audio/x-caf", "voice.caf", str(caf))
    add("ATT-CAF-GONE", None, CAF_NAME, str(files / "gone.caf"))      # never written
    add("ATT-CAF-BAD", None, CAF_NAME, str(bad))
    add("ATT-M4A", "audio/mp4", "clip.m4a", str(m4a), is_from_me=1)
    add("ATT-MP3", "audio/mpeg", "song.mp3", str(mp3))
    return SimpleNamespace(rows=rows, caf=caf, bad=bad, m4a=m4a, mp3=mp3, files=files)


@pytest.fixture
def client(relay_module):
    """ASGI test client; no ``with``, so startup hooks (poll loop, FCM) never run."""
    return TestClient(relay_module.module.app)


# ---------------------------------------------------------------------------
# _is_caf / att_meta / att_public
# ---------------------------------------------------------------------------

def test_is_caf(r):
    assert r._is_caf(None, CAF_NAME) is True
    assert r._is_caf(None, "VOICE.CAF") is True
    assert r._is_caf("audio/x-caf", None) is True
    assert r._is_caf("audio/caf", "whatever") is True
    assert r._is_caf("AUDIO/X-CAF", None) is True
    assert r._is_caf("application/octet-stream", CAF_NAME) is True   # the name wins
    assert r._is_caf(None, None) is False
    assert r._is_caf("audio/mp4", "clip.m4a") is False
    assert r._is_caf("audio/mpeg", "song.mp3") is False
    assert r._is_caf(None, "notes.caf.txt") is False
    assert r._is_caf("image/heic", "IMG_0001.HEIC") is False


@pytest.mark.parametrize("mime,name,expected", [
    (None, CAF_NAME, M4A_NAME),                           # the iPhone row
    ("audio/x-caf", CAF_NAME, M4A_NAME),
    ("audio/caf", "voice.caf", "voice.m4a"),
    (None, "VOICE.CAF", "VOICE.m4a"),                     # case-insensitive suffix, stem kept
    (None, "a.b.caf", "a.b.m4a"),
    ("audio/x-caf", "recording", "recording.m4a"),        # no .caf suffix: appended
    ("audio/x-caf", "clip.m4a", "clip.m4a.m4a"),          # still appended (rule is literal)
    ("audio/caf", None, None),                            # no name stays no name
])
def test_att_meta_maps_caf_to_m4a(r, mime, name, expected):
    assert r.att_meta(mime, name) == ("audio/mp4", expected)


def test_att_public_advertises_m4a_for_caf(r):
    assert r.att_public("ATT-X", None, CAF_NAME) == ("audio/mp4", M4A_NAME, "/attachment/ATT-X?f=m4a")
    assert r.att_public("ATT-X", "audio/x-caf", "voice.caf") == ("audio/mp4", "voice.m4a", "/attachment/ATT-X?f=m4a")
    assert r.att_public("ATT-X", "audio/caf", None) == ("audio/mp4", None, "/attachment/ATT-X?f=m4a")


def test_att_public_failed_caf_falls_back_to_the_original(r):
    r.FAILED_CAF.add("ATT-X")
    assert r.att_public("ATT-X", None, CAF_NAME) == (None, CAF_NAME, "/attachment/ATT-X")
    assert r.att_public("ATT-X", "audio/x-caf", "voice.caf") == ("audio/x-caf", "voice.caf", "/attachment/ATT-X")
    # the set is per guid
    assert r.att_public("ATT-Y", None, CAF_NAME) == ("audio/mp4", M4A_NAME, "/attachment/ATT-Y?f=m4a")
    # att_meta is unconditional (the endpoint only consults it on success)
    assert r.att_meta(None, CAF_NAME) == ("audio/mp4", M4A_NAME)


@pytest.mark.parametrize("mime,name", [
    ("audio/mp4", "clip.m4a"), ("audio/mpeg", "song.mp3"), ("audio/x-m4a", "x.m4a"),
    ("audio/wav", "x.wav"), (None, "x.m4a"), (None, None), ("image/png", "pic.png"),
    ("video/quicktime", "clip.mov"), ("application/pdf", "doc.pdf"), (None, "notes.caf.txt"),
])
def test_non_caf_untouched(r, mime, name):
    assert r.att_meta(mime, name) == (mime, name)
    assert r.att_public("ATT-X", mime, name) == (mime, name, "/attachment/ATT-X")


def test_heic_rule_unchanged(r):
    assert r.att_meta(None, "IMG_0001.HEIC") == ("image/jpeg", "IMG_0001.jpg")
    assert r.att_public("ATT-H", "image/heic", "b.heic") == ("image/jpeg", "b.jpg", "/attachment/ATT-H?f=jpg")
    r.FAILED_HEIC.add("ATT-H")
    try:
        assert r.att_public("ATT-H", "image/heic", "b.heic") == ("image/heic", "b.heic", "/attachment/ATT-H")
    finally:
        r.FAILED_HEIC.discard("ATT-H")


# ---------------------------------------------------------------------------
# GET /attachment/{guid}
# ---------------------------------------------------------------------------

def _is_m4a(data: bytes) -> bool:
    return len(data) > 12 and data[4:8] == b"ftyp"


def _disposition_name(resp) -> str:
    """The file name a Content-Disposition header carries: Starlette writes
    ``filename="x"`` for plain ASCII names and ``filename*=utf-8''x%20y`` (RFC
    5987) when the name has a space or non-ASCII; both are read here."""
    header = resp.headers["content-disposition"]
    assert header.startswith("attachment;")
    m = re.search(r"filename\*=utf-8''([^;]+)", header)
    if m:
        return unquote(m.group(1))
    m = re.search(r'filename="([^"]*)"', header)
    assert m, header
    return m.group(1)


def test_attachment_transcodes_a_real_caf(audio, r, client):
    resp = client.get("/attachment/ATT-CAF-NULL", params={"f": "m4a"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mp4"
    assert resp.headers["cache-control"] == IMMUTABLE
    assert _disposition_name(resp) == M4A_NAME
    assert _disposition_name(resp).endswith(".m4a")
    assert _is_m4a(resp.content)
    assert resp.content != audio.caf.read_bytes()
    out = r.AUDIO_CACHE / "ATT-CAF-NULL.m4a"
    assert out.is_file() and out.read_bytes() == resp.content
    assert out.stat().st_size > 0
    assert [p.name for p in r.AUDIO_CACHE.iterdir()] == ["ATT-CAF-NULL.m4a"]   # no temp file left
    assert "ATT-CAF-NULL" not in r.FAILED_CAF
    # the mime-carrying row behaves the same, with its own name
    resp = client.get("/attachment/ATT-CAF-MIME", headers=AUTH)
    assert resp.status_code == 200 and resp.headers["content-type"] == "audio/mp4"
    assert _disposition_name(resp) == "voice.m4a"
    assert _is_m4a(resp.content)


def test_attachment_second_request_is_served_from_the_cache(audio, r, client, monkeypatch):
    first = client.get("/attachment/ATT-CAF-NULL", headers=AUTH)
    assert first.status_code == 200
    out = r.AUDIO_CACHE / "ATT-CAF-NULL.m4a"
    mtime = out.stat().st_mtime_ns

    def no_afconvert(*a, **k):
        raise AssertionError("afconvert must not run again for a cached guid")

    monkeypatch.setattr(r.subprocess, "run", no_afconvert)
    second = client.get("/attachment/ATT-CAF-NULL", headers=AUTH)
    assert second.status_code == 200
    assert second.content == first.content
    assert second.headers["content-type"] == "audio/mp4"
    assert second.headers["cache-control"] == IMMUTABLE
    assert out.stat().st_mtime_ns == mtime


def test_attachment_range_request_returns_206(audio, r, client):
    full = client.get("/attachment/ATT-CAF-NULL", headers=AUTH)
    assert full.status_code == 200
    part = client.get("/attachment/ATT-CAF-NULL", headers={**AUTH, "Range": "bytes=0-99"})
    assert part.status_code == 206
    assert part.content == full.content[:100]
    assert part.headers["content-range"] == f"bytes 0-99/{len(full.content)}"
    assert part.headers["content-type"] == "audio/mp4"
    tail = client.get("/attachment/ATT-CAF-NULL", headers={**AUTH, "Range": "bytes=100-"})
    assert tail.status_code == 206 and tail.content == full.content[100:]


def test_attachment_direct_call_returns_the_cached_file_response(audio, r):
    resp = r.attachment("ATT-CAF-NULL")
    assert isinstance(resp, FileResponse)
    assert Path(resp.path) == r.AUDIO_CACHE / "ATT-CAF-NULL.m4a"
    assert resp.media_type == "audio/mp4" and resp.filename == M4A_NAME


def test_attachment_missing_caf_file_is_404(audio, r, client):
    with pytest.raises(HTTPException) as ei:
        r.attachment("ATT-CAF-GONE")
    assert (ei.value.status_code, ei.value.detail) == (404, "file missing on disk")
    assert client.get("/attachment/ATT-CAF-GONE", headers=AUTH).status_code == 404
    assert not (r.AUDIO_CACHE / "ATT-CAF-GONE.m4a").exists()
    assert "ATT-CAF-GONE" not in r.FAILED_CAF
    assert client.get("/attachment/NO-SUCH-ATT", headers=AUTH).status_code == 404


def test_attachment_afconvert_failure_serves_the_original(audio, r, client, capsys):
    before = r.att_public("ATT-CAF-BAD", None, CAF_NAME)
    assert before == ("audio/mp4", M4A_NAME, "/attachment/ATT-CAF-BAD?f=m4a")
    resp = client.get("/attachment/ATT-CAF-BAD", params={"f": "m4a"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.content == audio.bad.read_bytes()                     # the original bytes
    assert resp.headers["content-type"].startswith("application/octet-stream")   # NULL mime default
    assert _disposition_name(resp) == CAF_NAME                        # the original name
    assert "ATT-CAF-BAD" in r.FAILED_CAF
    assert not (r.AUDIO_CACHE / "ATT-CAF-BAD.m4a").exists()
    assert list(r.AUDIO_CACHE.iterdir()) == []                        # no temp file left
    # the metadata now matches what is served
    assert r.att_public("ATT-CAF-BAD", None, CAF_NAME) == (None, CAF_NAME, "/attachment/ATT-CAF-BAD")
    # one content-free log line: guid and rc, no path, no name
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("[attachment]")]
    assert len(lines) == 1
    assert lines[0].startswith("[attachment] afconvert failed for ATT-CAF-BAD: rc=")
    assert str(audio.files) not in out and "bad.caf" not in out and "Audio Message" not in out
    # a second request fails again (nothing cached) and keeps serving the original
    again = client.get("/attachment/ATT-CAF-BAD", headers=AUTH)
    assert again.status_code == 200 and again.content == audio.bad.read_bytes()


def test_attachment_afconvert_timeout_is_handled(audio, r, client, capsys, monkeypatch):
    def timeout(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, k.get("timeout"))

    monkeypatch.setattr(r.subprocess, "run", timeout)
    resp = client.get("/attachment/ATT-CAF-NULL", headers=AUTH)
    assert resp.status_code == 200 and resp.content == audio.caf.read_bytes()
    assert "ATT-CAF-NULL" in r.FAILED_CAF
    assert list(r.AUDIO_CACHE.iterdir()) == []
    out = capsys.readouterr().out
    assert out.count("[attachment] afconvert failed for ATT-CAF-NULL: rc=") == 1
    assert str(audio.files) not in out


def test_attachment_non_caf_audio_is_served_as_is(audio, r, client, monkeypatch):
    def no_afconvert(*a, **k):
        raise AssertionError("no transcode for non-CAF audio")

    monkeypatch.setattr(r.subprocess, "run", no_afconvert)
    for guid, path, mime in (("ATT-M4A", audio.m4a, "audio/mp4"), ("ATT-MP3", audio.mp3, "audio/mpeg")):
        resp = client.get(f"/attachment/{guid}", headers=AUTH)
        assert resp.status_code == 200, guid
        assert resp.content == path.read_bytes()
        assert resp.headers["content-type"].startswith(mime)
        assert "cache-control" not in resp.headers or resp.headers["cache-control"] != IMMUTABLE
    assert list(r.AUDIO_CACHE.iterdir()) == [] and not r.FAILED_CAF
    # and the fixture's non-audio files still come back untouched
    assert client.get("/attachment/ATT-FILE-TXT", headers=AUTH).content == b"synthetic notes\n"


# ---------------------------------------------------------------------------
# previews, thread_media and thread messages: all through att_public
# ---------------------------------------------------------------------------

def test_previews_say_voice_message(audio, r):
    rows = audio.rows
    conn = r.db()
    try:
        n = r.last_message_previews(conn, list(rows.values()))
        assert n[rows["ATT-CAF-NULL"]]["body"] == VOICE          # NULL mime: att_public first
        assert n[rows["ATT-CAF-MIME"]]["body"] == VOICE
        assert n[rows["ATT-CAF-GONE"]]["body"] == VOICE          # previews never touch the disk
        assert n[rows["ATT-M4A"]]["body"] == VOICE               # any audio/*
        assert n[rows["ATT-MP3"]]["body"] == VOICE
        assert n[rows["ATT-M4A"]]["is_from_me"] == 1
        # a failed transcode keeps the audio/* mime when the row has one, so it
        # is still a voice message; a NULL-mime row falls back to its file name
        # (what the client is actually told), exactly like the HEIC rule.
        r.FAILED_CAF.update({"ATT-CAF-MIME", "ATT-CAF-BAD"})
        n = r.last_message_previews(conn, [rows["ATT-CAF-MIME"], rows["ATT-CAF-BAD"]])
        assert n[rows["ATT-CAF-MIME"]]["body"] == VOICE
        assert n[rows["ATT-CAF-BAD"]]["body"] == "\U0001F4CE " + CAF_NAME
    finally:
        conn.close()


def test_thread_media_and_messages_carry_the_m4a_metadata(audio, r, client):
    by = {a["guid"]: a for a in r.thread_media(C1_GUID)["attachments"]}
    assert {k: by["ATT-CAF-NULL"][k] for k in ("mime_type", "name", "url")} == \
        {"mime_type": "audio/mp4", "name": M4A_NAME, "url": "/attachment/ATT-CAF-NULL?f=m4a"}
    assert {k: by["ATT-CAF-MIME"][k] for k in ("mime_type", "name", "url")} == \
        {"mime_type": "audio/mp4", "name": "voice.m4a", "url": "/attachment/ATT-CAF-MIME?f=m4a"}
    assert {k: by["ATT-MP3"][k] for k in ("mime_type", "name", "url")} == \
        {"mime_type": "audio/mpeg", "name": "song.mp3", "url": "/attachment/ATT-MP3"}
    # the message attachments (adapter -> att_public hook) say the same
    resp = client.get(f"/thread/{C1_GUID}/messages", params={"limit": 50}, headers=AUTH)
    assert resp.status_code == 200
    msgs = {m["rowid"]: m for m in resp.json()["messages"]}
    att = msgs[audio.rows["ATT-CAF-NULL"]]["attachments"][0]
    assert (att["mime_type"], att["name"], att["url"]) == ("audio/mp4", M4A_NAME, "/attachment/ATT-CAF-NULL?f=m4a")
    # and the advertised URL is the one that serves the M4A
    served = client.get(att["url"], headers=AUTH)
    assert served.status_code == 200 and served.headers["content-type"] == "audio/mp4" and _is_m4a(served.content)
    # a failed guid is advertised as the original everywhere
    r.FAILED_CAF.add("ATT-CAF-NULL")
    by = {a["guid"]: a for a in r.thread_media(C1_GUID)["attachments"]}
    assert {k: by["ATT-CAF-NULL"][k] for k in ("mime_type", "name", "url")} == \
        {"mime_type": None, "name": CAF_NAME, "url": "/attachment/ATT-CAF-NULL"}


def test_notification_label_for_a_voice_message(audio, r):
    """``attachment_label`` (push text) branches on the client-facing mime the
    adapter already ran through ``att_public``, so a NULL-mime CAF is audio."""
    msg = next(m for m in r.fetch_new(0) if m["rowid"] == audio.rows["ATT-CAF-NULL"])
    assert msg["attachments"][0]["mime_type"] == "audio/mp4"
    assert r.attachment_label(msg["attachments"]) == "\U0001F3A4 Audio message"
