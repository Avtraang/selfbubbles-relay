"""BlueBubbles send engine: the Private API over the BlueBubbles server's HTTP API.

The twelve ``httpx`` call sites the relay used to carry are here, verbatim:
same paths, payloads, ``tempGuid`` prefixes and timeouts.  The password only
ever travels as the ``password`` query parameter BlueBubbles requires; it is
never logged, and no error message includes it.  ``unsend`` (step R6) is the
thirteenth call; ``edit`` is deliberately not a capability (see the class).

``transport=`` injects an ``httpx`` transport (``httpx.MockTransport`` in
tests).  ``httpx.AsyncClient`` / ``httpx.Client`` are looked up on the module
at call time, so patching them also works.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

from engines.base import Capability, EngineError, SendResult, Unsupported, is_beeper_guid

#: The failure detail is frozen (the relay's 502 joins it:
#: "BlueBubbles failed (HTTP 500: ...); AppleScript fallback failed").
def _failed(err: str) -> str:
    return f"BlueBubbles failed ({err})"


#: Failures that happen before BlueBubbles can have received the request.
_NOT_REACHED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                httpx.UnsupportedProtocol, httpx.InvalidURL)


def _no_answer(e: Exception, via: str) -> SendResult:
    """A send that raised instead of answering. Unless it failed before the
    request could arrive, BlueBubbles may hold the message and still pass it
    to Messages: the outcome is uncertain, and nobody may send it again."""
    return SendResult(False, via, _failed(str(e)), uncertain=not isinstance(e, _NOT_REACHED))


#: Frozen: the detail of the 502 the relay's ``/ft_*`` routes answer when the
#: BlueBubbles server could not be reached at all (refused, timed out, ...).
UNREACHABLE = "BlueBubbles unreachable"

#: The detail of the 502 for a 2xx answer that is not the JSON BlueBubbles'
#: FaceTime endpoints return (another service on that port, an HTML page).
UNEXPECTED = "BlueBubbles returned an unexpected answer"

#: The detail of the 400 for a call id that cannot be one path segment.
BAD_CALL_ID = "invalid FaceTime call id"

#: The same for a message guid (``unsend``).
BAD_MESSAGE_GUID = "invalid message guid"

#: The same for a chat guid (``mark_read``).
BAD_CHAT_GUID = "invalid chat guid"


def _path_segment(value: str, refusal: str) -> str:
    """``value`` as exactly ONE URL path segment.

    The value comes from the client, and the path it goes into is requested
    with the server password.  Put in unescaped, ``../../message/text`` turned
    "leave this call" into a POST to any other BlueBubbles endpoint (``httpx``
    resolves dot segments before it sends).  Every reserved character is
    percent-encoded here, and a value that is empty or nothing but dots (``.``
    and ``..`` are path navigation, never an identifier) is refused with an
    ``EngineError`` carrying ``refusal`` and status 400.  A real identifier
    (letters, digits, hyphens) comes out exactly as it went in."""
    segment = quote(str(value), safe="")
    if not segment.strip("."):
        raise EngineError(refusal, 400)
    return segment


#: ``password=<value>`` as a quoted request URL carries it: the value up to
#: the next delimiter, in whatever encoding it was written.
_PASSWORD_PARAM = re.compile(r"""(?i)(password=)[^&\s"'<>#;,)\]}]*""")

#: An identifier shorter than this is not searched for in an upstream answer:
#: replacing two or three letters wherever they occur would shred the text.
_MIN_SCRUBBED_ID = 8


def _spellings(value: str) -> set[str]:
    """``value`` in every form a URL someone quotes back can carry it: as
    given, percent-encoded (``quote`` with nothing left safe), form-encoded
    (``quote_plus``: a space is ``+``), the way ``httpx`` writes a query
    value, and each of those with lower-case hex digits."""
    forms = {value, quote(value, safe=""), quote_plus(value)}
    try:
        forms.add(str(httpx.QueryParams({"v": value}))[len("v="):])
    except Exception:
        pass
    forms |= {re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), form) for form in forms}
    forms.discard("")
    return forms


def _call_segment(uuid: str) -> str:
    """A FaceTime call id as one path segment (``_path_segment``); the routes
    answer the ``EngineError`` for an id that is empty or only dots as 400."""
    return _path_segment(uuid, BAD_CALL_ID)


class BlueBubblesFaceTime:
    """``FaceTimeBridge`` over the BlueBubbles FaceTime endpoints."""

    def __init__(self, engine: "BlueBubblesEngine"):
        self._e = engine

    async def _call(self, path: str, timeout: Any) -> Any:
        """POST one FaceTime endpoint.  An HTTP error answer is an
        ``EngineError`` carrying BlueBubbles' status and body (the relay passes
        the status through).  A transport failure -- connection refused, DNS,
        a timeout -- used to escape as the raw ``httpx`` exception and reach
        the client as a bare 500; it is an ``EngineError`` too now, with the
        fixed detail ``UNREACHABLE`` and no status, which the routes answer as
        502.  The exception text is not passed on (its class is kept as
        ``__cause__`` for the relay's log line).  Every exception counts, not
        only ``httpx``'s own: a ``BB_URL`` with a port above 65535 fails
        inside the event loop with an ``ExceptionGroup`` around an
        ``OverflowError``, and getting no answer is all this wrapper reports."""
        try:
            r = await self._e._post(path, timeout=timeout)
        except Exception as e:
            raise EngineError(UNREACHABLE) from e
        if r.status_code >= 400:
            raise EngineError(f"HTTP {r.status_code}: {r.text[:200]}", r.status_code, r.text)
        return r

    @staticmethod
    def _link(r: Any) -> str | None:
        """``data.link`` of a 2xx answer, or ``None`` when the JSON carries
        none (the routes answer that as ``502 BlueBubbles returned no link``).
        A body that is not JSON, or JSON of another shape, used to escape as
        ``JSONDecodeError`` / ``AttributeError`` and reach the client as a
        bare 500 (seen with another service answering on BlueBubbles' port);
        it is an ``EngineError`` with the detail ``UNEXPECTED`` now, which the
        routes answer as 502."""
        try:
            link = ((r.json() or {}).get("data") or {}).get("link")
        except (ValueError, AttributeError, TypeError) as e:
            raise EngineError(UNEXPECTED) from e
        if link is not None and not isinstance(link, str):
            raise EngineError(UNEXPECTED)
        return link

    async def answer(self, uuid: str) -> str | None:
        """Answers the call on the Mac and returns the web link to join it.
        Blocks until BlueBubbles has answered + generated the link (5-40s
        typically; 90s cap covers BB's internal 30s connect timeout)."""
        r = await self._call(f"/api/v1/facetime/answer/{_call_segment(uuid)}", timeout=90)
        return self._link(r)

    async def leave(self, uuid: str) -> None:
        await self._call(f"/api/v1/facetime/leave/{_call_segment(uuid)}", timeout=30)

    async def new_link(self) -> str | None:
        r = await self._call("/api/v1/facetime/session", timeout=90)
        return self._link(r)


class BlueBubblesEngine:
    name = "bluebubbles"
    via = "bb"
    # Everything, FaceTime and unsend included, EXCEPT edit: on macOS 27 the
    # server's edit endpoint (1.9.9) answers 200 and changes nothing, because
    # the Messages selectors it calls were renamed.  Unsend was verified there.
    capabilities = frozenset(Capability) - {Capability.EDIT}

    def __init__(self, url: str, password: str, transport: Any = None):
        self.url = url.rstrip("/")
        self._password = password
        self._transport = transport
        self.facetime = BlueBubblesFaceTime(self)

    # -- transport ------------------------------------------------------------
    def _client_kw(self, timeout: Any) -> dict:
        # trust_env off: BlueBubbles is a local service, and a system or
        # environment proxy must never be handed its password and the messages.
        kw: dict = {"timeout": timeout, "trust_env": False}
        if self._transport is not None:
            kw["transport"] = self._transport
        return kw

    def _params(self) -> dict:
        return {"password": self._password}

    async def _post(self, path: str, *, timeout: Any, **kw: Any) -> Any:
        async with httpx.AsyncClient(**self._client_kw(timeout)) as client:
            return await client.post(f"{self.url}{path}", params=self._params(), **kw)

    # -- SendEngine -------------------------------------------------------------
    def configured(self) -> bool:
        return bool(self._password)

    def ping(self) -> bool:
        try:
            with httpx.Client(**self._client_kw(3)) as client:
                r = client.get(f"{self.url}/api/v1/ping", params=self._params())
            return r.status_code == 200
        except Exception:
            return False

    def handles(self, chat_guid: str) -> bool:
        return not is_beeper_guid(chat_guid)

    async def send_text(self, chat_guid: str, text: str,
                        reply_to_guid: str | None = None) -> SendResult:
        payload = {
            "chatGuid": chat_guid, "message": text,
            "method": "private-api", "tempGuid": f"relay-{int(time.time()*1000)}",
        }
        if reply_to_guid:
            payload["selectedMessageGuid"] = reply_to_guid
            payload["partIndex"] = 0
        try:
            r = await self._post("/api/v1/message/text", timeout=15, json=payload)
        except Exception as e:
            return _no_answer(e, self.via)
        if r.status_code < 400:
            # A 2xx means BlueBubbles accepted the message; a body that is not
            # JSON must not read as a failure, or the chain would fall through
            # to AppleScript and send the text a second time.
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            return SendResult(True, self.via, payload=payload)
        return SendResult(False, self.via, _failed(f"HTTP {r.status_code}: {r.text[:200]}"),
                          status=r.status_code, body=r.text)

    async def send_attachment(self, chat_guid: str, name: str, content: bytes,
                              content_type: str) -> SendResult:
        data = {
            "chatGuid": chat_guid,
            "tempGuid": f"relay-att-{int(time.time() * 1000)}",
            "name": name,
            "method": "private-api",
        }
        files = {"attachment": (name, content, content_type or "application/octet-stream")}
        try:
            r = await self._post("/api/v1/message/attachment",
                                 timeout=httpx.Timeout(300.0, connect=10.0),
                                 data=data, files=files)
        except Exception as e:
            return _no_answer(e, self.via)
        if r.status_code < 400:
            return SendResult(True, self.via)
        return SendResult(False, self.via, _failed(f"HTTP {r.status_code}: {r.text[:200]}"),
                          status=r.status_code, body=r.text)

    async def react(self, chat_guid: str, message_guid: str, reaction: str) -> SendResult:
        payload = {
            "chatGuid": chat_guid,
            "selectedMessageGuid": message_guid,
            "reaction": reaction,
            "partIndex": 0,
        }
        try:
            r = await self._post("/api/v1/message/react", timeout=15, json=payload)
        except Exception as e:
            return SendResult(False, self.via, _failed(str(e)))
        if r.status_code < 400:
            return SendResult(True, self.via)
        return SendResult(False, self.via, _failed(f"HTTP {r.status_code}: {r.text[:200]}"),
                          status=r.status_code, body=r.text)

    def _scrub(self, text: str, *identifiers: str) -> str:
        """``text`` without the server password, the server address or the
        ``identifiers`` (the message guid of an unsend), should an upstream
        answer ever quote the request URL.  The password is searched for in
        every spelling a URL can give it (``_spellings``: on the wire it is
        percent-encoded, so a password with a space, ``/`` or ``&`` in it does
        not appear as typed), and whatever follows ``password=`` is blanked
        as well, up to the next delimiter."""
        secrets = _spellings(self._password) if self._password else set()
        for identifier in identifiers:
            if identifier and len(identifier) >= _MIN_SCRUBBED_ID:
                secrets |= _spellings(identifier)
        if self.url:
            secrets.add(self.url)
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return _PASSWORD_PARAM.sub(r"\1***", text)

    async def unsend(self, chat_guid: str, message_guid: str,
                     part_index: int = 0) -> SendResult:
        """"Undo Send" for part ``part_index`` of the owner's message
        ``message_guid``: ``POST /api/v1/message/<guid>/unsend`` with
        ``{"partIndex": n}`` (BlueBubbles finds the chat itself; ``chat_guid``
        is not sent).  Verified on macOS 27.0 with server 1.9.9, which answers
        200 "Message unsent!" and 400 for a guid it does not know.

        The guid goes into the path as ONE percent-encoded segment and a guid
        that is empty or only dots is refused (``EngineError``, 400) before
        anything is sent.  Results follow ``react()``; what differs is that no
        failure detail can carry the request URL or the password: a transport
        failure is reported by its class name alone, and an upstream error
        body is passed on with both removed, and the message guid with them
        (``_scrub``).  ``ok`` is BlueBubbles' word that it asked Messages; the
        relay still confirms the change in ``chat.db``."""
        segment = _path_segment(message_guid, BAD_MESSAGE_GUID)
        try:
            r = await self._post(f"/api/v1/message/{segment}/unsend", timeout=15,
                                 json={"partIndex": part_index})
        except Exception as e:
            return SendResult(False, self.via, _failed(type(e).__name__))
        if r.status_code < 400:
            return SendResult(True, self.via)
        body = self._scrub(r.text, message_guid)
        return SendResult(False, self.via, _failed(f"HTTP {r.status_code}: {body[:200]}"),
                          status=r.status_code, body=body)

    async def mark_read(self, chat_guid: str) -> SendResult:
        """Messages on the Mac marks the chat read: ``POST /api/v1/chat/<guid>/read``
        (the server's ``markChatRead``, Private API).  The chat's unread
        state goes there, and with Messages in iCloud on the owner's other
        Apple devices too.  Marking a chat that is read already changes
        nothing.  The guid goes into the path as ONE percent-encoded segment
        and an empty one is refused (``EngineError``) before anything is
        sent; failures are results, worded as ``unsend()``'s (class name
        alone for a transport failure, the body scrubbed otherwise)."""
        segment = _path_segment(chat_guid, BAD_CHAT_GUID)
        try:
            r = await self._post(f"/api/v1/chat/{segment}/read", timeout=10)
        except Exception as e:
            return SendResult(False, self.via, _failed(type(e).__name__))
        if r.status_code < 400:
            return SendResult(True, self.via)
        body = self._scrub(r.text, chat_guid)
        return SendResult(False, self.via, _failed(f"HTTP {r.status_code}: {body[:200]}"),
                          status=r.status_code, body=body)

    async def edit(self, chat_guid: str, message_guid: str, text: str,
                   part_index: int = 0) -> SendResult:
        raise Unsupported(self.name, Capability.EDIT)

    async def create_chat(self, addresses: list[str], text: str) -> str | None:
        """Genuinely new recipient set -> BlueBubbles' chat-creation endpoint
        (the Private API handles group creation).  The new chat's guid."""
        payload = {"addresses": addresses, "message": text,
                   "method": "private-api", "service": "iMessage",
                   "tempGuid": f"relay-new-{int(time.time() * 1000)}"}
        try:
            r = await self._post("/api/v1/chat/new", timeout=30, json=payload)
        except Exception as e:
            raise EngineError(_failed(str(e))) from e
        if r.status_code >= 400:
            raise EngineError(_failed(f"HTTP {r.status_code}: {r.text[:300]}"),
                              r.status_code, r.text)
        data = (r.json() or {}).get("data") or {}
        return data.get("guid")

    async def chat_icon(self, chat_guid: str) -> bytes | None:
        """The group photo as BlueBubbles serves it (octet-stream; the caller
        sniffs the type), or ``None`` when the chat has none.  The guid goes
        into the path as one percent-encoded segment; a "guid" that is empty
        or only dots is path navigation, not a chat, and has no icon."""
        segment = quote(chat_guid, safe="")
        if not segment.strip("."):
            return None
        url = f"{self.url}/api/v1/chat/{segment}/icon"
        try:
            async with httpx.AsyncClient(**self._client_kw(30)) as client:
                r = await client.get(url, params=self._params())
        except Exception as e:
            raise EngineError(f"BlueBubbles unreachable: {e}") from e
        if r.status_code >= 400 or not r.content:
            return None
        return r.content

    def contacts(self) -> list[dict]:
        """Raw BlueBubbles contact records (the relay builds its address map)."""
        try:
            with httpx.Client(**self._client_kw(20)) as client:
                r = client.get(f"{self.url}/api/v1/contact", params=self._params())
        except Exception as e:
            raise EngineError(f"request error: {e}") from e
        if r.status_code != 200:
            raise EngineError(f"BlueBubbles returned HTTP {r.status_code}: {r.text[:200]}",
                              r.status_code, r.text)
        return r.json().get("data", [])
