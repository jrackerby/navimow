#!/usr/bin/env python3
"""model.py's judgement, which is the whole reason this component was written.

WHAT THIS PROVES AND WHAT IT DOES NOT. model.py imports nothing -- not
homeassistant, not mower_sdk -- so this suite runs with neither installed and
a red result is attributable to a decision in this repo rather than to a core
release or a vendor SDK bump moving under it. It says NOTHING about whether
the cloud actually emits the strings tested here: those were read out of
mower_sdk.models._RAW_STATE_TO_CANONICAL at 0.1.2, and if the vendor renames a
state this suite stays green while the mower reads `unknown` on the wall. That
is what sensor.<name>_raw_state exists to make visible.

The headline case is offline_is_not_error(). NavimowHA composed
`_RAW_STATE_TO_CANONICAL["Offline"] = "unknown"` with
`MOWER_STATUS_TO_ACTIVITY["unknown"] = "error"`, so a mower that had merely
lost its uplink reported a fault needing assistance.
"""
import importlib.util
import os
import sys

# tests/ sits directly under the component root in both layouts: this repo
# standing alone, and this repo installed as custom_components/navimow.
# One expression covers both.
COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(COMPONENT, "model.py")

# The canonical values mower_sdk.models._RAW_STATE_TO_CANONICAL can produce,
# copied from the SDK at 0.1.2 -- the input alphabet this map has to be total
# over. Kept here rather than imported so the suite needs no vendor install.
SDK_CANONICAL_STATES = (
    "docked", "idle", "mowing", "paused", "returning", "error", "unknown",
)
# `charging` is a MowerStatus member the SDK can also emit directly.
ALL_STATES = SDK_CANONICAL_STATES + ("charging",)

# LawnMowerActivity's complete membership, read from core's
# lawn_mower/const.py. Anything model.py returns must be one of these or None.
CORE_ACTIVITIES = frozenset({"error", "paused", "mowing", "docked", "returning"})

FAILURES = []


def _load():
    spec = importlib.util.spec_from_file_location("navimow_model", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = _load()


def check(condition, message):
    if not condition:
        FAILURES.append(message)


# -- activity ---------------------------------------------------------------

def test_offline_is_not_error():
    """THE BUG. `unknown` is where mower_sdk lands both spellings of Offline."""
    check(
        m.resolve_activity("unknown") is None,
        "resolve_activity('unknown') must be None (state `unknown`), not "
        f"{m.resolve_activity('unknown')!r} -- an offline mower is not a faulted one",
    )
    check(
        m.resolve_activity("unknown") != "error",
        "resolve_activity('unknown') must never be 'error' -- this is the "
        "defect this component exists to fix",
    )


def test_unrecognised_state_is_unknown_not_error():
    for junk in ("isDoingSomethingNew", "", "MOWING", "42"):
        check(
            m.resolve_activity(junk) is None,
            f"resolve_activity({junk!r}) must be None -- a gap in our own map "
            "is not evidence the mower is faulted",
        )


def test_every_sdk_state_maps_into_core_or_none():
    """Totality. A state the SDK can emit must not fall through by accident."""
    for state in ALL_STATES:
        value = m.resolve_activity(state)
        check(
            value is None or value in CORE_ACTIVITIES,
            f"resolve_activity({state!r}) = {value!r}, which is neither None "
            f"nor a LawnMowerActivity member {sorted(CORE_ACTIVITIES)}",
        )


def test_real_states_still_map_as_the_boards_expect():
    """Downstream templates and dashboards gate on exactly these four words.
    Changing any of them silently changes what every consumer reports, with
    no error anywhere to point at."""
    for state, expected in (
        ("mowing", "mowing"),
        ("returning", "returning"),
        ("paused", "paused"),
        ("error", "error"),
        ("docked", "docked"),
        ("charging", "docked"),
        ("idle", "docked"),
    ):
        actual = m.resolve_activity(state)
        check(
            actual == expected,
            f"resolve_activity({state!r}) = {actual!r}, expected {expected!r}",
        )


def test_none_state_is_none():
    check(m.resolve_activity(None) is None, "resolve_activity(None) must be None")


# -- reachability -----------------------------------------------------------

def test_reachability_is_a_separate_axis():
    check(m.is_reachable("unknown", None) is False,
          "is_reachable('unknown', None) must be False -- that is the offline state")
    check(m.is_reachable("mowing", None) is True,
          "a mowing mower with no explicit online flag is reachable")
    check(m.is_reachable("mowing", False) is False,
          "an explicit online=False outranks an activity reading")
    check(m.is_reachable(None, None) is None,
          "is_reachable(None, None) must be None -- 'we were not told' is not 'offline'")


def test_charging_is_separable_from_docked():
    """Core's LawnMowerActivity has no CHARGING member, so both read DOCKED on
    the mower entity. This axis is the only place they differ."""
    check(m.is_charging("charging") is True, "charging state must read charging")
    check(m.is_charging("docked") is False, "docked-and-full must not read charging")
    check(m.is_charging(None) is None, "no state means no charging opinion")
    check(m.resolve_activity("charging") == m.resolve_activity("docked"),
          "both must still present as DOCKED on the mower entity")


# -- fault ------------------------------------------------------------------

def test_error_none_is_not_a_fault():
    """mower_sdk builds {'code': 'none'} from MowerError.NONE. Treating that as
    a fault would light the problem sensor permanently."""
    check(m.normalise_error({"code": "none"}) == (None, None),
          "code 'none' must normalise to no error")
    check(m.normalise_error(None) == (None, None), "absent error is no error")
    check(m.normalise_error({}) == (None, None), "empty error dict is no error")
    check(m.normalise_error({"code": "stuck", "message": "blade jam"})
          == ("stuck", "blade jam"),
          "a real error must survive normalisation with its message")


def test_problem_and_offline_do_not_collapse():
    check(m.is_problem(None, "unknown") is None,
          "offline must not read as a problem -- nobody has to walk to the garden")
    check(m.is_problem(None, "mowing") is False, "a mowing mower has no problem")
    check(m.is_problem({"code": "stuck"}, "mowing") is True,
          "an error code outranks the activity string -- that is the second source")
    check(m.is_problem(None, "error") is True,
          "an error activity is a problem even with no code")
    check(m.is_problem(None, None) is None,
          "no reading at all is None, not False")


# -- events -----------------------------------------------------------------

def test_event_bucket_is_total_and_declared():
    """EventEntity rejects a type outside _attr_event_types, so any bucket this
    returns must be in EVENT_TYPES or events are dropped at the core boundary."""
    cases = [
        ("error", "x"), ("ERROR", "x"), ("fatal", "x"), ("critical", "x"),
        ("warn", "x"), ("Warning", "x"), ("info", "x"), ("notice", "x"),
        (None, "errorBladeJam"), (None, "somethingNew"), (None, None),
        ("wat", "wat"), ("", ""),
    ]
    for level, event in cases:
        bucket = m.event_bucket(level, event)
        check(bucket in m.EVENT_TYPES,
              f"event_bucket({level!r}, {event!r}) = {bucket!r}, not in EVENT_TYPES")
    check(m.event_bucket("error", None) == "error", "level error -> error")
    check(m.event_bucket("warn", None) == "warning", "level warn -> warning")
    check(m.event_bucket(None, "errorFoo") == "error", "error-prefixed name -> error")
    check(m.event_bucket(None, "jobComplete") == "other",
          "an unrecognised name must land in `other`, not be guessed at")


# -- the suite's own falsifiability -------------------------------

def test_a_scheme_bearing_host_is_never_used_as_a_hostname():
    """THE LIVE DEFECT. Measured on an X430 through the estate's own instance.

    mqtt/userInfo/get/v2 returned `mqttHost` = "wss://mqtt-fra.navimow.com"
    and no `mqttUrl` at all. The resolver handed that back unchanged as a bare
    hostname on 1883 with no ws_path, so paho was asked for a TCP connection
    to a name containing "wss://" -- which cannot resolve, and which
    connect_async never raises about. Setup reported ready, every entity came
    up on the HTTP fallback, and the push channel was never connected once.

    Diagnostics read `connected: false`, `broker: wss://mqtt-fra.navimow.com`,
    `transport: tcp`, `seconds_since_mqtt_push: null` -- which is how it was
    finally found, and why it could not be found before those keys existed.
    """
    broker, port, ws_path = m.mqtt_endpoint({"mqttHost": "wss://mqtt-fra.navimow.com"})
    check(broker == "mqtt-fra.navimow.com",
          f"broker is {broker!r}; a scheme-bearing host must be parsed, never "
          "passed to a TCP connect as-is")
    check("://" not in (broker or ""),
          f"broker {broker!r} still carries a URL scheme -- this is the defect")
    check(port == 443, f"wss must resolve to 443, got {port}")
    check(ws_path == "/", f"wss must carry a ws_path, got {ws_path!r}")


def test_a_non_websocket_mqtt_url_does_not_veto_a_websocket_host():
    """THE ROLLBACK CASE. This shape reached production and broke setup.

    The live cloud sends BOTH: an `mqttUrl` that is not a ws/wss URL, and an
    `mqttHost` that is. A resolver that picks mqttUrl when present and gives
    up when it is not a websocket URL returns no broker at all -- which raises
    ConfigEntryNotReady("returned no MQTT broker address"), put the entry into
    setup_retry on the live instance, and had to be reverted.

    Preferring EITHER field unconditionally gets one of the two real payloads
    wrong, so every field is tried and one that yields nothing is skipped
    rather than fatal.
    """
    for descriptor, why in (
        ({"mqttUrl": "mqtt://mqtt-fra.navimow.com:1883",
          "mqttHost": "wss://mqtt-fra.navimow.com"}, "a non-ws mqttUrl"),
        ({"mqttUrl": "mqtt-fra.navimow.com:443",
          "mqttHost": "wss://mqtt-fra.navimow.com"}, "an mqttUrl with no scheme"),
    ):
        broker, port, ws_path = m.mqtt_endpoint(descriptor)
        check(broker == "mqtt-fra.navimow.com",
              f"{why} beside a wss mqttHost resolved broker {broker!r}; it must "
              "fall through to the host, not veto it -- this exact shape went "
              "to setup_retry in production")
        check(port == 443 and ws_path == "/",
              f"{why}: resolved port={port} ws_path={ws_path!r}, not 443 and '/'")


def test_a_scheme_less_mqtt_url_is_the_websocket_path():
    """THE SECOND LIVE DEFECT, on the same X430, one release after the first.

    The cloud sends `mqttHost` = "wss://mqtt-fra.navimow.com" AND `mqttUrl` =
    "/mqtt/<userId>" -- no scheme, which is why the scheme-driven resolver
    skipped it and defaulted the path to "/". The gateway in front of the
    broker answers "/" with 502 and "/mqtt/<anything>" with 101, and paho
    retries a failed upgrade silently, so broker, port and TLS all read right
    while the session never once came up. navimow-sdk's own client hands
    `mqttUrl` to paho verbatim as the websocket path; so does this.
    """
    broker, port, ws_path = m.mqtt_endpoint({
        "mqttHost": "wss://mqtt-fra.navimow.com", "mqttUrl": "/mqtt/6201934",
    })
    check((broker, port) == ("mqtt-fra.navimow.com", 443),
          f"live descriptor resolved {broker!r}:{port}")
    check(ws_path == "/mqtt/6201934",
          f"live descriptor resolved ws_path {ws_path!r}; the vendor's path "
          "field is being ignored and the upgrade goes to '/' (502)")
    # A path in the URL itself still outranks the bare field.
    check(m.mqtt_endpoint({"mqttHost": "wss://h/explicit", "mqttUrl": "/other"})[2]
          == "/explicit",
          "a path carried by the wss URL no longer wins over the bare field")
    # And a scheme-less value that is NOT a path is still not guessed at.
    check(m.mqtt_endpoint({"mqttHost": "wss://h", "mqttUrl": "h:443"})[2] == "/",
          "a scheme-less host:port mqttUrl is being used as a path")


def test_the_descriptor_shape_reveals_no_value():
    """Key names and schemes are publishable; values never are. This is what
    makes the payload readable from a dump instead of reasoned about."""
    shape = m.mqtt_descriptor_shape({
        "mqttUrl": "mqtt://h:1883", "mqttHost": "wss://h",
        "userName": "a-real-username", "pwdInfo": "a-real-password",
    })
    check(shape["keys"] == ["mqttHost", "mqttUrl", "pwdInfo", "userName"],
          f"descriptor keys are {shape['keys']!r}")
    check(shape["schemes"] == {"mqttUrl": "mqtt", "mqttHost": "wss"},
          f"descriptor schemes are {shape['schemes']!r}")
    blob = repr(shape)
    for secret in ("a-real-username", "a-real-password", "h:1883"):
        check(secret not in blob,
              f"the descriptor shape leaked {secret!r} -- it may carry names "
              "and schemes only")
    # Present-but-bare is a finding, and must not read as absent.
    check(m.mqtt_descriptor_shape({"mqttHost": "plain.example"})["schemes"]
          == {"mqttHost": ""},
          "a scheme-less host does not report as present-with-no-scheme")
    # Whether mqttUrl is a path is the fact the resolver turns on; it is
    # published as a boolean because the live path carries the userId.
    path_shape = m.mqtt_descriptor_shape({"mqttHost": "wss://h", "mqttUrl": "/mqtt/42"})
    check(path_shape["mqttUrl_is_path"] is True,
          "a bare-path mqttUrl does not report mqttUrl_is_path")
    check("42" not in repr(path_shape), "the descriptor shape leaked the path")
    check(shape["mqttUrl_is_path"] is False,
          "a scheme-bearing mqttUrl reports as a bare path")


def test_a_clean_host_still_takes_the_plain_mqtt_path():
    """The fix must not convert every install to websockets. A host with no
    scheme is a hostname and still means plain MQTT on 1883."""
    check(m.mqtt_endpoint({"mqttHost": "mqtt.navimow.com"})
          == ("mqtt.navimow.com", 1883, None),
          "a bare mqttHost no longer resolves to plain MQTT on 1883")


def test_mqtt_url_still_wins_and_keeps_its_query():
    """ws_path carries the query when there is one: the vendor signs the
    websocket upgrade through it, so dropping it authenticates nothing."""
    check(m.mqtt_endpoint({"mqttUrl": "wss://a.example/mqtt?token=x",
                           "mqttHost": "a.example"})
          == ("a.example", 443, "/mqtt?token=x"),
          "mqttUrl no longer resolves, or its query was dropped")
    check(m.mqtt_endpoint({"mqttUrl": "ws://b.example:8083/mqtt"})
          == ("b.example", 8083, "/mqtt"),
          "an explicit ws port is not being honoured")


def test_an_unusable_descriptor_refuses_rather_than_guessing():
    """NO DEFAULT BROKER, and no scheme-bearing string smuggled through as a
    host either. Returning no broker is what raises ConfigEntryNotReady
    naming the real problem; NavimowHA's constant fallback is the
    counter-example."""
    for descriptor, why in (
        ({}, "an empty descriptor"),
        ({"mqttHost": "tcp://c.example"}, "a non-websocket scheme"),
        ({"mqttUrl": "mqtt://d.example"}, "a non-websocket mqttUrl"),
    ):
        broker, _, _ = m.mqtt_endpoint(descriptor)
        check(broker is None,
              f"{why} resolved to broker {broker!r} instead of refusing; "
              "setup would connect somewhere nobody verified")


def test_the_assertions_can_fail():
    """Every assertion set needs a self-test proving it CAN fail.

    Without this, a `check()` that silently stopped appending -- or a
    resolve_activity that returned a constant -- would report green over a
    suite that tested nothing. This deliberately breaks the headline invariant
    and asserts the checker notices.
    """
    before = len(FAILURES)
    original = m.CANONICAL_TO_ACTIVITY.copy()
    try:
        m.CANONICAL_TO_ACTIVITY["unknown"] = "error"  # reintroduce the defect
        test_offline_is_not_error()
        if len(FAILURES) == before:
            FAILURES.append(
                "SELF-TEST FAILED: reintroducing the NavimowHA offline->error "
                "mapping produced NO failure, so this suite cannot detect the "
                "one defect it exists to detect"
            )
            return
        # The injected failures are expected; discard them.
        del FAILURES[before:]
    finally:
        m.CANONICAL_TO_ACTIVITY.clear()
        m.CANONICAL_TO_ACTIVITY.update(original)
    # Prove the restore worked, or every test after this one is meaningless.
    if m.resolve_activity("unknown") is not None:
        FAILURES.append("SELF-TEST FAILED: could not restore CANONICAL_TO_ACTIVITY")

    # And the endpoint gate: reintroduce the live defect -- hand the host
    # back untouched -- and assert the check notices. A gate over a resolver
    # that quietly started returning its input would otherwise read green.
    before = len(FAILURES)
    original = m.mqtt_endpoint
    try:
        m.mqtt_endpoint = lambda info: (info.get("mqttHost"), 1883, None)
        test_a_scheme_bearing_host_is_never_used_as_a_hostname()
        if len(FAILURES) == before:
            FAILURES.append(
                "SELF-TEST FAILED: reinstating the pass-the-host-through "
                "resolver produced NO failure, so the endpoint gate cannot "
                "detect the defect it was written for"
            )
        else:
            del FAILURES[before:]
    finally:
        m.mqtt_endpoint = original
    if m.mqtt_endpoint({"mqttHost": "wss://x.example"})[0] != "x.example":
        FAILURES.append("SELF-TEST FAILED: could not restore mqtt_endpoint")
    # The path gate: reinstate the resolver that shipped in 1.2.1 -- scheme
    # decides, bare mqttUrl ignored, path defaults to "/" -- and assert the
    # live-shape test trips on it.
    before = len(FAILURES)
    try:
        m.mqtt_endpoint = lambda info: ("mqtt-fra.navimow.com", 443, "/")
        test_a_scheme_less_mqtt_url_is_the_websocket_path()
        if len(FAILURES) == before:
            FAILURES.append(
                "SELF-TEST FAILED: the 1.2.1 resolver (ws_path always '/') "
                "produced NO failure, so the path gate cannot detect the "
                "defect it was written for"
            )
        else:
            del FAILURES[before:]
    finally:
        m.mqtt_endpoint = original


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} finding(s) across {len(tests)} tests")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"PASS: {len(tests)} tests, model.py judgement intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
