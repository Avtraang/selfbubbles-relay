"""
Google Messages (SMS/RCS) via the Beeper Desktop local API.

Beeper Desktop already bridges Google Messages and decrypts everything; it
exposes a local REST + WebSocket API on 127.0.0.1:23373. This module reads only
the gmessages account, normalises its chats/messages into the SAME dict shape
the iMessage path produces, and sends through Beeper. The Android app never
learns any of this exists — a Google Messages thread is just a thread whose
chat_guid starts with "bp:".

iMessage stays on the chat.db path (it's better and needs no Beeper). This is
purely additive.
"""
import asyncio
import html
import json
import os
import re
import sqlite3
import time
from urllib.parse import quote

import httpx

from placeholders import drop_placeholder

BEEPER_URL = os.environ.get("BEEPER_URL", "http://127.0.0.1:23373").rstrip("/")
# A shipped placeholder (CHANGE-ME, change-me..., see placeholders.py) is not a
# token: it counts as unset, so the bridge stays off instead of starting the
# watcher with a bearer token that cannot work.
BEEPER_TOKEN = drop_placeholder(os.environ.get("BEEPER_TOKEN", "").strip())
# Only this Beeper account is surfaced. iMessage is handled elsewhere.
GM_ACCOUNT = os.environ.get("BEEPER_GM_ACCOUNT", "sh-gmessages")

# Google Messages chat_guids carry this prefix so the relay can route sends and
# message fetches to Beeper instead of chat.db. The app treats it as opaque.
PREFIX = "bp:"

_enabled = bool(BEEPER_TOKEN)

# Messages only carry the Matrix chatID, not the localChatID the app-facing
# guid uses. Chats carry both, so we learn the mapping when listing threads and
# consult it when a live message event arrives.
_chatid_to_local: dict[str, str] = {}

# Which phone each Google Messages account lives on, for the chat header
# ("RCS · Google Messages · from Pixel (…0100)"). Configured by the
# BEEPER_GM_ACCOUNT_LABELS env var as comma-separated "email=Label" pairs, keyed
# by the Google account email inside Beeper's accountID
# ("sh-gmessages_<email>/<loginID>"). An email not listed just drops the
# " · from …" part.
def _parse_account_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for pair in raw.split(","):
        email, sep, label = pair.partition("=")
        if sep and email.strip() and label.strip():
            labels[email.strip().lower()] = label.strip()
    return labels


GM_ACCOUNT_LABELS = _parse_account_labels(os.environ.get("BEEPER_GM_ACCOUNT_LABELS", ""))

# The Beeper API doesn't say whether a chat is RCS or SMS; the mautrix-gmessages
# bridge DB does (portal.metadata "type": 2 = RCS, 1 = SMS/MMS). That DB belongs
# to the running bridge, so it's opened strictly read-only (file:…?mode=ro),
# briefly, off the event loop, and cached for PORTAL_TYPE_TTL seconds.
BRIDGE_DB = os.path.expanduser(os.environ.get(
    "BEEPER_BRIDGE_DB",
    "~/Library/Application Support/bbctl/prod/sh-gmessages/mautrix-gmessages.db"))
PORTAL_TYPE_TTL = 60
GM_TYPE_NAMES = {2: "RCS", 1: "SMS"}
_portal_types: dict[str, int] = {}   # Matrix room id (== Beeper chat "id") -> type
_portal_types_at = 0.0


def enabled() -> bool:
    return _enabled


def _headers():
    return {"Authorization": f"Bearer {BEEPER_TOKEN}"}


def is_beeper_guid(chat_guid: str) -> bool:
    return chat_guid.startswith(PREFIX)


def local_id(chat_guid: str) -> str:
    """bp:401 -> 401"""
    return chat_guid[len(PREFIX):]


#: Characters a Beeper chat id never contains and a URL path gives a meaning to.
_NOT_IN_A_CHAT_ID = frozenset("/\\?#%")


def chat_path_id(chat_guid: str) -> str | None:
    """The Beeper chat id of a ``bp:`` guid, checked for use as ONE segment of
    an API path, or ``None`` when it cannot be one.

    The guid comes from the client, and the path it goes into is requested
    with the Beeper token. A real id is a small number (``401``) or a Matrix
    room id (``!abc:beeper.local``) and is returned exactly as it is. An id
    that is empty or only dots, or that contains ``/``, ``\\``, ``?``, ``#``,
    ``%``, whitespace or a control character, is refused: unchecked,
    ``bp:../../v1/accounts#`` turned "this chat's messages" into a request to
    any other endpoint of Beeper Desktop's API (``httpx`` resolves dot
    segments before it sends)."""
    cid = local_id(chat_guid)
    if not cid.strip("."):
        return None
    if any(ch in _NOT_IN_A_CHAT_ID or ch.isspace() or not ch.isprintable() for ch in cid):
        return None
    return cid


#: Logged, without the id, when chat_path_id() refuses one.
REFUSED_CHAT_ID = "[beeper] refused a chat id that is not one path segment"

_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _error_code(r) -> str:
    """``" (<code>)"`` for a Beeper error answer whose JSON body carries a
    short machine-readable ``code``, else ``""``. Nothing else of the body is
    logged: it is not known whether Beeper repeats the message it was asked
    to send in an error answer, and a log file is no place to find out."""
    try:
        code = r.json().get("code")
    except (ValueError, AttributeError):
        return ""
    return f" ({code})" if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""


def _clean(text: str | None) -> str:
    # The API HTML-escapes message text (&amp;, &lt;, ...). Undo it.
    return html.unescape(text or "")


def _ts_to_unix(ts: str | None):
    """Beeper timestamps are ISO-8601 with a Z suffix. Returns None when absent
    so nullable fields (date_edited) stay null instead of 0.0 — otherwise the
    app treats a 0.0 edit time as 'this message was edited'."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


async def _get(client, path, **params):
    r = await client.get(f"{BEEPER_URL}{path}", headers=_headers(),
                         params=params or None, timeout=15)
    r.raise_for_status()
    return r.json()


# ---------- RCS/SMS + which-phone labels ----------

def _read_portal_types() -> dict[str, int]:
    """portal.mxid -> Google Messages conversation type, from the bridge DB.
    Read-only URI, 1 s busy timeout, one short query, connection always closed."""
    conn = sqlite3.connect("file:" + quote(BRIDGE_DB) + "?mode=ro", uri=True, timeout=1)
    try:
        out = {}
        for mxid, meta in conn.execute(
                "SELECT mxid, metadata FROM portal WHERE mxid IS NOT NULL"):
            try:
                t = json.loads(meta or "{}").get("type")
            except Exception:
                continue
            if type(t) is int:
                out[mxid] = t
        return out
    finally:
        conn.close()


async def refresh_portal_types() -> None:
    """Refresh the type cache at most every PORTAL_TYPE_TTL seconds, in a worker
    thread. A failure keeps the last good map (empty at first), so threads just
    degrade to service null / "Text" — never an error."""
    global _portal_types, _portal_types_at
    if time.time() - _portal_types_at < PORTAL_TYPE_TTL:
        return
    _portal_types_at = time.time()       # set first: concurrent callers skip
    try:
        _portal_types = await asyncio.to_thread(_read_portal_types)
    except Exception as e:
        print(f"[beeper] bridge DB type lookup failed: {e}")


def _account_email(account_id: str) -> str | None:
    """"sh-gmessages_<email>/<loginID>" -> "<email>"; None for any other shape
    (e.g. the old bridge's bare "sh-gmessages")."""
    if not account_id.startswith(GM_ACCOUNT + "_"):
        return None
    return account_id[len(GM_ACCOUNT) + 1:].split("/", 1)[0].strip().lower() or None


def gm_labels(c: dict) -> dict:
    """service / via_label / send_warning for a Beeper chat. Pure cache lookup —
    no I/O — so it's safe inside chat_to_thread."""
    try:
        service = GM_TYPE_NAMES.get(_portal_types.get(c.get("id") or ""))
    except Exception:
        service = None
    try:
        phone = GM_ACCOUNT_LABELS.get(_account_email(c.get("accountID") or "") or "")
    except Exception:
        phone = None
    via = f"{service or 'Text'} · Google Messages" + (f" · from {phone}" if phone else "")
    # send_warning is for Mac-side text threads only; Google Messages sends
    # from the phone that owns the conversation, so nothing to warn about.
    return {"service": service, "via_label": via, "send_warning": None}


# ---------- normalisation into the app's shared shapes ----------

#: Chat guid -> (title, is a group), as Beeper's chat listing last gave them.
#: For a group without a title of its own the title is its members' names.
_chat_meta: dict[str, tuple[str, bool]] = {}


def chat_meta(chat_guid: str) -> tuple[str, bool] | None:
    """What the chat listing said about this chat, or None if it has not shown it yet."""
    return _chat_meta.get(chat_guid)


def chat_to_thread(c: dict) -> dict:
    """Beeper chat -> the same thread dict fetch_threads() emits."""
    parts = (c.get("participants") or {}).get("items") or []
    others = [p for p in parts if not p.get("isSelf")]
    is_group = c.get("type") == "group" or len(others) > 1
    title = c.get("title") or (others[0].get("fullName") if others else "") or "Unknown"
    preview = c.get("preview") or {}
    local = str(c.get("localChatID") or c.get("id"))
    if c.get("id"):
        _chatid_to_local[c["id"]] = local
    names = [p.get("fullName") for p in others if p.get("fullName")]
    more = len(others) - len(names[:4])
    # A group is never titled with one member's name alone: that is how a
    # one-to-one chat with that member looks.
    members = (", ".join(names[:4]) + (f" +{more}" if more > 0 else "")) if (len(names) > 1 or (names and more > 0)) else ""
    if c.get("type") in ("group", "single") or is_group:
        _chat_meta[PREFIX + local] = (
            ((c.get("title") or "").strip() or members or "Group chat") if is_group else title, is_group)
    else:
        _chat_meta.pop(PREFIX + local, None)      # the kind is not known: say nothing rather than "not a group"
    last_date = _ts_to_unix(c.get("lastActivity")) or 0.0
    # Per-chat watermark for the live watcher: a message older than the chat's
    # last activity is history being replayed, not news.
    if last_date > _chat_last.get(PREFIX + local, 0.0):
        _chat_last[PREFIX + local] = last_date
    return {
        "chat_guid": PREFIX + local,
        "chat_name": title,
        "is_group": is_group,
        # Names only — Beeper gives display names, not phone numbers, so the
        # contact-photo pipeline can't match these. Empty handles => initials.
        "handles": [],
        "icon_url": None,
        "last_date": _ts_to_unix(c.get("lastActivity")) or 0.0,
        "last_rowid": 0,
        "pinned": False,
        "pin_index": -1,
        "archived": bool(c.get("isArchived")),
        "network": "gmessages",
        # The app's Thread.preview is a String?, so send a plain string. (iMessage
        # sends a dict here, which the app's lenient JSON coerces to null and
        # falls back to a timestamp — gmessages can do better and show the text.)
        "preview": (_clean(preview.get("text"))[:120] if preview.get("text") else None)
                   if preview else None,
        "unread": int(c.get("unreadCount") or 0),
        **gm_labels(c),
    }


def msg_to_dict(m: dict, chat_guid: str, is_group: bool) -> dict:
    """Beeper message -> the same dict row_to_msg() emits."""
    atts = []
    for a in (m.get("attachments") or []):
        src = a.get("srcURL") or ""
        atts.append({
            "guid": str(a.get("id") or a.get("uploadID") or ""),
            "mime_type": a.get("mimeType") or a.get("type") or "",
            "name": a.get("fileName") or "",
            # Beeper asset URLs (mxc://, localmxc://, file://) aren't directly
            # fetchable by the phone — the relay proxies them via /bp_asset.
            # str(QueryParams) percent-encodes the value, so the mxc URL's own
            # "?…=…" survives as one query parameter named `u` (what /bp_asset takes).
            "url": "/bp_asset?" + str(httpx.QueryParams({"u": src}))
                   if src else "",
        })
    linked = m.get("linkedMessageID")
    return {
        "rowid": int(str(m.get("sortKey") or m.get("id") or 0).replace("-", "") or 0)
        if str(m.get("sortKey") or "").lstrip("-").isdigit() else 0,
        "guid": str(m.get("id") or ""),
        "text": _clean(m.get("text")),
        "date": _ts_to_unix(m.get("timestamp")) or 0.0,
        "date_read": None,
        "date_edited": _ts_to_unix(m.get("editedTimestamp")),  # None unless truly edited
        "is_from_me": bool(m.get("isSender")),
        "sender": "" if m.get("isSender") else _clean(m.get("senderName")),
        "sender_handle": m.get("senderID") or "",
        "chat_guid": chat_guid,
        "chat_name": "",
        "is_group": is_group,
        "has_attachments": bool(atts),
        "assoc_guid": None,
        "assoc_type": 0,
        "attachments": atts,
        "link": None,
        "reply_to_guid": str(linked) if linked else None,
        "reply_to": None,
        "network": "gmessages",
    }


# ---------- reads ----------

async def fetch_threads(limit: int = 100) -> list[dict]:
    if not _enabled:
        return []
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            data = await _get(client, "/v1/chats", limit=limit)
        except Exception as e:
            print(f"[beeper] fetch_threads failed: {e}")
            return []
    await refresh_portal_types()   # cached; RCS/SMS for gm_labels()
    out = []
    for c in data.get("items", []):
        # Prefix match: bridgev2 namespaces accounts as "sh-gmessages_<loginID>",
        # and the loginID changes on every re-pairing. Old bridge used the bare name.
        if not (c.get("accountID") or "").startswith(GM_ACCOUNT):
            continue
        out.append(chat_to_thread(c))
    return out


async def fetch_messages(chat_guid: str, limit: int = 50,
                         cursor: str | None = None) -> list[dict]:
    if not _enabled:
        return []
    cid = chat_path_id(chat_guid)
    if cid is None:
        print(REFUSED_CHAT_ID)
        return []
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
        params["direction"] = "before"
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            data = await _get(client, f"/v1/chats/{cid}/messages", **params)
        except Exception as e:
            print(f"[beeper] fetch_messages({chat_guid}) failed: {e}")
            return []
    items = data.get("items", [])
    # Group-ness: infer from participants once, cheaply, via the chat object.
    is_group = False
    # Beeper returns newest-first; the app wants oldest-first like chat.db.
    msgs = [msg_to_dict(m, chat_guid, is_group) for m in items]
    msgs.reverse()
    return msgs


async def asset_url(src: str) -> tuple[bytes, str] | None:
    """Download a Beeper asset (mxc://…/file://…) to bytes for the phone."""
    if not _enabled or not src:
        return None
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            r = await client.get(
                f"{BEEPER_URL}/v1/assets/serve", headers=_headers(),
                params={"url": src}, timeout=60,
            )
            if r.status_code >= 400:
                return None
            return r.content, r.headers.get("content-type", "application/octet-stream")
        except Exception as e:
            print(f"[beeper] asset fetch failed: {e}")
            return None


# ---------- send ----------

async def send(chat_guid: str, text: str, reply_to: str | None = None) -> bool:
    if not _enabled:
        return False
    cid = chat_path_id(chat_guid)
    if cid is None:
        print(REFUSED_CHAT_ID)
        return False
    body = {"text": text}
    if reply_to:
        body["replyToMessageID"] = reply_to
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            r = await client.post(
                f"{BEEPER_URL}/v1/chats/{cid}/messages",
                headers=_headers(), json=body, timeout=30,
            )
            if r.status_code >= 400:
                # The status and Beeper's error code, never the body (see _error_code).
                print(f"[beeper] send failed HTTP {r.status_code}{_error_code(r)}")
                return False
            return True
        except Exception as e:
            print(f"[beeper] send error: {e}")
            return False


async def mark_read(chat_guid: str) -> bool:
    if not _enabled:
        return False
    cid = chat_path_id(chat_guid)
    if cid is None:
        return False
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            r = await client.post(f"{BEEPER_URL}/v1/chats/{cid}/read",
                                  headers=_headers(), timeout=15)
            return r.status_code < 400
        except Exception:
            return False


# ---------- live: WebSocket subscription ----------

async def _resolve_local(chat_id: str) -> str:
    """Matrix chatID -> localChatID. Uses the cache; if the chat is new since the
    last thread list, refresh threads once to learn it."""
    if chat_id in _chatid_to_local:
        return _chatid_to_local[chat_id]
    await fetch_threads(200)   # repopulates the map
    return _chatid_to_local.get(chat_id, chat_id)


# Beeper message IDs already handed to the relay. "message.updated" events (read
# receipts, delivery status) and replays after a reconnect re-send messages the
# phone was already notified about — those must become updates, never a second push.
_seen_msg_ids: dict = {}
# chat_guid -> unix time of the chat's newest message as last fetched. An upserted
# entry dated before this is backfilled history (Beeper sends several entries per
# event and replays on reconnect) — the 2026-10-02 "notified for every past message
# in the thread" bug. Strictly newer only (2026-10-03): a replayed copy of the
# chat's newest message is dated exactly at the watermark.
_chat_last: dict = {}
# A message in a chat with no seeded watermark counts as live only if it is
# dated within this many seconds of now (delayed deliveries still qualify;
# replayed history, dated days or months back, does not).
DORMANT_GRACE = 15 * 60
# A message in a seeded chat must also be strictly newer than the chat's last
# activity and no older than this to count as live.
LIVE_MAX_AGE = 6 * 3600


def _first_time(msg_id: str) -> bool:
    """True the first time a Beeper message id is seen; bounded memory."""
    if msg_id in _seen_msg_ids:
        return False
    _seen_msg_ids[msg_id] = None
    if len(_seen_msg_ids) > 4000:
        for old in list(_seen_msg_ids)[:1000]:
            _seen_msg_ids.pop(old, None)
    return True


async def watch(on_message):
    """Subscribe to Beeper's event WebSocket and hand each gmessages message to
    on_message(dict, is_new) in the app's shared shape. is_new is True only for a
    "message.upserted" event whose id has not been seen before; "message.updated"
    events (e.g. the read receipt after the app marks a chat read) and replays
    arrive with is_new=False so the relay broadcasts an update and does NOT push.
    Reconnects on drop."""
    if not _enabled:
        return
    import websockets

    ws_url = BEEPER_URL.replace("http://", "ws://").replace("https://", "wss://") + "/v1/ws"
    while True:
        try:
            async with websockets.connect(
                ws_url, additional_headers=_headers(), ping_interval=20,
            ) as ws:
                # REQUIRED: without a subscriptions.set command Beeper streams
                # nothing. "*" = every chat. This was the whole reason live SMS
                # never arrived until a REST refetch was forced.
                await ws.send(json.dumps({
                    "type": "subscriptions.set",
                    "requestID": "imsg-relay",
                    "chatIDs": ["*"],
                }))
                print("[beeper] websocket connected + subscribed")
                # Seed the per-chat watermarks so anything Beeper replays on this
                # (re)connect is recognised as history.
                try:
                    await fetch_threads(200)
                except Exception as e:
                    print(f"[beeper] watermark seed failed: {e}")
                async for raw in ws:
                    try:
                        evt = json.loads(raw)
                    except Exception:
                        continue
                    etype = evt.get("type")
                    if etype not in ("message.upserted", "message.updated"):
                        continue
                    # Events carry the messages in an "entries" array, each a
                    # full message object (id, senderName, text, chatID, ...).
                    entries = [m for m in evt.get("entries", [])
                               if isinstance(m, dict) and (m.get("accountID") or "").startswith(GM_ACCOUNT)]
                    pushed = 0
                    for msg in entries:
                        chat_id = msg.get("chatID") or evt.get("chatID") or ""
                        if not chat_id or not msg.get("id"):
                            continue
                        local = await _resolve_local(chat_id)
                        guid = PREFIX + str(local)
                        d = msg_to_dict(msg, guid, False)
                        wm = _chat_last.get(guid)
                        when = d.get("date") or 0.0
                        now = time.time()
                        if wm is None:
                            # Dormant chat outside the seeded window (the seed
                            # covers only the newest chats): Beeper replays and
                            # backfills its old messages too, and with no
                            # watermark every one of them looked live — the
                            # 2026-10-02 "notifications for months-old chats"
                            # bug. A live message is dated about now; history
                            # is not.
                            fresh = when >= now - DORMANT_GRACE
                        else:
                            # Strictly newer than the chat's last activity: a
                            # replay of the chat's NEWEST message is dated
                            # exactly at the watermark, and "equal counts as
                            # new" pushed 4-day- and 9-month-old messages on
                            # 2026-10-03. Plus an age cap: a message that only
                            # surfaces hours after it was sent is shown in the
                            # app but not announced as fresh.
                            fresh = when > wm and when >= now - LIVE_MAX_AGE
                        is_new = (etype == "message.upserted"
                                  and _first_time(str(msg["id"]))
                                  and fresh)
                        if is_new:
                            pushed += 1
                            if when > (wm or 0.0):
                                _chat_last[guid] = when
                            print(f"[beeper] {time.strftime('%H:%M:%S')} new message in {guid} "
                                  f"(dormant={wm is None}, age={now - when:.0f}s, "
                                  f"wm={wm}, date={when})")
                        await on_message(d, is_new)
                    if etype == "message.upserted" and (len(entries) != 1 or pushed != 1):
                        # Evidence line: how many entries an event carried vs how many were news.
                        print(f"[beeper] {etype}: {len(entries)} entries, {pushed} new")
        except Exception as e:
            print(f"[beeper] websocket error: {e} — reconnecting in 5s")
            await asyncio.sleep(5)
