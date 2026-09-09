# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The capability screens: the §5 workflows, running on the real engines.

WHY THIS FILE EXISTS. core/capabilities/ holds seven engines - prior auth,
trial screening, chart abstraction, data quality, patient instructions,
triage, summarization - all written, all tested, and six of them wired to
NOTHING. Their only callers were two test modules. Meanwhile the same
seven workflows had working screens in the demonstration, and the platform
offered static worked-example pages under /product/<key> in their place.
So the product had the logic and no screens, the demo had the screens, and
the two were described as the same system.

These routes close that. Each one runs the real engine over the real
chart, through the same reader every other screen reads with, and none of
them reimplements a decision the engine already makes.

THREE RULES, EACH ONE ALREADY THIS PROJECT'S:

1. EVERY SCREEN HONOURS THE PATIENT IN CONTEXT, or says it has no patient
   dimension. See core/web/patient_context.py. A capability screen with
   no patient in context asks for one; it never guesses, and it never
   quietly reports on the whole store.

2. THE ROLE DICTATES VISIBILITY, AND THE ROUTE ENFORCES IT. Every screen
   goes through _require_screen against core/web/nav.py, so the sidebar
   and the routes cannot disagree - the same gate core/web/product_routes.py
   applies, deliberately reused rather than reimplemented.

3. READING A CHART IS A DISCLOSURE AND IS AUDITED BEFORE THE READ. These
   screens read clinical content to do their work, so each one writes its
   audit entry first, with the purpose the person asserted, exactly as
   the record screens do.

ORDER OF REGISTRATION MATTERS. These paths are bespoke `/product/<key>`
routes and must be registered BEFORE product_routes' generic
`/product/{key}`, because Starlette matches in registration order and the
catch-all would otherwise swallow them.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.web import nav as product_nav
from core.web.data import utcnow
from core.web.auth import (
    Identity,
    NotAuthorized,
    purpose_allowed,
    role_default_purpose,
    validate_purpose,
)
from core.web.patient_context import (
    NO_PATIENT_DIMENSION as _NO_PATIENT_DIMENSION,
    patient_in_context,
)

log = logging.getLogger("phi-ai.web.capabilities")

#: Where a generated instructions draft waits for a signature. Session
#: state, like the signature queue's own signed-set: a draft is one
#: person's work in progress and nothing has been committed anywhere yet.
INSTRUCTIONS_DRAFT_KEY = "instructions_draft"


def register(app, page, require, current_identity, record, reader) -> None:
    """Attach the capability screens. Register BEFORE product_routes."""

    def _flags() -> dict:
        return {
            "assistant_enabled": getattr(app.state, "assistant", None) is not None,
            "local_accounts": getattr(app.state, "local_accounts", None) is not None,
            "imaging_enabled": getattr(app.state, "imaging_connection_factory", None) is not None,
        }

    def _require_screen(identity: Identity, key: str) -> None:
        for group in product_nav.NAV:
            for item in group.items:
                if item.key == key:
                    if item.visible(identity, _flags()):
                        return
                    raise HTTPException(
                        status_code=403,
                        detail=f"your role does not include the {item.label} screen",
                    )
        raise HTTPException(status_code=404, detail="no such screen")

    def _purpose(identity: Identity, asserted: str) -> str:
        """The purpose this act of reading is done under, validated
        against what this role may assert. Refused purposes are audited
        as denials by require(), like any other.

        AN UNSTATED PURPOSE FALLS BACK TO THE ROLE'S OWN DEFAULT, never to
        a literal: a clinician's default is treatment and a records role's
        is operations, so a screen that hardcoded one would be a screen
        half the roles could not open. The person can still assert a
        different one on the request, where the assertion attaches.
        """
        asserted = (asserted or "").strip() or role_default_purpose(identity)
        try:
            purpose = validate_purpose(asserted)
            if not purpose_allowed(identity, purpose):
                require(identity, f"purpose:{purpose}")
            return purpose
        except NotAuthorized as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _context_or_prompt(request: Request) -> Optional[dict]:
        return patient_in_context(request.session)

    def _chart(identity: Identity, reference: str, purpose: str, action: str) -> dict:
        """The patient's resources as {storage_key: resource}.

        AUDITED BEFORE THE READ, never after: an entry written afterwards
        is one a failure can lose, and a disclosure that happened without
        a record of it is the exact condition the audit trail exists to
        make impossible.
        """
        record(identity, action, reference, purpose)
        out: dict[str, dict] = {}
        for row in reader.resources_for_patient(reference):
            key = row.get("storage_key")
            if not key:
                continue
            try:
                out[key] = reader.read_resource(key)
            except Exception as exc:      # one unreadable object is not a dead screen
                log.warning("chart read skipped %s: %s", key, exc)
        return out

    def _store_sample(limit: int = 400, resource_type: Optional[str] = None) -> dict:
        """{storage_key: resource} from across the store.

        Goes through reader.sample_resources(), NOT expiring_resources().
        The latter filters to records near their retention date - three
        screens were using it as a store sample and were reporting on a
        biased slice, and on a deployment that sets no retention dates, on
        nothing at all. A sweep that silently covers zero records looks
        exactly like a clean store.
        """
        out: dict[str, dict] = {}
        for row in reader.sample_resources(limit=limit, resource_type=resource_type):
            key = row.get("storage_key")
            if not key:
                continue
            try:
                out[key] = reader.read_resource(key)
            except Exception as exc:
                log.warning("store sample skipped %s: %s", key, exc)
        return out

    def _hour_of(value: str):
        """The local hour from a FHIR dateTime, or None.

        None rather than a guess: an Encounter with no start time is not
        an encounter at midnight, and bucketing it there would put a spike
        at 00:00 that no clinic worked. The count of undated encounters is
        shown beside the curve instead.
        """
        text = (value or "").strip()
        if len(text) < 13 or "T" not in text:
            return None
        try:
            return int(text.split("T", 1)[1][:2])
        except (ValueError, IndexError):
            return None

    def _claims_history() -> "object":
        """Adjudicated outcomes read from the store's own ExplanationOfBenefit
        resources.

        DERIVED, NOT INJECTED. This was an app.state key nothing ever set,
        so the screen said "no adjudicated history" on every deployment
        including ones holding thousands of adjudicated claims. The history
        is in the store; it just was not being read.

        An EOB's `outcome` is the adjudication: FHIR R4 defines complete /
        error / partial. Anything that is not `complete` counts as a denial
        for this purpose, and a resource with no outcome at all is not
        counted either way - an unadjudicated claim is not a denied one,
        and treating it as one would inflate every rate on the screen.
        """
        from core.capabilities.claims import History

        by_payer: dict[str, list[int]] = {}
        by_line: dict[str, list[int]] = {}
        for resource in _store_sample(500, resource_type="ExplanationOfBenefit").values():
            outcome = (resource.get("outcome") or "").strip().lower()
            if not outcome:
                continue                      # never adjudicated; not a denial
            denied = 1 if outcome != "complete" else 0
            payer = ((resource.get("insurer") or {}).get("display")
                     or (resource.get("insurer") or {}).get("reference") or "").strip()
            if payer:
                slot = by_payer.setdefault(payer, [0, 0])
                slot[0] += denied
                slot[1] += 1
            for item in (resource.get("item") or []):
                for coding in ((item.get("productOrService") or {}).get("coding") or []):
                    code = (coding.get("code") or "").strip()
                    if code:
                        slot = by_line.setdefault(code, [0, 0])
                        slot[0] += denied
                        slot[1] += 1
        return History(
            by_payer={k: (v[0], v[1]) for k, v in by_payer.items()},
            by_service_line={k: (v[0], v[1]) for k, v in by_line.items()},
        )

    def _value_sets():
        from core.terminology.loader import configured_value_sets

        return configured_value_sets()

    def _chunks(resources_by_key: dict):
        """Serialized chunks for the retrieval-backed engines.

        Goes through core/rag/pipeline.serialize_corpus - the one place
        resources become text, shared with the ETL - so a capability
        screen and the assistant can never disagree about what a record
        says or about which records segmentation excluded.
        """
        from core.governance.segmentation import CategoryValueSets
        from core.rag.pipeline import serialize_corpus

        value_sets = _value_sets() or CategoryValueSets(codes={}, departments={})
        return serialize_corpus(resources_by_key, value_sets)

    # ---- 5.13 ingest & mapping QA ------------------------------------

    @app.get("/product/ingest", response_class=HTMLResponse)
    def ingest_qa(
        request: Request,
        purpose_of_use: str = "",
        identity: Identity = Depends(current_identity),
    ):
        """The real data-quality sweep (core/capabilities/data_quality.py).

        NO PATIENT DIMENSION BY DESIGN, and it says so: unmapped codings,
        duplicate-patient candidates and display drift are properties of
        the store, not of one chart. Scoped to the patient in context
        would answer a different and much less useful question.
        """
        _require_screen(identity, "ingest")
        purpose = _purpose(identity, purpose_of_use)

        from core.capabilities.data_quality import analyze

        stats = reader.stats()
        record(identity, "quality.sweep", "store", purpose)
        # A bounded sample: the sweep is O(resources) and this screen is
        # interactive. The count it sampled is shown, so nobody reads a
        # partial sweep as a complete one.
        sampled = _store_sample(400)
        report = analyze(sampled)
        return page(request, "ingest_qa.html", identity, active="ingest",
                    stats=stats, report=report, sampled=len(sampled),
                    purpose=purpose, no_patient_dimension=_NO_PATIENT_DIMENSION)

    # ---- 5.2 summarization -------------------------------------------

    @app.get("/product/summary", response_class=HTMLResponse)
    def summary_screen(
        request: Request,
        purpose_of_use: str = "",
        identity: Identity = Depends(current_identity),
    ):
        """The chart spine, rendered by core/capabilities/summarization.py.

        Deterministic: no model runs here. Every line is a dated entry
        the spine builder found in the record, which is why this screen
        needs no release gate - there is nothing generated to gate.
        """
        _require_screen(identity, "summary")
        ctx = _context_or_prompt(request)
        if ctx is None:
            return page(request, "summary.html", identity, active="summary",
                        summary=None, needs_patient=True, purpose=None)

        purpose = _purpose(identity, purpose_of_use)
        from core.capabilities.summarization import render_summary
        from core.rag.spine import build_spine

        resources = _chart(identity, ctx["reference"], purpose, "record.read.summary")
        spine = build_spine(_chunks(resources), resources)
        return page(request, "summary.html", identity, active="summary",
                    summary=render_summary(spine), needs_patient=False,
                    patient=ctx, purpose=purpose, resource_count=len(resources))

    # ---- 5.4 prior auth & appeals ------------------------------------

    @app.get("/product/priorauth", response_class=HTMLResponse)
    def priorauth_screen(
        request: Request,
        criteria: str = "",
        kind: str = "prior_authorization",
        identity: Identity = Depends(current_identity),
    ):
        """A packet assembled by core/capabilities/prior_auth.py.

        The purpose is Payment by definition - the engine says so and
        writes it that way - so this screen does not offer a choice it
        would then have to ignore.
        """
        _require_screen(identity, "priorauth")
        ctx = _context_or_prompt(request)
        typed = [c.strip() for c in criteria.splitlines() if c.strip()]
        if ctx is None or not typed:
            return page(request, "priorauth.html", identity, active="priorauth",
                        packet=None, needs_patient=ctx is None, patient=ctx,
                        criteria=criteria, kind=kind)

        if kind not in ("prior_authorization", "appeal"):
            raise HTTPException(status_code=400, detail="unknown packet kind")

        from core.capabilities.prior_auth import Criterion, assemble_packet

        resources = _chart(identity, ctx["reference"], "payment", f"{kind}.assemble")
        packet = assemble_packet(
            [Criterion(criterion_id=f"c{i}", text=c, query=c)
             for i, c in enumerate(typed, start=1)],
            _chunks(resources),
            patient_reference=ctx["reference"],
            packet_kind=kind,
            actor=identity.username,
        )
        return page(request, "priorauth.html", identity, active="priorauth",
                    packet=packet, needs_patient=False, patient=ctx,
                    criteria=criteria, kind=kind)

    # ---- 5.9 trial screening -----------------------------------------

    @app.get("/product/trials", response_class=HTMLResponse)
    def trials_screen(
        request: Request,
        inclusion: str = "",
        exclusion: str = "",
        identity: Identity = Depends(current_identity),
    ):
        """The coordinator worklist from core/capabilities/trial_screening.py.

        A POPULATION SCREEN, so it deliberately does NOT follow the patient
        in context - the question is which patients might be eligible, and
        scoping it to one would answer nothing. It says so on the screen
        rather than silently ignoring the context.

        EXCLUSIONS ARE SHOWN, NEVER SILENTLY APPLIED. A candidate with an
        exclusion hit still appears, flagged, because the decision to rule
        somebody out of a trial belongs to a coordinator reading the
        citation - not to a retrieval score.
        """
        _require_screen(identity, "trials")
        inc = [c.strip() for c in inclusion.splitlines() if c.strip()]
        exc = [c.strip() for c in exclusion.splitlines() if c.strip()]
        if not inc:
            return page(request, "trials.html", identity, active="trials",
                        candidates=None, inclusion=inclusion, exclusion=exclusion,
                        screened=0, no_patient_dimension=_NO_PATIENT_DIMENSION)

        purpose = _purpose(identity, "research")
        from core.capabilities.trial_screening import TrialCriterion, screen

        record(identity, "trial.screen", f"{len(inc)} inclusion criteria", purpose)
        chunks_by_patient: dict[str, list] = {}
        for row in reader.search_patients("", limit=25) or []:
            ref = row.get("patient_reference")
            if not ref:
                continue
            resources = {}
            for r in reader.resources_for_patient(ref):
                key = r.get("storage_key")
                if key:
                    try:
                        resources[key] = reader.read_resource(key)
                    except Exception:
                        continue
            chunks_by_patient[ref] = _chunks(resources)

        criteria = (
            [TrialCriterion(criterion_id=f"i{i}", kind="inclusion", text=c, query=c)
             for i, c in enumerate(inc, start=1)]
            + [TrialCriterion(criterion_id=f"e{i}", kind="exclusion", text=c, query=c)
               for i, c in enumerate(exc, start=1)]
        )
        candidates = screen(criteria, chunks_by_patient, actor=identity.username)
        return page(request, "trials.html", identity, active="trials",
                    candidates=candidates, inclusion=inclusion, exclusion=exclusion,
                    screened=len(chunks_by_patient),
                    no_patient_dimension=_NO_PATIENT_DIMENSION)

    # ---- 5.10 chart abstraction --------------------------------------

    @app.get("/product/abstraction", response_class=HTMLResponse)
    def abstraction_screen(
        request: Request,
        elements: str = "",
        identity: Identity = Depends(current_identity),
    ):
        """The abstraction worklist (core/capabilities/abstraction.py).

        THE ENGINE PROPOSES; A NAMED HUMAN CONFIRMS. Nothing here exports
        while any element is unconfirmed - the worklist refuses, by its own
        rule, and there is no partial export and no auto-confirm. This
        screen shows the proposals and their citations so the abstractor
        reads the chart text, not a score.
        """
        _require_screen(identity, "abstraction")
        ctx = _context_or_prompt(request)
        wanted = [e.strip() for e in elements.splitlines() if e.strip()]
        if ctx is None or not wanted:
            return page(request, "abstraction.html", identity, active="abstraction",
                        worklist=None, needs_patient=ctx is None, patient=ctx,
                        elements=elements)

        purpose = _purpose(identity, "")
        from core.capabilities.abstraction import AbstractionWorklist, MeasureElement

        resources = _chart(identity, ctx["reference"], purpose, "abstraction.propose")
        worklist = AbstractionWorklist(
            measure_id="ad-hoc",
            patient_reference=ctx["reference"],
            elements=[MeasureElement(element_id=f"e{i}", description=e, query=e)
                      for i, e in enumerate(wanted, start=1)],
        )
        worklist.propose(_chunks(resources))
        return page(request, "abstraction.html", identity, active="abstraction",
                    worklist=worklist, needs_patient=False, patient=ctx,
                    elements=elements,
                    states=[worklist._elements[k] for k in sorted(worklist._elements)],
                    pending=worklist.unconfirmed())

    # ---- 5.6 inbox triage --------------------------------------------

    @app.get("/product/inbox", response_class=HTMLResponse)
    def inbox_triage(
        request: Request,
        min_urgent_recall: float = 0.98,
        identity: Identity = Depends(current_identity),
    ):
        """Inbox triage's operating point, and what it costs.

        THE MISS RATE IS THE HEADLINE, not the accuracy.
        core/capabilities/triage.py picks the threshold that holds urgent
        recall at or above the bar the operator sets, and reports the
        routine-escalation rate that buys it. A triage screen that leads
        with accuracy is one where the urgent message it missed is a
        rounding error; here it is the first number.

        WITHOUT A REGISTERED SCORER NOTHING ROUTES. The engine asks the
        model registry, which refuses an unregistered model - so this
        screen shows the operating point and says plainly that routing is
        unavailable, rather than routing on a model nobody approved.
        """
        _require_screen(identity, "inbox")
        purpose = _purpose(identity, "")

        from core.capabilities.triage import TriageError, choose_operating_point

        # The platform HAS a model registry - the Control panel writes to it -
        # and this screen was reading an app.state key nothing set, so it
        # reported "no scorer registered" on deployments with a populated
        # registry. Falls back to the governance registry when one is wired
        # for model-governance use.
        registry = getattr(app.state, "model_registry", None)
        if registry is not None:
            registered = tuple(registry.registered_ids())
        else:
            state = getattr(app.state, "platform_state", None)
            registered = tuple(
                m["model_id"] for m in (state.list_models() if state else [])
                if m.get("model_id") and m.get("slot") == "triage"
            )

        # The published validation set this deployment calibrated on. Held
        # by the deployment, not invented here: an operating point with no
        # validation behind it is a threshold with a decimal point.
        validation = getattr(app.state, "triage_validation", None)
        point = error = None
        if validation:
            try:
                point = choose_operating_point(
                    validation["scores"], validation["is_urgent"],
                    min_urgent_recall=min_urgent_recall,
                )
            except (TriageError, ValueError) as exc:
                error = str(exc)
        record(identity, "triage.operating_point", f"recall>={min_urgent_recall}", purpose)
        return page(request, "inbox.html", identity, active="inbox",
                    point=point, error=error, registered=registered,
                    min_urgent_recall=min_urgent_recall,
                    validated=bool(validation),
                    no_patient_dimension=_NO_PATIENT_DIMENSION)

    # ---- 6.3 fairness screen -----------------------------------------

    @app.get("/product/fairness", response_class=HTMLResponse)
    def fairness_screen(
        request: Request,
        variables: str = "",
        identity: Identity = Depends(current_identity),
    ):
        """Screen a model's declared input schema (45 CFR 92.210).

        RUNS core/governance/fairness.py, which had no caller anywhere in
        the codebase - the module that decides whether a model may be
        registered was reachable only from its own tests.

        WHAT IT CATCHES AND WHAT IT CANNOT. A declared protected variable
        fails outright. A named proxy candidate - ZIP, and the rest of the
        §5.7 list - fails unless an operator has recorded a basis for it.
        What it cannot catch is an UNDECLARED proxy, and the module says so
        itself; this screen repeats that rather than letting a pass read as
        a clean bill of health.
        """
        _require_screen(identity, "fairness")
        declared = [v.strip() for v in variables.replace(",", "\n").splitlines() if v.strip()]
        result = None
        if declared:
            from core.governance.fairness import screen_input_schema

            result = screen_input_schema(declared)
            record(identity, "fairness.screen",
                   f"{len(declared)} variables ok={result.ok}", _purpose(identity, ""))

        from core.governance.fairness import (
            PROTECTED_CATEGORIES,
            PROXY_CANDIDATE_VARIABLES,
        )

        return page(request, "fairness.html", identity, active="fairness",
                    result=result, variables=variables,
                    categories=PROTECTED_CATEGORIES,
                    proxies=sorted(PROXY_CANDIDATE_VARIABLES),
                    no_patient_dimension=_NO_PATIENT_DIMENSION)

    # ---- 6.1 sensitive categories ------------------------------------

    @app.get("/product/segmentation", response_class=HTMLResponse)
    def segmentation_screen(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        """What segmentation excluded from this deployment's own store.

        THE COUNT IS THE PRODUCT. core/governance/segmentation.py decides
        per resource and fails closed: any category hit, any restricted
        confidentiality code, any unmapped sensitivity label and any shape
        it cannot classify all exclude. This screen runs that real decision
        over a sample and reports what came back - including the
        unclassifiable ones, which are the interesting number, because an
        exclusion nobody can explain is a mapping gap rather than a
        sensitive record.

        WITH NO VALUE SETS CONFIGURED it says so. A screen reporting zero
        exclusions because no category is defined looks identical to one
        reporting zero because the store holds nothing sensitive.
        """
        _require_screen(identity, "segmentation")
        purpose = _purpose(identity, "")
        value_sets = _value_sets()
        if value_sets is None:
            return page(request, "segmentation.html", identity, active="segmentation",
                        configured=False, stats=None, decisions=[], sampled=0,
                        no_patient_dimension=_NO_PATIENT_DIMENSION)

        from core.governance.segmentation import SegmentationStats, classify

        record(identity, "segmentation.sweep", "store", purpose)
        stats = SegmentationStats()
        excluded = []
        resources = _store_sample(400)
        sampled = len(resources)
        for key, resource in sorted(resources.items()):
            decision = classify(resource, value_sets)
            stats.observe(decision)
            if not decision.include:
                excluded.append((key, decision))
        return page(request, "segmentation.html", identity, active="segmentation",
                    configured=True, stats=stats, decisions=excluded[:50],
                    sampled=sampled, no_patient_dimension=_NO_PATIENT_DIMENSION)

    # ---- 5.14 / 6.2 / 6.5 ambient documentation ----------------------

    @app.get("/product/ambient", response_class=HTMLResponse)
    def ambient_screen(
        request: Request,
        jurisdiction: str = "",
        modality: str = "in_person",
        attested: str = "0",
        consented: str = "0",
        identity: Identity = Depends(current_identity),
    ):
        """Whether this encounter may be recorded at all, and why not.

        TWO INDEPENDENT GATES, BOTH REAL, NEITHER PREVIOUSLY REACHABLE.
        core/governance/consent_gate.py decides the state-law question and
        core/governance/preflight.py decides the egress question, and both
        had zero callers outside their own tests.

        THE ORDER IS NOT ARBITRARY. Consent first: if this encounter may
        not be recorded, whether the transcription service is configured
        safely is a question about nothing. A screen that led with the
        egress evidence would invite an operator to fix the easy technical
        gate and read that as permission.

        DENY IS NOT AN ERROR STATE. An unresolved jurisdiction, an
        unsettled one, and a missing consent record all refuse, and each
        refusal names which of the three it was - because "capture is not
        available" without a reason is what gets worked around.
        """
        _require_screen(identity, "ambient")
        purpose = _purpose(identity, "")
        ctx = _context_or_prompt(request)

        from core.governance.consent_gate import (
            ConsentRecord,
            ConsentStatus,
            Modality,
            consent_standard,
            evaluate_capture,
        )

        mode = Modality.TELEHEALTH if modality == "telehealth" else Modality.IN_PERSON
        state = (jurisdiction or "").strip().upper()
        consent = None
        if consented == "1":
            consent = ConsentRecord(
                status=ConsentStatus.GRANTED,
                timestamp=utcnow().isoformat(),
                obtained_by=identity.username,
                verbal_attestation_captured=(attested == "1"),
            )
        standard = consent_standard(state, mode) if state else None
        decision = evaluate_capture(
            state or None, mode, consent,
            actor=identity.username,
            encounter_key=(ctx["reference"] if ctx else "ambient/unattributed"),
        )
        record(identity, "ambient.capture.evaluated",
               f"{state or 'unresolved'}/{mode.value} allowed={decision.allowed}", purpose)

        # The egress gate, evaluated from whatever evidence the deployment
        # has actually recorded. No evidence is a refusal, not a pass.
        from core.governance.preflight import (
            AwsTranscribeEvidence,
            preflight_aws_transcribe,
        )

        evidence = getattr(app.state, "transcribe_evidence", None)
        egress = preflight_aws_transcribe(
            evidence if isinstance(evidence, AwsTranscribeEvidence)
            else AwsTranscribeEvidence(
                ai_services_opt_out_effective=False,
                output_bucket_name=None,
                output_encryption_kms_key_id=None,
            ),
            actor=identity.username,
        )
        return page(request, "ambient.html", identity, active="ambient",
                    patient=ctx, decision=decision, egress=egress,
                    standard=standard.value if standard else None,
                    jurisdiction=state, modality=modality,
                    attested=attested == "1", consented=consented == "1",
                    store=_ambient_store())

    def _ambient_store():
        """The deployment's ambient audio store, if it is genuinely one.

        TYPE-CHECKED, NOT DUCK-TYPED. AmbientAudioStore refuses at
        construction to sit in a bucket whose label contains "general" -
        ambient audio does not live with the record, and it expires on its
        own schedule rather than the note's. Accepting anything with the
        right method names here would let a deployment wire the general
        store in by accident and lose both properties silently.
        """
        from core.governance.ambient_store import AmbientAudioStore

        candidate = getattr(app.state, "ambient_store", None)
        return candidate if isinstance(candidate, AmbientAudioStore) else None

    # ---- 6.6 HTI-1 source attributes ---------------------------------

    @app.get("/product/attributes", response_class=HTMLResponse)
    def source_attributes_screen(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        """Where this record actually came from - HTI-1's question.

        RUNS core/governance/source_attributes.py, which had no caller
        anywhere. Two different artifacts share the name and this screen
        shows both: the PER-RECORD provenance for the patient in context
        (which run carried it, which vendor profile the connector spoke),
        and the PER-MODEL source-attribute set 45 CFR 170.315(b)(11)
        obligates a certified Module to let its users record.

        NOTHING HERE IS INVENTED. A model with no recorded attribute set
        reports as absent, not as a set of empty categories - the module's
        own validate() refuses an empty category, and a screen that filled
        them in with placeholders would be manufacturing the artifact the
        obligation exists to make somebody produce.
        """
        _require_screen(identity, "attributes")
        ctx = _context_or_prompt(request)
        purpose = _purpose(identity, "")

        from core.governance.source_attributes import (
            ATTRIBUTE_CATEGORIES,
            IRM_CHARACTERISTICS,
            SourceAttributeError,
        )

        rows = []
        if ctx is not None:
            record(identity, "record.read.provenance", ctx["reference"], purpose)
            for r in reader.resources_for_patient(ctx["reference"])[:200]:
                rows.append({
                    "resource_type": r.get("resource_type"),
                    "storage_key": r.get("storage_key"),
                    "stored_at": r.get("stored_at"),
                })

        # Per-model artifacts the deployment has actually recorded.
        published = getattr(app.state, "source_attributes", None) or {}
        models = []
        for model_id, artifact in sorted(published.items()):
            try:
                artifact.validate()
                models.append({"model_id": model_id, "valid": True, "error": None})
            except SourceAttributeError as exc:
                models.append({"model_id": model_id, "valid": False, "error": str(exc)})

        return page(request, "source_attributes.html", identity, active="attributes",
                    patient=ctx, needs_patient=ctx is None, rows=rows,
                    categories=ATTRIBUTE_CATEGORIES,
                    irm_characteristics=IRM_CHARACTERISTICS,
                    models=models)

    # ---- 5.7 no-show risk and the action space -----------------------

    @app.get("/product/noshow", response_class=HTMLResponse)
    def noshow_screen(
        request: Request,
        action: str = "",
        basis: str = "",
        identity: Identity = Depends(current_identity),
    ):
        """What an operational prediction is allowed to DO.

        RUNS core/governance/action_space.py, which had no caller
        anywhere. The point of this screen is not the score - it is the
        vocabulary of actions a score may trigger. A no-show model that
        sends an extra reminder or offers transport improves access for
        the patients who struggle to attend. The same model wired to
        denial, deprioritization or double-booking quietly penalizes
        them, and that is the same model.

        THE ACTION VOCABULARY IS CLOSED. An action the module has never
        heard of is REFUSED, not guessed at - classifying an unknown
        action as "probably supportive" is exactly the silent fallback
        the invariants prohibit.
        """
        _require_screen(identity, "noshow")
        purpose = _purpose(identity, "")

        from core.governance.action_space import (
            RESTRICTIVE_ACTIONS,
            SUPPORTIVE_ACTIONS,
            OperatorOverride,
            evaluate_action,
        )

        decision = None
        if action.strip():
            override = (OperatorOverride(operator=identity.username, basis=basis.strip())
                        if basis.strip() else None)
            decision = evaluate_action(
                action.strip(), override=override, actor=identity.username,
                subject_key="noshow/screen",
            )
            record(identity, "action_space.evaluated",
                   f"{action.strip()} allowed={decision.allowed}", purpose)

        return page(request, "noshow.html", identity, active="noshow",
                    decision=decision, action=action, basis=basis,
                    supportive=sorted(SUPPORTIVE_ACTIONS),
                    restrictive=sorted(RESTRICTIVE_ACTIONS),
                    no_patient_dimension=_NO_PATIENT_DIMENSION)

    # ---- 5.5 documentation gaps --------------------------------------

    @app.get("/product/coding", response_class=HTMLResponse)
    def coding_screen(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        """Documentation gaps, read in BOTH directions.

        core/capabilities/coding_integrity.py refuses to look one way, and
        this screen shows both counts side by side so a deployment that
        only ever acts on the revenue-positive list can see itself doing
        it. A tool that finds only diagnoses to add is upcoding with
        better branding.

        IT DRAFTS QUERIES. Every finding is a question for a coder, with
        the citations that provoked it. Nothing here writes to a record.
        """
        _require_screen(identity, "coding")
        ctx = _context_or_prompt(request)
        if ctx is None:
            return page(request, "coding.html", identity, active="coding",
                        report=None, needs_patient=True, patient=None)

        purpose = _purpose(identity, "")
        from core.capabilities.coding_integrity import analyze

        resources = _chart(identity, ctx["reference"], purpose, "coding.gaps")
        report = analyze(resources, support_map=getattr(app.state, "code_support_map", None))
        return page(request, "coding.html", identity, active="coding",
                    report=report, needs_patient=False, patient=ctx)

    # ---- population health & quality measures -------------------------

    @app.get("/product/measures", response_class=HTMLResponse)
    def measures_screen(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        """Prevalence, and never without its interval.

        core/capabilities/measures.py will not return a proportion on its
        own: "14% have diabetes" reads identically whether it came from
        40,000 patients or from 14, and the interval is what tells them
        apart. Wilson rather than Wald, so a condition nobody in the
        sample has gets an honest upper bound instead of [0, 0].
        """
        _require_screen(identity, "measures")
        purpose = _purpose(identity, "")
        from core.capabilities.measures import profile

        stats = reader.stats()
        record(identity, "measures.profile", "store", purpose)

        # PREVALENCE NEEDS A PATIENT-LEVEL NUMERATOR, AND stats DOES NOT
        # HAVE ONE. resource_type_counts counts OBJECTS - one patient with
        # forty Observations contributes forty - so dividing it by the
        # patient count is not a proportion at all, and the first version
        # of this route did exactly that. measures.py caught it by
        # refusing a numerator larger than its denominator, which is the
        # check earning its place.
        #
        # So prevalence comes from the OMOP layer, which counts DISTINCT
        # PERSONS, or it does not appear. Composition counts are shown
        # either way and are labelled as counts, never as rates.
        # PATIENT-LEVEL COUNTS, COUNTED IN PATIENTS. A prevalence needs a
        # numerator of PEOPLE: one patient with forty Observations of the
        # same condition is one case, not forty. So the Conditions are
        # sampled and reduced to a set of distinct patient references per
        # condition before anything is divided.
        #
        # The denominator is the number of patients IN THE SAMPLE, not the
        # store's total patient count. Dividing a sampled numerator by a
        # store-wide denominator understates every rate by whatever
        # fraction the sample missed, and does it silently.
        conditions = _store_sample(600, resource_type="Condition")
        by_condition: dict[str, set[str]] = {}
        patients_seen: set[str] = set()
        for key, resource in conditions.items():
            subject = ((resource.get("subject") or {}).get("reference")
                       or resource.get("patient_reference") or key)
            patients_seen.add(subject)
            for coding in ((resource.get("code") or {}).get("coding") or []):
                label = (coding.get("display") or coding.get("code") or "").strip()
                if label:
                    by_condition.setdefault(label, set()).add(subject)

        prof = None
        if patients_seen:
            prof = profile(len(patients_seen),
                           {name: len(refs) for name, refs in by_condition.items()})
        return page(request, "measures.html", identity, active="measures",
                    profile=prof, stats=stats,
                    sampled_conditions=len(conditions),
                    sampled_patients=len(patients_seen),
                    composition=sorted((stats.resource_type_counts or {}).items()),
                    no_patient_dimension=_NO_PATIENT_DIMENSION)

    # ---- claims & billing --------------------------------------------

    @app.get("/product/claims", response_class=HTMLResponse)
    def claims_screen(
        request: Request,
        payer: str = "",
        service_line: str = "",
        documented: str = "1",
        identity: Identity = Depends(current_identity),
    ):
        """Denial risk, every factor printed and cited to its counts.

        THE PURPOSE IS PAYMENT and this read is audited as one, the same
        way a chart view is audited under treatment.

        A PAYER THIS STORE HAS NEVER SEEN ADJUDICATE ANYTHING GETS NO
        SCORE. An average borrowed from other payers is not a fact about
        this one, and the screen says "unscored" rather than inventing a
        number a biller would act on.
        """
        _require_screen(identity, "claims")
        purpose = _purpose(identity, "payment")
        ctx = _context_or_prompt(request)

        from core.capabilities.claims import History, score_claim

        history = _claims_history()
        risk = None
        if payer.strip() and service_line.strip():
            record(identity, "claims.scored",
                   f"{payer.strip()}/{service_line.strip()}", purpose)
            risk = score_claim(
                claim_id=f"{payer.strip()}:{service_line.strip()}",
                payer=payer.strip(), service_line=service_line.strip(),
                history=history, documented=documented == "1",
            )
        return page(request, "claims.html", identity, active="claims",
                    risk=risk, payer=payer, service_line=service_line,
                    documented=documented == "1", patient=ctx,
                    payers=sorted(history.by_payer),
                    lines=sorted(history.by_service_line),
                    has_history=bool(history.by_payer or history.by_service_line))

    # ---- scheduling ---------------------------------------------------

    @app.get("/product/scheduling", response_class=HTMLResponse)
    def scheduling_screen(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        """The demand curve, and what may be done about it.

        COUNTS AND ARITHMETIC ONLY. No patient is named on this plane and
        no schedule is changed by a model - the screen reports encounter
        volume by hour and then defers, explicitly, to
        core/governance/action_space.py for what an operational prediction
        is permitted to trigger. Overbooking is a restrictive action and
        appears here as one.
        """
        _require_screen(identity, "scheduling")
        purpose = _purpose(identity, "")

        from core.governance.action_space import (
            RESTRICTIVE_ACTIONS,
            SUPPORTIVE_ACTIONS,
        )

        record(identity, "scheduling.demand", "store", purpose)

        # THE CURVE IS BUILT FROM WHEN CARE HAPPENED, not from when the
        # record was written. The first version of this screen bucketed
        # `stored_at` - the index's storage timestamp - and called the
        # result a demand curve. It is not one: it measures when the
        # ingestion job ran, so a nightly bulk load produces a single
        # enormous 2am spike and a clinic that runs 9-5 looks like it
        # operates at night. Encounter.period.start is the appointment.
        encounters = _store_sample(600, resource_type="Encounter")
        by_hour: dict[int, int] = {}
        undated = 0
        for resource in encounters.values():
            start = ((resource.get("period") or {}).get("start")
                     or resource.get("start") or "")
            hour = _hour_of(start)
            if hour is None:
                undated += 1
                continue
            by_hour[hour] = by_hour.get(hour, 0) + 1
        curve = [(h, by_hour.get(h, 0)) for h in range(24)]
        peak = max((n for _, n in curve), default=0)
        return page(request, "scheduling.html", identity, active="scheduling",
                    curve=curve, peak=peak, total=sum(n for _, n in curve),
                    sampled=len(encounters), undated=undated,
                    supportive=sorted(SUPPORTIVE_ACTIONS),
                    restrictive=sorted(RESTRICTIVE_ACTIONS),
                    no_patient_dimension=_NO_PATIENT_DIMENSION)

    # ---- psychotherapy notes ------------------------------------------

    @app.get("/product/psychotherapy", response_class=HTMLResponse)
    def psychotherapy_screen(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        """The record class 45 CFR 164.508(a)(2) treats separately.

        THIS SCREEN DELIBERATELY OFFERS LESS THAN EVERY OTHER ONE. No
        assistant prompt, no copy control, no export. The assistant has no
        tool that reaches this store unless four independent switches are
        all on, and putting an ask box on the screen would imply otherwise.

        WHAT IT SHOWS IS THE POSTURE: which of those four gates this
        deployment has actually set, and the reader's own role among them.
        Somebody who cannot see these notes should be able to see WHY
        without being shown one.
        """
        _require_screen(identity, "psychotherapy")
        purpose = _purpose(identity, "")
        record(identity, "psychotherapy.posture", "deployment", purpose)

        rt = getattr(app.state, "assistant", None)
        settings = rt.effective_settings() if rt is not None else None
        gates = [
            {"name": "PHI access tier is 'lookup'",
             "on": bool(settings and settings.allows_lookup),
             "detail": "PHI_AI_ASSISTANT_PHI_ACCESS - the lookup tier alone "
                       "never reaches psychotherapy notes"},
            {"name": "Psychotherapy access acknowledged",
             "on": bool(settings and settings.psychotherapy_access),
             "detail": "PHI_AI_ASSISTANT_PSYCHOTHERAPY_ACKNOWLEDGED - its own "
                       "switch, off by default at every tier"},
            {"name": "Psychotherapy retrieval role configured",
             "on": bool(getattr(app.state, "psychotherapy_search", None)),
             "detail": "its own bucket under its own key, with its own "
                       "database role"},
            {"name": "Your role carries 'psychotherapy'",
             "on": identity.can("psychotherapy"),
             "detail": "and a stated purpose, asserted per read"},
        ]
        return page(request, "psychotherapy.html", identity, active="psychotherapy",
                    gates=gates, open_count=sum(1 for g in gates if g["on"]))

    # ---- 5.3 patient instructions ------------------------------------

    @app.get("/product/instructions", response_class=HTMLResponse)
    def instructions_screen(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        _require_screen(identity, "instructions")
        ctx = _context_or_prompt(request)
        return page(request, "instructions.html", identity, active="instructions",
                    needs_patient=ctx is None, patient=ctx,
                    draft=request.session.get(INSTRUCTIONS_DRAFT_KEY),
                    notice=None)

    @app.post("/product/instructions", response_class=HTMLResponse)
    def instructions_check(
        request: Request,
        source_text: str = Form(""),
        draft_text: str = Form(""),
        medications: str = Form(""),
        identity: Identity = Depends(current_identity),
    ):
        """Run the no-new-assertions check over a drafted instruction.

        THE CHECK IS THE PRODUCT. Plain-language instructions are the one
        thing here written FOR the patient, and the failure that matters
        is not an awkward sentence - it is a dose, a frequency or a
        follow-up date the chart never said. core/capabilities/
        patient_instructions.py compares the numbers and the vocabulary of
        the draft against the source and the structured lists, and
        anything new is surfaced rather than smoothed over.
        """
        _require_screen(identity, "instructions")
        ctx = _context_or_prompt(request)
        if ctx is None:
            raise HTTPException(status_code=400, detail="no patient in context")

        from core.capabilities.patient_instructions import check_no_new_assertions

        meds = [m.strip() for m in medications.split(",") if m.strip()]
        check = check_no_new_assertions(
            source_text, draft_text, medication_names=meds,
        )
        draft = {
            "patient": ctx,
            "source_text": source_text,
            "draft_text": draft_text,
            "medications": meds,
            "passed": bool(check.ok),
            "new_numbers": list(check.new_numbers),
            "new_medications": list(check.new_medications),
            "new_followups": list(check.new_followups),
            "reason": check.reason,
        }
        request.session[INSTRUCTIONS_DRAFT_KEY] = draft
        purpose = _purpose(identity, "")
        record(identity, "instructions.checked",
               f"{ctx['reference']} pass={draft['passed']}", purpose)
        return page(request, "instructions.html", identity, active="instructions",
                    needs_patient=False, patient=ctx, draft=draft, notice=None)

    @app.post("/product/instructions/file", response_class=HTMLResponse)
    def instructions_file(
        request: Request, identity: Identity = Depends(current_identity)
    ):
        """File a PASSING draft into the signature queue.

        THE RELEASE GATE, AND IT IS THE POINT OF THE SCREEN. A draft that
        failed the check, or that has no check at all, never files - and
        the refusal is AUDITED, because "the system declined to release
        this" is a fact somebody may later need to show. Nothing reaches
        a patient without a human signature either way; this gate decides
        whether a human is even offered the chance to sign.
        """
        _require_screen(identity, "instructions")
        draft = request.session.get(INSTRUCTIONS_DRAFT_KEY)
        if not draft:
            raise HTTPException(status_code=400, detail="nothing drafted")

        if not draft.get("passed"):
            record(identity, "ai.release_refused",
                   f"instructions/{draft['patient']['reference']} no-new-assertions gate",
                   _purpose(identity, ""))
            return page(request, "instructions.html", identity, active="instructions",
                        needs_patient=False, patient=draft["patient"], draft=draft,
                        notice=("The no-new-assertions check did not pass, so nothing "
                                "was filed. The refusal is on the audit trail. Correct "
                                "the draft so every number and medication it states is "
                                "one the chart states, then check it again."))

        # STAGED THROUGH THE RELEASE GATE, not merely recorded as filed.
        # core/governance/release_gate.py is the module that owns "nothing
        # patient-directed leaves without a named human releasing it, and
        # it leaves exactly once" - and it had no caller anywhere, which
        # made it a rule the codebase stated and did not run. Staging here
        # means the signature step is that gate's own release() rather
        # than a second, parallel notion of the same decision.
        gate = _release_gate()
        staged = gate.stage(
            patient_key=draft["patient"]["reference"],
            channel="discharge_instructions",
            content=draft["draft_text"],
        )
        record(identity, "instructions.filed",
               f"{draft['patient']['reference']} unsigned draft={staged.draft_id}",
               _purpose(identity, ""))
        request.session.pop(INSTRUCTIONS_DRAFT_KEY, None)
        return RedirectResponse("/signature", status_code=303)

    def _release_gate():
        """This worker's patient-release gate, made once.

        The outbound callable is the deployment's - a portal message
        writer, an EHR write. Absent one, staging still works and release
        raises, which is the correct posture: a draft that cannot be
        delivered must not silently look delivered.
        """
        from core.governance.release_gate import PatientReleaseGate

        gate = getattr(app.state, "release_gate", None)
        if gate is None:
            def _undeliverable(draft):
                raise RuntimeError(
                    "no outbound channel is configured for patient-directed "
                    "output; the draft stays staged rather than appearing sent"
                )

            gate = PatientReleaseGate(_undeliverable)
            app.state.release_gate = gate
        return gate
# Made by Ryan Gomez & Co. Inc.
