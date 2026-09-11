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
