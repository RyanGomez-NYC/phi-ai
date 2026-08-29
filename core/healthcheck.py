# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Healthcheck.

    python -m core.healthcheck

Deliberately checks compliance posture, not just "can I reach the
bucket". A deployment that can write PHI to a bucket with public access
unblocked, or without a organization-managed KMS key, is *working* but not
*compliant*, and that's the failure mode worth catching automatically -
it's silent otherwise.

Exits non-zero on any FAIL. Used as the container healthcheck and as the
post-install verification step in runbooks/RUNBOOK_INSTALL.md.
"""

from __future__ import annotations

import sys

from core.config.settings import ConfigError, Settings

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


class Check:
    def __init__(self):
        self.results: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.results.append((status, name, detail))

    def report(self) -> int:
        width = max(len(n) for _, n, _ in self.results)
        for status, name, detail in self.results:
            line = f"[{status:4}] {name.ljust(width)}"
            if detail:
                line += f"  {detail}"
            print(line)

        failures = sum(1 for s, _, _ in self.results if s == FAIL)
        warnings = sum(1 for s, _, _ in self.results if s == WARN)
        print()
        print(f"{len(self.results)} checks, {failures} failed, {warnings} warnings")
        return 1 if failures else 0


def main() -> int:
    check = Check()

    # --- Config ---
    try:
        settings = Settings.from_env()
        check.add(PASS, "config.load", f"provider={settings.cloud_provider}")
    except ConfigError as exc:
        check.add(FAIL, "config.load", str(exc))
        return check.report()

    # --- AI assistant (optional add-on) ---
    #
    # Checked BEFORE the provider gate below, deliberately: this is the
    # one component whose posture is identical on all three clouds, and
    # it is the one that opens a network path off the deployment. A GCP
    # or Azure operator should not have to wait for per-cloud checks to
    # exist before they can confirm where their model traffic goes.
    from core.assistant import assistant_enabled

    if not assistant_enabled():
        check.add(PASS, "assistant.enabled", "off (the default)")
    else:
        from core.assistant.config import AssistantConfigError, settings_from_env

        try:
            assistant = settings_from_env()
        except AssistantConfigError as exc:
            check.add(FAIL, "assistant.config", str(exc).splitlines()[0])
            assistant = None

        if assistant is not None:
            if assistant.stays_in_org_cloud:
                check.add(
                    PASS,
                    "assistant.egress",
                    f"{assistant.resolved_model} via {assistant.provider} - model runs "
                    "inside your own cloud account",
                )
            else:
                # WARN, not FAIL. Using the Anthropic API directly is a
                # legitimate choice and the only one available on Azure -
                # but it is a third-party egress path from a PHI system,
                # and an operator reading a health check should see it
                # named rather than inferring it from a variable.
                check.add(
                    WARN,
                    "assistant.egress",
                    f"{assistant.resolved_model} via the Anthropic API - requests leave "
                    "this deployment's cloud account (no PHI is sent; see "
                    "runbooks/RUNBOOK_AI_ASSISTANT.md)",
                )

            # The PHI tier is posture, not a detail. An operator reading
            # a health check should be able to see whether this
            # deployment lets the assistant read records without going
            # to look at .env.
            if not assistant.reads_clinical_content:
                check.add(PASS, "assistant.phi", "no access to clinical content")
            else:
                check.add(
                    WARN,
                    "assistant.phi",
                    f"tier '{assistant.phi_access}' - the assistant CAN read records "
                    "(permission-gated, and every read audited as a disclosure)",
                )

            # An empty corpus is the realistic assistant failure: the
            # thing still answers, it just stops being grounded in this
            # project's documentation, which is silent unless checked.
            from core.assistant import knowledge

            corpus = knowledge.load()
            if corpus.is_empty():
                check.add(
                    FAIL,
                    "assistant.knowledge",
                    "no documentation found - answers would not be grounded in this "
                    "project's runbooks. Check docs/ and runbooks/ are present, or set "
                    "PHI_AI_ASSISTANT_DOCS_ROOT",
                )
            else:
                check.add(
                    PASS,
                    "assistant.knowledge",
                    f"{len(corpus.documents)} documents indexed",
                )

    if settings.cloud_provider != "aws":
        # FOUND AND FIXED (2026-08-17 audit, MEDIUM): this was previously
        # a WARN, not a FAIL - and Check.report() only fails the run
        # (nonzero exit) when at least one FAIL is present; a WARN-only
        # run reports "0 failed" and exits 0, i.e. success. Proven live:
        # a Check with exactly one PASS ("config.load") and one WARN
        # ("config.provider") reports "0 failed" and returns exit code
        # 0. That is a real, silent problem, not a hypothetical one:
        # this script is both the Docker `healthcheck:` command for the
        # `app` service (docker-compose.yml) and the explicit
        # post-install verification step runbooks/RUNBOOK_INSTALL.md
        # tells every operator to run - on GCP or Azure, both of those
        # would report "healthy" / a clean exit code having performed
        # *zero* of the bucket/KMS/role-separation checks below, on a
        # cloud where none of those checks are implemented yet at all.
        # RUNBOOK_AZURE_SETUP.md's own Step 8 and "Known gaps" section
        # both asserted this "fails honestly (a WARN, not a silent
        # false-PASS)" - true of the human-readable output line, false
        # of the actual exit code a monitor or CI step would observe,
        # which is the only thing an automated health check or
        # post-install verification gate actually looks at. Changed to
        # FAIL so the exit code matches what both runbooks already
        # claimed it did: a deployer/monitor on GCP or Azure now sees a
        # genuine failure, not a quiet 0, until real per-cloud
        # compliance checks are implemented (tracked as a fast-follow in
        # both cloud setup runbooks' own Known gaps sections - this fix
        # makes the interim behavior honest, it does not implement those
        # checks).
        check.add(FAIL, "config.provider", f"{settings.cloud_provider} healthchecks not implemented yet")
        return check.report()

    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3", region_name=settings.storage_region)
    kms = boto3.client("kms", region_name=settings.storage_region)

    # --- Identity ---
    try:
        ident = boto3.client("sts", region_name=settings.storage_region).get_caller_identity()
        check.add(PASS, "aws.identity", ident["Arn"])
    except Exception as exc:
        check.add(FAIL, "aws.identity", str(exc))
        return check.report()

    # --- Buckets ---
    for label, bucket in (("store", settings.storage_bucket), ("audit", settings.audit_bucket)):
        try:
            s3.head_bucket(Bucket=bucket)
            check.add(PASS, f"s3.{label}.reachable", bucket)
        except ClientError as exc:
            check.add(FAIL, f"s3.{label}.reachable", f"{bucket}: {exc}")
            continue

        # Versioning
        try:
            ver = s3.get_bucket_versioning(Bucket=bucket).get("Status")
            if ver == "Enabled":
                check.add(PASS, f"s3.{label}.versioning", "enabled")
            else:
                check.add(FAIL, f"s3.{label}.versioning", f"status={ver!r}; required for integrity controls")
        except ClientError as exc:
            check.add(WARN, f"s3.{label}.versioning", f"could not read: {exc}")

        # Object Lock - expected to be ABSENT.
        #
        # This inverts what this check used to assert. The stack no
        # longer provisions Object Lock, and no write path sends
        # ObjectLockRetainUntilDate, so a bucket that still carries a
        # default retention rule will silently lock every object it
        # receives at the bucket default - permanently, in COMPLIANCE
        # mode. A leftover lock is now a hazard, not a reassurance.
        try:
            lock = s3.get_object_lock_configuration(Bucket=bucket)
            rule = lock["ObjectLockConfiguration"].get("Rule", {}).get("DefaultRetention", {})
            mode = rule.get("Mode")
            days = rule.get("Days") or (rule.get("Years", 0) * 365)

            if mode == "COMPLIANCE":
                check.add(
                    FAIL,
                    f"s3.{label}.object_lock",
                    f"COMPLIANCE/{days}d default retention is ACTIVE on a stack that no "
                    "longer expects Object Lock. Every object written here is locked "
                    "irreversibly for that period, including against root. This bucket "
                    "cannot be un-locked; it must be replaced.",
                )
            elif mode:
                check.add(
                    WARN,
                    f"s3.{label}.object_lock",
                    f"{mode}/{days}d default retention is active but unexpected - Object "
                    "Lock was removed from this design. Objects written here are being "
                    "locked at the bucket default.",
                )
            else:
                check.add(
                    WARN,
                    f"s3.{label}.object_lock",
                    "Object Lock is enabled with no default retention rule. Not expected "
                    "on this stack; the bucket-level flag is permanent and cannot be "
                    "removed, so only a replacement bucket clears it.",
                )
        except ClientError as exc:
            if "ObjectLockConfigurationNotFoundError" in str(exc):
                check.add(PASS, f"s3.{label}.object_lock", "not enabled (expected)")
            else:
                check.add(WARN, f"s3.{label}.object_lock", f"could not read: {exc}")

        # Retention posture, stated so it cannot be mistaken for a control.
        check.add(
            WARN,
            f"s3.{label}.retention_enforcement",
            f"NONE - retention ({settings.retention_years}y declared) is recorded as "
            "object metadata only. Any principal holding s3:DeleteObject can delete "
            "stored data at any point inside that period. Integrity relies on "
            "versioning, SHA-256 digests, the audit hash chain, and CloudTrail.",
        )

        # Encryption
        try:
            enc = s3.get_bucket_encryption(Bucket=bucket)
            rules = enc["ServerSideEncryptionConfiguration"]["Rules"]
            alg = rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
            if alg == "aws:kms":
                check.add(PASS, f"s3.{label}.encryption", "SSE-KMS with CMK")
            else:
                check.add(FAIL, f"s3.{label}.encryption", f"{alg} - organization-managed KMS key required")
        except ClientError as exc:
            check.add(FAIL, f"s3.{label}.encryption", f"could not read: {exc}")

        # Public access
        try:
            pab = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
            if all(pab.values()):
                check.add(PASS, f"s3.{label}.public_access", "fully blocked")
            else:
                check.add(FAIL, f"s3.{label}.public_access", f"incomplete: {pab}")
        except ClientError as exc:
            check.add(FAIL, f"s3.{label}.public_access", f"not configured: {exc}")

    # --- KMS ---
    for label, key_id in (("store", settings.kms_key_id), ("audit", settings.audit_kms_key_id)):
        try:
            meta = kms.describe_key(KeyId=key_id)["KeyMetadata"]
            if meta["KeyState"] != "Enabled":
                check.add(FAIL, f"kms.{label}.state", meta["KeyState"])
            else:
                check.add(PASS, f"kms.{label}.state", "enabled")

            if meta.get("KeyManager") != "CUSTOMER":
                check.add(FAIL, f"kms.{label}.managed_by", "AWS-managed key; a organization-managed CMK is required")
            else:
                check.add(PASS, f"kms.{label}.managed_by", "organization-managed")

            rot = kms.get_key_rotation_status(KeyId=key_id)
            if rot.get("KeyRotationEnabled"):
                check.add(PASS, f"kms.{label}.rotation", "enabled")
            else:
                check.add(WARN, f"kms.{label}.rotation", "automatic rotation is off")
        except ClientError as exc:
            check.add(FAIL, f"kms.{label}.describe", str(exc))

    # --- Role separation ---
    # Distinct keys are what make the ingest role's lack of kms:Decrypt
    # meaningful. If the object store and audit key are the same key, the
    # ingest role holds Decrypt on it (it needs that to read the audit
    # chain tip), so the separation silently does not exist. Detect that
    # explicitly rather than letting the probe below report a misleading
    # PASS.
    if settings.kms_key_id == settings.audit_kms_key_id:
        check.add(
            WARN,
            "iam.key_separation",
            "the object store and audit share one KMS key - ingest can decrypt PHI; "
            "role separation is OFF (dev cost config, not valid for real PHI)",
        )
    else:
        check.add(PASS, "iam.key_separation", "the object store and audit use distinct KMS keys")

    # The ingest role must NOT be able to decrypt stored PHI. If this
    # check passes when running as the ingest role, role separation has
    # been broken by a policy change.
    try:
        kms.decrypt(CiphertextBlob=b"\x00" * 32, KeyId=settings.kms_key_id)
        check.add(WARN, "iam.role_separation", "this identity CAN call kms:Decrypt on the object store key")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "AccessDeniedException":
            check.add(PASS, "iam.role_separation", "kms:Decrypt denied (expected for ingest role)")
        else:
            # InvalidCiphertextException means we DO have decrypt permission,
            # we just sent garbage - which is the informative outcome here.
            check.add(WARN, "iam.role_separation", f"this identity CAN call kms:Decrypt ({code})")

    # OCR document ingestion (core/ocr/). Checked here specifically
    # because the failure mode is invisible until someone tries to ingest
    # a document: pytesseract and pdf2image are pure-Python wrappers that
    # import cleanly whether or not the native binaries they call exist,
    # so a deployment missing tesseract or poppler looks completely
    # healthy right up to the moment a scanned record fails to store.
    #
    # WARN, not FAIL, when unavailable: document ingestion is an optional
    # capability, and a deployment doing FHIR ingestion only is properly
    # functional without it. A deployment that intends to ingest scanned
    # documents should treat this warning as a failure.
    try:
        from core.ocr.tesseract import TesseractOCR

        engine = TesseractOCR()
        try:
            check.add(PASS, "ocr.tesseract", f"version {engine.version()}")
        except Exception as exc:
            check.add(
                WARN,
                "ocr.tesseract",
                f"unavailable - document ingestion will fail: {exc}",
            )
            raise _OCRUnavailable

        try:
            import pdf2image  # noqa: F401

            from pdf2image.exceptions import PDFInfoNotInstalledError

            try:
                pdf2image.pdfinfo_from_bytes(b"%PDF-1.4\n%%EOF\n")
            except PDFInfoNotInstalledError:
                check.add(
                    WARN,
                    "ocr.poppler",
                    "poppler-utils is missing - images will OCR but PDFs will not "
                    "(apt-get install poppler-utils)",
                )
            except Exception:
                # Any other error means poppler ran and rejected the stub
                # PDF, which is the outcome that proves it is installed.
                check.add(PASS, "ocr.poppler", "installed (PDF rasterisation available)")
        except ImportError:
            check.add(WARN, "ocr.poppler", "pdf2image not installed - PDFs cannot be OCR'd")
    except _OCRUnavailable:
        pass
    except ImportError:
        check.add(WARN, "ocr.tesseract", "core.ocr is not installed in this image")

    return check.report()


class _OCRUnavailable(Exception):
    """Internal control-flow marker: skip the poppler probe when the OCR
    engine itself is already known to be missing, so one missing package
    produces one actionable finding rather than two."""


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
