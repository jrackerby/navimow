"""Config entry diagnostics.

REDACTION IS THE WHOLE JOB HERE. This dump is what a user pastes into an issue
tracker, so anything derived from a credential or a location has to be gone
before it is written -- not masked. NavimowHA's `first2***last2` masking is the
counter-example this file exists not to repeat: a partial secret in a bug
report is a secret in a bug report.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NavimowConfigEntry

# The only `entry.data` keys this component reads; the OAuth helper takes both
# and nothing here touches `entry.data` anywhere else. Everything beside them
# was written by segwaynavimow/NavimowHA into the entry this component
# inherited, and is dead storage -- see `entry_keys_unused` below.
ENTRY_KEYS_USED = frozenset({"token", "auth_implementation"})

TO_REDACT = {
    "access_token",
    "refresh_token",
    "token",
    "client_id",
    "client_secret",
    "userName",
    "pwdInfo",
    "serial_number",
    "mac_address",
    "position",
    "latitude",
    "longitude",
    "lat",
    "lng",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NavimowConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    now = time.monotonic()
    devices: list[dict[str, Any]] = []
    for device_id, coordinator in runtime.coordinators.items():
        state = coordinator.get_device_state()
        attributes = coordinator.get_device_attributes()
        last_push = (coordinator.data or {}).get("last_mqtt_monotonic")
        devices.append(
            {
                "id_present": bool(device_id),
                "model": coordinator.device.model,
                "firmware_version": coordinator.device.firmware_version,
                "online": getattr(coordinator.device, "online", None),
                "last_update_success": coordinator.last_update_success,
                "source": (coordinator.data or {}).get("source"),
                "state": async_redact_data(state.to_dict(), TO_REDACT)
                if state is not None
                else None,
                # THE ONLY WINDOW ONTO AN UNDOCUMENTED CHANNEL.
                # /downlink/vehicle/<id>/realtimeDate/attributes carries a
                # free-form dict that no published source characterises, and
                # it is the one place a mowing schedule or a blade/service
                # figure could still be arriving -- neither is anywhere on the
                # REST surface, whose command vocabulary is Google Smart Home
                # and therefore has no such concept to carry.
                #
                # It is dumped WHOLE and unfiltered on purpose. Selecting keys
                # here would require knowing which ones matter, which is the
                # very thing this exists to find out, and a redaction pass that
                # dropped an unrecognised key would delete the finding. Values
                # go through the same TO_REDACT pass as everything else, so a
                # credential or a coordinate keyed under a name we already know
                # is still removed; anything genuinely new arrives intact and
                # visible, which is the point.
                "attributes": async_redact_data(
                    dict(attributes.attributes or {}), TO_REDACT
                )
                if attributes is not None
                else None,
                "attributes_present": attributes is not None,
                # NEVER READS ZERO FOR "NO PUSH". None means this device has
                # received nothing over MQTT since setup, which is a different
                # answer from "a push arrived 0 seconds ago" and is the one
                # that distinguishes a silent mower from a silent session.
                "seconds_since_mqtt_push": (
                    round(now - last_push, 1) if last_push is not None else None
                ),
            }
        )
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        # THE INHERITED-KEY TRAP, NAMED RATHER THAN HIDDEN. This entry was
        # created by NavimowHA and still stores its `mqtt_broker`, `mqtt_port`,
        # `mqtt_username`, `mqtt_password` and `api_base_url` keys. This
        # component reads none of them -- it resolves the broker and its
        # credentials from `mqtt/userInfo/get/v2` on every setup -- so
        # `mqtt_username: null` in the dump above says nothing whatever about
        # whether MQTT is authenticated. It has already been read as proof
        # that it was not. Listing the dead keys costs one line and answers
        # that misreading beside the thing that caused it; deleting them from
        # the dump would instead hide what the entry really holds.
        "entry_keys_unused": sorted(set(entry.data) - ENTRY_KEYS_USED),
        # THE LIVE SESSION, which is what every question about this channel
        # actually turns on. `is_connected` is a public NavimowSDK property
        # (it forwards to paho's own `is_connected()`), so this needs no reach
        # into `sdk._mqtt`. Without it a dump showing no attributes and no
        # events cannot say whether the broker session is down or the mower is
        # simply docked and asleep with nothing to publish -- the two have
        # identical symptoms and opposite fixes.
        "mqtt": {
            "connected": runtime.sdk.is_connected if runtime.sdk is not None else None,
            "broker": runtime.mqtt_broker,
            "port": runtime.mqtt_port,
            "transport": runtime.mqtt_transport,
            # WHAT THE CLOUD ACTUALLY SENT, in the only form that is safe to
            # print: key names and URL schemes, never a value. The endpoint
            # resolver was once corrected against a descriptor shape nobody
            # had read, and the correction was wrong about the real one.
            "descriptor": runtime.mqtt_descriptor,
        },
        "devices": devices,
    }
