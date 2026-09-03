# Runbook: Suspected security incident / breach

This runbook covers the operational steps for the PHI AI Platform
specifically. It does not replace your organization's HIPAA-required
breach notification policy (45 CFR §164.400–414) or your incident
response plan — coordinate with your Privacy/Security Officer
immediately.

## 1. Contain

- [ ] Revoke the suspected-compromised credential at the source. What
      that means depends on the EMR connection's auth model
      (`core/fhir/emr_profiles.py`): for the SMART Backend Services
      vendors (every profile whose `auth_flow` is
      `smart_backend_services` — Epic, Oracle Health and the rest)
      there is no client secret to revoke — auth is a signed JWT client
      assertion (RFC 7523), so containment means generating a new
      keypair (RSA via `scripts/generate_epic_keypair.sh`, or EC P-384
      for the ES384 vendors ModMed and Greenway — each chapter's
      "Setting it up" in `docs/EMR_CONNECTORS.md` has the commands),
      registering the new public key on the client ID at the vendor's
      console (fhir.epic.com for Epic; System Account Management for
      Oracle Health; for the vendors that fetch a hosted JWKS URL,
      publishing the new key at that URL and **removing the old one** —
      Altera and Veradigm pick the change up in a nightly job, so the
      compromised key keeps working until it is gone from the file),
      and updating `PHI_AI_FHIR_PRIVATE_KEY_PATH`. An athenahealth
      deployment
      DOES hold a client secret — revoke and reissue it through the
      athenahealth portal. For a compromised cloud IAM credential or KMS
      access, revoke/rotate at the cloud IAM console. Either way — do
      not just stop the container; the credential itself needs to stop
      working.
- [ ] If a KMS key may be compromised, disable it (do not delete it —
      you need it to read the audit trail and, forensics permitting,
      re-encrypt with a new key afterward).
- [ ] Isolate the host: `docker compose stop scheduler bulk-scheduler`
      (storage stays intact; this only stops further ingestion calls —
      `app` itself runs no scheduled process and can stay up for the
      investigation steps below).

## 2. Assess scope using the audit trail

- [ ] Export the audit log for the affected time window.
- [ ] Verify chain integrity first — a broken chain is itself a finding.
      Audit objects are keyed `audit/YYYY/MM/DD/...`, so restrict to a
      window with `--prefix` (verifying a partial range confirms
      internal linkage within it, not that it correctly connects to
      events outside that range — see the tool's own `--help`):
      ```bash
      python -m core.audit.verify --prefix audit/2026/08/
      ```
      A `Note: N fork point(s) detected ...` line in the output is
      **not** itself an incident — it means the incremental scheduler
      and the bulk-export scheduler (or two replicas of either) resumed
      from the same chain tip and wrote near-simultaneously, which the
      verifier now tolerates by design (fixed 2026-08-17 audit MEDIUM,
      "audit chain forks under concurrent writers" — see
      `core/audit/log.py`'s `AuditLog.diagnose_chain()` docstring for
      the full reasoning). Only a `RESULT: CHAIN BROKEN` line — driven
      by an event whose own content doesn't match its recorded hash, or
      whose predecessor is missing from the read — is a real finding
      worth pursuing through the rest of this runbook.
- [ ] Enumerate every `record.read` / `record.export` event by the
      compromised actor to determine which resource keys (and therefore
      which patients) were accessed. This list is what scopes your
      breach notification obligation, so an under-inclusive filter here
      produces an under-inclusive notification.

      Over an exported log:
      ```bash
      grep -E '"action": *"record\.(read|export)"' exported-audit.jsonl
      ```
      Then narrow to the compromised actor and the incident window
      before treating the result as the scope. Read access is not the
      only disclosure worth enumerating: if the actor could reach the
      release-of-information or delivery paths, include their action
      types in the same pass rather than running the step twice.
- [ ] Cross-reference with cloud-provider-level access logs (CloudTrail /
      Cloud Audit Logs / Azure Activity Log) for the storage bucket and
      KMS key — the application audit log and the cloud-native log
      should agree; a mismatch is itself evidence of out-of-band access.
      On AWS, `cloudtrail:LookupEvents` (granted to the `auditor` role)
      only returns KMS management-event activity (Decrypt/GenerateDataKey
      calls) — it does NOT return S3 object-level events
      (GetObject/PutObject/DeleteObject), which is what actually scopes
      "which resource keys were touched outside the application." For
      that, the `auditor` role now also holds read access to the
      CloudTrail bucket itself (`deploy/aws/iam.tf`'s
      `ReadCloudTrailLogFiles`, fixed 2026-08-17 audit MEDIUM
      "CloudTrail cross-check limitations") — read the delivered log
      files directly rather than relying on LookupEvents for this step.
      See `runbooks/RUNBOOK_INDEX_MAINTENANCE.md` Step 3 for the exact
      procedure (listing/downloading/`jq`-filtering the gzipped JSON log
      files by S3 key); the same approach applies here, just filtered by
      time window and principal instead of a single key.

## 3. Determine notification obligations

Work with your Privacy/Security Officer and counsel to determine, per
§164.400–414 and applicable state law (see `docs/COMPLIANCE.md`):
- Whether this constitutes a reportable breach (vs. a permitted
  disclosure or a secured/encrypted-data exception).
- Notification deadlines (HIPAA: without unreasonable delay, ≤60 days;
  several states require faster).
- Whether HHS OCR, state AG, and/or affected individuals must be notified.

## 4. Remediate

- [ ] Rotate all credentials: a new EMR keypair (see Step 1 — for the
      SMART Backend Services vendors there is no client secret to
      rotate, only the private key and its registered public
      counterpart; athenahealth's client secret is the exception, as is
      any deployment that chose a documented secret grant — TruBridge,
      Netsmart),
      cloud IAM keys, KMS key (create
      new, re-encrypt going forward — do not re-encrypt already-stored
      objects. Nothing blocks that any more now that Object Lock is gone,
      which makes it a discipline question rather than a technical one:
      re-encryption of at-rest data is a deliberate, planned migration
      with its own integrity verification, not an incident-response step,
      and rewriting stored objects mid-incident destroys the version
      history you are still investigating).
- [ ] Patch/redeploy the application if the compromise vector was a
      software vulnerability.
- [ ] Document root cause and corrective action for the Security Risk
      Assessment record.

## 5. Post-incident

- [ ] Update the risk assessment.
- [ ] Confirm audit log retention itself is long enough to have covered
      the investigation window (if not, that's a finding for future
      configuration).
