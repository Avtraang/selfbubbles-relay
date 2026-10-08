"""AppleScript send engine: Messages.app's scripting dictionary (the public-API floor).

Messages' scripting dictionary is documented, decades-stable public API.  It
can send text/files to EXISTING chats only -- no tapbacks, edits, or chat
creation -- but it keeps core sending alive when a macOS update breaks the
BlueBubbles Private API injection, and it is the whole chain when no
BlueBubbles password is configured.

``_AS_TEXT``, ``_AS_FILE``, ``OUTBOX``, ``_applescript`` and ``_guid_variants``
are the relay's originals, moved here (the two log lines of ``_applescript``
were rewritten since: see ``_failure_words``).  The engine resolves the
``osascript`` runner and the outbox directory at call time (``runner`` /
``outbox`` may be callables) so the relay can keep exposing them under their
old names and a test can patch either side.

What a failed ``osascript`` run logs never includes its arguments.  They are
the chat guid (a phone number or e-mail address for a one-to-one chat) and the
message text or the staged file's path, and both the exception text of a
timeout (``Command '['osascript', '-e', ..., guid, text]' timed out``) and
Messages' own error text (``Can't get chat id "..."``) quote them.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

from engines.base import Capability, SendResult, Unsupported, is_beeper_guid

_AS_TEXT = """on run {targetGuid, theText}
    tell application "Messages" to send theText to chat id targetGuid
end run"""

_AS_FILE = """on run {targetGuid, filePath}
    set theFile to POSIX file filePath as alias
    tell application "Messages" to send theFile to chat id targetGuid
end run"""

# Messages.app is sandboxed: it can only read files inside its own container.
# Staging outbound fallback files under ~/Library/Messages is the documented
# trick — the relay can write there because Full Disk Access covers it (same
# grant that lets it read chat.db), and Messages can always read it.
OUTBOX = Path.home() / "Library" / "Messages" / "RelayOutbox"


#: The quote pairs AppleScript may put around a value in an error message.
_QUOTE_PAIRS = (('"', '"'), (chr(0x201C), chr(0x201D)))


def _collapse_quoted(line: str) -> str:
    """``line`` with everything between its first and last quote replaced by
    ``...``.  When the quotes do not pair up (a value that continues on the
    next line, or one with quotes of its own) the rest of the line goes too:
    what follows an unbalanced quote may still be part of the value."""
    for opener, closer in _QUOTE_PAIRS:
        first = line.find(opener)
        if first < 0:
            continue
        last = line.rfind(closer)
        balanced = (line.count(opener) % 2 == 0 if opener == closer
                    else line.count(opener) == line.count(closer))
        tail = line[last + 1:] if balanced and last > first else ""
        line = f"{line[:first]}{opener}...{closer}{tail}"
    return line


def _hfs_form(path: str) -> str:
    """A POSIX path the way AppleScript spells a file in an error message:
    HFS style, ``:`` between the parts (and ``/`` where a name contains a
    colon), without the volume name in front.  ``/Users/x/Outbox/a.pdf`` is
    ``Users:x:Outbox:a.pdf``.  Messages reports a file it cannot open as
    ``File <volume>:<this> wasn't found``, outside any quotes, so the path
    has to be searched for in this spelling too or the line keeps the macOS
    user name and the file name."""
    return ":".join(part.replace(":", "/") for part in path.strip("/").split("/"))


def _failure_words(stderr, args) -> str:
    """The loggable part of ``osascript``'s error output: its FIRST line, with
    every literal occurrence of an argument replaced by ``<arg>`` (the
    argument as given, AppleScript-escaped, line by line when it has several
    lines, the file name of a path, and the path in AppleScript's HFS
    spelling; each of them in both Unicode normal forms, because macOS hands
    file names back decomposed) and whatever is quoted replaced by ``"..."``
    (AppleScript quotes the values it complains about, e.g. ``Can't get chat
    id "<guid>"``), cut at 200 characters.  Arguments shorter than three
    characters are not searched for: replacing them would shred the line.
    What is left is the error wording and its number (``-1743`` consent
    denied, ``-1728`` no such chat), which is what a reader needs."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    lines = (stderr or "").strip().splitlines()
    line = lines[0] if lines else ""
    forms: set[str] = set()
    for arg in args:
        whole = str(arg)
        for a in (whole, *whole.splitlines()):
            forms.add(a)
            forms.add(a.replace("\\", "\\\\").replace('"', '\\"'))
            if "/" in a:
                name = a.rsplit("/", 1)[-1]
                forms.update((name, name.replace(":", "/"), _hfs_form(a)))
    for form in list(forms):
        forms.update((unicodedata.normalize("NFC", form), unicodedata.normalize("NFD", form)))
    for form in sorted(forms, key=len, reverse=True):
        if len(form) >= 3:
            line = line.replace(form, "<arg>")
    return _collapse_quoted(line)[:200]


def _applescript(script: str, *args) -> bool:
    try:
        r = subprocess.run(["osascript", "-e", script, *args],
                           capture_output=True, text=True, timeout=20)
    except subprocess.CalledProcessError as e:
        # Not raised by the call above (no check=True); handled so that a
        # runner that does raise it is logged by the same rule.
        print(f"[fallback] osascript failed: rc={e.returncode} {_failure_words(e.stderr, args)}".rstrip())
        return False
    except Exception as e:
        # The class only: str() of a TimeoutExpired is the whole command line,
        # chat guid and message text (or file path) included.
        print(f"[fallback] osascript error: {type(e).__name__}")
        return False
    if r.returncode != 0:
        print(f"[fallback] osascript failed: rc={r.returncode} {_failure_words(r.stderr, args)}".rstrip())
    return r.returncode == 0


def _guid_variants(chat_guid: str) -> Iterator[str]:
    yield chat_guid
    if chat_guid.startswith("any;"):
        yield "iMessage;" + chat_guid[len("any;"):]


Runner = Callable[..., bool]

#: The failure detail is frozen: the relay's 502 joins it after the BlueBubbles
#: one ("BlueBubbles failed (...); AppleScript fallback failed").
FAILED = "AppleScript fallback failed"


class AppleScriptEngine:
    name = "applescript"
    via = "applescript"
    capabilities = frozenset({Capability.TEXT, Capability.ATTACHMENT})
    facetime = None

    def __init__(self, runner: Runner | None = None,
                 outbox: Path | Callable[[], Path] | None = None):
        self._runner = runner
        self._outbox = outbox

    # -- hooks resolved at call time -------------------------------------
    def _run(self, script: str, *args: str) -> bool:
        runner = self._runner if self._runner is not None else _applescript
        return runner(script, *args)

    def _outbox_dir(self) -> Path:
        if self._outbox is None:
            return OUTBOX
        return self._outbox() if callable(self._outbox) else self._outbox

    # -- SendEngine ---------------------------------------------------------
    def configured(self) -> bool:
        return True              # osascript ships with macOS

    def ping(self) -> bool:
        return True

    def handles(self, chat_guid: str) -> bool:
        return not is_beeper_guid(chat_guid)

    # osascript blocks for up to 20 s per attempt, so the sync work runs in a
    # worker thread (as the relay's old applescript_send_* calls did).
    def _send_text_sync(self, chat_guid: str, text: str) -> bool:
        return any(self._run(_AS_TEXT, g, text) for g in _guid_variants(chat_guid))

    def _send_file_sync(self, chat_guid: str, data: bytes, fname: str) -> bool:
        outbox = self._outbox_dir()
        outbox.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for old in outbox.iterdir():
            if now - old.stat().st_mtime > 3600:
                old.unlink(missing_ok=True)
        path = outbox / f"{int(now * 1000)}-{Path(fname).name}"
        path.write_bytes(data)
        return any(self._run(_AS_FILE, g, str(path)) for g in _guid_variants(chat_guid))

    async def send_text(self, chat_guid: str, text: str,
                        reply_to_guid: str | None = None) -> SendResult:
        # AppleScript can't thread: a reply lands as a plain message rather
        # than not at all (reply_to_guid is accepted and ignored).
        if await asyncio.to_thread(self._send_text_sync, chat_guid, text):
            return SendResult(True, self.via)
        return SendResult(False, self.via, FAILED)

    async def send_attachment(self, chat_guid: str, name: str, content: bytes,
                              content_type: str) -> SendResult:
        if await asyncio.to_thread(self._send_file_sync, chat_guid, content, name):
            return SendResult(True, self.via)
        return SendResult(False, self.via, FAILED)

    async def react(self, chat_guid: str, message_guid: str, reaction: str) -> SendResult:
        raise Unsupported(self.name, Capability.REACT)

    # Messages' scripting dictionary has no verb for either (step R6).
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
