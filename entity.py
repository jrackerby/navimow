"""DeviceInfo defined once, plus the availability rule the whole set obeys.

THE AVAILABILITY SPLIT, WHICH IS THE POINT OF THIS FILE.

There are two different failures and NavimowHA answered them with one value:

  * WE cannot reach the Navimow cloud -- no broker, no REST, nothing pushed.
    We hold no reading. Every entity is legitimately unavailable, and the
    quality scale's `entity-unavailable` rule is about exactly this case.

  * The cloud CAN be reached and reports that the mower is offline. That is a
    READING. An entity that goes unavailable here cannot report it, which is
    the failure mode LAW.md §11 names -- a monitor that disappears with its
    subject cannot say the subject is down -- and it is why
    binary_sensor.<name>_connectivity in particular must stay available and
    read `off` rather than vanish.

So: available == the coordinator's last update succeeded AND we hold a state,
independent of what that state says. The mower being offline never makes an
entity disappear.

Note this is NOT the household_state contract of overriding `available` to
True unconditionally. LAW.md §15 says to apply a rule where it GOVERNS: that
contract exists because household_state reads other entities and has no
service to lose. This integration has a cloud to lose, so losing it is
reportable as unavailability -- and only that.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NavimowCoordinator


class NavimowEntity(CoordinatorEntity[NavimowCoordinator]):
    """Base for every Navimow entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator)
        device = coordinator.device
        self._device_id = device.id
        self._attr_device_info = DeviceInfo(
            # NavimowHA's identifier, unchanged, so the existing device row and
            # every entity attached to it survives the swap. See model.py.
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or None,
            sw_version=device.firmware_version or None,
            serial_number=device.serial_number or device.id,
            connections=(
                {("mac", device.mac_address)} if device.mac_address else set()
            ),
        )

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.get_device_state() is not None
        )

    # -- shared readings --------------------------------------------------

    @property
    def _state(self) -> Any | None:
        return self.coordinator.get_device_state()

    @property
    def _canonical_state(self) -> str | None:
        state = self._state
        return state.state if state is not None else None

    @property
    def _device_online(self) -> bool | None:
        device = self.coordinator.device
        return getattr(device, "online", None)
