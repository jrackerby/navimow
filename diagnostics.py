"""Config entry diagnostics.

REDACTION IS THE WHOLE JOB HERE. This dump is what a user pastes into an issue
tracker, so anything derived from a credential or a location has to be gone
before it is written -- not masked. NavimowHA's `first2***last2` masking is the
counter-example this file exists not to repeat: a partial secret in a bug
report is a secret in a bug report.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NavimowConfigEntry

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
    devices: list[dict[str, Any]] = []
    for device_id, coordinator in runtime.coordinators.items():
        state = coordinator.get_device_state()
        attributes = coordinator.get_device_attributes()
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
                # THE ONLY WINDOW ONTO AN UNDOCUMENTED CHANNEL (GH-548).
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
            }
        )
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "devices": devices,
    }
