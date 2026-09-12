#!/usr/bin/env python3
"""Structural gates over custom_components/navimow, by AST and by text.

WHY NOT BY IMPORT. Every module here except model.py imports `homeassistant`,
which tools-tests.yml deliberately does not install (that workflow installs
only what the component manifests declare). So this suite parses the files
rather than running them. It therefore proves nothing about RUNTIME behaviour
-- an entity that registers correctly and then returns the wrong value passes
every check below. What it does catch is the class of defect that made
NavimowHA worth replacing: a channel subscribed but never listened to, a
translation key with nothing behind it, a credential reaching a log line.
"""
import ast
import io
import json
import os
import re
import sys
import tokenize

# tests/ sits directly under the component root in both layouts: this repo
# standing alone, and this repo installed as custom_components/navimow.
# One expression covers both.
COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# filename stem -> the strings.json entity section it must resolve against
PLATFORM_FILES = {
    "binary_sensor": "binary_sensor",
    "button": "button",
    "device_tracker": "device_tracker",
    "event": "event",
    "lawn_mower": "lawn_mower",
    "sensor": "sensor",
}

FAILURES = []
READ = []


def check(condition, message):
    if not condition:
        FAILURES.append(message)


def source(name):
    path = os.path.join(COMPONENT, name)
    with open(path, encoding="utf-8") as handle:
        body = handle.read()
    READ.append(name)
    return body


def code_only(body):
    """Strip comments and string literals, leaving executable text.

    ASSERT ON CODE FORMS, NOT ON IDENTIFIERS, AND STRIP COMMENTS FIRST. A file
    that documents its own history matches every check that says the history
    is gone: this component's diagnostics.py explains in a comment why it does
    NOT reach into the SDK internal, and a plain substring gate read that
    explanation as the reach itself and failed a correct file. A negative gate
    over raw text can only ever be right about a file that never discusses
    what it refuses to do.
    """
    out = []
    previous_end = (1, 0)
    for token in tokenize.generate_tokens(io.StringIO(body).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        if token.start[0] != previous_end[0]:
            out.append("\n")
        elif token.start[1] > previous_end[1]:
            out.append(" ")
        out.append(token.string)
        previous_end = token.end
    return "".join(out)


def _png_size(path):
    """Width/height straight out of the IHDR chunk. No Pillow: CI installs only
    what the manifest declares, and a suite that needs an imaging library to
    check an image is a suite that stops running."""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None, None
    return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


def _png_is_transparent(path):
    """Does this PNG actually render with transparency?

    THREE COLOUR TYPES ANSWER YES, AND CHECKING ONLY ONE FAILS CORRECT FILES.
    Types 6 (RGBA) and 4 (grey+alpha) carry a per-pixel alpha channel. Type 3
    (palette) carries none, and is still transparent when a tRNS chunk marks
    palette entries see-through -- which is what every palette-quantised PNG
    does, and quantising is how these assets got from 850KB to 158KB. An
    earlier cut of this gate tested the colour type alone and failed seven
    correct images.

    A palette PNG with NO tRNS is genuinely opaque and renders as the solid
    slab this is here to refuse, so the chunk is walked rather than assumed.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    colour_type = data[25]
    if colour_type in (4, 6):
        return True
    if colour_type != 3:
        return False
    # Walk the chunk list properly: a bare `b"tRNS" in data` would also match
    # the bytes falling inside compressed image data by coincidence.
    offset = 8
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        if kind == b"tRNS":
            return True
        if kind == b"IDAT":
            break  # tRNS is required to precede IDAT
        offset += 12 + length
    return False


def test_every_declared_platform_has_a_module_that_sets_up():
    """const.PLATFORMS is parsed, not imported -- it pulls in homeassistant."""
    const_src = source("const.py")
    declared = re.findall(r"Platform\.([A-Z_]+)", const_src)
    check(declared, "no Platform.* entries parsed out of const.py -- the parse is wrong")
    for name in declared:
        stem = name.lower()
        path = os.path.join(COMPONENT, f"{stem}.py")
        if not os.path.exists(path):
            FAILURES.append(f"const.PLATFORMS declares {name} but {stem}.py does not exist")
            continue
        tree = ast.parse(source(f"{stem}.py"))
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        }
        check("async_setup_entry" in functions,
              f"{stem}.py has no async_setup_entry, so PLATFORMS lists a platform "
              "that will forward to nothing")


def test_the_event_channel_is_actually_subscribed():
    """THE HEADLINE REGRESSION GATE.

    mower_sdk subscribes /downlink/vehicle/<id>/realtimeDate/event and parses
    every message into a DeviceEventMessage. NavimowHA registered on_state and
    on_attributes and never on_event, so all of it was decoded and dropped
    inside the SDK. If a future edit drops this callback the events go quiet
    again with no error anywhere -- exactly how the gap survived upstream.
    """
    coordinator = source("coordinator.py")
    for callback in ("on_state", "on_attributes", "on_event"):
        check(f"self.sdk.{callback}(" in coordinator,
              f"coordinator.async_setup does not register sdk.{callback}() -- "
              "that channel's messages are parsed by the SDK and then dropped")
    check("_handle_event" in coordinator,
          "no _handle_event handler in coordinator.py")
    check("async_add_event_listener" in coordinator,
          "no listener fan-out, so event.py has nothing to subscribe to")
    check("async_add_event_listener" in source("event.py"),
          "event.py does not subscribe to the coordinator's event fan-out")


def test_every_coordinator_accessor_has_a_consumer():
    """A reading cached and read by NOTHING is the defect this catches.

    The worked example: the coordinator subscribed the MQTT `attributes`
    channel, cached every payload, and exposed get_device_attributes() -- and no entity and no
    diagnostic ever called it. The data arrived, was stored, and was invisible,
    which is worse than not collecting it: the integration looked like it
    covered the channel. It is also the ONE channel that could still be
    carrying a mowing schedule or a blade/service figure, so the blind spot
    correlated exactly with the open question.

    Nothing errors in that state and no test of behaviour would have caught
    it, because the behaviour was correct -- so the gate is structural: every
    public accessor coordinator.py defines must be referenced somewhere else
    in the component.
    """
    coordinator = source("coordinator.py")
    accessors = set(re.findall(r"^    def (get_\w+)\(", coordinator, re.M))
    check(accessors, "no get_* accessors parsed out of coordinator.py -- the "
                     "parse is wrong, not the component")

    consumers = {}
    for name in sorted(os.listdir(COMPONENT)):
        if not name.endswith(".py") or name == "coordinator.py":
            continue
        body = source(name)
        for accessor in accessors:
            if f".{accessor}(" in body:
                consumers.setdefault(accessor, []).append(name)

    for accessor in sorted(accessors):
        check(accessor in consumers,
              f"coordinator.{accessor}() is defined and cached but nothing "
              "outside coordinator.py reads it -- either surface it or stop "
              "collecting it; a channel stored and never read reads as covered")


def test_the_mqtt_session_is_reported_by_diagnostics():
    """A CHANNEL WHOSE OWN STATE IS UNOBSERVABLE CANNOT BE DIAGNOSED.

    The failure this gate exists to keep fixed: a dump showing no attributes
    and no events is produced BOTH by a dead broker session and by a mower
    docked and asleep with nothing to publish. The symptoms are identical and
    the fixes are opposite, so without the session's own state the dump sends
    the reader to guess -- and the guess that was actually made, off this very
    file's output, was that `mqtt_username: null` in the entry meant MQTT had
    no credentials. It does not: those keys are NavimowHA's, this component
    reads neither, and it re-resolves both from mqtt/userInfo/get/v2 on every
    setup.

    It must come off the PUBLIC property. NavimowSDK.is_connected forwards to
    paho's own is_connected(); reaching `sdk._mqtt` here would tie the dump to
    an SDK internal for a reading the facade already answers.
    """
    diagnostics = source("diagnostics.py")
    check('"mqtt"' in diagnostics,
          "diagnostics.py publishes no mqtt block, so a dump cannot tell a "
          "dead broker session from a mower with nothing to say")
    check("is_connected" in diagnostics,
          "the mqtt block does not report sdk.is_connected -- broker/port "
          "alone say where we would have connected, never whether we did")
    # Comments stripped first: this file explains in prose why it does not do
    # the thing, and a raw-text gate read the explanation as the thing.
    check("sdk._mqtt" not in code_only(diagnostics),
          "diagnostics.py reaches into sdk._mqtt; NavimowSDK.is_connected is "
          "public and answers this without pinning an SDK internal")
    check("seconds_since_mqtt_push" in diagnostics,
          "no per-device time-since-push, so a connected session that has "
          "gone quiet reads exactly like one that never received anything")
    check("entry_keys_unused" in diagnostics,
          "the inherited NavimowHA entry keys are dumped with nothing saying "
          "they are dead; mqtt_username: null has already been read as proof "
          "that MQTT was unauthenticated")

    # The reported endpoint must be the one setup RESOLVED, not the entry's
    # inherited copy -- that is the whole point of carrying it on runtime data.
    init = source("__init__.py")
    for field_name in ("mqtt_broker", "mqtt_port", "mqtt_transport"):
        check(f"{field_name}:" in init or f"{field_name}=" in init,
              f"NavimowRuntimeData does not carry {field_name}, so diagnostics "
              "can only report the entry's inherited value")


def test_the_brand_assets_exist_and_meet_core_s_sizes():
    """Core 2026.3+ serves a custom integration's OWN brand/ directory.

    The sizes are core's, not ours, and a file outside them is not rejected
    loudly -- it is served and renders wrong, which is the kind of defect that
    ships because nothing errors. icon must be square 256/512; a logo's SHORT
    side must be 128-256 normal and 256-512 hDPI.

    Dark variants are not optional here on judgement rather than on rule: the
    wordmark is black, so without them the logo is invisible against every dark
    theme, which is most of them.
    """
    brand = os.path.join(COMPONENT, "brand")
    check(os.path.isdir(brand), "no brand/ directory -- core has nothing to serve "
                                "and the UI falls back to a generic icon")
    if not os.path.isdir(brand):
        return

    # (filename, is_square, short-side low, short-side high)
    EXPECTED = [
        ("icon.png", True, 256, 256), ("icon@2x.png", True, 512, 512),
        ("dark_icon.png", True, 256, 256), ("dark_icon@2x.png", True, 512, 512),
        ("logo.png", False, 128, 256), ("logo@2x.png", False, 256, 512),
        ("dark_logo.png", False, 128, 256), ("dark_logo@2x.png", False, 256, 512),
    ]
    for name, square, low, high in EXPECTED:
        path = os.path.join(brand, name)
        if not os.path.exists(path):
            FAILURES.append(f"brand/{name} is missing")
            continue
        w, h = _png_size(path)
        READ.append(f"brand/{name}")
        if w is None:
            FAILURES.append(f"brand/{name} is not a readable PNG")
            continue
        if square:
            check(w == h == low,
                  f"brand/{name} is {w}x{h}; core wants a square {low}x{low}")
        else:
            check(low <= min(w, h) <= high,
                  f"brand/{name} short side is {min(w, h)}; core wants "
                  f"{low}-{high}")
        # A brand image with no transparency renders as a white slab on a dark
        # theme even when the artwork itself is correct.
        check(_png_is_transparent(path),
              f"brand/{name} renders opaque -- no alpha channel and no tRNS -- "
              "so it will show as a solid block against a dark theme")

    # The tile picture must point at what core actually serves, or it 404s and
    # the tile renders an empty frame rather than falling back to an icon.
    const_src = source("const.py")
    check('"/api/brands/integration/' in const_src
          or "'/api/brands/integration/" in const_src,
          "const.py does not build the core-served brand url; a tile picture "
          "pointing anywhere else needs a static path this component does not "
          "register")
    check("BRAND_ICON_URL" in source("lawn_mower.py"),
          "the mower entity does not carry the brand picture")
    for stem in ("sensor", "binary_sensor", "button", "device_tracker", "event"):
        check("entity_picture" not in source(f"{stem}.py"),
              f"{stem}.py sets an entity_picture -- a picture outranks the "
              "icon, so this costs every one of those entities the meaning "
              "its icon carries. The brand mark belongs on the mower alone.")


def test_every_translation_key_resolves():
    """An audit is a join. A translation_key with no strings.json
    entry renders as a raw slug on the wall and nothing errors."""
    strings = json.loads(source("strings.json"))
    entity_sections = strings.get("entity", {})
    for stem, section in PLATFORM_FILES.items():
        path = os.path.join(COMPONENT, f"{stem}.py")
        if not os.path.exists(path):
            continue
        body = source(f"{stem}.py")
        keys = set(re.findall(r'translation_key\s*=\s*"([^"]+)"', body))
        # Keys given as a const reference (KEY_FOO) resolve through const.py.
        const_src = source("const.py")
        for const_name in re.findall(r"translation_key\s*=\s*(KEY_\w+)", body):
            match = re.search(rf'^{const_name}:\s*Final\s*=\s*"([^"]+)"',
                              const_src, re.M)
            if match:
                keys.add(match.group(1))
            else:
                FAILURES.append(f"{stem}.py uses {const_name} which const.py does not define")
        declared = set(entity_sections.get(section, {}))
        for key in sorted(keys):
            check(key in declared,
                  f"{stem}.py uses translation_key {key!r} but strings.json has "
                  f"no entity.{section}.{key} -- it will render as a slug")


def test_no_credential_reaches_a_log_line():
    """NavimowHA logged the MQTT password at INFO as `first2***last2` --
    four real characters of a live secret, on every setup.
    Partial masking is not redaction, so the gate is on the NAME appearing
    anywhere in a logging call, not on whether it looked masked."""
    secrets = ("pwdInfo", "userName", "password", "client_secret",
               "access_token", "refresh_token", "_mask_secret")
    for name in sorted(os.listdir(COMPONENT)):
        if not name.endswith(".py"):
            continue
        body = source(name)
        for match in re.finditer(
            r"_LOGGER\.(debug|info|warning|error|exception|critical)\((.*?)\)\n",
            body, re.S,
        ):
            call = match.group(2)
            for secret in secrets:
                check(secret not in call,
                      f"{name}: a _LOGGER.{match.group(1)} call references "
                      f"{secret!r}. Masking is not redaction -- keep it out of "
                      "the log entirely.")


def test_the_always_failing_service_is_gone():
    """NavimowHA registered set_blade_height and its handler raised
    unconditionally, in Chinese. A service that can only fail is worse than an
    absent one: it appears in the UI and in every automation picker."""
    check(not os.path.exists(os.path.join(COMPONENT, "services.yaml")),
          "services.yaml is back; if a real service was added, this gate needs updating")
    for name in sorted(os.listdir(COMPONENT)):
        if name.endswith(".py"):
            check("set_blade_height" not in source(name),
                  f"{name} still references set_blade_height")


def test_no_cjk_in_user_facing_text():
    """NavimowHA's strings.json, its service error and most of its comments are
    Chinese. Nothing here should reach an English-language wall untranslated."""
    cjk = re.compile(r"[一-鿿]")
    for root, dirs, files in os.walk(COMPONENT):
        # PRUNED, and not as tidiness: this suite lives INSIDE the tree it
        # sweeps now that the component owns its own repo, and it has to carry
        # the CJK pattern above to test for CJK. Sweeping itself makes the check
        # fail on every correct component, forever. Only what SHIPS is in scope
        # -- tests/ and .github/ are not installed into HA, and __pycache__ is
        # build output. The dirs list is mutated in place, which is what prunes
        # the walk rather than merely skipping the files.
        dirs[:] = [
            d for d in dirs
            if d not in {"tests", "tools", ".git", ".github", "__pycache__"}
        ]
        for name in sorted(files):
            if not name.endswith((".py", ".json", ".yaml")):
                continue
            rel = os.path.relpath(os.path.join(root, name), COMPONENT)
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                body = handle.read()
            READ.append(rel)
            check(not cjk.search(body),
                  f"{rel} contains CJK characters; this component ships English only")


def test_manifest_claims_only_what_is_checkable():
    manifest = json.loads(source("manifest.json"))
    check("quality_scale" not in manifest,
          "manifest.json declares quality_scale. hassfest's validate_iqs_file "
          "returns immediately for a non-core integration, so nothing would "
          "ever test the claim. quality_scale.yaml carries the honest "
          "self-assessment instead.")
    check(manifest.get("iot_class") == "cloud_push",
          f"iot_class is {manifest.get('iot_class')!r}; state arrives over MQTT "
          "push, and NavimowHA's 'cloud_polling' was already wrong")
    check("application_credentials" in manifest.get("dependencies", []),
          "__init__.async_setup calls async_import_client_credential, which "
          "raises ValueError unless application_credentials is a dependency")
    check(manifest.get("single_config_entry") is True,
          "config_flow aborts a second entry; the manifest must say so too")


def test_strings_and_english_translation_agree():
    check(source("strings.json") == source(os.path.join("translations", "en.json")),
          "translations/en.json has drifted from strings.json")


def test_the_assertions_can_fail():
    """Every assertion set needs a self-test proving it CAN fail.

    Each gate below is fed a string it MUST trip. Without this, a regex that
    stopped matching -- the credential one is a multiline pattern over a call
    body, which is exactly the kind that silently stops matching after an
    unrelated reformat -- would report green over files it no longer inspects.
    """
    log_call = re.compile(
        r"_LOGGER\.(debug|info|warning|error|exception|critical)\((.*?)\)\n", re.S
    )
    leaky = '_LOGGER.info("MQTT auth: %s", _mask_secret(pwdInfo))\n'
    if not any("pwdInfo" in match.group(2) for match in log_call.finditer(leaky)):
        FAILURES.append(
            "SELF-TEST FAILED: the credential-in-log pattern did not match a "
            "line that logs pwdInfo, so that gate cannot detect the leak it "
            "exists to detect"
        )
    if re.compile(r"[\u4e00-\u9fff]").search("plain ascii") is not None:
        FAILURES.append("SELF-TEST FAILED: the CJK pattern matches ASCII")
    if not re.compile(r"[\u4e00-\u9fff]").search("当前 REST API"):
        FAILURES.append("SELF-TEST FAILED: the CJK pattern does not match Chinese")
    if "self.sdk.on_event(" in "self.sdk.on_state(self._handle_state)":
        FAILURES.append("SELF-TEST FAILED: the on_event check also matches on_state")
    # The private-reach gate is a NEGATIVE check, which is the kind that goes
    # vacuous without ever saying so: if `sdk._mqtt` stopped being the spelling
    # of the thing being refused, the check would pass over a file that reached
    # straight into the SDK. Prove it still trips on the string it forbids.
    if "sdk._mqtt" not in code_only("runtime.sdk._mqtt.is_connected\n"):
        FAILURES.append(
            "SELF-TEST FAILED: the private-reach gate does not match a line "
            "that reaches into sdk._mqtt, so it cannot refuse what it exists "
            "to refuse"
        )
    # And the stripper must actually strip, or the gate above passes for the
    # wrong reason -- a code_only() that returned "" would satisfy every
    # negative check in this suite forever.
    if "sdk._mqtt" in code_only('# a comment naming sdk._mqtt\nx = 1\n'):
        FAILURES.append(
            "SELF-TEST FAILED: code_only() leaves comment text in place, so "
            "the private-reach gate still fails a file that merely documents "
            "the reach it refuses"
        )
    if "x = 1" not in code_only("# comment\nx = 1\n"):
        FAILURES.append(
            "SELF-TEST FAILED: code_only() dropped executable code; a gate "
            "over its output would pass over a file it can no longer see"
        )
    if "is_connected" not in "runtime.sdk.is_connected":
        FAILURES.append(
            "SELF-TEST FAILED: the mqtt-session gate cannot see is_connected"
        )


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    # A sweep that does not assert its own completeness is not evidence.
    # Count successful reads, never attempts.
    if not READ:
        print("CANNOT RUN: no component file was read; the paths are wrong")
        return 2
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} finding(s) across {len(tests)} tests")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"PASS: {len(tests)} tests over {len(set(READ))} distinct files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
