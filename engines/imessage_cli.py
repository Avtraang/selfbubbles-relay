"""imessage-cli edit engine: Beeper's open-source command-line tool, which drives
Messages.app through Accessibility (step R6).

``imessage-cli`` (package ``platform-imessage``, ``brew install
beeper/tap/imessage-cli``) is the one way found to EDIT a sent message on
macOS 27: BlueBubbles' edit endpoint answers 200 there and changes nothing.
The reverse holds for unsending: this tool's ``undo-send`` printed ``ok`` and
did nothing (version 0.24.2, macOS 27.0), so the engine advertises ``EDIT``
and nothing else, and a success reported here is still only the tool's word.
The relay confirms every change in ``chat.db`` before it answers the app.

The one call is::

    imessage-cli --json --no-events --data-dir <dir> edit CHAT_ID MESSAGE_ID TEXT

run without a shell, with stdin closed, a four-variable environment and a
timeout, one call at a time (the tool works one Messages window).  It needs
the Accessibility grant for the Python that runs the relay (and the Full Disk
Access that Python has anyway); ``imessage-cli authorize`` shows, and asks
for, the grants of the program that runs it.

Everything below was checked against ONE version, 0.24.2 (``CHECKED_VERSION``):
with ``imessage-cli help ...`` and ``imessage-cli version <argument>``, never
with a real edit.  A ``brew upgrade`` replaces the binary behind the same
path; the relay's doctor row shows which version is installed.

What the tool does with its arguments decides what the engine refuses:

* it has no end-of-options marker: ``--`` is passed on as an ordinary
  argument, so it cannot protect what follows it;
* an argument that is exactly one of its own options (``--json``,
  ``--verbose``, ``--format=...``, ``-h``, ...) is consumed as that option
  wherever it stands, the text position included, and ``-h=anything`` prints
  the help;
* an argument that starts with three or more hyphens (``---``, ``--- note
  ---``) is rejected by its argument parser before any command runs (exit
  status 64);
* other arguments that start with a hyphen (``- milk``, ``-5 degrees``,
  ``--not an option``) are passed through as written;
* ``MESSAGE_ID`` also accepts ``latest``, ``latest-3``, ``last-message`` and
  the like, meaning "the newest message", and ``CHAT_ID`` accepts a bare
  phone number that the tool resolves by itself;
* the text is trimmed at both ends before it is entered.

So: the message id must look like a guid and not like one of those aliases,
the chat id must be a full chat guid (``any;-;...``), and a text that has the
SHAPE of a command-line option (``-x``, ``-x=...``, ``--word``,
``--word=...``, or anything behind three hyphens) is refused, whether or not
this version has such an option: the tool would take it as one and edit
nothing, or reject it, and the next version may have options this one lacks.
A text with a control character in it is refused too (what a key code does
when it is entered into Messages was never tried).  Everything else is handed
over as one argv element, as given.

Two more things are checked before the tool is started, because of what it
does when they fail:

* **Accessibility.**  Without the grant the tool does not fail: going by
  its source (not tried), it asks for it (the system prompt, its own window)
  and waits up to two minutes, so every edit would hang until the timeout
  and leave permission windows on the Mac.  ``accessibility_trusted()`` asks macOS whether this process is
  trusted, which never prompts; when the answer is no, the edit is refused
  with a fixed detail and the tool is not run.  (The tool is a child of this
  process, and macOS attributes a child's request to the program responsible
  for it, here the Python that runs the relay: that is why the grant belongs
  to that Python.  That the two answers agree under launchd follows from
  that rule and was not measured.)
* **The data directory** must be the relay's own: a real directory (not a
  symbolic link), owned by this user, and closed to everyone else (its mode
  is tightened to 0700 when it is wider).  What the tool keeps there has not
  been examined.

Nothing here logs or returns the arguments.  The tool echoes them (``[00001]
call edit [...]``), so its output is read for the ``ok edit`` line and then
dropped: a failure is reported as a short classification (``imessage-cli
timed out``, ``imessage-cli exited 1``, ``imessage-cli reported an error``).
The output goes to an unnamed temporary file rather than a pipe: a pipe stays
open for as long as anything the tool started holds it, and waiting for its
end would turn a finished edit into a timeout.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from engines.base import Capability, SendResult, Unsupported, is_beeper_guid

NAME = "imessage-cli"

#: The one version of the tool the engine's rules were checked against.
CHECKED_VERSION = "0.24.2"

#: Where Homebrew puts the binary (Apple silicon, then Intel).  launchd's PATH
#: has neither directory, so the binary is found by absolute path.
DEFAULT_PATHS: tuple[str, ...] = ("/opt/homebrew/bin/imessage-cli", "/usr/local/bin/imessage-cli")

#: ``IMESSAGE_CLI`` values that switch the engine off.
OFF_VALUES = frozenset({"0", "off"})

#: Seconds one ``edit`` may take (2.7 s when measured; the tool retries inside).
EDIT_TIMEOUT = 45.0

#: Seconds to wait before counting Messages instances a second time, when the
#: first count after a call is higher than the one before it.
RECOUNT_DELAY = 1.0

#: How much of the tool's output is read back (its three lines are far shorter).
MAX_OUTPUT_BYTES = 1 << 20

# Failure details: fixed words, never an argument and never the tool's output.
NOT_FOUND = "imessage-cli is not installed"
TIMED_OUT = "imessage-cli timed out"
NOT_STARTED = "imessage-cli could not be started"
REPORTED_ERROR = "imessage-cli reported an error"
NEEDS_ACCESSIBILITY = "imessage-cli needs the Accessibility grant for the relay's Python"
BAD_MESSAGE_ID = "imessage-cli cannot take this message id"
BAD_CHAT_ID = "imessage-cli cannot take this chat id"
BAD_TEXT = "imessage-cli cannot take this text"
OPTION_TEXT = "imessage-cli cannot take a text that reads as one of its options"
ONLY_FIRST_PART = "imessage-cli can only edit the first part of a message"

_EXITED = re.compile(r"imessage-cli exited -?\d+")


def exited(code: int) -> str:
    return f"imessage-cli exited {code}"


def tool_ran(detail: str) -> bool:
    """Is ``detail`` one of the failures reported AFTER the tool was started
    (it timed out, exited with a status, or ended without its ``ok`` line)?
    Then Messages may have been changed all the same, and only ``chat.db``
    can say.  False for every refusal made before the tool was run."""
    return detail in (TIMED_OUT, REPORTED_ERROR) or _EXITED.fullmatch(detail or "") is not None


#: A message guid as chat.db holds one (a UUID; ``p:0/<uuid>`` is tolerated):
#: letters, digits, ``:``, ``/``, ``_``, ``.`` and ``-``, at most 128
#: characters, starting with a letter or digit so it can never read as an option.
_MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:/_.-]{0,127}")

#: The tool's own words for "the newest message": never a guid of ours.
_NEWEST_ALIAS = re.compile(r"(?i)(last-?message|latest-?message|latest(-\d+)?)")

#: A full chat guid: ``<service>;<-|+>;<identifier>`` (``any;-;+1555...``,
#: ``iMessage;+;chat123``).  A bare address is refused: the tool would look
#: for "a chat with that recipient" by itself.
_CHAT_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*;[-+];[^\x00-\x1f\x7f]{1,256}")

#: The shape of one command-line option: ``-x``, ``-x=...``, ``--name`` or
#: ``--name=...``; and anything that starts with three or more hyphens, which
#: the tool's argument parser rejects outright.
_OPTION_SHAPE = re.compile(r"-[A-Za-z](=.*)?|--[A-Za-z0-9][A-Za-z0-9-]*(=.*)?|-{3,}.*", re.DOTALL)

#: Control characters a text may not carry: all of C0 except tab, line feed
#: and carriage return, and DEL.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: The tool's two log lines for a call, as 0.24.2 prints them to stdout:
#: ``[00001] call edit [ ...the arguments as JSON, on one line... ]`` and
#: ``[00001] ok edit (1673.394ms)``.
_CALL_EDIT = re.compile(r"(?m)^\[(\d{5,})\] call edit ")
_OK_EDIT = re.compile(r"(?m)^\[(\d{5,})\] ok edit \(\d+(?:\.\d+)?ms\)$")

#: Homebrew keeps each version in a folder of its own, and the binary on the
#: PATH is a link into it: ``.../Cellar/imessage-cli/0.24.2/bin/imessage-cli``.
_CELLAR = re.compile(r"/Cellar/imessage-cli/([0-9][0-9A-Za-z.+-]{0,31})(?:_\d+)?/")


def reads_as_option(text: str) -> bool:
    """Would the tool take ``text``, alone in an argv element, for an option,
    or reject it as a malformed one?  True for ``-h``, ``-h=x``, ``--json``,
    ``--format=yaml`` and anything else of that shape, whether or not this
    version of the tool has such an option, and for every text that starts
    with three or more hyphens (``---``, ``--- note ---``); False for every
    other text with a space before any ``=``, for ``-``, ``--``, ``- milk``,
    ``-5 degrees`` and ordinary sentences."""
    return _OPTION_SHAPE.fullmatch(text) is not None


def argument_problem(chat_guid: str, message_guid: str, text: str, part_index: int = 0) -> str | None:
    """Why this edit cannot be handed to the tool, or ``None`` when it can.
    The answer is one of the fixed details above: it never quotes a value."""
    if part_index != 0:
        return ONLY_FIRST_PART
    if not isinstance(message_guid, str) or not _MESSAGE_ID.fullmatch(message_guid) \
            or _NEWEST_ALIAS.fullmatch(message_guid):
        return BAD_MESSAGE_ID
    if not isinstance(chat_guid, str) or is_beeper_guid(chat_guid) or not _CHAT_ID.fullmatch(chat_guid):
        return BAD_CHAT_ID
    if not isinstance(text, str) or not text or _CONTROL.search(text):
        return BAD_TEXT                       # nothing to put in an argument, a NUL, or a key code
    if reads_as_option(text):
        return OPTION_TEXT
    return None


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_binary(setting: str | None = None, *, candidates: Iterable[str] = DEFAULT_PATHS,
                which: Callable[[str], str | None] = shutil.which) -> str | None:
    """The ``imessage-cli`` binary to run, as an absolute path, or ``None``.

    ``setting`` is the ``IMESSAGE_CLI`` value: ``0`` / ``off`` disables the
    engine; any other non-empty value is an explicit path (``~`` expanded)
    and is used if it is an executable file, with no fallback when it is not,
    because naming a binary means that binary.  Unset, the first executable
    among ``candidates`` wins, then whatever ``which("imessage-cli")`` finds
    on the PATH.  Nothing is run."""
    value = (setting or "").strip()
    if value.lower() in OFF_VALUES:
        return None
    if value:
        path = os.path.abspath(os.path.expanduser(value))
        return path if _is_executable(path) else None
    for candidate in candidates:
        if _is_executable(candidate):
            return candidate
    found = which(NAME)
    return os.path.abspath(found) if found and _is_executable(found) else None


def installed_version(binary: str | None) -> str | None:
    """The version of a Homebrew-installed ``binary``, read from the folder
    its link points into (``.../Cellar/imessage-cli/<version>/bin/...``), or
    ``None`` when the path does not say.  Nothing is run."""
    if not binary:
        return None
    try:
        found = _CELLAR.search(os.path.realpath(binary))
    except (OSError, ValueError):
        return None
    return found.group(1) if found else None


def child_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The whole environment the tool gets: ``HOME``, ``PATH``, ``LANG`` and
    ``TMPDIR``.  The relay's own environment holds its token and every
    upstream credential; none of that is the tool's business."""
    env = os.environ if environ is None else environ
    return {
        "HOME": env.get("HOME") or os.path.expanduser("~"),
        "PATH": env.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": env.get("LANG") or "en_US.UTF-8",
        "TMPDIR": env.get("TMPDIR") or tempfile.gettempdir(),
    }


#: The framework that answers "is this process trusted for Accessibility".
_APPLICATION_SERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"


def accessibility_trusted() -> bool | None:
    """Does macOS trust THIS process to control other apps (Privacy &
    Security > Accessibility)?  ``None`` when that cannot be told (not macOS,
    the framework would not load).  ``AXIsProcessTrusted`` only reads the
    state and never prompts (the prompting call is another one, with
    options).  macOS may keep the answer for the life of a process, so a
    grant given while the relay runs can need a restart to be seen."""
    try:
        import ctypes
        trusted = ctypes.CDLL(_APPLICATION_SERVICES).AXIsProcessTrusted
        trusted.restype = ctypes.c_bool
        trusted.argtypes = []
        return bool(trusted())
    except Exception:
        return None


def prepare_data_dir(path: Path) -> bool:
    """Make ``path`` the tool's data directory: created 0700 when missing
    (parents as the system makes them), and accepted when it exists only if
    it is a real directory, not a symbolic link, owned by this user; a mode
    wider than 0700 is tightened.  False when it cannot be had that way."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        found = os.lstat(path)
        if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
            return False                     # a link (lstat does not follow it), or somebody else's
        if stat.S_IMODE(found.st_mode) != 0o700:
            os.chmod(path, 0o700)
    except OSError:
        return False
    return True


def count_messages_instances() -> int | None:
    """How many Messages.app processes are running, or ``None`` when that
    cannot be told.  Read-only: one ``pgrep`` for the exact process name."""
    try:
        r = subprocess.run(["/usr/bin/pgrep", "-x", "Messages"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode not in (0, 1):          # 1 = no such process
        return None
    return sum(1 for line in r.stdout.split() if line.isdigit())


def reported_ok(stdout: bytes | str | None) -> bool:
    """Did the tool say its edit succeeded?  Its stdout must hold the ``[n]
    call edit`` line and, with the same number, a line that is exactly ``[n]
    ok edit (<elapsed>ms)``.  Standard error is not looked at: 0.24.2 writes
    both lines to stdout and only a failure to stderr.

    The tool prints its arguments in the call line as JSON on ONE line, so a
    text with a line break in it cannot start a line of its own and pass for
    the ``ok`` line.  That is the tool's doing, not this check's: a version
    that echoed its arguments raw could be made to print a forged line, which
    is one more reason the relay believes ``chat.db`` and not this."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if not stdout:
        return False
    call = _CALL_EDIT.search(stdout)
    if call is None:
        return False
    return any(ok.group(1) == call.group(1) for ok in _OK_EDIT.finditer(stdout))


def _run_tool(argv: list[str], *, timeout: float, env: Mapping[str, str]) -> tuple[int, bytes]:
    """Run the tool once and wait for it: ``(exit status, its stdout)``.

    stdout goes to an unnamed temporary file (gone when this returns) and
    stderr nowhere; stdin is closed.  Not pipes: ``subprocess.run`` with
    captured output waits for the END of the pipes, and anything the tool
    started and left running (it opens a second Messages instance) would
    hold them open, so that an edit that finished in three seconds came back
    as a timeout.  With a file, the tool's own exit is all that is waited
    for.  On a timeout the tool itself is killed, and nothing else: no
    process group, no other process.  Raises ``subprocess.TimeoutExpired``
    (whose text is the command line and is never used) or whatever starting
    the process raised."""
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.DEVNULL, env=dict(env))
        try:
            code = proc.wait(timeout=timeout)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        out.seek(0)
        return code, out.read(MAX_OUTPUT_BYTES)


# One call at a time: the tool drives a single Messages window.  The lock
# belongs to the module, not to an engine object, because the relay builds a
# new chain (and so a new engine object) for every request; it is kept per
# event loop, because an ``asyncio.Lock`` cannot be shared between loops.
# Waiting on it costs no thread.  ``_RUNNING`` is the second half: held by
# the worker thread for as long as the tool runs, so a request that was
# cancelled while its call was still in flight cannot let the next one start
# a second copy of the tool beside it.
_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()
_LOCKS_BY_ID: dict[int, asyncio.Lock] = {}
_RUNNING = threading.Lock()


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    try:
        lock = _LOCKS.get(loop)
        if lock is None:
            lock = _LOCKS[loop] = asyncio.Lock()
    except TypeError:
        # A loop type that cannot be weakly referenced (asyncio's and uvloop's
        # can): keep its lock by identity instead.
        lock = _LOCKS_BY_ID.setdefault(id(loop), asyncio.Lock())
    return lock


class ImessageCliEngine:
    """``binary`` is what ``find_binary`` returned (``None``: not installed,
    the engine is then not ``configured()`` and never joins a chain).
    ``data_dir`` is where the tool keeps its own state between calls (the
    relay passes ``<data dir>/imessage-cli``); it may be a callable, resolved
    at call time, and an engine without one cannot run the tool.  ``count``,
    ``trusted`` and ``log`` are for tests."""

    name = NAME
    via = NAME
    capabilities = frozenset({Capability.EDIT})
    facetime = None

    def __init__(self, binary: str | None, data_dir: Path | str | Callable[[], Path] | None = None, *,
                 timeout: float = EDIT_TIMEOUT,
                 count: Callable[[], int | None] | None = None,
                 trusted: Callable[[], bool | None] | None = None,
                 log: Callable[[str], None] = print,
                 recount_delay: float = RECOUNT_DELAY):
        self.binary = binary or None
        self._data_dir = data_dir
        self._timeout = timeout
        self._count = count
        self._trusted = trusted
        self._log = log
        self._recount_delay = recount_delay

    # -- hooks resolved at call time -----------------------------------------
    def data_dir(self) -> Path | None:
        if self._data_dir is None:
            return None
        return Path(self._data_dir() if callable(self._data_dir) else self._data_dir)

    def _instances(self) -> int | None:
        counter = self._count if self._count is not None else count_messages_instances
        try:
            return counter()
        except Exception:
            return None

    def _is_trusted(self) -> bool | None:
        check = self._trusted if self._trusted is not None else accessibility_trusted
        try:
            return check()
        except Exception:
            return None

    # -- SendEngine -------------------------------------------------------------
    def configured(self) -> bool:
        return bool(self.binary)

    def ping(self) -> bool:
        return bool(self.binary) and _is_executable(self.binary)

    def handles(self, chat_guid: str) -> bool:
        return not is_beeper_guid(chat_guid)

    def command(self, chat_guid: str, message_guid: str, text: str) -> list[str]:
        """The argv of one edit; ids and text are single elements, as given."""
        return [str(self.binary), "--json", "--no-events", "--data-dir", str(self.data_dir()),
                "edit", chat_guid, message_guid, text]

    def _note_instances(self, before: int | None) -> None:
        """One log line when the call left more Messages.app processes running
        than it found.  (In an earlier trial, August 2026, every one-shot call
        left a hidden second instance behind; 0.24.2 on macOS 27 left none.)
        Counts and reports only: nothing is ever quit or killed from here."""
        if before is None:
            return
        after = self._instances()
        if after is not None and after > before and self._recount_delay > 0:
            time.sleep(self._recount_delay)          # a helper instance may still be closing
            after = self._instances()
        if after is not None and after > before:
            self._log(f"[imessage-cli] {after - before} extra Messages instance(s) left running")

    def _edit_sync(self, chat_guid: str, message_guid: str, text: str) -> str | None:
        """Run the tool once; ``None`` for success, else the failure detail."""
        with _RUNNING:
            if self._is_trusted() is False:
                # Started without the grant, the tool asks for it and waits:
                # a hung request and permission windows on the Mac, every time.
                return NEEDS_ACCESSIBILITY
            data_dir = self.data_dir()
            if data_dir is None or not prepare_data_dir(data_dir):
                return NOT_STARTED
            before = self._instances()
            try:
                code, stdout = _run_tool(self.command(chat_guid, message_guid, text),
                                         timeout=self._timeout, env=child_environment())
            except subprocess.TimeoutExpired:
                # str() of this exception is the whole command line: never used.
                outcome: str | None = TIMED_OUT
            except Exception:
                outcome = NOT_STARTED
            else:
                if code != 0:
                    outcome = exited(code)
                elif not reported_ok(stdout):
                    outcome = REPORTED_ERROR
                else:
                    outcome = None
            self._note_instances(before)
            return outcome

    async def edit(self, chat_guid: str, message_guid: str, text: str,
                   part_index: int = 0) -> SendResult:
        """Edit the owner's message ``message_guid`` in ``chat_guid`` to
        ``text``.  Success needs BOTH exit status 0 and the tool's ``ok edit``
        line; the detail of a failure is a fixed classification
        (``tool_ran(detail)`` says whether the tool was started at all).
        Apple allows an edit for 15 minutes after sending and five per
        message; the relay's route checks both before it asks."""
        if not self.binary:
            return SendResult(False, self.via, NOT_FOUND)
        problem = argument_problem(chat_guid, message_guid, text, part_index)
        if problem is not None:
            return SendResult(False, self.via, problem)
        async with _lock():
            outcome = await asyncio.to_thread(self._edit_sync, chat_guid, message_guid, text)
        if outcome is None:
            return SendResult(True, self.via)
        return SendResult(False, self.via, outcome)

    async def unsend(self, chat_guid: str, message_guid: str, part_index: int = 0) -> SendResult:
        # The tool has an undo-send command; it reported success and retracted
        # nothing when tried (0.24.2, macOS 27.0), so it is not offered.
        raise Unsupported(self.name, Capability.UNSEND)

    async def send_text(self, chat_guid: str, text: str,
                        reply_to_guid: str | None = None) -> SendResult:
        raise Unsupported(self.name, Capability.TEXT)

    async def send_attachment(self, chat_guid: str, name: str, content: bytes,
                              content_type: str) -> SendResult:
        raise Unsupported(self.name, Capability.ATTACHMENT)

    async def react(self, chat_guid: str, message_guid: str, reaction: str) -> SendResult:
        raise Unsupported(self.name, Capability.REACT)

    async def create_chat(self, addresses: list[str], text: str) -> str | None:
        raise Unsupported(self.name, Capability.CREATE_CHAT)

    async def chat_icon(self, chat_guid: str) -> bytes | None:
        raise Unsupported(self.name, Capability.CHAT_ICON)

    def contacts(self) -> list[dict]:
        raise Unsupported(self.name, Capability.CONTACTS)
