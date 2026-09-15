"""Navimow. Replaces segwaynavimow/NavimowHA in place, on the same domain.

WHAT CHANGES AND WHAT DELIBERATELY DOES NOT. The unique_ids of the mower and
its battery sensor are byte-identical to NavimowHA's, so the entity registry
reuses those rows and lawn_mower.navimow_x430_2 / sensor.navimow_x430_battery_2
keep their ids and their history. model.py carries the full reasoning and
tools/test_navimow_migration.py pins the strings. Everything else here is new.

SETUP CANNOT VALIDATE ITSELF ANY FURTHER THAN IT DOES. A setup check that
exercises a different channel than the one that will be used certifies
nothing. So setup proves the two channels it will actually use, in
the order it will use them -- an authenticated REST call (authList) and then
an authenticated MQTT connection -- and does not report ready on the strength
of the OAuth token alone.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .const import (
    API_BASE_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    DOMAIN,
    MQTT_KEEPALIVE_SECONDS,
    MQTT_RECONNECT_MAX_DELAY,
    MQTT_RECONNECT_MIN_DELAY,
    PLATFORMS,
)
from .model import mqtt_descriptor_shape, mqtt_endpoint

_LOGGER = logging.getLogger(__name__)


@dataclass
class NavimowRuntimeData:
    """Quality scale `runtime-data`: state lives on the entry, not hass.data."""

    sdk: Any
    api: Any
    devices: list[Any]
    coordinators: dict[str, Any] = field(default_factory=dict)
    unloading: bool = False
    # THE ENDPOINT ACTUALLY CONNECTED TO, kept because nothing else retains it.
    # `entry.data` carries NavimowHA-era `mqtt_broker`/`mqtt_port` keys that
    # this component never reads, so a diagnostics dump that reported only the
    # entry was reporting an inherited value for a session established from a
    # live `mqtt/userInfo/get/v2` response. Resolved here, reported from here.
    mqtt_broker: str | None = None
    mqtt_port: int | None = None
    mqtt_transport: str | None = None
    # The websocket path handed to paho. The live one is "/mqtt/<userId>", so
    # diagnostics publish it with that segment masked; the id itself is kept
    # here only to do the masking.
    mqtt_ws_path: str | None = None
    mqtt_user_id: str | None = None
    # Key names and URL schemes only -- never a value. model.py says why.
    mqtt_descriptor: dict | None = None
    # A HANDSHAKE THAT NEVER COMPLETES IS INVISIBLE WITHOUT THESE. paho runs
    # connect_async in its own thread and, when the TCP/TLS/websocket upgrade
    # fails, retries silently: no CONNACK, so on_connect never fires, and no
    # session, so on_disconnect never fires either. Its only word on the
    # matter is the on_connect_fail callback, which the SDK does not wire.
    # Counted from there; a session that connected once resets it.
    mqtt_connect_failures: int = 0
    mqtt_last_connect_failure_monotonic: float | None = None
    # WHEN THE DEVICE RECORD WAS READ, because it is read exactly once and
    # `model`/`name`/`firmware_version` come off it. Without an age beside
    # them, a stale field in a dump is indistinguishable from a live one.
    devices_read_monotonic: float | None = None
    # WHAT THE BROKER ACTUALLY PUBLISHES FOR THIS VEHICLE, per topic. The
    # SDK subscribes three topics it GUESSED (`realtimeDate/state`, `event`,
    # `attributes` -- its topic helpers still carry the author's own TODO
    # to "adjust to the real format"), so a silent `attributes` never said
    # whether the channel is quiet or does not exist. The session now takes
    # the vehicle's whole `/downlink/vehicle/<id>/#` namespace and every
    # frame is counted here by the topic it arrived on, with the union of
    # its top-level payload KEY NAMES beside it -- names only, never a value,
    # which is enough to say whether anything schedule- or blade-shaped is
    # on the wire and nothing else. Keyed by topic; each record carries
    # `frames`, `last_monotonic` and `keys`.
    mqtt_topic_census: dict[str, dict[str, Any]] = field(default_factory=dict)
    # None until the broker answers the wildcard SUBACK; False when it
    # refused (the SDK's three topics then stay in force and the census
    # sees only those). Reported, so a thin census is void, not clean.
    mqtt_wildcard_granted: bool | None = None
    # EVERY SUBSCRIPTION'S SUBACK VERDICT, BY TOPIC -- the SDK's three
    # included. paho hands back a mid per subscribe and a granted-qos list
    # per SUBACK; the SDK discards the mid and wires no on_subscribe, so a
    # topic the broker REFUSED (0x80) has always been indistinguishable from
    # a topic it granted that never speaks. The first wildcard tried came
    # back refused, which is exactly the answer this map exists to give for
    # each of the three the SDK guessed. Values: "sent" until the SUBACK,
    # then "granted qos<n>" or "refused".
    mqtt_subscriptions: dict[str, str] = field(default_factory=dict)
    # mid -> topic(s) awaiting a SUBACK. Lives here, not in a closure: the
    # subscribe wrapper survives a re-install while the on_subscribe hook is
    # replaced, and the two must read one map or the verdict is dropped.
    mqtt_pending_subacks: dict[int, str] = field(default_factory=dict)


NavimowConfigEntry = ConfigEntry[NavimowRuntimeData]


# THE SDK'S OWN MASKED-CREDENTIAL LINES, AND WHAT IS DONE ABOUT THEM.
# navimow-sdk's `mower_sdk.mqtt` logs, at INFO, the broker username and the
# Authorization header rendered as `first2***last2` -- the NavimowHA defect
# this component's README cites as a reason it exists, one layer down. The
# lines are silent at default levels and appear the moment anyone raises
# `mower_sdk` to debug this integration, which is exactly when a log gets
# pasted into an issue. The SDK cannot be edited from here, so its records
# are dropped at the logger they are emitted from. Keyed on the FORMAT
# STRING, not on the mask: a line that would print `username=` or
# `auth_headers=` at all is refused, whatever the SDK renders into them, so a
# change to the masking does not reopen the leak. Every other SDK line still
# passes, so debugging keeps its trace.
SDK_LOGGERS_FILTERED: tuple[str, ...] = ("mower_sdk.mqtt",)
SDK_LOG_FORMAT_MARKERS: tuple[str, ...] = ("username=", "auth_headers=")


class _NoCredentialFieldsFilter(logging.Filter):
    """Drop any record whose format string names a credential field."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = str(record.msg)
        return not any(marker in message for marker in SDK_LOG_FORMAT_MARKERS)


_SDK_LOG_FILTER = _NoCredentialFieldsFilter()


def install_sdk_log_filter() -> None:
    """Idempotent: a logger filter list tolerates one instance, not a stack."""
    for name in SDK_LOGGERS_FILTERED:
        logger = logging.getLogger(name)
        if _SDK_LOG_FILTER not in logger.filters:
            logger.addFilter(_SDK_LOG_FILTER)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Seed the vendor's fixed OAuth2 client credential.

    Navimow issues one client id/secret for every Home Assistant installation
    (const.py records why they are constants rather than something the user
    holds), so importing them here means the user never sees an
    "add application credentials" step for values they cannot change.
    """
    install_sdk_log_filter()
    await async_import_client_credential(
        hass,
        DOMAIN,
        ClientCredential(CLIENT_ID, CLIENT_SECRET, name="Navimow"),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: NavimowConfigEntry) -> bool:
    from mower_sdk.api import MowerAPI
    from mower_sdk.errors import MowerAPIError
    from mower_sdk.sdk import NavimowSDK

    from .auth import NavimowOAuth2Implementation
    from .coordinator import NavimowCoordinator

    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    try:
        await oauth_session.async_ensure_token_valid()
    except ConfigEntryAuthFailed:
        raise
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Navimow token endpoint unreachable: {err}") from err

    access_token = (oauth_session.token or {}).get("access_token")
    if not access_token:
        raise ConfigEntryAuthFailed("Navimow returned no access token")

    api = MowerAPI(
        session=async_get_clientsession(hass),
        token=access_token,
        base_url=API_BASE_URL,
    )

    # CHANNEL ONE, exercised: an authenticated REST call.
    try:
        devices = await api.async_get_devices()
    except MowerAPIError as err:
        raise ConfigEntryNotReady(f"Navimow device list unavailable: {err}") from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryAuthFailed(f"Navimow rejected the token: {err}") from err

    if not devices:
        # An account with no mower is not a transient failure and retrying
        # forever would hide it. `no_devices` is a documented abort reason.
        raise ConfigEntryNotReady(
            "Navimow returned an empty device list for this account"
        )

    try:
        mqtt_info = await api.async_get_mqtt_user_info()
    except MowerAPIError as err:
        raise ConfigEntryNotReady(f"Navimow MQTT credentials unavailable: {err}") from err

    broker, port, ws_path = mqtt_endpoint(mqtt_info)
    if not broker:
        raise ConfigEntryNotReady("Navimow returned no MQTT broker address")

    # NOTHING DERIVED FROM A CREDENTIAL IS LOGGED, at any level. NavimowHA
    # logged the broker password at INFO masked as `first2***last2`, which is
    # four real characters of a live secret written to the journal on every
    # setup.
    _LOGGER.debug(
        "Navimow MQTT endpoint resolved: broker=%s port=%s ws_path=%s",
        broker, port, _mask_user_id(ws_path, mqtt_info.get("userId")),
    )

    runtime = NavimowRuntimeData(
        sdk=None,
        api=api,
        devices=devices,
        devices_read_monotonic=time.monotonic(),
        mqtt_broker=broker,
        mqtt_port=port,
        mqtt_transport="websocket" if ws_path else "tcp",
        mqtt_ws_path=ws_path,
        mqtt_user_id=str(mqtt_info.get("userId") or "") or None,
        mqtt_descriptor=mqtt_descriptor_shape(mqtt_info),
    )

    def _build_sdk() -> Any:
        # paho's connect path does blocking TLS work (tls_set,
        # load_default_certs); it must not run on the event loop.
        sdk = NavimowSDK(
            broker=broker,
            port=port,
            username=mqtt_info.get("userName"),
            password=mqtt_info.get("pwdInfo"),
            ws_path=ws_path,
            auth_headers={"Authorization": f"Bearer {access_token}"} if ws_path else None,
            loop=hass.loop,
            records=devices,
            keepalive_seconds=MQTT_KEEPALIVE_SECONDS,
            reconnect_min_delay=MQTT_RECONNECT_MIN_DELAY,
            reconnect_max_delay=MQTT_RECONNECT_MAX_DELAY,
        )
        _install_session_observers(hass, runtime, sdk)
        sdk.connect()
        return sdk

    # CHANNEL TWO, exercised.
    try:
        sdk = await hass.async_add_executor_job(_build_sdk)
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Navimow MQTT connect failed: {err}") from err
    runtime.sdk = sdk

    _install_credential_refresh(hass, entry, runtime, oauth_session)

    for device in devices:
        coordinator = NavimowCoordinator(hass, entry, sdk, api, device, oauth_session)
        await coordinator.async_setup()
        await coordinator.async_config_entry_first_refresh()
        runtime.coordinators[device.id] = coordinator
        entry.async_on_unload(
            async_at_started(hass, coordinator.async_arm_logging)
        )

    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NavimowConfigEntry) -> bool:
    """Quality scale `config-entry-unloading`."""
    runtime = entry.runtime_data
    # Set BEFORE unloading platforms: tearing down the MQTT client fires the
    # disconnect callback, and without this flag that callback races to fetch
    # fresh credentials for a connection that is going away.
    runtime.unloading = True

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and runtime.sdk is not None:
        try:
            await hass.async_add_executor_job(runtime.sdk.disconnect)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Navimow MQTT disconnect raised on unload: %s", err)
    return unloaded


def _install_credential_refresh(
    hass: HomeAssistant,
    entry: NavimowConfigEntry,
    runtime: NavimowRuntimeData,
    oauth_session: config_entry_oauth2_flow.OAuth2Session,
) -> None:
    """Re-fetch MQTT credentials after a broker disconnect.

    The broker's userName/pwdInfo are minted against the OAuth token, so a
    token refresh invalidates them and reconnecting with the old pair answers
    CODE_OAUTH_INFO_ILLEGAL. The refresh is serialised with an asyncio.Lock
    because a broker-side mass disconnect fires this callback several times at
    once and each run rebuilds the paho client.
    """
    import asyncio

    lock = asyncio.Lock()
    sdk = runtime.sdk
    api = runtime.api

    async def _on_disconnected() -> None:
        if runtime.unloading or lock.locked():
            return
        async with lock:
            if runtime.unloading:
                return
            try:
                await oauth_session.async_ensure_token_valid()
                token = (oauth_session.token or {}).get("access_token")
                if token:
                    api.set_token(token)
                info = await api.async_get_mqtt_user_info()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Navimow MQTT credential refresh deferred: %s", err)
                return

            headers = {"Authorization": f"Bearer {token}"} if token else None

            def _apply() -> None:
                sdk.update_mqtt_credentials(
                    auth_headers=headers,
                    username=info.get("userName"),
                    password=info.get("pwdInfo"),
                )
                # update_credentials REBUILDS the paho client when the session
                # is down, and a new client carries none of the callbacks set
                # on the old one. Re-attach, or the failure count goes blind
                # at exactly the moment a refresh was tried.
                _install_session_observers(hass, runtime, sdk)

            await hass.async_add_executor_job(_apply)
            # No credential, masked or otherwise, in this line.
            _LOGGER.debug("Navimow MQTT credentials refreshed")

    # REACHING INTO sdk._mqtt IS DELIBERATE AND IS THE ONLY ROUTE. NavimowSDK
    # exposes on_state/on_event/on_attributes but forwards no connection
    # lifecycle hook, so the disconnect callback can only be attached to the
    # NavimowMQTT it wraps. If navimow-sdk ever grows a public equivalent this
    # is the line to move; the manifest pins >=0.1.2 and this is the private
    # surface that pin is really protecting.
    sdk._mqtt.on_disconnected = _on_disconnected


def _mask_user_id(path: str | None, user_id: object) -> str | None:
    """The live websocket path is /mqtt/<userId>; publish the shape, not the id."""
    if not path:
        return path
    text = str(user_id or "")
    return path.replace(text, "<userId>") if text else path


def _install_session_observers(
    hass: HomeAssistant, runtime: NavimowRuntimeData, sdk: Any
) -> None:
    """Make a session that never comes up SAY SO, once, and once on recovery.

    Same private reach as `_install_credential_refresh`, for the same reason:
    the facade forwards no lifecycle hook. Two observers -- paho's own
    on_connect_fail on the client the SDK built (the only signal a failed
    upgrade produces at all), and NavimowMQTT.on_connected, which the facade
    never uses. Called from the paho thread for the first and from the loop
    for the second; both touch plain ints and log, nothing that needs the
    loop. INFO on both edges: nobody can edit their way out of a broker that
    will not answer, so it is the integration reporting its own subject.
    """
    mqtt = sdk._mqtt
    endpoint = "%s:%s%s" % (
        runtime.mqtt_broker, runtime.mqtt_port,
        _mask_user_id(runtime.mqtt_ws_path, runtime.mqtt_user_id) or "",
    )

    def _on_connect_fail(_client: Any, _userdata: Any) -> None:
        runtime.mqtt_connect_failures += 1
        runtime.mqtt_last_connect_failure_monotonic = time.monotonic()
        if runtime.mqtt_connect_failures == 1:
            _LOGGER.info(
                "Navimow MQTT session to %s is not coming up: the connection "
                "or websocket upgrade failed before any CONNACK; paho will "
                "keep retrying and diagnostics count the attempts",
                endpoint,
            )

    async def _on_connected() -> None:
        if runtime.mqtt_connect_failures:
            _LOGGER.info(
                "Navimow MQTT session to %s is up after %d failed attempt(s)",
                endpoint, runtime.mqtt_connect_failures,
            )
        runtime.mqtt_connect_failures = 0

    mqtt.client.on_connect_fail = _on_connect_fail
    mqtt.on_connected = _on_connected
    _install_topic_census(runtime, mqtt)


# The three topics navimow-sdk subscribes on every CONNACK. They are its
# guess at the vendor's namespace, and the wildcard below covers all three.
# THEY ARE NOT UNSUBSCRIBED when the wildcard is granted: a gateway can
# answer a wildcard SUBACK with a grant and still route only exact matches,
# and v1.6.1 measured six minutes of a live mowing session with the three
# dropped and not one frame over `realtimeDate/+` -- too short to convict
# the wildcard, long enough to refuse to bet the entity on it. Both stay
# subscribed; a broker that delivers one copy per matching subscription is
# handled by the duplicate gate in the census wrapper instead.
VEHICLE_TOPIC_PREFIX: str = "/downlink/vehicle/"
# A broker delivering two copies of one publish does so back to back; a real
# repeat of the same payload from the mower is seconds apart at the least.
DUPLICATE_WINDOW_SECONDS: float = 0.5


# Tried in order; the census listens on the first one granted. `#`
# was refused by the live broker (v1.6.0's first dump), so the single-level
# `realtimeDate/+` is the fallback: it still covers the three and answers
# whether the ACL is on the depth or on the whole namespace.
def _vehicle_wildcards(device_id: str) -> list[str]:
    return [
        f"{VEHICLE_TOPIC_PREFIX}{device_id}/#",
        f"{VEHICLE_TOPIC_PREFIX}{device_id}/realtimeDate/+",
    ]


def _wildcard_device_id(topic: str) -> str:
    rest = topic[len(VEHICLE_TOPIC_PREFIX):]
    return rest.split("/", 1)[0]


def _install_topic_census(runtime: NavimowRuntimeData, mqtt: Any) -> None:
    """Listen to the vehicle's whole namespace and count what arrives, by topic.

    Two hooks on the paho client the SDK built. `on_message` is WRAPPED, not
    replaced: the SDK's own handler still runs for every frame, so nothing
    the entities read changes; the wrapper only records topic, count, age
    and top-level key names first. It is installed once per client -- the
    marker guards a re-install onto a client that was not rebuilt, which
    would otherwise count every frame twice. `subscribe` is wrapped so every
    mid maps back to its topic, and `on_subscribe` records each SUBACK's
    verdict by topic -- the SDK's three included; on a refusal it tries the next
    wildcard. The three guessed topics stay subscribed either way -- the
    module comment above VEHICLE_TOPIC_PREFIX says why -- so nothing that
    worked before this census is bet on the wildcard.
    """
    client = mqtt.client
    device_ids = [
        str(getattr(device, "id", "") or "")
        for device in runtime.devices
        if getattr(device, "id", None)
    ]
    pending = runtime.mqtt_pending_subacks
    # Wildcards still untried, per device, so a refusal of one tries the
    # next and the verdict is "refused" only when all have been.
    remaining: dict[str, list[str]] = {
        device_id: _vehicle_wildcards(device_id) for device_id in device_ids
    }

    # EVERY subscribe goes through here, the SDK's own included: the mid is
    # the only thing that ties a SUBACK back to its topic, and the SDK
    # throws it away.
    if not getattr(client.subscribe, "_navimow_census", False):
        # A fresh client restarts its mids; nothing outstanding can still
        # be answered.
        pending.clear()
        sdk_subscribe = client.subscribe

        def _subscribe(topic: Any, *args: Any, **kwargs: Any) -> Any:
            result, mid = sdk_subscribe(topic, *args, **kwargs)
            names = [topic] if isinstance(topic, str) else [
                item[0] if isinstance(item, (list, tuple)) else item for item in topic
            ]
            for name in names:
                runtime.mqtt_subscriptions[str(name)] = "sent"
            if result == 0 and mid is not None:
                pending[mid] = "\n".join(str(name) for name in names)
            return result, mid

        _subscribe._navimow_census = True  # type: ignore[attr-defined]
        client.subscribe = _subscribe

    if not getattr(client.on_message, "_navimow_census", False):
        sdk_on_message = client.on_message

        last_frame: dict[str, Any] = {"key": None, "monotonic": 0.0}

        def _on_message(_client: Any, _userdata: Any, msg: Any) -> None:
            record = runtime.mqtt_topic_census.setdefault(
                msg.topic,
                {"frames": 0, "duplicates": 0, "last_monotonic": None, "keys": set()},
            )
            now = time.monotonic()
            # ONE COPY PER MATCHING SUBSCRIPTION is what MQTT 3.1.1 lets a
            # broker do (3.3.5), and the explicit topic and the wildcard both
            # match every frame. A second copy is byte-identical and arrives
            # on the same topic within the same delivery; it is counted, so
            # the dump says which kind of broker this is, and NOT forwarded,
            # so neither the SDK's cache nor `mqtt_frames` sees it twice.
            key = (msg.topic, bytes(msg.payload or b""))
            if key == last_frame["key"] and now - last_frame["monotonic"] < DUPLICATE_WINDOW_SECONDS:
                record["duplicates"] += 1
                return
            last_frame["key"] = key
            last_frame["monotonic"] = now
            record["frames"] += 1
            record["last_monotonic"] = now
            try:
                payload = json.loads((msg.payload or b"").decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                record["keys"].update(str(key) for key in payload)
            else:
                # A frame that is not a JSON object is still a frame; say
                # what shape it had rather than counting it as keyless.
                record["keys"].add(f"<{type(payload).__name__}>")
            if sdk_on_message is not None:
                sdk_on_message(_client, _userdata, msg)

        _on_message._navimow_census = True  # type: ignore[attr-defined]
        client.on_message = _on_message

    def _try_next_wildcard(device_id: str) -> None:
        if not remaining.get(device_id):
            if runtime.mqtt_wildcard_granted is not False:
                _LOGGER.info(
                    "Navimow broker refused every vehicle wildcard subscription; "
                    "the topic census sees only the SDK's three guessed topics"
                )
            runtime.mqtt_wildcard_granted = False
            return
        client.subscribe(remaining[device_id].pop(0))

    def _on_subscribe(
        _client: Any, _userdata: Any, mid: int, granted: Any, *_args: Any
    ) -> None:
        joined = pending.pop(mid, None)
        if joined is None:
            return
        topics = joined.split("\n")
        codes = list(granted) if isinstance(granted, (list, tuple)) else [granted]
        for index, topic in enumerate(topics):
            code = codes[index] if index < len(codes) else codes[-1]
            refused = getattr(code, "is_failure", False) or (
                isinstance(code, int) and code >= 128
            )
            runtime.mqtt_subscriptions[topic] = (
                "refused" if refused else f"granted qos{int(code)}"
            )
            if not topic.startswith(VEHICLE_TOPIC_PREFIX):
                continue
            if not (topic.endswith("/#") or topic.endswith("/+")):
                continue
            device_id = _wildcard_device_id(topic)
            if refused:
                _try_next_wildcard(device_id)
                continue
            runtime.mqtt_wildcard_granted = True

    client.on_subscribe = _on_subscribe

    # The SDK subscribes its three on the paho thread inside on_connect and
    # only then schedules on_connected onto the loop, which is where the
    # session observers above run. Chaining the wildcard onto that edge puts
    # it after the SDK's own subscriptions on every CONNACK, reconnects
    # included, so the narrowing is redone each time the SDK redoes its part.
    # A re-install onto an unreset hook would chain wrapper onto wrapper and
    # subscribe the wildcard once per install; unwrap our own first.
    previous_on_connected = mqtt.on_connected
    if getattr(previous_on_connected, "_navimow_census", False):
        previous_on_connected = previous_on_connected._previous

    async def _on_connected_subscribe_wildcard() -> None:
        if previous_on_connected is not None:
            await previous_on_connected()
        for device_id in device_ids:
            remaining[device_id] = _vehicle_wildcards(device_id)
            _try_next_wildcard(device_id)

    _on_connected_subscribe_wildcard._navimow_census = True  # type: ignore[attr-defined]
    _on_connected_subscribe_wildcard._previous = previous_on_connected  # type: ignore[attr-defined]
    mqtt.on_connected = _on_connected_subscribe_wildcard
