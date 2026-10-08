"""Row builders for synthetic ``chat.db`` fixtures (DESIGN.md section 8.1).

All builders take an open *writer* connection (autocommit, as ``make_db`` yields
it) and return the new ROWID.  Columns that the connection's schema profile
lacks are silently dropped from the INSERT, so one test body can run on all
four profiles; use ``has_column`` when a test needs to branch.

Only synthetic handles may be used: ``+1555...`` numbers and addresses at
``*@example.invalid`` or ``*@example.com`` (both domains are reserved for
documentation and tests; ``tools/make_demo_db.py`` uses ``example.com`` so an
address looks ordinary in a screenshot).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

#: Apple-epoch nanoseconds for 2024-01-01T00:00:00Z
#: ((1704067200 - 978307200) * 1_000_000_000).
BASE_DATE_NS = 725_760_000_000_000_000

#: Spacing between auto-assigned message dates (one second).
DATE_STEP_NS = 1_000_000_000

SYNTHETIC_PHONE = "+15550001234"
SYNTHETIC_EMAIL = "test@example.invalid"

def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Column names of ``table`` on ``conn``.

    Not cached: ``PRAGMA table_info`` is microseconds, and caching by connection
    identity is unsafe once a closed connection's ``id()`` is recycled.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    cols = [str(r[1]) for r in rows]
    if not cols:
        raise ValueError(f"table {table!r} does not exist on this connection")
    return cols


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True when ``table.column`` exists on ``conn``."""
    return column in table_columns(conn, table)


def _insert(conn: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> int:
    present = table_columns(conn, table)
    row = {k: v for k, v in values.items() if k in present}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
    rowid = cur.lastrowid
    assert rowid is not None
    return int(rowid)


def _count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(row[0])


def add_handle(
    conn: sqlite3.Connection,
    id: str = SYNTHETIC_PHONE,
    service: str = "iMessage",
    *,
    country: str | None = None,
    uncanonicalized_id: str | None = None,
    person_centric_id: str | None = None,
) -> int:
    """Insert a ``handle`` row and return its ROWID."""
    return _insert(
        conn,
        "handle",
        {
            "id": id,
            "service": service,
            "country": country,
            "uncanonicalized_id": uncanonicalized_id,
            "person_centric_id": person_centric_id,
        },
    )


def handle_rowid(conn: sqlite3.Connection, id: str, *, create: bool = True) -> int:
    """ROWID of the handle with ``id``; created (iMessage) when missing and ``create``."""
    row = conn.execute(
        "SELECT ROWID FROM handle WHERE id = ? ORDER BY ROWID LIMIT 1", (id,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    if not create:
        raise LookupError(f"no handle with id {id!r}")
    return add_handle(conn, id)


def add_chat(
    conn: sqlite3.Connection,
    guid: str,
    style: int = 45,
    identifier: str | None = None,
    display_name: str | None = None,
    service_name: str | None = None,
    handles: Iterable[int | str] = (),
    *,
    group_id: str | None = None,
    is_archived: int = 0,
    is_filtered: int = 0,
    last_read_message_timestamp: int = 0,
) -> int:
    """Insert a ``chat`` row plus one ``chat_handle_join`` row per handle.

    ``style`` is 45 (one-to-one) or 43 (group).  ``handles`` may mix handle ROWIDs
    and handle id strings (looked up or created).  Group guids look like
    ``any;+;chat123``, one-to-one guids like ``any;-;+15550001234``.
    """
    if identifier is None:
        identifier = guid.split(";")[-1]
    chat_rowid = _insert(
        conn,
        "chat",
        {
            "guid": guid,
            "style": style,
            "chat_identifier": identifier,
            "display_name": display_name,
            "service_name": service_name,
            "group_id": group_id,
            "is_archived": is_archived,
            "is_filtered": is_filtered,
            "last_read_message_timestamp": last_read_message_timestamp,
        },
    )
    for h in handles:
        hid = h if isinstance(h, int) else handle_rowid(conn, h)
        _insert(conn, "chat_handle_join", {"chat_id": chat_rowid, "handle_id": hid})
    return chat_rowid


def add_message(
    conn: sqlite3.Connection,
    chat_rowid: int,
    *,
    text: str | None = None,
    body: bytes | None = None,
    date_ns: int | None = None,
    is_from_me: int = 0,
    handle: int | str | None = None,
    assoc_guid: str | None = None,
    assoc_type: int = 0,
    assoc_emoji: str | None = None,
    reply_to: str | None = None,
    reply_to_part: str | None = None,
    balloon: str | None = None,
    payload: bytes | None = None,
    service: str | None = "iMessage",
    has_att: int = 0,
    date_edited: int = 0,
    date_read: int = 0,
    join: bool = True,
    guid: str | None = None,
    date_delivered: int = 0,
    date_retracted: int = 0,
    date_updated: int = 0,
    item_type: int = 0,
    group_action_type: int = 0,
    group_title: str | None = None,
    filter_action: int = 0,
    is_spam: int = 0,
    expressive_send_style_id: str | None = None,
) -> int:
    """Insert a ``message`` row and (when ``join``) its ``chat_message_join`` row.

    ``date_ns`` defaults to ``BASE_DATE_NS + n * DATE_STEP_NS`` where ``n`` is the
    number of messages already in the table, so auto dates ascend with ROWID.
    ``handle`` is a handle ROWID or id string (``None`` -> ``handle_id = 0``, as
    chat.db stores for outgoing rows).  ``cache_has_attachments`` is ``has_att``;
    ``join=False`` leaves the message orphaned (no chat row).
    """
    n = _count(conn, "message")
    if date_ns is None:
        date_ns = BASE_DATE_NS + n * DATE_STEP_NS
    if guid is None:
        guid = f"SYN-MSG-{n + 1:06d}"
    if handle is None:
        handle_id = 0
    elif isinstance(handle, int):
        handle_id = handle
    else:
        handle_id = handle_rowid(conn, handle)
    msg_rowid = _insert(
        conn,
        "message",
        {
            "guid": guid,
            "text": text,
            "attributedBody": body,
            "date": date_ns,
            "is_from_me": is_from_me,
            "handle_id": handle_id,
            "associated_message_guid": assoc_guid,
            "associated_message_type": assoc_type,
            "associated_message_emoji": assoc_emoji,
            "thread_originator_guid": reply_to,
            "thread_originator_part": reply_to_part,
            "balloon_bundle_id": balloon,
            "payload_data": payload,
            "service": service,
            "cache_has_attachments": has_att,
            "date_edited": date_edited,
            "date_read": date_read,
            "date_delivered": date_delivered,
            "date_retracted": date_retracted,
            "date_updated": date_updated,
            "item_type": item_type,
            "group_action_type": group_action_type,
            "group_title": group_title,
            "filter_action": filter_action,
            "is_spam": is_spam,
            "expressive_send_style_id": expressive_send_style_id,
        },
    )
    if join:
        _insert(
            conn,
            "chat_message_join",
            {"chat_id": chat_rowid, "message_id": msg_rowid, "message_date": date_ns},
        )
    return msg_rowid


def add_attachment(
    conn: sqlite3.Connection,
    msg_rowid: int,
    guid: str,
    mime: str | None,
    transfer_name: str | None,
    filename: str | None,
    *,
    hide: int = 0,
    sticker: int = 0,
    uti: str | None = None,
    total_bytes: int = 0,
    created_date: int = 0,
) -> int:
    """Insert an ``attachment`` row plus its ``message_attachment_join`` row.

    Does not touch ``message.cache_has_attachments``; pass ``has_att=1`` to
    ``add_message`` for rows that should count as having attachments.
    """
    att_rowid = _insert(
        conn,
        "attachment",
        {
            "guid": guid,
            "mime_type": mime,
            "transfer_name": transfer_name,
            "filename": filename,
            "hide_attachment": hide,
            "is_sticker": sticker,
            "uti": uti,
            "total_bytes": total_bytes,
            "created_date": created_date,
        },
    )
    _insert(
        conn,
        "message_attachment_join",
        {"message_id": msg_rowid, "attachment_id": att_rowid},
    )
    return att_rowid


def set_date_edited(conn: sqlite3.Connection, msg_rowid: int, date_edited: int) -> None:
    """Bump ``message.date_edited`` in place (no-op when the column is absent)."""
    if has_column(conn, "message", "date_edited"):
        conn.execute("UPDATE message SET date_edited = ? WHERE ROWID = ?", (date_edited, msg_rowid))
