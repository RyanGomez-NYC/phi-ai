#!/bin/sh
# Local pre-push gates - the project runs NO GitHub Actions (docs/SPEC.md
# §7.1 R5: hosted CI is a project constraint, and metered runner minutes
# are a bill nobody here wants). These are the same gates the retired
# workflow ran, executed on the developer's machine before every push
# instead of on GitHub's runners after it.
#
# Install (once per clone):
#
#   ln -s ../../scripts/pre_push_gates.sh .git/hooks/pre-push
#
# Run by hand any time:
#
#   scripts/pre_push_gates.sh
#
# The Terraform validate and docker build gates stay in
# docs/RELEASE_CHECKLIST.md §3's manual list, exactly as they did when
# the workflow existed - they need tooling a quick pre-push shouldn't
# assume.

set -e

# Resolve the repo root so the hook works no matter where git invokes
# it from. As a hook, $0 is .git/hooks/pre-push (a symlink dirname
# cannot see through), so ask git itself; the dirname fallback covers
# running the script by hand from outside a work tree.
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$ROOT" ]; then
    ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
cd "$ROOT"

if [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
else
    PYTHON="python3"
fi

echo "pre-push gate 1/3: byte-compile every module"
"$PYTHON" -m compileall -q core emulators scripts install tests

echo "pre-push gate 2/3: synthetic-fixture gates (SPEC §7.1 R2/R4)"
"$PYTHON" scripts/check_fixtures.py

echo "pre-push gate 3/3: full test suite"
# tests/test_e2e_matrix.py binds every emulator on its DEFAULT_PORTS port
# (9101-9115 today) and fails the session if one is held - by a
# long-running `python -m emulators`, say. The pinned proof runs on those
# ports; on a machine where they are legitimately busy, export
# E2E_MATRIX_PORT_OFFSET=N before running this gate as the documented
# fallback (the proof document then states the shifted ports).
if [ -n "${E2E_MATRIX_PORT_OFFSET:-}" ]; then
  echo "  (E2E_MATRIX_PORT_OFFSET=${E2E_MATRIX_PORT_OFFSET}: the e2e matrix runs on DEFAULT_PORTS + ${E2E_MATRIX_PORT_OFFSET})"
fi
"$PYTHON" -m pytest tests/ -q

echo "pre-push gates passed"
# Made by Ryan Gomez & Co. Inc.
