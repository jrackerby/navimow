"""The MQTT event channel, finally subscribed.

mower_sdk.mqtt.NavimowMQTT.subscribe_all() has always subscribed
/downlink/vehicle/<id>/realtimeDate/event alongside state and attributes, and
NavimowSDK parses each one into a DeviceEventMessage and offers it through
on_event(). NavimowHA registered on_state and on_attributes and never
on_event, so every event the mower has ever published -- blade jam, lifted,
rain stop, job complete -- was decoded and then dropped inside the SDK.

An EventEntity rather than a fired bus event, because an event entity is
visible in the UI, keeps its last occurrence across a restart, and can be
automated on without knowing an undocumented event name in advance.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NavimowConfigEntry
from .const import KEY_EVENTS
from .entity import NavimowEntity
from .model import EVENT_TYPES, event_bucket, sensor_unique_id

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavimowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        NavimowEventEntity(coordinator)
        for coordinator in entry.runtime_data.coordinators.values()
    )


class NavimowEventEntity(NavimowEntity, EventEntity):
    _attr_translation_key = KEY_EVENTS
    _attr_event_types = list(EVENT_TYPES)

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = sensor_unique_id(coordinator.device.id, KEY_EVENTS)

    async def async_added_to_hass(self) -> None:
        """Subscribe here, not in __init__ -- quality scale `entity-event-setup`."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_event_listener(self._handle_event)
        )

    @property
    def available(self) -> bool:
        """Available on the coordinator alone.

        An event entity holds the LAST event, which stays meaningful while the
        mower is offline -- arguably it is most meaningful then, since the
        event is often why it went offline. Gating this on holding a current
        state message would blank the record of what just happened.
        """
        return self.coordinator.last_update_success

    @callback
    def _handle_event(self, event: Any) -> None:
        bucket = event_bucket(getattr(event, "level", None), getattr(event, "event", None))
        # The raw vocabulary is undocumented, so everything the cloud sent is
        # carried through rather than reduced to the bucket. `event_bucket`
        # only decides which declared type core will accept.
        self._trigger_event(
            bucket,
            {
                "event": getattr(event, "event", None),
                "type": getattr(event, "type", None),
                "level": getattr(event, "level", None),
                "message": getattr(event, "message", None),
                "params": getattr(event, "params", None),
                "timestamp": getattr(event, "timestamp", None),
            },
        )
        self.async_write_ha_state()
