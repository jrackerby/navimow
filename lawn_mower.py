"""The mower entity. Inherits NavimowHA's unique_id so its id does not move."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NavimowConfigEntry
from .const import BRAND_ICON_URL
from .entity import NavimowEntity
from .model import mower_unique_id, resolve_activity

_LOGGER = logging.getLogger(__name__)

# Commands are one round trip to a cloud that serialises them per device;
# firing them concurrently gains nothing and can race the state read that
# follows. Quality scale `parallel-updates`.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavimowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        NavimowLawnMower(coordinator)
        for coordinator in entry.runtime_data.coordinators.values()
    )


class NavimowLawnMower(NavimowEntity, LawnMowerEntity):
    """Start / pause / dock. Core's LawnMowerEntityFeature has no other members."""

    _attr_name = None  # the device's own name, via _attr_has_entity_name
    # THE BRAND MARK ON THE TILE, AND IT IS ONLY ON THIS ONE ENTITY.
    # `entity_picture` outranks the icon wherever a surface renders one, so
    # putting it on the whole set would replace the battery, signal and error
    # icons with a logo and cost every one of them the meaning its icon
    # carries. This is the entity that IS the mower, so it is the one the
    # branding belongs to.
    #
    # THE COST, STATED RATHER THAN DISCOVERED: a lawn_mower's default icon
    # tracks its activity, and a fixed picture does not. Docked, mowing and
    # errored now look identical on a tile that shows only the picture. That is
    # Joel's call and it is one attribute to remove; the activity is still on
    # the entity's state, and binary_sensor.<name>_problem still carries the
    # fault on its own axis, which is where a surface should have been reading
    # it anyway.
    _attr_entity_picture = BRAND_ICON_URL
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = mower_unique_id(coordinator.device.id)

    @property
    def activity(self) -> LawnMowerActivity | None:
        """None -- rendering as `unknown` -- when we cannot say.

        NavimowHA returned ERROR here for the cloud's own "Offline" string.
        model.resolve_activity() carries the full reasoning; the short version
        is that a mower off the network is not a mower needing assistance.
        """
        value = resolve_activity(self._canonical_state)
        return LawnMowerActivity(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Deliberately thin.

        NavimowHA carried battery, signal_strength, position, error and
        metrics here because it had nowhere else to put them. Each of those is
        now its own entity, where it is recorded, graphable and usable as a
        trigger -- an attribute is none of those things. What is left is the
        pair that describes THIS READING rather than the mower: which
        transport delivered it, and the vendor's own untranslated state
        string, which is the thing to quote in a bug report.
        """
        state = self._state
        if state is None:
            return {}
        attrs: dict[str, Any] = {"source": (self.coordinator.data or {}).get("source")}
        metrics = getattr(state, "metrics", None) or {}
        if metrics.get("raw_state"):
            attrs["raw_state"] = metrics["raw_state"]
        return attrs

    # -- commands ---------------------------------------------------------

    async def _async_command(self, command_name: str) -> None:
        """Send one command.

        Quality scale `action-exceptions`: a failure the user caused or can
        act on surfaces as HomeAssistantError with the vendor's reason in it,
        not as an unhandled traceback in the log.
        """
        from mower_sdk.models import MowerCommand

        await self.coordinator.async_ensure_valid_token()
        try:
            await self.coordinator.api.async_send_command(
                self._device_id, MowerCommand[command_name]
            )
        except Exception as err:  # noqa: BLE001 -- vendor SDK raises broadly
            raise HomeAssistantError(
                f"Navimow refused {command_name.lower()} for {self.name}: {err}"
            ) from err
        # The mower answers the command over MQTT within a second or two, so
        # this is belt-and-braces for the case where push is down and the
        # entity would otherwise sit on a stale activity until the next poll.
        await self.coordinator.async_request_refresh()

    async def async_start_mowing(self) -> None:
        """START from rest, RESUME from paused -- they are different commands.

        The cloud maps START to `action.devices.commands.StartStop {on: true}`
        and RESUME to `action.devices.commands.PauseUnpause {on: true}`
        (mower_sdk.api.async_send_command). NavimowHA bound start_mowing to
        START unconditionally and defined async_resume for RESUME -- but core's
        lawn_mower component registers only start_mowing, pause and dock
        (read from its __init__.py), so nothing HA can call ever reached
        RESUME and a paused mower was resumed with the command that begins a
        job. Dispatching on the current activity is the only way to reach
        both through the three services core actually has.
        """
        if self.activity is LawnMowerActivity.PAUSED:
            await self._async_command("RESUME")
        else:
            await self._async_command("START")

    async def async_pause(self) -> None:
        await self._async_command("PAUSE")

    async def async_dock(self) -> None:
        await self._async_command("DOCK")
