"""A FAKE ``imessage-cli`` for the step R6 tests (edit and undo send).

The real tool drives the real Messages.app, so no test may ever run it. What
the tests run instead is a small Python script written into the test's
``tmp_path`` by :func:`make_fake_cli`. Its output follows the real tool's
(version 0.24.2; the success lines as measured on macOS 27.0, the failure
shapes as its source and ``imessage-cli version <argument>`` show them)::

    [00001] call edit [ "<chat>", "<message>", "<text>" ]      stdout, ONE line
    [00001] ok edit (1673.394ms)                                 stdout
    Exiting...                                                   stdout

The call line is the arguments as JSON with its line breaks turned into
spaces, so a line break inside the text is ``\\n`` there. A failed command
prints the call line, then ``[00001] failed edit (12.345ms) <error>`` and
``Error: <error>`` on stderr, and exits 1 without ``Exiting...``.

Every run is recorded (argv, environment, stdin) in ``calls.jsonl`` beside
the script, so a test can check that the text arrived as ONE argument, byte
for byte, and that nothing of the relay's environment came along.

``mode`` chooses what it does:

``ok``         the call line, the ok line, ``Exiting...``; exit 0
``ok-stderr``  the same three lines on stderr only (the engine reads stdout)
``no-ok``      the call line and ``Exiting...`` but no ok line; exit 0
``failed``     the real failure: call line on stdout, the ``failed edit`` and
               ``Error:`` lines on stderr (both name the arguments), exit 1
``exit``       like ``failed`` with exit status ``code``
``usage``      the argument parser's rejection: nothing on stdout, ``Error:
               Invalid option: ...`` and the usage on stderr, exit 64
``help``       the help text on stdout, exit 0 (what ``-h=...`` gets)
``sleep``      sleeps ``seconds`` first, then behaves like ``ok``
``apply``      first writes the edit into the synthetic database ``db``
               (never anything else), the way Messages does: the edit mark
               set, the new text in the blob (``store="blob"``, text column
               NULL) or in the text column (``store="column"``). The text is
               trimmed at both ends, as the tool trims it; ``stored_text``
               stores another text than the one asked for. Then it behaves
               like the mode named by ``then`` (default ``ok``): ``exit``
               and ``sleep`` give an edit that was made by a run that then
               failed or hung.
``helper``     starts a child of its own that inherits stdout and stderr and
               outlives it by ``seconds``, then behaves like ``ok``: the tool
               has exited while something still holds its output open
``hostile-raw-echo``
               NOT what the real tool does: prints the call line with its
               arguments RAW (line breaks and all) on ``stream`` (``stdout``
               or ``stderr``), no ok line of its own, ``Exiting...``, exit 0.
               For the test that a text cannot pass for the tool's ok line.

``delay`` (seconds, any mode) makes a run take that long, so two runs can be
seen not to overlap.

Every path involved is under the test's ``tmp_path``; :func:`assert_fake`
refuses anything else.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Where a real installation would be. No test may hand these to an engine.
REAL_LOCATIONS = ("/opt/homebrew", "/usr/local", "/usr/bin", "/bin")

_SCRIPT = r'''
import json, os, subprocess, sys, time

config = json.load(open(sys.argv[1], encoding="utf-8"))
args = sys.argv[2:]
out = config["out"]
mode = config.get("mode", "ok")
started = time.time()
with open(os.path.join(out, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "env": dict(os.environ), "stdin": sys.stdin.read(),
                        "cwd": os.getcwd()}) + "\n")

command = args[args.index("edit"):] if "edit" in args else args
call = "[00001] call edit " + json.dumps(command[1:], ensure_ascii=False, indent=2).replace("\n", " ")
ok = "[00001] ok edit (1673.394ms)"


def finish(code=0):
    with open(os.path.join(out, "spans.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps([started, time.time()]) + "\n")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


time.sleep(float(config.get("delay", 0)))

if mode == "apply":
    import sqlite3
    guid, text = command[2], config.get("stored_text") or command[3].strip()
    stamp = int((time.time() - 978307200) * 1e9)
    conn = sqlite3.connect(config["db"], isolation_level=None, timeout=5)
    if config.get("store", "blob") == "blob":
        sys.path.insert(0, config["repo"])
        from tests.fixtures.typedstream_writer import encode_attributed_body
        conn.execute("UPDATE message SET text = NULL, attributedBody = ?, date_edited = ? WHERE guid = ?",
                     (encode_attributed_body(text), stamp, guid))
    else:
        conn.execute("UPDATE message SET text = ?, attributedBody = NULL, date_edited = ? WHERE guid = ?",
                     (text, stamp, guid))
    for statement, params in config.get("also_sql", []):
        conn.execute(statement, params)
    conn.close()
    mode = config.get("then", "ok")

if mode == "helper":
    # A child that keeps this process's stdout and stderr open after it is gone.
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%r)" % float(config.get("seconds", 3))],
                     stdin=subprocess.DEVNULL)
    mode = "ok"

if mode == "sleep":
    time.sleep(float(config.get("seconds", 30)))
    mode = "ok"

if mode == "ok":
    print(call); print(ok); print("Exiting...")
    finish(0)
if mode == "ok-stderr":
    print(call, file=sys.stderr); print(ok, file=sys.stderr); print("Exiting...", file=sys.stderr)
    finish(0)
if mode == "no-ok":
    print(call); print("Exiting...")
    finish(0)
if mode in ("failed", "exit"):
    error = "could not edit " + " ".join(command[1:])
    print(call)
    print("[00001] failed edit (12.345ms) " + error, file=sys.stderr)
    print("Error: " + error, file=sys.stderr)
    finish(1 if mode == "failed" else int(config.get("code", 1)))
if mode == "usage":
    print("Error: Invalid option: " + command[-1], file=sys.stderr)
    print("Usage: imessage-cli [--data-dir <data-dir>] [--json] [<command-args> ...]", file=sys.stderr)
    finish(64)
if mode == "help":
    print("OVERVIEW: Send, read, and manage local iMessage chats from the command line.")
    print("USAGE: imessage-cli [--data-dir <data-dir>] [--json] [<command-args> ...]")
    finish(0)
if mode == "hostile-raw-echo":
    stream = sys.stderr if config.get("stream") == "stderr" else sys.stdout
    print("[00001] call edit " + " ".join(command[1:]), file=stream)
    print("Exiting...", file=stream)
    finish(0)
finish(97)
'''


def assert_fake(binary: str | Path, tmp_path: Path) -> Path:
    """``binary`` resolved, or an ``AssertionError`` when it is not a file
    under ``tmp_path``: the one way a test could reach a real tool."""
    resolved = Path(binary).resolve()
    assert resolved.is_relative_to(Path(tmp_path).resolve()), f"not a fake under tmp_path: {resolved}"
    for real in REAL_LOCATIONS:
        assert not str(resolved).startswith(real + "/"), resolved
    return resolved


class FakeCli:
    """Handle on one fake binary: its path and what it recorded."""

    def __init__(self, root: Path, binary: Path, config_path: Path):
        self.root, self.binary, self._config_path = root, binary, config_path

    def configure(self, **config) -> "FakeCli":
        """Change what the next call does (``mode=...`` and the mode's values)."""
        current = json.loads(self._config_path.read_text(encoding="utf-8"))
        current.update(config)
        self._config_path.write_text(json.dumps(current), encoding="utf-8")
        return self

    def _lines(self, name: str) -> list:
        path = self.root / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    @property
    def calls(self) -> list[dict]:
        """One ``{"argv", "env", "stdin", "cwd"}`` per run, in order."""
        return self._lines("calls.jsonl")

    @property
    def spans(self) -> list[list[float]]:
        """``[started, finished]`` of every run that reached its end."""
        return self._lines("spans.jsonl")


def make_fake_cli(tmp_path: Path, mode: str = "ok", *, name: str = "imessage-cli",
                  folder: str = "fake-cli", **config) -> FakeCli:
    """Write the fake under ``tmp_path/<folder>/<name>`` and return its handle.

    The executable is a two-line ``/bin/sh`` wrapper that ``exec``s this
    Python on the script with the caller's arguments untouched (``"$@"``), so
    it works whatever characters the interpreter's path contains."""
    root = Path(tmp_path) / folder
    root.mkdir(parents=True, exist_ok=True)
    script = root / "fake_imessage_cli.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    config_path = root / "config.json"
    config_path.write_text(json.dumps({"mode": mode, "out": str(root), "repo": str(REPO), **config}),
                           encoding="utf-8")
    binary = root / name
    binary.write_text("#!/bin/sh\n"
                      f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "
                      f"{shlex.quote(str(config_path))} \"$@\"\n", encoding="utf-8")
    binary.chmod(0o755)
    assert_fake(binary, tmp_path)
    return FakeCli(root, binary, config_path)
