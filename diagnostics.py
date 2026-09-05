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
            }
        )
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "devices": devices,
    }
