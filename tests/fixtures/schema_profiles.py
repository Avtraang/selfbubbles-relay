"""Synthetic ``chat.db`` DDL subsets for four macOS generations (DESIGN.md section 8.1).

Each profile is the same seven tables with the real key structure
(``ROWID INTEGER PRIMARY KEY AUTOINCREMENT``, ``guid TEXT UNIQUE NOT NULL``,
composite keys on the join tables) and a profile-dependent set of optional
columns:

- ``macos14``: no ``associated_message_emoji``, ``filter_action``, ``date_retracted``,
  ``date_updated``, ``thread_originator_part``, ``chat.is_filtered``
  (and, by construction, none of the macos27 additions).
- ``macos15``: + ``thread_originator_part``.
- ``macos26``: + ``associated_message_emoji``, ``filter_action``, ``date_retracted``,
  ``chat.is_filtered``.
- ``macos27``: + ``date_updated``, ``is_spam``, ``group_title``.

Only column *names* and declared types are modelled; nothing here is copied
from a real database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

PROFILES: tuple[str, ...] = ("macos14", "macos15", "macos26", "macos27")

TABLES: tuple[str, ...] = (
    "handle",
    "chat",
    "message",
    "attachment",
    "chat_message_join",
    "chat_handle_join",
    "message_attachment_join",
)

# Columns present in every profile, in CREATE TABLE order.
_BASE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "handle": [
        ("ROWID", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("id", "TEXT NOT NULL"),
        ("country", "TEXT"),
        ("service", "TEXT NOT NULL"),
        ("uncanonicalized_id", "TEXT"),
        ("person_centric_id", "TEXT"),
    ],
    "chat": [
        ("ROWID", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("guid", "TEXT UNIQUE NOT NULL"),
        ("style", "INTEGER"),
        ("chat_identifier", "TEXT"),
        ("service_name", "TEXT"),
        ("display_name", "TEXT"),
        ("group_id", "TEXT"),
        ("is_archived", "INTEGER DEFAULT 0"),
        ("last_read_message_timestamp", "INTEGER DEFAULT 0"),
    ],
    "message": [
        ("ROWID", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("guid", "TEXT UNIQUE NOT NULL"),
        ("text", "TEXT"),
        ("handle_id", "INTEGER DEFAULT 0"),
        ("service", "TEXT"),
        ("date", "INTEGER"),
        ("date_read", "INTEGER"),
        ("date_delivered", "INTEGER"),
        ("is_from_me", "INTEGER DEFAULT 0"),
        ("cache_has_attachments", "INTEGER DEFAULT 0"),
        ("item_type", "INTEGER DEFAULT 0"),
        ("group_action_type", "INTEGER DEFAULT 0"),
        ("balloon_bundle_id", "TEXT"),
        ("payload_data", "BLOB"),
        ("expressive_send_style_id", "TEXT"),
        ("associated_message_guid", "TEXT"),
        ("associated_message_type", "INTEGER DEFAULT 0"),
        ("attributedBody", "BLOB"),
        ("date_edited", "INTEGER DEFAULT 0"),
        ("thread_originator_guid", "TEXT"),
    ],
    "attachment": [
        ("ROWID", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("guid", "TEXT UNIQUE NOT NULL"),
        ("created_date", "INTEGER DEFAULT 0"),
        ("filename", "TEXT"),
        ("uti", "TEXT"),
        ("mime_type", "TEXT"),
        ("transfer_name", "TEXT"),
        ("total_bytes", "INTEGER DEFAULT 0"),
        ("is_sticker", "INTEGER DEFAULT 0"),
        ("hide_attachment", "INTEGER DEFAULT 0"),
    ],
    "chat_message_join": [
        ("chat_id", "INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE"),
        ("message_id", "INTEGER REFERENCES message (ROWID) ON DELETE CASCADE"),
        ("message_date", "INTEGER DEFAULT 0"),
    ],
    "chat_handle_join": [
        ("chat_id", "INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE"),
        ("handle_id", "INTEGER REFERENCES handle (ROWID) ON DELETE CASCADE"),
    ],
    "message_attachment_join": [
        ("message_id", "INTEGER REFERENCES message (ROWID) ON DELETE CASCADE"),
        ("attachment_id", "INTEGER REFERENCES attachment (ROWID) ON DELETE CASCADE"),
    ],
}

# Table-level constraints appended after the column list.
_TABLE_CONSTRAINTS: dict[str, list[str]] = {
    "handle": ["UNIQUE (id, service)"],
    "chat_message_join": ["PRIMARY KEY (chat_id, message_id)"],
    "chat_handle_join": ["UNIQUE (chat_id, handle_id)"],
    "message_attachment_join": ["UNIQUE (message_id, attachment_id)"],
}

# Optional columns added per generation, cumulative in PROFILES order.
_ADDED_BY_PROFILE: dict[str, list[tuple[str, str, str]]] = {
    "macos14": [],
    "macos15": [
        ("message", "thread_originator_part", "TEXT"),
    ],
    "macos26": [
        ("message", "associated_message_emoji", "TEXT"),
        ("message", "filter_action", "INTEGER DEFAULT 0"),
        ("message", "date_retracted", "INTEGER DEFAULT 0"),
        ("chat", "is_filtered", "INTEGER DEFAULT 0"),
    ],
    "macos27": [
        ("message", "date_updated", "INTEGER DEFAULT 0"),
        ("message", "is_spam", "INTEGER DEFAULT 0"),
        ("message", "group_title", "TEXT"),
    ],
}


def _check_profile(profile: str) -> None:
    if profile not in PROFILES:
        raise ValueError(f"unknown schema profile {profile!r}; expected one of {PROFILES}")


def columns(profile: str) -> dict[str, list[tuple[str, str]]]:
    """Return ``{table: [(column, decltype), ...]}`` for ``profile``."""
    _check_profile(profile)
    out: dict[str, list[tuple[str, str]]] = {t: list(cols) for t, cols in _BASE_COLUMNS.items()}
    for p in PROFILES:
        for table, name, decl in _ADDED_BY_PROFILE[p]:
            out[table].append((name, decl))
        if p == profile:
            break
    return out


def column_names(profile: str) -> dict[str, list[str]]:
    """Return ``{table: [column, ...]}`` for ``profile``."""
    return {t: [name for name, _ in cols] for t, cols in columns(profile).items()}


def all_optional_columns() -> frozenset[str]:
    """Every ``"table.column"`` that some profile lacks (present only in the newest)."""
    full = column_names(PROFILES[-1])
    oldest = column_names(PROFILES[0])
    return frozenset(
        f"{t}.{c}" for t, cols in full.items() for c in cols if c not in oldest[t]
    )


def missing_columns(profile: str) -> frozenset[str]:
    """``"table.column"`` names that ``profile`` lacks relative to the newest profile."""
    have = column_names(profile)
    full = column_names(PROFILES[-1])
    return frozenset(f"{t}.{c}" for t, cols in full.items() for c in cols if c not in have[t])


def ddl(profile: str) -> list[str]:
    """CREATE TABLE statements for ``profile`` in dependency order."""
    cols = columns(profile)
    statements: list[str] = []
    for table in TABLES:
        parts = [f"{name} {decl}" for name, decl in cols[table]]
        parts.extend(_TABLE_CONSTRAINTS.get(table, []))
        body = ",\n    ".join(parts)
        statements.append(f"CREATE TABLE {table} (\n    {body}\n)")
    return statements


def create_schema(conn: sqlite3.Connection, profile: str) -> None:
    """Execute the DDL for ``profile`` on an open connection."""
    for stmt in ddl(profile):
        conn.execute(stmt)


def table_columns(conn: sqlite3.Connection, tables: Iterable[str] = TABLES) -> dict[str, list[str]]:
    """Introspect ``PRAGMA table_info`` for ``tables`` on ``conn`` (test helper)."""
    out: dict[str, list[str]] = {}
    for table in tables:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        out[table] = [str(r[1]) for r in rows]
    return out
