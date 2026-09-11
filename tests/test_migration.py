#!/usr/bin/env python3
"""Pins the unique_ids that keep the existing entity ids alive.

WHY THIS IS THE MOST IMPORTANT SUITE IN THE SET. This component replaces
segwaynavimow/NavimowHA on the SAME domain. Home Assistant's entity registry
keys a row on (platform, domain, unique_id); re-emit the same string and the
row -- entity_id, history, area, every reference to it -- is reused, change it
and HA mints a new row while the old one is orphaned. The new row then cannot
take the old id, because it is still occupied, so it lands on the next free
suffix (a platform assigning entity_id takes `_2` when the id is taken and
never reclaims it).

WHAT IS DELIBERATELY NOT HERE. A matching check that each CONSUMER of these
ids -- a template, a dashboard, an automation -- still names them belongs
wherever those consumers live, not here. Their subject is a file this
repository does not contain and must never assume is present, and a check that
silently skips when its subject is missing is the vacuous-green shape both
halves were written to refuse.

WHAT THIS DOES NOT PROVE. It compares against NavimowHA's unique_id
EXPRESSIONS as read from its source at 1.1.0, not against the live registry.
If the installed version is not the one these shapes were read from, or if the
rows were created by some third integration, the ids will still move and this
suite will still be green. Confirming that needs one read of the live entity
registry, which this suite deliberately cannot do.
"""
import importlib.util
import os
import re
import sys

# tests/ sits directly under the component root in both layouts: this repo
# standing alone, and this repo installed as custom_components/navimow.
# One expression covers both.
COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(COMPONENT, "model.py")

# NavimowHA 1.1.0, verbatim:
#   lawn_mower.py : self._attr_unique_id = f"{DOMAIN}_{device_id}"
#   sensor.py     : self._attr_unique_id = f"{DOMAIN}_{device.id}_{description.key}"
#   both          : identifiers={(DOMAIN, device.id)}
# with DOMAIN = "navimow" and the battery description key = "battery".
UPSTREAM_MOWER = "navimow_{device_id}"
UPSTREAM_SENSOR = "navimow_{device_id}_{key}"

FAILURES = []


def check(condition, message):
    if not condition:
        FAILURES.append(message)


spec = importlib.util.spec_from_file_location("navimow_model", MODEL)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_domain_is_unchanged():
    check(m.DOMAIN == "navimow",
          f"DOMAIN is {m.DOMAIN!r}; changing it orphans EVERY registry row, "
          "not just the two ids this suite names")


def test_mower_unique_id_matches_upstream():
    for device_id in ("abc123", "1234567890", "AA-BB-CC"):
        expected = UPSTREAM_MOWER.format(device_id=device_id)
        actual = m.mower_unique_id(device_id)
        check(actual == expected,
              f"mower_unique_id({device_id!r}) = {actual!r}, but NavimowHA "
              f"emitted {expected!r}. lawn_mower.navimow_x430_2 would become _3.")


def test_battery_unique_id_matches_upstream():
    for device_id in ("abc123", "1234567890"):
        expected = UPSTREAM_SENSOR.format(device_id=device_id, key="battery")
        actual = m.sensor_unique_id(device_id, "battery")
        check(actual == expected,
              f"sensor_unique_id({device_id!r}, 'battery') = {actual!r}, but "
              f"NavimowHA emitted {expected!r}. "
              "sensor.navimow_x430_battery_2 would become _3.")


def test_new_keys_do_not_collide_with_the_inherited_ones():
    """Every OTHER entity must mint a NEW row, never land on an inherited one."""
    const_src = open(os.path.join(COMPONENT, "const.py"), encoding="utf-8").read()
    keys = re.findall(r'^KEY_\w+:\s*Final\s*=\s*"([^"]+)"', const_src, re.M)
    check(len(keys) >= 10, f"only {len(keys)} KEY_* constants parsed out of "
                           "const.py -- the parse, not the component, is wrong")
    check("battery" in keys, "the inherited 'battery' key is missing from const.py")

    seen = {}
    for key in keys:
        uid = m.sensor_unique_id("DEV", key)
        check(uid not in seen,
              f"unique_id collision: keys {seen.get(uid)!r} and {key!r} both "
              f"produce {uid!r}")
        seen[uid] = key
    # The mower's own id must not be reachable from any keyed entity.
    check(m.mower_unique_id("DEV") not in seen,
          "a keyed entity produces the same unique_id as the mower entity")


def test_the_assertions_can_fail():
    """Prove the check CAN go red, or a green result means nothing."""
    before = len(FAILURES)
    original = m.mower_unique_id
    try:
        m.mower_unique_id = lambda device_id: f"navimow_mower_{device_id}"
        test_mower_unique_id_matches_upstream()
        if len(FAILURES) == before:
            FAILURES.append(
                "SELF-TEST FAILED: a deliberately wrong mower_unique_id "
                "produced no failure, so this suite cannot detect the id "
                "regression it exists to detect"
            )
            return
        del FAILURES[before:]
    finally:
        m.mower_unique_id = original
    if m.mower_unique_id("x") != "navimow_x":
        FAILURES.append("SELF-TEST FAILED: could not restore mower_unique_id")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} finding(s) across {len(tests)} tests")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"PASS: {len(tests)} tests, inherited unique_ids intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
