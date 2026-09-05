"""Sensors.

`battery` INHERITS NavimowHA's unique_id and must keep it -- see model.py.
Everything else here is data the vendor SDK already parsed and NavimowHA
dropped into extra_state_attributes, where the recorder does not keep it as a
series, statistics cannot use it, and no automation can trigger on it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NavimowConfigEntry
from .const import (
    KEY_BATTERY,
    KEY_ERROR,
    KEY_RAW_STATE,
    KEY_SESSION_TIME,
    KEY_SIGNAL,
    KEY_TOTAL_TIME,
)
from .entity import NavimowEntity
from .model import normalise_error, sensor_unique_id

PARALLEL_UPDATES = 0  # read-only, coordinator-driven


def _metric(state: Any, name: str) -> Any:
    metrics = getattr(state, "metrics", None) or {}
    return metrics.get(name)


@dataclass(frozen=True, kw_only=True)
class NavimowSensorDescription(SensorEntityDescription):
    """A sensor plus how to read it off a DeviceStateMessage."""

    value_fn: Callable[[Any], Any]
    attrs_fn: Callable[[Any], dict[str, Any]] | None = None


SENSORS: tuple[NavimowSensorDescription, ...] = (
    NavimowSensorDescription(
        key=KEY_BATTERY,
        translation_key=KEY_BATTERY,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.battery,
    ),
    NavimowSensorDescription(
        key=KEY_SIGNAL,
        translation_key=KEY_SIGNAL,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.signal_strength,
    ),
    NavimowSensorDescription(
        # The fault code as its own state, so history answers "when did it
        # last jam" without parsing an attribute out of a lawn_mower row.
        key=KEY_ERROR,
        translation_key=KEY_ERROR,
        value_fn=lambda state: normalise_error(state.error)[0],
        attrs_fn=lambda state: (
            {"message": normalise_error(state.error)[1]}
            if normalise_error(state.error)[1]
            else {}
        ),
    ),
    NavimowSensorDescription(
        # The vendor's untranslated string. NOTHING derives behaviour from
        # this -- it exists so that a state this component has never seen is
        # visible rather than silently flattened into `unknown` by
        # model.CANONICAL_TO_ACTIVITY, and so a bug report can quote it.
        key=KEY_RAW_STATE,
        translation_key=KEY_RAW_STATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _metric(state, "raw_state") or state.state,
    ),
    NavimowSensorDescription(
        key=KEY_SESSION_TIME,
        translation_key=KEY_SESSION_TIME,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda state: _metric(state, "mowing_time"),
    ),
    NavimowSensorDescription(
        key=KEY_TOTAL_TIME,
        translation_key=KEY_TOTAL_TIME,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # TOTAL_INCREASING, so the lifetime counter becomes a long-term
        # statistic and survives purge_keep_days -- LAW.md §3 pins that window
        # untouched, which makes statistics the only place a multi-season
        # runtime figure can live.
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda state: _metric(state, "total_mowing_time"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavimowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        NavimowSensor(coordinator, description)
        for coordinator in entry.runtime_data.coordinators.values()
        for description in SENSORS
    )


class NavimowSensor(NavimowEntity, SensorEntity):
    """One reading off the pushed state message."""

    entity_description: NavimowSensorDescription

    def __init__(self, coordinator, description: NavimowSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = sensor_unique_id(coordinator.device.id, description.key)

    @property
    def native_value(self) -> Any:
        state = self._state
        if state is None:
            return None
        return self.entity_description.value_fn(state)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        if state is None or self.entity_description.attrs_fn is None:
            return {}
        return self.entity_description.attrs_fn(state)
