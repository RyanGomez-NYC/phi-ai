# Runbook: Retrieving stored records from the object store

Typical trigger: a records request, legal hold, litigation discovery, or
a patient right-of-access request under §164.524.

## 1. Authorize the request

- [ ] Confirm the request is authorized under your organization's
      policies (patient right of access, court order, internal legal
      hold, etc.) *before* performing any retrieval — this is where
      "minimum necessary" is enforced operationally, not just in code.
- [ ] Determine the specific resource types/date range needed. Avoid
      broad exports where a narrower query satisfies the request.

## 2. Retrieve

```
docker compose exec app python -m core.fhir.restore \
  --patient-id <id> \
  --purpose-of-use "<documented reason>" \
  --role-arn arn:aws:iam::<account>:role/phi-ai-<env>-restore \
  --output ./restore-output/
```

`--resource-type <type>` is optional — add it to restrict the restore to
one FHIR resource type; omit it to retrieve everything stored for this
patient. `--role-arn` is your deployment's restore role
(`terraform output restore_role_arn` in `deploy/aws/`) — this requires
an MFA-verified AWS session already in place, the same as any other use
of that role.

The role name is built from this stack's Terraform `name_prefix`, so a
deployment that changed that variable has a correspondingly different
ARN. Take it from `terraform output restore_role_arn` rather than
typing the form above from memory; that output is always correct for
the stack in front of you.

Bucket, region, and Postgres connection details come from the same
`PHI_AI_*` environment the rest of this deployment already uses
(`.env`), not separate flags — this tool can never point at a different
object store than what's actually configured.

Every retrieval is audit-logged automatically as `record.read`,
including the `purpose_of_use` you provide — this field is required and
appears in the breach-scoping audit trail described in
`RUNBOOK_INCIDENT_RESPONSE.md` Step 2, so keep it specific and accurate.
A vague purpose of use is not a cosmetic problem: it is what an
accounting of disclosures under §164.528 has to show, and "records
request" answers none of the questions that accounting exists to answer.

## 3. Verify integrity

Before decrypting anything, the restore command recomputes the SHA-256
digest of the stored ciphertext and compares it against the digest
recorded when the object was stored — a check on the encrypted bytes
themselves, done before any attempt to decrypt them, not a check of the
decrypted output afterward. If verification fails, **stop** — this
indicates possible tampering or corruption and should be escalated per
`RUNBOOK_INCIDENT_RESPONSE.md`, not silently retried. The tool stops at
the first failure rather than skipping the bad object and continuing to
the rest of the request; anything already restored before that point is
unaffected and does not need to be treated as suspect.

## 4. Handle the output

- [ ] Deliver via your organization's existing secure channel (patient
      portal, encrypted email, etc.) — the platform's job ends at
      producing verified plaintext locally; it does not include a
      delivery mechanism by design (delivery channels vary too much by
      organization and by request type to standardize safely).
- [ ] Delete the local `./restore-output/` plaintext copy once delivered,
      per your data handling policy. Ciphertext in the object store is
      unaffected either way.

## 5. Record

- [ ] Log the fulfillment (not just the retrieval) in your organization's
      records-request tracking system, separate from the platform's audit
      log, since "the data was retrieved" and "the request was fulfilled
      appropriately" are different facts.
