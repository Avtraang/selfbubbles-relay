"""Shipped placeholder values are not credentials.

``.env.example`` and ``launchd/org.selfbubbles.relay.plist.example`` ship a
few values that only mark where a secret goes (``change-me-to-a-long-random-string``,
``CHANGE-ME``).  Those strings are public, so the relay never uses one as a
token or password:

* ``IMSG_TOKEN``: ``relay.py`` refuses to start with a placeholder (or with no
  token at all) unless ``IMSG_ALLOW_NO_TOKEN=1``, and never accepts a
  placeholder as the token.
* ``BB_PASSWORD`` / ``BEEPER_TOKEN`` / ``HA_TOKEN`` / ``MAPKIT_TOKEN``: a
  placeholder counts as unset wherever the value gates behaviour (the send
  chain, the advertised features, the Beeper watcher, ``/locations``, ``/map``),
  and the doctor says so.

The rule is deliberately small and case-insensitive: a value is a placeholder
when, after stripping surrounding whitespace, it starts with ``change-me``,
``changeme``, ``replace-with`` or ``your-`` (which covers the plist example's
``CHANGE-ME``).  A real secret that happens to start with one of those words is
treated as a placeholder too; pick another one.

No imports and no I/O: this module is a leaf so that ``beeper.py``, the
``engines`` package and ``relay.py`` can all use it without importing each
other.
"""

from __future__ import annotations

#: Lower-case prefixes that mark a shipped placeholder.
PLACEHOLDER_PREFIXES = ("change-me", "changeme", "replace-with", "your-")


def is_placeholder(value: object) -> bool:
    """True when ``value`` is one of the shipped placeholder strings (see the
    module docstring).  ``None``, the empty string and non-strings are not
    placeholders: they are simply unset."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    return v == "CHANGE-ME" or v.lower().startswith(PLACEHOLDER_PREFIXES)


def drop_placeholder(value: str | None) -> str:
    """``value`` exactly as given, or ``""`` when it is ``None`` or a
    placeholder.  For configuration values a placeholder must never switch on."""
    if value is None or is_placeholder(value):
        return ""
    return value
