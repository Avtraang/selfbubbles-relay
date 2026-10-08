"""The engine chain: which engines exist, in what order, and how one operation
walks them (selfbubbles plan 2026-10-06, section 4).

``build_chain(env)`` reads only the keys it names from the mapping it is
given (``os.environ``, or the relay's own already-read values):

* ``BEEPER_TOKEN`` set  -> ``beeper`` (``bp:`` guids; text + reply)
* ``BB_PASSWORD`` set   -> ``bluebubbles`` (everything else; every capability
  but ``EDIT``)
* ``SEND_APPLESCRIPT_FALLBACK`` not ``0`` -> ``applescript`` (text + files)

(a value that is still a shipped placeholder such as ``change-me`` or
``CHANGE-ME`` counts as not set, see ``placeholders.py``: an example file
copied verbatim never puts a dead engine in the chain)

in that order, or in the order ``SEND_ENGINES=`` names (comma-separated engine
names; an engine named there but not configured is left out, an unknown name
is a ``ValueError``).  No BlueBubbles password therefore means the chain is
``[applescript]`` and texts and files still go out -- the guard-order fix "by
construction": nothing raises ``500 BB_PASSWORD not set`` any more.

A fourth engine does not come from ``env``: ``imessage-cli`` (``EDIT`` only,
step R6) joins the chain, after the others, when the caller hands one in
(``build_chain(env, imessage_cli=...)``) and its binary was found.  The relay
does: it knows where the tool may keep its state, and it found the binary
(``IMESSAGE_CLI``, else Homebrew's two places and the PATH).  A caller that
passes none gets the chain it always got, on every machine, whether or not
the tool happens to be installed there.  ``SEND_ENGINES`` may name it, which
decides where it stands; a list that does not name it still gets it, at the
end.  That list was written to order the engines that SEND, and this one
sends nothing (no existing delivery can reach it), so a list pinned before
the engine existed must not switch editing off without saying so.  Its own
switch is ``IMESSAGE_CLI=0``.

``deliver(chain, cap, chat_guid, ...)`` tries the engines that ``handles`` the
chat and have ``cap``, first ``ok`` wins.  None eligible -> ``DeliveryError``
501 "no configured engine can <op> in this chat"; all failed -> 502 with the
engines' details joined by "; " -- except that a single engine's upstream HTTP
error is passed through with its own status, which is what ``/react`` and
``/create_chat`` did before the chain existed.

What ``deliver`` logs about a failed hop is NOT the detail it answers with: an
upstream HTTP error is logged without the ``data`` member of its JSON body
(``_log_detail``), because that is where BlueBubbles returns the message that
failed to send, text included.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from placeholders import drop_placeholder

from engines.applescript import AppleScriptEngine
from engines.base import (CAPABILITY_VERB, Capability, EngineError, SendEngine,
                          SendResult, Unsupported)
from engines.beeper import BeeperEngine
from engines.bluebubbles import BlueBubblesEngine
from engines.imessage_cli import ImessageCliEngine

#: Default order.  ``SEND_ENGINES=`` may list any subset in any order.
DEFAULT_ORDER = ("beeper", "bluebubbles", "applescript", "imessage-cli")

_METHOD: dict[Capability, str] = {
    Capability.TEXT: "send_text",
    Capability.ATTACHMENT: "send_attachment",
    Capability.REACT: "react",
    Capability.CREATE_CHAT: "create_chat",
    Capability.CHAT_ICON: "chat_icon",
    Capability.UNSEND: "unsend",
    Capability.EDIT: "edit",
}

#: What ``/health.capabilities`` may name: what a chain can do in an iMessage
#: chat beyond plain sending.  The values are the ``Capability`` values.
IMESSAGE_EXTRAS = (Capability.REACT, Capability.REPLY, Capability.CREATE_CHAT,
                   Capability.UNSEND, Capability.EDIT)

#: A guid no engine mistakes for a Google Messages one: stands for "an
#: iMessage chat" when a question is about the chain, not about one chat.
IMESSAGE_PROBE_GUID = "iMessage;-;"


#: The 502 for a send whose engine took the request and gave no answer.
MAYBE_SENT = ("the send engine gave no answer: the message may have been sent. "
              "Check the chat before sending again.")


class DeliveryError(Exception):
    """``deliver`` could not get the operation done; ``status`` is the HTTP
    status the relay answers with (501 nothing capable, 502 all failed, or an
    upstream status passed through) and ``detail`` the message."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _flag(env: Mapping[str, str], key: str, default: str) -> str:
    return (env.get(key) or default).strip()


def build_chain(env: Mapping[str, str], *,
                applescript: AppleScriptEngine | None = None,
                bluebubbles_transport: Any = None,
                imessage_cli: ImessageCliEngine | None = None) -> list[SendEngine]:
    """The ordered engine chain for ``env`` (see the module docstring).

    ``applescript`` lets the relay pass an engine wired to its own runner /
    outbox hooks; ``bluebubbles_transport`` is for tests.  ``imessage_cli`` is
    the edit engine: in the chain when one is passed and it has a binary,
    whether or not ``SEND_ENGINES`` names it, and never otherwise.
    """
    available: dict[str, SendEngine] = {
        "beeper": BeeperEngine(drop_placeholder(_flag(env, "BEEPER_TOKEN", ""))),
        "bluebubbles": BlueBubblesEngine(_flag(env, "BB_URL", "http://localhost:1234"),
                                         drop_placeholder(env.get("BB_PASSWORD")),
                                         transport=bluebubbles_transport),
        "applescript": applescript if applescript is not None else AppleScriptEngine(),
        # Not passed in: an engine without a binary, which is never configured.
        "imessage-cli": imessage_cli if imessage_cli is not None else ImessageCliEngine(None),
    }
    order = [n.strip() for n in _flag(env, "SEND_ENGINES", "").split(",") if n.strip()]
    if not order:
        order = list(DEFAULT_ORDER)
        if _flag(env, "SEND_APPLESCRIPT_FALLBACK", "1") == "0":
            order.remove("applescript")
    unknown = sorted(set(order) - set(available))
    if unknown:
        raise ValueError(f"SEND_ENGINES names unknown engine(s): {', '.join(unknown)}; "
                         f"known: {', '.join(DEFAULT_ORDER)}")
    chain: list[SendEngine] = []
    for name in order:
        engine = available[name]
        if engine.configured() and engine not in chain:
            chain.append(engine)
    # The edit engine joins whatever SEND_ENGINES says (see the module
    # docstring): last, unless the list gave it a place of its own.
    edit_engine = available["imessage-cli"]
    if edit_engine.configured() and edit_engine not in chain:
        chain.append(edit_engine)
    return chain


def engine_names(chain: list[SendEngine]) -> list[str]:
    return [e.name for e in chain]


def imessage_capabilities(chain: list[SendEngine]) -> list[str]:
    """What ``chain`` can do in an iMessage chat beyond plain sending, sorted:
    a subset of ``react``, ``reply``, ``create_chat``, ``unsend``, ``edit``.
    An engine that only handles Google Messages chats does not count."""
    return sorted(cap.value for cap in IMESSAGE_EXTRAS
                  if any(cap in e.capabilities and e.handles(IMESSAGE_PROBE_GUID) for e in chain))


def first_with(chain: list[SendEngine], cap: Capability,
               chat_guid: str | None = None) -> SendEngine | None:
    """The first engine that has ``cap`` (and handles ``chat_guid`` when given)."""
    for engine in chain:
        if cap in engine.capabilities and (chat_guid is None or engine.handles(chat_guid)):
            return engine
    return None


def no_engine(cap: Capability, chat_guid: str | None = None) -> DeliveryError:
    where = " in this chat" if chat_guid is not None else ""
    return DeliveryError(501, f"no configured engine can {CAPABILITY_VERB[cap]}{where}")


#: Longest upstream error excerpt the hop log carries.
LOG_BODY_CHARS = 200


def _log_detail(res: SendResult) -> str:
    """What the hop log says about a failed result.

    A failure without an upstream HTTP body is logged with the engine's own
    words (``res.detail``).  An upstream HTTP error is logged as its status
    plus the JSON body WITHOUT its ``data`` member, cut at ``LOG_BODY_CHARS``:
    BlueBubbles answers a failed send with the message it could not send,
    text included, under ``data`` (seen in BlueBubbles server 1.9.9), and a
    log file is no place for that.  A body that is not a JSON object is not
    quoted at all.  The HTTP answer to the client is built from ``res.detail``
    / ``res.body`` elsewhere and is not affected.
    """
    if res.status is None or res.body is None:
        return res.detail
    try:
        parsed = json.loads(res.body)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        return f"HTTP {res.status}, body not quoted ({len(res.body)} characters, not a JSON object)"
    parsed.pop("data", None)
    return f"HTTP {res.status}: {json.dumps(parsed, ensure_ascii=False)[:LOG_BODY_CHARS]}"


async def deliver(chain: list[SendEngine], cap: Capability, chat_guid: str | None,
                  *args: Any, log: Callable[[str], None] = print, **kwargs: Any) -> SendResult:
    """Walk ``chain`` for ``cap``; the first ``ok`` result wins.

    ``chat_guid`` filters by ``handles`` (``None`` for operations without a
    chat, i.e. ``create_chat``).  The engine method gets ``chat_guid`` (when
    not ``None``) followed by ``*args``/``**kwargs``.  A method that returns a
    plain value (``create_chat`` -> guid, ``chat_icon`` -> bytes) is wrapped
    as an ``ok`` result with it as ``payload``.
    """
    eligible = [e for e in chain
                if cap in e.capabilities and (chat_guid is None or e.handles(chat_guid))]
    if not eligible:
        raise no_engine(cap, chat_guid)
    failures: list[tuple[SendEngine, SendResult]] = []
    for i, engine in enumerate(eligible):
        method = getattr(engine, _METHOD[cap])
        call_args = (chat_guid, *args) if chat_guid is not None else args
        try:
            res = await method(*call_args, **kwargs)
        except Unsupported as e:            # capability advertised but not implemented
            res = SendResult(False, engine.via, e.detail)
        except EngineError as e:
            res = SendResult(False, engine.via, e.detail, status=e.status, body=e.body)
        except Exception as e:              # a transport error the engine did not wrap
            res = SendResult(False, engine.via, f"{engine.name} failed ({e})")
        if not isinstance(res, SendResult):
            res = SendResult(True, engine.via, payload=res)
        if res.ok:
            if failures:
                log(f"[send] delivered via {engine.name} -> {chat_guid}")
            return res
        if res.uncertain:
            # It may be on its way: a second engine would deliver it twice.
            log(f"[send] {engine.name} gave no answer ({_log_detail(res)}) — not trying another engine")
            raise DeliveryError(502, MAYBE_SENT)
        failures.append((engine, res))
        nxt = eligible[i + 1].name if i + 1 < len(eligible) else None
        log(f"[send] {engine.name} failed ({_log_detail(res)})"
            + (f" — trying {nxt}" if nxt else ""))
    if len(failures) == 1 and failures[0][1].status is not None:
        _, only = failures[0]
        raise DeliveryError(only.status, only.body if only.body is not None else only.detail)
    raise DeliveryError(502, "; ".join(res.detail for _, res in failures))
