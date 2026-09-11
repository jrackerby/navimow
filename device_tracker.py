"""GPS tracker for the mower.

WHY THIS IS WORTH A PLATFORM. Pairing a network tracker for the mower with the
mower's own integration entity normally has to be hardcoded, because there is
no structural signal to discover it from: a Wi-Fi tracker and the Navimow
integration entity are different devices with no shared device_id.

The cloud has been sending position on every state message and NavimowHA put it
in an attribute. A tracker on the Navimow DEVICE supplies the shared device_id
that pairing lacks, so it can eventually be derived rather than pinned. This
only supplies the signal; deriving the pin is a separate change.

It does NOT replace the UniFi tracker. That one answers "is the mower on the
house Wi-Fi", this one answers "where in the garden is it", and the jinja
comment's measured finding stands -- a docked mower reads not_home on the
UniFi tracker for minutes afterwards.
"""

from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NavimowConfigEntry
from .const import KEY_TRACKER
from .entity import NavimowEntity
from .model import sensor_unique_id

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavimowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        NavimowTracker(coordinator)
        for coordinator in entry.runtime_data.coordinators.values()
    )


class NavimowTracker(NavimowEntity, TrackerEntity):
    _attr_translation_key = KEY_TRACKER
    _attr_source_type = SourceType.GPS
    # RTK-corrected, but the cloud publishes no accuracy figure and inventing
    # one would draw a confidence circle on a map from nothing.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = sensor_unique_id(coordinator.device.id, KEY_TRACKER)

    def _position(self) -> dict | None:
        state = self._state
        position = getattr(state, "position", None) if state is not None else None
        return position if isinstance(position, dict) else None

    @property
    def latitude(self) -> float | None:
        position = self._position()
        if not position:
            return None
        # The cloud has used both spellings across payload shapes; neither is
        # documented, so read both rather than pick one and silently plot
        # nothing when it changes.
        value = position.get("lat", position.get("latitude"))
        return float(value) if value is not None else None

    @property
    def longitude(self) -> float | None:
        position = self._position()
        if not position:
            return None
        value = position.get("lng", position.get("lon", position.get("longitude")))
        return float(value) if value is not None else None

    @property
    def available(self) -> bool:
        return super().available and self._position() is not None
