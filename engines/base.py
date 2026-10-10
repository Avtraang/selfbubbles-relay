"""Send-engine interface (selfbubbles plan 2026-10-06, section 4).

A *send engine* is one way of getting a message out of this Mac: the
BlueBubbles Private API, Messages.app over AppleScript, or Beeper Desktop
for Google Messages.  The relay keeps an ordered *chain* of them
(``engines.chain``) and hands each outbound operation to the first engine that
``handles`` the chat and has the matching ``Capability``.

Every engine returns a :class:`SendResult` (or, for ``create_chat`` and
``chat_icon``, the raw value the plan names; the chain wraps it) and never
raises for an *expected* failure such as an upstream HTTP error -- that is an
``ok=False`` result with a human ``detail`` so the chain can fall through to
the next engine and join the details into one error.  :class:`EngineError` is
for the synchronous helpers (``contacts``, the FaceTime bridge) and for
transport failures the chain turns into a failed result.  :class:`Unsupported`
is raised by a method the engine does not implement; the chain never calls
one, because it checks ``capabilities`` first, but a direct caller gets a
clear error instead of an ``AttributeError``.  (``unsend`` and ``edit`` came
later, step R6; every engine has both methods, and all but the one engine
that holds the capability raise ``Unsupported`` from them, so each engine
still satisfies the runtime-checkable ``SendEngine`` protocol.)

Nothing in this package logs a token, a password or a message body.  The two
lines it does log on a failure -- the chain's hop log and the AppleScript
engine's ``[fallback]`` line -- name the engine, the chat guid the message was
delivered to, and an error without its payload; see ``chain._log_detail`` and
``applescript._failure_words``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Capability(str, Enum):
    """What an engine can do.  ``REPLY`` and ``EFFECT`` are advisory: a reply
    or a send effect through an engine without them lands as a plain message
    (today's AppleScript fallback behaviour), so the chain does not require
    them."""

    TEXT = "text"
    ATTACHMENT = "attachment"
    REPLY = "reply"
    REACT = "react"
    CREATE_CHAT = "create_chat"
    CHAT_ICON = "chat_icon"
    CONTACTS = "contacts"
    FACETIME = "facetime"
    # Step R6: changing a message the owner already sent.  Two different
    # engines hold these on the Mac they were measured on (macOS 27.0):
    # BlueBubbles can unsend but its edit call does nothing there, and
    # imessage-cli can edit but its undo-send reports success without doing
    # anything.  No engine advertises a capability it was not seen to perform.
    UNSEND = "unsend"
    EDIT = "edit"
    # Send effects (Slam, Confetti, ...; ``EFFECTS`` below), 2026-10-10.  Only
    # the Private API applies one, so ``/health.capabilities`` names ``effect``
    # exactly where BlueBubbles is in the chain for iMessage chats, and the app
    # offers the picker there and nowhere else (the ``edit`` and ``send_id``
    # precedent: a client relies on a thing only where it is named).  Advisory
    # like ``REPLY``: ``/send`` asks the chain for ``TEXT``, and a fallback
    # sends the text plain and says so (``effect_sent``).
    EFFECT = "effect"


#: Human phrase for the 501 the chain raises when nothing can do ``cap``.
CAPABILITY_VERB: dict[Capability, str] = {
    Capability.TEXT: "send text",
    Capability.ATTACHMENT: "send attachments",
    Capability.REPLY: "reply",
    Capability.REACT: "react",
    Capability.CREATE_CHAT: "create a chat",
    Capability.CHAT_ICON: "fetch chat icons",
    Capability.CONTACTS: "list contacts",
    Capability.FACETIME: "handle FaceTime",
    Capability.UNSEND: "unsend messages",
    Capability.EDIT: "edit messages",
    Capability.EFFECT: "send effects",
}

#: Beeper (Google Messages) chat guids carry this prefix (``beeper.PREFIX``);
#: every other guid is a chat.db one (``iMessage;-;...``, ``SMS;-;...``,
#: ``any;-;...``, ``chatNNN``).  Spelled out here so the iMessage engines do
#: not import ``beeper``.
BEEPER_PREFIX = "bp:"


def is_beeper_guid(chat_guid: str) -> bool:
    return chat_guid.startswith(BEEPER_PREFIX)


#: Apple's iMessage send effects, by the id Messages writes to
#: ``message.expressive_send_style_id`` and BlueBubbles takes as ``effectId``,
#: with the name iOS shows for each.  The relay serves these ids as a
#: message's ``effect`` and accepts one of them on ``/send``; this dictionary
#: is the ONE list both are checked against, and nothing else from the client
#: reaches BlueBubbles as a protocol identifier.  The four ``MobileSMS``
#: ids are bubble effects, the ``CK...Effect`` ids full-screen ones.
EFFECTS: dict[str, str] = {
    "com.apple.MobileSMS.expressivesend.impact": "Slam",
    "com.apple.MobileSMS.expressivesend.loud": "Loud",
    "com.apple.MobileSMS.expressivesend.gentle": "Gentle",
    "com.apple.MobileSMS.expressivesend.invisibleink": "Invisible Ink",
    "com.apple.messages.effect.CKEchoEffect": "Echo",
    "com.apple.messages.effect.CKSpotlightEffect": "Spotlight",
    "com.apple.messages.effect.CKHappyBirthdayEffect": "Balloons",
    "com.apple.messages.effect.CKConfettiEffect": "Confetti",
    "com.apple.messages.effect.CKHeartEffect": "Love",
    "com.apple.messages.effect.CKLasersEffect": "Lasers",
    "com.apple.messages.effect.CKFireworksEffect": "Fireworks",
    "com.apple.messages.effect.CKShootingStarEffect": "Shooting Star",
    "com.apple.messages.effect.CKSparklesEffect": "Celebration",
}

#: The 422 details ``/send`` answers for an effect it will not pass on.
UNKNOWN_EFFECT = "unknown effect id"
EFFECT_NOT_FOR_GOOGLE_MESSAGES = "a Google Messages chat cannot carry an iMessage effect"


def effect_problem(chat_guid: str, effect: str) -> str | None:
    """Why ``effect`` cannot go out in ``chat_guid``, or ``None`` when it can:
    the id is not one of ``EFFECTS`` (anything else, the empty string
    included, is refused: the value is forwarded to BlueBubbles as given), or
    the chat is a Google Messages one, which has no effects at all."""
    if effect not in EFFECTS:
        return UNKNOWN_EFFECT
    if is_beeper_guid(chat_guid):
        return EFFECT_NOT_FOR_GOOGLE_MESSAGES
    return None


@dataclass(frozen=True)
class SendResult:
    """Outcome of one engine call.

    ``via`` is the app-facing engine label (``"bb"``, ``"applescript"``,
    ``"gmessages"``) -- frozen, because ``ChatVM.noteSendPath`` keys on it.
    ``detail`` is the human failure message the chain joins into its 502
    (``"BlueBubbles failed (HTTP 500: ...)"``).  ``payload`` is engine-specific
    success data (BlueBubbles' JSON answer, a new chat guid, icon bytes).
    ``status`` and ``body`` carry an upstream HTTP error so that, when this
    engine was the only one tried, the chain can pass the status through
    unchanged (today's ``/react`` and ``/create_chat`` behaviour).
    """

    ok: bool
    via: str
    detail: str = ""
    payload: Any = None
    status: int | None = None
    body: str | None = None
    #: The engine was handed the whole request and gave no answer: it may have
    #: delivered. The chain stops there instead of sending again through the
    #: next engine.
    uncertain: bool = False


class EngineError(Exception):
    """An engine could not complete the call.

    ``detail`` is the human message; ``status``/``body`` are the upstream HTTP
    status and response text when there was one.
    """

    def __init__(self, detail: str, status: int | None = None, body: str | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.body = body


class Unsupported(EngineError):
    """The engine does not implement this operation (not in its capabilities)."""

    def __init__(self, engine: str, cap: Capability):
        super().__init__(f"{engine} cannot {CAPABILITY_VERB[cap]}")
        self.engine = engine
        self.cap = cap


@runtime_checkable
class FaceTimeBridge(Protocol):
    """FaceTime control an engine may expose (``SendEngine.facetime``)."""

    async def answer(self, uuid: str) -> str | None:
        """Answer the incoming call ``uuid``; the web link to join it, or ``None``."""

    async def leave(self, uuid: str) -> None:
        """Decline / leave call ``uuid``."""

    async def new_link(self) -> str | None:
        """Mint a fresh outbound FaceTime link, or ``None``."""


@runtime_checkable
class SendEngine(Protocol):
    """The interface every chain member implements.

    ``name`` is the registry key (``SEND_ENGINES=`` and ``/health.engines``);
    ``via`` the frozen app-facing label put in responses.  ``configured()``
    says whether the engine has what it needs (a password, a token);
    ``build_chain`` only includes configured engines.  ``ping()`` is a cheap
    liveness probe for ``/health``.  ``handles(chat_guid)`` says whether the
    engine routes that chat at all (Beeper: ``bp:`` guids; the iMessage
    engines: everything else).
    """

    name: str
    via: str
    capabilities: frozenset[Capability]
    facetime: FaceTimeBridge | None

    def configured(self) -> bool: ...

    def ping(self) -> bool: ...

    def handles(self, chat_guid: str) -> bool: ...

    async def send_text(self, chat_guid: str, text: str,
                        reply_to_guid: str | None = None,
                        effect: str | None = None) -> SendResult:
        """``effect`` is one of ``EFFECTS`` (the relay checks it first) or
        ``None``.  Like ``REPLY`` it is advisory: an engine that cannot do
        effects sends the text plain, and the relay tells the app which
        happened (``effect_sent``) from the ``via`` of the result."""
        ...

    async def send_attachment(self, chat_guid: str, name: str, content: bytes,
                              content_type: str) -> SendResult: ...

    async def react(self, chat_guid: str, message_guid: str, reaction: str) -> SendResult: ...

    async def unsend(self, chat_guid: str, message_guid: str,
                     part_index: int = 0) -> SendResult:
        """Retract ("Undo Send") part ``part_index`` of the owner's own message.
        ``ok`` means the engine's upstream accepted the request, not that the
        message is gone: the relay confirms every change in ``chat.db``."""
        ...

    async def edit(self, chat_guid: str, message_guid: str, text: str,
                   part_index: int = 0) -> SendResult:
        """Replace the text of part ``part_index`` of the owner's own message.
        ``ok`` is the engine's word only, as for ``unsend``."""
        ...

    async def create_chat(self, addresses: list[str], text: str) -> str | None: ...

    async def chat_icon(self, chat_guid: str) -> bytes | None: ...

    def contacts(self) -> list[dict]: ...
