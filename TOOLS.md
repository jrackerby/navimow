# TOOLS

Traps in the instruments THIS repo owns: `navimow-sdk` (the `mower_sdk`
package) and the vendor cloud behind it. Rules about the work are
jrackerby/HA's `tools/work_docs/LAW.md` and there is only ever one of those;
Home Assistant's own instrument traps are its `TOOLS.md` beside it, and no
line may be in two of them. Every line here is a claim with a timestamp --
re-verify before planning on it, and edit it when it stops being true.

## `mower_sdk.models.Device`

- **`from_dict` DEFAULTS EVERY ABSENT KEY SILENTLY, AND TWO OF ITS DEFAULTS
  READ AS VENDOR DATA.** At 0.1.2 `firmware_version` is
  `data.get("firmware_version", "")` while the vendor's key is `firmware`, and
  `online` is `data.get("online", False)` while the vendor sends no such key at
  all. The raw `authList` record carries exactly `firmware`, `id`, `model`,
  `name`. So a blank firmware and a `False` online are the SDK's own constants
  and not readings: `online` read `off` unbroken through two complete mowing
  sessions and was taken as the mower being asleep. **An empty or falsy field
  off a parsed `Device` is evidence about the SDK, never about the mower** --
  read the raw record before building anything on one.

- **`MowerAPI` RETURNS PARSED OBJECTS AND DROPS THE RESPONSE THEY CAME FROM**,
  so any vendor key `from_dict` does not map is unrecoverable downstream.
  `api._async_request(method, endpoint)` is the only way to the raw record and
  it is a PRIVATE: guard it with a three-argument `getattr` and fall back to
  the public read rather than failing setup over a field. Its contract for
  `authList` -- which is then a second copy of a contract the SDK also holds --
  is `code == 1` with the records under `data.payload.devices`.

## `NavimowSDK`

- **IT FORWARDS NO CONNECTION LIFECYCLE HOOK.** `on_state`, `on_attributes`
  and `on_event` are the whole facade, so disconnect, connect and paho's own
  `on_connect_fail` -- the only signal a failed TLS/websocket upgrade produces
  at all, since paho then retries silently with no CONNACK and no disconnect --
  are reachable only on the `NavimowMQTT` it wraps (`sdk._mqtt`) and on that
  object's paho client. `is_connected` is the one session fact the facade does
  answer, so nothing needs the private for that.

- **IT DISCARDS EVERY SUBSCRIBE'S `mid` AND WIRES NO `on_subscribe`**, so a
  topic the broker REFUSED (0x80) is indistinguishable from one it granted
  that never speaks. Its three `realtimeDate/*` topics are its own guess at the
  vendor's namespace and still carry the author's TODO to adjust them. The live
  broker refuses `/downlink/vehicle/<id>/#` and grants `realtimeDate/+`; a
  granted wildcard is not proof of routing, so the guessed three stay
  subscribed beside it and duplicate deliveries are gated instead.

## The vendor cloud

- **`mqttHost` CAN CARRY A WHOLE URL AND `mqttUrl` CAN CARRY A BARE PATH.**
  Measured on an X430: `mqttHost` = `wss://mqtt-fra.navimow.com`, `mqttUrl` =
  `/mqtt/<userId>`. Preferring either field unconditionally gets one of the two
  real payloads wrong, and the gateway in front of the broker answers `/` with
  502 and `/mqtt/<anything>` with 101 -- so a resolver that defaults the path
  produces a session that never comes up, silently, with every entity served by
  the HTTP fallback and nothing in the log to say why.

- **THE COMMAND VOCABULARY IS GOOGLE SMART HOME**, so START and RESUME are
  different commands (`StartStop {on: true}` vs `PauseUnpause {on: true}`) and
  there is no REST concept of a mowing schedule or a blade figure to read. If
  either exists on the wire it is on the undocumented
  `realtimeDate/attributes` channel and nowhere else.
