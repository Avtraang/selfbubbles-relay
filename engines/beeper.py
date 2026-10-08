"""Beeper (Google Messages) as a chain member.

A thin adapter over the relay's ``beeper`` module (left untouched): it owns
``bp:`` chat guids, can send text and replies, and nothing else.  Attachments
are *not* a capability, so ``/send_attachment`` into a Google Messages thread
gets the chain's honest 501 instead of a BlueBubbles error (KB item 16).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import beeper as _bridge   # the top-level beeper.py (Beeper Desktop local API)

from engines.base import Capability, SendResult, Unsupported

SendFn = Callable[[str, str, str | None], Awaitable[bool]]

#: Frozen: today's ``/send`` 502 detail for a Google Messages failure.
FAILED = "Google Messages send failed"


class BeeperEngine:
    name = "beeper"
    via = "gmessages"
    capabilities = frozenset({Capability.TEXT, Capability.REPLY})
    facetime = None

    def __init__(self, token: str, send: SendFn | None = None):
        self._token = token
        self._send = send

    def configured(self) -> bool:
        return bool(self._token)

    def ping(self) -> bool:
        return _bridge.enabled()

    def handles(self, chat_guid: str) -> bool:
        return _bridge.is_beeper_guid(chat_guid)

    async def send_text(self, chat_guid: str, text: str,
                        reply_to_guid: str | None = None) -> SendResult:
        # ``beeper.send`` is looked up at call time so a test can patch it.
        send = self._send if self._send is not None else _bridge.send
        ok = await send(chat_guid, text, reply_to_guid)
        return SendResult(True, self.via) if ok else SendResult(False, self.via, FAILED)

    async def send_attachment(self, chat_guid: str, name: str, content: bytes,
                              content_type: str) -> SendResult:
        raise Unsupported(self.name, Capability.ATTACHMENT)

    async def react(self, chat_guid: str, message_guid: str, reaction: str) -> SendResult:
        raise Unsupported(self.name, Capability.REACT)

    # Google Messages chats can do neither (step R6).
    async def unsend(self, chat_guid: str, message_guid: str, part_index: int = 0) -> SendResult:
        raise Unsupported(self.name, Capability.UNSEND)

    async def edit(self, chat_guid: str, message_guid: str, text: str,
                   part_index: int = 0) -> SendResult:
        raise Unsupported(self.name, Capability.EDIT)

    async def create_chat(self, addresses: list[str], text: str) -> str | None:
        raise Unsupported(self.name, Capability.CREATE_CHAT)

    async def chat_icon(self, chat_guid: str) -> bytes | None:
        raise Unsupported(self.name, Capability.CHAT_ICON)

    def contacts(self) -> list[dict]:
        raise Unsupported(self.name, Capability.CONTACTS)
