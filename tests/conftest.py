"""Shared fixtures for the relay's compatibility tests.

Adapted from imessage-chatdb's ``tests/conftest.py`` (the ``make_db`` factory and
the path guards); the suite-wide ``pristine_db`` and the per-profile
parametrisation were dropped because the relay harness builds one synthetic
``macos27`` database per test.

Every database a test touches is a synthetic file created under ``tmp_path``.
``assert_safe_db_path`` refuses anything under ``~/Library`` (where the real
``chat.db`` lives) and anything outside the test's ``tmp_path``; ``make_db``
calls it before creating a file.

That check is opt-in, so the autouse ``refuse_real_database`` fixture backs it
up for the whole session: ``sqlite3.connect`` is wrapped so that *any* open of
a file under ``~/Library`` - a plain path, a ``PathLike``, bytes, or the
``file:...?mode=ro`` URI that ``imessage_chatdb.open_connection`` builds -
raises ``UnsafeDatabasePath`` before SQLite ever sees the path.  This covers
``chatdb_adapter.db()`` (whose configured path is the relay's ``IMSG_CHATDB``,
which defaults to the real database) and the library itself, so nothing in
the harness can reach the real database by accident.

The plain functions (``guard_sqlite_connect``, ``create_fixture_db``,
``import_relay_stubbed``) are what the fixtures are built from; the golden
recorder (``tests/record_golden.py``) uses them outside pytest to build the
very same database and import the relay the very same way.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from tests.fixtures.schema_profiles import PROFILES, create_schema


class UnsafeDatabasePath(RuntimeError):
    """Raised when a test tries to use a database path that is not a tmp_path file."""


def _library_dir() -> Path:
    return (Path.home() / "Library").resolve()


def assert_safe_db_path(path: Path, tmp_path: Path) -> Path:
    """Return ``path`` resolved, or raise ``UnsafeDatabasePath``.

    The check is purely lexical (``Path.resolve`` + ``is_relative_to``); it never
    opens the file.
    """
    resolved = Path(path).expanduser().resolve()
    if resolved.is_relative_to(_library_dir()):
        raise UnsafeDatabasePath(f"refusing to touch a database under ~/Library: {resolved}")
    root = Path(tmp_path).resolve()
    if not resolved.is_relative_to(root):
        raise UnsafeDatabasePath(f"database path {resolved} is not under tmp_path {root}")
    return resolved


def connect_target(database: object) -> Path | None:
    """The file a ``sqlite3.connect`` ``database`` argument names, or ``None`` for in-memory.

    Understands plain paths (``str``, ``bytes``, ``os.PathLike``) and ``file:``
    URIs (``file:/abs/path?mode=ro`` as ``imessage_chatdb.open_connection``
    builds them, percent-encoding undone).  ``""``, ``:memory:`` and
    ``mode=memory`` URIs have no file.  Purely lexical: nothing is opened.
    """
    if isinstance(database, (bytes, os.PathLike)):
        text = os.fsdecode(database)
    elif isinstance(database, str):
        text = database
    else:
        return None
    if text.startswith("file:"):
        parts = urlsplit(text)
        if parse_qs(parts.query).get("mode") == ["memory"]:
            return None
        text = unquote(parts.path)
    if text == "" or text.startswith(":memory:"):
        return None
    return Path(text).expanduser().resolve()


def refuse_if_under_library(database: object) -> None:
    """Raise ``UnsafeDatabasePath`` when ``database`` names a file under ``~/Library``."""
    target = connect_target(database)
    if target is not None and target.is_relative_to(_library_dir()):
        raise UnsafeDatabasePath(f"refusing to open a database under ~/Library: {target}")


def guard_sqlite_connect(mp: pytest.MonkeyPatch) -> Callable[..., sqlite3.Connection]:
    """Wrap ``sqlite3.connect`` (through ``mp``) so ``~/Library`` is unreachable; returns the wrapper."""
    real_connect = sqlite3.connect

    def guarded_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        database = args[0] if args else kwargs.get("database")
        refuse_if_under_library(database)
        return real_connect(*args, **kwargs)

    mp.setattr(sqlite3, "connect", guarded_connect)
    return guarded_connect


@pytest.fixture(scope="session", autouse=True)
def refuse_real_database() -> Iterator[Callable[..., sqlite3.Connection]]:
    """Install ``guard_sqlite_connect`` for the whole session.

    Yields the guarded ``connect`` so a test can assert it is installed.
    """
    with pytest.MonkeyPatch.context() as mp:
        yield guard_sqlite_connect(mp)


@dataclass
class FixtureDB:
    """A synthetic WAL-mode database built from one schema profile.

    ``writer`` is an autocommit connection (``isolation_level=None``) so builder
    inserts are immediately visible to any reader.
    """

    path: Path
    profile: str
    writer: sqlite3.Connection


MakeDB = Callable[..., FixtureDB]


def create_fixture_db(path: Path, profile: str = PROFILES[-1]) -> FixtureDB:
    """Create ``path`` (must not exist) with ``PRAGMA journal_mode=WAL`` and the profile's DDL.

    ``path`` is taken as already checked (``assert_safe_db_path``); the caller
    owns the writer connection.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    writer = sqlite3.connect(path, isolation_level=None)
    mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    assert str(mode).lower() == "wal", mode
    create_schema(writer, profile)
    return FixtureDB(path=path, profile=profile, writer=writer)


@pytest.fixture
def safe_db_path(tmp_path: Path) -> Callable[[Path], Path]:
    """The path guard, bound to this test's ``tmp_path``."""

    def _check(path: Path) -> Path:
        return assert_safe_db_path(path, tmp_path)

    return _check


@pytest.fixture
def make_db(tmp_path: Path) -> Iterator[MakeDB]:
    """Factory: ``make_db(profile, name=None) -> FixtureDB`` under ``tmp_path``.

    Writer connections are closed at teardown.
    """
    opened: list[sqlite3.Connection] = []
    counter = 0

    def _make(profile: str = PROFILES[-1], name: str | None = None) -> FixtureDB:
        nonlocal counter
        counter += 1
        if name is None:
            name = f"chat-{profile}-{counter}.db"
        fx = create_fixture_db(assert_safe_db_path(tmp_path / name, tmp_path), profile)
        opened.append(fx.writer)
        return fx

    yield _make

    for conn in opened:
        conn.close()


# ---------------------------------------------------------------------------
# relay.py under a stub environment (DESIGN.md section 7.4, gate 1: "the
# test imports relay with stub env")
# ---------------------------------------------------------------------------

#: Which relay module the glue tests import: ``relay`` (the running one) by
#: default; ``RELAY_MODULE=relay_new`` points them at the next version before a
#: cutover.  Read once at collection so every test in the session sees one module.
RELAY_MODULE = os.environ.get("RELAY_MODULE", "").strip() or "relay"


@dataclass
class RelayNew:
    """The relay module (``RELAY_MODULE``) imported once per session, plus what its import did.

    ``module`` is the imported module and ``name`` the module name it was
    imported under.  ``import_hooks`` is a snapshot of the adapter's bound
    hooks taken immediately after the import, i.e. what the relay's own
    ``chatdb_adapter.configure(...)`` call wired (later tests re-configure the
    shared adapter module, so this cannot be checked later).
    ``configure(path)`` re-binds the adapter to a synthetic database with
    relay's OWN hooks -- the same call relay makes at import -- so a test
    can exercise the shipped relay glue against its ``make_db`` database.
    ``state_path`` is the relay's state file and ``chatdb_path`` the
    (uncreated) placeholder database the import was pointed at, both under
    the session tmp dir.
    """

    module: Any
    import_hooks: dict[str, Any]
    state_path: Path
    chatdb_path: Path
    name: str = "relay"

    def configure(self, path: Path) -> None:
        r = self.module
        r.chatdb_adapter.configure(chatdb_path=str(path), resolve=r.resolve,
                                   att_public=r.att_public, person_key=r.person_key,
                                   group_title=r.group_title, self_raw=r.SELF_RAW)


# Placeholder values only: never the real token / password, never read from .env.
RELAY_STUB_ENV = {
    "IMSG_TOKEN": "stub-token-for-tests-not-real",
    "BB_PASSWORD": "stub-bb-password-not-real",
    "BB_URL": "http://127.0.0.1:9",          # discard port: nothing answers
    "BEEPER_TOKEN": "",                       # beeper disabled
    "FCM_CREDS": "",                          # no firebase
    "IMSG_POLL_SECONDS": "0.01",
    "TEXT_RELAY_LABEL": "stub relay phone",
    # Step R6: the edit engine is OFF unless a test points it at a fake binary
    # under tmp_path. Without this line a machine that has imessage-cli
    # installed would put the real tool (which drives the real Messages.app)
    # into the chain of every relay the suite imports or starts.
    "IMESSAGE_CLI": "0",
}

#: The synthetic own identity the relay is imported with (``IMSG_SELF``).
RELAY_STUB_SELF = "+15550009999"


def import_relay_stubbed(mp: pytest.MonkeyPatch, name: str, base: Path) -> RelayNew:
    """Import the relay module ``name`` with placeholder env, pointed at paths under ``base``.

    * ``dotenv`` is replaced by a no-op stub in ``sys.modules`` BEFORE the import,
      so relay's ``load_dotenv(.../.env)`` never reads the real ``.env``.
    * ``IMSG_CHATDB`` names an (uncreated) file under ``base`` -- the adapter
      opens nothing at configure time; callers re-point it with
      ``RelayNew.configure``.  ``IMSG_STATE`` is a file under ``base`` too, so the
      relay's real ``relay_state.json`` is neither read nor written.
    * ``IMSG_SELF`` is a synthetic number.
    Nothing network-facing runs at import (startup hooks are not invoked).

    The patches live on ``mp`` (the caller decides when they are undone).  A
    ``name`` that does not exist (``relay_new`` before it is written) fails with
    a clear message rather than silently importing ``relay``.
    """
    import importlib
    import importlib.util
    import sys
    import types

    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    chatdb_path = assert_safe_db_path(base / "placeholder-chat.db", base)
    state_path = assert_safe_db_path(base / "relay_state.json", base)
    env = dict(RELAY_STUB_ENV, IMSG_CHATDB=str(chatdb_path), IMSG_STATE=str(state_path),
               IMSG_SELF=RELAY_STUB_SELF)

    if name in sys.modules:
        raise RuntimeError(f"{name} was imported before the stub environment was set")
    if importlib.util.find_spec(name) is None:
        raise RuntimeError(f"RELAY_MODULE={name!r} but no such module is importable "
                           f"(is {name}.py written yet?); unset RELAY_MODULE to test relay.py")

    stub_dotenv = types.ModuleType("dotenv")

    def _no_dotenv(*_a: Any, **_k: Any) -> bool:
        return False

    stub_dotenv.load_dotenv = _no_dotenv  # type: ignore[attr-defined]

    for k, v in env.items():
        mp.setenv(k, v)
    # Keys the stub environment leaves at the relay's built-in default, even
    # when the shell running the suite has them set (step R5).
    for k in ("IMSG_BIND", "IMSG_ALLOW_NO_TOKEN"):
        mp.delenv(k, raising=False)
    mp.setitem(sys.modules, "dotenv", stub_dotenv)
    relay = importlib.import_module(name)
    adapter = relay.chatdb_adapter
    hooks = {"resolve": adapter._resolve, "att_public": adapter._att_public,
             "person_key": adapter._person_key, "group_title": adapter._group_title,
             "self_raw": adapter._self_raw,
             "chatdb_path": Path(adapter._cdb().path)}
    return RelayNew(module=relay, import_hooks=hooks, state_path=state_path,
                    chatdb_path=chatdb_path, name=name)


@pytest.fixture(scope="session")
def relay_module(tmp_path_factory: pytest.TempPathFactory,
                 refuse_real_database: Callable[..., sqlite3.Connection]) -> Iterator[RelayNew]:
    """``import_relay_stubbed`` for the session: the module named by ``RELAY_MODULE``
    under the placeholder environment, its paths under the session tmp dir."""
    with pytest.MonkeyPatch.context() as mp:
        yield import_relay_stubbed(mp, RELAY_MODULE, tmp_path_factory.mktemp("relay_env"))


# ---------------------------------------------------------------------------
# step R5 (the pre-publication fixes, 2026-10-07)
# ---------------------------------------------------------------------------

def has_r5(module: Any) -> bool:
    """Does this relay module carry step R5? Its relay.py changes landed
    together (no ``/health?nonce=``, the placeholder and missing-token rules,
    ``IMSG_BIND``, the line-buffered ``MaskingStream``, the ``/bp_asset``
    allow-list, the robust ``chat.db`` doctor row, log lines without message
    text); ``startup_refusal`` is one of them. A module without it is
    ``relay.py`` before the swap, where the R5 tests skip."""
    return hasattr(module, "startup_refusal")


@pytest.fixture
def r5(relay_module: RelayNew) -> Any:
    """The relay under test; skips when it predates step R5 (``relay.py`` until the swap)."""
    if not has_r5(relay_module.module):
        pytest.skip(f"{relay_module.name} predates step R5 (the pre-publication fixes)")
    return relay_module.module


# ---------------------------------------------------------------------------
# step R6 (edit and undo send, 2026-10-07)
# ---------------------------------------------------------------------------

def has_r6(module: Any) -> bool:
    """Does this relay module carry step R6? Its relay.py changes landed
    together (``POST /edit`` and ``POST /unsend``, ``/health.capabilities``,
    the doctor's ``edit / unsend`` row, ``IMESSAGE_CLI``); the ``/edit`` route
    function is one of them. A module without it is ``relay.py`` before the
    swap, where the R6 tests skip."""
    return hasattr(module, "edit_message")


@pytest.fixture
def r6(relay_module: RelayNew) -> Any:
    """The relay under test; skips when it predates step R6 (``relay.py`` until the swap)."""
    if not has_r6(relay_module.module):
        pytest.skip(f"{relay_module.name} predates step R6 (edit and undo send)")
    return relay_module.module
