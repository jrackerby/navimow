#!/usr/bin/env python3
"""Every credential-bearing log line in the INSTALLED navimow-sdk is filtered.

tests/test_wiring.py proves the component's filter against format strings
pinned from navimow-sdk 0.1.2. That is a fixture, and a fixture goes stale the
release the SDK moves or adds a line. This runs where the SDK is actually
installed (the CI imports job, or a venv) and derives the set from its source:
every `_LOGGER.<level>(` call whose arguments name `_mask_secret`,
`_format_auth_headers`, `username` or `password` must (a) live in a module
whose logger is in SDK_LOGGERS_FILTERED and (b) carry one of
SDK_LOG_FORMAT_MARKERS in its format string, or the filter cannot see it.

Exit 0 only when at least one such line was found and every one is covered.
Finding none is CANNOT RUN, not a pass: 0.1.2 has three.
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENT = os.path.dirname(HERE)
LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}
CREDENTIAL_ARGS = ("_mask_secret", "_format_auth_headers", "username", "password")


def component_constants() -> tuple[tuple[str, ...], tuple[str, ...]]:
    with open(os.path.join(COMPONENT, "__init__.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id in (
                "SDK_LOGGERS_FILTERED", "SDK_LOG_FORMAT_MARKERS"
            ):
                found[target.id] = tuple(ast.literal_eval(node.value))
    missing = {"SDK_LOGGERS_FILTERED", "SDK_LOG_FORMAT_MARKERS"} - set(found)
    if missing:
        sys.exit(f"CANNOT RUN: __init__.py no longer defines {sorted(missing)}")
    return found["SDK_LOGGERS_FILTERED"], found["SDK_LOG_FORMAT_MARKERS"]


def main() -> int:
    try:
        import mower_sdk
    except ImportError:
        print("CANNOT RUN: mower_sdk (navimow-sdk) is not importable here")
        return 2
    loggers, markers = component_constants()
    package_dir = os.path.dirname(mower_sdk.__file__)
    findings: list[str] = []
    seen = 0
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith(".py"):
            continue
        module = f"mower_sdk.{name[:-3]}" if name != "__init__.py" else "mower_sdk"
        with open(os.path.join(package_dir, name), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in LEVELS and node.args):
                continue
            args_text = ast.unparse(node.args[1:])
            if not any(marker in args_text for marker in CREDENTIAL_ARGS):
                continue
            seen += 1
            fmt = node.args[0]
            fmt_text = fmt.value if isinstance(fmt, ast.Constant) else ast.unparse(fmt)
            where = f"{module}:{node.lineno}"
            if module not in loggers:
                findings.append(f"{where} logs a credential field and its logger "
                                f"{module!r} is not in SDK_LOGGERS_FILTERED")
            if not any(marker in str(fmt_text) for marker in markers):
                findings.append(f"{where} logs a credential field with a format "
                                f"string carrying none of {markers}: {fmt_text!r}")
    if seen == 0:
        print("CANNOT RUN: no credential-bearing log call found in the installed "
              "SDK -- 0.1.2 has three, so the scan, not the SDK, is suspect")
        return 2
    if findings:
        print(f"FAIL: {len(findings)} uncovered SDK log line(s)")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print(f"PASS: {seen} credential-bearing SDK log line(s), all filtered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
