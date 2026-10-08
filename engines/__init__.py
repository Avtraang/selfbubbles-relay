"""Send engines for the relay (selfbubbles plan 2026-10-06, section 4).

    base         the interface: Capability, SendResult, SendEngine, FaceTimeBridge,
                 EngineError, Unsupported
    bluebubbles  BlueBubbles Private API (every capability except edit)
    applescript  Messages.app scripting (text + attachments; the floor)
    beeper       Google Messages via Beeper Desktop (``bp:`` guids; text + reply)
    imessage_cli Beeper's imessage-cli driving Messages.app (edit only)
    chain        build_chain(env), deliver(...), DeliveryError
    features     derive_features(env) for /health
"""

from engines.base import (CAPABILITY_VERB, Capability, EngineError, FaceTimeBridge,
                          SendEngine, SendResult, Unsupported, is_beeper_guid)
from engines.chain import (DEFAULT_ORDER, DeliveryError, build_chain, deliver,
                           engine_names, first_with, imessage_capabilities, no_engine)
from engines.features import FEATURES, derive_features

#: ``/health.protocol``: bumped when the relay's HTTP contract changes in a
#: way the app must know about.
PROTOCOL = 1

__all__ = [
    "CAPABILITY_VERB", "Capability", "DEFAULT_ORDER", "DeliveryError", "EngineError",
    "FEATURES", "FaceTimeBridge", "PROTOCOL", "SendEngine", "SendResult", "Unsupported",
    "build_chain", "deliver", "derive_features", "engine_names", "first_with",
    "imessage_capabilities", "is_beeper_guid", "no_engine",
]
