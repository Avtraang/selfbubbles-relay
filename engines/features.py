"""``/health.features``: what this relay can offer the app, derived from its
configuration (selfbubbles plan 2026-10-06, section 4 and objection 1).

``derive_features(env)`` reads the RAW environment (``os.environ`` after
``.env``), not the relay's defaulted globals, so an absent key means the
feature is unavailable:

* ``facetime`` = ``BB_PASSWORD`` set (the FaceTime endpoints are BlueBubbles
  ones).  NOT keyed on ``FT_AUTOADMIT``, which only drives the Mac-side
  auto-admit helper and is ``0`` in the owner's own plist.
* ``map``      = ``MAPKIT_TOKEN`` and ``HA_TOKEN`` both set
* ``translate``= ``OLLAMA_MODEL`` or ``MARIAN_URL`` set
* ``voice``    = ``facetime``

A credential that is still a shipped placeholder (``BB_PASSWORD=change-me``,
``HA_TOKEN=CHANGE-ME``, ...; see ``placeholders.py``) counts as not set.

``FEATURE_FACETIME`` / ``FEATURE_MAP`` / ``FEATURE_TRANSLATE`` /
``FEATURE_VOICE`` override the derived value when set (non-empty):
``1``/``true``/``yes``/``on`` is on, anything else is off.  The flags drive
the app's UI only: every router stays mounted (objection 15).
"""

from __future__ import annotations

from collections.abc import Mapping

from placeholders import is_placeholder

FEATURES = ("facetime", "map", "translate", "voice")

_TRUE = {"1", "true", "yes", "on"}


def _set(env: Mapping[str, str], key: str) -> bool:
    return bool((env.get(key) or "").strip())


def _secret_set(env: Mapping[str, str], key: str) -> bool:
    """``_set`` for a credential: a shipped placeholder is not a credential."""
    return _set(env, key) and not is_placeholder(env.get(key))


def _override(env: Mapping[str, str], feature: str) -> bool | None:
    raw = (env.get(f"FEATURE_{feature.upper()}") or "").strip()
    if not raw:
        return None
    return raw.lower() in _TRUE


def derive_features(env: Mapping[str, str]) -> dict[str, bool]:
    facetime = _secret_set(env, "BB_PASSWORD")
    derived = {
        "facetime": facetime,
        "map": _secret_set(env, "MAPKIT_TOKEN") and _secret_set(env, "HA_TOKEN"),
        "translate": _set(env, "OLLAMA_MODEL") or _set(env, "MARIAN_URL"),
        "voice": facetime,
    }
    out: dict[str, bool] = {}
    for feature in FEATURES:
        forced = _override(env, feature)
        out[feature] = derived[feature] if forced is None else forced
    return out
