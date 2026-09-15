<picture>
  <source media="(prefers-color-scheme: dark)" srcset="brand/dark_logo.png">
  <img alt="Segway Navimow" src="brand/logo.png" width="420">
</picture>

# Navimow

Home Assistant integration for Segway Navimow robot mowers, built against the
vendor's own `navimow-sdk` cloud API. It replaces
[`segwaynavimow/NavimowHA`](https://github.com/segwaynavimow/NavimowHA) on the
same `navimow` domain and deliberately re-emits that integration's `unique_id`
strings, so existing entity ids and their history survive the swap.

## What it does

State arrives by MQTT push from the Navimow cloud. Commands go out over the
REST API. A 15-minute timer keeps the OAuth token fresh and falls back to an
HTTP status read if push has been silent for five minutes.

| Entity | Notes |
| --- | --- |
| `lawn_mower` | Start / pause / dock. Start dispatches to the cloud's RESUME command when the mower is paused, because start and resume are different calls. |
| `sensor` battery | Inherited from NavimowHA, same `unique_id`. |
| `sensor` signal strength | Diagnostic, disabled by default. |
| `sensor` error code | The fault code as a state, so history answers "when did it last jam". |
| `sensor` reported state | The vendor's untranslated state string. Diagnostic. |
| `sensor` session / total mowing time | Disabled by default; total is `TOTAL_INCREASING` so it becomes a long-term statistic. |
| `binary_sensor` problem | Fault, independent of the activity string. |
| `binary_sensor` cloud connectivity | Whether the cloud can reach the mower. Stays available when the mower does not. |
| `binary_sensor` charging | Core's `LawnMowerActivity` has no CHARGING member; this is the only place docked-and-charging differs from docked-and-full. |
| `device_tracker` | GPS position. Disabled by default. |
| `button` stop | `MowerCommand.STOP` has no `LawnMowerEntityFeature`, so it cannot live on the mower entity. |
| `event` | The MQTT event channel. |

## Why it exists

`navimow-sdk` already parses six things NavimowHA never surfaced, and
subscribes a third MQTT topic it never listened to:

- **The event channel was dead.** `NavimowMQTT.subscribe_all()` subscribes
  `/downlink/vehicle/<id>/realtimeDate/event` for every device and the SDK
  decodes each message into a `DeviceEventMessage`, but NavimowHA registered
  `on_state` and `on_attributes` and never `on_event`. Every event the mower
  published was parsed and then dropped inside the SDK.
- **An offline mower reported `error`.** `mower_sdk` maps the cloud's
  `"Offline"` to canonical `"unknown"`, and NavimowHA's `const.py` mapped
  `"unknown"` to `LawnMowerActivity.ERROR`. Losing the uplink is not a fault
  needing assistance; reachability is now its own axis, answered in both
  directions.
- **`error`, `position`, `signal_strength`, `mowing_time`,
  `total_mowing_time` and `online` were attributes**, where the recorder keeps
  no series, statistics cannot reach them and no automation can trigger on
  them. (`online` turned out to be nothing at all: the vendor's `authList`
  record has no such key, and the value was the SDK's default -- see
  `model.is_reachable`.)
- **The MQTT password was logged at INFO** on every setup, masked as
  `first2***last2` — four real characters of a live secret. Nothing derived
  from a credential is logged by this component at any level, and
  `diagnostics.py` redacts rather than masks. The vendor SDK underneath still
  logs the broker username and the `Authorization` header masked the same way,
  at INFO in `mower_sdk.mqtt`, so those records are dropped by a filter on that
  logger — keyed on the format string, not the mask — and
  `tools/check_sdk_log_lines.py` fails CI the release the SDK adds one the
  filter cannot see.
- **`set_blade_height` was registered and raised unconditionally.** A service
  that can only fail still appears in the UI and in every automation picker.
  It is gone rather than ported.

## What the cloud does not expose

**There is no mowing schedule and no blade/service data on this API, and that
is a property of the API rather than a gap in this component.** The
whole surface is five endpoints — `authList`, `getVehicleStatus`,
`sendCommands`, `responseCommands`, `mqtt/userInfo/get/v2` — and its command
vocabulary is Google Smart Home (`action.devices.commands.StartStop`,
`PauseUnpause`, `Dock`), a schema with no mower schedule or service concept to
carry. `segwaynavimow/navimow-sdk` at HEAD is byte-identical to the published
0.1.2 wheel, and the independent `niddu85/home-assistant-navimow` reaches the
same four `/openapi/smarthome/*` paths, so this is two implementations
converging rather than one author missing an endpoint. Segway keeps blade life
in the phone app instead, under Settings > MOWER > Maintenance & Tools > Part
Maintenance, at roughly 80 hours.

Two things are open rather than answered, and `diagnostics.py` is the
instrument for the first:

- The MQTT `attributes` channel (`/downlink/vehicle/<id>/realtimeDate/attributes`)
  carries a free-form dict nobody has characterised. Diagnostics dumps it
  whole and unfiltered for that reason — selecting keys would need us to
  already know which ones matter. Download diagnostics after a real mowing
  session to see what is actually on it.

  That topic is navimow-sdk's *guess* at the vendor's layout (its topic
  helpers still carry the author's TODO to adjust to the real format), so
  the session also takes the vehicle's whole `/downlink/vehicle/<id>/#`
  namespace and the dump's `mqtt.topics` counts every frame by the topic it
  actually arrived on, with the union of its top-level payload key names. A
  topic the SDK never subscribed shows up there; a channel that never
  speaks does not. Read `mqtt.wildcard_granted` first — `false` means the
  broker refused every wildcard and the census is the three guessed topics
  again. The three stay subscribed beside a granted wildcard — a grant is not
  proof the gateway routes wildcard matches — and a byte-identical second
  copy is counted under `duplicates`, never forwarded. `mqtt.subscriptions` carries the broker's SUBACK verdict for every
  topic subscribed, the SDK's three included — a refused topic and a granted
  one that never speaks used to read identically.

  Read the dump's `mqtt.connected` and `seconds_since_mqtt_push` before
  concluding anything from an empty `attributes`. A docked mower sleeps and
  publishes nothing, which produces a dump identical to a dead broker session;
  those two have opposite fixes. **The first dump carrying those keys found a
  real one**: `mqtt/userInfo/get/v2` returns the whole endpoint in `mqttHost`
  (`wss://mqtt-fra.navimow.com`) **and** an `mqttUrl` that is not a websocket
  URL, and the resolver had been handing the host string to paho as a TCP
  hostname on 1883. It cannot resolve, `connect_async` never raises, so setup
  reported ready and the push channel was never connected once — every entity
  was quietly running on the HTTP fallback. `model.mqtt_endpoint` now tries
  every field that could carry an endpoint, takes the first that yields a
  `ws`/`wss` one, and refuses rather than passing a scheme-bearing string
  through as a host.

  **Do not install v1.2.0.** It fixed the host but let the non-websocket
  `mqttUrl` veto the websocket `mqttHost`, so it resolved no broker at all and
  put the entry into `setup_retry`; v1.2.1 supersedes it. The lesson is in
  `mqtt_descriptor_shape`: that resolver was corrected against descriptor
  shapes that were reasoned about rather than read, because every field
  carrying one sits beside `userName`/`pwdInfo` and so was never dumped. The
  dump now carries `mqtt.descriptor` — key names and URL schemes, never a
  value — so the payload's shape is a read rather than a guess.

  Note also that `entry` still stores
  NavimowHA's `mqtt_username` / `mqtt_password` / `mqtt_broker` keys on an
  upgraded installation — this integration reads none of them and re-resolves
  the broker and its credentials from `mqtt/userInfo/get/v2` on every setup,
  so a `null` there is not an authentication finding. `entry_keys_unused`
  lists them for exactly that reason.
- Segway's published Open API is documented for the **X3 series Expansion
  Bay**; this was developed against an X430 (X4 series). Whether it reaches X4
  is unverified.

`set_blade_height` is not implemented here, and the SDK's own version does not
work either: it publishes to `navimow/<id>/command`, which does not match the
broker's real `/downlink/vehicle/<id>/realtimeDate/*` scheme.

## Branding

`brand/` carries the Segway Navimow mark at the sizes Home Assistant core
wants: `icon` square at 256/512, `logo` with its short side at 256/512, and a
`dark_` variant of each because the wordmark is black and would otherwise be
invisible against every dark theme. Since core **2026.3** a custom integration
serves its own brand images from that directory — core proxies them at
`/api/brands/integration/navimow/icon.png`, they take priority over the brands
CDN, and nothing needs submitting to `home-assistant/brands` or declaring in
`manifest.json`.

The same served path is the mower entity's `entity_picture`, so any surface
that renders the entity gets the mark — Home Assistant's own dashboard and an
external board alike, since it rides on the entity rather than on one
dashboard's config. It is set on the **mower entity only**: a picture outranks
an icon, so putting it on the whole set would cost the battery, signal and
error entities the meaning their icons carry. The trade-off it does make is
that a `lawn_mower`'s icon normally tracks its activity and a fixed picture
does not — docked, mowing and errored look alike on a picture-only tile.
`binary_sensor.<name>_problem` still carries the fault on its own axis.

**HACS still shows a generic icon, and that is not fixable from here.** HACS
fetches integration icons from `data-v2.hacs.xyz`, which has no entry for
custom integrations, and it does not fall back to the local brands API —
[hacs/integration#5171](https://github.com/hacs/integration/issues/5171), open,
with no documented workaround. The only route to a HACS icon is the legacy
`custom_integrations/` folder of `home-assistant/brands`.

The marks are Segway's, used to identify the product this integration talks
to; `brand/` mirrors what the vendor's own integration shipped.

## Installation

### HACS

1. In Home Assistant: **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/jrackerby/navimow` with category **Integration**.
3. Install **Navimow**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → "Navimow"**.

### Manual

The integration lives at the repository **root**, not under
`custom_components/` — `hacs.json` declares `content_in_root: true`. To install
by hand, copy this repository's contents into
`config/custom_components/navimow/` and restart Home Assistant.

Either way a `custom_components/` change needs a **full Home Assistant
restart**; `homeassistant.reload_core_config` does not re-import a custom
component.

### Migrating from NavimowHA

This integration takes over the `navimow` domain from
[`segwaynavimow/NavimowHA`](https://github.com/segwaynavimow/NavimowHA) and
re-emits its `unique_id` strings, so entity ids and their history survive the
swap. Before installing:

1. Remove the NavimowHA custom repository from HACS **and delete its config
   entry** first. Two integrations on the same domain cannot both load, and
   two entries would share one set of broker credentials and displace each
   other.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Navimow.** The OAuth
   client is registered automatically; you are not asked for one. Sign in with
   the same account the Navimow phone app uses.

The sign-in reaches Navimow's servers in China. A DNS filter or ad-blocker in
front of Home Assistant can break both the login and the MQTT session.

## Configuration

None. There are no options, no YAML and no parameters — the account is the
whole configuration.

## Removal

Delete the config entry from **Settings → Devices & Services**. That
disconnects MQTT and removes every entity and the device. Remove the
`custom_components/navimow/` directory and restart to uninstall the code.

## Re-authentication

Navimow access tokens last a day or two and the account is issued **no refresh
token**, so being asked to sign in again is the normal end of a session, not a
fault. Home Assistant raises a repair notification; signing in again through it
updates the existing entry rather than creating a second one.

## Tests

Three suites, run with neither Home Assistant nor the vendor SDK installed:

```
./tools/run_tests.sh                 # all three
python3 tests/test_model.py          # the activity/fault/reachability judgement
python3 tests/test_migration.py      # the inherited unique_ids
python3 tests/test_wiring.py         # platform, translation and credential gates
```

They run as the `tests` job in `.github/workflows/validate.yml`, alongside a
`hassfest` job and an `imports` job that installs the core version `hacs.json`
declares support for and imports every module against it — which is the only
gate that catches a `homeassistant.*` symbol moving, since the three suites
above deliberately run with core absent.

What they do **not** cover is stated in each file's docstring;
`quality_scale.yaml` records which quality-scale rules are met and which four
are still `todo`; the standard itself is `jrackerby/HA` LAW §15.
