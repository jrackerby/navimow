"""Stop.

mower_sdk.models.MowerCommand has START, PAUSE, DOCK, RESUME and STOP, and
mower_sdk.api maps STOP to `action.devices.commands.StartStop {on: false}`.
Core's LawnMowerEntityFeature has exactly three members -- START_MOWING,
PAUSE, DOCK (read from lawn_mower/const.py) -- so STOP cannot be exposed on
the mower entity at all. NavimowHA left it unreachable. A button is the
smallest surface that does not require the user to write a script.

STOP is not PAUSE: pause holds the job for resuming, stop ends it.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NavimowConfigEntry
from .const import KEY_STOP
from .entity import NavimowEntity
from .model import sensor_unique_id

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavimowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        NavimowStopButton(coordinator)
        for coordinator in entry.runtime_data.coordinators.values()
    )


class NavimowStopButton(NavimowEntity, ButtonEntity):
    _attr_translation_key = KEY_STOP

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = sensor_unique_id(coordinator.device.id, KEY_STOP)

    async def async_press(self) -> None:
        from mower_sdk.models import MowerCommand

        await self.coordinator.async_ensure_valid_token()
        try:
            await self.coordinator.api.async_send_command(
                self._device_id, MowerCommand.STOP
            )
        except Exception as err:  # noqa: BLE001 -- vendor SDK raises broadly
            raise HomeAssistantError(
                f"Navimow refused stop for {self.coordinator.device.name}: {err}"
            ) from err
        # THE SAME DEFECT LIVED HERE TOO. #28 named lawn_mower's `_async_command`,
        # but STOP is a command on the same cloud with the same follow-up read,
        # and a plain `async_request_refresh()` is refused by the coordinator's
        # HTTP fallback gates exactly as it was there. Fixing one of two
        # identical paths is a fix that works on one instance.
        await self.coordinator.async_request_command_refresh()
