"""``requirements.txt`` pins the ``imessage-chatdb`` API the relay calls
(plan 2026-10-06, step R3: "requirements.txt pinned from the venv").

The relay's own venv carries an editable checkout of the library, so the
suite passes there whatever the pin says; a stranger's ``pip install -r
requirements.txt`` gets the pinned release. ``chatdb_adapter.search_rows`` passes
``chat_guid=`` unconditionally and the library's ``search()`` only grew that
keyword in 0.1.1, so a pin at 0.1.0 made every ``GET /search`` a 500 for
them. These tests keep the pin at or above that floor and on the same
minor as the library the relay is developed against.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import imessage_chatdb
from imessage_chatdb import search as lib_search
from packaging.version import Version

import chatdb_adapter

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"

#: Oldest release whose ``search()`` accepts ``chat_guid`` (library commit
#: "0.1.1: search() can be scoped to one chat").
MIN_CHATDB = Version("0.1.1")


def pinned_version(name: str) -> Version:
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(rf"{re.escape(name)}==(\S+)", line.strip())
        if m:
            return Version(m.group(1))
    raise AssertionError(f"{name} is not pinned (name==version) in requirements.txt")


def test_chatdb_pin_covers_the_search_scope_keyword():
    assert pinned_version("imessage-chatdb") >= MIN_CHATDB
    # the adapter passes it, and the library actually in use takes it
    assert "chat_guid" in inspect.signature(chatdb_adapter.search_rows).parameters
    assert "chat_guid" in inspect.signature(lib_search.search).parameters


def test_chatdb_pin_tracks_the_library_the_relay_is_developed_against():
    pin = pinned_version("imessage-chatdb")
    have = Version(imessage_chatdb.__version__)
    assert (pin.major, pin.minor) == (have.major, have.minor), \
        f"requirements.txt pins {pin} but the relay runs against {have}: bump the pin"
    assert pin <= have, f"the pin {pin} is newer than the library in use ({have})"


def test_requirements_header_says_how_it_was_made_and_names_no_editable_install():
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# SelfBubbles relay: runtime dependencies, pinned from")
    assert all(not l.startswith(("-e", "file:", "git+")) and "@ " not in l for l in lines), \
        "requirements.txt must install from PyPI only"
