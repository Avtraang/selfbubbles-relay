"""The shipped example configurations start a stranger with everything off
(plan 2026-10-06, section 4: "strangers start with everything off").

``engines.features.derive_features`` treats any non-empty value that is not a
shipped placeholder as configured, so an example that leaves a sample live (a
MarianMT address nobody runs, a model name) would advertise a feature to the
app or probe a non-existent service at every start. Both examples therefore:

* set ``FEATURE_FACETIME/MAP/TRANSLATE/VOICE=0`` (what ``/health.features``
  advertises; an app built with no ``FEATURES`` line takes its first switch
  values from it, once), and still derive map and translate OFF when those
  four lines are deleted ("delete a line to let the relay derive it");
* leave every optional integration unset or commented out, keeping the key
  NAMES in the file so the reader knows what exists;
* leave ``FT_AUTOADMIT``/``OLLAMA_MODEL``/``MARIAN_URL`` commented out.

Step R5 (2026-10-07) added the stranger-safe defaults the code itself does not
impose (the code keeps the author's behaviour when a key is absent):

* ``IMSG_BIND=127.0.0.1`` in both files, with the two-line explanation;
* ``PYTHONUNBUFFERED=1`` in the LaunchAgent;
* ``BB_PASSWORD`` commented out in ``.env.example``, so a literal copy yields
  the AppleScript-only chain the README promises;
* placeholders that the relay recognises (``placeholders.is_placeholder``)
  wherever a secret is mandatory, and the rule explained in both files.

Step R6 (2026-10-07) added ``IMESSAGE_CLI`` (the tool that edits a sent
message) to both files, commented out: what it is, how to install it, which
grants it needs and how to switch it off.

The exact sets of live keys are pinned, so a key cannot go live by accident.

Parsed with the standard library only: ``plistlib`` for the LaunchAgent and a
dotenv-shaped line parser for ``.env.example`` (the conftest stubs ``dotenv``
out for the whole session). No value here is real.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from engines import build_chain, engine_names
from engines.features import FEATURES, derive_features
from placeholders import is_placeholder

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
PLIST_EXAMPLE = ROOT / "launchd" / "org.selfbubbles.relay.plist.example"

ALL_OFF = {feature: False for feature in FEATURES}
FEATURE_KEYS = tuple(f"FEATURE_{feature.upper()}" for feature in FEATURES)

#: Integrations the relay runs without; a present value switches them on.
#: (``IMESSAGE_CLI``, step R6, names the edit tool: documented in both
#: examples, live in neither.)
OPTIONAL_KEYS = ("FCM_CREDS", "BEEPER_TOKEN", "BEEPER_GM_ACCOUNT_LABELS", "OLLAMA_MODEL",
                 "MARIAN_URL", "HA_URL", "HA_TOKEN", "HA_LOCATIONS", "MAPKIT_TOKEN", "IMESSAGE_CLI")
#: Must never be live in an example: each one turns a feature on by itself.
DERIVING_KEYS = ("OLLAMA_MODEL", "MARIAN_URL", "HA_TOKEN", "MAPKIT_TOKEN", "BEEPER_TOKEN", "FCM_CREDS",
                 "IMESSAGE_CLI")


def parse_env_example(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines as python-dotenv would load them: comments and blank
    lines skipped, one matching pair of surrounding quotes stripped."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def without_feature_lines(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k not in FEATURE_KEYS}


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------

#: Every key that is LIVE (not commented out) in ``.env.example``.
ENV_EXAMPLE_LIVE_KEYS = {
    "IMSG_PORT", "IMSG_BIND", "IMSG_SELF", "TEXT_RELAY_LABEL", "IMSG_CHATDB", "IMSG_POLL_SECONDS",
    "IMSG_TOKEN", "APPLE_NEWS_PREVIEWS", "SEND_APPLESCRIPT_FALLBACK", "BB_URL", "BB_MARK_READ", "FCM_CREDS",
    "BEEPER_URL", "BEEPER_TOKEN", "BEEPER_GM_ACCOUNT", "BEEPER_BRIDGE_DB", "BEEPER_GM_ACCOUNT_LABELS",
    "OLLAMA_URL", "OLLAMA_KEEP_ALIVE", "HA_URL", "HA_TOKEN", "HA_LOCATIONS", "MAPKIT_TOKEN",
    *FEATURE_KEYS,
}

#: The two-line explanation both examples carry above ``IMSG_BIND``.
BIND_EXPLANATION = (
    "Address to bind: 127.0.0.1 (loopback) when the HTTPS route (Tailscale Serve, a Cloudflare Tunnel) "
    "runs on this same Mac.",
    "Use 0.0.0.0 (the built-in default when this key is absent) only if the route reaches the relay "
    "over the LAN.",
)


def test_env_example_starts_with_everything_off():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    env = parse_env_example(text)
    assert set(env) == ENV_EXAMPLE_LIVE_KEYS
    assert {k: env[k] for k in FEATURE_KEYS} == {k: "0" for k in FEATURE_KEYS}
    assert derive_features(env) == ALL_OFF
    # "delete a line to let the relay derive it": nothing derives on, because
    # no credential is live (BB_PASSWORD ships commented out since R5)
    assert derive_features(without_feature_lines(env)) == ALL_OFF
    for key in DERIVING_KEYS:
        assert not (env.get(key) or "").strip(), f"{key} must not be live in .env.example"
    assert "MARIAN_URL" not in env and "OLLAMA_MODEL" not in env and "FT_AUTOADMIT" not in env
    # the knobs are still documented, commented out
    assert "#MARIAN_URL=" in text and "#OLLAMA_MODEL=" in text and "#FT_AUTOADMIT=0" in text
    assert "#FT_AUTOADMIT=1" not in text
    for key in OPTIONAL_KEYS:
        assert key in text, key


def test_env_example_copied_verbatim_gives_the_applescript_only_chain():
    """The README promises: without a BlueBubbles password the chain is
    AppleScript alone. A literal ``cp .env.example .env`` must give exactly
    that, not a dead ``bluebubbles`` engine in front of it."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    env = parse_env_example(text)
    assert "BB_PASSWORD" not in env
    assert "#BB_PASSWORD=your-bluebubbles-server-password" in text         # documented, commented out
    assert "BB_PASSWORD=change-me" not in text
    assert engine_names(build_chain(env)) == ["applescript"]
    # uncommented but not filled in, it is still a placeholder: same chain
    assert is_placeholder("your-bluebubbles-server-password")
    assert engine_names(build_chain({**env, "BB_PASSWORD": "your-bluebubbles-server-password"})) == ["applescript"]
    assert engine_names(build_chain({**env, "BB_PASSWORD": "a-real-password"})) == ["bluebubbles", "applescript"]


def test_both_examples_document_the_edit_tool_and_leave_it_commented_out():
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    plist_text = PLIST_EXAMPLE.read_text(encoding="utf-8")
    env = parse_env_example(env_text)
    plist_env = plistlib.loads(plist_text.encode("utf-8"))["EnvironmentVariables"]
    assert "IMESSAGE_CLI" not in env and "IMESSAGE_CLI" not in plist_env
    assert "\n#IMESSAGE_CLI=/opt/homebrew/bin/imessage-cli\n" in env_text
    assert "<key>IMESSAGE_CLI</key>" in plist_text
    # the wording, read as running text (comment markers and line breaks removed)
    env_prose = " ".join(line.lstrip("#").strip() for line in env_text.splitlines())
    for prose in (" ".join(env_prose.split()), " ".join(plist_text.split())):
        for words in ("brew install beeper/tap/imessage-cli",      # how to get it
                      "Accessibility and Automation",               # what the relay's Python needs
                      "imessage-cli authorize",                     # how to see the grants
                      "drives Messages.app",                        # what it does
                      "/opt/homebrew/bin, /usr/local/bin and on the PATH",
                      "0 to keep the engine off",
                      "goes through BlueBubbles"):                  # Undo Send needs no key
            assert words in prose, words
    # neither file switches the engine on or off by itself, and the chain a
    # literal copy gives does not depend on this key being read from the file
    assert engine_names(build_chain(env)) == ["applescript"] == engine_names(build_chain(plist_env))
    assert "imessage-cli" in env_text.split("---- send engines (engines/) ----", 1)[1].split("SEND_ENGINES", 1)[0]


def test_env_example_token_is_a_recognised_placeholder_and_the_rule_is_explained():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    env = parse_env_example(text)
    assert env["IMSG_TOKEN"] == "change-me-to-a-long-random-string"
    assert is_placeholder(env["IMSG_TOKEN"])                 # so the relay refuses to start with it
    # the only live value that looks like a placeholder is the token
    assert sorted(k for k, v in env.items() if is_placeholder(v)) == ["IMSG_TOKEN"]
    for word in ("Placeholders:", "change-me, changeme, replace-with", "your-",
                 "refuses to start", "as unset", "openssl rand -hex 32"):
        assert word in text, word
    assert "IMSG_ALLOW_NO_TOKEN" not in text                 # the escape hatch is not one keystroke away


def test_both_examples_bind_to_loopback_with_the_explanation():
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    plist_text = PLIST_EXAMPLE.read_text(encoding="utf-8")
    assert parse_env_example(env_text)["IMSG_BIND"] == "127.0.0.1"
    assert plistlib.loads(plist_text.encode("utf-8"))["EnvironmentVariables"]["IMSG_BIND"] == "127.0.0.1"
    first, second = BIND_EXPLANATION
    assert f"# {first}\n# {second}\nIMSG_BIND=127.0.0.1\n" in env_text
    assert first in plist_text and second in plist_text
    assert plist_text.index(first) < plist_text.index(second) < plist_text.index("<key>IMSG_BIND</key>")


# ---------------------------------------------------------------------------
# launchd/org.selfbubbles.relay.plist.example
# ---------------------------------------------------------------------------

def test_plist_example_optional_blocks_are_commented_out():
    text = PLIST_EXAMPLE.read_text(encoding="utf-8")
    plist = plistlib.loads(text.encode("utf-8"))                     # well-formed XML, comments and all
    env = plist["EnvironmentVariables"]
    assert set(env) == {"IMSG_TOKEN", "IMSG_BIND", "PYTHONUNBUFFERED", "IMSG_SELF", "TEXT_RELAY_LABEL",
                        "BB_PASSWORD", "FT_AUTOADMIT", *FEATURE_KEYS}
    assert env["PYTHONUNBUFFERED"] == "1" and env["IMSG_BIND"] == "127.0.0.1"
    assert {k: env[k] for k in FEATURE_KEYS} == {k: "0" for k in FEATURE_KEYS}
    assert env["FT_AUTOADMIT"] == "0"
    for key in OPTIONAL_KEYS:
        assert key not in env, f"{key} is live in the plist example"
        assert key in text, f"{key} is no longer documented in the plist example"
    # the finding's stranger: edits only the token and the BlueBubbles password
    stranger = {**env, "IMSG_TOKEN": "a-real-token", "BB_PASSWORD": "a-real-password"}
    assert derive_features(stranger) == ALL_OFF
    derived = derive_features(without_feature_lines(stranger))
    assert derived == {"facetime": True, "map": False, "translate": False, "voice": True}
    # placeholders only where a value is mandatory; nothing resembling a real path or user
    assert sorted(k for k, v in env.items() if "CHANGE-ME" in v) == ["BB_PASSWORD", "IMSG_TOKEN"]
    # ... and they are placeholders the relay recognises: left as shipped, the
    # token stops the relay from starting and the password counts as unset
    assert sorted(k for k, v in env.items() if is_placeholder(v)) == ["BB_PASSWORD", "IMSG_TOKEN"]
    assert engine_names(build_chain(env)) == ["applescript"]
    assert derive_features(without_feature_lines(env)) == ALL_OFF
    assert "CHANGE-ME" in text.split("Placeholders:", 1)[1].split("-->", 1)[0]     # the rule, in the header
    for arg in (*plist["ProgramArguments"], plist["WorkingDirectory"],
                plist["StandardOutPath"], plist["StandardErrorPath"]):
        assert arg.startswith("/Users/YOU/"), arg
    assert plist["Label"] == "org.selfbubbles.relay"


def test_plist_example_header_tells_kickstart_from_bootout_bootstrap():
    """``launchctl kickstart -k`` restarts the process with the environment
    launchd already holds; an edited plist needs bootout + bootstrap. The
    header must say so, and stay a valid XML comment (no double hyphen)."""
    text = PLIST_EXAMPLE.read_text(encoding="utf-8")
    header = text.split("<!--", 1)[1].split("-->", 1)[0]
    kick = header.index("launchctl kickstart -k gui/$UID/org.selfbubbles.relay")
    out = header.index("launchctl bootout gui/$UID/org.selfbubbles.relay")
    assert kick < header.index("a kickstart is not") < out < header.rindex("launchctl bootstrap gui/$UID")
    assert "an edit to .env" in header[:out] and "does not re-read the plist" in header
    assert "--" not in header


def test_both_examples_agree_on_the_feature_defaults():
    env = parse_env_example(ENV_EXAMPLE.read_text(encoding="utf-8"))
    plist_env = plistlib.loads(PLIST_EXAMPLE.read_bytes())["EnvironmentVariables"]
    assert {k: env[k] for k in FEATURE_KEYS} == {k: plist_env[k] for k in FEATURE_KEYS}
    assert derive_features(env) == derive_features(plist_env) == ALL_OFF
