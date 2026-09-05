"""Pure decision functions. IMPORTS NOTHING FROM `homeassistant` OR `mower_sdk`.

Everything here takes primitives and returns primitives, so the whole of this
integration's judgement -- what activity a raw cloud string means, whether the
mower is reachable, whether it is faulted, and what unique_id an entity claims
-- is testable by `tools/test_navimow_*.py` with no Home Assistant install and
no vendor SDK. That is the same split household_state draws at resolver.py.

THE UNIQUE_ID FUNCTIONS ARE LOAD-BEARING AND MUST NOT BE "TIDIED".
This component replaces segwaynavimow/NavimowHA in place, on the same domain.
The entity registry keys rows on (platform, domain, unique_id), so the ids
`lawn_mower.navimow_x430_2` and `sensor.navimow_x430_battery_2` -- referenced
from custom_templates/net_tiers.jinja:52, dashboards/sunroom-panel.yaml:50,
dashboards/control-card-test.yaml:54 and www/robot-fleet-card.js:225 -- survive
the swap if and only if we re-emit NavimowHA's exact strings. Its shapes were
`f"{DOMAIN}_{device_id}"` for the mower and `f"{DOMAIN}_{device_id}_{key}"` for
the battery sensor (key="battery"). Change either and HA mints a fresh row, the
old one is orphaned, and the new one takes `_3` because the id is occupied
(TOOLS.md: a platform assigning entity_id takes _2 when the id is taken and
never reclaims it). test_navimow_migration.py pins both strings.
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
    # needing assistance. LAW.md §11: absent and unreachable do not collapse.
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
    not silently render as 'offline' (LAW.md §11: `ok at zero` and `could not
    read` are different values at the source).
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

    THE SECOND SOURCE www/robot-fleet-card.js SAYS IT DOES NOT HAVE. That
    card's own header records the gap: "the mower reports through one entity,
    so a Mowing reading drops to neutral and says single source (LAW 7: an
    unpaired state can never read good) -- verified live, this device has no
    second source to check against." An error channel that is independent of
    the activity string is exactly that source.

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
        # We have no fault reading at all. Not False -- see LAW.md §11.
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
    keyed on one pattern is not an audit (LAW.md §5) -- so this does not
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
