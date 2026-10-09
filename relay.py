#!/usr/bin/env python3
"""
imsg-relay — a self-hosted iMessage relay that runs on the Mac signed into Messages.

Receive: polls ~/Library/Messages/chat.db by monotonic ROWID (immune to the
  timestamp/backfill freeze that hit the mautrix bridge).
Send: an ordered chain of engines (engines/): BlueBubbles Private API,
  Messages.app over AppleScript, Beeper Desktop for Google Messages. Outbound
  attachments arrive as multipart on /send_attachment (python-multipart).
Change: /unsend (BlueBubbles) and /edit (imessage-cli) for the owner's own
  recent iMessages; each change is confirmed in chat.db before it is answered.
Names: resolved from BlueBubbles' Contacts endpoint (falls back to raw handle).
Attachments: served from disk at /attachment/{guid}.
Transport: WebSocket broadcast, plus FCM data pushes when FCM_CREDS is set.

Configuration is environment variables (an optional .env beside this file is
loaded when python-dotenv is installed; real environment wins). The full list
with defaults is in .env.example; `relay.py --check` prints a status table
(never a secret) for every dependency and exits without starting the server.
Started as a program, the relay refuses to run without a real IMSG_TOKEN
(exit status 78) unless IMSG_ALLOW_NO_TOKEN=1 on a loopback bind; see
startup_refusal().
"""

import asyncio
import collections
import contextlib
import difflib
import hashlib
import hmac
import ipaddress
import json
import logging
import threading
import os
import plistlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import weakref
from pathlib import Path
from urllib.parse import quote, unquote

# Optional .env beside this file, for running without launchd (see .env.example).
# override=False: anything already in the environment (the LaunchAgent plist's
# EnvironmentVariables) always wins, so this is a no-op for the launchd install.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:  # python-dotenv not installed: fine, plain env vars still work
    pass

import httpx
try:
    import firebase_admin
    from firebase_admin import credentials as fb_credentials, messaging as fb_messaging
except ImportError:
    firebase_admin = None
from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from placeholders import drop_placeholder, is_placeholder

# Where things live. RELAY_DIR is the checkout (code and the caches beside it);
# RELAY_DATA_DIR is where the FaceTime auto-admit rig writes its log and
# working files. It defaults to RELAY_DIR, which is exactly where everything
# was before this knob existed, so an install that never sets it is unchanged.
RELAY_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.path.expanduser(os.environ.get("RELAY_DATA_DIR", "").strip()
                                   or str(RELAY_DIR)))

PORT = int(os.environ.get("IMSG_PORT", "8700"))
# The address the HTTP server binds. Unset means 0.0.0.0, every IPv4 interface,
# which is what the relay always did and what an HTTPS route that reaches it
# over the LAN needs. .env.example and the launchd example ship 127.0.0.1: with
# Tailscale Serve or a Cloudflare Tunnel on this same Mac nothing else has to
# reach the port. The doctor's "listening on" row shows what is in force.
BIND = os.environ.get("IMSG_BIND", "").strip() or "0.0.0.0"


def bind_kind(host: str) -> str:
    """How wide an ``IMSG_BIND`` value opens the port: ``"all"`` (an
    unspecified address: every interface), ``"loopback"``, ``"address"`` (one
    other numeric address) or ``"name"`` (not a numeric address at all).

    The value is read the way the socket layer will read it, with no lookup
    (``AI_NUMERICHOST``), so the other spellings of ``0.0.0.0`` that also bind
    every interface (``0``, ``0x0``, ``::0``) are ``"all"`` too. ``localhost``
    is the one name counted as loopback. Any other name (and a value with a
    port or brackets in it, which is not an address) is only resolved when
    the server starts; the doctor flags it instead of guessing."""
    if host.strip().lower() == "localhost":
        return "loopback"
    try:
        infos = socket.getaddrinfo(host, PORT, type=socket.SOCK_STREAM,
                                   flags=socket.AI_PASSIVE | socket.AI_NUMERICHOST)
        addresses = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]
    except (OSError, ValueError):      # socket.gaierror is an OSError, UnicodeError a ValueError
        return "name"
    if not addresses:
        return "name"
    if any(a.is_unspecified for a in addresses):
        return "all"
    return "loopback" if all(a.is_loopback for a in addresses) else "address"


# Names of the credential keys whose value is still a shipped placeholder
# ("change-me...", "CHANGE-ME", "your-...", "replace-with..."; placeholders.py).
# A placeholder is public, so it is never used: BB_PASSWORD, BEEPER_TOKEN,
# HA_TOKEN and MAPKIT_TOKEN then count as unset wherever they gate behaviour,
# and IMSG_TOKEN is handled under "Auth" below. The doctor reports each one.
PLACEHOLDER_KEYS = frozenset(
    key for key in ("IMSG_TOKEN", "BB_PASSWORD", "BEEPER_TOKEN", "HA_TOKEN", "MAPKIT_TOKEN")
    if is_placeholder(os.environ.get(key)))

# Your own outbound identities that this Mac sees as *foreign* senders —
# numbers registered to iMessage elsewhere (e.g. OpenBubbles on the phone).
# Excluded from chat-membership matching. Comma-separated in the IMSG_SELF env
# var (LaunchAgent plist or .env); empty means nothing is excluded.
SELF_RAW = [a.strip() for a in
            os.environ.get("IMSG_SELF", "").split(",") if a.strip()]
if SELF_RAW:
    print(f"[self] excluding {len(SELF_RAW)} own identit{'y' if len(SELF_RAW) == 1 else 'ies'} from chat matching")
CHATDB = os.path.expanduser(os.environ.get("IMSG_CHATDB", "~/Library/Messages/chat.db"))
STATE_PATH = Path(os.environ.get("IMSG_STATE", str(Path(__file__).parent / "relay_state.json")))
POLL_SECONDS = float(os.environ.get("IMSG_POLL_SECONDS", "2"))
BB_URL = os.environ.get("BB_URL", "http://localhost:1234").rstrip("/")
BB_PASSWORD = drop_placeholder(os.environ.get("BB_PASSWORD", ""))
FCM_CREDS = os.environ.get("FCM_CREDS", "")

APPLE_EPOCH_OFFSET = 978307200

# Auth. Mandatory once the relay is reachable from the internet (Cloudflare
# tunnel) — without it, anyone who finds the hostname can read every message and
# send as you. Sent as the X-Imsg-Token header, or ?token= for the WebSocket and
# image URLs that Coil fetches.
#
# A shipped placeholder is never accepted as the token. Started as a program,
# the relay refuses to run without a real token unless IMSG_ALLOW_NO_TOKEN=1
# AND the bind address is loopback (startup_refusal(), called under __main__
# only: an import never exits). Imported with a placeholder still in place and
# no such flag, the API is locked instead: nothing can authenticate, so every
# route but /health answers 401. With the flag, a placeholder is the same as
# no token: no authentication.
ALLOW_NO_TOKEN = os.environ.get("IMSG_ALLOW_NO_TOKEN", "").strip() == "1"
IMSG_TOKEN = drop_placeholder(os.environ.get("IMSG_TOKEN", "").strip())
AUTH_LOCKED = "IMSG_TOKEN" in PLACEHOLDER_KEYS and not ALLOW_NO_TOKEN

#: sysexits.h EX_CONFIG: what `python relay.py` exits with when it refuses to start.
EX_CONFIG = 78


def startup_refusal() -> str | None:
    """Why ``python relay.py`` must not start the server, or ``None`` when it
    may. A relay without a real token answers every request from anyone who
    can reach the port, so that has to be asked for (``IMSG_ALLOW_NO_TOKEN=1``),
    not arrived at by copying an example file, and it is only granted when
    the port is bound to loopback: the built-in bind address is every
    interface, where "no authentication" would mean the whole LAN. Only
    ``__main__`` calls this: importing the module (the tests, ``uvicorn
    relay:app``) never exits."""
    if IMSG_TOKEN:
        return None
    if ALLOW_NO_TOKEN:
        if bind_kind(BIND) == "loopback":
            return None
        return ("[auth] refusing to start: IMSG_ALLOW_NO_TOKEN=1 needs a loopback bind, and IMSG_BIND "
                "is not one (unset, it is 0.0.0.0: every interface).\n"
                "[auth] Without a token every request is answered, so that mode is for a first test "
                "on this Mac only: set IMSG_BIND=127.0.0.1 and start again.\n"
                "[auth] Or set IMSG_TOKEN to a long random string (openssl rand -hex 32) and remove "
                "IMSG_ALLOW_NO_TOKEN. `relay.py --check` shows the same finding without starting.")
    what = ("IMSG_TOKEN is still the placeholder from the example file"
            if "IMSG_TOKEN" in PLACEHOLDER_KEYS else "IMSG_TOKEN is not set")
    return (f"[auth] refusing to start: {what}.\n"
            "[auth] Set IMSG_TOKEN to a long random string (openssl rand -hex 32) in .env or in the "
            "LaunchAgent's EnvironmentVariables (a key in the plist beats .env), then start again.\n"
            "[auth] To run without authentication on purpose (a first test on this Mac only), set "
            "IMSG_ALLOW_NO_TOKEN=1 together with IMSG_BIND=127.0.0.1. "
            "`relay.py --check` shows the same finding without starting.")


def auth_status_line() -> str:
    """The one ``[auth]`` line printed when the module loads (stdout, so
    ``relay.log`` under launchd). It states the configuration in words that
    stay true whatever happens next: the module is served, ``__main__``
    refuses to start (the reasons then go to stderr), or ``--check`` only
    prints the table. In particular, a start that is about to be refused does
    not leave "running without authentication" in the log."""
    if IMSG_TOKEN:
        return "[auth] token required"
    if AUTH_LOCKED:
        return "[auth] IMSG_TOKEN IS A PLACEHOLDER — every request is refused until a real token is set"
    if startup_refusal() is None:
        return ("[auth] NO TOKEN SET — IMSG_ALLOW_NO_TOKEN is 1: every request is answered WITHOUT "
                "authentication (a test on this Mac only)")
    return ("[auth] NO TOKEN SET — relay.py does not start like this; imported into another server "
            "(uvicorn relay:app) it answers every request without authentication")


print(auth_status_line())


# ---------- keeping the token out of the logs ----------
# ?token= is still ACCEPTED everywhere the middleware and the WebSocket gate
# look (BlueBubbles registers its webhook URL with it and cannot set headers;
# the map page and older clients use it too). What changes is what gets
# written down: uvicorn's access log records the full request line, so a
# filter on its loggers and handlers rewrites every token= value to *** before
# the line reaches relay.log / relay.err, and MaskingStream does the same for
# anything print()ed once the server is running under __main__.
#
# Two more shapes go the same way (step R5):
# * password=<value>. The BlueBubbles password travels as that query parameter
#   (engines/bluebubbles.py). No line of the relay prints it, but three lines
#   quote the start of an upstream error body, and a server that echoes the
#   request URL there would otherwise put the password in the log.
# * the whole query string of the voice routes. They read the dictated
#   sentence from the query when an automation app calls them that way
#   (read_fields), and the access line would record the message text.

_TOKEN_PARAM = re.compile(r"(?i)(\b[\w-]*(?:token|password)=)[^&#\s\"'<>]+")
_VOICE_QUERY = re.compile(r"(/(?:v|assistant)/(?:prepare|confirm)/?)\?\S*")


def mask_token(text):
    """``text`` with the value of every ``token=`` / ``*_token=`` /
    ``password=`` query parameter replaced by ``***``, and the query string of
    a voice route (``/v/prepare?...``, ``/v/confirm?...``, ``/assistant/...``)
    replaced whole by ``***``. Non-strings come back unchanged."""
    if not isinstance(text, str):
        return text
    low = text.lower()
    if "token=" in low or "password=" in low:
        text = _TOKEN_PARAM.sub(r"\1***", text)
    if "/prepare" in text or "/confirm" in text:
        text = _VOICE_QUERY.sub(r"\1?***", text)
    return text


class TokenMaskFilter(logging.Filter):
    """Applies ``mask_token`` to a log record's message AND its args (uvicorn's
    access line is '%s - "%s %s HTTP/%s" %d' with the path+query as an arg)."""

    def filter(self, record):
        # The arguments first, one by one: uvicorn's AccessFormatter reads them
        # itself (client, method, path + query, version, status), so they have
        # to be clean and they have to stay.
        args = record.args
        if isinstance(args, dict):
            record.args = {k: mask_token(v) for k, v in args.items()}
        elif isinstance(args, tuple):
            record.args = tuple(mask_token(a) for a in args)
        # Then the message as it will read. The format string is not masked
        # as text: that would eat a placeholder ("token=%s" -> "token=***"),
        # the record would no longer format, and logging's own error report
        # prints the raw arguments. If the formatted text still has something
        # to mask (a value whose "token=" sits in the format), the record is
        # replaced by the masked text, with no arguments left to print.
        try:
            text = record.getMessage()
        except Exception:
            return True                  # a record that does not format is logging's to report
        masked = mask_token(text)
        if masked != text:
            record.msg, record.args = masked, ()
        return True


class MaskingStream:
    """A text stream wrapper that puts everything written through ``mask_token``.
    Installed over sys.stdout / sys.stderr by __main__ so a print() that
    echoes a URL (and uvicorn's handlers, which bind the streams at startup)
    can never land the token in a log file. Everything else is delegated.

    Line-buffered by default: a write that contains a newline is flushed
    straight away. Under launchd stdout is a file, which Python block-buffers,
    so without this the doctor table and every later print() sat in the buffer
    until uvicorn's access logger happened to flush the stream on the first
    request."""

    def __init__(self, raw, line_buffered=True):
        self._raw = raw
        self._line_buffered = line_buffered

    def write(self, s):
        n = self._raw.write(mask_token(s))
        if self._line_buffered and isinstance(s, str) and "\n" in s:
            self.flush()
        return n

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        flush = getattr(self._raw, "flush", None)
        if flush is not None:
            flush()

    def __getattr__(self, name):
        return getattr(self._raw, name)


_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def install_log_masking():
    """Attach TokenMaskFilter to uvicorn's loggers (idempotent). Logger-level
    filters cover records emitted on those loggers whatever handlers end up
    attached; masked_log_config() adds the same filter at the handler level."""
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenMaskFilter) for f in logger.filters):
            logger.addFilter(TokenMaskFilter())


def masked_log_config() -> dict:
    """uvicorn's default logging config with TokenMaskFilter on every handler."""
    import copy
    from uvicorn.config import LOGGING_CONFIG
    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["mask_token"] = {"()": TokenMaskFilter}
    for handler in cfg["handlers"].values():
        filters = handler.setdefault("filters", [])
        if "mask_token" not in filters:
            filters.append("mask_token")
    return cfg


install_log_masking()

import beeper  # Google Messages via the Beeper Desktop local API
from engines import (PROTOCOL, Capability, DeliveryError, EngineError, build_chain,
                     deliver, derive_features, engine_names, first_with,
                     imessage_capabilities, no_engine)
from engines.applescript import (  # kept under their old names: tests and tools patch them here
    _AS_FILE, _AS_TEXT, OUTBOX, AppleScriptEngine, _applescript, _guid_variants)
from engines.bluebubbles import BlueBubblesEngine
import engines.imessage_cli as imessage_cli_engine  # accessibility_trusted is looked up there at call time
from engines.base import SendResult
from engines.chain import IMESSAGE_PROBE_GUID, MAYBE_SENT
from engines.imessage_cli import CHECKED_VERSION as IMESSAGE_CLI_CHECKED
from engines.imessage_cli import OFF_VALUES as IMESSAGE_CLI_OFF
from engines.imessage_cli import ImessageCliEngine
from engines.imessage_cli import find_binary as find_imessage_cli
from engines.imessage_cli import installed_version as imessage_cli_version
from engines.imessage_cli import tool_ran as imessage_cli_ran

# Send-engine chain (engines/chain.py): beeper (bp: guids) if BEEPER_TOKEN,
# bluebubbles if BB_PASSWORD, applescript unless SEND_APPLESCRIPT_FALLBACK=0;
# SEND_ENGINES=name,name overrides the order. Built from these values on every
# send (cheap: a few small objects), so no BB password simply means the chain
# is [applescript] and texts/files still go out — the old "BB_PASSWORD not
# set" 500s are gone by construction.
SEND_ENGINES = os.environ.get("SEND_ENGINES", "").strip()
SEND_APPLESCRIPT_FALLBACK = os.environ.get("SEND_APPLESCRIPT_FALLBACK", "1").strip()
# The edit engine (engines/imessage_cli.py): Beeper's imessage-cli, the one way
# found to edit a sent message on macOS 27. IMESSAGE_CLI names the binary, and
# "0" or "off" switches the engine off; unset, Homebrew's two locations and the
# PATH are searched (launchd's PATH has neither, hence the absolute paths). The
# binary is looked for once, here, and never run before an /edit asks for it.
# It joins the chain after the engines above, whether or not SEND_ENGINES
# names it (that list orders the engines that send), and can only edit.
IMESSAGE_CLI = os.environ.get("IMESSAGE_CLI", "").strip()
IMESSAGE_CLI_BIN = find_imessage_cli(IMESSAGE_CLI)
# What the app may show (UI only; every router stays mounted). Derived from the
# raw environment: facetime = BB_PASSWORD set (not FT_AUTOADMIT), map = MAPKIT_TOKEN
# + HA_TOKEN, translate = OLLAMA_MODEL or MARIAN_URL, voice = facetime;
# FEATURE_FACETIME/MAP/TRANSLATE/VOICE override.
FEATURES = derive_features(os.environ)

# Group-chat photos proxied from BlueBubbles, cached beside the relay (git-ignored).
ICON_CACHE = RELAY_DIR / "icons"
# Most groups have no photo. Once we learn that, threads stop advertising an
# icon_url for them, so the app stops requesting it — one round of misses after
# a restart, then silence. Expires hourly so a newly-set photo still appears.
NO_ICON: dict = {}
NO_ICON_TTL = 3600

# chat_guid -> human title, learned from thread listings. Push notifications use
# this instead of raw identifiers ("chat14332...") that unnamed groups carry.
CHAT_TITLES: dict = {}

# Mac-side SMS/RCS sends are handed by macOS to its text relay — e.g. OpenBubbles
# on an Android phone — so they leave from that phone's number (or show "Not
# Delivered"). TEXT_RELAY_LABEL names that phone in the chat header and the
# composer warning; set it in the LaunchAgent plist or .env (see .env.example).
TEXT_RELAY_LABEL = os.environ.get("TEXT_RELAY_LABEL", "").strip() or "SMS relay phone"
TEXT_SEND_WARNING = (f"Replies here go out as text messages through your "
                     f"{TEXT_RELAY_LABEL}, not iMessage, and may show as Not Delivered.")
CONTACT_REFRESH_SECONDS = 6 * 3600
THUMB_DIR = Path(__file__).parent / "thumb_cache"
THUMB_DIR.mkdir(exist_ok=True)

# iPhone HEIC (grid-tiled, ftyp heic/MiHE) won't decode on the Android side —
# Coil/BitmapFactory reject it. The Mac converts it in ~100ms, so /attachment
# serves HEIC as JPEG (cached here) and the metadata advertises image/jpeg.
HEIC_CACHE = Path(__file__).parent / "heic_cache"
HEIC_CACHE.mkdir(exist_ok=True)

# iPhone voice messages are Core Audio Format files ("Audio Message.caf",
# mime NULL, Opus inside) that Android cannot play. afconvert turns one into
# AAC-in-M4A in ~0.1 s, so /attachment serves CAF as M4A (cached here) and the
# metadata advertises audio/mp4 — same shape as the HEIC rule above.
AUDIO_CACHE = Path(__file__).parent / "audio_cache"
AUDIO_CACHE.mkdir(exist_ok=True)


def _is_heic(mime, name):
    return ((mime or "").lower() in ("image/heic", "image/heif")
            or (name or "").lower().endswith((".heic", ".heif")))


def _is_caf(mime, name):
    return ((mime or "").lower() in ("audio/x-caf", "audio/caf")
            or (name or "").lower().endswith(".caf"))


def _caf_m4a_name(name):
    """The .m4a name for a CAF: the stem kept, ".caf" (any case) swapped for
    ".m4a"; a name with no .caf suffix gets ".m4a" appended. None stays None."""
    if not name:
        return name
    return re.sub(r"\.caf$", "", name, flags=re.I) + ".m4a"


def att_meta(mime, name):
    """Client-facing mime/name for an attachment: HEIC is advertised as the
    JPEG that /attachment actually serves, so saves, notifications and
    viewers all see consistent metadata."""
    if _is_heic(mime, name):
        jpg = re.sub(r"\.hei[cf]$", ".jpg", name, flags=re.I) if name else name
        return "image/jpeg", jpg
    if _is_caf(mime, name):
        return "audio/mp4", _caf_m4a_name(name)
    return mime, name


# Guids whose HEIC->JPEG transcode failed: stop advertising JPEG for them so
# the metadata always matches the bytes actually served. In-memory only —
# repopulates on the next fetch after a restart, which also retries the
# transcode.
FAILED_HEIC: set = set()
# Same for CAF->M4A: a guid here is advertised (and served) as the original.
FAILED_CAF: set = set()


def att_public(guid, mime, name):
    """Client-facing (mime, name, url) for an attachment. Transcoded HEIC gets
    a ?f=jpg cache-buster: phones disk-cached the undecodable HEIC bytes under
    the bare URL, and would keep replaying them without a fresh key."""
    if _is_heic(mime, name) and guid not in FAILED_HEIC:
        m, n = att_meta(mime, name)
        return m, n, f"/attachment/{guid}?f=jpg"
    if _is_caf(mime, name) and guid not in FAILED_CAF:
        m, n = att_meta(mime, name)
        return m, n, f"/attachment/{guid}?f=m4a"
    return mime, name, f"/attachment/{guid}"


# ---------- chat.db ----------
# Phase 1 (imessage-chatdb DESIGN.md §7): the chat.db code that used to live
# here is chatdb_adapter.py, built on the imessage-chatdb library. Same names,
# same signatures, same JSON. It is configured once person_key exists (below).

import chatdb_adapter
import link_enrich
from chatdb_adapter import (CursorAhead, LINK_BALLOON, _norm_service,
                            apple_date_to_unix, attachment_by_guid, chat_attachments,
                            chat_services, db, embedded_images, extract_urls,
                            fetch_edited, fetch_new, fetch_thread_messages,
                            find_chat_for_addresses, last_rowid_for, lite_rows,
                            max_date_edited, max_rowid, one_to_one_activity,
                            parse_attributed_body, participants, participants_map,
                            recent_messages, search_rows, unread_count)
from chatdb_adapter import chats as chat_summaries
from chatdb_adapter import payload as link_payload
from chatdb_adapter import snippet as search_snippet


# ---------- contacts ----------

CONTACTS: dict[str, str] = {}
#: Address key -> the smallest key of the contact CARD it came from. A person is a card, not a
#: display name: two cards that share a name are two people.
CONTACT_CANON: dict[str, str] = {}
#: Address key -> the card's own address in sendable form (its country code kept).
CONTACT_ADDRS: dict[str, str] = {}
#: Address key -> the number of the contact card that names it (after the same
#: person's cards on two accounts were folded into one).
CONTACT_CARD: dict[str, int] = {}
#: Address keys that sit on more than one card (a couple's landline).
CONTACT_SHARED: set[str] = set()
#: Names (as they sound) carried by more than one contact card, counted from
#: the cards themselves: also a card none of whose numbers can be sent to.
CARD_NAMESAKES: set[str] = set()
#: Names (as they sound) of cards that have no address the relay can send to.
CARD_UNSENDABLE: set[str] = set()

#: Country calling code assumed for a phone number written without one.
DEFAULT_COUNTRY_CODE = "".join(
    ch for ch in os.environ.get("IMSG_DEFAULT_COUNTRY_CODE", "") if ch.isdigit()) or "1"


class BadAddress(ValueError):
    """A recipient that is neither an e-mail address nor a usable phone number."""


def normalize_phone(s: str) -> str:
    s = s.split("X-SHARED")[0]  # strip BB's "+1...X-SHARED-PHOTO-DISPLAY-PREF" junk
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def norm_key(addr):
    """Comparison key of an address: an e-mail address lower-cased; a phone
    number as the digits of its full international form ("15550000001",
    "972525551234"), read by the rules of normalize_address, so two numbers
    are equal only when they are the same number; a short code as its digits.
    Whatever those rules refuse is equal only to itself."""
    if not addr:
        return None
    try:
        sendable = normalize_address(addr.split("X-SHARED")[0])
    except BadAddress:
        return "raw:" + " ".join(addr.lower().split())
    return sendable if "@" in sendable else sendable.lstrip("+")


#: What is dropped from the front of a national number before the country code
#: goes on: "0" unless the country is listed here.
_TRUNK_PREFIX = {"39": "", "7": "8"}
#: How many digits a national number has (trunk prefix dropped) where that is
#: known; elsewhere five to twelve. A number of another length is refused.
_NATIONAL_DIGITS = {"7": {10}, "33": {9}, "39": set(range(6, 12)), "44": {9, 10}, "49": set(range(6, 12)),
                    "52": {10}, "61": {9}, "81": {9, 10}, "91": {10}, "972": {8, 9}}
#: Default countries that dial something other than 00 to call abroad.
_NO_00_PREFIX = {"7", "55", "57", "61", "62", "65", "66", "81", "82", "234", "254", "852", "886"}


def normalize_address(addr: str) -> str:
    """User-typed recipient -> sendable address, or BadAddress.

    An e-mail address is lower-cased. A phone number that carries its own
    country code keeps it: written with "+" (also behind "tel:", brackets or
    invisible characters), or with the international prefix (00, or 011 where
    the default country is 1). A number without one is read as a national
    number of IMSG_DEFAULT_COUNTRY_CODE (default 1): for 1, ten digits with an
    area code that can exist, or eleven starting with 1; elsewhere the digits
    with the trunk prefix dropped. Three to six digits are a short code.

    Everything else is refused, never guessed at, because a guess is another
    person's number: letters (a name, a vanity number, an extension), a
    separator that starts an extension or a service code, a second "+", a
    length or an area code the default country does not have."""
    s = unicodedata.normalize("NFKC", addr or "").strip()
    if any(unicodedata.category(ch) == "Cc" for ch in s):
        raise BadAddress("more than one line")       # a line break or a tab inside: two values, not one
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf").strip()
    s = re.sub(r"^(tel|sms|mailto)\s*:\s*(//)?", "", s, flags=re.I).strip()
    if "@" in s:
        if not re.fullmatch(r"[^@\s<>?&,;]+@[^@\s<>?&,;]+\.[^@\s<>?&,;]+", s):
            raise BadAddress("not an e-mail address")
        return s.lower()
    if any(ch.isalpha() for ch in s):
        raise BadAddress("not a phone number or an e-mail address")
    if any(ch in s for ch in ";,#*"):
        raise BadAddress("a number with an extension or a service code")
    s = "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() else ch for ch in s)
    if "+" in s and not "".join(ch for ch in s if ch.isdecimal()).startswith("39"):
        s = re.sub(r"\(\s*0\s*\)", " ", s)        # "+44 (0)7700 ...": the bracketed trunk zero is not dialled
                                                    # (in Italy, +39, the zero is part of the number and stays)
    digits = "".join(ch for ch in s if ch.isdecimal())
    if not digits:
        raise BadAddress("not a phone number or an e-mail address")
    first = next(i for i, ch in enumerate(s) if ch.isdecimal())
    if s.count("+") > 1 or ("+" in s and "+" not in s[:first]):
        raise BadAddress("not a phone number")
    if "+" in s:
        return _international(digits)
    if 3 <= len(digits) <= 6 and digits[0] != "0":
        return digits
    hint = "write the number with + and its country code"
    if digits.startswith("00"):
        # 00 is the international prefix in most countries, not in all: where it
        # is not, what follows is not a country code and nothing is guessed.
        if digits.startswith("000") or DEFAULT_COUNTRY_CODE in _NO_00_PREFIX:
            raise BadAddress(hint)
        return _international(digits[2:])
    if DEFAULT_COUNTRY_CODE == "1":
        if digits.startswith("011"):
            return _international(digits[3:])
        if len(digits) == 11 and digits[0] == "1":
            digits = digits[1:]
        if len(digits) == 10 and digits[0] in "23456789":
            return "+1" + digits
        raise BadAddress(hint)
    if digits.startswith(DEFAULT_COUNTRY_CODE):
        # "44 7700 900123" with a default of 44: a national number, or one written
        # with its country code and no "+"? Not for the relay to decide.
        raise BadAddress(hint)
    trunk = _TRUNK_PREFIX.get(DEFAULT_COUNTRY_CODE, "0")
    national = digits
    if trunk and digits.startswith(trunk) and (trunk == "0" or len(digits) == 11):
        national = digits[len(trunk):]
    if len(national) not in _NATIONAL_DIGITS.get(DEFAULT_COUNTRY_CODE, range(5, 13)) or (trunk == "0" and national[0] == "0"):
        raise BadAddress(hint)
    return _international(DEFAULT_COUNTRY_CODE + national)


def _international(digits: str) -> str:
    """"+" and the digits of a number that carries its country code, or
    BadAddress: no country code starts with 0, no number has fewer than seven
    or more than fifteen digits, and a North American one has exactly eleven
    with an area code that does not start with 0 or 1."""
    if not 7 <= len(digits) <= 15 or digits[0] == "0":
        raise BadAddress("not a complete international number")
    if digits[0] == "1" and (len(digits) != 11 or digits[1] in "01"):
        raise BadAddress("not a complete North American number")
    return "+" + digits


def contact_address(key: str) -> str | None:
    """Contact-map key -> the address to offer and to send to: the card's own,
    or None. Never the key and never a rebuilt number."""
    return CONTACT_ADDRS.get(key)


def load_contacts():
    bb = first_with(_chain(), Capability.CONTACTS)
    if bb is None:
        print("[contacts] BB_PASSWORD not set in the relay's env — "
              "names won't resolve; sends go out over AppleScript. Set BB_PASSWORD and restart the relay.")
        return True          # nothing to retry
    try:
        data = bb.contacts()
    except EngineError as e:
        print(f"[contacts] {e.detail}")
        return False         # not loaded: the caller tries again soon
    m, sendable, last_card = {}, {}, {}
    people: dict[str, list[set]] = {}        # a name as it sounds -> its cards, each a set of address keys
    unread = 0
    for c in data:
        name = (c.get("displayName")
                or " ".join(x for x in [c.get("firstName"), c.get("lastName")] if x).strip()
                or c.get("nickname")
                or c.get("company")
                or "")
        if not name:
            continue
        keys = set()
        for group in ("phoneNumbers", "emails"):
            for item in (c.get(group) or []):
                addr = item.get("address") if isinstance(item, dict) else item
                if not isinstance(addr, str):
                    continue
                try:
                    send = normalize_address(addr.split("X-SHARED")[0])
                except BadAddress:
                    unread += 1
                    continue        # not an address anyone can be sent to: not offered, and it names nobody
                k = send if "@" in send else send.lstrip("+")
                m[k] = name
                sendable[k] = send
                keys.add(k)
        # Every named card counts as a card of that name, also one with nothing
        # the relay can send to. Two cards are one only when they list exactly
        # the same addresses (the same card on two accounts); nothing else is
        # ever folded together, whatever the names or the order they arrive in.
        cards_of_name = people.setdefault(name_key(name), [])
        target = next((other for other in cards_of_name if keys and other == keys), None)
        if target is None:
            target = set(keys)
            cards_of_name.append(target)
        for k in keys:
            last_card[k] = target
    if unread:
        print(f"[contacts] {unread} number(s) on contact cards are not usable phone numbers "
              "(an extension, a service code, a number of another country without +): not offered, and they name no chat")
    CARD_NAMESAKES.clear()
    CARD_NAMESAKES.update(nk for nk, sets in people.items() if len(sets) > 1)
    CARD_UNSENDABLE.clear()
    CARD_UNSENDABLE.update(nk for nk, sets in people.items()
                           if not any(name_key(m[k]) == nk for ks in sets for k in ks))
    cards = [ks for sets in people.values() for ks in sets]
    index = {id(ks): i for i, ks in enumerate(cards)}
    on: dict[str, int] = {}
    for ks in cards:
        for k in ks:
            on[k] = on.get(k, 0) + 1
    canon = {}
    self_keys = {norm_key(a) for a in SELF_RAW}
    for ks in cards:
        own = sorted(k for k in ks if on[k] == 1 and k not in self_keys)
        for k in ks:
            # An address on more than one card is nobody's in particular: it is its
            # own person, and never the key another address is known by. Nor is one
            # of the owner's own addresses (IMSG_SELF): whoever shares a card with
            # it is not the owner.
            canon[k] = own[0] if own and on[k] == 1 and k not in self_keys else k
    set_contacts(m, canon, sendable, {k: index[id(ks)] for k, ks in last_card.items()},
                 {k for k, n in on.items() if n > 1})
    return True
    print(f"[contacts] {len(data)} contacts from BlueBubbles -> {len(m)} phone/email keys")


def resolve(handle):
    if not handle:
        return handle
    return CONTACTS.get(norm_key(handle)) or handle


# ---------- message assembly ----------

TAPBACK_VERBS = ["Loved", "Liked", "Disliked", "Laughed at", "Emphasized", "Questioned"]


def last_message_previews(conn, rowids):
    """iMessage-style one-liners for the newest message of each thread."""
    if not rowids:
        return {}
    out = {}
    for rowid, lm in lite_rows(conn, rowids).items():
        text = lm.text
        t = lm.associated_type or 0
        if 2000 <= t <= 2005:
            body = f"{TAPBACK_VERBS[t - 2000]} a message"
        elif 3000 <= t <= 3005:
            body = "Removed a reaction"
        elif text:
            body = text
        elif lm.attachments:
            a = lm.attachments[0]
            # att_public first, then branch on what the client is told: a
            # NULL-mime .heic is advertised as image/jpeg, so it is a Photo.
            mime, name, _url = att_public(a.guid, a.mime_type, a.transfer_name)
            mime = mime or ""
            body = ("\U0001F4F7 Photo" if mime.startswith("image/")
                    else "\U0001F3A5 Video" if mime.startswith("video/")
                    else "\U0001F3A4 Voice message" if mime.startswith("audio/")
                    else "\U0001F4CE " + (name or "Attachment"))
        elif lm.has_attachments:
            body = "\U0001F4CE Attachment"
        else:
            body = ""
        out[rowid] = {"body": body, "is_from_me": lm.is_from_me,
                      "sender": lm.sender_handle}
    return out


def group_title(conn, chat_rowid, display_name):
    if display_name:
        return display_name
    names = []
    for hid in participants(conn, chat_rowid):
        n = resolve(hid)
        names.append(n.split()[0] if n and " " in n else n)
    if not names:
        return None
    return ", ".join(names[:4]) + ("…" if len(names) > 4 else "")


def set_contacts(names: dict, canon: dict | None = None, sendable: dict | None = None,
                 card: dict | None = None, shared: set | None = None) -> None:
    """Replace the contact map and its companions together. The person map
    goes first and comes back last: while the others are being replaced an
    address is only ever equal to itself, never to somebody else's."""
    CONTACT_CANON.clear()
    CONTACTS.clear()
    CONTACTS.update(names)
    CONTACT_ADDRS.clear()
    CONTACT_ADDRS.update(sendable or {})
    CONTACT_CARD.clear()
    CONTACT_CARD.update(card or {})
    CONTACT_SHARED.clear()
    CONTACT_SHARED.update(shared or ())
    CONTACT_CANON.update(canon or {})


def person_key(addr):
    """Collapse a handle/address to a person: the contact card it is on when
    known, else its normalized key. The phone and the e-mail on ONE card
    compare equal; two cards never do, whatever names they carry. An address
    with no key at all (no digits, no "@") is only ever equal to itself."""
    k = norm_key(addr)
    if not k:
        return "raw:" + (addr or "").strip().lower()
    return (CONTACT_CANON.get(k) or k) if k in CONTACTS else k


chatdb_adapter.configure(chatdb_path=CHATDB, resolve=resolve, att_public=att_public,
                         person_key=person_key, group_title=group_title, self_raw=SELF_RAW,
    readonly_uri=True,  # DESIGN §7.5 step 5: mode=ro URI (honours WAL, never creates -wal/-shm)
)


def icon_known_missing(guid: str) -> bool:
    """True if we've already learned this group has no photo (within the TTL).
    Threads then omit icon_url entirely, so the app stops asking."""
    seen = NO_ICON.get(guid)
    return bool(seen and time.time() - seen < NO_ICON_TTL)


def mac_thread_labels(service: str | None) -> dict:
    """service / via_label / send_warning for a Mac-side (chat.db) thread."""
    if service == "iMessage":
        return {"service": "iMessage", "via_label": "iMessage", "send_warning": None}
    if service in ("RCS", "SMS"):
        return {"service": service,
                "via_label": f"{service} · sent through {TEXT_RELAY_LABEL}",
                "send_warning": TEXT_SEND_WARNING}
    return {"service": None, "via_label": None, "send_warning": None}


def fetch_threads(limit=200):
    conn = db()
    try:
        # Chats with messages, newest activity first (the relay's THREADS
        # statement, run by the library): rowid, guid, display_name,
        # chat_identifier, style, last_date, last_rowid.
        rows = chat_summaries(conn, limit)

        previews = last_message_previews(conn, [c.last_rowid for c in rows])

        # Participant addresses per chat — the app matches these against the
        # phone's own contacts to pull photos (the Mac's address book has none).
        parts = participants_map(conn)

        # Read state lives relay-side (like pins): chat.db's is_read never gets
        # set in this stack, so the app reports a per-chat high-water ROWID and
        # everything past it counts as unread. Baseline = "now" on first run,
        # so history starts clean.
        st = load_state()
        reads = st.get("reads", {})
        baseline = st.get("reads_baseline")
        if baseline is None:
            baseline = conn.execute(
                "SELECT MAX(ROWID) AS m FROM message").fetchone()["m"] or 0
            save_state(reads_baseline=baseline)
            print(f"[reads] baseline initialized at ROWID {baseline} — "
                  "older history counts as read")

        services = chat_services(conn, [c.rowid for c in rows])
        out = []
        for c in rows:
            is_group = (c.style == 43)
            title = group_title(conn, c.rowid, c.display_name) if is_group \
                else resolve(c.chat_identifier)

            p = previews.get(c.last_rowid)
            preview = None
            if p and p["body"]:
                if is_group:
                    who = "You" if p["is_from_me"] else \
                        ((resolve(p["sender"]) or "").split(" ")[0] if p["sender"] else "")
                    preview = f"{who}: {p['body']}" if who else p["body"]
                else:
                    preview = p["body"]

            mark = int(reads.get(c.guid, baseline))
            unread = 0
            if c.guid in FORCED_UNREAD:
                unread = 1                      # manually marked unread
            elif c.last_rowid > mark:
                # incoming rows past the high-water mark (the relay's UNREAD SQL)
                unread = unread_count(conn, c.rowid, mark)

            handles = ([c.chat_identifier] if not is_group
                       else [h for h in parts.get(c.rowid, []) if h])
            icon_url = None
            if is_group and not icon_known_missing(c.guid):
                icon_url = f"/chat_icon/{quote(c.guid, safe='')}"
            out.append({
                "chat_guid": c.guid, "chat_name": title, "is_group": is_group,
                "handles": handles, "icon_url": icon_url,
                "last_date": apple_date_to_unix(c.last_date), "last_rowid": c.last_rowid,
                "pinned": c.guid in PINS,
                "pin_index": PINS.index(c.guid) if c.guid in PINS else -1,
                "archived": c.guid in ARCHIVED,
                "auto_translate": c.guid in AUTO_TRANSLATE,
                "preview": preview, "unread": unread,
                **mac_thread_labels(services.get(c.rowid)),
            })
        return out
    finally:
        conn.close()


# ---------- state ----------

def _read_state() -> tuple[dict, bool]:
    """(state, damaged): damaged when a state file exists and neither it nor
    its backup can be read as a state."""
    seen = False
    for path in (STATE_PATH, STATE_PATH.with_suffix(".bak")):
        try:
            text = _read_state_file(path)
        except FileNotFoundError:
            continue
        except UnicodeDecodeError:
            seen = True
            continue                       # not text at all: damage, try the backup
        seen = True
        try:
            data = json.loads(text)
        except ValueError:
            # Unparseable: read it once more before calling it damage (a read
            # that came back short), then try the backup.
            try:
                data = json.loads(_read_state_file(path))
            except (ValueError, FileNotFoundError):
                continue
        if isinstance(data, dict):
            if path != STATE_PATH:
                _note_backup_in_use()
            return data, False
    return {}, seen


_BACKUP_NOTED = False


def _note_backup_in_use() -> None:
    global _BACKUP_NOTED
    if not _BACKUP_NOTED:
        _BACKUP_NOTED = True
        print(f"[state] {STATE_PATH.name} is missing or not a state: reading {STATE_PATH.with_suffix('.bak').name}")


def _read_state_file(path: Path) -> str:
    """The file's text. A file that is there and cannot be read right now is
    not a damaged file and not a missing one: the read is tried once more, and
    then the error goes to the caller. Nobody may take it for an empty state
    and write over a file that is intact."""
    try:
        return path.read_text()
    except (FileNotFoundError, UnicodeDecodeError):
        raise
    except OSError:
        time.sleep(0.05)
        return path.read_text()


def load_state() -> dict:
    return _read_state()[0]


_state_lock = threading.Lock()


def save_state(**updates):
    # Serialized + atomic: a partial/concurrent write can't corrupt the file
    # and wipe pins/archives (which is exactly what happened before).
    with _state_lock:
        st, damaged = _read_state()
        bak = STATE_PATH.with_suffix(".bak")
        if damaged:
            # Neither file is a state. Writing now would replace both with an
            # almost empty one in silence: keep what is there under another
            # name and say so, once.
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for path in (STATE_PATH, bak):
                with contextlib.suppress(Exception):
                    if path.exists():
                        path.replace(path.with_name(f"{path.name}.damaged-{stamp}"))
            print(f"[state] {STATE_PATH.name} and its backup were unreadable: kept as *.damaged-{stamp}, "
                  "starting from an empty state (pins, read marks and push registrations start over)")
        st.update(updates)
        blob = json.dumps(st)
        tmp = STATE_PATH.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()               # a save that failed leaves nothing behind
            raise
        if STATE_PATH.exists():
            # The backup is a copy: the live file is never absent, not even
            # between two system calls. It is best effort.
            copy = bak.with_suffix(".bak.tmp")
            try:
                shutil.copyfile(STATE_PATH, copy)
                if not isinstance(json.loads(copy.read_text()), dict):
                    raise ValueError("the live file is not a state: the backup stays as it is")
                copy.replace(bak)
            except Exception:
                with contextlib.suppress(OSError):
                    copy.unlink()
        try:
            tmp.replace(STATE_PATH)   # atomic on POSIX
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise


def load_cursor():
    try:
        return int(load_state().get("last_rowid", 0))
    except (TypeError, ValueError):
        return 0          # a state without a usable cursor; a state that cannot be READ is an error, not a zero


def save_cursor(rowid):
    save_state(last_rowid=rowid)


# Ordered: list position is the display order of the pinned row.
PINS: list = list(load_state().get("pins", []))
# Which groups have no photo, learned once and remembered across restarts.
NO_ICON.update({k: float(v) for k, v in (load_state().get("no_icon") or {}).items()})
# Archived chats: hidden from the main list, kept in the Archive view. Relay-side
# like pins, so any future client inherits it. Never touches chat.db.
ARCHIVED: set = set(load_state().get("archived", []))
# Threads the user wants translated automatically on open.
AUTO_TRANSLATE: set = set(load_state().get("auto_translate", []))
# Chats the user manually marked unread; cleared when they open the chat.
FORCED_UNREAD: set = set(load_state().get("forced_unread", []))

# ---------- push notifications (FCM) ----------

FCM_READY = False
#: Firebase's own limit for one attempt. The library's default is two minutes;
#: nothing here is worth waiting that long for, and the next notification
#: waits behind this one. The library tries once more after a timeout, so a
#: Firebase that does not answer costs twice this per registered phone.
PUSH_TIMEOUT_SECONDS = 10


def init_fcm():
    global FCM_READY
    if not FCM_CREDS:
        print("[fcm] FCM_CREDS not set — push notifications disabled")
        return
    if firebase_admin is None:
        print("[fcm] firebase-admin not installed — run: pip install firebase-admin")
        return
    try:
        cred = fb_credentials.Certificate(os.path.expanduser(FCM_CREDS))
        firebase_admin.initialize_app(cred, options={"httpTimeout": PUSH_TIMEOUT_SECONDS})
        FCM_READY = True
        print("[fcm] initialized — push enabled")
    except Exception as e:
        print(f"[fcm] init failed: {e}")


def push_tokens() -> set:
    return set(load_state().get("push_tokens", []))


def save_tokens(tokens: set):
    save_state(push_tokens=sorted(tokens))


def attachment_label(atts):
    """A human label for an attachment-only message, shown in the notification."""
    if not atts:
        return ""
    if len(atts) > 1:
        return f"\U0001F4CE {len(atts)} attachments"
    mime = (atts[0].get("mime_type") or "").lower()
    name = atts[0].get("name") or ""
    if mime == "image/gif" or name.lower().endswith(".gif"):
        return "\U0001F39E GIF"
    if mime.startswith("image/"):
        return "\U0001F4F7 Photo"
    if mime.startswith("video/"):
        return "\U0001F3A5 Video"
    if mime.startswith("audio/"):
        return "\U0001F3A4 Audio message"
    if "pdf" in mime or name.lower().endswith(".pdf"):
        return "\U0001F4C4 PDF"
    if name:
        return f"\U0001F4CE {name}"
    return "\U0001F4CE Attachment"


#: The registrations as last read for a push, for a moment in which the state file cannot be read.
_TOKENS_SEEN: set = set()


def push_group_flag(msg: dict) -> str | None:
    """"1" / "0" for the push's is_group, so the app does not have to guess a
    group from its title (a group whose title is not cached yet must not look
    like a one-to-one chat). For a Google Messages row the answer comes from
    Beeper's chat listing; None only for a chat that listing has not shown
    yet, and the app then judges by the title as before."""
    guid = str(msg.get("chat_guid") or "")
    if guid.startswith("bp:"):
        meta = beeper.chat_meta(guid)
        # A chat Beeper's listing has not shown is not known to be one-to-one,
        # so it is not presented as one: a reply typed into the notification
        # may reach more people than the sender.
        return "1" if meta is None or meta[1] else "0"
    return "1" if msg.get("is_group") else "0"


def send_push(msg: dict):
    if not FCM_READY:
        return
    global _TOKENS_SEEN
    try:
        tokens = push_tokens()
        _TOKENS_SEEN, fresh = set(tokens), True
    except OSError as e:
        # The state file cannot be read right now. A push must not be lost over
        # that, nor may the error stop the loop that called: the registrations
        # read last are used, and nothing is written back.
        print(f"[fcm] state file unreadable ({type(e).__name__}) — pushing to the registrations read last")
        tokens, fresh = set(_TOKENS_SEEN), False
    if not tokens:
        return
    # Archived chats are silenced: skip the push entirely (don't even wake the
    # phone). The WS broadcast still goes out, so an open app stays live, and
    # unarchiving restores pushes immediately — membership is checked per
    # message. Same guid test /threads uses, so iMessage and bp: both match.
    chat_guid = str(msg.get("chat_guid") or "")
    if chat_guid in ARCHIVED:
        print(f"[fcm] skip push for archived chat {chat_guid}")
        return
    # Evidence line (no content, no handles): which side pushed and to how many devices.
    print(f"[fcm] {time.strftime('%H:%M:%S')} push: {'gm' if chat_guid.startswith('bp:') else 'imsg'} "
          f"chat -> {len(tokens)} device(s)")
    # Attachment-only messages carry U+FFFC ("object replacement") as their
    # text — strip it or the notification shows a literal question mark.
    raw_text = (msg.get("text") or "").replace("\ufffc", "").replace("\ufffd", "").strip()
    data = {
        "chat_guid": str(msg.get("chat_guid") or ""),
        "chat_name": str(msg.get("chat_name") or ""),
        "sender": str(msg.get("sender") or ""),
        "text": (raw_text or attachment_label(msg.get("attachments"))
                 or ("\U0001F4CE Attachment" if msg.get("has_attachments") else ""))[:300],
        "rowid": str(msg.get("rowid")),
        # Stable id for the app's own dedup (gmessages rows all have rowid 0).
        "guid": str(msg.get("guid") or ""),
    }
    group = push_group_flag(msg)
    if group is not None:
        data["is_group"] = group
    # First image rides along so the notification can show the actual picture.
    img = next((a for a in (msg.get("attachments") or [])
                if (a.get("mime_type") or "").startswith("image/") and a.get("url")), None)
    if img:
        data["image_url"] = img["url"]
        data["image_mime"] = img.get("mime_type") or "image/jpeg"
    # Prefer the human title the thread list computes (member names for unnamed
    # groups); blank a raw "chatNNN" identifier so the app falls back to sender.
    better = CHAT_TITLES.get(data["chat_guid"])
    if data["chat_guid"].startswith("bp:"):
        # A Google Messages group is titled from Beeper's own chat listing
        # (its name, or its members), whether or not a client has fetched the
        # thread list; a chat the listing has not shown is "Group chat".
        meta = beeper.chat_meta(data["chat_guid"])
        if meta is None:
            better = "Group chat"
        elif meta[1]:
            better = meta[0]
    if better:
        data["chat_name"] = better[:200]
    elif data["chat_name"].startswith("chat") and data["chat_name"][4:].isdigit():
        data["chat_name"] = ""
    dead = []
    for t in tokens:
        try:
            fb_messaging.send(fb_messaging.Message(
                token=t, data=data,
                android=fb_messaging.AndroidConfig(priority="high"),
            ))
        except fb_messaging.UnregisteredError:
            dead.append(t)
        except Exception as e:
            print(f"[fcm] send error: {e}")
    if dead and fresh:
        with contextlib.suppress(OSError):
            save_tokens(tokens - set(dead))
            print(f"[fcm] pruned {len(dead)} dead token(s)")


class PushLane:
    """Hands work to Firebase one piece at a time and in order, without the
    caller waiting for it. Receiving a message must not wait for its
    notification: a push that hangs used to hold up every message behind it,
    the frames for an open app included."""

    def __init__(self, limit: int):
        self.limit = limit
        self.waiting: collections.deque = collections.deque()
        self.worker = None

    def put(self, fn, *args) -> None:
        while len(self.waiting) >= self.limit:
            self.waiting.popleft()
            print("[fcm] too many notifications waiting — the oldest gave way")
        self.waiting.append((fn, args))
        loop = asyncio.get_running_loop()
        if self.worker is None or self.worker.done() or self.worker.get_loop() is not loop:
            self.worker = loop.create_task(self._run())

    async def _run(self) -> None:
        while self.waiting:
            fn, args = self.waiting.popleft()
            try:
                await asyncio.to_thread(fn, *args)
            except Exception as e:
                print(f"[fcm] push failed ({type(e).__name__})")

    def idle(self) -> bool:
        return not self.waiting and (self.worker is None or self.worker.done())


PUSH_QUEUE_MAX = 200
MESSAGE_PUSHES = PushLane(PUSH_QUEUE_MAX)
#: A FaceTime ring does not wait behind message notifications.
CALL_PUSHES = PushLane(20)


def queue_push(msg: dict) -> None:
    """The notification for a received message: queued, sent behind the caller's back."""
    MESSAGE_PUSHES.put(send_push, msg)


# ---------- ws hub ----------

#: How long one connected app may take to accept a frame. A phone that went
#: out of reach without closing its connection takes nothing; waiting for it
#: without a limit stopped the loop that reads new messages.
HUB_SEND_SECONDS = 5.0


class Hub:
    def __init__(self):
        self.clients = set()
        self._closing = set()

    async def connect(self, ws):
        await ws.accept(); self.clients.add(ws)

    def drop(self, ws):
        self.clients.discard(ws)

    async def broadcast(self, payload):
        clients = list(self.clients)
        if clients:
            # All at once: one client that is slow does not delay the others.
            await asyncio.gather(*(self._send(ws, payload) for ws in clients))

    async def _send(self, ws, payload):
        try:
            await asyncio.wait_for(ws.send_json(payload), HUB_SEND_SECONDS)
        except Exception:               # gone, or took longer than the limit
            self.drop(ws)
            # Closed, not only forgotten: an app that is merely slow must
            # notice, reconnect and reload, instead of sitting on a connection
            # nothing is sent to any more. Behind the caller's back, since
            # closing a connection that takes nothing can hang as well.
            task = asyncio.get_running_loop().create_task(self._close(ws))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def _close(self, ws):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(code=1011), HUB_SEND_SECONDS)


hub = Hub()
app = FastAPI(title="imsg-relay")


def token_matches(supplied) -> bool:
    """Constant-time check of a caller-supplied token against IMSG_TOKEN.
    Compares bytes: hmac.compare_digest(str, str) raises TypeError on any
    non-ASCII character, which turned a bad token into a 500 instead of a
    rejection. None / "" / no configured token never match."""
    if not (IMSG_TOKEN and supplied):
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), IMSG_TOKEN.encode("utf-8"))


@app.middleware("http")
async def require_token(request: Request, call_next):
    """Rejects unauthenticated HTTP. /health stays open so a tunnel can be
    probed without leaking anything."""
    if (IMSG_TOKEN or AUTH_LOCKED) and request.url.path != "/health":
        supplied = (request.headers.get("x-imsg-token")
                    or request.query_params.get("token"))
        if not token_matches(supplied):      # never matches while AUTH_LOCKED: there is no token
            return Response(status_code=401, content="unauthorized")
    return await call_next(request)


# ---------- Apple News link previews ----------
# An apple.news link arrives with a title but almost never an image. link_enrich
# resolves it to the publisher article (Open Graph tags, SSRF-guarded fetches),
# caches the outcome under link_cache/ and overlays it on the message's link
# card. The overlay is cache-only; the network work runs in the background and
# the card upgrades through a later "update" event. APPLE_NEWS_PREVIEWS=0 turns
# the whole feature off.

LINK_ENRICHER = link_enrich.Enricher(
    Path(__file__).parent / "link_cache",
    enabled=os.environ.get("APPLE_NEWS_PREVIEWS", "1").strip() != "0")
LINK_MAX_RESOLVES = 2
_LINK_WAITERS: dict[str, set] = {}   # apple.news URL (queued or in flight) -> waiting rowids
_LINK_QUEUE: list[str] = []          # URLs not yet picked up by a worker
_LINK_TASKS: set = set()             # running workers (strong refs), at most LINK_MAX_RESOLVES


def enrich_links(msgs) -> list:
    """Overlays cached Apple News previews on the message dicts in place and
    returns [(apple.news URL, rowid)] for the links that still need resolving.
    Messages without an apple.news link are not touched."""
    need = []
    for msg in msgs:
        try:
            url = LINK_ENRICHER.apply(msg)
        except Exception as e:
            print(f"[links] {time.strftime('%H:%M:%S')} overlay failed ({type(e).__name__})")
            continue
        if url:
            need.append((url, msg.get("rowid")))
    return need


def schedule_link_resolves(need) -> None:
    """Queues background resolves for enrich_links() output. Event loop only.
    One resolve per URL however often it is asked for while queued or in
    flight; at most LINK_MAX_RESOLVES run at a time."""
    for url, rowid in need:
        if url not in _LINK_WAITERS:
            _LINK_WAITERS[url] = set()
            _LINK_QUEUE.append(url)
        if isinstance(rowid, int):
            _LINK_WAITERS[url].add(rowid)
    _link_pump()


def _link_pump() -> None:
    while _LINK_QUEUE and len(_LINK_TASKS) < LINK_MAX_RESOLVES:
        task = asyncio.create_task(_link_worker(_LINK_QUEUE.pop(0)))
        _LINK_TASKS.add(task)
        task.add_done_callback(_link_worker_done)


def _link_worker_done(task) -> None:
    # A finished worker still counts against LINK_MAX_RESOLVES until this
    # callback runs; a URL queued in that window found no free slot and no
    # worker looking at the queue, so start one for it now.
    _LINK_TASKS.discard(task)
    _link_pump()


async def _link_worker(url: str):
    while True:
        try:
            await _resolve_link(url)
        except Exception as e:
            print(f"[links] {time.strftime('%H:%M:%S')} apple.news unresolved (error: {type(e).__name__})")
        if not _LINK_QUEUE:
            return
        url = _LINK_QUEUE.pop(0)


async def _resolve_link(url: str):
    """Resolves one URL; on success re-reads every waiting message, overlays it
    and broadcasts it as an update. Logs hosts and reasons only -- never the
    URL path, never message text."""
    entry = None
    try:
        entry = await LINK_ENRICHER.resolve(url)
    finally:
        rowids = sorted(_LINK_WAITERS.pop(url, ()))
    stamp = time.strftime("%H:%M:%S")
    if not entry:
        print(f"[links] {stamp} apple.news unresolved ({LINK_ENRICHER.reason(url) or 'unknown'})")
        return
    host = link_enrich.urlsplit(entry["resolved_url"]).hostname or "?"
    print(f"[links] {stamp} apple.news -> {host} (image={'yes' if entry.get('image') else 'no'})")
    for rowid in rowids:
        try:
            msg = await asyncio.to_thread(chatdb_adapter.fetch_message, rowid)
            if msg is None:
                continue
            LINK_ENRICHER.apply(msg)
            await hub.broadcast({"type": "update", "data": msg})
        except Exception as e:
            print(f"[links] {time.strftime('%H:%M:%S')} update failed ({type(e).__name__})")


@app.get("/link_preview_image/{key}")
def link_preview_image(key: str):
    """Serves an image link_enrich cached for an Apple News card (the key is a
    hash; anything else is refused before the filesystem is touched)."""
    path = LINK_ENRICHER.image_path(key)
    if path is None:
        raise HTTPException(404, "unknown image")
    # nosniff: the bytes came from a third-party site; the type is fixed by the
    # magic-byte check at download time and must not be second-guessed by a client.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable",
                                       "X-Content-Type-Options": "nosniff"})


async def poll_loop():
    cursor = load_cursor()
    if cursor == 0:
        cursor = max_rowid(); save_cursor(cursor)
        print(f"[poll] initialized cursor at ROWID {cursor}")
    edit_mark = load_state().get("last_edit", 0) or 0
    if edit_mark == 0:
        edit_mark = await asyncio.to_thread(max_date_edited)
        save_state(last_edit=edit_mark)
        print(f"[poll] initialized edit mark at {edit_mark}")
    next_contacts = 0.0
    while True:
        # Contacts first and on their own: whatever goes wrong here, the
        # database is still read below. A load that failed is tried again
        # soon, not in six hours.
        if time.time() >= next_contacts:
            try:
                loaded = await asyncio.to_thread(load_contacts)
            except Exception as e:
                loaded = False
                print(f"[contacts] refresh failed: {type(e).__name__}")
            next_contacts = time.time() + (CONTACT_RETRY_SECONDS if loaded is False
                                           else CONTACT_REFRESH_SECONDS)
        try:
            new = await asyncio.to_thread(fetch_new, cursor)
            if new:
                link_need = await asyncio.to_thread(enrich_links, new)
                passed = set()
                try:
                    for msg in new:
                        if msg["text"] is None and not msg["attachments"]:
                            print(f"[poll] ROWID {msg['rowid']} no text/att "
                                  f"(assoc_type={msg['assoc_type']}) — hardening candidate")
                        await hub.broadcast({"type": "message", "data": msg})
                        if (not msg["is_from_me"]
                                and (msg["assoc_type"] or 0) < 2000
                                and (msg["text"] or msg["attachments"])):
                            queue_push(msg)
                        # The position moves with every row that was passed on:
                        # whatever fails further down this round, this row is
                        # not broadcast and notified a second time.
                        passed.add(msg["rowid"])
                        cursor = msg["rowid"]
                        await asyncio.to_thread(save_cursor, cursor)
                finally:
                    # only now: an "update" must never overtake its own "message"
                    schedule_link_resolves([n for n in link_need if n[1] in passed])
            edited, new_mark = await asyncio.to_thread(fetch_edited, edit_mark)
            if edited:
                link_need = await asyncio.to_thread(enrich_links, edited)
                for msg in edited:
                    print(f"[poll] edit detected on ROWID {msg['rowid']}")
                    await hub.broadcast({"type": "update", "data": msg})
                schedule_link_resolves(link_need)
                edit_mark = new_mark
                save_state(last_edit=edit_mark)
        except CursorAhead as e:
            # chat.db was rebuilt (MAX(ROWID) fell below the cursor): restart
            # from "now" instead of waiting for ROWIDs that may never arrive.
            # e.max_rowid is the MAX(ROWID) fetch_new just read, so no second
            # query runs here (one that raised -- the DB is busy exactly while
            # Messages rebuilds it -- would escape this clause and end the loop).
            cursor = e.max_rowid
            try:
                save_cursor(cursor)
            except Exception as se:
                print(f"[poll] error saving re-initialized cursor: {se}")
            print(f"[poll] {e} — re-initialized cursor at ROWID {cursor}")
        except Exception as e:
            print(f"[poll] error: {e}")
        await asyncio.sleep(POLL_SECONDS)


#: Background loops, held so they are not garbage-collected.
_BACKGROUND: set = set()
LOOP_RESTART_SECONDS = 5.0
CONTACT_RETRY_SECONDS = 300.0


async def _supervised(name: str, start) -> None:
    """Run a background loop for the life of the process. If it ever ends or
    raises (a log line that cannot be written is enough), say so when that is
    possible and start it again after a pause. Without this, one escaped
    exception ends receiving for good while the relay goes on answering."""
    while True:
        try:
            await start()
            why = "ended"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            why = f"stopped ({type(e).__name__})"
        with contextlib.suppress(Exception):
            print(f"[{name}] {why} — starting it again in {LOOP_RESTART_SECONDS:g} s")
        await asyncio.sleep(LOOP_RESTART_SECONDS)


def _spawn(name: str, start) -> None:
    task = asyncio.create_task(_supervised(name, start))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _tighten_files() -> None:
    """What the relay writes from here on is for its own account only, and
    what it wrote before is brought in line: the state holds push
    registrations, the caches hold attachments."""
    os.umask(0o077)
    for path, mode in ((STATE_PATH, 0o600), (STATE_PATH.with_suffix(".bak"), 0o600), (SEND_IDS.path, 0o600),
                       (HEIC_CACHE, 0o700), (AUDIO_CACHE, 0o700), (ICON_CACHE, 0o700)):
        with contextlib.suppress(Exception):
            if path.exists():
                os.chmod(path, mode)


async def _on_beeper_message(msg: dict, is_new: bool = True):
    """A live Google Messages message: broadcast to the app and push, mirroring
    exactly what the chat.db poll loop does for iMessage. A message the bridge
    re-sends (read-receipt "message.updated" after the app marks a chat read, or
    a replay after a reconnect) is broadcast as an update and never pushed again —
    that was the "notification twice when I open the chat" bug (2026-10-02)."""
    if not is_new:
        await hub.broadcast({"type": "update", "data": msg})
        return
    await hub.broadcast({"type": "message", "data": msg})
    if not msg.get("is_from_me") and (msg.get("text") or msg.get("attachments")):
        queue_push(msg)


#: The newest Google Messages time the watcher has accounted for: as it last
#: reported it, as last written to the state file, and the write in flight.
_BEEPER_MARK = {"want": 0.0, "saved": 0.0, "task": None}


def _beeper_since() -> float | None:
    """The mark a previous run saved. With it the watcher announces what
    arrived while the relay was not running; without one (a first start, or a
    state file that cannot be read just now) it takes everything Beeper
    already holds for history, which announces nothing old."""
    try:
        v = load_state().get("beeper_seen")
    except OSError:
        return None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _note_beeper_mark(when: float) -> None:
    """The watcher's on_mark. Returns at once: the state file is written
    behind it, and of several marks in a row the newest is the one saved."""
    if when <= _BEEPER_MARK["want"]:
        return
    _BEEPER_MARK["want"] = when
    loop = asyncio.get_running_loop()
    task = _BEEPER_MARK["task"]
    if task is None or task.done() or task.get_loop() is not loop:
        _BEEPER_MARK["task"] = loop.create_task(_save_beeper_mark())


async def _save_beeper_mark() -> None:
    while _BEEPER_MARK["saved"] < _BEEPER_MARK["want"]:
        want = _BEEPER_MARK["want"]
        try:
            await asyncio.to_thread(save_state, beeper_seen=want)
        except Exception as e:
            print(f"[beeper] the mark could not be saved ({type(e).__name__})")
            return
        _BEEPER_MARK["saved"] = want


def _watch_beeper():
    return beeper.watch(_on_beeper_message, since=_beeper_since(), on_mark=_note_beeper_mark)


@app.on_event("startup")
async def _startup():
    _tighten_files()
    init_fcm()
    # Prime the title cache so the first push after a reboot has proper names.
    for _t in fetch_threads(200):
        CHAT_TITLES.setdefault(_t["chat_guid"], _t["chat_name"])
    _spawn("poll", poll_loop)
    if beeper.enabled():
        print("[beeper] enabled — watching Google Messages")
        _spawn("beeper", _watch_beeper)
    else:
        print("[beeper] disabled (no BEEPER_TOKEN)")


class PushReq(BaseModel):
    token: str


@app.post("/register_push")
def register_push(req: PushReq):
    tokens = push_tokens()
    if req.token not in tokens:
        tokens.add(req.token)
        save_tokens(tokens)
        print(f"[fcm] registered device token ({len(tokens)} total)")
    return {"ok": True, "count": len(tokens)}


@app.get("/health")
def health(request: Request):
    # An unauthenticated caller (the middleware exempts /health so a tunnel can
    # be probed) learns only that something answers. The cursor, contact count,
    # own identities and BlueBubbles reachability go only to a caller
    # presenting the token the way the middleware accepts it.
    #
    # There is no ?nonce= variant any more. It answered HMAC(token, nonce) to
    # anyone, for a LAN probe the app no longer performs, and one such answer
    # is enough to test token guesses offline. A nonce parameter is now
    # ignored like any other unknown query parameter.
    supplied = (request.headers.get("x-imsg-token")
                or request.query_params.get("token"))
    if not token_matches(supplied):
        return {"ok": True}
    # The ping is unguarded on purpose (as before): it answers "is a BlueBubbles
    # server up at BB_URL" even when no password is configured.
    bb = _bluebubbles().ping()
    chain = _chain()
    # "capabilities" (step R6, additive: the protocol number stays): what this
    # chain can do in an iMessage chat beyond plain sending, sorted, out of
    # react / reply / create_chat / unsend / edit, so a client can offer
    # "Edit" and "Undo Send" only where they can work.
    try:
        cursor = load_cursor()
    except OSError:
        cursor = None             # the state file cannot be read right now
    return {"ok": True, "cursor": cursor, "contacts": len(CONTACTS),
            "self": SELF_RAW, "bb_reachable": bb,
            "engines": engine_names(chain), "features": FEATURES, "protocol": PROTOCOL,
            # "send_id": /send and /create_chat keep an id the client sends (client_id) and do
            # not send the same one twice. A client may rely on that only where it is named.
            "capabilities": sorted(imessage_capabilities(chain) + ["send_id"])}


@app.get("/contacts")
def contacts_dump():
    items = list(CONTACTS.items())[:10]
    return {"count": len(CONTACTS), "sample": dict(items)}


@app.post("/contacts/refresh")
def contacts_refresh():
    load_contacts()
    return {"count": len(CONTACTS)}


# ---------- voice assistant ----------
# Phone-side voice (Gemini/Bixby -> MacroDroid) ships the raw sentence here; ALL
# the intelligence lives on this side, where the authoritative contact map and
# the chat matcher already are.
#
# Speech-to-text mangles surnames ("Schaefer" -> "shaffer"), so recipients are
# matched fuzzily with first names weighted heavily. Confident matches go
# straight to confirmation; ambiguous ones are read back as suggestions. Every
# path is confirmed out loud before anything sends, which is what makes fuzzy
# matching safe here.

ASSIST_VERBS = ("send an imessage to", "send imessage to", "send a message to",
                "send message to", "send a text to", "send text to",
                "imessage", "i message", "text", "message", "tell", "send")
ASSIST_JOINERS = ("that says", "with the message", "to say", "saying", "that")
YES_WORDS = ("yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm",
             "send it", "send", "do it", "correct", "right")
NO_WORDS = ("no", "nope", "cancel", "stop", "nevermind", "never mind", "abort")
# A refusal anywhere in the answer. squash() turns "don't" into "don t"; a bare
# "don" is not listed, because a contact can be called Don.
NEGATIONS = ("not", "don t", "dont", "do not", "never", "wrong", "incorrect", "wait",
             "neither", "none", "nobody", "nothing")


def _says(ans: str, words) -> bool:
    """Whole words and phrases only: "correct" is not in "incorrect"."""
    padded = f" {ans} "
    return any(f" {w} " in padded for w in words)


#: Every answer that sends. After squash() the WHOLE answer must be one of
#: these. A closed list on purpose: a list of refusals is never complete, and
#: a list of allowed words is not closed either ("that's ok" is a polite no,
#: "correct that" is a correction, "ok do" was cut off).
YES_PHRASES = frozenset({
    "yes", "yes please", "yes thanks", "yes thank you", "yes send it", "yes do it", "yes send",
    "yes correct", "yes that s right", "yes that s correct",
    "yeah", "yeah send it", "yeah do it", "yep", "yup",
    "ok", "okay", "ok send it", "okay send it", "ok send", "ok do it", "okay do it",
    "ok thanks", "okay thanks", "ok thank you", "okay thank you", "ok yes", "okay yes",
    "sure", "sure send it", "sure do it",
    "confirm", "send", "send it", "send it please", "please send it", "do it", "do it please",
    "correct", "that s correct", "right", "that s right",
})


def _only_yes(answer: str) -> bool:
    """A plain yes and nothing else: one of YES_PHRASES, said as a statement."""
    return "?" not in (answer or "") and squash(answer) in YES_PHRASES


def _latin(name: str) -> bool:
    """True when squash() keeps every letter of the name, so that words said can be told from its words."""
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(ch))
    return not any(ch.isalpha() and not ch.isascii() for ch in folded)


def _named(ans: str, cands):
    """The one candidate the answer names: words that all belong to its name
    and to no other candidate's. Never a resemblance, and never a name part
    of which is in another script (its other part cannot be heard here)."""
    words = set(ans.split())
    hits = [c for c in cands if words and words <= set(squash(c[1]).split())]
    return hits[0] if len(hits) == 1 and _latin(hits[0][1]) else None


def _not_understood(answer: str) -> bool:
    """A digit, or a letter of another script: nothing here can read it as a yes or a name."""
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", answer or "") if not unicodedata.combining(ch))
    plain = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ .,!'’-"
    return any(ch not in plain for ch in folded)

PENDING: dict = {}
PENDING_TTL = 180
NAMESAKES_SPEAK = "You have more than one contact called {}. Send that one from the app."

CONFIDENT_SCORE = 0.80   # accept outright
CONFIDENT_LEAD = 0.06    # ...if it also beats the runner-up by this much
SUGGEST_SCORE = 0.55     # otherwise offer as a suggestion


def _failure_status(e: Exception) -> str:
    """A failed send, for the log: the HTTP status it is answered with, or the
    exception class. Never the detail: a DeliveryError can pass an upstream
    body through whole, and BlueBubbles' body for a failed send carries the
    message itself. The chain has already logged the hop ("[send] ... failed")
    without that part."""
    status = getattr(e, "status", None)
    return f"HTTP {status}" if status else type(e).__name__


def squash(s):
    """Lower-case Latin letters and single spaces: accents folded, everything else dropped."""
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z ]", " ", s.lower()).split())


def _sound_word(w: str) -> str:
    """One word of a name as it is said, roughly: letters that are written and
    not said are dropped (Leigh, Knight, Thompson, Lamb), spellings of one
    sound are one (ph/f/v, c/k/q, c/s/z, g/j before e and i), a run of vowels
    is one of three classes. Rough on purpose: it is only ever used to refuse."""
    for old, new in (("tch", "ch"), ("ph", "f"), ("ck", "k"), ("dg", "j"), ("mps", "ms"), ("mpt", "mt"),
                     ("nds", "ns"), ("lm", "m")):
        w = w.replace(old, new)
    for old, new in (("kn", "n"), ("gn", "n"), ("pn", "n"), ("wr", "r"), ("ps", "s"), ("wh", "w")):
        if w.startswith(old):
            w = new + w[len(old):]
    if w.endswith("mb"):
        w = w[:-1]
    if w.endswith(("ay", "ey")) and len(w) > 3:
        w = w[:-2] + "y"                         # Lindsay / Linsey
    w = w[:1] + w[1:].replace("gh", "")
    w = w.replace("sh", "S").replace("ch", "C").replace("th", "T")
    if len(w) > 2 and w[-1] == "e" and w[-2] not in "aeiouy" and any(c in "aeiouy" for c in w[:-2]):
        w = w[:-1]                               # a final e that is written and not said (Anne, Jane)
    out = []
    for i, ch in enumerate(w):
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if ch == "h" and i:
            continue
        if ch == "c":
            ch = "s" if nxt and nxt in "eiy" else "k"
        elif ch == "g" and nxt and nxt in "eiy":
            ch = "j"
        elif ch in "qk":
            ch = "k"
        elif ch == "x":
            ch = "ks"
        elif ch == "z":
            ch = "s"
        elif ch == "v":
            ch = "f"
        elif ch in "aeiouy":
            ch = "a" if ch == "a" else ("o" if ch in "ou" else "i")
            if out and out[-1] in ("a", "i", "o"):
                continue                         # a run of vowels counts once, as its first
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def name_key(name) -> str:
    """A contact name as it sounds, roughly. Case, spacing, punctuation,
    accents and common spelling variants of one sound ("Sara" / "Sarah",
    "Jon" / "John", "Lee" / "Leigh", "O'Brien" / "OBrien") do not tell two
    names apart, because speech-to-text and a read-back cannot tell them
    apart either. Only ever used to REFUSE: two cards with one key are
    namesakes for the voice path. Nothing is merged by it, and it does not
    claim to know every pair of names that sound alike."""
    words = squash(name).split()
    key = re.sub(r"([aio])[aio]+", r"\1", "".join(_sound_word(w) for w in words))   # "Mary Ann" is "Maryann"
    return key or "".join((name or "").lower().split())


def _ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def name_score(spoken, name):
    """Fuzzy similarity, weighting first names (STT keeps those; surnames die)."""
    a, b = squash(spoken), squash(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    at, bt = a.split(), b.split()
    full = _ratio(a, b)
    first = _ratio(at[0], bt[0])
    if len(at) == 1:
        tok = max(_ratio(at[0], y) for y in bt)
        return max(full, first * 0.95, tok * 0.80)
    return 0.45 * first + 0.55 * full


def name_index():
    """lowercase name -> (display name, [addresses]) from the contact map. An
    address the card shares with another card comes after the card's own, so
    a message for one of a couple is not addressed to their landline."""
    idx = {}
    for key, name in sorted(CONTACTS.items(), key=lambda kv: kv[0] in CONTACT_SHARED):
        addr = contact_address(key)
        if not addr:
            continue
        entry = idx.setdefault(name.lower(), (name, []))
        if addr not in entry[1]:
            entry[1].append(addr)
    return idx


def namesake_names() -> set:
    """Lower-cased contact names that sit on more than one card: two people
    the voice path cannot tell apart, so it sends to neither."""
    cards: dict[str, set] = {}
    for key, name in CONTACTS.items():
        card = CONTACT_CARD[key] if key in CONTACT_CARD else (CONTACT_CANON.get(key) or key)
        cards.setdefault(name_key(name), set()).add(card)
    return {name for name, on in cards.items() if len(on) > 1} | CARD_NAMESAKES


def preferred_address(addrs):
    """A phone number before an e-mail address, and the card's own addresses
    before one it shares with another card."""
    own = [a for a in addrs if norm_key(a) not in CONTACT_SHARED] or addrs
    phones = [a for a in own if "@" not in a]
    return (phones or own)[0]


def contact_recency():
    """Contact name -> newest ROWID in their 1:1 chat. Ranks people you
    actually text above namesakes you don't."""
    conn = db()
    try:
        # (chat_identifier, newest ROWID) per 1:1 chat with messages; rows with
        # a NULL identifier are left out (they could never name a contact).
        rows = one_to_one_activity(conn)
    finally:
        conn.close()
    out = {}
    for ci, last in rows:
        nm = CONTACTS.get(norm_key(ci))
        if nm and last > out.get(nm, 0):
            out[nm] = last
    return out


def _strip_joiner(msg):
    ml = msg.lower()
    for j in sorted(ASSIST_JOINERS, key=len, reverse=True):
        if ml.startswith(j + " "):
            return msg[len(j) + 1:].strip()
    return msg


def _has_one_to_one(addr: str) -> bool:
    conn = db()
    try:
        found = find_chat_for_addresses(conn, [addr])
    finally:
        conn.close()
    return bool(found) and not found.get("is_group")


def resolve_assistant(q: str):
    """-> (status, candidates, text). Candidates are (score, display, addrs)."""
    s = " ".join((q or "").strip().split())
    low = s.lower()
    for v in sorted(ASSIST_VERBS, key=len, reverse=True):
        if low.startswith(v + " "):
            s = s[len(v) + 1:]
            break
    if not s:
        return "no_recipient", [], ""

    # Spoken raw number: "text 310 555 0123 hey"
    m = re.match(r"^([\d\s\-\(\)\+\.]{7,})\s+(\S.*)$", s)
    if m and sum(c.isdigit() for c in m.group(1)) >= 7:
        num = m.group(1).strip()
        # The digits run on into the message ("text 555 0142 100 dollars"): only
        # a number that is grouped like one is taken for one.
        shape = tuple(len(g) for g in re.findall(r"\d+", num))
        if not num.startswith("+") and shape not in ((10,), (11,), (3, 3, 4), (1, 3, 3, 4), (3, 7), (1, 10)):
            return "no_recipient", [], ""
        try:
            addr = normalize_address(num)
        except BadAddress:
            return "no_recipient", [], ""
        # And it has to be a number the relay already knows: a contact's, or one
        # there is a conversation with. A number that is only spoken can have
        # swallowed the digits the message began with, and nobody would notice.
        if norm_key(addr) not in CONTACTS and not _has_one_to_one(addr):
            return "no_recipient", [], ""
        return "confident", [(1.0, num, [addr])], m.group(2).strip()

    idx = name_index()
    recency = contact_recency()
    words = s.split()
    # The name that was said belongs to a card none of whose numbers can be
    # used: say so (as "not found"), and do not offer a name that resembles it.
    have = {name_key(display) for display, _ in idx.values()}
    for i in range(1, min(4, len(words)) + 1):
        said = name_key(" ".join(words[:i]))
        if said and said in CARD_UNSENDABLE and said not in have:
            return "not_found", [], s
    best = {}   # display name -> (score, msg, addrs)
    for i in range(1, min(4, len(words)) + 1):
        span = " ".join(words[:i])
        msg = _strip_joiner(" ".join(words[i:]))
        for key, (display, addrs) in idx.items():
            sc = name_score(span, key)
            if sc <= 0 or display in best and best[display][0] >= sc:
                continue
            best[display] = (sc, msg, addrs)

    if not best:
        return "not_found", [], s
    ranked = sorted(
        ((sc, d, a, msg) for d, (sc, msg, a) in best.items()),
        key=lambda c: (round(c[0], 2), len(squash(c[1]).split()) if c[0] >= 1.0 else 0, recency.get(c[1], 0)),
        reverse=True,
    )
    top = ranked[0]
    shared = namesake_names()
    if name_key(top[1]) in shared and top[0] >= SUGGEST_SCORE:
        return "namesakes", [(top[0], top[1], top[2])], top[3]
    if not top[3]:
        return "no_message", [(top[0], top[1], top[2])], ""

    second = ranked[1][0] if len(ranked) > 1 else 0.0
    exact = squash(" ".join(words[:len(top[1].split())])) == squash(top[1])
    if top[0] >= CONFIDENT_SCORE and (exact or top[0] - second >= CONFIDENT_LEAD):
        return "confident", [(top[0], top[1], top[2])], top[3]
    if top[0] >= SUGGEST_SCORE:
        # Only names that leave the same message: the question reads ONE message
        # back, and it has to be the one that is sent whoever is then named.
        cands = [(sc, d, a) for sc, d, a, msg in ranked
                 if sc >= SUGGEST_SCORE and name_key(d) not in shared and msg == top[3]][:3]
        return "suggest", cands, top[3]
    return "not_found", [], top[3]


async def read_fields(request, *fields):
    """Accept JSON body, form-encoded body, or query params interchangeably —
    automation apps disagree about how to POST, and a 422 is a dead end."""
    data = {}
    try:
        raw = await request.body()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                from urllib.parse import parse_qs
                data = {k: v[0] for k, v in parse_qs(raw.decode(errors="ignore"), keep_blank_values=True).items()}
    except Exception:
        pass
    # A body that names one of the fields decides them all: an empty answer in
    # the body is an empty answer, not a reason to look in the URL. A value
    # that is not a string was not spoken and counts as nothing.
    if any(f in data for f in fields):
        return {f: (data[f].strip() if isinstance(data.get(f), str) else "") for f in fields}
    qp = dict(request.query_params)
    return {f: str(qp.get(f) or "").strip() for f in fields}


@app.post("/assistant/prepare")
async def assistant_prepare(request: Request):
    f = await read_fields(request, "query")
    status, cands, text = resolve_assistant(f["query"])

    if status == "no_recipient" or status == "not_found":
        return {"ok": False, "status": "not_found",
                "speak": "I couldn't tell who to send that to."}
    if status == "namesakes":
        return {"ok": False, "status": "not_found", "speak": NAMESAKES_SPEAK.format(cands[0][1])}
    if status == "no_message":
        return {"ok": False, "status": "no_message",
                "speak": f"What should I send to {cands[0][1]}?"}

    for t, p in list(PENDING.items()):
        if p["expires"] < time.time():
            PENDING.pop(t, None)
    token = uuid.uuid4().hex[:12]

    if status == "confident":
        _, display, addrs = cands[0]
        PENDING[token] = {"kind": "confirm", "name": display, "addresses": addrs,
                          "text": text, "expires": time.time() + PENDING_TTL}
        # The dictated message is never logged, only its length.
        print(f"[assist] {len(text)}-character message -> {display} (confident)")
        return {"ok": True, "status": "confirm", "token": token,
                "chat_name": display, "text": text,
                "speak": f"Send {text} to {display}?"}

    PENDING[token] = {"kind": "choose", "candidates": cands, "text": text,
                      "expires": time.time() + PENDING_TTL}
    names = [c[1] for c in cands]
    listed = names[0] if len(names) == 1 else \
        " or ".join([", ".join(names[:-1]), names[-1]])
    print(f"[assist] {len(text)}-character message -> ambiguous: {names}")
    return {"ok": True, "status": "choose", "token": token,
            "candidates": names, "text": text,
            "speak": f"Did you mean {listed}? The message is: {text}. Say the full name, or say cancel."}


async def assistant_deliver(p: dict, answer: str) -> dict:
    """Interpret the second voice turn and send. Handles both a yes/no
    confirmation and a 'which one did you mean' suggestion round. Shared by the
    JSON and plain-text endpoints."""
    ans = squash(answer)
    cancelled = {"ok": False, "status": "cancelled", "speak": "Cancelled."}
    # Fail closed. Nothing is sent unless the answer is understood from its
    # first word to its last: as a yes and nothing else, or as one candidate's
    # name. An answer with a digit or another script in it, an empty one, a
    # refusal, a doubt, a correction, a pause: all of them cancel.
    if not ans or _not_understood(answer) or "?" in answer or ans in NO_WORDS + NEGATIONS:
        return cancelled
    refused = _says(ans, NO_WORDS + NEGATIONS)

    if p["kind"] == "choose":
        cands = p["candidates"]
        # Only a candidate's FULL name picks it: a word of a name is also an
        # ordinary word ("skip", "other", "will"), and nothing here can tell
        # which was meant. The words must also belong to no other candidate.
        said_in_full = [c for c in cands if squash(c[1]) == ans]
        if len(said_in_full) == 1 and _named(ans, cands) is said_in_full[0]:
            pick = said_in_full[0]            # whatever words the name is made of
        elif said_in_full or refused:
            return cancelled                  # more than one candidate answers to that, or it is a refusal
        else:
            pick = cands[0] if len(cands) == 1 and _only_yes(answer) else None
        if pick is None:
            return {"ok": False, "status": "cancelled",
                    "speak": "I didn't catch which one. Cancelled."}
        _, display, addrs = pick
    else:
        if refused or not _only_yes(answer):
            return cancelled
        display, addrs = p["name"], p["addresses"]

    addr = preferred_address(addrs)
    conn = db()
    try:
        found = find_chat_for_addresses(conn, [addr])
    finally:
        conn.close()

    if found:
        try:
            res = await deliver_text(found["chat_guid"], p["text"])
        except Exception as e:
            print(f"[assist] send failed: {_failure_status(e)}")
            return {"ok": False, "status": "failed",
                    "speak": f"I couldn't send that to {display}."}
        print(f"[assist] sent to {display} via {res.get('via', 'bb')}")
        return {"ok": True, "status": "sent", "via": res.get("via", "bb"),
                "speak": f"Sent to {display}."}

    # No existing chat: only an engine with CREATE_CHAT (BlueBubbles) can start
    # one (AppleScript can't), so this path has no fallback — fail with a
    # spoken reason, never a bare 500.
    try:
        res = await deliver(_chain(), Capability.CREATE_CHAT, None, [addr], p["text"])
    except Exception as e:
        print(f"[assist] new-chat failed: {_failure_status(e)}")
        return {"ok": False, "status": "failed",
                "speak": f"I couldn't start a new conversation with {display}."}
    print(f"[assist] started new chat with {display}")
    return {"ok": True, "status": "sent", "via": res.via, "speak": f"Sent to {display}."}


@app.post("/assistant/confirm")
async def assistant_confirm(request: Request):
    f = await read_fields(request, "token", "answer")
    p = PENDING.pop(f["token"], None)
    if not p or p["expires"] < time.time():
        return {"ok": False, "status": "expired",
                "speak": "That message expired. Try again."}
    return await assistant_deliver(p, f["answer"])


# ---------- plain-text voice endpoints (for automation apps) ----------
# MacroDroid/Tasker save an HTTP response as one raw string and can't easily
# pluck fields out of JSON. These endpoints return a bare English sentence the
# app can speak directly, and keep the pending message in a single server-side
# slot — so no token has to survive the trip back to the phone. Single-user
# relay, so one slot is enough.

LAST_PENDING: dict = {}


# POST only, like /assistant/*: a GET is what an address makes when anything
# merely loads it (the image of a received link preview, a page in a browser),
# and loading an address must never arm a message or send one.
@app.post("/v/prepare")
async def v_prepare(request: Request):
    LAST_PENDING.clear()          # first of all: whatever goes wrong below, the previous message is not left armed
    f = await read_fields(request, "query", "q")
    query = f["query"] or f["q"]
    status, cands, text = resolve_assistant(query)

    if status in ("no_recipient", "not_found"):
        return PlainTextResponse("I couldn't tell who to send that to.")
    if status == "namesakes":
        return PlainTextResponse(NAMESAKES_SPEAK.format(cands[0][1]))
    if status == "no_message":
        return PlainTextResponse(f"What should I send to {cands[0][1]}?")

    if status == "confident":
        _, display, addrs = cands[0]
        LAST_PENDING.update({"kind": "confirm", "name": display, "addresses": addrs,
                             "text": text, "expires": time.time() + PENDING_TTL})
        print(f"[assist] {len(text)}-character message -> {display} (confident)")
        return PlainTextResponse(f"Send {text} to {display}?")

    LAST_PENDING.update({"kind": "choose", "candidates": cands, "text": text,
                         "expires": time.time() + PENDING_TTL})
    names = [c[1] for c in cands]
    listed = names[0] if len(names) == 1 else \
        " or ".join([", ".join(names[:-1]), names[-1]])
    print(f"[assist] {len(text)}-character message -> ambiguous: {names}")
    return PlainTextResponse(f"Did you mean {listed}? The message is: {text}. Say the full name, or say cancel.")


@app.post("/v/confirm")
async def v_confirm(request: Request):
    f = await read_fields(request, "answer", "a")
    p = dict(LAST_PENDING)
    LAST_PENDING.clear()
    if not p or p.get("expires", 0) < time.time():
        return PlainTextResponse("There's nothing waiting to send.")

    res = await assistant_deliver(p, f["answer"] or f["a"])
    return PlainTextResponse(res["speak"])


@app.get("/search")
def search_messages(q: str = "", limit: int = 30, chat: str = ""):
    """Message-content search across all chats, or within one chat when `chat`
    is a chat guid. Scans both the text column and the attributedBody blob
    (where modern macOS often stores the text); a Python-side recheck against
    the parsed text drops binary false positives.

    `chat` empty: the global search, unchanged. An iMessage guid: the same
    statement scoped with `AND chat.guid = ?` (bound, never interpolated), so
    the oversample and the cap apply to that chat's rows alone; an unknown guid
    yields no results. A Beeper ("bp:") guid: the chat's newest 500 messages
    from the Beeper API, filtered here case-insensitively, newest first, capped
    at `limit`; those results carry `rowid` 0 and the message `guid`."""
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    if chat and beeper.is_beeper_guid(chat):
        # Sync endpoint (FastAPI runs it on a worker thread with no event loop),
        # so the async Beeper read gets a loop of its own.
        msgs = asyncio.run(beeper.fetch_messages(chat, limit=500))
        needle = q.lower()
        out = []
        for m in sorted(msgs, key=lambda m: m.get("date") or 0, reverse=True):
            text = m.get("text") or ""
            i = text.lower().find(needle)
            if i < 0:
                continue
            who = "You" if m.get("is_from_me") else (m.get("sender") or "").split(" ")[0]
            out.append({
                "chat_guid": chat, "chat_name": CHAT_TITLES.get(chat) or "",
                "rowid": 0, "date": m.get("date") or 0.0,
                "snippet": (f"{who}: " if who else "") + search_snippet(text, i),
                "guid": m.get("guid") or "",
            })
            if len(out) >= limit:
                break
        return {"results": out}
    # attributedBody is typedstream: NUL bytes in its headers kill LIKE (SQLite
    # string matching stops at the first NUL), so the library searches the blob
    # with byte-level instr() across case variants, oversamples the SQL LIMIT
    # (limit * 2) and rechecks every row against the decoded, whitespace-
    # collapsed text, which keeps results honest either way. Each hit carries
    # that text and the match index; at most `limit` hits come back.
    conn = db()
    try:
        out = []
        for hit in search_rows(conn, q, limit, chat_guid=chat or None):
            snippet = search_snippet(hit.text, hit.match_index)
            is_group = hit.is_group
            title = group_title(conn, hit.chat_rowid, hit.chat_display_name) if is_group \
                else resolve(hit.chat_identifier)
            who = "You" if hit.is_from_me else \
                ((resolve(hit.sender_handle) or "").split(" ")[0] if hit.sender_handle else "")
            out.append({
                "chat_guid": hit.chat_guid, "chat_name": title,
                "rowid": hit.rowid, "date": apple_date_to_unix(hit.date),
                "snippet": (f"{who}: " if who else "") + snippet,
            })
        return {"results": out}
    finally:
        conn.close()


@app.get("/contacts/lookup")
def contacts_lookup(q: str = ""):
    """Debug: how does an address normalize, and does the map resolve it?"""
    k = norm_key(q)
    return {"query": q, "norm_key": k, "found": k in CONTACTS,
            "name": CONTACTS.get(k), "contact_count": len(CONTACTS)}


@app.get("/contacts/search")
def contacts_search(q: str = ""):
    """Typeahead for the app's compose screen: match name or number/email."""
    q = q.strip().lower()
    if len(q) < 2:
        return {"results": []}
    seen, results = set(), []
    for key, name in CONTACTS.items():
        if q not in name.lower() and q not in key:
            continue
        addr = contact_address(key)
        if not addr:
            continue
        k = (name.lower(), addr)
        if k in seen:
            continue
        seen.add(k)
        results.append({"name": name, "address": addr})
    results.sort(key=lambda r: (not r["name"].lower().startswith(q), r["name"].lower()))
    return {"results": results[:15]}


@app.get("/threads")
async def threads(limit: int = 200):
    imsg = fetch_threads(limit)
    gm = await beeper.fetch_threads(limit) if beeper.enabled() else []
    # Merge; carry over pin/archive state the relay owns (keyed by chat_guid).
    for t in gm:
        t["pinned"] = t["chat_guid"] in PINS
        t["pin_index"] = PINS.index(t["chat_guid"]) if t["chat_guid"] in PINS else -1
        t["archived"] = t["chat_guid"] in ARCHIVED
        t["auto_translate"] = t["chat_guid"] in AUTO_TRANSLATE
        if t["chat_guid"] in FORCED_UNREAD:
            t["unread"] = max(1, int(t.get("unread") or 0))
    merged = imsg + gm
    for t in merged:
        if t.get("chat_name"):
            CHAT_TITLES[t["chat_guid"]] = t["chat_name"]
    merged.sort(key=lambda t: t.get("last_date") or 0, reverse=True)
    return {"threads": merged}


@app.get("/thread/{chat_guid}/messages")
async def thread_messages(chat_guid: str, limit: int = 50, before: int | None = None):
    if beeper.is_beeper_guid(chat_guid):
        return {"messages": await beeper.fetch_messages(chat_guid, limit)}
    msgs = fetch_thread_messages(chat_guid, limit, before)
    schedule_link_resolves(enrich_links(msgs))
    return {"messages": msgs}


@app.get("/chat_icon/{guid}")
async def chat_icon(guid: str):
    """Real iMessage group photo, proxied from BlueBubbles and cached on disk.
    BB returns it as octet-stream, so the type is sniffed from magic bytes."""
    ICON_CACHE.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]", "_", guid)[:120]
    cached = ICON_CACHE / safe
    if cached.exists() and cached.stat().st_size > 0:
        return FileResponse(cached, media_type=sniff_image(cached.read_bytes()[:12]))

    seen = NO_ICON.get(guid)
    if seen and time.time() - seen < NO_ICON_TTL:
        return Response(status_code=404)   # known iconless — don't bother BB again

    # No engine with CHAT_ICON -> 501; BB unreachable -> 502 (DeliveryError handler).
    content = (await deliver(_chain(), Capability.CHAT_ICON, guid)).payload
    if not content:
        NO_ICON[guid] = time.time()
        save_state(no_icon=NO_ICON)
        print(f"[icon] {guid} has no group photo — won't ask again")
        return Response(status_code=404)
    if NO_ICON.pop(guid, None) is not None:
        save_state(no_icon=NO_ICON)
    cached.write_bytes(content)
    print(f"[icon] cached group icon for {guid} ({len(content)} bytes)")
    return FileResponse(cached, media_type=sniff_image(content[:12]))


@app.post("/chat_icon/refresh")
def chat_icon_refresh():
    """Forget cached icons (and misses) — use after changing a group photo."""
    NO_ICON.clear()
    save_state(no_icon={})
    n = 0
    if ICON_CACHE.exists():
        for f in ICON_CACHE.iterdir():
            f.unlink(missing_ok=True)
            n += 1
    print(f"[icon] cache cleared ({n} files)")
    return {"ok": True, "cleared": n}


def sniff_image(head: bytes) -> str:
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[4:8] == b"ftyp":
        return "image/heic"
    if head.startswith(b"GIF8"):
        return "image/gif"
    return "application/octet-stream"


@app.get("/link_image/{rowid}")
def link_image(rowid: int):
    """Serves the preview image Apple embedded in the message's payload."""
    p = link_payload(rowid)
    if not p:
        raise HTTPException(404, "no payload")
    # Every PNG/JPEG/ISOBMFF blob over 3000 bytes in the archive's $objects
    # table: None when the payload is not a plist / has no $objects, [] when
    # it carries no image. The largest wins (first of equals).
    blobs = embedded_images(p)
    if blobs is None:
        raise HTTPException(404, "unparseable payload")
    if not blobs:
        raise HTTPException(404, "no embedded image")
    best = max(blobs, key=len)
    return Response(content=best, media_type=sniff_image(best[:12]))


# Tiny always-resident MarianMT service for Spanish/Latin-script -> English.
# Instant (no model loading); Qwen handles non-Latin and acts as fallback.
MARIAN_URL = os.environ.get("MARIAN_URL", "http://127.0.0.1:8701").rstrip("/")

# Home Assistant, for family/vehicle locations (Tessie, device_trackers, etc.).
HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = drop_placeholder(os.environ.get("HA_TOKEN", "").strip())
# Comma-separated "Label=entity_id" pairs, e.g.
#   HA_LOCATIONS="Alice=device_tracker.alice_car,Bob=device_tracker.bob_phone"
HA_LOCATIONS = os.environ.get("HA_LOCATIONS", "").strip()
# Apple MapKit JS token (the long-lived one from HA's findmy-map). Served in the
# /map page below so the app's WebView loads it from the relay's public origin.
MAPKIT_TOKEN = drop_placeholder(os.environ.get("MAPKIT_TOKEN", "").strip())

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:30b")
# How long Ollama keeps the model in RAM after the last request. "5m" default;
# "0" unloads immediately (each translate then pays a cold load), "-1" = never.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "5m")


class UnreadReq(BaseModel):
    chat_guid: str


@app.post("/unread")
def mark_unread(req: UnreadReq):
    """Manually flag a chat unread; cleared next time it's opened (/read)."""
    FORCED_UNREAD.add(req.chat_guid)
    save_state(forced_unread=sorted(FORCED_UNREAD))
    return {"ok": True}


class AutoTranslateReq(BaseModel):
    chat_guid: str
    enabled: bool


@app.post("/auto_translate")
def auto_translate(req: AutoTranslateReq):
    """Mark/unmark a conversation for translate-on-open."""
    if req.enabled:
        AUTO_TRANSLATE.add(req.chat_guid)
    else:
        AUTO_TRANSLATE.discard(req.chat_guid)
    save_state(auto_translate=sorted(AUTO_TRANSLATE))
    print(f"[translate] auto={'on' if req.enabled else 'off'} for {req.chat_guid}")
    return {"ok": True}


class TranslateReq(BaseModel):
    text: str
    target: str = "English"


_EN_WORDS = {
    "the", "and", "you", "for", "that", "this", "with", "have", "are", "was",
    "were", "not", "but", "what", "your", "will", "can", "from", "they",
    "just", "about", "when", "how", "get", "its", "it's", "i'm", "im",
    "dont", "don't", "do", "is", "of", "to", "in", "it", "on", "my", "me",
    "we", "be", "at", "so", "if", "or", "he", "she", "did", "got", "thanks",
    "thank", "ok", "okay", "yes", "yeah", "good", "see", "there", "here",
}


def _looks_english(text: str) -> bool:
    """Cheap stopword check: enough common English words -> skip translating.
    Needs 3+ words to judge; shorter messages fall through to the identity
    check after Marian instead."""
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if len(words) < 3:
        return False
    hits = sum(1 for w in words if w in _EN_WORDS)
    return hits / len(words) >= 0.25


def _norm(t: str) -> str:
    return re.sub(r"[\W_]+", "", t.lower())


def _mostly_latin(text: str) -> bool:
    """Latin-script text (Spanish, French, ...) — Marian territory. Persian,
    Arabic, CJK etc. go to Qwen, which handles them far better."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    non_latin = sum(1 for c in letters if ord(c) > 0x024F)
    return non_latin / len(letters) < 0.4


@app.post("/translate")
async def translate(req: TranslateReq):
    """Latin script -> Marian microservice (instant, 300MB resident);
    everything else -> local Ollama/Qwen. All on this machine."""
    # Attachment placeholders (U+FFFC) are invisible non-text; translating
    # them produced hallucinated single letters. Strip before any judgment.
    text = req.text.replace("\ufffc", "").replace("\ufffd", "").strip()
    # No letters at all (emoji, URLs, numbers)? Nothing any model can do —
    # this exact case was 502ing out of Marian and waking Ollama for emoji.
    if not any(c.isalpha() for c in text):
        return {"translation": ""}
    # Already English? Nothing to translate — empty result tells the app to
    # cache the skip (no card, no retry) instead of burning a model on it.
    if _looks_english(text):
        return {"translation": ""}

    if _mostly_latin(text):
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                r = await client.post(f"{MARIAN_URL}/translate",
                                      json={"text": text})
            if r.status_code < 400:
                out = (r.json().get("translation") or "").strip()
                if out and _norm(out) == _norm(text):
                    # Model echoed the input — it was English (or untranslatable).
                    # Do NOT fall through to Ollama; that's how English messages
                    # were waking the 19GB model.
                    return {"translation": ""}
                if out:
                    return {"translation": out}
                return {"translation": ""}
            # Marian answered but couldn't translate (empty/bad input). For
            # Latin-script text Qwen won't do better — skip, don't escalate.
            print(f"[translate] marian HTTP {r.status_code} — treating as skip")
            return {"translation": ""}
        except Exception as e:
            # Service actually unreachable — only then is Ollama the backstop.
            print(f"[translate] marian unavailable ({e}) — falling back to ollama")

    prompt = (f"Translate the following message into natural {req.target}. "
              f"Output ONLY the translation, nothing else.\n\n{req.text}")
    payload = {
        "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
        "think": False,                      # qwen3: skip the reasoning preamble
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {"temperature": 0.2},
    }
    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            r = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
    except Exception as e:
        raise HTTPException(502, f"ollama unreachable: {e}")
    if r.status_code >= 400:
        raise HTTPException(502, f"ollama: {r.text[:200]}")
    out = r.json().get("response", "")
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    if not out:
        raise HTTPException(502, "empty translation")
    return {"translation": out}


def _extract_latlon(attrs: dict):
    """Find coordinates regardless of how the integration names them."""
    lat = attrs.get("latitude")
    lon = attrs.get("longitude")
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    # nested {"location": {...}} or gps list forms
    loc = attrs.get("location") or {}
    if isinstance(loc, dict) and loc.get("latitude") is not None:
        return float(loc["latitude"]), float(loc["longitude"])
    gps = attrs.get("gps")
    if isinstance(gps, (list, tuple)) and len(gps) == 2:
        return float(gps[0]), float(gps[1])
    return None, None


@app.get("/map", response_class=Response)
def map_page(token: str = ""):
    """Full MapKit JS page, served from the relay's public tunnel origin so the
    app's WebView loads a genuine URL on the domain the MapKit token is
    restricted to. The page fetches /locations itself and refreshes live."""
    html = """<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="referrer" content="origin">
<style>html,body,#map{height:100%;width:100%;margin:0;padding:0;background:#000}
#err{position:absolute;top:0;left:0;right:0;padding:8px;font:12px sans-serif;color:#fff;
background:#c00;z-index:9999;display:none;white-space:pre-wrap}</style>
</head><body><div id="map"></div><div id="err"></div>
<script src="https://cdn.apple-mapkit.com/mk/5.x.x/mapkit.js"></script>
<script>
function showErr(m){var e=document.getElementById('err');e.textContent=m;e.style.display='block';}
window.addEventListener("error",function(e){showErr("js: "+e.message);});
var map=null;
mapkit.init({ authorizationCallback:function(done){ done("__TOKEN__"); } });
mapkit.addEventListener("error",function(ev){
  showErr("mapkit: "+(ev&&ev.type)+" status="+(ev&&ev.status)+" "+(ev&&ev.message||""));
});
function ready(cb){
  if (mapkit.initialized) return cb();
  mapkit.addEventListener("configuration-change",function(ev){ if(ev.status==="Initialized") cb(); });
  var n=0, iv=setInterval(function(){ if(mapkit.initialized){clearInterval(iv);cb();} else if(++n>40){clearInterval(iv);showErr("never init | origin="+window.location.origin+" | ref="+document.referrer+" | ua="+navigator.userAgent.substring(0,60));} },250);
}
function render(places){
  if(!map){
    map=new mapkit.Map("map",{colorScheme:mapkit.Map.ColorSchemes.Dark});
  }
  map.removeAnnotations(map.annotations);
  var annos=places.filter(function(p){return p.lat!=null&&p.lon!=null;}).map(function(p){
    var t=p.name+(p.state?(" \u00B7 "+p.state):"");
    return new mapkit.MarkerAnnotation(new mapkit.Coordinate(p.lat,p.lon),{title:t});
  });
  if(annos.length){
    map.showItems(annos);
    if(annos.length===1){ map.setCameraDistanceAnimated(4000,false); }
  }
}
function poll(){
  fetch("/locations?token=__IMSGTOK__").then(function(r){return r.json();}).then(function(d){
    render(d.locations||[]);
  }).catch(function(e){ showErr("locations: "+e.message); });
}
ready(function(){ poll(); setInterval(poll, 30000); });
</script></body></html>"""
    html = html.replace("__TOKEN__", MAPKIT_TOKEN).replace("__IMSGTOK__", token)
    return Response(content=html, media_type="text/html",
                    headers={"Referrer-Policy": "origin"})


@app.get("/locations")
async def locations():
    """Family/vehicle positions from Home Assistant, for the app's map.
    Self-diagnosing: if an entity has no recognizable coordinates, the entry
    includes the raw attribute keys it *did* have, so the right field can be
    pinned down without guessing."""
    if not HA_TOKEN or not HA_LOCATIONS:
        raise HTTPException(503, "HA not configured (set HA_TOKEN and HA_LOCATIONS)")
    pairs = []
    for chunk in HA_LOCATIONS.split(","):
        if "=" in chunk:
            label, ent = chunk.split("=", 1)
            pairs.append((label.strip(), ent.strip()))
    out = []
    async with httpx.AsyncClient(timeout=15) as client:
        for label, ent in pairs:
            try:
                r = await client.get(
                    f"{HA_URL}/api/states/{ent}",
                    headers={"Authorization": f"Bearer {HA_TOKEN}"},
                )
                if r.status_code >= 400:
                    out.append({"name": label, "entity": ent,
                                "error": f"HA HTTP {r.status_code}"})
                    continue
                data = r.json()
                attrs = data.get("attributes", {})
                lat, lon = _extract_latlon(attrs)
                if lat is None:
                    # Couldn't find coords — report what WAS there so we can fix
                    # the field name in one pass.
                    out.append({"name": label, "entity": ent,
                                "state": data.get("state"),
                                "no_coords": True,
                                "attribute_keys": sorted(attrs.keys())})
                else:
                    out.append({
                        "name": label, "entity": ent,
                        "lat": lat, "lon": lon,
                        "accuracy": attrs.get("gps_accuracy"),
                        "updated": data.get("last_updated"),
                        "state": data.get("state"),
                    })
            except Exception as e:
                out.append({"name": label, "entity": ent, "error": str(e)})
    return {"locations": out}


#: The URL schemes Beeper Desktop's own asset endpoint takes (the pattern in
#: its 4.3.160 bundle is ^(mxc|localmxc|file)://) and the only ones /bp_asset
#: hands on to it. Matched as written: the check is case-sensitive, like Beeper's.
BP_ASSET_SCHEMES = ("mxc://", "localmxc://", "file://")


def _file_url_navigates(u: str) -> bool:
    """True for a ``file://`` URL whose path, percent-decoded once, has a
    ``..`` segment or a NUL byte. Beeper names a file where it is; a path
    that climbs out of its starting directory, or that ends early for a C
    string, is somebody probing what Beeper Desktop will serve, so the relay
    does not pass it on. This is one narrow check, not a jail: where an
    accepted ``file://`` URL points is still Beeper Desktop's decision."""
    path = unquote(u[len("file://"):])
    return "\x00" in path or ".." in re.split(r"[/\\]", path)


@app.get("/bp_asset")
async def bp_asset(u: str | None = None, src: str | None = None):
    """Proxy a Beeper asset (mxc://, localmxc://, file://) as bytes for the phone.
    `src` is accepted as an alias: beeper.py emitted `?src=` (unencoded) until
    2026-09-27, and the phone may still hold cached message rows with that shape.
    Any other URL scheme is refused here (400) and never reaches Beeper Desktop,
    and so is a file:// URL with a ".." segment or a NUL in its path.
    What Beeper Desktop serves for an accepted URL is its own decision."""
    u = u or src
    if not u:
        raise HTTPException(422, "u required")
    if not u.startswith(BP_ASSET_SCHEMES) or (u.startswith("file://") and _file_url_navigates(u)):
        raise HTTPException(400, "unsupported asset url")
    got = await beeper.asset_url(u)
    if not got:
        raise HTTPException(404, "asset unavailable")
    content, mime = got
    # Beeper usually answers application/octet-stream; sniff so the phone sees
    # image/gif etc. Matrix media (mxc://) is immutable, so let Coil cache it —
    # without this a query-string URL gets zero heuristic freshness and every
    # scroll-back re-downloads the whole GIF phone <- relay <- Beeper.
    if not mime or mime == "application/octet-stream":
        mime = sniff_image(content[:12])
    return Response(content=content, media_type=mime,
                    headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/attachment/{guid}")
def attachment(guid: str):
    a = attachment_by_guid(guid)
    if not a:
        raise HTTPException(404, "unknown attachment")
    path = os.path.expanduser(a.filename or "")
    if not path or not os.path.exists(path):
        raise HTTPException(404, "file missing on disk")
    name = a.transfer_name or os.path.basename(path)
    if _is_heic(a.mime_type, name) or _is_heic(None, path):
        out = HEIC_CACHE / f"{guid}.jpg"
        if not out.exists():
            # Temp-then-move, same as /thumbnail: a killed sips must not leave
            # a partial file to be cached (and served) forever.
            try:
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td) / "out.jpg"
                    p = subprocess.run(
                        ["sips", "-s", "format", "jpeg",
                         "-s", "formatOptions", "85", path, "--out", str(tmp)],
                        capture_output=True, timeout=20,
                    )
                    if tmp.exists() and tmp.stat().st_size > 0:
                        if not out.exists():
                            shutil.move(str(tmp), out)
                    else:
                        # sips fails by exit code, not exception — log it or
                        # every request re-fails with nothing to diagnose.
                        print(f"[attachment] sips failed for {guid}: rc={p.returncode} "
                              f"{p.stderr.decode(errors='replace')[:200]}")
            except Exception as e:
                print(f"[attachment] heic transcode failed for {guid}: {e}")
        if out.exists() and out.stat().st_size > 0:
            _, jpg_name = att_meta(a.mime_type, name)
            # Immutable: message attachments never change, and a clean cache
            # policy stops the phone's heuristic caching from replaying stale
            # bytes for this (fresh ?f=jpg) URL.
            return FileResponse(out, media_type="image/jpeg",
                                filename=jpg_name or "image.jpg",
                                headers={"Cache-Control": "private, max-age=31536000, immutable"})
        # Transcode failed: serve the original bytes, and stop advertising
        # JPEG for this guid so metadata matches what clients actually get.
        FAILED_HEIC.add(guid)
    if _is_caf(a.mime_type, name) or _is_caf(None, path):
        out = AUDIO_CACHE / f"{guid}.m4a"
        if not out.exists():
            # Temp file in the cache dir, then an atomic os.replace: a killed
            # afconvert must not leave a partial file to be served forever.
            tmp = AUDIO_CACHE / f".{guid}.{uuid.uuid4().hex}.tmp"
            try:
                p = subprocess.run(
                    ["afconvert", "-f", "m4af", "-d", "aac", "-b", "64000",
                     path, str(tmp)],
                    capture_output=True, timeout=60,
                )
                if p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    os.replace(tmp, out)
                else:
                    print(f"[attachment] afconvert failed for {guid}: rc={p.returncode}")
            except Exception as e:
                print(f"[attachment] afconvert failed for {guid}: rc={type(e).__name__}")
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        if out.exists() and out.stat().st_size > 0:
            _, m4a_name = att_meta(a.mime_type, name)
            # FileResponse honours Range requests, so seeking works on the
            # phone; immutable because message attachments never change.
            return FileResponse(out, media_type="audio/mp4",
                                filename=m4a_name or "audio.m4a",
                                headers={"Cache-Control": "private, max-age=31536000, immutable"})
        # Transcode failed: serve the original CAF, and stop advertising M4A
        # for this guid so metadata matches what clients actually get.
        FAILED_CAF.add(guid)
    return FileResponse(
        path,
        media_type=a.mime_type or "application/octet-stream",
        filename=name,
    )


@app.get("/thumbnail/{guid}")
def thumbnail(guid: str):
    """Quick Look preview for any attachment (videos, PDFs, docs...). Generated
    once via `qlmanage -t` and cached on disk under thumb_cache/."""
    out = THUMB_DIR / f"{guid}.png"
    if not out.exists():
        a = attachment_by_guid(guid)
        if not a:
            raise HTTPException(404, "unknown attachment")
        path = os.path.expanduser(a.filename or "")
        if not path or not os.path.exists(path):
            raise HTTPException(404, "file missing on disk")
        try:
            with tempfile.TemporaryDirectory() as td:
                subprocess.run(
                    ["qlmanage", "-t", "-s", "512", "-o", td, path],
                    capture_output=True, timeout=25,
                )
                produced = list(Path(td).glob("*.png"))
                if not produced:
                    raise HTTPException(404, "no preview available")
                if not out.exists():
                    shutil.move(str(produced[0]), out)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(404, f"thumbnail failed: {e}")
    return FileResponse(out, media_type="image/png")


@app.get("/thread/{chat_guid}/media")
def thread_media(chat_guid: str):
    """Everything shareable in a chat: attachments (newest first) and links."""
    conn = db()
    try:
        # Newest message first (ORDER BY m.date DESC); rich-link payload blobs
        # (.pluginPayloadAttachment) are already left out.
        attachments = []
        for a in chat_attachments(conn, chat_guid):
            mime, name, url = att_public(a.guid, a.mime_type, a.transfer_name)
            attachments.append({
                "guid": a.guid, "mime_type": mime, "name": name,
                "url": url,
                "date": apple_date_to_unix(a.message_date),
            })
        # Links often live in NULL-text rows whose body is in the attributedBody
        # blob, so scan recent rows as decoded messages (text comes from the
        # blob when the column is empty) instead of relying on SQL LIKE against
        # m.text. extract_urls = https?://\S+ hits, trailing ".,);" stripped.
        links, seen = [], set()
        for m in recent_messages(conn, chat_guid, 1000):
            for u in extract_urls(m.text):
                if u in seen:
                    continue
                seen.add(u)
                links.append({
                    "url": u,
                    "date": apple_date_to_unix(m.date),
                    "sender": "me" if m.is_from_me else resolve(m.sender_handle),
                })
        return {"attachments": attachments, "links": links}
    finally:
        conn.close()


class CreateChatReq(BaseModel):
    addresses: list[str]
    text: str
    client_id: str | None = None


class MatchChatReq(BaseModel):
    addresses: list[str]


@app.post("/match_chat")
def match_chat(req: MatchChatReq):
    """Does an existing chat exactly match this recipient set? Drives the
    compose screen's inline history."""
    try:
        addrs = [normalize_address(a) for a in req.addresses if a.strip()]
    except BadAddress:
        return {"found": False}
    if not addrs:
        return {"found": False}
    conn = db()
    try:
        found = find_chat_for_addresses(conn, addrs)
        if found:   # same optional thread fields /threads carries
            rid = conn.execute("SELECT ROWID FROM chat WHERE guid = ?",
                               (found["chat_guid"],)).fetchone()
            svc = chat_services(conn, [rid[0]]).get(rid[0]) if rid else None
            found.update(mac_thread_labels(svc))
    finally:
        conn.close()
    return {"found": True, **found} if found else {"found": False}


@app.post("/create_chat")
async def create_chat(req: CreateChatReq):
    """Start a conversation. Any recipient set (1:1 or group) that already has
    a chat gets the message sent into it; only genuinely new sets go through
    BlueBubbles' chat-creation endpoint (Private API handles group creation)."""
    try:
        addrs = [normalize_address(a) for a in req.addresses if a.strip()]
    except BadAddress as e:
        raise HTTPException(400, f"a recipient is {e}")
    if not addrs or not req.text.strip():
        raise HTTPException(400, "addresses and text required")

    def existing():
        conn = db()
        try:
            return find_chat_for_addresses(conn, addrs)
        finally:
            conn.close()

    async def run():
        found = existing()
        if found:
            guid = found["chat_guid"]
            res = await deliver(_chain(), Capability.TEXT, guid, req.text)
            print(f"[create] reused existing chat {guid} for {addrs}")
            return {"ok": True, "chat_guid": guid}, res.via
        # Only an engine with CREATE_CHAT (BlueBubbles) can start a chat: none
        # configured -> 501, an upstream error -> passed through with its status.
        res = await deliver(_chain(), Capability.CREATE_CHAT, None, addrs, req.text)
        print(f"[create] new chat {res.payload} -> {addrs}")
        return {"ok": True, "chat_guid": res.payload}, res.via

    def again(rec):
        # Where the conversation is now, looked up afresh: the id file keeps no recipient.
        found = existing()
        return {"chat_guid": found["chat_guid"] if found else None}

    # The same people in another order are the same conversation.
    return await _send_once(req.client_id, _send_fingerprint("create_chat", sorted(addrs), req.text), run, again)


# ---------- send-engine chain (engines/) ----------
# The AppleScript fallback (the public-API floor), the BlueBubbles Private API
# and the Beeper bridge are chain members now; see engines/chain.py. The
# AppleScript engine reads the osascript runner and the outbox through THIS
# module's `_applescript` / `OUTBOX` at call time, so patching them here (as the
# tests do) still takes effect.

_APPLESCRIPT = AppleScriptEngine(runner=lambda script, *args: _applescript(script, *args),
                                 outbox=lambda: OUTBOX)


def _engine_env() -> dict:
    """The values build_chain reads, as this module read them at import (so a
    test can patch BB_PASSWORD etc. on the module)."""
    return {"BB_URL": BB_URL, "BB_PASSWORD": BB_PASSWORD, "BEEPER_TOKEN": beeper.BEEPER_TOKEN,
            "SEND_ENGINES": SEND_ENGINES, "SEND_APPLESCRIPT_FALLBACK": SEND_APPLESCRIPT_FALLBACK}


def _imessage_cli() -> ImessageCliEngine:
    """The edit engine for the binary found at import (none: not configured,
    and build_chain leaves it out). The tool keeps its own state between calls
    under the data directory."""
    return ImessageCliEngine(IMESSAGE_CLI_BIN, lambda: DATA_DIR / "imessage-cli")


def _chain():
    return build_chain(_engine_env(), applescript=_APPLESCRIPT, imessage_cli=_imessage_cli())


def _bluebubbles() -> BlueBubblesEngine:
    """A BlueBubbles engine for BB_URL whether or not a password is set (the
    /health ping is deliberately unguarded)."""
    return BlueBubblesEngine(BB_URL, BB_PASSWORD)


print(f"[engines] send chain: {', '.join(engine_names(_chain())) or '(none)'}; "
      f"features: {', '.join(k for k, v in FEATURES.items() if v) or '(none)'}")


@app.exception_handler(DeliveryError)
async def _delivery_error(request: Request, exc: DeliveryError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


# ---------- send ids: one send is one message, however often its request arrives ----------
# The relay could not tell a repeated request from a second message, so a text
# that had gone out and was sent "again" after an answer that never arrived
# went out twice. A text send may carry an id the client chose (`client_id`,
# on /send and /create_chat). The id is written down before any engine is
# asked, and what became of the send when it ends. A request whose id is
# known is answered from that and, unless the first try certainly failed,
# nothing is sent. Voice and attachments carry no id and are as they were.

#: How long an id is remembered, and how many at most (the oldest go first).
SEND_ID_KEEP_SECONDS = 48 * 3600
SEND_ID_MAX = 2000
_SEND_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_SEND_STATES = ("started", "delivered", "failed", "unknown")


class SendIds:
    """The ids of text sends and what became of each, in a file beside the
    state file: per id a fingerprint of the message (so that an id cannot
    stand for two), a state, a time, and for a delivered send its path. No
    text and no recipient.

    States: ``started`` (written before any engine is asked), ``delivered``,
    ``failed`` (certainly not sent: it may be sent again under the same id)
    and ``unknown``. A ``started`` that this process did not write itself was
    begun by a process that never said how it ended: it reads as ``unknown``.

    Every change is on disk (fsync) before the call returns. A file that
    cannot be read raises ``OSError`` and is tried again at the next call:
    "not readable" is not "nothing known". A file that is not a list of ids
    is put aside and said so; the ids in it are then no longer recognised."""

    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._known: dict | None = None
        self._own: set = set()

    def _read(self) -> dict:
        if self._known is None:
            try:
                text = self.path.read_text()
            except FileNotFoundError:
                self._known = {}
                return self._known
            try:
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                self._known = {k: v for k, v in data.items()
                               if isinstance(k, str) and isinstance(v, dict)
                               and v.get("state") in _SEND_STATES and isinstance(v.get("fp"), str)}
            except ValueError:
                aside = self.path.with_name(f"{self.path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
                with contextlib.suppress(OSError):
                    self.path.replace(aside)
                print(f"[send] {self.path.name} did not hold send ids: kept as {aside.name}, starting empty "
                      "(a send made before now is no longer recognised if its request comes again)")
                self._known = {}
        return self._known

    def _write(self) -> None:
        known = self._known
        cutoff = self._clock() - SEND_ID_KEEP_SECONDS
        for sid in [s for s, rec in known.items() if rec.get("at", 0) < cutoff and s not in self._own]:
            del known[sid]
        if len(known) > SEND_ID_MAX:
            oldest = sorted((s for s in known if s not in self._own), key=lambda s: known[s].get("at", 0))
            for sid in oldest[: len(known) - SEND_ID_MAX]:
                del known[sid]
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(known))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def look(self, sid: str) -> dict | None:
        with self._lock:
            rec = self._read().get(sid)
            if rec is None:
                return None
            rec = dict(rec)
            if rec["state"] == "started" and sid not in self._own:
                rec["state"] = "unknown"
            return rec

    def begin(self, sid: str, fingerprint: str) -> None:
        """On disk when this returns, before any engine is asked."""
        with self._lock:
            known = self._read()
            before = known.get(sid)
            known[sid] = {"fp": fingerprint, "state": "started", "at": self._clock()}
            self._own.add(sid)
            try:
                self._write()
            except BaseException:
                self._own.discard(sid)
                if before is None:
                    known.pop(sid, None)
                else:
                    known[sid] = before
                raise

    def finish(self, sid: str, state: str, via: str | None = None) -> None:
        with self._lock:
            rec = self._read().get(sid)
            if rec is None:
                return
            rec["state"] = state
            if via:
                rec["via"] = via
            self._own.discard(sid)
            self._write()


SEND_IDS = SendIds(STATE_PATH.with_name("send_ids.json"))
#: client_id -> a future that ends when the request working on that id has ended.
_SENDS_IN_FLIGHT: dict = {}


def _send_fingerprint(*parts) -> str:
    """What an id stands for, without keeping it: the route, the chat or the recipients, the text."""
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]


def _send_state_for(status: int) -> str:
    """What an answer of the relay's says about a send. 4xx and 501 refuse it
    before any engine could have sent: certainly not sent. Everything else
    (the relay's own 502 included, which the app already reads as "may have
    been sent") leaves it open."""
    return "failed" if 400 <= status < 500 or status == 501 else "unknown"


async def _send_once(client_id, fingerprint: str, run, again):
    """One send per id. ``run()`` sends and returns ``(reply, via)``;
    ``again(record)`` gives what a repeat of a delivered send is answered with.

    Without an id: ``run()``, as it always was. With one: a request whose id
    is still being worked on waits for that request and is then answered like
    any repeat; a delivered send is answered ``ok`` with ``duplicate`` and not
    sent; a send whose outcome is not known (it ended without an answer, or
    the relay stopped in the middle of it) is refused with 409 and never sent
    under this id again; a send that certainly failed is sent."""
    if client_id is None:
        return (await run())[0]
    if not isinstance(client_id, str) or not _SEND_ID.fullmatch(client_id):
        raise HTTPException(400, "client_id has to be 8 to 64 letters, digits, '-' or '_'")
    while client_id in _SENDS_IN_FLIGHT:
        await asyncio.wait({_SENDS_IN_FLIGHT[client_id]})
    mine = asyncio.get_running_loop().create_future()
    _SENDS_IN_FLIGHT[client_id] = mine          # no await between the check above and this line
    try:
        try:
            rec = await asyncio.to_thread(SEND_IDS.look, client_id)
            if rec is not None:
                if rec["fp"] != fingerprint:
                    raise HTTPException(409, {"code": "send_id_reused",
                                              "message": "this send id belongs to another message"})
                if rec["state"] == "delivered":
                    print("[send] a repeated request for a delivered send was answered without sending")
                    return {**again(rec), "ok": True, "duplicate": True}
                if rec["state"] != "failed":
                    print("[send] a repeated request was refused: what became of its first try is not known")
                    raise HTTPException(409, {"code": "send_outcome_unknown", "message": MAYBE_SENT})
            await asyncio.to_thread(SEND_IDS.begin, client_id, fingerprint)
        except OSError as e:
            # Not sent: without the ids nobody could say later whether it was.
            print(f"[send] the send ids cannot be read or written ({type(e).__name__}) — a send that carries an id is not made")
            raise HTTPException(503, {"code": "send_ids_unavailable",
                                      "message": "the relay cannot keep track of sends right now: nothing was sent"})
        state, via = None, None
        try:
            reply, via = await run()
            state = "delivered"
            return reply
        except DeliveryError as e:
            state = _send_state_for(e.status)
            raise
        except HTTPException as e:
            state = _send_state_for(e.status_code)
            raise
        except Exception:
            state = "unknown"
            raise
        finally:
            # Anything else that ends the request (it was cancelled, the relay
            # is stopping) leaves the id as "started", which reads as unknown.
            if state is not None:
                try:
                    await asyncio.to_thread(SEND_IDS.finish, client_id, state, via)
                except Exception as e:
                    print(f"[send] what became of a send could not be written ({type(e).__name__}): it reads as unknown after a restart")
    finally:
        _SENDS_IN_FLIGHT.pop(client_id, None)
        mine.set_result(None)


class SendReq(BaseModel):
    chat_guid: str
    text: str
    reply_to_guid: str | None = None
    client_id: str | None = None


async def deliver_text(chat_guid: str, text: str, reply_to_guid: str | None = None):
    """Walk the chain for TEXT: BlueBubbles first, AppleScript fallback (Beeper
    for bp: guids). Shared by /send and the voice assistant path. Returns
    {"ok": True, "via": ...} or raises DeliveryError (501/502/upstream status).

    Replies need the Private API (AppleScript can't thread), so a reply that
    falls back to AppleScript lands as a normal message rather than not at all."""
    res = await deliver(_chain(), Capability.TEXT, chat_guid, text, reply_to_guid=reply_to_guid)
    body = {"ok": True, "via": res.via}
    if res.via == "bb":
        body["bb"] = res.payload
    return body


@app.post("/send")
async def send(req: SendReq):
    async def run():
        body = await deliver_text(req.chat_guid, req.text, req.reply_to_guid)
        return body, body.get("via")

    return await _send_once(req.client_id, _send_fingerprint("send", req.chat_guid, req.text, req.reply_to_guid),
                            run, lambda rec: {"via": rec.get("via")})


@app.post("/send_attachment")
async def send_attachment(chat_guid: str = Form(...), file: UploadFile = File(...)):
    """Multipart upload from the app -> the chain's ATTACHMENT engines
    (BlueBubbles, then AppleScript; a bp: chat has none -> 501). The sent
    message then lands in chat.db and flows back through the normal poll."""
    fname = file.filename or f"upload-{int(time.time())}"
    content = await file.read()
    if not content:
        raise HTTPException(400, "empty file")
    res = await deliver(_chain(), Capability.ATTACHMENT, chat_guid, fname, content,
                        file.content_type or "application/octet-stream")
    print(f"[send] attachment {fname} ({len(content)} bytes) -> {chat_guid} via {res.via}")
    # Shape frozen (plan objection 13): a BlueBubbles success carries no "via"
    # — ChatVM.noteSendPath keys on its presence.
    if res.via == "bb":
        return {"ok": True}
    return {"ok": True, "via": res.via}


class PinReq(BaseModel):
    chat_guid: str
    pinned: bool


class ReadReq(BaseModel):
    chat_guid: str
    rowid: int


@app.post("/read")
async def mark_read(req: ReadReq):
    """High-water mark from the app: everything up to rowid in this chat is read."""
    if beeper.is_beeper_guid(req.chat_guid):
        await beeper.mark_read(req.chat_guid)
        if req.chat_guid in FORCED_UNREAD:      # clear our manual flag too
            FORCED_UNREAD.discard(req.chat_guid)
            save_state(forced_unread=sorted(FORCED_UNREAD))
        return {"ok": True}
    st = load_state()
    reads = st.get("reads", {})
    changed = False
    if req.rowid > int(reads.get(req.chat_guid, 0)):
        reads[req.chat_guid] = req.rowid
        changed = True
    if req.chat_guid in FORCED_UNREAD:
        FORCED_UNREAD.discard(req.chat_guid)
        changed = True
    if changed:
        save_state(reads=reads, forced_unread=sorted(FORCED_UNREAD))
    return {"ok": True}


@app.post("/pin")
def pin(req: PinReq):
    if req.pinned and req.chat_guid not in PINS:
        PINS.append(req.chat_guid)  # new pins go to the end of the row
    if not req.pinned and req.chat_guid in PINS:
        PINS.remove(req.chat_guid)
    save_state(pins=list(PINS))
    return {"ok": True, "pins": list(PINS)}


class ArchiveReq(BaseModel):
    chat_guid: str
    archived: bool


@app.post("/archive")
def archive(req: ArchiveReq):
    """Archive/unarchive a conversation. Relay-side only — nothing is deleted
    and Messages on the Mac is untouched."""
    if req.archived:
        ARCHIVED.add(req.chat_guid)
        PINS[:] = [g for g in PINS if g != req.chat_guid]   # archiving unpins
        save_state(archived=sorted(ARCHIVED), pins=list(PINS))
    else:
        ARCHIVED.discard(req.chat_guid)
        save_state(archived=sorted(ARCHIVED))
    print(f"[archive] {'archived' if req.archived else 'restored'} {req.chat_guid}")
    return {"ok": True, "archived": sorted(ARCHIVED)}


class PinOrderReq(BaseModel):
    order: list[str]


@app.post("/pin_order")
def pin_order(req: PinOrderReq):
    """Full pinned ordering from the app. Unknown guids are dropped; any
    pinned chat missing from the payload keeps its spot at the end."""
    new = [g for g in req.order if g in PINS]
    new += [g for g in PINS if g not in new]
    PINS[:] = new
    save_state(pins=list(PINS))
    return {"ok": True, "pins": list(PINS)}


class ReactReq(BaseModel):
    chat_guid: str
    message_guid: str
    reaction: str  # love | like | dislike | laugh | emphasize | question


@app.post("/react")
async def react(req: ReactReq):
    # REACT has no fallback engine: a BlueBubbles error passes through with
    # its status; no engine (no BB_PASSWORD) -> 501.
    await deliver(_chain(), Capability.REACT, req.chat_guid, req.message_guid, req.reaction)
    return {"ok": True}


# ---------- edit / undo send (step R6) ----------
# Changing a message the owner has already sent. No single engine does both on
# macOS 27: BlueBubbles unsends (its edit call answers 200 and changes
# nothing), imessage-cli edits (its undo-send prints "ok" and retracts
# nothing). Hence three rules, the same for both routes:
#
# * the message is looked up in chat.db BEFORE any engine is asked: it must
#   exist in the chat that was named, be a message (not a tapback or a group
#   event), be the owner's own, be an iMessage, not be unsent already, and be
#   young enough for Apple to still allow the change;
# * an engine's "ok" is not believed. chat.db is read again, for up to
#   CHANGE_CONFIRM_SECONDS, and the app is told ok only once the database
#   shows the change. The poll loop then broadcasts the row as an "update",
#   exactly as it does for an edit made on another device;
# * nor is an engine's failure, once it may have reached Messages (the tool
#   was started and then timed out or exited badly; BlueBubbles gave no
#   answer, or a 5xx): chat.db is read for CHANGE_RECHECK_SECONDS, and a
#   change that is there is answered as the success it is.
#
# Edits run one at a time, and the checks are made again when an edit's turn
# has come (_edit_turn): the tool takes seconds, and what was true when the
# request arrived (inside Apple's window, a different text, fewer than five
# edits) need not be true any more when the edit before it has finished.
#
# What the app is told about a failure is the relay's own, on both routes.
# 501 is answered only when no engine in the chain can do the action at all
# (the chain's own words): the app stops offering the action for the rest of
# its session when it reads one, so a refusal for one chat or one message is
# a 409. And an engine that was asked and failed is a 502 with one of the
# fixed details below, never the status or the body of what it talks to: the
# app would read BlueBubbles' 401 as the relay rejecting its token, a 403 as
# "not your message", a 404 as a relay without these routes.
#
# The log lines of this section name the action, the engine and the outcome,
# never a chat, a message or its text.

UNSEND_WINDOW_SECONDS = 120          # Apple: Undo Send for two minutes
EDIT_WINDOW_SECONDS = 900            # Apple: Edit for fifteen minutes ...
MAX_EDITS = 5                        # ... and five times per message
MAX_EDIT_CHARS = 10000
FUTURE_TOLERANCE_SECONDS = 60        # a date this far ahead of the clock is still "just sent"
CHANGE_CONFIRM_SECONDS = 8.0         # how long chat.db is watched after an engine said ok
CHANGE_CONFIRM_INTERVAL = 0.25
CHANGE_RECHECK_SECONDS = 2.0         # ... and after an engine that failed once it had been started
CHANGE_SETTLE_SECONDS = 1.0          # how long an edited row may hold another text before that is the answer
EDIT_QUEUE_LIMIT = 3                 # edits that may wait behind the one that is running
EDIT_QUEUE_SECONDS = 20.0            # ... and for how long one of them waits for its turn

CHANGE_UNKNOWN = "unknown message"
CHANGE_NOT_YOURS = "only your own messages can be changed"
CHANGE_UNSUPPORTED = "this chat cannot edit or unsend"
CHANGE_ALREADY_UNSENT = "already unsent"
CHANGE_NOT_SENT_YET = "this message has not been sent yet"
CHANGE_TOO_LATE_UNSEND = "too late to unsend (Apple allows 2 minutes)"
CHANGE_TOO_LATE_EDIT = "too late to edit (Apple allows 15 minutes)"
CHANGE_EDITED_OUT = "this message has been edited 5 times already"
CHANGE_BUSY = "another edit is still running on the Mac, try again in a moment"
CHANGE_NOT_APPLIED = "the Mac did not apply the change"
CHANGE_DB_UNREADABLE = "the Messages database could not be read"
# An engine that was asked and failed: 502 with one of these three, or with
# one of imessage-cli's own phrases (_CLI_DETAILS). None of them may contain
# "did not apply": the app takes the 502 that says so (CHANGE_NOT_APPLIED)
# for "certainly nothing changed", and every other 502 for "cannot say".
UNSEND_REFUSED = "BlueBubbles refused the request"           # it answered 4xx: Messages was not touched
UNSEND_FAILED = "BlueBubbles could not unsend the message"   # no answer or a 5xx, and chat.db shows no change
EDIT_FAILED = "the edit engine failed"                       # a failure that is none of the tool's phrases
EDIT_TEXT_EMPTY = "text must not be empty"
EDIT_TEXT_CONTROL = "text must not contain control characters"

#: imessage-cli's failure phrases (engines/imessage_cli.py) other than
#: "imessage-cli exited <status>": fixed words that quote no argument and
#: nothing the tool printed, so an edit's 502 carries them as they are.
_CLI_DETAILS = frozenset({
    imessage_cli_engine.NOT_FOUND, imessage_cli_engine.TIMED_OUT, imessage_cli_engine.NOT_STARTED,
    imessage_cli_engine.REPORTED_ERROR, imessage_cli_engine.NEEDS_ACCESSIBILITY,
    imessage_cli_engine.BAD_MESSAGE_ID, imessage_cli_engine.BAD_CHAT_ID, imessage_cli_engine.BAD_TEXT,
    imessage_cli_engine.OPTION_TEXT, imessage_cli_engine.ONLY_FIRST_PART})

#: What a later reading of the row says about a change (_await_change).
CONFIRMED = "confirmed"              # the change that was asked for
DIFFERS = "differs"                  # an edit landed, with another text than the one asked for


class UnsendReq(BaseModel):
    chat_guid: str
    guid: str
    part_index: int = 0


class EditReq(BaseModel):
    chat_guid: str
    guid: str
    text: str
    part_index: int = 0


def _changeable(chat_guid: str, guid: str, part_index: int) -> chatdb_adapter.ChangeTarget:
    """The checks /edit and /unsend share, in the order the answers are
    documented, and the message as chat.db holds it right now. One read-only
    query; called in a worker thread.

    A Google Messages chat is answered before the lookup rather than after
    it: its messages are not in chat.db at all, so every one of them would
    otherwise read as "unknown message". A row of the message table that is
    not a message (a tapback, a group event: the owner's own rows too, when
    the owner reacted or renamed the group) is "unknown message" as well:
    Messages offers neither change for one, and what an engine would do when
    pointed at it was never tried.

    "This chat cannot" (a Google Messages chat, a message that is not an
    iMessage) is a 409 and not a 501: it is about one chat or one message,
    and a 501 from these routes says that no engine can do the action at all,
    which the app remembers for the rest of its session."""
    if beeper.is_beeper_guid(chat_guid):
        raise HTTPException(409, CHANGE_UNSUPPORTED)
    try:
        target = chatdb_adapter.change_target(chat_guid, guid)
    except Exception as e:
        print(f"[change] chat.db could not be read ({type(e).__name__})")
        raise HTTPException(503, CHANGE_DB_UNREADABLE)
    if target is None or not target.is_message:      # no such guid, not in this chat, not a message: one answer
        raise HTTPException(404, CHANGE_UNKNOWN)
    if not target.is_from_me:
        raise HTTPException(403, CHANGE_NOT_YOURS)
    if _norm_service(target.service) != "iMessage":     # SMS / RCS / unknown: Apple offers neither
        raise HTTPException(409, CHANGE_UNSUPPORTED)
    if target.is_retracted(part_index):
        raise HTTPException(409, CHANGE_ALREADY_UNSENT)
    if not 0 <= part_index <= chatdb_adapter.MAX_PART_INDEX:
        raise HTTPException(422, f"part_index must be between 0 and {chatdb_adapter.MAX_PART_INDEX}")
    return target


def _age_seconds(target: chatdb_adapter.ChangeTarget) -> float | None:
    """Seconds since the message was sent, by the database's own date; None
    when the row carries no date (the change is then refused as too late)."""
    sent = apple_date_to_unix(target.date)
    return None if sent is None else time.time() - sent


def _check_window(target: chatdb_adapter.ChangeTarget, seconds: float, too_late: str) -> None:
    """409 unless the message was sent within the last ``seconds``. A date
    ahead of the clock by more than FUTURE_TOLERANCE_SECONDS is not "recent":
    it is a message that has not gone out (Send Later keeps its send time in
    the date), and that is refused in its own words."""
    age = _age_seconds(target)
    if age is None or age > seconds:
        raise HTTPException(409, too_late)
    if age < -FUTURE_TOLERANCE_SECONDS:
        raise HTTPException(409, CHANGE_NOT_SENT_YET)


#: Characters Messages may substitute while a text is typed into it (smart
#: quotes, dashes, the ellipsis), folded before an edit is compared.
_TYPOGRAPHY = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'",
                             "“": '"', "”": '"', "„": '"', "‟": '"',
                             "–": "-", "—": "--", "…": "...", "￼": None})

#: What an edit's text may not carry: every C0 control character except tab
#: and line feed, and DEL. The tool enters the text into Messages through
#: Accessibility, and what a key code does there was never tried.
_EDIT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text_key(text: str | None) -> str:
    """``text`` as two texts are compared after an edit: Unicode-normalised,
    typographic substitutions folded, the attachment placeholder dropped and
    the ends trimmed. "It's here" asked for and "It’s here " stored are
    the same edit; any other difference is not."""
    return unicodedata.normalize("NFC", (text or "").translate(_TYPOGRAPHY)).strip()


def _plain_text(text: str | None) -> str:
    """``text`` the way an edit hands it to the tool: line ends as "\\n", the
    attachment placeholder (U+FFFC, which nobody types) dropped, both ends
    trimmed. The tool trims them itself before it enters the text, so a text
    that differs from the message's only there is not an edit."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("￼", "").strip()


def _edit_text(text: str) -> str:
    """The text of an /edit as it will be handed over (_plain_text), or a 422:
    longer than MAX_EDIT_CHARS as sent; nothing in it but white space,
    invisible characters (zero-width ones, the placeholder) and controls;
    or a control character other than tab and line feed."""
    if len(text) > MAX_EDIT_CHARS:
        raise HTTPException(422, f"text is longer than {MAX_EDIT_CHARS} characters")
    wanted = _plain_text(text)
    # Unicode categories Z* (separators) and C* (controls, format characters
    # such as U+200B, unassigned): a text made of those alone shows nothing.
    if not any(unicodedata.category(ch)[0] not in "ZC" for ch in wanted):
        raise HTTPException(422, EDIT_TEXT_EMPTY)
    if _EDIT_CONTROLS.search(wanted):
        raise HTTPException(422, EDIT_TEXT_CONTROL)
    return wanted


def _editable(req: EditReq) -> tuple[chatdb_adapter.ChangeTarget, str, bool]:
    """Every check /edit makes before the tool is asked, against the row as
    chat.db holds it NOW: ``(the row, the text to hand over, whether the
    message already has that text)``. Made twice for an edit that goes ahead:
    when the request arrives, so that a refusal does not wait for another
    edit to finish, and again once its turn has come. One read-only query;
    called in a worker thread."""
    target = _changeable(req.chat_guid, req.guid, req.part_index)
    _check_window(target, EDIT_WINDOW_SECONDS, CHANGE_TOO_LATE_EDIT)
    wanted = _edit_text(req.text)
    # The same text again (the placeholder an attachment leaves in the row
    # and white space at the ends apart): nothing to do, no engine is asked.
    if wanted == _plain_text(target.text):
        return target, wanted, True
    # None: the edit history could not be read (no such column in this
    # database, or not a property list). The check is then skipped, and a
    # sixth edit is left to Apple to refuse.
    edits = target.edit_count(req.part_index)
    if edits is not None and edits >= MAX_EDITS:
        raise HTTPException(409, CHANGE_EDITED_OUT)
    return target, wanted, False


def _unsent_since(before: chatdb_adapter.ChangeTarget, part_index: int):
    """Does a later reading of the row show that this part was unsent AFTER
    ``before`` was read? The part newly listed as retracted, a retraction date
    that moved, or an edit mark that moved on a row with no text left. (A row
    that had no text to begin with, a photo, proves nothing by being empty.)"""
    def verdict(now: chatdb_adapter.ChangeTarget) -> str | None:
        if part_index in now.retracted_parts and part_index not in before.retracted_parts:
            return CONFIRMED
        if now.date_retracted > before.date_retracted:
            return CONFIRMED
        return CONFIRMED if now.date_edited > before.date_edited and not now.has_text else None
    return verdict


def _edited_since(before: chatdb_adapter.ChangeTarget, text: str, part_index: int = 0):
    """What a later reading says about an edit to ``text``. CONFIRMED: the
    edit mark moved AND the text is the one that was asked for (_text_key).
    DIFFERS: the edit mark moved and the row holds a text that is neither
    that one nor the one it had, and was not unsent: an edit landed, and
    Messages entered something else than it was given (a text replacement,
    autocorrect). That edit is on every device; calling it "not applied"
    would be wrong, and a retry would spend another of the five edits. None:
    no edit is to be seen."""
    wanted, had = _text_key(text), _text_key(before.text)

    def verdict(now: chatdb_adapter.ChangeTarget) -> str | None:
        if now.date_edited <= before.date_edited:
            return None
        holds = _text_key(now.text)
        if holds == wanted:
            return CONFIRMED
        return DIFFERS if holds and holds != had and not now.is_retracted(part_index) else None
    return verdict


def _await_change(chat_guid: str, guid: str, verdict, seconds: float) -> str | None:
    """Read the row until ``verdict(row)`` is CONFIRMED, or has been DIFFERS
    for CHANGE_SETTLE_SECONDS (the row may still be on its way to the text
    that was asked for), or ``seconds`` have passed; the last verdict then.
    Blocking (it sleeps between readings): called in a worker thread."""
    deadline = time.monotonic() + seconds
    differs_since: float | None = None
    while True:
        try:
            now = chatdb_adapter.change_target(chat_guid, guid)
        except Exception:
            now = None                       # a busy database: look again
        outcome = verdict(now) if now is not None else None
        moment = time.monotonic()
        if outcome == CONFIRMED:
            return CONFIRMED
        if outcome == DIFFERS:
            differs_since = moment if differs_since is None else differs_since
            if moment - differs_since >= CHANGE_SETTLE_SECONDS:
                return DIFFERS
        if moment >= deadline:
            return outcome
        time.sleep(CHANGE_CONFIRM_INTERVAL)


#: An identifier shorter than this is not searched for in a log line.
_HIDE_MIN_CHARS = 8


def _change_log(action: str, *hide: str):
    """The chain's hop log for /edit and /unsend: its lines under the action's
    own tag, without the chat guid one of them ends with, and with ``hide``
    (the request's message and chat identifiers) blanked wherever an upstream
    error should quote them."""
    hidden = sorted({form for value in hide if value and len(value) >= _HIDE_MIN_CHARS
                     for form in (value, quote(value, safe=""))}, key=len, reverse=True)

    def log(line: str) -> None:
        line = line.replace("[send]", f"[{action}]", 1).split(" -> ", 1)[0]
        for value in hidden:
            line = line.replace(value, "***")
        print(line)
    return log


def _change_answer(via: str, outcome: str) -> dict:
    """{"ok": true, "via": ...}; plus "text_differs": true when the edit that
    landed holds another text than the one asked for (additive: a client that
    does not know the key reads the answer as the success it is, and the
    row's actual text reaches it as an "update" like every edit)."""
    answer: dict = {"ok": True, "via": via}
    if outcome == DIFFERS:
        answer["text_differs"] = True
    return answer


async def _confirm_change(action: str, via: str, chat_guid: str, guid: str, verdict) -> dict:
    """The answer once an engine has said ok: {"ok": true, "via": ...} when
    chat.db shows the change, 502 when it does not."""
    outcome = await asyncio.to_thread(_await_change, chat_guid, guid, verdict, CHANGE_CONFIRM_SECONDS)
    if outcome is None:
        print(f"[{action}] {via}: reported ok, but chat.db did not change")
        raise HTTPException(502, CHANGE_NOT_APPLIED)
    print(f"[{action}] {via}: " + ("confirmed in chat.db" if outcome == CONFIRMED
                                   else "applied, but chat.db holds another text than the one asked for"))
    return _change_answer(via, outcome)


async def _after_failure(action: str, via: str, chat_guid: str, guid: str, verdict,
                         detail: str) -> dict:
    """The answer when an engine failed AFTER it may have reached Messages.
    The tool prints its ok line and then closes the Messages instance it
    opened: a failure or a hang there ends as "exited 1" or "timed out" with
    the edit made; BlueBubbles can time out over an unsend it carried out.
    chat.db is read for CHANGE_RECHECK_SECONDS: a change that is there is the
    answer, and when none is, 502 with ``detail``: the relay's own fixed
    words for the failure, never the engine's upstream status or body."""
    outcome = await asyncio.to_thread(_await_change, chat_guid, guid, verdict, CHANGE_RECHECK_SECONDS)
    if outcome is None:
        raise HTTPException(502, detail)
    print(f"[{action}] {via}: failed, but chat.db shows the change")
    return _change_answer(via, outcome)


def _engine_via(chain, cap: Capability, chat_guid: str) -> str:
    engine = first_with(chain, cap, chat_guid)
    return engine.via if engine is not None else "engine"


def _edit_failure_detail(error: DeliveryError) -> str:
    """The 502 detail for an edit an engine failed at: imessage-cli's own
    fixed phrase when that is what the chain reports, and EDIT_FAILED for
    anything else (an upstream status or body, the text of an exception),
    which is never handed on."""
    detail = error.detail
    if error.status == 502 and isinstance(detail, str) and (detail in _CLI_DETAILS or imessage_cli_ran(detail)):
        return detail
    return EDIT_FAILED


@app.post("/unsend")
async def unsend_message(req: UnsendReq):
    """Undo Send for one of the owner's own iMessages, within Apple's two
    minutes. BlueBubbles only; 501 without it, and 502 in the relay's own
    words when BlueBubbles was asked and failed."""
    target = await asyncio.to_thread(_changeable, req.chat_guid, req.guid, req.part_index)
    _check_window(target, UNSEND_WINDOW_SECONDS, CHANGE_TOO_LATE_UNSEND)
    chain = _chain()
    verdict = _unsent_since(target, req.part_index)
    try:
        res = await deliver(chain, Capability.UNSEND, req.chat_guid, req.guid,
                            part_index=req.part_index,
                            log=_change_log("unsend", req.guid, req.chat_guid))
    except DeliveryError as e:
        if first_with(chain, Capability.UNSEND, req.chat_guid) is None:
            raise                            # 501 in the chain's words: no engine can unsend, none was asked
        # BlueBubbles was asked and failed. Neither its status nor its body is
        # handed on (the hop log has what may be kept of them). A 4xx: it
        # refused the request before it touched Messages. Anything else (no
        # answer, a 5xx, a 501 of its own included) may have reached Messages.
        if 400 <= e.status < 500:
            raise HTTPException(502, UNSEND_REFUSED)
        return await _after_failure("unsend", _engine_via(chain, Capability.UNSEND, req.chat_guid),
                                    req.chat_guid, req.guid, verdict, UNSEND_FAILED)
    return await _confirm_change("unsend", res.via, req.chat_guid, req.guid, verdict)


# One edit at a time, from the second reading of the row to the answer. The
# engine has a lock of its own around the tool; this one is around the whole
# step, so that the row an edit is checked against is the row the edit before
# it left behind. Kept per event loop (an asyncio.Lock cannot be shared
# between loops); the count of waiting edits is the process's.
_EDIT_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()
_EDIT_LOCKS_BY_ID: dict[int, asyncio.Lock] = {}
_EDITS_WAITING = 0


def _edit_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    try:
        lock = _EDIT_LOCKS.get(loop)
        if lock is None:
            lock = _EDIT_LOCKS[loop] = asyncio.Lock()
    except TypeError:                        # a loop that cannot be weakly referenced
        lock = _EDIT_LOCKS_BY_ID.setdefault(id(loop), asyncio.Lock())
    return lock


@contextlib.asynccontextmanager
async def _edit_turn():
    """Wait for this edit's turn and hold it. 409 CHANGE_BUSY instead of
    waiting when EDIT_QUEUE_LIMIT edits are waiting already, and after
    EDIT_QUEUE_SECONDS of waiting: an edit runs for seconds (45 at the tool's
    timeout), and a queue without an end would let requests pile up that
    drive Messages.app one after another long after their senders gave up.
    The wait and one run together stay inside the 75 seconds the app waits."""
    global _EDITS_WAITING
    if _EDITS_WAITING >= EDIT_QUEUE_LIMIT:
        raise HTTPException(409, CHANGE_BUSY)
    lock = _edit_lock()
    _EDITS_WAITING += 1
    try:
        async with asyncio.timeout(EDIT_QUEUE_SECONDS):
            await lock.acquire()
    except TimeoutError:
        raise HTTPException(409, CHANGE_BUSY)
    finally:
        _EDITS_WAITING -= 1
    try:
        yield
    finally:
        lock.release()


def _unchanged() -> dict:
    """The answer to an edit that would change nothing."""
    return {"ok": True, "via": None, "unchanged": True}


@app.post("/edit")
async def edit_message(req: EditReq):
    """Edit one of the owner's own iMessages, within Apple's fifteen minutes
    and five edits. imessage-cli only; 501 without it, and 502 with one of
    the tool's fixed phrases when it was asked and failed."""
    # First reading: a refusal is answered at once, whatever is running.
    _, _, unchanged = await asyncio.to_thread(_editable, req)
    if unchanged:
        return _unchanged()
    async with _edit_turn():
        # Second reading, now that no other edit is running: a request that
        # waited may be too late by now, may find its text already there (the
        # same edit sent twice), or may find the five edits used up.
        target, wanted, unchanged = await asyncio.to_thread(_editable, req)
        if unchanged:
            return _unchanged()
        chain = _chain()
        verdict = _edited_since(target, wanted, req.part_index)
        try:
            res = await deliver(chain, Capability.EDIT, req.chat_guid, req.guid, wanted,
                                part_index=req.part_index,
                                log=_change_log("edit", req.guid, req.chat_guid))
        except DeliveryError as e:
            if first_with(chain, Capability.EDIT, req.chat_guid) is None:
                raise                        # 501 in the chain's words: no engine can edit, none was asked
            detail = _edit_failure_detail(e)
            # Only a failure reported after the tool was started (or one the
            # engine has no phrase for) can have left an edit behind; a
            # refusal before that changed nothing.
            if detail != EDIT_FAILED and not imessage_cli_ran(detail):
                raise HTTPException(502, detail)
            return await _after_failure("edit", _engine_via(chain, Capability.EDIT, req.chat_guid),
                                        req.chat_guid, req.guid, verdict, detail)
        return await _confirm_change("edit", res.via, req.chat_guid, req.guid, verdict)


@app.websocket("/ws")
async def ws(ws: WebSocket):
    # HTTP middleware doesn't run for websockets — check the token here.
    # Accept the header too (the app stamps X-Imsg-Token on the WS upgrade via
    # its OkHttp interceptor); ?token= stays for the map page and old clients.
    # Closing before accept refuses the upgrade: uvicorn answers the handshake
    # with HTTP 403 and no WebSocket is ever opened, so the 1008 below never
    # reaches a real client (the in-process test client is what reports it).
    if IMSG_TOKEN or AUTH_LOCKED:
        supplied = (ws.headers.get("x-imsg-token")
                    or ws.query_params.get("token"))
        if not token_matches(supplied):
            await ws.close(code=1008)
            return
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.drop(ws)


# ---------- facetime ----------
#
# BlueBubbles detects incoming FaceTime calls (its "FaceTime Calling
# (Experimental)" feature) and delivers "ft-call-status-changed" events to a
# webhook pointed at /bb_event below (registered with ?token= so the normal
# auth middleware admits it). The relay forwards the ring to the phone as an
# FCM push (it also broadcasts a "facetime" frame on the WebSocket, which the
# app does not act on). When the user taps Answer, the app calls /ft_answer and
# the relay proxies BlueBubbles' answer endpoint, which answers the call on the
# Mac and mints a facetime.apple.com link for it. The link goes back to the
# app, which opens it in a browser. Expect the answer call to block 5-40s (BB
# waits for the call to connect, sleeps 4s around a Sonoma crash, then
# generates the link).
#
# The link leads to FaceTime's web lobby, where the phone waits until a
# participant already in the call lets it in. That participant is the Mac.
# BlueBubbles has logic of its own for admitting the web joiner and then
# leaving the call, but it does not work on the author's Mac (BlueBubbles
# 1.9.9 polls a notification database at a path that does not exist there;
# docs/facetime-bridge.md, "Why the Mac has to click"). So either a person
# clicks the green check in FaceTime on the Mac, or the display-specific
# auto-admit rig below does, when it is on. It is off by default.

FT_STATUS_INCOMING = "incoming"
FT_STATUS_DISCONNECTED = "disconnected"


def send_facetime_push(event: str, uuid: str, caller: str, caller_name: str,
                       is_video: bool):
    """FCM ring/cancel for a FaceTime call. Data-only, all-string values, same
    delivery rules as send_push but none of the message-specific logic (no
    archive silencing — a call should always ring)."""
    if not FCM_READY:
        return
    tokens = push_tokens()
    if not tokens:
        return
    data = {
        "kind": "facetime",
        "ft_event": event,                      # "incoming" | "ended"
        "uuid": uuid,
        "caller": caller,
        "caller_name": caller_name,
        "is_video": "1" if is_video else "0",
    }
    dead = []
    for t in tokens:
        try:
            fb_messaging.send(fb_messaging.Message(
                token=t, data=data,
                android=fb_messaging.AndroidConfig(priority="high"),
            ))
        except fb_messaging.UnregisteredError:
            dead.append(t)
        except Exception as e:
            print(f"[facetime] fcm send error: {e}")
    if dead:
        save_tokens(tokens - set(dead))
        print(f"[facetime] pruned {len(dead)} dead token(s)")


@app.post("/bb_event")
async def bb_event(request: Request):
    """Webhook receiver for BlueBubbles server events. Registered for
    ft-call-status-changed only; anything else is acknowledged and ignored.
    BB emits exactly one "incoming" per new call and a "disconnected" when it
    ends, so no dedupe is needed here."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}
    if body.get("type") != "ft-call-status-changed":
        return {"ok": True}
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return {"ok": True}
    status = data.get("status")
    uuid = str(data.get("uuid") or "")
    if not uuid or data.get("is_outgoing"):
        return {"ok": True}
    caller = str(data.get("address") or "")
    caller_name = resolve(caller)
    is_video = bool(data.get("is_video", True))
    if status == FT_STATUS_INCOMING:
        print(f"[facetime] incoming call {uuid} from {caller_name}")
        # Not on the event loop (a send that hangs stopped the whole relay),
        # and not waited for: an open app rings from the frame below.
        CALL_PUSHES.put(send_facetime_push, "incoming", uuid, caller, caller_name, is_video)
        await hub.broadcast({"type": "facetime", "data": {
            "event": "incoming", "uuid": uuid, "caller": caller,
            "caller_name": caller_name, "is_video": is_video,
        }})
    elif status == FT_STATUS_DISCONNECTED:
        print(f"[facetime] call {uuid} ended")
        CALL_PUSHES.put(send_facetime_push, "ended", uuid, caller, caller_name, is_video)
        await hub.broadcast({"type": "facetime", "data": {
            "event": "ended", "uuid": uuid,
        }})
    return {"ok": True}


#: A FaceTime call id as the routes below accept it: letters, digits, ".", "_"
#: and "-", starting with a letter or digit, at most 128 characters.
#: BlueBubbles reports calls by UUID, which fits.
_FT_CALL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _ft_call_id(uuid: str) -> str:
    """``uuid`` if it looks like a call id, else 422. The value goes into the
    path of a BlueBubbles request made with the server password, and into a
    log line: unchecked, ``uuid=../../message/text`` reached any other
    BlueBubbles POST endpoint, and a newline in it wrote a forged log line.
    (The engine escapes the path segment as well, engines/bluebubbles.py.)"""
    if not _FT_CALL_ID.fullmatch(uuid):
        raise HTTPException(422, "uuid is not a FaceTime call id")
    return uuid


@app.post("/ft_answer")
async def ft_answer(uuid: str):
    """Answers the call on the Mac and returns the web link to join it.
    Blocks until BlueBubbles has answered + generated the link (5-40s
    typically; 90s cap covers BB's internal 30s connect timeout)."""
    uuid = _ft_call_id(uuid)
    try:
        link = await _facetime().answer(uuid)
    except EngineError as e:
        print(f"[facetime] answer {uuid} failed: {_ft_error_words(e)}")
        raise HTTPException(e.status or 502, e.body if e.body is not None else e.detail)
    if not link:
        raise HTTPException(502, "BlueBubbles returned no link")
    print(f"[facetime] answered {uuid} -> link generated")
    # BB has already answered (Mac is in the call), so run the admit loop in
    # incoming mode (skips the open+Join steps).
    _launch_autoadmit(link, incoming=True)
    return {"link": link}


@app.post("/ft_decline")
async def ft_decline(uuid: str):
    """Declines/leaves the call on the Mac."""
    uuid = _ft_call_id(uuid)
    try:
        await _facetime().leave(uuid)
    except EngineError as e:
        print(f"[facetime] decline {uuid} failed: {_ft_error_words(e)}")
        raise HTTPException(e.status or 502, e.body if e.body is not None else e.detail)
    return {"ok": True}


@app.post("/ft_link")
async def ft_link():
    """Mints a fresh FaceTime link for a new call (BlueBubbles' POST
    /api/v1/facetime/session). Minting does not put the Mac into a call, and
    nothing admits a web joiner by itself: whoever opens the link waits in
    FaceTime's web lobby until a participant already in the call lets them
    in. With the auto-admit rig on, _launch_autoadmit opens the link in
    FaceTime on the Mac, joins and admits; with it off (the default) that is
    a person's job. See docs/facetime-bridge.md."""
    try:
        link = await _facetime().new_link()
    except EngineError as e:
        print(f"[facetime] link failed: {_ft_error_words(e)}")
        raise HTTPException(e.status or 502, e.body if e.body is not None else e.detail)
    if not link:
        raise HTTPException(502, "BlueBubbles returned no link")
    _launch_autoadmit(link)
    return {"link": link}


def _ft_error_words(e: EngineError) -> str:
    """A FaceTime bridge failure for the log: BlueBubbles' status and the start
    of its answer, or the bridge's own words ("BlueBubbles unreachable",
    "BlueBubbles returned an unexpected answer") plus the class of the error
    behind them (ConnectError, ReadTimeout, JSONDecodeError, ...). The client
    gets the detail without the class: 502 {"detail": "BlueBubbles unreachable"}."""
    cause = e.__cause__
    return e.detail + (f" ({type(cause).__name__})" if cause is not None else "")


def _facetime():
    """The FaceTime bridge of the first chain engine that has one (BlueBubbles);
    none -> 501 "no configured engine can handle FaceTime"."""
    engine = first_with(_chain(), Capability.FACETIME)
    if engine is None or engine.facetime is None:
        raise HTTPException(501, no_engine(Capability.FACETIME).detail)
    return engine.facetime


# The auto-admit rig: the orchestrating script ships with the relay (code,
# beside this file; its Python helpers are in facetime/), while its log and
# working files (trigger files, shots/, the lock) go under RELAY_DATA_DIR.
# FaceTimeAdmit.app is the Accessibility-granted loader the AppleScript side
# runs in; FT_ADMIT_APP names it for the doctor only (it is never rebuilt).
FT_ADMIT_SCRIPT = RELAY_DIR / "ft-autoadmit.sh"
FT_AUTO_LOG = DATA_DIR / "ft-auto.log"
FT_ADMIT_APP = Path(os.path.expanduser(os.environ.get("FT_ADMIT_APP", "").strip()
                                       or str(RELAY_DIR / "FaceTimeAdmit.app")))


def autoadmit_state() -> tuple[bool, str]:
    """``(on, why_off)`` for the auto-admit rig. Off when ``FT_AUTOADMIT=0`` or
    when the Accessibility-granted helper app is not there: the script only
    writes trigger files the app's AppleScript acts on, so without it the rig
    would just open FaceTime on the Mac and abort. A checkout without
    ``FaceTimeAdmit.app`` (every stranger's; it is git-ignored and never
    rebuilt) is therefore off by construction (plan 2026-10-06, risk 6)."""
    if os.environ.get("FT_AUTOADMIT", "1") == "0":
        return False, "FT_AUTOADMIT=0"
    if not FT_ADMIT_APP.exists():
        return False, "helper app missing"
    return True, ""


def _launch_autoadmit(link: str, incoming: bool = False):
    """Fire the Mac-side auto-admit sequence in the background. Outbound: opens
    the link in FaceTime, joins, then admits. Incoming: BB already answered so
    the Mac is in the call -> skip open+Join and go straight to admit. The admit
    loop clicks the green check when the phone requests to join, then leaves.
    Non-blocking. Runs only when ``autoadmit_state()`` says so: FT_AUTOADMIT=0
    disables it, and so does a missing FaceTimeAdmit.app (FT_ADMIT_APP)."""
    on, why = autoadmit_state()
    if not on:
        if why != "FT_AUTOADMIT=0":
            print(f"[facetime] auto-admit skipped: {why} (FT_ADMIT_APP)")
        return
    try:
        logf = open(FT_AUTO_LOG, "a")
        args = ["/bin/bash", str(FT_ADMIT_SCRIPT)]
        if incoming:
            args.append("--incoming")
        args.append(link)
        env = dict(os.environ, RELAY_DATA_DIR=str(DATA_DIR))
        subprocess.Popen(args, stdout=logf, stderr=logf, start_new_session=True, env=env)
        print(f"[facetime] auto-admit ({'incoming' if incoming else 'outbound'}) "
              f"launched for {mask_token(link)[:40]}...")
    except Exception as e:
        print(f"[facetime] auto-admit launch failed: {e}")


# ---------- doctor (relay.py --check) ----------
# One row per dependency, STATUS WORDS ONLY: no token, password, URL, path of
# a secret, phone number or name ever appears. The only paths printed are the
# interpreter (the Full Disk Access hint must name the exact binary to grant)
# and the data directory; the only address is the one the server binds
# ("listening on", IMSG_BIND and IMSG_PORT). Printed at startup under __main__
# and on demand with `relay.py --check`, which then exits 0 without starting
# the server.

DOCTOR_TIMEOUT = 3


def _http_probe(url: str, headers: dict | None = None, timeout: float = DOCTOR_TIMEOUT):
    """HTTP status of a GET, or None when nothing answered. The default
    prober; doctor_rows(probe=...) takes a stand-in so tests never dial out.
    One retry after a second: the table is printed right after a restart,
    when a single probe can miss a service that is perfectly reachable
    (seen once with Home Assistant on 2026-10-06)."""
    for attempt in (1, 2):
        try:
            with httpx.Client(timeout=timeout) as client:
                return client.get(url, headers=headers or {}).status_code
        except Exception:
            if attempt == 1:
                time.sleep(1)
    return None


def _probe_word(status) -> str:
    return "unreachable" if status is None else "reachable"


def _file_state(path) -> str:
    """'present', 'missing' or 'denied' for a path, from one os.stat that is
    not allowed to raise. Only a plain "no such file" is 'missing'. When macOS
    withholds Full Disk Access the stat itself fails with EPERM: that used to
    surface either as a traceback out of the doctor (pathlib before 3.14
    re-raises it from Path.exists()) or as a wrong NOT FOUND (3.14's
    Path.exists() answers False), and is 'denied' here."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except (OSError, ValueError):
        return "denied"
    return "present"


def _chatdb_row() -> tuple[str, str, str]:
    """('chat.db', status, hint). Opens the configured database read-only and
    reads one row from `message`; a failure on a file that is there, or that
    macOS will not even let us look at, is the Full Disk Access symptom, so
    the hint names the interpreter to grant it to."""
    try:                       # the path the adapter actually opens (IMSG_CHATDB at import)
        path = Path(chatdb_adapter._cdb().path)
    except Exception:
        path = Path(CHATDB)
    try:
        conn = db()
        try:
            conn.execute("SELECT 1 FROM message LIMIT 1").fetchall()
        finally:
            conn.close()
        return ("chat.db", "readable", "")
    except Exception as e:
        kind = type(e).__name__
        if _file_state(path) == "missing":
            return ("chat.db", "NOT FOUND",
                    "IMSG_CHATDB must name the Messages database (default ~/Library/Messages/chat.db)")
        exe = sys.executable
        real = os.path.realpath(exe)
        where = exe if real == exe else f"{exe} (resolves to {real})"
        return ("chat.db", f"NOT READABLE ({kind})",
                f"grant Full Disk Access to {where} in System Settings > Privacy & Security, "
                "then restart the relay")


#: What the doctor says for a credential that still holds an example's placeholder.
PLACEHOLDER_STATUS = "placeholder — treated as unset"


def _placeholder_hint(key: str, what: str, effect: str) -> str:
    return f"{key} still holds the example's placeholder ({effect}): set {what}, or remove the key"


def _change_row() -> tuple[str, str, str]:
    """('edit / unsend', status, hint): which engine, if any, can change a
    message after it was sent. The tool is never run for this: the binary
    was only looked for (at import), its version is read from the Homebrew
    folder its link points into, and macOS is asked whether THIS process has
    the Accessibility grant the tool needs (a question that never prompts).
    The one path this row prints is where that binary is."""
    chain = _chain()
    edit_engine = first_with(chain, Capability.EDIT, IMESSAGE_PROBE_GUID)
    unsend_engine = first_with(chain, Capability.UNSEND, IMESSAGE_PROBE_GUID)
    hints = []
    if edit_engine is not None:
        version = imessage_cli_version(IMESSAGE_CLI_BIN)
        edit = "imessage-cli found" if version is None else f"imessage-cli {version} found"
        if imessage_cli_engine.accessibility_trusted() is False:
            edit += ", Accessibility NOT GRANTED"
            hints.append("grant Accessibility to the Python that runs the relay (System Settings > Privacy & "
                         "Security) and restart it: until then /edit is refused without starting the tool "
                         "(run from Terminal, this row shows Terminal's grant, not the LaunchAgent's)")
        hints.append(f"{IMESSAGE_CLI_BIN} drives Messages.app: the Python that runs the relay needs "
                     "Accessibility and Automation (`imessage-cli authorize` shows them, and asks for a "
                     "missing one, for the program that runs it)")
        if version is not None and version != IMESSAGE_CLI_CHECKED:
            hints.append(f"the edit engine was checked with imessage-cli {IMESSAGE_CLI_CHECKED}: make one edit "
                         "of a test message with this version (`brew pin imessage-cli` keeps a version)")
    elif IMESSAGE_CLI.lower() in IMESSAGE_CLI_OFF:
        edit = "off (IMESSAGE_CLI)"
    elif IMESSAGE_CLI:
        edit = "not available (IMESSAGE_CLI names no executable file)"
        hints.append("point IMESSAGE_CLI at the imessage-cli binary, or remove the key to search for it")
    else:
        edit = "not available (imessage-cli not found)"
        hints.append("to edit sent messages: brew install beeper/tap/imessage-cli")
    if unsend_engine is not None:
        unsend = "BlueBubbles" if unsend_engine.name == "bluebubbles" else unsend_engine.name
    else:
        unsend = "not available"
        hints.append("unsend needs BlueBubbles with the Private API (BB_PASSWORD)")
    return ("edit / unsend", f"edit: {edit} | unsend: {unsend}", "; ".join(hints))


def doctor_rows(probe=_http_probe) -> list[tuple[str, str, str]]:
    """The doctor table as (component, status, hint) rows. `probe(url, headers,
    timeout) -> status|None` is the only thing that touches the network."""
    rows: list[tuple[str, str, str]] = [_chatdb_row()]

    if IMSG_TOKEN:
        rows.append(("token (IMSG_TOKEN)", "set", ""))
    else:
        placeholder = "IMSG_TOKEN" in PLACEHOLDER_KEYS
        if ALLOW_NO_TOKEN and bind_kind(BIND) == "loopback":
            # "is 1", not "=1": at startup this table goes through MaskingStream,
            # which would print a "...TOKEN=1" as "...TOKEN=***".
            hint = ("IMSG_ALLOW_NO_TOKEN is 1: running WITHOUT authentication"
                    + (" (the placeholder is ignored)" if placeholder else "")
                    + "; never expose the relay like this")
        elif ALLOW_NO_TOKEN:
            hint = ("the relay refuses to start: IMSG_ALLOW_NO_TOKEN is honoured only on a loopback "
                    "bind, so set IMSG_BIND to 127.0.0.1, or set IMSG_TOKEN")
        else:
            hint = ("the relay refuses to start: set IMSG_TOKEN to a long random string "
                    "(openssl rand -hex 32), or IMSG_ALLOW_NO_TOKEN=1 with a loopback IMSG_BIND "
                    "to run without authentication")
        rows.append(("token (IMSG_TOKEN)", "PLACEHOLDER" if placeholder else "NOT SET", hint))

    kind = bind_kind(BIND)
    if kind == "name":
        # Not echoed: a value that is not an address is whatever was typed there.
        rows.append(("listening on", f"NOT AN IP ADDRESS, port {PORT}",
                     "IMSG_BIND must be an IP address such as 127.0.0.1 (no port, no brackets); "
                     "the relay exits at start if the value cannot be bound"))
    else:
        rows.append(("listening on", f"[{BIND}]:{PORT}" if ":" in BIND else f"{BIND}:{PORT}",
                     "every network interface: set IMSG_BIND=127.0.0.1 unless the HTTPS route "
                     "reaches the relay over the LAN" if kind == "all" else ""))

    bb_word = "reachable" if _bluebubbles().ping() else "unreachable"
    if BB_PASSWORD:
        rows.append(("BlueBubbles", f"password set, server {bb_word}",
                     "" if bb_word == "reachable"
                     else "check BB_URL and that the BlueBubbles server is running"))
    elif "BB_PASSWORD" in PLACEHOLDER_KEYS:
        rows.append(("BlueBubbles", f"{PLACEHOLDER_STATUS}, server {bb_word}",
                     _placeholder_hint("BB_PASSWORD", "the BlueBubbles server password",
                                       "sends then go through AppleScript only")))
    else:
        rows.append(("BlueBubbles", f"no password (AppleScript only), server {bb_word}",
                     "set BB_PASSWORD for tapbacks, replies, new chats, group icons, names and FaceTime"))

    if beeper.enabled():
        bridge_db = Path(beeper.BRIDGE_DB).exists()
        rows.append(("Beeper (Google Messages)",
                     "token set, bridge db " + ("found" if bridge_db else "missing"),
                     "" if bridge_db else "RCS/SMS labels need BEEPER_BRIDGE_DB (mautrix-gmessages.db)"))
    elif "BEEPER_TOKEN" in PLACEHOLDER_KEYS:
        rows.append(("Beeper (Google Messages)", PLACEHOLDER_STATUS,
                     _placeholder_hint("BEEPER_TOKEN", "the Beeper Desktop API token",
                                       "the Google Messages bridge stays off")))
    else:
        rows.append(("Beeper (Google Messages)", "disabled", "set BEEPER_TOKEN to merge Google Messages threads"))

    if not FCM_CREDS:
        rows.append(("FCM push", "disabled", "set FCM_CREDS to a Firebase service-account JSON for push notifications"))
    else:
        found = Path(os.path.expanduser(FCM_CREDS)).is_file()
        status = "credentials file " + ("found" if found else "MISSING")
        hint = "" if found else "FCM_CREDS does not name a readable file"
        if firebase_admin is None:
            status += ", firebase-admin NOT installed"
            hint = "pip install firebase-admin"
        rows.append(("FCM push", status, hint))

    if not HA_TOKEN and "HA_TOKEN" in PLACEHOLDER_KEYS:
        rows.append(("Home Assistant", PLACEHOLDER_STATUS,
                     _placeholder_hint("HA_TOKEN", "a Home Assistant long-lived access token",
                                       "/locations answers 503")))
    elif not HA_TOKEN:
        rows.append(("Home Assistant", "disabled", "set HA_TOKEN and HA_LOCATIONS for the family map"))
    else:
        n = sum(1 for c in HA_LOCATIONS.split(",") if "=" in c)
        st = probe(f"{HA_URL}/api/", {"Authorization": f"Bearer {HA_TOKEN}"}, DOCTOR_TIMEOUT)
        if st is None:
            status, hint = "UNREACHABLE", "check HA_URL"
        elif st in (401, 403):
            status, hint = "reachable, token REJECTED", "HA_TOKEN is not a valid long-lived access token"
        else:
            status, hint = "reachable, token accepted", ""
        status += f", {n} location(s) configured"
        rows.append(("Home Assistant", status, hint if n else "HA_LOCATIONS is empty"))

    if not MAPKIT_TOKEN and "MAPKIT_TOKEN" in PLACEHOLDER_KEYS:
        rows.append(("MapKit", PLACEHOLDER_STATUS,
                     _placeholder_hint("MAPKIT_TOKEN", "an Apple MapKit JS token",
                                       "the /map page has no token")))
    else:
        rows.append(("MapKit", "token set" if MAPKIT_TOKEN else "not set",
                     "" if MAPKIT_TOKEN else "set MAPKIT_TOKEN to serve the /map page"))

    marian = probe(f"{MARIAN_URL}/", None, DOCTOR_TIMEOUT)
    ollama = probe(f"{OLLAMA_URL}/api/tags", None, DOCTOR_TIMEOUT)
    model = "set" if (os.environ.get("OLLAMA_MODEL") or "").strip() else "default"
    rows.append(("translation",
                 f"marian {_probe_word(marian)}, ollama {_probe_word(ollama)} (OLLAMA_MODEL {model})",
                 "" if (marian is not None or ollama is not None)
                 else "/translate will fail until MARIAN_URL or OLLAMA_URL answers"))

    names = engine_names(_chain())
    rows.append(("send engines", ", ".join(names) or "NONE",
                 "" if names else "every send will 501: set BB_PASSWORD or SEND_APPLESCRIPT_FALLBACK=1"))
    rows.append(_change_row())
    rows.append(("features advertised",
                 ", ".join(k for k, v in FEATURES.items() if v) or "(none)",
                 "FEATURE_FACETIME/MAP/TRANSLATE/VOICE override what /health reports"))

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        writable = os.access(DATA_DIR, os.W_OK)
    except Exception:
        writable = False
    rows.append(("data dir", f"{DATA_DIR} ({'writable' if writable else 'NOT writable'})",
                 "" if writable else "RELAY_DATA_DIR must be a directory the relay can write"))

    auto, why_off = autoadmit_state()
    script = FT_ADMIT_SCRIPT.is_file()
    app_found = FT_ADMIT_APP.exists()
    if not auto:
        hint = ("" if why_off == "FT_AUTOADMIT=0"
                else "off by construction: the rig needs the Accessibility-granted helper app "
                     "named by FT_ADMIT_APP (owner-specific, never rebuilt)")
    elif not script:
        hint = "ft-autoadmit.sh is missing beside relay.py"
    else:
        hint = "the auto-admit rig is display-specific; FT_AUTOADMIT=0 turns it off"
    rows.append(("FaceTime auto-admit",
                 f"{'on' if auto else 'off (' + why_off + ')'}, script {'found' if script else 'MISSING'}, "
                 f"helper app {'found' if app_found else 'missing'}",
                 hint))
    return rows


def doctor_table(rows=None) -> str:
    rows = doctor_rows() if rows is None else rows
    w1 = max(len(r[0]) for r in rows)
    w2 = max(len(r[1]) for r in rows)
    lines = ["[check] relay doctor",
             f"  {'component'.ljust(w1)}  {'status'.ljust(w2)}  hint",
             f"  {'-' * w1}  {'-' * w2}  {'-' * 4}"]
    for name, status, hint in rows:
        lines.append(f"  {name.ljust(w1)}  {status.ljust(w2)}  {hint}".rstrip())
    return "\n".join(lines)


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        print(doctor_table())
        sys.exit(0)
    _refusal = startup_refusal()
    if _refusal:
        print(_refusal, file=sys.stderr, flush=True)
        sys.exit(EX_CONFIG)
    # Line-buffered from here on, twice over: the raw streams are switched to
    # line buffering where Python allows it, and MaskingStream flushes after
    # every line it writes. Under launchd stdout is a file, and without this
    # the doctor table below stayed in the buffer until the first request.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass
    sys.stdout = MaskingStream(sys.stdout)
    sys.stderr = MaskingStream(sys.stderr)
    print(doctor_table(), flush=True)
    import uvicorn
    uvicorn.run(app, host=BIND, port=PORT, log_config=masked_log_config())
