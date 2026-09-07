# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""Data orchestration: wire an exchange, bound its scope, decide every
delivery, and carry the decisions onto the audit trail.

Pure decisions, no I/O. What an operator SET lives in
core/web/platform_state.py (with SQL write-through), the screens live in
core/web/orchestration_routes.py, and the movement itself rides
core/fhir. This package is the part that has to be the same answer
wherever it is asked: the preflight, the run and the ledger all consult
`decide.decide_delivery` and nothing else, so they cannot disagree.
"""
from core.orchestration.consents import Consent, ConsentStore, consent_key
from core.orchestration.decide import (PURPOSES_ALLOWING_SENSITIVE, Decision,
                                       decide_delivery)
from core.orchestration.exchange import (CADENCES, DELIVERY_MODES, NAME_MAX,
                                         SET_MAX, Exchange, Selection,
                                         normalise_selection, selection_chosen)
from core.orchestration.qa import Finding, audit_run
from core.orchestration.run import (MoveResult, Mover, Run, Step, decided_not_written,
                                    execute)
from core.orchestration.heightened import (CATEGORY_ALIASES, CATEGORY_LABELS,
                                           categories_for_chart, category_label,
                                           normalise_category, withheld_prose)
from core.orchestration.scope import (AGE_BANDS, MODES, POPULATION_PURPOSES,
                                      Scope, mode_for_purpose, normalise_scope,
                                      purpose_allows_population, scope_has_selectors,
                                      scope_label)
from core.orchestration.systems import System, system_keys, systems
from core.orchestration.links import FHIR_ID, LINK_STATES, Link, LinkRefusal, LinkStore
from core.orchestration.crosswalk import Crosswalk, CrosswalkStore

__all__ = [
    "AGE_BANDS", "CADENCES", "CATEGORY_ALIASES", "CATEGORY_LABELS", "DELIVERY_MODES", "MODES",
    "NAME_MAX", "POPULATION_PURPOSES", "PURPOSES_ALLOWING_SENSITIVE", "SET_MAX",
    "Consent", "ConsentStore", "Decision", "Exchange", "Finding", "MoveResult", "Mover",
    "Run", "Scope", "Selection", "Step", "System", "audit_run", "decided_not_written", "execute",
    "categories_for_chart", "category_label", "consent_key", "decide_delivery",
    "mode_for_purpose", "normalise_category", "normalise_scope", "normalise_selection",
    "purpose_allows_population", "scope_has_selectors", "scope_label",
    "selection_chosen", "system_keys", "systems", "withheld_prose",
]
