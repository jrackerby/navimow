"""Pure decision functions. IMPORTS NOTHING FROM `homeassistant` OR `mower_sdk`.

Everything here takes primitives and returns primitives, so the whole of this
integration's judgement -- what activity a raw cloud string means, whether the
mower is reachable, whether it is faulted, and what unique_id an entity claims
-- is testable by `tools/test_navimow_*.py` with no Home Assistant install and
no vendor SDK. That is the same split household_state draws at resolver.py.

THE UNIQUE_ID FUNCTIONS ARE LOAD-BEARING AND MUST NOT BE "TIDIED".
This component replaces segwaynavimow/NavimowHA in place, on the same domain.
The entity registry keys rows on (platform, domain, unique_id), so the ids
`lawn_mower.navimow_x430_2` and `sensor.navimow_x430_battery_2` -- and every
dashboard, template and automation already referencing them -- survive the swap
if and only if we re-emit NavimowHA's exact strings. Its shapes were
`f"{DOMAIN}_{device_id}"` for the mower and `f"{DOMAIN}_{device_id}_{key}"` for
the battery sensor (key="battery"). Change either and HA mints a fresh row, the
old one is orphaned, and the new one takes `_3` because the id is occupied: a
platform assigning entity_id takes the next free suffix and never reclaims the
one it lost. test_navimow_migration.py pins both strings.
"""

from __future__ import annotations

DOMAIN = "navimow"


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def mower_unique_id(device_id: str) -> str:
    """The lawn_mower entity's unique_id. NavimowHA's exact shape."""
    return f"{DOMAIN}_{device_id}"


def sensor_unique_id(device_id: str, key: str) -> str:
    """A keyed entity's unique_id. NavimowHA's exact shape for key='battery'.

    Every key OTHER than 'battery' is new to this component and therefore
    mints a new registry row, which is correct and intended -- only 'battery'
    is inheriting an existing one.
    """
    return f"{DOMAIN}_{device_id}_{key}"


# --------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------

# mower_sdk.models normalises the cloud's raw vehicleState strings ("isDocked",
# "isRunning", "Offline", ...) down to these canonical values before we see
# them. This maps canonical -> homeassistant.components.lawn_mower.
# LawnMowerActivity, whose complete membership is ERROR / PAUSED / MOWING /
# DOCKED / RETURNING (read from core's lawn_mower/const.py, not remembered).
#
# `None` means "we do not know", which HA renders as state `unknown`. It is a
# real answer and it is NOT `error`.
CANONICAL_TO_ACTIVITY: dict[str, str | None] = {
    "mowing": "mowing",
    "paused": "paused",
    "returning": "returning",
    "docked": "docked",
    "charging": "docked",
    # IDLE -> DOCKED is inherited from NavimowHA and deliberately kept. It is
    # an assumption, not a reading: the cloud's `isIdel`/`isIdle`/`Self-Checking`
    # say the mower is doing nothing, not where it is standing. Keeping it
    # preserves the behaviour the sunroom board and robot-fleet-card.js were
    # built against; the honest value is published alongside on
    # sensor.<name>_raw_state, which never guesses.
    "idle": "docked",
    "error": "error",
    # THE BUG THIS COMPONENT EXISTS TO FIX, AND THE ONLY MAPPING THAT CHANGES.
    # NavimowHA's const.py had `"unknown": "error"`, while mower_sdk's
    # _RAW_STATE_TO_CANONICAL maps BOTH "Offline" and "offline" to "unknown".
    # Composed, a mower that had merely lost its uplink reported a fault
    # needing assistance. Absent and unreachable do not collapse.
    # Reachability is binary_sensor.<name>_connectivity's job and is answered
    # there, correctly, in both directions.
    "unknown": None,
}


def resolve_activity(canonical_state: str | None) -> str | None:
    """Canonical cloud state -> LawnMowerActivity value, or None for unknown.

    An unrecognised string is None rather than 'error' for the same reason
    'unknown' is: a state this component has never seen is a gap in this
    table, and reporting the mower as faulted because our own map is short
    would be a monitor inventing its subject's condition.
    """
    if canonical_state is None:
        return None
    return CANONICAL_TO_ACTIVITY.get(canonical_state)


def is_reachable(canonical_state: str | None, device_online: bool | None) -> bool | None:
    """Is the mower itself reachable by the CLOUD?

    Distinct from whether WE can reach the cloud -- that is availability, and
    it is handled by the coordinator's last_update_success. This answers only
    the question the cloud is answering for us.

    Returns None when neither signal is present, so 'we were not told' does
    not silently render as 'offline' -- `ok at zero` and `could not read` are
    different values at the source.
    """
    if canonical_state == "unknown":
        return False
    if device_online is not None:
        return bool(device_online)
    if canonical_state is None:
        return None
    return True


def is_charging(canonical_state: str | None) -> bool | None:
    """Charging is a state the mower reports and LawnMowerActivity cannot hold.

    Core's activity enum collapses charging into DOCKED (there is no CHARGING
    member), so a docked-and-full mower and a docked-and-charging one are one
    value on the mower entity. This is the axis that separates them.
    """
    if canonical_state is None:
        return None
    return canonical_state == "charging"


# --------------------------------------------------------------------------
# Fault
# --------------------------------------------------------------------------

# mower_sdk.models.MowerError's members, as strings so this module stays
# import-free. 'none' is the SDK's explicit no-fault value and is NOT absence.
NO_ERROR = "none"


def normalise_error(error: dict | None) -> tuple[str | None, str | None]:
    """(code, message) from the SDK's error dict, or (None, None) if absent.

    An error dict carrying code 'none' collapses to (None, None): the SDK
    builds that shape from DeviceStatus.error_code and it means no fault.
    """
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    if not code or code == NO_ERROR:
        return None, None
    message = error.get("message")
    return str(code), str(message) if message else None


def is_problem(error: dict | None, canonical_state: str | None) -> bool | None:
    """BinarySensorDeviceClass.PROBLEM -- true when the mower needs a human.

    THE SECOND SOURCE A DASHBOARD NEEDS AND DID NOT HAVE. With the mower
    reporting through one entity, a "Mowing" reading has nothing to be checked
    against, so a careful surface drops it to neutral and says single source --
    an unpaired state can never read good. An error channel that is independent
    of the activity string is exactly that missing second source.

    Being OFFLINE is not a problem in this sense -- nobody has to walk to the
    garden about it -- so it returns False here and is reported by the
    connectivity axis instead. That separation is the whole point of the fix.
    """
    code, _ = normalise_error(error)
    if code is not None:
        return True
    if canonical_state == "error":
        return True
    if canonical_state is None or canonical_state == "unknown":
        # We have no fault reading at all. Not False: absent is not ok.
        return None
    return False


# --------------------------------------------------------------------------
# Events (the MQTT channel NavimowHA never subscribed a callback to)
# --------------------------------------------------------------------------

# EventEntity requires its event_types declared up front, and an event whose
# type is not in the list is rejected by core. The cloud's vocabulary on
# /downlink/vehicle/<id>/realtimeDate/event is not documented anywhere we can
# read, so rather than guess a list and silently drop everything outside it,
# the entity declares these buckets and every unrecognised event lands in
# `other` with its raw name preserved in the attributes.
EVENT_TYPES: tuple[str, ...] = ("error", "warning", "info", "other")


def event_bucket(level: str | None, event: str | None) -> str:
    """Map an SDK DeviceEventMessage onto a declared event_type.

    Buckets on LEVEL first because that is the field the cloud sets
    deliberately; the event NAME is a free string and is carried through as an
    attribute rather than being pattern-matched into meaning here. A scan
    keyed on one pattern is not an audit -- so this does not
    pretend to classify names it has never seen.
    """
    if level:
        lowered = str(level).strip().lower()
        if lowered in ("error", "fatal", "critical", "alarm"):
            return "error"
        if lowered in ("warn", "warning"):
            return "warning"
        if lowered in ("info", "information", "notice", "debug"):
            return "info"
    if event and str(event).strip().lower().startswith("error"):
        return "error"
    return "other"


# --------------------------------------------------------------------------
# MQTT endpoint
# --------------------------------------------------------------------------


def mqtt_endpoint(mqtt_info: dict) -> tuple[str | None, int, str | None]:
    """Resolve (broker, port, ws_path) from the cloud's MQTT descriptor.

    NO DEFAULT BROKER. NavimowHA fell back to a constant `mqtt.navimow.com`
    carrying its own `TODO: needs the actual address` comment, so a malformed
    response produced a connection attempt against a hostname nobody had
    verified rather than a setup failure naming the real problem.

    A HOST FIELD MAY CARRY A WHOLE URL, AND TAKING IT LITERALLY IS SILENT.
    Observed live on an X430: `mqttHost` was `wss://mqtt-fra.navimow.com` and
    no `mqttUrl` came back at all. The earlier resolver returned that string
    unchanged as a bare hostname with port 1883 and no ws_path, so paho was
    asked for a TCP connection to a name containing `wss://`, which cannot
    resolve. Nothing raised: `connect_async` retries in the background, so
    setup reported ready, every entity came up on the HTTP fallback, and the
    push channel was never once connected. It read as a quiet mower rather
    than as a broken endpoint, which is why the diagnostics dump needed
    `mqtt.connected` before this was findable at all.

    So the scheme decides, wherever it is written. A field carrying one is
    parsed; only a field carrying none is treated as a hostname.
    """
    host = mqtt_info.get("mqttHost")
    # A host with no scheme is the only thing that may be used as a hostname.
    bare_host = host if host and "://" not in host else None
    candidate = mqtt_info.get("mqttUrl") or host
    if not candidate:
        return bare_host, 1883, None

    parsed = _urlparse(candidate)
    if parsed[0] not in ("ws", "wss"):
        # Not a websocket endpoint. Fall back to plain MQTT, but ONLY on a
        # host we can actually hand to a TCP connect.
        return bare_host, 1883, None

    scheme, hostname, url_port, path, query = parsed
    port = url_port or (443 if scheme == "wss" else 80)
    if query:
        path = f"{path}?{query}"
    # Prefer a clean mqttHost when there is one; fall back to the hostname the
    # URL carried. Preferring mqttHost unconditionally is what produced the
    # `wss://`-prefixed broker above.
    return bare_host or hostname, port, path or "/"


def _urlparse(value: str) -> tuple[str | None, str | None, int | None, str, str]:
    """Minimal URL split, so model.py keeps importing nothing.

    Returns (scheme, hostname, port, path, query). Only the shapes this
    descriptor actually produces are handled -- scheme://host[:port][/path][?q]
    -- and anything else falls out as a null scheme, which the caller treats
    as "not a websocket endpoint" rather than guessing.
    """
    text = str(value)
    if "://" not in text:
        return None, None, None, "", ""
    scheme, _, rest = text.partition("://")
    scheme = scheme.strip().lower()
    authority, slash, remainder = rest.partition("/")
    path = f"{slash}{remainder}" if slash else ""
    path, _, query = path.partition("?")
    # Strip userinfo, then split an optional port off the host.
    authority = authority.rpartition("@")[2]
    port: int | None = None
    if ":" in authority and not authority.endswith("]"):
        maybe_host, _, maybe_port = authority.rpartition(":")
        if maybe_port.isdigit():
            authority, port = maybe_host, int(maybe_port)
    return scheme, (authority or None), port, path, query
