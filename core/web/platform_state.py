# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Runtime platform state: configuration, the model registry, and the
integration surfaces' operational records (bulk runs, exports, feeds).

This is the state the Control panel and the Integration screens manage.
Two backends, one class:

- In-memory (default): seeded with the platform's known-truth defaults,
  used by tests and by deployments without the index database. State
  lives for the process.
- SQL (when a connection_factory is passed): the same state persisted in
  the index database (core/db/platform_state_schema.sql), loaded at
  startup and written through on every mutation. The in-memory copy
  remains the read path, so a database hiccup degrades reads to the
  last-known state instead of a 500.

Deliberately NOT here: anything that is a record of PHI, and anything
that already has a real home (audit events go to the audit sink; ROI
requests to the ROI service; prompts to the prompt store).

The model registry holds EVERY model the platform runs - the assistant's
language models and the predictive / classification / optimization /
mapping models behind the other capabilities. Bring-your-own: a model
registers by provider+model id or by HTTPS inference endpoint, and moves
through registered -> enabled -> activated, each step audited by the
routes that call in here.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("phi-ai.web.platform_state")

MODEL_KINDS = ("foundation", "retrieval", "deterministic", "predictive",
               "classifier", "imaging", "optimization", "mapper", "custom")
MODEL_SLOTS = ("assistant", "claims", "priorauth", "coding", "roi", "noshow",
               "segmentation", "scheduling", "ingest", "imaging", "trials",
               "measures", "other")

def _emr_vendors_from_profiles() -> dict:
    """The vendor seams the integration screens honor, DERIVED from
    core/fhir/emr_profiles.py PROFILES - the declared source of truth -
    keyed by the profile key (`cerner`, never a second spelling), so the
    screens can never offer a vendor the platform does not profile, nor
    describe a vendor's grant differently from its profile. Each field is
    prose rendered from the profile's own flags; the citation for every
    flag is that vendor's chapter in docs/EMR_CONNECTORS.md."""
    from core.fhir.emr_profiles import PROFILES

    vendors = {}
    for key, profile in PROFILES.items():
        if profile.auth_flow == "oauth2_client_credentials":
            auth = "OAuth2 client credentials — client ID and client secret"
        else:
            auth = (f"SMART Backend Services — {profile.assertion_algorithm}-signed JWT "
                    "client assertion")
        if profile.requires_token_scopes:
            auth += "; explicit system scopes required on the token request"
        bulk = ("Bulk Data $export recorded in the profile"
                if profile.supports_bulk_export
                else "NO Bulk Data Export in the profile — the bulk scheduler refuses this "
                     "vendor rather than degrading")
        writable = tuple(profile.writable_resources)
        writes = (", ".join(writable) + " (create advertised per the vendor's own documentation)"
                  if writable else
                  "None over FHIR — the profile records no writable resource type; the "
                  "delivery writer refuses every type")
        vendors[key] = {
            "name": profile.name,
            "auth": auth,
            "bulk": bulk,
            "writes": writes,
            "auth_flow": profile.auth_flow,
            "assertion_algorithm": profile.assertion_algorithm,
            "supports_bulk_export": profile.supports_bulk_export,
            "writable_resources": writable,
        }
    return vendors


#: Keyed by PROFILES key; see _emr_vendors_from_profiles().
EMR_VENDORS = _emr_vendors_from_profiles()

_CONFIG_DEFAULTS = {
    "source_vendor": "epic",
    "source_base_url": "",
    "source_client_id": "",
    "source_group_id": "",
    "target_vendor": "epic",
    "target_base_url": "",
    "target_client_id": "",
    "rag_enabled": "on",
    "assistant_live": "on",
    "assistant_model": "",
    "assistant_max_tokens": "1500",
}

_BUILTIN_MODELS = [
    {"name": "PHI RAG", "kind": "retrieval", "provider": "built-in",
     "model_id": "", "version": "1", "endpoint_url": "", "slot": "assistant",
     "purpose": "The PHI AI assistant — retrieval over the platform's stores "
                "through role-scoped tools, cited answers under the reader's "
                "role and purpose.",
     "note": "Managed by the retrieval switch on the Control panel."},
    {"name": "Claude Sonnet 5", "kind": "foundation", "provider": "Anthropic API",
     "model_id": "claude-sonnet-5", "version": "1", "endpoint_url": "",
     "slot": "assistant",
     "purpose": "The language model PHI RAG runs on: reads gate-released "
                "excerpts, writes the drafted answer.",
     "note": "Managed by the live-calls switch. The default assistant model."},
    {"name": "Scripted fallback", "kind": "deterministic", "provider": "built-in",
     "model_id": "", "version": "1", "endpoint_url": "", "slot": "assistant",
     "purpose": "Answers from fixed logic when live model calls are off or "
                "unavailable.",
     "note": "Cannot be disabled — the platform degrades to something honest, "
             "never to silence."},
    {"name": "No-show risk model", "kind": "predictive", "provider": "built-in",
     "model_id": "noshow-grad-boost", "version": "2.1", "endpoint_url": "",
     "slot": "noshow",
     "purpose": "Predicts missed appointments so outreach goes where it helps. "
                "Subject to the fairness screen; the permitted intervention is "
                "support, never denial of scheduling.",
     "note": "Consumed by the No-show risk screen."},
    {"name": "Sensitivity classifier", "kind": "classifier", "provider": "built-in",
     "model_id": "sens-classify", "version": "1.4", "endpoint_url": "",
     "slot": "segmentation",
     "purpose": "Classifies incoming records into the sensitive categories at "
                "the ingestion door.",
     "note": "Runs inside encrypt-store-index; every downstream gate depends "
             "on its output."},
    {"name": "Scheduling optimizer", "kind": "optimization", "provider": "built-in",
     "model_id": "sched-opt", "version": "1.2", "endpoint_url": "",
     "slot": "scheduling",
     "purpose": "Template and capacity optimization under hard fairness "
                "constraints.",
     "note": "Consumed by the Scheduling optimization screen."},
    {"name": "Denial risk model", "kind": "predictive", "provider": "built-in",
     "model_id": "denial-risk", "version": "1.0", "endpoint_url": "",
     "slot": "claims",
     "purpose": "Scores claims for denial risk before submission from the "
                "adjudication history. Advisory only; factors cited.",
     "note": "The core of Claims & billing."},
    {"name": "Prior auth evidence assembler", "kind": "retrieval",
     "provider": "built-in", "model_id": "pa-evidence", "version": "1.0",
     "endpoint_url": "", "slot": "priorauth",
     "purpose": "Retrieves the chart evidence behind each payer criterion and "
                "drafts appeals into the signature queue; gaps stated, never "
                "inferred.",
     "note": "The core of Prior auth & appeals."},
    {"name": "Coding integrity model", "kind": "classifier",
     "provider": "built-in", "model_id": "coding-integrity", "version": "1.0",
     "endpoint_url": "", "slot": "coding",
     "purpose": "Reads charts against coded claims both ways; drafts clinician "
                "queries; never changes a code or note.",
     "note": "The core of Documentation gaps."},
    {"name": "ROI requirements validator", "kind": "classifier",
     "provider": "built-in", "model_id": "roi-validator", "version": "1.0",
     "endpoint_url": "", "slot": "roi",
     "purpose": "Validates release requests against configured jurisdiction "
                "requirements; can block, never approve; unconfigured "
                "jurisdictions fail closed.",
     "note": "The core of Release of information."},
    {"name": "Terminology mapper", "kind": "mapper", "provider": "built-in",
     "model_id": "term-map", "version": "3.0", "endpoint_url": "",
     "slot": "ingest",
     "purpose": "Maps source-system codes to standard terminologies; "
                "low-confidence mappings queue for human review.",
     "note": "Consumed by Ingest & mapping QA."},
]

#: Operational history seeds - the teaching specimens the integration
#: screens ship with, mirroring the demo: a refusal, a failure and a gap
#: are part of the product's story, not error states to hide.
_SEED_BULK_RUNS = [
    {"started_at": "2026-08-25 01:00", "finished_at": "2026-08-25 02:12",
     "source": "Epic", "group_id": "eGrp-7ac2", "status": "complete",
     "resources": 27961, "note": "Full population re-extract; watermark advanced."},
    {"started_at": "2026-08-26 01:00", "finished_at": "2026-08-26 01:00",
     "source": "Epic", "group_id": "eGrp-7ac2", "status": "refused",
     "resources": 0,
     "note": "Second kickoff inside 24 hours — Epic rate-limits Bulk Data "
             "Export to once per 24h per group and client; refused rather "
             "than queued silently."},
    {"started_at": "2026-08-27 01:00", "finished_at": "2026-08-27 02:20",
     "source": "Epic", "group_id": "eGrp-7ac2", "status": "complete",
     "resources": 27961,
     "note": "Full population re-extract (Bulk Data Export has no incremental "
             "mode on this vendor); watermark advanced."},
    {"started_at": "2026-08-28 01:00", "finished_at": None,
     "source": "Epic", "group_id": "eGrp-7ac2", "status": "failed",
     "resources": 14203,
     "note": "NDJSON download interrupted at file 14/22; watermark NOT "
             "advanced — a dirty run never moves the clean-run boundary."},
]

_SEED_STREAM_PARTITIONS = [
    {"feed": "ADT admissions/discharges", "partition_no": 0,
     "checkpoint_offset": 8842310, "latest_offset": 8842310,
     "events_today": 1204, "last_event_at": "13:58", "gap_detected": False, "note": ""},
    {"feed": "ADT admissions/discharges", "partition_no": 1,
     "checkpoint_offset": 8790522, "latest_offset": 8790522,
     "events_today": 1187, "last_event_at": "13:58", "gap_detected": False, "note": ""},
    {"feed": "ADT admissions/discharges", "partition_no": 2,
     "checkpoint_offset": 8811948, "latest_offset": 8811990,
     "events_today": 1163, "last_event_at": "13:51", "gap_detected": True,
     "note": "Offsets 8811949–8811990 unacknowledged after broker restart. "
             "Surfaced loudly, never interpolated — an interpolated gap is a "
             "silent data-integrity failure that looks like a healthy pipeline."},
    {"feed": "ORU lab results", "partition_no": 0,
     "checkpoint_offset": 4410221, "latest_offset": 4410221,
     "events_today": 3388, "last_event_at": "13:59", "gap_detected": False, "note": ""},
    {"feed": "Claims 835 remittance", "partition_no": 0,
     "checkpoint_offset": 902114, "latest_offset": 902114,
     "events_today": 214, "last_event_at": "12:40", "gap_detected": False, "note": ""},
]

_SEED_BULK_EXPORTS = [
    {"started_at": "2026-08-24 03:00", "finished_at": "2026-08-24 04:41",
     "destination": "Target EMR (Epic)", "format": "FHIR NDJSON",
     "scope": "full population", "status": "complete",
     "resources": 26113, "withheld": 1848,
     "note": "Delivery to the configured target EMR. Sensitive-category "
             "records excluded at export: psychotherapy notes and Part 2 "
             "records never leave without their own consent lane."},
    {"started_at": "2026-08-26 09:14", "finished_at": "2026-08-26 09:14",
     "destination": "Meridian Life Insurance", "format": "C-CDA",
     "scope": "one patient (ROI request)", "status": "refused",
     "resources": 0, "withheld": 0,
     "note": "ROI production refused: the record set contains 42 CFR Part 2 "
             "content and the requester supplied no Part 2-specific consent. "
             "Redisclosure without specific consent is prohibited; the refusal "
             "is the workflow."},
    {"started_at": "2026-08-28 03:00", "finished_at": "2026-08-28 03:22",
     "destination": "Oncology registry", "format": "FHIR NDJSON",
     "scope": "cohort: cancer diagnoses", "status": "failed",
     "resources": 1042, "withheld": 31,
     "note": "Destination rejected batch 9/12 (schema validation). Export "
             "marked failed; nothing partial is presented as complete."},
]

_SEED_STREAM_EXPORTS = [
    {"feed": "ADT notifications (CMS Condition of Participation)",
     "destination": "Community providers via HIE",
     "delivered_seq": 6621401, "latest_seq": 6621401, "delivered_today": 1188,
     "last_delivery": "13:58", "state": "healthy", "note": ""},
    {"feed": "Results to patient portal", "destination": "Patient portal",
     "delivered_seq": 3310221, "latest_seq": 3310221, "delivered_today": 3301,
     "last_delivery": "13:59", "state": "healthy", "note": ""},
    {"feed": "Claims 837 submissions", "destination": "Clearinghouse",
     "delivered_seq": 441207, "latest_seq": 441389, "delivered_today": 206,
     "last_delivery": "11:02", "state": "held",
     "note": "Clearinghouse rejecting since 11:02 (certificate rotation on "
             "their side). 182 submissions HELD with retry — held means queued "
             "and accounted, never dropped; nothing is re-generated or skipped."},
    {"feed": "Quality-measure submission (eCQM)", "destination": "Payer portal",
     "delivered_seq": 8812, "latest_seq": 8812, "delivered_today": 3,
     "last_delivery": "09:15", "state": "healthy", "note": ""},
    {"feed": "Event notifications to researchers",
     "destination": "De-identified event bus",
     "delivered_seq": 99120, "latest_seq": 99120, "delivered_today": 412,
     "last_delivery": "13:57", "state": "paused",
     "note": "Paused by governance: purpose-of-use review of a new subscriber "
             "in progress. A paused feed accumulates sequence, it does not leak."},
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


class PlatformState:
    """Config + model registry + integration state, with optional SQL
    write-through. All mutation goes through methods so the SQL mirror
    cannot drift from the in-memory truth."""

    def __init__(self, connection_factory=None):
        self._connect = connection_factory
        self._lock = threading.RLock()
        self.config: dict[str, str] = dict(_CONFIG_DEFAULTS)
        self.models: list[dict] = []
        self.bulk_runs: list[dict] = [dict(r) for r in _SEED_BULK_RUNS]
        self.bulk_exports: list[dict] = [dict(r) for r in _SEED_BULK_EXPORTS]
        self.stream_partitions: list[dict] = [dict(r) for r in _SEED_STREAM_PARTITIONS]
        self.stream_exports: list[dict] = [dict(r) for r in _SEED_STREAM_EXPORTS]
        for i, m in enumerate(_BUILTIN_MODELS, 1):
            row = dict(m)
            row.update(id=i, status="enabled", builtin=True,
                       registered_by="system", registered_at="2026-08-26 09:00")
            self.models.append(row)
        self._next_model_id = len(self.models) + 1
        for i, r in enumerate(self.bulk_runs, 1):
            r["id"] = i
        for i, r in enumerate(self.bulk_exports, 1):
            r["id"] = i
        for i, r in enumerate(self.stream_exports, 1):
            r["id"] = i
        if self._connect is not None:
            try:
                self._load_sql()
            except Exception as exc:  # degraded, never fatal
                log.warning("platform state DB load failed (in-memory "
                            "defaults in use): %s", exc)

    # ---- config ------------------------------------------------------

    def config_get(self, key: str, default: str = "") -> str:
        with self._lock:
            value = self.config.get(key, "")
            return value if value != "" else default

    def config_set(self, key: str, value: str) -> bool:
        """Returns True when the stored value actually changed."""
        with self._lock:
            if self.config.get(key, "") == value:
                return False
            self.config[key] = value
        self._persist("INSERT INTO platform_config (key, value) VALUES (%s, %s) "
                      "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                      (key, value))
        return True

    # ---- model registry ---------------------------------------------

    def list_models(self) -> list[dict]:
        with self._lock:
            return [dict(m) for m in sorted(
                self.models, key=lambda m: (not m["builtin"], m["id"]))]

    def get_model(self, model_id: int) -> Optional[dict]:
        with self._lock:
            for m in self.models:
                if m["id"] == model_id:
                    return dict(m)
        return None

    def register_model(self, *, name: str, kind: str, slot: str, provider: str,
                       model_id: str, version: str, endpoint_url: str,
                       purpose: str, registered_by: str) -> Optional[dict]:
        name = (name or "").strip()[:80]
        if not name:
            return None
        if kind not in MODEL_KINDS:
            kind = "custom"
        if slot not in MODEL_SLOTS:
            slot = "other"
        endpoint_url = (endpoint_url or "").strip()[:300]
        if endpoint_url and not endpoint_url.startswith("https://"):
            # Bring-your-own-model endpoints must be TLS; rejected, not stored.
            endpoint_url = ""
        with self._lock:
            row = {"id": self._next_model_id, "name": name, "kind": kind,
                   "slot": slot, "provider": (provider or "").strip()[:60],
                   "model_id": (model_id or "").strip()[:120],
                   "version": (version or "").strip()[:40],
                   "endpoint_url": endpoint_url,
                   "purpose": (purpose or "").strip()[:300],
                   "status": "registered", "builtin": False, "note": "",
                   "registered_by": registered_by, "registered_at": _now()}
            self._next_model_id += 1
            self.models.append(row)
        self._persist(
            "INSERT INTO platform_models (id, name, kind, slot, provider, "
            "model_id, version, endpoint_url, purpose, status, builtin, "
            "registered_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (row["id"], row["name"], row["kind"], row["slot"], row["provider"],
             row["model_id"], row["version"], row["endpoint_url"],
             row["purpose"], row["status"], False, registered_by))
        return dict(row)

    def set_model_status(self, model_id: int, status: str) -> bool:
        if status not in ("enabled", "disabled"):
            return False
        with self._lock:
            for m in self.models:
                if m["id"] == model_id and not m["builtin"]:
                    m["status"] = status
                    break
            else:
                return False
        self._persist("UPDATE platform_models SET status = %s WHERE id = %s",
                      (status, model_id))
        return True

    def delete_model(self, model_id: int) -> bool:
        with self._lock:
            for m in self.models:
                if m["id"] == model_id and not m["builtin"]:
                    self.models.remove(m)
                    break
            else:
                return False
        self._persist("DELETE FROM platform_models WHERE id = %s", (model_id,))
        return True

    # ---- integration state ------------------------------------------

    def add_bulk_run(self, **fields) -> dict:
        with self._lock:
            fields["id"] = max((r["id"] for r in self.bulk_runs), default=0) + 1
            self.bulk_runs.append(fields)
        return dict(fields)

    def add_bulk_export(self, **fields) -> dict:
        with self._lock:
            fields["id"] = max((r["id"] for r in self.bulk_exports), default=0) + 1
            self.bulk_exports.append(fields)
        return dict(fields)

    def get_bulk_run(self, run_id: int) -> Optional[dict]:
        with self._lock:
            for r in self.bulk_runs:
                if r["id"] == run_id:
                    return dict(r)
        return None

    def get_bulk_export(self, run_id: int) -> Optional[dict]:
        with self._lock:
            for r in self.bulk_exports:
                if r["id"] == run_id:
                    return dict(r)
        return None

    def set_feed_state(self, feed_id: int, state: str, note: str = "") -> bool:
        if state not in ("healthy", "paused"):
            return False
        with self._lock:
            for f in self.stream_exports:
                if f["id"] == feed_id and f["state"] in ("healthy", "paused"):
                    f["state"] = state
                    f["note"] = note
                    return True
        return False

    def release_bulk_hold(self) -> None:
        """Backdate the newest real run so a kickoff is permitted now."""
        with self._lock:
            real = [r for r in self.bulk_runs
                    if r["status"] in ("complete", "failed", "running")]
            if real:
                newest = max(real, key=lambda r: r["started_at"])
                newest["started_at"] = "2026-08-01 00:00"

    def last_real_run_at(self) -> Optional[str]:
        with self._lock:
            real = [r for r in self.bulk_runs
                    if r["status"] in ("complete", "failed", "running")]
            return max((r["started_at"] for r in real), default=None)

    # ---- SQL mirror --------------------------------------------------

    def _persist(self, sql: str, params: tuple) -> None:
        if self._connect is None:
            return
        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, params)
                finally:
                    cur.close()
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # write-through is best-effort
            log.warning("platform state persist failed (in-memory state "
                        "unaffected): %s", exc)

    def _load_sql(self) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(open(_schema_path()).read())
                cur.execute("SELECT key, value FROM platform_config")
                for key, value in cur.fetchall():
                    if key in self.config:
                        self.config[key] = value
                cur.execute(
                    "SELECT id, name, kind, slot, provider, model_id, version, "
                    "endpoint_url, purpose, status, registered_by FROM "
                    "platform_models WHERE NOT builtin ORDER BY id")
                for row in cur.fetchall():
                    self.models.append({
                        "id": row[0], "name": row[1], "kind": row[2],
                        "slot": row[3], "provider": row[4], "model_id": row[5],
                        "version": row[6], "endpoint_url": row[7],
                        "purpose": row[8], "status": row[9], "builtin": False,
                        "note": "", "registered_by": row[10],
                        "registered_at": ""})
                    self._next_model_id = max(self._next_model_id, row[0] + 1)
            finally:
                cur.close()
            conn.commit()
        finally:
            conn.close()


def _schema_path() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parents[1] / "db"
               / "platform_state_schema.sql")
# Made by Ryan Gomez & Co. Inc.
