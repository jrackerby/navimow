#!/usr/bin/env bash
# Run every suite under tests/. Plain scripts, not pytest: each one prints its
# own PASS/FAIL line and exits non-zero on a finding.
#
# A RUNNER THAT FINDS NOTHING MUST FAIL, NOT PASS. A loop over a glob that
# matches nothing exits 0 having run nothing, which is the exact vacuous-green
# these suites are written to refuse, so the count is asserted before anything
# is judged by it.
set -uo pipefail

cd "$(dirname "$0")/.."

shopt -s nullglob
SUITES=(tests/test_*.py)
shopt -u nullglob

if [[ ${#SUITES[@]} -eq 0 ]]; then
  echo "CANNOT RUN: no suites matched tests/test_*.py -- this would have exited 0"
  exit 2
fi

echo "running ${#SUITES[@]} suite(s)"
FAILED=0
for suite in "${SUITES[@]}"; do
  echo "=== $suite"
  if ! python3 "$suite"; then
    FAILED=$((FAILED + 1))
  fi
done

if [[ $FAILED -gt 0 ]]; then
  echo "FAIL: $FAILED of ${#SUITES[@]} suite(s) reported findings"
  exit 1
fi
echo "PASS: ${#SUITES[@]} suite(s)"
