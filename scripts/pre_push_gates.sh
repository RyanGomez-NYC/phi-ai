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

# ---------------------------------------------------------------------------
# UNEXPORT GIT'S OWN CONTEXT BEFORE RUNNING ANYTHING.
#
# Git sets GIT_DIR (and friends) for its hooks, and those variables
# OVERRIDE `git -C`. -C is a chdir, not a scope. So every git call made by
# anything this script runs - the test suite, the fixture check, a build -
# acted on THE REPOSITORY BEING PUSHED rather than on the directory it was
# handed.
#
# That is not hypothetical. On 2026-09-09 this gate ran the suite, a test
# fixture's `git commit` landed on the branch being pushed and moved its
# HEAD to a three-file tree, and the same fixture's `git init` re-initialised
# the main repository and set core.bare=true - after which every `git status`
# in the checkout answered "fatal: this operation must be run in a work
# tree". The push was refused on unrelated grounds; a green run would have
# published a corrupted tree.
#
# core/components/build.git_env() scrubs these per call, and every git
# subprocess in core/ and tests/ now goes through it. THIS IS THE BELT to
# that pair of braces: it removes the variables once, here, so a git call
# added later - in a test, a script, a tool this gate grows - cannot
# reintroduce the bug by forgetting to scrub. The gate's own `git
# rev-parse` above runs BEFORE this, deliberately, because that one does
# want the hook's repository.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
      GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE GIT_PREFIX

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
