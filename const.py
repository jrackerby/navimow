"""Constants and the endpoints the vendor cloud is reached on.

The pure judgement lives in model.py, which imports nothing. This file holds
only values and the reasoning behind the ones that are not obvious.
"""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

from .model import DOMAIN  # noqa: F401  -- re-exported; model.py owns the string

# OAuth2. These are the vendor's own published values, verbatim from
# segwaynavimow/NavimowHA's const.py: the "secret" is a fixed string shipped
# inside a public HACS repository and burned into their hosted login page, so
# it is an identifier, not a credential, and moving it into
# application_credentials would ask the user to paste back a constant they
# cannot change. If Navimow ever issues per-installation credentials this
# becomes an application_credentials integration and this comment is the
# reason it was not one first.
OAUTH2_AUTHORIZE: Final = (
    "https://navimow-h5-fra.willand.com/smartHome/login?channel=homeassistant"
)
OAUTH2_TOKEN: Final = "https://navimow-fra.ninebot.com/openapi/oauth/getAccessToken"
CLIENT_ID: Final = "homeassistant"
CLIENT_SECRET: Final = "57056e15-722e-42be-bbaa-b0cbfb208a52"
API_BASE_URL: Final = "https://navimow-fra.ninebot.com"

# THE ONE ENDPOINT THIS COMPONENT CALLS ITSELF RATHER THAN THROUGH THE SDK.
# navimow-sdk's `MowerAPI.async_get_devices` GETs this path and hands every
# record to `Device.from_dict`, which reads `firmware_version` -- a key the
# vendor does not send. The raw record carries exactly `firmware`, `id`,
# `model`, `name` (read 2026-09-14, #22), so the parsed object's
# `firmware_version` is permanently "" and nothing downstream can recover it:
# the SDK returns parsed objects only. `__init__._async_read_devices` takes
# this one call itself and reads both off the same response. Verbatim the
# SDK's own path at 0.1.2; a divergence surfaces as the `code != 1` refusal
# that function raises, not as a silent empty list.
AUTH_LIST_ENDPOINT: Final = "/openapi/smarthome/authList"

# THE BRAND MARK, SERVED BY HOME ASSISTANT ITSELF, NOT BY US AND NOT BY A CDN.
# Since core 2026.3 a custom integration ships its own brand images in a
# `brand/` directory and core proxies them at this path -- so this needs no
# `www/` write, no `register_static_path`, and no entry in the
# home-assistant/brands repository. It is a RELATIVE url on purpose: a surface
# resolves it against whatever origin it reached Home Assistant on, so the same
# attribute is correct on the local address, through the Nabu Casa relay, and
# behind a dashboard app's own `/api/*` proxy. An absolute url would pin one of
# those three and break the other two.
BRAND_ICON_URL: Final = f"/api/brands/integration/{DOMAIN}/icon.png"

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.EVENT,
    Platform.LAWN_MOWER,
    Platform.SENSOR,
]

# POLLING. State arrives by MQTT push; this interval is NOT a state poll and
# must not be read as one. It does two things: keeps the OAuth token fresh so
# a command issued after a long idle does not meet CODE_OAUTH_INFO_ILLEGAL,
# and gives the stale-MQTT check below somewhere to run.
#
# NavimowHA ran this at 30 SECONDS, which is 2,880 token refreshes a day
# against an HTTP fallback that is rate-limited to one an hour -- i.e. the
# interval was set by the shortest thing on the timer rather than by what any
# of them needed. The vendor's tokens last 1-2 days (auth.py). Fifteen minutes
# is 96 a day and still refreshes ~100x inside the shortest token lifetime.
# Quality scale `appropriate-polling`.
UPDATE_INTERVAL_SECONDS: Final = 900

# How long without an MQTT message before we stop trusting the cached state
# and pay for an HTTP read. Five minutes, inherited: the mower publishes on
# change, so silence is normal and this is a liveness bound, not a refresh.
MQTT_STALE_SECONDS: Final = 300

# Floor between HTTP fallback reads, so a broker outage cannot turn the
# fallback into a poll of its own.
HTTP_FALLBACK_MIN_INTERVAL: Final = 3600

# MQTT keepalive. The broker drops idle connections around the hour mark;
# PINGREQ/PINGRESP at 40 minutes holds the session open through it.
MQTT_KEEPALIVE_SECONDS: Final = 2400
MQTT_RECONNECT_MIN_DELAY: Final = 1
MQTT_RECONNECT_MAX_DELAY: Final = 60

# Entity keys. 'battery' is INHERITED from NavimowHA and its unique_id must
# not move -- model.py's docstring carries the full reason. The rest are new.
KEY_BATTERY: Final = "battery"
KEY_SIGNAL: Final = "signal_strength"
KEY_ERROR: Final = "error"
KEY_RAW_STATE: Final = "raw_state"
KEY_SESSION_TIME: Final = "mowing_time"
KEY_TOTAL_TIME: Final = "total_mowing_time"
KEY_PROBLEM: Final = "problem"
KEY_CONNECTIVITY: Final = "connectivity"
KEY_CHARGING: Final = "charging"
KEY_STOP: Final = "stop"
KEY_EVENTS: Final = "events"
KEY_TRACKER: Final = "tracker"
