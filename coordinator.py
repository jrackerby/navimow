"""Coordinator: MQTT push in, HTTP read as a fallback, events fanned out.

THREE THINGS THIS DOES THAT NavimowHA DID NOT.

1. It subscribes the SDK's EVENT channel. NavimowSDK exposes on_state,
   on_attributes AND on_event, and NavimowMQTT already subscribes
   /downlink/vehicle/<id>/realtimeDate/event for every device, so the cloud
   was delivering events into a callback list nobody had appended to. They
   are now fanned out to event.py.

2. It separates "we cannot reach the cloud" from "the cloud says the mower is
   offline". The first is unavailability and belongs to
   DataUpdateCoordinator.last_update_success; the second is a READING, and an
   entity that goes unavailable cannot report it. entity.py implements the
   split; this class supplies both halves.

3. It logs nothing derived from a credential. NavimowHA logged the MQTT
   password at INFO as `first2***last2` on every setup -- four real characters
   of a live secret, per restart, forever.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    HTTP_FALLBACK_MIN_INTERVAL,
    MQTT_STALE_SECONDS,
    UPDATE_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class NavimowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One coordinator per mower."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        sdk: Any,
        api: Any,
        device: Any,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device.name}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.sdk = sdk
        self.api = api
        self.device = device
        self.oauth_session = oauth_session

        self._last_state: Any | None = None
        self._last_attributes: Any | None = None
        self._last_mqtt_update: float | None = None

        # PER-CHANNEL, BECAUSE ONE TIMESTAMP ACROSS THREE CHANNELS CANNOT
        # ANSWER THE QUESTION THIS COMPONENT EXISTS TO ANSWER. `attributes`
        # is an undocumented free-form channel and the only place a mowing
        # schedule or a blade figure could still be arriving; `_last_state`
        # being None told us nothing about whether a frame had EVER landed
        # on it, because a single shared `seconds_since_mqtt_push` moves on
        # every `state` frame and so reads healthy while `attributes` is
        # silent. Counted separately, a dump distinguishes "never published"
        # from "published and empty" from "published before we were
        # listening" without anybody having to be standing next to the mower.
        #
        # NEVER ZERO FOR "NOTHING ARRIVED": the count starts at 0 and the
        # timestamp starts at None, and it is the timestamp that carries the
        # never/just-now distinction.
        self._frames: dict[str, int] = {"state": 0, "attributes": 0, "event": 0}
        self._frame_last: dict[str, float | None] = {
            "state": None, "attributes": None, "event": None,
        }
        # The poll path reads the SDK's own cache, so a frame can reach
        # `_last_attributes` without this object's callback ever running.
        # Counted apart, or "0 frames but attributes present" would read as a
        # contradiction when it is just the other door.
        #
        # COUNTS PAYLOADS, NOT POLLS. The SDK keeps serving the same cached
        # object until a new frame replaces it, so incrementing on every poll
        # that found something would count the poll interval and print a
        # figure that looks like a frame count sitting next to a real one.
        # Gated on object identity, so it moves only when the payload does.
        self._cache_pickups: dict[str, int] = {"state": 0, "attributes": 0}
        self._last_http_fetch: float | None = None
        self._last_data_source: str | None = None

        # log-when-unavailable (Silver). Deduped on a STABLE CONDITION TOKEN,
        # never on the rendered message: a once-only log keyed on its own
        # text turns one condition into a new line every time a count inside
        # the string moves.
        self._logged_condition: str | None = None
        self._logging_armed = False

        self._event_listeners: list[Callable[[Any], None]] = []

    # -- setup ------------------------------------------------------------

    async def async_setup(self) -> None:
        """Register SDK callbacks. All three channels, this time."""
        self.sdk.on_state(self._handle_state)
        self.sdk.on_attributes(self._handle_attributes)
        self.sdk.on_event(self._handle_event)

    def async_arm_logging(self, *_: Any) -> None:
        """Start logging source conditions once HA has reached RUNNING.

        Gate on HA reaching RUNNING, never on the readings, and note that a
        gate that RECORDS while silent is worse than none -- the condition
        would then read as already-reported the moment logging arrives and a
        real fault present through startup would never log at all. So
        `_logged_condition` is left untouched while disarmed and the first
        armed evaluation reports whatever is true then.
        """
        self._logging_armed = True

    def async_add_event_listener(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        """Subscribe to device events. Returns an unsubscribe callable.

        Quality scale `entity-event-setup`: entities subscribe in
        async_added_to_hass and the returned callable is handed to
        async_on_remove, so nothing is registered from __init__.
        """
        self._event_listeners.append(callback)

        def _remove() -> None:
            if callback in self._event_listeners:
                self._event_listeners.remove(callback)

        return _remove

    # -- token ------------------------------------------------------------

    async def async_ensure_valid_token(self) -> str:
        """Refresh the OAuth token if needed and push it into the REST client.

        PUBLIC. NavimowHA's lawn_mower.py reached across the module boundary
        into `coordinator._async_ensure_valid_token()` before every command;
        commands legitimately need this, so it is part of the interface rather
        than a private the caller is expected to ignore.
        """
        try:
            await self.oauth_session.async_ensure_token_valid()
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:  # noqa: BLE001 -- transport, not auth
            # A DNS blip is not a revoked grant. Fall back to the cached
            # access token and only escalate to re-auth when there is none:
            # ConfigEntryAuthFailed drops the user into a re-login flow, which
            # is an expensive answer to a five-second network hiccup.
            cached = self.oauth_session.token or {}
            if cached.get("access_token"):
                _LOGGER.debug(
                    "Token refresh failed transiently, using cached token: %s", err
                )
            else:
                raise ConfigEntryAuthFailed(
                    "Token refresh failed and no cached token is available"
                ) from err

        token = self.oauth_session.token or {}
        access_token = token.get("access_token")
        if not access_token:
            raise ConfigEntryAuthFailed("No access token after refresh")
        self.api.set_token(access_token)
        return access_token

    # -- readings ---------------------------------------------------------

    def _build_data(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "state": self._last_state,
            "attributes": self._last_attributes,
            "source": self._last_data_source,
            "last_mqtt_monotonic": self._last_mqtt_update,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        await self.async_ensure_valid_token()

        cached_state = self.sdk.get_cached_state(self.device.id)
        if cached_state is not None:
            if cached_state is not self._last_state:
                self._cache_pickups["state"] += 1
            self._last_state = cached_state
            self._last_data_source = "mqtt_cache"

        cached_attrs = self.sdk.get_cached_attributes(self.device.id)
        if cached_attrs is not None:
            if cached_attrs is not self._last_attributes:
                self._cache_pickups["attributes"] += 1
            self._last_attributes = cached_attrs

        now = time.monotonic()
        mqtt_stale = (
            self._last_mqtt_update is None
            or now - self._last_mqtt_update > MQTT_STALE_SECONDS
        )
        may_fetch = (
            self._last_http_fetch is None
            or now - self._last_http_fetch > HTTP_FALLBACK_MIN_INTERVAL
        )

        if mqtt_stale and may_fetch:
            try:
                status = await self.api.async_get_device_status(self.device.id)
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:  # noqa: BLE001 -- vendor SDK raises broadly
                self._note_condition(
                    "http_fallback_failed",
                    "Navimow cloud did not answer a status read for %s: %s",
                    self.device.name,
                    err,
                )
                # NOT UpdateFailed WHEN WE STILL HOLD A PUSHED STATE. The
                # broker and the REST endpoint are separate paths; losing the
                # second while the first is live would blank every entity over
                # a reading we did not actually need.
                if self._last_state is None:
                    raise UpdateFailed(f"No state for {self.device.name}: {err}") from err
            else:
                self._last_state = _status_to_state_message(status)
                self._last_http_fetch = now
                self._last_data_source = "http_fallback"
                self._note_recovery("http_fallback_failed", self.device.name)

        return self._build_data()

    # -- push -------------------------------------------------------------

    def _handle_state(self, state: Any) -> None:
        """SDK callback. Runs on the MQTT thread -- hop to the event loop."""
        if state.device_id != self.device.id:
            return
        self._note_frame("state")
        self.hass.loop.call_soon_threadsafe(self._apply_state, state)

    def _handle_attributes(self, attrs: Any) -> None:
        if attrs.device_id != self.device.id:
            return
        self._note_frame("attributes")
        self.hass.loop.call_soon_threadsafe(self._apply_attributes, attrs)

    def _handle_event(self, event: Any) -> None:
        """The channel NavimowHA left unsubscribed."""
        if event.device_id != self.device.id:
            return
        self._note_frame("event")
        self.hass.loop.call_soon_threadsafe(self._apply_event, event)

    def _note_frame(self, channel: str) -> None:
        """One MQTT frame landed. Runs on the MQTT thread.

        `_last_mqtt_update` stays the newest across all three channels --
        that is what the HTTP-fallback staleness gate is asking about, and it
        should not start fetching over REST merely because `attributes` has
        never spoken.
        """
        now = time.monotonic()
        self._last_mqtt_update = now
        self._frames[channel] = self._frames.get(channel, 0) + 1
        self._frame_last[channel] = now

    def _apply_state(self, state: Any) -> None:
        self._last_state = state
        self._last_data_source = "mqtt_push"
        self.async_set_updated_data(self._build_data())

    def _apply_attributes(self, attrs: Any) -> None:
        self._last_attributes = attrs
        self.async_set_updated_data(self._build_data())

    def _apply_event(self, event: Any) -> None:
        for listener in list(self._event_listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                # One entity raising must not swallow the event for the rest.
                _LOGGER.exception("Navimow event listener failed")

    # -- accessors --------------------------------------------------------

    def get_device_state(self) -> Any | None:
        return (self.data or {}).get("state")

    def get_device_attributes(self) -> Any | None:
        return (self.data or {}).get("attributes")

    def mqtt_frame_counts(self) -> dict[str, int]:
        """Frames received per MQTT channel since setup."""
        return dict(self._frames)

    def mqtt_cache_pickups(self) -> dict[str, int]:
        """Times the poll found a frame in the SDK cache rather than here."""
        return dict(self._cache_pickups)

    def mqtt_seconds_since_frame(self, now: float | None = None) -> dict[str, float | None]:
        """Per channel: seconds since its last frame, or None if never.

        None, never 0. A channel that has published nothing since setup is a
        different finding from one that published a moment ago, and on the
        `attributes` channel it is the entire finding.
        """
        current = time.monotonic() if now is None else now
        return {
            channel: (round(current - last, 1) if last is not None else None)
            for channel, last in self._frame_last.items()
        }

    def mqtt_push_is_recent(self, now: float | None = None) -> bool:
        """Has the cloud pushed a STATE frame for this device recently?

        Positive evidence that the cloud is in live contact with this mower,
        which is the question binary_sensor.<name>_connectivity is asking.
        Gated on the `state` channel alone: `attributes` and `event` are
        occasional by nature and their silence is not a reachability finding.

        THE DECAY IS NOT INSTANT AND THAT IS STATED RATHER THAN HIDDEN. This
        window is MQTT_STALE_SECONDS but nothing re-renders the entity except
        a coordinator update, and while pushes are arriving each one renders
        it. The gap only opens when pushes STOP -- then the reading stands
        until the next poll, up to UPDATE_INTERVAL_SECONDS later. That is a
        bounded lag on a value that previously could not move at all;
        shortening the poll is a separate decision with its own cost.
        """
        last = self._frame_last.get("state")
        if last is None:
            return False
        current = time.monotonic() if now is None else now
        return (current - last) <= MQTT_STALE_SECONDS

    # -- once-only logging ------------------------------------------------

    def _note_condition(self, token: str, message: str, *args: Any) -> None:
        """Log once at the crossing into a condition, not on every poll.

        Level is INFO because this is the integration reporting on its own
        subject and nobody can act on it -- the level splits on who acts, and
        a cloud that is briefly unreachable needs no edit.
        """
        if not self._logging_armed or self._logged_condition == token:
            return
        if self.hass.state is not CoreState.running:
            return
        self._logged_condition = token
        _LOGGER.info(message, *args)

    def _note_recovery(self, token: str, name: str) -> None:
        if self._logged_condition != token:
            return
        self._logged_condition = None
        if self._logging_armed:
            _LOGGER.info("Navimow cloud is answering status reads for %s again", name)


def _status_to_state_message(status: Any) -> Any:
    """DeviceStatus (REST) -> DeviceStateMessage (the shape the MQTT path uses).

    Imported here rather than at module scope so that importing this module
    does not require the vendor SDK to be installed.
    """
    from mower_sdk.models import DeviceStateMessage

    error: dict[str, Any] | None = None
    if status.error_code is not None and status.error_code.value != "none":
        error = {"code": status.error_code.value, "message": status.error_message}

    metrics: dict[str, Any] = {}
    if status.mowing_time is not None:
        metrics["mowing_time"] = status.mowing_time
    if status.total_mowing_time is not None:
        metrics["total_mowing_time"] = status.total_mowing_time
    if status.extra:
        raw = status.extra.get("vehicleState")
        if raw:
            metrics["raw_state"] = raw

    return DeviceStateMessage(
        device_id=status.device_id,
        timestamp=status.timestamp,
        state=status.status.value,
        battery=status.battery,
        signal_strength=status.signal_strength,
        position=status.position,
        error=error,
        metrics=metrics or None,
    )
