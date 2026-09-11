"""Binary sensors: the fault axis, the reachability axis, and charging.

THE PROBLEM SENSOR IS THE POINT OF THIS FILE. With the mower reporting through
one entity, a "Mowing" reading has nothing to be checked against, so a careful
surface drops it to neutral and says single source -- an unpaired state can
never read good. The vendor SDK has been parsing an independent error channel
the whole time; NavimowHA put it in an attribute. Here it is an entity, which is
what a card can actually pair against.

CONNECTIVITY AND PROBLEM ARE DELIBERATELY SEPARATE AXES. NavimowHA had one
value for both and resolved an offline mower to `error`. A mower that has lost
its uplink needs nothing from anybody; a mower that is stuck in the hydrangeas
needs somebody to walk out there. Rolling those together produces a fault
alert every time the garden Wi-Fi hiccups, which trains the household to
ignore the one signal that should never be ignored.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NavimowConfigEntry
from .const import KEY_CHARGING, KEY_CONNECTIVITY, KEY_PROBLEM
from .entity import NavimowEntity
from .model import is_charging, is_problem, is_reachable, sensor_unique_id

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NavimowBinaryDescription(BinarySensorEntityDescription):
    """A binary reading. `value_fn` takes (canonical_state, error, online)."""

    value_fn: Callable[[str | None, dict | None, bool | None], bool | None]
    # True for the one entity that must survive its subject going away.
    always_available: bool = False


BINARY_SENSORS: tuple[NavimowBinaryDescription, ...] = (
    NavimowBinaryDescription(
        key=KEY_PROBLEM,
        translation_key=KEY_PROBLEM,
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda state, error, online: is_problem(error, state),
    ),
    NavimowBinaryDescription(
        key=KEY_CONNECTIVITY,
        translation_key=KEY_CONNECTIVITY,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state, error, online: is_reachable(state, online),
        # THE ONE EXCEPTION TO entity.py's AVAILABILITY RULE, and it is the
        # rule's own reasoning applied: this entity's whole job is to say the
        # mower is unreachable, so it may not vanish when the mower becomes
        # unreachable. It still returns None -- state `unknown` -- when we
        # genuinely hold no reading, which is a different answer from `off`.
        always_available=True,
    ),
    NavimowBinaryDescription(
        key=KEY_CHARGING,
        translation_key=KEY_CHARGING,
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda state, error, online: is_charging(state),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavimowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        NavimowBinarySensor(coordinator, description)
        for coordinator in entry.runtime_data.coordinators.values()
        for description in BINARY_SENSORS
    )


class NavimowBinarySensor(NavimowEntity, BinarySensorEntity):
    entity_description: NavimowBinaryDescription

    def __init__(self, coordinator, description: NavimowBinaryDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = sensor_unique_id(coordinator.device.id, description.key)

    @property
    def available(self) -> bool:
        if self.entity_description.always_available:
            # Still gated on the coordinator: if we cannot reach the CLOUD we
            # have no opinion on whether the mower can, and saying `off` then
            # would be inventing a reading. is_on returns None in that case.
            return self.coordinator.last_update_success
        return super().available

    @property
    def is_on(self) -> bool | None:
        state = self._state
        error = getattr(state, "error", None) if state is not None else None
        return self.entity_description.value_fn(
            self._canonical_state, error, self._device_online
        )
