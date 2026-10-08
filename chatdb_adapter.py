"""chatdb_adapter -- the relay's chat.db layer, rebuilt on the imessage-chatdb library.

Phase 1 of DESIGN.md section 7: every name the relay used for its own chat.db
code (``db``, ``apple_date_to_unix``, ``parse_attributed_body``, ``LINK_BALLOON``,
``fetch_new``, ``fetch_edited``, ``max_rowid``, ``max_date_edited``,
``fetch_thread_messages``, ``last_rowid_for``, ``find_chat_for_addresses``,
``_norm_service``, ``chat_services``) is defined here with the same signature and
the same JSON-visible output (same dict keys, same insertion order, same
strings), so relay call sites import them unchanged.

Phases 2 and 3 (``search_messages``, ``link_image``, ``/attachment``,
``/thumbnail``, ``thread_media``, ``fetch_threads``, ``contact_recency``) add the
thin passthroughs under "endpoint helpers": library query functions and pure
helpers re-exported under the names the relay imports, with no relay strings in
them.  The relay keeps every presentation rule (titles, "You"/"me", first names,
``att_public``, the 404 bodies, HEIC transcoding).

The relay-side hooks (``resolve``, ``att_public``, ``person_key``, ``group_title``,
``SELF_RAW``) are handed in through :func:`configure` and looked up at call time,
so this module never imports ``relay.py`` and can be unit-tested on a synthetic
database.  Importing this module opens nothing; the ``ChatDB`` is created by
``configure()`` and first touched by the first query.
"""

from __future__ import annotations

import plistlib
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from imessage_chatdb import (
    LINK_BALLOON,
    Attachment,
    ChatDB,
    CursorAhead,
    LinkPreview,
    Message,
    ReplyTarget,
    effective_text,
    embedded_images,
    extract_urls,
    snippet,
)
from imessage_chatdb import apple_to_unix as apple_date_to_unix
from imessage_chatdb import extract_text as parse_attributed_body
from imessage_chatdb import service_family as _norm_service
from imessage_chatdb import attachments as _attachments
from imessage_chatdb import messages as _messages
from imessage_chatdb import search as _search
from imessage_chatdb.chats import chat_services as _lib_chat_services
from imessage_chatdb.chats import chats_by_activity as _lib_chats_by_activity
from imessage_chatdb.chats import find_chat as _lib_find_chat
from imessage_chatdb.chats import (
    last_rowid_for,
    lite_messages,
    one_to_one_activity,
    participants,
    participants_map,
    unread_count,
)
from imessage_chatdb.models import ChatSummary, LiteMessage, SearchHit

__all__ = [
    "configure", "db", "apple_date_to_unix", "parse_attributed_body", "LINK_BALLOON", "relay_msg", "relay_att", "relay_link", "relay_reply",
    "fetch_new", "fetch_edited", "max_rowid", "max_date_edited", "fetch_thread_messages",
    "fetch_message",
    "last_rowid_for", "find_chat_for_addresses", "_norm_service", "chat_services",
    "participants", "lite_rows", "CursorAhead",
    # Phase 2 / 3 endpoint helpers
    "search_rows", "snippet", "extract_urls", "payload", "embedded_images",
    "attachment_by_guid", "chat_attachments", "recent_messages",
    "chats", "participants_map", "unread_count", "one_to_one_activity",
    # step R6: the facts the /edit and /unsend routes check before and after
    "ChangeTarget", "change_target",
]

Hook1 = Callable[[str | None], str | None]
AttPublic = Callable[[str, str | None, str | None], tuple[str | None, str | None, str]]
GroupTitle = Callable[[sqlite3.Connection, int, str | None], str | None]

_CDB: ChatDB | None = None
_resolve: Hook1
_att_public: AttPublic
_person_key: Hook1
_group_title: GroupTitle
_self_raw: Sequence[str] = ()


def configure(*, chatdb_path: str, resolve: Hook1, att_public: AttPublic, person_key: Hook1,
              group_title: GroupTitle, self_raw: Sequence[str], readonly_uri: bool = False) -> ChatDB:
    """Bind the relay's hooks and the database path; returns the ``ChatDB`` (nothing is opened)."""
    global _CDB, _resolve, _att_public, _person_key, _group_title, _self_raw
    _CDB = ChatDB(chatdb_path, timeout=5, readonly_uri=readonly_uri)
    # Stored as given (no copy): the relay's SELF_RAW list is read at call time.
    _resolve, _att_public, _person_key, _group_title, _self_raw = (
        resolve, att_public, person_key, group_title, self_raw)
    return _CDB


def _cdb() -> ChatDB:
    if _CDB is None:
        raise RuntimeError("chatdb_adapter.configure() has not been called")
    return _CDB


def db() -> sqlite3.Connection:
    """A fresh connection, as the relay's ``db()``; untouched endpoints still ``conn.close()``."""
    return _cdb().connect()


# ---------- model -> relay dict (the old literals, key for key, in the old order) ----------

def relay_att(a: Attachment) -> dict:
    mime, name, url = _att_public(a.guid, a.mime_type, a.transfer_name)
    return {"guid": a.guid, "mime_type": mime, "name": name, "url": url}


def relay_link(lp: LinkPreview | None, rowid: int) -> dict | None:
    if lp is None:
        return None
    return {"url": lp.url, "title": lp.title, "summary": lp.summary, "site": lp.site,
            "image": f"/link_image/{rowid}" if lp.has_embedded_image else lp.image_url}


def relay_reply(r: ReplyTarget | None) -> dict | None:
    if r is None:
        return None
    return {"text": (r.text or "Attachment")[:120],
            "sender": "You" if r.is_from_me else (_resolve(r.sender_handle) or "")}


def relay_msg(m: Message) -> dict:
    """The message dict the relay serves: the enriched ``Message`` (attachments,
    link preview and reply target already resolved) rendered through the relay's
    hooks, keys in the order the API has always emitted them."""
    return {
        "rowid": m.rowid, "guid": m.guid, "text": m.text,
        "date": apple_date_to_unix(m.date), "date_read": apple_date_to_unix(m.date_read),
        "date_edited": apple_date_to_unix(m.date_edited),
        "is_from_me": m.is_from_me,
        "sender": _resolve(m.sender_handle), "sender_handle": m.sender_handle,
        "chat_guid": m.chat_guid,
        "chat_name": ((m.chat_display_name or m.chat_identifier) if m.is_group
                      else _resolve(m.chat_identifier)),
        "is_group": m.is_group,
        "has_attachments": m.has_attachments,
        "assoc_guid": m.associated_guid, "assoc_type": m.associated_type,
        "attachments": [relay_att(a) for a in m.attachments],
        "link": relay_link(m.link, m.rowid),
        "reply_to_guid": m.reply_to_guid,
        "reply_to": relay_reply(m.reply_to),
        "service": m.service_raw,
    }


# ---------- fetches (one connection per call, enriched) ----------

def fetch_new(cursor: int) -> list[dict]:
    """Messages with ROWID > cursor; raises ``CursorAhead`` when the database was rebuilt
    (MAX(ROWID) < cursor) so the poll loop can re-initialise instead of waiting forever."""
    cdb = _cdb()
    with cdb.connection() as conn:
        top = _messages.max_rowid(conn)
        if top < cursor:
            raise CursorAhead(cursor, top)
        return [relay_msg(m) for m in _messages.messages_after(conn, cdb.schema, cursor)]


def fetch_edited(mark: int) -> tuple[list[dict], int]:
    msgs, new_mark = _cdb().messages_edited_after(mark)
    return [relay_msg(m) for m in msgs], new_mark


def max_rowid() -> int:
    return _cdb().max_rowid()


def max_date_edited() -> int:
    return _cdb().max_date_edited()


def fetch_thread_messages(chat_guid: str, limit: int, before_rowid: int | None) -> list[dict]:
    return [relay_msg(m) for m in
            _cdb().thread_messages(chat_guid, limit=limit, before_rowid=before_rowid)]


def fetch_message(rowid: int) -> dict | None:
    """One message by ROWID as the relay dict (enriched like every other fetch),
    or ``None`` when the row is gone; used to re-broadcast a message whose link
    card was upgraded in the background."""
    m = _cdb().message(rowid)
    return relay_msg(m) if m is not None else None


# ---------- chat-side helpers taking the caller's connection ----------

def lite_rows(conn: sqlite3.Connection, rowids: Iterable[int]) -> dict[int, LiteMessage]:
    """``lite_messages`` passthrough: the facts ``last_message_previews`` renders."""
    return lite_messages(conn, rowids)


def chat_services(conn: sqlite3.Connection, chat_rowids: Iterable[int]) -> dict:
    try:
        return _lib_chat_services(conn, chat_rowids)
    except Exception as e:
        print(f"[service] chat service lookup failed: {e}")
        return {}


def find_chat_for_addresses(conn: sqlite3.Connection, addrs: Sequence[str]) -> dict | None:
    m = _lib_find_chat(conn, addrs, key=_person_key, exclude=_self_raw)
    if not m:
        return None
    return {"chat_guid": m.chat_guid,
            "chat_name": (_group_title(conn, m.chat_rowid, m.display_name) if m.is_group
                          else _resolve(m.chat_identifier)),
            "last_rowid": m.last_rowid}


# ---------- endpoint helpers (Phase 2 / 3): library facts, relay presentation ----------
#
# Each is the library function the relay endpoint used to express as its own
# SQL, under the name the relay imports.  Those taking ``conn`` run on the
# endpoint's own ``db()`` connection (one consistent view per request, closed by
# the endpoint); the others are point lookups on the facade (one connection
# per call, as the old ``conn = db(); ...; conn.close()`` was).

def search_rows(conn: sqlite3.Connection, q: str, limit: int,
                chat_guid: str | None = None) -> list[SearchHit]:
    """The relay's SEARCH statement verbatim (``LIMIT limit * 2`` oversample) plus the
    Python recheck; each hit carries the cleaned text and the match index.
    ``chat_guid`` scopes it to one chat (the library's ``SEARCH_IN_CHAT``);
    ``None`` is the global search, unchanged."""
    return _search.search(conn, q, limit=limit, chat_guid=chat_guid)


def payload(rowid: int) -> bytes | None:
    """Raw ``message.payload_data`` for ``rowid``; ``None`` when absent or NULL."""
    return _cdb().payload(rowid)


def attachment_by_guid(guid: str) -> Attachment | None:
    """The ``attachment`` row with ``guid`` (``filename``, ``mime_type``, ``transfer_name``)."""
    return _cdb().attachment(guid)


def chat_attachments(conn: sqlite3.Connection, chat_guid: str) -> list[Attachment]:
    """Every attachment of the chat, ``ORDER BY m.date DESC`` (the relay's statement),
    ``.pluginPayloadAttachment`` rows skipped; ``message_date`` is the owning message's date."""
    return _attachments.chat_attachments(conn, chat_guid)


def recent_messages(conn: sqlite3.Connection, chat_guid: str, limit: int = 1000) -> list[Message]:
    """The newest ``limit`` messages of the chat, newest first (``ORDER BY m.ROWID DESC``),
    text decoded from the blob; not enriched (no attachment, link or reply lookups),
    which is all ``thread_media``'s link scan needs."""
    return _messages.recent_messages(conn, _cdb().schema, chat_guid, limit=limit)


def chats(conn: sqlite3.Connection, limit: int) -> list[ChatSummary]:
    """The relay's THREADS statement verbatim: chats with messages, newest activity first."""
    return _lib_chats_by_activity(conn, limit=limit)


# ---------- edit / unsend (step R6): one message's changeable state ----------
#
# The library (0.2) does not read ``message.message_summary_info``, the binary
# plist where Messages keeps what happened to a message after it was sent, so
# this is the adapter's one statement of its own.  Read-only like everything
# else here, on the facade's connection, with every optional column rendered
# as NULL when the schema lacks it (the library's ``select_or_null``).
#
# Keys of that plist as seen on macOS 27.0 after one real edit and one real
# unsend: ``ec`` (edit history: a dictionary keyed by part index, each value a
# list), ``ep`` (edited part indexes) and ``rp`` (retracted part indexes).
# ``date_retracted`` stayed 0 for the unsent row; its text was emptied and
# ``date_edited`` was set.

#: The parts of a message are numbered from 0; Apple's own limit is far below this.
MAX_PART_INDEX = 63


def _int_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v for v in value if isinstance(v, int) and not isinstance(v, bool))


@dataclass(frozen=True)
class ChangeTarget:
    """One message as the edit / unsend routes need to see it: raw facts only.

    ``date`` / ``date_edited`` / ``date_retracted`` are raw Apple integers (0
    for "never").  ``text`` is the text the relay serves (column, else blob).
    ``summary`` is the decoded ``message_summary_info`` dictionary; ``{}`` for
    a row that has none, ``None`` when it could not be read at all (the column
    is missing from this schema, or the blob is not a property list).
    ``associated_message_type`` and ``item_type`` are the two columns that
    tell a message from the other rows of the ``message`` table: both are 0
    for a message somebody wrote (0 too when the schema lacks the column)."""

    rowid: int
    guid: str
    chat_guid: str
    is_from_me: bool
    date: int
    date_edited: int
    date_retracted: int
    text: str | None
    service: str | None
    summary: dict | None
    associated_message_type: int = 0
    item_type: int = 0

    @property
    def is_message(self) -> bool:
        """Is this row a message, and not a tapback or another row attached
        to one (``associated_message_type``: 2000 and up for reactions, small
        numbers for an app's own rows) or a group event (``item_type``: a
        member added, the group renamed)?  Messages offers "Edit" and "Undo
        Send" for none of those."""
        return self.associated_message_type == 0 and self.item_type == 0

    @property
    def retracted_parts(self) -> tuple[int, ...]:
        """Part indexes Messages recorded as unsent (``rp``); ``()`` when unknown."""
        return _int_tuple((self.summary or {}).get("rp"))

    @property
    def has_text(self) -> bool:
        """Is there any text left?  U+FFFC, the placeholder an attachment
        leaves in the text, does not count."""
        return bool((self.text or "").replace("\ufffc", "").strip())

    def is_retracted(self, part_index: int = 0) -> bool:
        """Has this part been unsent already?  When Messages listed the unsent
        parts (``rp``), that list decides.  Without one, a row that is marked
        as changed and has no text left counts as unsent."""
        listed = self.retracted_parts
        if listed:
            return part_index in listed
        return bool(self.date_edited or self.date_retracted) and not self.has_text

    def edit_count(self, part_index: int = 0) -> int | None:
        """How often this part has been edited, or ``None`` when the history
        cannot be read.  ``ec[<part>]`` is taken to list the ORIGINAL text
        first and one entry per edit after it (so five edits are six entries).
        That reading follows other readers of this database and was not
        checked against a message edited five times; if a history holds the
        edits alone, this counts one too few and Apple's own refusal of the
        sixth edit is what the caller sees."""
        if self.summary is None:
            return None
        history = self.summary.get("ec")
        if history is None:
            return 0
        if not isinstance(history, dict):
            return None
        events = history.get(str(part_index), history.get(part_index))
        if events is None:
            return 0
        if not isinstance(events, (list, tuple)):
            return None
        return max(len(events) - 1, 0)


def _summary_info(blob: object, column_present: bool) -> dict | None:
    if not column_present:
        return None
    if blob is None:
        return {}
    try:
        parsed = plistlib.loads(bytes(blob))        # type: ignore[arg-type]
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def change_target(chat_guid: str, guid: str) -> ChangeTarget | None:
    """The message ``guid`` IF it belongs to chat ``chat_guid``, else ``None``
    (an unknown guid and a guid of another chat are the same answer).  One
    connection, one statement, nothing written."""
    cdb = _cdb()
    opt = cdb.schema.select_or_null
    has_summary = cdb.schema.has("message", "message_summary_info")
    q = ("SELECT m.ROWID AS rowid, m.guid AS guid, m.text AS text, "
         "m.attributedBody AS attributed_body, m.date AS date, m.is_from_me AS is_from_me, "
         f"{opt('m', 'message', 'date_edited', 'date_edited')}, "
         f"{opt('m', 'message', 'date_retracted', 'date_retracted')}, "
         f"{opt('m', 'message', 'service', 'service')}, "
         f"{opt('m', 'message', 'message_summary_info', 'summary_info')}, "
         f"{opt('m', 'message', 'associated_message_type', 'associated_message_type')}, "
         f"{opt('m', 'message', 'item_type', 'item_type')}, "
         "c.guid AS chat_guid "
         "FROM message m "
         "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
         "JOIN chat c ON c.ROWID = cmj.chat_id "
         "WHERE m.guid = ? AND c.guid = ? LIMIT 1")
    with cdb.connection() as conn:
        rows = conn.execute(q, (guid, chat_guid)).fetchall()
    if not rows:
        return None
    r = rows[0]
    return ChangeTarget(
        rowid=int(r["rowid"]), guid=str(r["guid"]), chat_guid=str(r["chat_guid"]),
        is_from_me=bool(r["is_from_me"]),
        date=int(r["date"] or 0),
        date_edited=int(r["date_edited"] or 0),
        date_retracted=int(r["date_retracted"] or 0),
        text=effective_text(r["text"], r["attributed_body"]),
        service=None if r["service"] is None else str(r["service"]),
        summary=_summary_info(r["summary_info"], has_summary),
        associated_message_type=int(r["associated_message_type"] or 0),
        item_type=int(r["item_type"] or 0),
    )
