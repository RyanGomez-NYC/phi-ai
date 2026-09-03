# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
End to end, one to one to many to many: every vendor as a source into
every vendor as a target, each pair its own test.

The helpers - emulator start-up on the DEFAULT_PORTS ports, the two
signing key pairs, authenticate/ingest/deliver, the matrix report - live
in scripts/e2e_matrix.py and are imported here, so the CLI that writes
the proof document and this suite run exactly the same code. Nothing is
duplicated in either direction.

  one to one     any single test below, e.g. test_pair[epic->cerner]
  one to many    one source's row: test_pair[epic->*]
  many to one    one target's column: test_pair[*->cerner]
  many to many   the whole product, plus the final test that assembles
                 and prints the matrix and insists every cell is there

ALL DATA IS SYNTHETIC (the eSyn* fixtures every emulator serves; the
ingest helper asserts the prefix on every resource it pages). The
emulators bind 127.0.0.1 on their DEFAULT_PORTS ports - the ports the
proof document names - so a port already in use fails the session
rather than moving elsewhere.

No xfail, no skip: a vendor without $export PASSES by returning the
OperationOutcome refusal, a target that advertises no create PASSES by
the writer's structured refusal, and the diagonal PASSES by the writer
refusing to write back into a source system. Every assertion names the
source or the pair.
"""

import itertools
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import e2e_matrix as e2e  # noqa: E402  (scripts/e2e_matrix.py - the shared helpers)
from emulators.vendors import VENDORS  # noqa: E402

VENDOR_KEYS = sorted(VENDORS)
PAIRS = list(itertools.product(VENDOR_KEYS, VENDOR_KEYS))
PAIR_IDS = [f"{source}->{target}" for source, target in PAIRS]


@pytest.fixture(scope="session")
def signing_keys():
    """One RSA-2048 pair and one EC P-384 pair for the whole session."""
    return e2e.generate_signing_keys()


@pytest.fixture(scope="session")
def emulators(signing_keys):
    """Every emulator in emulators.vendors.VENDORS, in-process, on its
    DEFAULT_PORTS port, with this session's public JWK Set registered so
    each token endpoint verifies assertion signatures.

    reuse_running=False: the ports must be free, and a port in use fails
    the session loudly. E2E_MATRIX_PORT_OFFSET=N (default 0, never
    chosen silently) shifts every port for a machine where the
    DEFAULT_PORTS ports are held by emulators that belong to something
    else, such as a long-running `python -m emulators`."""
    offset = int(os.environ.get("E2E_MATRIX_PORT_OFFSET", "0"))
    handles = e2e.start_emulators(
        VENDOR_KEYS, reuse_running=False, client_jwks=signing_keys.jwks, port_offset=offset,
    )
    yield handles
    e2e.stop_emulators(handles)


@pytest.fixture(scope="session")
def session(emulators, signing_keys):
    """The caches: one authentication and one ingest per vendor, one
    delivery per pair. Session-scoped so the 15x15 product shares them."""
    return e2e.MatrixSession(emulators, signing_keys)


# ---------------------------------------------------------------------------
# Every pair
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("source", "target"), PAIRS, ids=PAIR_IDS)
def test_pair(session, source, target):
    """Pull from `source` with the real client (cached: once per source),
    push into `target` with the real writer, assert every outcome.

    Asserted on the way: the source authenticated by its profile's grant
    with an assertion signed in the profile's algorithm by a key of the
    right family and accepted by the emulator; /metadata is a
    CapabilityStatement; Patient plus at least two more types were paged
    with more than one page observed; $export produced NDJSON where the
    emulator has it and an OperationOutcome where it does not; and per
    delivered type, created-and-confirmed where the target advertises
    create, the writer's own refusal where it does not - or, on the
    diagonal, the writer's refusal to write into a source system."""
    cell = session.run_pair(source, target)

    assert set(cell.outcomes) == set(e2e.DELIVERY_TYPES), (
        f"{source}->{target}: outcomes recorded for {sorted(cell.outcomes)}, "
        f"not {list(e2e.DELIVERY_TYPES)}"
    )
    if source == target:
        assert all(o == e2e.REFUSED_SOURCE_SYSTEM for o in cell.outcomes.values()), (
            f"{source}->{target}: the diagonal must be the writer's source-system refusal, got {cell.outcomes}"
        )
    else:
        for rtype, outcome in cell.outcomes.items():
            expected = e2e.CREATED if rtype in cell.creatable else e2e.REFUSED_NOT_ADVERTISED
            assert outcome == expected, (
                f"{source}->{target}: {rtype}: expected {expected!r} given the target advertises "
                f"create for {sorted(cell.creatable)}, got {outcome!r}"
            )


# ---------------------------------------------------------------------------
# The matrix, assembled and printed
# ---------------------------------------------------------------------------

def test_the_full_matrix_is_assembled_and_printed(session):
    """Runs last (file order). Builds the same report the CLI writes to
    the proof document, prints it, and insists every one of the pairs
    above produced a cell and none recorded a failure - so a pair that
    failed above fails here too, by name."""
    from datetime import datetime, timezone

    report = session.report(VENDOR_KEYS)
    text = e2e.render_markdown(
        report,
        commit=e2e.git_commit(),
        dataset=e2e.dataset_description(),
        started_at=datetime.now(timezone.utc),
        duration_seconds=0.0,
        command="python -m pytest tests/test_e2e_matrix.py",
    )
    print()
    print(text)

    assert not report.failures, "pairs that failed: " + "; ".join(
        f"{name}: {reason}" for name, reason in report.failures.items()
    )
    missing = sorted(f"{s}->{t}" for s, t in PAIRS if (s, t) not in report.cells)
    assert not missing, f"pairs with no cell (never ran?): {missing}"
    assert len(report.cells) == len(VENDOR_KEYS) ** 2
    assert report.ok
# Made by Ryan Gomez & Co. Inc.
