"""Config entry diagnostics.

REDACTION IS THE WHOLE JOB HERE. This dump is what a user pastes into an issue
tracker, so anything derived from a credential or a location has to be gone
before it is written -- not masked. NavimowHA's `first2***last2` masking is the
counter-example this file exists not to repeat: a partial secret in a bug
report is a secret in a bug report.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NavimowConfigEntry, _mask_user_id

# The only `entry.data` keys this component reads; the OAuth helper takes both
# and nothing here touches `entry.data` anywhere else. Everything beside them
# was written by segwaynavimow/NavimowHA into the entry this component
# inherited, and is dead storage -- see `entry_keys_unused` below.
ENTRY_KEYS_USED = frozenset({"token", "auth_implementation"})

TO_REDACT = {
    "access_token",
    "refresh_token",
    "token",
    "client_id",
    "client_secret",
    "userName",
    "pwdInfo",
    "serial_number",
    "mac_address",
    "position",
    "latitude",
    "longitude",
    "lat",
    "lng",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NavimowConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    now = time.monotonic()
    devices: list[dict[str, Any]] = []
    for device_id, coordinator in runtime.coordinators.items():
        state = coordinator.get_device_state()
        attributes = coordinator.get_device_attributes()
        last_push = (coordinator.data or {}).get("last_mqtt_monotonic")
        devices.append(
            {
                "id_present": bool(device_id),
                "model": coordinator.device.model,
                # Empty on SDK 0.1.2: the vendor sends `firmware`, the SDK
                # reads `firmware_version` (measured 2026-09-14, model.py).
                "firmware_version": coordinator.device.firmware_version,
                # NO `online` KEY IS PRINTED, BECAUSE NONE IS SENT. The raw
                # `authList` record was read on 2026-09-14 and carries
                # exactly `firmware`, `id`, `model`, `name`; the SDK's
                # `Device.online` is its own `False` default and printing it
                # here was read, in #7's history, as the mower being asleep.
                # The record's age stays: it is read once, at setup, and
                # `model`/`firmware_version` above are as old as this says.
                "device_record_age_seconds": (
                    round(now - runtime.devices_read_monotonic, 1)
                    if runtime.devices_read_monotonic is not None
                    else None
                ),
                "last_update_success": coordinator.last_update_success,
                "source": (coordinator.data or {}).get("source"),
                "state": async_redact_data(state.to_dict(), TO_REDACT)
                if state is not None
                else None,
                # THE ONLY WINDOW ONTO AN UNDOCUMENTED CHANNEL.
                # /downlink/vehicle/<id>/realtimeDate/attributes carries a
                # free-form dict that no published source characterises, and
                # it is the one place a mowing schedule or a blade/service
                # figure could still be arriving -- neither is anywhere on the
                # REST surface, whose command vocabulary is Google Smart Home
                # and therefore has no such concept to carry.
                #
                # It is dumped WHOLE and unfiltered on purpose. Selecting keys
                # here would require knowing which ones matter, which is the
                # very thing this exists to find out, and a redaction pass that
                # dropped an unrecognised key would delete the finding. Values
                # go through the same TO_REDACT pass as everything else, so a
                # credential or a coordinate keyed under a name we already know
                # is still removed; anything genuinely new arrives intact and
                # visible, which is the point.
                "attributes": async_redact_data(
                    dict(attributes.attributes or {}), TO_REDACT
                )
                if attributes is not None
                else None,
                "attributes_present": attributes is not None,
                # NEVER READS ZERO FOR "NO PUSH". None means this device has
                # received nothing over MQTT since setup, which is a different
                # answer from "a push arrived 0 seconds ago" and is the one
                # that distinguishes a silent mower from a silent session.
                "seconds_since_mqtt_push": (
                    round(now - last_push, 1) if last_push is not None else None
                ),
                # PER CHANNEL, WHICH IS THE WHOLE QUESTION. The figure above
                # moves on every `state` frame, so it reads healthy while
                # `attributes` -- the only carrier a mowing schedule or a
                # blade figure could still be on -- has published nothing at
                # all. Split out, one dump separates the three answers that
                # `attributes_present: false` collapses into one: never
                # published, published empty, or published before this entry
                # was listening. Read `seconds_since_frame.attributes` FIRST:
                # None means the channel has never spoken since setup, and
                # `device_record_age_seconds` says how long that has been.
                "mqtt_frames": coordinator.mqtt_frame_counts(),
                "seconds_since_frame": coordinator.mqtt_seconds_since_frame(now),
                # The poll reads the SDK's own cache, so a frame can reach the
                # coordinator without its callback running. Counted apart, or
                # zero frames beside a present payload reads as a contradiction
                # when it is just the other door.
                "mqtt_cache_pickups": coordinator.mqtt_cache_pickups(),
                "mqtt_push_is_recent": coordinator.mqtt_push_is_recent(now),
            }
        )
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        # THE INHERITED-KEY TRAP, NAMED RATHER THAN HIDDEN. This entry was
        # created by NavimowHA and still stores its `mqtt_broker`, `mqtt_port`,
        # `mqtt_username`, `mqtt_password` and `api_base_url` keys. This
        # component reads none of them -- it resolves the broker and its
        # credentials from `mqtt/userInfo/get/v2` on every setup -- so
        # `mqtt_username: null` in the dump above says nothing whatever about
        # whether MQTT is authenticated. It has already been read as proof
        # that it was not. Listing the dead keys costs one line and answers
        # that misreading beside the thing that caused it; deleting them from
        # the dump would instead hide what the entry really holds.
        "entry_keys_unused": sorted(set(entry.data) - ENTRY_KEYS_USED),
        # THE LIVE SESSION, which is what every question about this channel
        # actually turns on. `is_connected` is a public NavimowSDK property
        # (it forwards to paho's own `is_connected()`), so this needs no reach
        # into `sdk._mqtt`. Without it a dump showing no attributes and no
        # events cannot say whether the broker session is down or the mower is
        # simply docked and asleep with nothing to publish -- the two have
        # identical symptoms and opposite fixes.
        "mqtt": {
            "connected": runtime.sdk.is_connected if runtime.sdk is not None else None,
            "broker": runtime.mqtt_broker,
            "port": runtime.mqtt_port,
            "transport": runtime.mqtt_transport,
            # THE PATH IS HALF THE ENDPOINT. Broker and port read correct for
            # a whole release while the session never came up, because the
            # upgrade was being sent to "/" and the gateway answers that with
            # 502. The live path carries the account's userId; that segment
            # is published as a placeholder.
            "ws_path": _mask_user_id(runtime.mqtt_ws_path, runtime.mqtt_user_id),
            # paho retries a failed connection silently; this is the only
            # count of it anywhere. 0 with connected=false means no attempt
            # has failed YET, which right after setup is not the same as
            # none failing.
            "connect_failures": runtime.mqtt_connect_failures,
            "seconds_since_connect_failure": (
                round(now - runtime.mqtt_last_connect_failure_monotonic, 1)
                if runtime.mqtt_last_connect_failure_monotonic is not None
                else None
            ),
            # WHAT THE CLOUD ACTUALLY SENT, in the only form that is safe to
            # print: key names and URL schemes, never a value. The endpoint
            # resolver was once corrected against a descriptor shape nobody
            # had read, and the correction was wrong about the real one.
            "descriptor": runtime.mqtt_descriptor,
            # THE NAMESPACE, NOT THE GUESS. The SDK's three subscriptions are
            # its own reading of the vendor's topic layout, so `mqtt_frames`
            # above can only count what was guessed at. This is every frame
            # the broker delivered under `/downlink/vehicle/<id>/#`, by the
            # topic it arrived on, with the union of top-level payload key
            # names -- so a topic the SDK never heard of shows up here, and
            # whether anything schedule- or blade-shaped is on the wire is
            # read off the names. `wildcard_granted: false` means the broker
            # refused the wildcard and this is the three topics again.
            "wildcard_granted": runtime.mqtt_wildcard_granted,
            # THE BROKER'S VERDICT ON EACH SUBSCRIPTION, the SDK's three
            # included. The `#` wildcard was refused on the live broker, and
            # nothing before this could say whether `realtimeDate/attributes`
            # was ever granted either -- a refused topic and a granted one
            # that never speaks read identically. "sent" means no SUBACK yet.
            "subscriptions": {
                _mask_device_ids(topic, runtime.devices): verdict
                for topic, verdict in sorted(runtime.mqtt_subscriptions.items())
            },
            "topics": {
                _mask_device_ids(topic, runtime.devices): {
                    "frames": record["frames"],
                    "seconds_since_frame": (
                        round(now - record["last_monotonic"], 1)
                        if record["last_monotonic"] is not None
                        else None
                    ),
                    "payload_keys": sorted(record["keys"]),
                }
                for topic, record in sorted(runtime.mqtt_topic_census.items())
            },
        },
        "devices": devices,
    }


def _mask_device_ids(topic: str, devices: list[Any]) -> str:
    """A topic carries the vehicle serial; publish the shape, not the serial."""
    for device in devices:
        device_id = str(getattr(device, "id", "") or "")
        if device_id:
            topic = topic.replace(device_id, "<deviceId>")
    return topic
