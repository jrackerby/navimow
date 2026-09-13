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
