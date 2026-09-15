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


def is_reachable(
    canonical_state: str | None,
    mqtt_push_is_recent: bool | None = None,
) -> bool | None:
    """Is the mower itself reachable by the CLOUD?

    Distinct from whether WE can reach the cloud -- that is availability, and
    it is handled by the coordinator's last_update_success. This answers only
    the question the cloud is answering for us.

    Returns None when no signal is present, so 'we were not told' does not
    silently render as 'offline' -- `ok at zero` and `could not read` are
    different values at the source.

    THERE IS NO `online` FLAG, AND THIS AXIS NO LONGER TAKES ONE. Through
    1.5.0 a `device_online` argument came off the `authList` device record
    via mower_sdk's `Device.online`. That SDK field is
    `data.get("online", False)`, and the raw record was read on 2026-09-14
    (shape only, through the same host-side probe that cracked the MQTT
    endpoint): it carries exactly four keys -- `firmware`, `id`, `model`,
    `name`. The vendor never sends `online`. Every `False` the component ever
    read off it was the SDK's own default, and `True` was unreachable, so the
    argument carried no information in either direction and is deleted
    rather than kept "for the True case". (The same read shows the vendor's
    firmware key is `firmware`, which the SDK does not map -- that is why
    `Device.firmware_version` is always empty.)

    History, for the recorder: the flag read `off` unbroken through two
    complete mowing sessions on 2026-09-12, and that reading was taken in
    #7's history as proof the mower was asleep. It was a default.

    Reachability is ASSERTED from live evidence and denied only from a
    reading that means it: a state frame pushed for this device inside the
    staleness window is the cloud telling us it is in contact with the mower,
    and `unknown` is the canonical offline state.
    """
    if mqtt_push_is_recent:
        return True
    if canonical_state == "unknown":
        return False
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
# HTTP fallback gating
# --------------------------------------------------------------------------


def should_http_fetch(
    forced: bool,
    mqtt_is_stale: bool,
    seconds_since_http_fetch: float | None,
    min_interval: float,
) -> bool:
    """Should this refresh pay for a REST status read?

    TWO GATES STAND BETWEEN A REFRESH AND A READ, AND A COMMAND HAS TO CLEAR
    BOTH. The unattended poll is rate-limited by `min_interval` so a broker
    outage cannot turn the fallback into a poll of its own, and it is skipped
    entirely while MQTT is fresh because a pushed state is the better reading.
    Both are correct for the poll and both are wrong for the refresh a command
    schedules behind it:

      * The hourly floor made that refresh a no-op for up to an hour.
        Measured 2026-09-15: `lawn_mower.start_mowing` at 10:08 EDT was
        accepted, the mower left the dock, and the entity read `docked` until
        a config-entry reload forced a fresh read at 10:16.
      * The staleness gate closes it for the first five minutes after any
        frame on any channel -- which is precisely the window the follow-up
        refresh runs in. Clearing only the floor would have left the same
        symptom behind a different gate.

    So `forced` clears both, once, and prices itself honestly: one extra REST
    call per user action, and nothing changes for the unattended poll. The
    forced read still stamps `min_interval`'s clock, because it IS an HTTP
    read and the floor exists to protect the cloud from us, not to protect us
    from the floor.

    `seconds_since_http_fetch` is None -- never 0 -- when no read has ever been
    taken: never-fetched and just-fetched are opposite answers here.
    """
    if forced:
        return True
    if not mqtt_is_stale:
        return False
    return seconds_since_http_fetch is None or seconds_since_http_fetch > min_interval


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

    AND A SCHEME-LESS `mqttUrl` IS THE WEBSOCKET PATH, NOT NOISE. That is the
    vendor's own contract: navimow-sdk's `MowerClient.async_refresh_mqtt_info`
    takes `mqttHost` as the `wss://` host and hands `mqttUrl` to paho
    VERBATIM as `ws_path`. Measured live on the same X430, once the value was
    finally read rather than reasoned about: `mqttHost` =
    "wss://mqtt-fra.navimow.com", `mqttUrl` = "/mqtt/<userId>". The resolver
    that fixed the scheme defect skipped that field for carrying no scheme
    and defaulted the path to "/", and the gateway in front of the broker
    (an Azure Application Gateway) answers "/" with 502 and "/mqtt/<anything>"
    with 101. paho retries a failed upgrade silently in its own thread, so
    the endpoint was right, the port was right, TLS was right, and the
    session still never came up -- with no CONNACK and no disconnect to say
    why. A path that begins with "/" is taken as the path; anything else
    scheme-less is still not guessed at.
    """
    host = mqtt_info.get("mqttHost")
    # A host with no scheme is the only thing that may be used as a hostname.
    bare_host = host if host and "://" not in host else None
    url_path = _bare_ws_path(mqtt_info.get("mqttUrl"))

    # EVERY FIELD THAT COULD CARRY ONE IS TRIED, AND A FIELD THAT DOES NOT
    # YIELD ONE IS SKIPPED RATHER THAN FATAL. This loop replaces a version
    # that picked `mqttUrl` when present and gave up if it was not a websocket
    # URL -- which on the live descriptor returned no broker at all and put the
    # entry into setup_retry, because this cloud sends BOTH: an `mqttUrl` that
    # is not a ws/wss URL, and an `mqttHost` that is. Preferring either field
    # unconditionally gets one of the two real payloads wrong.
    for candidate in (mqtt_info.get("mqttUrl"), host):
        if not candidate:
            continue
        scheme, hostname, url_port, path, query = _urlparse(candidate)
        if scheme not in ("ws", "wss") or not hostname:
            continue
        port = url_port or (443 if scheme == "wss" else 80)
        if query:
            path = f"{path}?{query}"
        # Prefer a clean mqttHost when there is one; fall back to the hostname
        # the URL carried. Preferring mqttHost unconditionally is what produced
        # the `wss://`-prefixed broker handed to a TCP connect.
        # The URL's own path wins when it has one; then the vendor's path
        # field; "/" only when neither said anything.
        return bare_host or hostname, port, path or url_path or "/"

    # No websocket endpoint in any field. Plain MQTT, but ONLY on a host we can
    # actually hand to a TCP connect -- never a scheme-bearing string.
    return bare_host, 1883, None


def _bare_ws_path(value: object) -> str | None:
    """A scheme-less `mqttUrl` that begins with "/" is a websocket path.

    Nothing else scheme-less is interpreted: "host:port" is not a path and
    would not be one on the wire either, and the resolver has already been
    corrected once against a shape nobody had read.
    """
    if not value:
        return None
    text = str(value)
    if "://" in text or not text.startswith("/"):
        return None
    return text


def mqtt_descriptor_shape(mqtt_info: dict) -> dict:
    """Describe the cloud's MQTT descriptor WITHOUT revealing any value.

    THIS EXISTS BECAUSE NOT HAVING IT COST A ROLLBACK. The endpoint resolver
    was corrected against descriptor shapes that were reasoned about rather
    than read -- the live payload was never visible anywhere, because every
    field that carries it sits beside `userName` and `pwdInfo` and the whole
    dict was therefore never dumped. The corrected resolver was right about
    the shape it was shown and wrong about the one the cloud actually sends,
    setup went to setup_retry on the live instance, and it had to be reverted.

    Key NAMES and URL SCHEMES are not credentials. Publishing those two makes
    the payload's shape readable from a diagnostics dump, which is the read
    that should have preceded the fix.
    """
    keys = sorted(str(k) for k in mqtt_info)
    schemes = {}
    for field in ("mqttUrl", "mqttHost"):
        value = mqtt_info.get(field)
        if not value:
            continue
        scheme = _urlparse(value)[0]
        # "" means the field is present and carries no scheme at all -- a bare
        # hostname. That is a different finding from the field being absent.
        schemes[field] = scheme or ""
    # Whether the scheme-less mqttUrl is a PATH is the one shape fact the
    # resolver above turns on, so the dump says so -- as a boolean, not a
    # value: the live path carries the account's userId.
    return {
        "keys": keys,
        "schemes": schemes,
        "mqttUrl_is_path": _bare_ws_path(mqtt_info.get("mqttUrl")) is not None,
    }


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
