"""Send Later: a text the owner wants sent at a later time (2026-10-11).

The app hands the relay the text, the chat, a time and the send id it will go
out under (``POST /scheduled``). The relay keeps the item in its state file
(key ``scheduled``: it survives a restart, and the text is on the Mac until
the item is cancelled, or a day after it is sent or fails) and a background
loop delivers it at ``send_at``, through exactly
the path ``POST /send`` takes: ``deliver_text`` under the item's ``client_id``
via ``_send_once``, so the send-id file decides whether a message goes out,
and a relay that stops in the middle of a delivery cannot send it twice after
it starts again.

An item is ``pending``, then ``sending``, then ``sent`` or ``failed``. Only a
pending item can be changed; a pending or a failed one can be cancelled or
sent now (for a failed one, a retry under its own send id, or under a fresh
one when the owner chooses "send anyway"). A finished item stays listed for a
day. An item whose time passed while the relay was not running is sent if it
is less than six hours late; later than that it fails with a fixed reason,
and the owner decides what to do with the text. One outcome does not end an
item: a send the send-id file could not record (it cannot be read or
written) was certainly not made and most likely can be soon, and the loop
never retries a failed item on its own, so the item is pending again and
tried at the next look, until the six hours are up.

Every state change is a ``{"type": "scheduled"}`` frame on the WebSocket; a
finished item is also one push to the owner's devices. Nothing here imports
``relay``: the relay hands the ``Scheduler`` what it needs (the state file,
the send path, the WebSocket, the push lane, a clock, and for tests a sleep
hook), and calls ``mount(app, ...)`` for the routes.

Logs name ids and states, never a text or a chat.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel

from engines import DeliveryError, effect_problem

#: Where the items live in the relay's state file.
STATE_KEY = "scheduled"
#: How far ahead a send may be asked for: at least a minute, at most 30 days.
MIN_AHEAD_SECONDS = 60
MAX_AHEAD_SECONDS = 30 * 86400
#: Due while the relay was not running: still sent up to this late, failed after.
LATE_LIMIT_SECONDS = 6 * 3600
#: How long a sent or failed item stays listed.
KEEP_FINISHED_SECONDS = 24 * 3600
#: The loop looks at the items at least this often, whatever is due.
LOOP_TICK_SECONDS = 30.0
#: How much of the text a push carries.
PREVIEW_CHARS = 60

STATES = ("pending", "sending", "sent", "failed")

#: The 4xx details, fixed phrases so the app can match them.
TEXT_EMPTY = "text must not be empty"
CHAT_EMPTY = "chat_guid must not be empty"
TOO_SOON = "send_at must be at least a minute from now"
TOO_FAR = "send_at must be within 30 days"
BAD_CLIENT_ID = "client_id has to be 8 to 64 letters, digits, '-' or '_'"
UNKNOWN_ITEM = "unknown scheduled message"
NOT_PENDING_EDIT = "only a pending message can be changed"
NOT_PENDING_CANCEL = "only a pending or failed message can be cancelled"
NOT_PENDING_SEND = "only a pending or failed message can be sent now"
#: The states a cancel or a "send now" takes an item from: one that waits, or one
#: whose send failed (the owner's way to retry it or let it go). Sending and sent
#: are final for both.
ACTIONABLE = ("pending", "failed")
NEW_ID_FOR_FAILED_ONLY = "a new send id is for a failed message only"

#: The ``error`` of a failed item: a reason, never the text or the chat.
NOT_RUNNING = "the relay was not running at the time"
OUTCOME_UNKNOWN = "what became of the send is not known"
IDS_UNAVAILABLE = "the relay cannot keep track of sends right now"
ID_REUSED = "the send id belongs to another message"

#: The keys an item is served with, in this order.
PUBLIC_KEYS = ("id", "chat_guid", "text", "send_at", "created_at", "client_id", "reply_to_guid",
               "effect", "state", "sent_at", "sent_via", "error")


def public(item: dict) -> dict:
    """The item as the app sees it: the contract's keys and nothing else
    (``finished_at`` is the relay's own, for the day a finished item stays;
    ``held`` too: why a pending item did not go out at its time)."""
    return {k: item.get(k) for k in PUBLIC_KEYS}


def _table(st: dict) -> dict:
    """The items in a state dict, by id. A key that holds something else is
    not a table of items and is replaced (written only if the change writes)."""
    items = st.get(STATE_KEY)
    if not isinstance(items, dict):
        items = st[STATE_KEY] = {}
    return items


def _reason(exc: Exception) -> str:
    """What a failed delivery is recorded as. The send chain's detail can quote
    BlueBubbles' answer, which repeats the message: only a status or one of
    the fixed phrases is kept."""
    if isinstance(exc, DeliveryError):
        return f"the message could not be sent (HTTP {exc.status})"
    if isinstance(exc, HTTPException):
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        if code == "send_outcome_unknown":
            return OUTCOME_UNKNOWN
        if code == "send_ids_unavailable":
            return IDS_UNAVAILABLE
        if code == "send_id_reused":
            return ID_REUSED
        if detail == NOT_RUNNING:
            return NOT_RUNNING
        return f"the message could not be sent (HTTP {exc.status_code})"
    return f"the message could not be sent ({type(exc).__name__})"


class Scheduler:
    """The items, the loop and the delivery. Everything that touches the rest
    of the relay comes in through the constructor, looked up at call time, so
    a test can replace the relay's send path or WebSocket as it does for
    ``/send``.

    ``clock`` gives unix seconds; ``sleep(seconds)`` is how the loop waits
    between looks (the default waits on an event that ``wake()`` sets, so a
    new or changed item is looked at at once). A test passes a stepped clock
    and a sleep that moves it, and nothing waits for real."""

    def __init__(self, *, load_state: Callable[[], dict], change_state: Callable[[Callable], None],
                 deliver: Callable[..., Awaitable[dict]], send_once: Callable[..., Awaitable[dict]],
                 fingerprint: Callable[[dict], str], id_ok: Callable[[str], bool],
                 broadcast: Callable[[dict], Awaitable[None]], push: Callable[[dict], None],
                 chat_name: Callable[[str], str], enabled: Callable[[], bool] = lambda: True,
                 clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]] | None = None):
        self.load_state = load_state
        self.change_state = change_state
        self.deliver = deliver
        self.send_once = send_once
        self.fingerprint = fingerprint
        self.id_ok = id_ok
        self.broadcast = broadcast
        self.push = push
        self.chat_name = chat_name
        self.enabled = enabled
        self.clock = clock
        self._sleep = sleep or self._sleep_until_woken
        self._wake: asyncio.Event | None = None
        #: The ids this process is delivering right now: a "sending" item that
        #: is not among them was left behind by a run that ended in the middle.
        self._in_flight: set = set()

    # ----- the state file -----

    def _now(self) -> int:
        return int(self.clock())

    def _items(self) -> dict:
        items = self.load_state().get(STATE_KEY)
        return dict(items) if isinstance(items, dict) else {}

    async def _change(self, change) -> None:
        # The state file is written with fsync; not on the loop.
        await asyncio.to_thread(self.change_state, change)

    async def _snapshot(self) -> dict:
        return await asyncio.to_thread(self._items)

    def _listed(self, items: dict, now: int) -> list:
        """Sorted by time; a finished item older than a day is not shown even
        before the loop has dropped it."""
        keep = [it for it in items.values()
                if it["state"] in ("pending", "sending") or it.get("finished_at", now) + KEEP_FINISHED_SECONDS > now]
        return sorted(keep, key=lambda it: (it["send_at"], it["created_at"], it["id"]))

    # ----- validation -----

    def _check(self, chat_guid: str, text: str, send_at: int, effect: str | None, *,
               time_rule: bool = True) -> None:
        """The 422s, in this order. ``time_rule`` is off for an edit that
        leaves the time alone: a text changed a few seconds before it goes
        out is not refused for being due soon."""
        now = self._now()
        if not chat_guid.strip():
            raise HTTPException(422, CHAT_EMPTY)
        if not text.strip():
            raise HTTPException(422, TEXT_EMPTY)
        if time_rule and send_at < now + MIN_AHEAD_SECONDS:
            raise HTTPException(422, TOO_SOON)
        if time_rule and send_at > now + MAX_AHEAD_SECONDS:
            raise HTTPException(422, TOO_FAR)
        if effect is not None:
            # The same rule as /send: one of Apple's ids, and never into a Google Messages chat.
            problem = effect_problem(chat_guid, effect)
            if problem is not None:
                raise HTTPException(422, problem)

    # ----- the routes' work -----

    async def create(self, chat_guid: str, text: str, send_at: int, client_id: str,
                     reply_to_guid: str | None, effect: str | None) -> tuple[dict, bool]:
        """The new item, or the existing one with the same ``client_id`` and
        True: the app asking twice (an answer that never arrived) adds nothing."""
        if not self.id_ok(client_id):
            raise HTTPException(422, BAD_CLIENT_ID)
        # A repeat is answered before the rules, whatever it says: the app asks
        # again with the same body when an answer was lost, and by then the time
        # it named may be less than a minute away, or the item already sent. A
        # 422 there would read as "nothing stored", and a fresh id would send
        # the text twice (review of 2026-10-13).
        for other in (await self._snapshot()).values():
            if other.get("client_id") == client_id:
                print(f"[schedule] {other['id']}: asked for again, answered without adding")
                return public(other), True
        self._check(chat_guid, text, send_at, effect)
        now = self._now()
        item = {"id": uuid.uuid4().hex, "chat_guid": chat_guid, "text": text, "send_at": int(send_at),
                "created_at": now, "client_id": client_id, "reply_to_guid": reply_to_guid, "effect": effect,
                "state": "pending", "sent_at": None, "sent_via": None, "error": None}
        found = {}

        def change(st):
            items = _table(st)
            for other in items.values():
                if other.get("client_id") == client_id:
                    found["item"] = other
                    return False
            items[item["id"]] = item

        await self._change(change)
        if found:
            print(f"[schedule] {found['item']['id']}: asked for again, answered without adding")
            return public(found["item"]), True
        print(f"[schedule] {item['id']}: created, due in {max(0, (send_at - now) // 60)} min")
        await self._event(item, "created")
        self.wake()
        return public(item), False

    async def edit(self, item_id: str, fields: dict) -> dict:
        """``fields`` holds only what the request named (``text``, ``send_at``,
        ``effect``, where ``None`` clears the effect). Checked against the item
        as it will be, then written if the item is still pending."""
        current = (await self._snapshot()).get(item_id)
        if current is None:
            raise HTTPException(404, UNKNOWN_ITEM)
        if current["state"] != "pending":
            raise HTTPException(409, NOT_PENDING_EDIT)
        wanted = {**current, **fields}
        self._check(wanted["chat_guid"], wanted["text"], wanted["send_at"], wanted["effect"],
                    time_rule="send_at" in fields)
        done = {}

        def change(st):
            item = _table(st).get(item_id)
            if item is None:
                return False
            if item["state"] != "pending":
                done["busy"] = item
                return False
            item.update({k: fields[k] for k in ("text", "send_at", "effect") if k in fields})
            if "send_at" in fields:
                # A new time is a new appointment: what kept the old one from going out is forgotten.
                item.pop("held", None)
            done["item"] = item

        await self._change(change)
        # It may have gone out, or been cancelled, between the look above and the write.
        if "busy" in done:
            raise HTTPException(409, NOT_PENDING_EDIT)
        if "item" not in done:
            raise HTTPException(404, UNKNOWN_ITEM)
        print(f"[schedule] {item_id}: edited")
        await self._event(done["item"], "edited")
        self.wake()
        return public(done["item"])

    async def cancel(self, item_id: str) -> None:
        done = {}

        def change(st):
            items = _table(st)
            item = items.get(item_id)
            if item is None:
                return False
            if item["state"] not in ACTIONABLE:
                done["busy"] = item
                return False
            done["item"] = items.pop(item_id)

        await self._change(change)
        if "busy" in done:
            raise HTTPException(409, NOT_PENDING_CANCEL)
        if "item" not in done:
            raise HTTPException(404, UNKNOWN_ITEM)
        print(f"[schedule] {item_id}: cancelled")
        await self._event(done["item"], "cancelled")
        self.wake()

    async def send_now(self, item_id: str, new_client_id: str | None = None) -> dict:
        """Delivered at once, by the loop's own path; the answer is the item
        in its final state. The owner asked, so the late rule does not apply.

        A failed item is retried under its own send id: the id file decides
        whether that is a fresh attempt (the earlier send certainly failed) or
        a repeat it must refuse (the outcome unknown, or delivered after all),
        in which case the item stays failed with that reason. ``new_client_id``
        is the owner's "send anyway" for exactly that case: a new id, so a new
        message, accepted for a failed item only."""
        current = (await self._snapshot()).get(item_id)
        if current is None:
            raise HTTPException(404, UNKNOWN_ITEM)
        if current["state"] not in ACTIONABLE:
            raise HTTPException(409, NOT_PENDING_SEND)
        if new_client_id is not None:
            if not self.id_ok(new_client_id):
                raise HTTPException(422, BAD_CLIENT_ID)
            if current["state"] != "failed":
                raise HTTPException(409, NEW_ID_FOR_FAILED_ONLY)
            swapped = {}

            def change(st):
                item = _table(st).get(item_id)
                if item is None or item["state"] != "failed":
                    return False
                item["client_id"] = new_client_id
                swapped["ok"] = True

            await self._change(change)
            if "ok" not in swapped:
                raise HTTPException(409, NOT_PENDING_SEND)
            print(f"[schedule] {item_id}: sent anyway under a new send id")
        final = await self._deliver(item_id, late_rule=False, by_request=True, from_state=current["state"])
        if final is None:
            # The loop took it in the same moment: it is being sent, not pending.
            raise HTTPException(409, NOT_PENDING_SEND)
        return public(final)

    async def listing(self, chat_guid: str | None) -> list:
        items = await self._snapshot()
        return [public(it) for it in self._listed(items, self._now())
                if chat_guid is None or it["chat_guid"] == chat_guid]

    # ----- the loop -----

    def wake(self) -> None:
        """The loop looks at the items now, not at its next tick."""
        if self._wake is not None:
            self._wake.set()

    async def _sleep_until_woken(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def run(self) -> None:
        """The supervised background loop: a look at the items, then sleep
        until the earliest pending item is due or 30 s, whichever is first, or
        until ``wake()``."""
        self._wake = asyncio.Event()
        while True:
            due_in = await self.tick()
            await self._sleep(LOOP_TICK_SECONDS if due_in is None else min(LOOP_TICK_SECONDS, max(0.0, due_in)))

    async def tick(self) -> float | None:
        """One look: deliver what is due, resolve what an earlier run left as
        sending, drop finished items older than a day. Returns how long until
        the earliest pending item is due, or None when nothing is pending."""
        now = self._now()
        due, resumed, next_due = [], [], None
        for item in sorted((await self._snapshot()).values(), key=lambda it: (it["send_at"], it["created_at"])):
            if item["state"] == "pending":
                if item["send_at"] <= now:
                    due.append(item)
                elif next_due is None:
                    next_due = item["send_at"] - now
            elif item["state"] == "sending" and item["id"] not in self._in_flight:
                resumed.append(item)
        for item in due:
            if self._too_late(item, now):
                # Due while the relay was not running (or asleep), too long ago
                # to send unasked; or held that long by the send ids, which is
                # then the reason given.
                final = await self._finish(item["id"], "failed", error=item.get("held") or NOT_RUNNING,
                                           from_state="pending")
                if final is not None:
                    await self._announce(final)
                continue
            await self._deliver(item["id"], late_rule=True)
        for item in resumed:
            print(f"[schedule] {item['id']}: left as sending by an earlier run, asking the send ids what became of it")
            await self._deliver(item["id"], late_rule=True, resumed=True)
        await self._prune(now)
        return next_due

    def _too_late(self, item: dict, now: int) -> bool:
        return now - item["send_at"] > LATE_LIMIT_SECONDS

    async def _prune(self, now: int) -> None:
        dropped = []

        def change(st):
            items = _table(st)
            for sid, it in list(items.items()):
                if it["state"] in ("sent", "failed") and it.get("finished_at", now) + KEEP_FINISHED_SECONDS <= now:
                    dropped.append(sid)
                    del items[sid]
            if not dropped:
                return False

        await self._change(change)
        if dropped:
            print(f"[schedule] dropped {len(dropped)} finished item(s) older than a day")

    # ----- delivery -----

    async def _transition(self, item_id: str, to: str, from_state: str, **fields) -> dict | None:
        """``from_state`` -> ``to`` as one step under the state lock, or None
        when the item is not in ``from_state`` any more (another path took it)."""
        done = {}

        def change(st):
            item = _table(st).get(item_id)
            if item is None or item["state"] != from_state:
                return False
            item["state"] = to
            item.update(fields)
            done["item"] = dict(item)

        await self._change(change)
        return done.get("item")

    async def _finish(self, item_id: str, state: str, *, from_state: str = "sending",
                      error: str | None = None, sent_via: str | None = None) -> dict | None:
        now = self._now()
        fields = {"finished_at": now, "sent_at": now if state == "sent" else None,
                  "sent_via": sent_via, "error": error}
        return await self._transition(item_id, state, from_state, **fields)

    async def _deliver(self, item_id: str, *, late_rule: bool, by_request: bool = False,
                       resumed: bool = False, from_state: str = "pending") -> dict | None:
        """The item's text, through the relay's send path under its own send id.
        ``late_rule``: a send that would go out more than six hours late fails
        instead (checked again inside the send, where it decides for an item an
        earlier run left as sending and whose id was never begun). Returns the
        finished item, or None when it was not pending any more or is pending
        again (the send ids could not be read or written: see below). For
        ``by_request`` that last case is /send's own 503, raised."""
        if resumed:
            item = (await self._snapshot()).get(item_id)
            if item is None or item["state"] != "sending":
                return None
        else:
            # From pending, or from failed on the owner's "send now": the earlier
            # outcome is cleared, the send id stays (it is what makes a retry safe).
            item = await self._transition(item_id, "sending", from_state, error=None, finished_at=None, held=None)
            if item is None:
                return None
        self._in_flight.add(item_id)
        try:
            print(f"[schedule] {item_id}: sending" + (" now" if by_request else ""))
            if not resumed:
                await self._event(item, "sending")

            async def run():
                if late_rule and self._too_late(item, self._now()):
                    # 409 is "certainly not sent": the id may be used again by send now.
                    raise HTTPException(409, NOT_RUNNING)
                body = await self.deliver(item["chat_guid"], item["text"], item["reply_to_guid"], item["effect"])
                return body, body.get("via")

            def again(rec):
                return {"via": rec.get("via")}

            try:
                reply = await self.send_once(item["client_id"], self.fingerprint(item), run, again)
            except Exception as e:           # a BaseException (the process ending) leaves it "sending"
                reason = _reason(e)
                if reason == IDS_UNAVAILABLE and (by_request or not self._too_late(item, self._now())):
                    # The send-id file could not be read or written: the id was
                    # never begun, so certainly not sent, and most likely for a
                    # moment only. The item is pending again, with the reason, so
                    # the next look tries it once more while the late window
                    # lasts (a failed item could be retried by hand, but this one
                    # needs no hand). The owner's send now is answered as /send
                    # would be: 503, and asked again.
                    final = None
                    if await self._transition(item_id, "pending", "sending", held=IDS_UNAVAILABLE) is not None:
                        print(f"[schedule] {item_id}: not sent, the send ids are not available; pending again")
                        await self._event(item, "pending")
                    if by_request:
                        raise
                else:
                    final = await self._finish(item_id, "failed", error=reason)
            else:
                final = await self._finish(item_id, "sent", sent_via=reply.get("via"))
        finally:
            self._in_flight.discard(item_id)
        if final is not None:
            await self._announce(final)
        return final

    # ----- what the app hears -----

    async def _event(self, item: dict, state: str) -> None:
        try:
            await self.broadcast({"type": "scheduled", "chat_guid": item["chat_guid"], "id": item["id"],
                                  "state": state})
        except Exception as e:
            print(f"[schedule] {item['id']}: the WebSocket frame was not sent ({type(e).__name__})")

    async def _announce(self, final: dict) -> None:
        """A finished item: the log line, the frame, and one push."""
        if final["state"] == "sent":
            print(f"[schedule] {final['id']}: sent via {final.get('sent_via')}")
        else:
            print(f"[schedule] {final['id']}: failed ({final.get('error')})")
        await self._event(final, final["state"])
        try:
            self.push({"kind": "scheduled", "chat_guid": final["chat_guid"], "id": final["id"],
                       "state": final["state"], "chat_name": self.chat_name(final["chat_guid"]),
                       "text_preview": final["text"][:PREVIEW_CHARS]})
        except Exception as e:
            print(f"[schedule] {final['id']}: the push was not queued ({type(e).__name__})")


# ---------- the routes ----------

class ScheduleReq(BaseModel):
    chat_guid: str
    text: str
    send_at: int
    #: The send id the delivery runs under (``/send``'s ``client_id`` format). Required.
    client_id: str
    reply_to_guid: str | None = None
    effect: str | None = None


class ScheduleEdit(BaseModel):
    text: str | None = None
    send_at: int | None = None
    #: ``null`` clears the effect; a request that leaves the key out keeps it.
    effect: str | None = None


class SendNowReq(BaseModel):
    #: The owner's "send anyway" for a failed item whose earlier outcome is not
    #: known: a fresh send id, so a new message. Left out for a plain retry.
    client_id: str | None = None


def mount(app: FastAPI, scheduler: Callable[[], Scheduler]) -> None:
    """The five routes, on the relay's own app (not through a sub-router,
    which this FastAPI lists in ``app.routes`` as an entry without a path),
    bound to whatever ``scheduler()`` answers at call time. With the feature
    off they answer as an absent route would."""

    def current() -> Scheduler:
        s = scheduler()
        if not s.enabled():
            raise HTTPException(404, "Not Found")
        return s

    @app.post("/scheduled")
    async def schedule_create(req: ScheduleReq):
        item, duplicate = await current().create(req.chat_guid, req.text, req.send_at, req.client_id,
                                                 req.reply_to_guid, req.effect)
        body = {"ok": True, "item": item}
        if duplicate:
            body["duplicate"] = True
        return body

    @app.get("/scheduled")
    async def schedule_list(chat_guid: str | None = None):
        return {"items": await current().listing(chat_guid)}

    @app.patch("/scheduled/{item_id}")
    async def schedule_edit(item_id: str, req: ScheduleEdit):
        # Only what the request named changes; a null effect clears it, a null text or time is no change.
        fields = {k: getattr(req, k) for k in ("text", "send_at", "effect")
                  if k in req.model_fields_set and (k == "effect" or getattr(req, k) is not None)}
        return {"ok": True, "item": await current().edit(item_id, fields)}

    @app.delete("/scheduled/{item_id}")
    async def schedule_cancel(item_id: str):
        await current().cancel(item_id)
        return {"ok": True}

    @app.post("/scheduled/{item_id}/send_now")
    async def schedule_send_now(item_id: str, req: SendNowReq | None = Body(default=None)):
        return {"ok": True, "item": await current().send_now(item_id, req.client_id if req else None)}
