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
  them.
- **The MQTT password was logged at INFO** on every setup, masked as
  `first2***last2` — four real characters of a live secret. Nothing derived
  from a credential is logged here at any level, and `diagnostics.py` redacts
  rather than masks.
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
- Segway's published Open API is documented for the **X3 series Expansion
  Bay**; this was developed against an X430 (X4 series). Whether it reaches X4
  is unverified.

`set_blade_height` is not implemented here, and the SDK's own version does not
work either: it publishes to `navimow/<id>/command`, which does not match the
broker's real `/downlink/vehicle/<id>/realtimeDate/*` scheme.

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
are still `todo`.
