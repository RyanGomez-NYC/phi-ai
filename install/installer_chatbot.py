#!/usr/bin/env python3
# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
PHI AI Platform guided installer chatbot.

A deliberately rule-based (not LLM-backed) conversational installer: for
software that will touch PHI, predictable and auditable install-time
behavior matters more than natural-language flexibility. It walks an
operator through cloud selection, infrastructure prerequisites, EMR
connection details, and retention policy, then writes a .env file that
install.sh consumes.

Run: python3 install/installer_chatbot.py

FOUND AND FIXED (2026-08-17 audit, H8). This script previously
OVERWROTE .env with `write_text()` of only the ~14 variables it itself
collects - but the documented flow (README.md's Quick start,
runbooks/RUNBOOK_AWS_SETUP.md Steps 4 and 6) is:

    terraform output -raw env_fragment > .env      # Step 4 - writes
                                                     # PHI_AI_DB_*,
                                                     # PHI_AI_PSYCHOTHERAPY_*,
                                                     # storage/KMS values
    python3 install/installer_chatbot.py            # Step 6 - "add EMR
                                                     # connection details"

RUNBOOK_AWS_SETUP.md Step 6a then explicitly promises "the five
PHI_AI_DB_* lines already in .env from Step 4's env_fragment are
all that's needed" - a promise the code could never keep: the only two
outcomes of running this script against an existing .env were (a) abort
entirely without writing anything (if the operator declined to
overwrite), or (b) silently discard every line env_fragment wrote -
including the five PHI_AI_DB_* lines and any
PHI_AI_PSYCHOTHERAPY_* lines - the moment the operator confirmed
"Yes." There was no third path that actually appended or merged,
despite both the runbook and this project's own README describing the
flow as if there were. The practical consequence: an operator following
the documented Quick start exactly loses their Postgres index
configuration and their psychotherapy-notes bucket/key configuration
silently - core/config/settings.py's db_target_configured() then
correctly, but silently, reports False, and core/fhir/client.py's
psychotherapy path fails loudly ONLY IF a psychotherapy resource is
ever actually stored (see that function's own docstring) - so a
deployer who never happens to store one would never find out their
psychotherapy configuration went missing at all.

Fixed with genuine merge semantics: _load_existing_env() below parses
any existing .env into a dict, this run's newly-collected values
overlay it (so re-running the installer to fix a typo updates that
field in place, as expected), and anything this run doesn't ask about -
PHI_AI_DB_*, PHI_AI_PSYCHOTHERAPY_*, PHI_AI_FHIR_GROUP_ID,
PHI_AI_RETENTION_RULESET_PATH, or any other line a human or
Terraform previously put there - passes through untouched. No more
destructive "Overwrite?" prompt: the operator is told plainly what will
happen (their answers update matching fields; everything else is kept)
rather than forced into an all-or-nothing choice.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# The vendor table is the ONE source of truth for what is integrated
# (core/fhir/emr_profiles.py) - imported rather than duplicated here, so
# a vendor added or corrected there shows up in this installer without
# anyone remembering to copy it. The sys.path insert exists because the
# documented invocation (`python3 install/installer_chatbot.py`) puts
# install/ on the path, not the repo root; emr_profiles imports nothing
# beyond the standard library, so this works before any dependencies are
# installed - the same "usable from step one" property the assistant's
# own terminal mode advertises.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.fhir.emr_profiles import PROFILES  # noqa: E402

BANNER = r"""
  ____  _   _ ___    _      ___
 |  _ \| | | |_ _|  / \    |_ _|
 | |_) | |_| || |  / _ \    | |
 |  __/|  _  || | / ___ \   | |
 |_|   |_| |_|___/_/   \_\ |___|

 Guided installer
"""


def ask(
    prompt: str,
    default: str | None = None,
    validator=None,
    secret: bool = False,
    optional: bool = False,
) -> str:
    """
    Prompt for a value. `optional=True` allows an empty response (returns
    ""), distinct from `default`, which substitutes a value but still
    requires the field to end up non-empty. Without this distinction, a
    field that's optional-with-no-default has no way to say "blank is a
    valid answer" - conflating the two makes it loop forever waiting for
    input the caller explicitly said wasn't required.
    """
    suffix = f" [{default}]" if default else " (optional)" if optional else ""
    while True:
        if secret:
            import getpass
            val = getpass.getpass(f"{prompt}{suffix}: ").strip()
        else:
            val = input(f"{prompt}{suffix}: ").strip()
        if not val and default is not None:
            val = default
        if not val and optional:
            return ""
        if not val:
            print("  This value is required.")
            continue
        if validator and not validator(val):
            continue
        return val


def ask_choice(prompt: str, choices: list[str]) -> str:
    print(prompt)
    for i, c in enumerate(choices, 1):
        print(f"  {i}) {c}")
    while True:
        raw = input(f"Choose 1-{len(choices)}: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        print("  Invalid choice, try again.")


def validate_url(url: str) -> bool:
    if not re.match(r"^https://", url):
        print("  URL must start with https:// — PHI AI Platform requires TLS for all endpoints.")
        return False
    return True


def validate_int(val: str) -> bool:
    if not val.isdigit():
        print("  Must be a whole number.")
        return False
    return True


def _load_existing_env(path: Path) -> dict[str, str]:
    """
    Parses an existing .env file into {key: value} - only simple
    KEY=VALUE lines, matching exactly what both this script and
    `terraform output -raw env_fragment` ever produce (see each cloud's
    own outputs.tf). Blank lines and comments are not preserved
    verbatim; this is a deliberate, stated scope limit (H8 fix), not a
    silent gap: the goal is to never lose a VALUE Terraform or a prior
    installer run wrote, not to byte-for-byte round-trip hand-written
    formatting/comments in a .env a human has been editing directly. A
    value survives even if its original comment doesn't.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        values[key.strip()] = val
    return values


def main() -> None:
    print(BANNER)
    print(
        "This will collect the information needed to deploy the PHI AI Platform into "
        "your own cloud environment and write a .env file for install.sh.\n"
        "It does NOT create any cloud resources itself — provision infrastructure "
        "first with the Terraform in deploy/<cloud>/, per runbooks/RUNBOOK_AWS_SETUP.md,\n"
        "RUNBOOK_GCP_SETUP.md, or RUNBOOK_AZURE_SETUP.md (all three clouds have a "
        "complete Terraform stack - see README.md's Status section).\n"
    )

    if not ask_choice(
        "Have you already provisioned the storage bucket, KMS key, and IAM role "
        "described in your cloud's setup runbook (RUNBOOK_AWS_SETUP.md / "
        "RUNBOOK_GCP_SETUP.md / RUNBOOK_AZURE_SETUP.md)?",
        ["Yes, continue", "No, exit and do that first"],
    ).startswith("Yes"):
        print("\nNo problem — run this again once infrastructure is provisioned.")
        sys.exit(0)

    provider = ask_choice("Which cloud provider are you deploying to?", ["aws", "gcp", "azure"])

    print(f"\n-- {provider.upper()} storage settings --")
    print(
        f"If you deployed with the Terraform in deploy/{provider}/, you already have\n"
        "these in .env from `terraform output -raw env_fragment > .env` - re-entering\n"
        "the same values here is safe and just confirms/updates them in place; anything\n"
        "else already in .env (e.g. PHI_AI_DB_*, PHI_AI_PSYCHOTHERAPY_*) is left\n"
        "untouched, not discarded.\n"
    )
    bucket = ask("PHI storage bucket/container name")
    region = ask("Region", default="us-east-1" if provider == "aws" else None)
    kms_key = ask("KMS key ID/ARN for the storage key")
    audit_bucket = ask("Audit log bucket name")
    audit_kms_key = ask("KMS key ID/ARN for the audit key")

    # Provider-specific fields the storage/KMS backend classes actually
    # need at runtime (core/storage/factory.py) but that the generic
    # bucket/region/key questions above don't cover.
    gcp_project = ""
    azure_account_url = ""
    azure_vault_url = ""
    if provider == "gcp":
        gcp_project = ask("GCP project ID (for GCS and Cloud KMS)")
    elif provider == "azure":
        azure_account_url = ask(
            "Azure Storage account URL (e.g. https://myaccount.blob.core.windows.net)",
            validator=validate_url,
        )
        azure_vault_url = ask(
            "Azure Key Vault URL (e.g. https://myvault.vault.azure.net)",
            validator=validate_url,
        )

    print("\n-- Source EMR --")
    print(
        "Six EMR vendors are integrated, each with its own capability profile\n"
        "(core/fhir/emr_profiles.py) recording its real auth model, resource\n"
        "surface and bulk-export support - see docs/EMR_CONNECTORS.md for the\n"
        "per-vendor registration walk-through. Pick the vendor this deployment's\n"
        "source EMR runs; registration is per customer/practice for every one of\n"
        "them, so have your registration details from that walk-through at hand.\n"
    )
    # Epic first (the deepest integration and the common case), the rest
    # in stable alphabetical order. Labels come from the profile table so
    # this list can never disagree with what the platform accepts.
    vendor_keys = ["epic"] + sorted(k for k in PROFILES if k != "epic")
    label_to_key = {f"{PROFILES[k].name}  [{k}]": k for k in vendor_keys}
    emr_vendor = label_to_key[
        ask_choice("Which EMR vendor is the source system?", list(label_to_key))
    ]
    profile = PROFILES[emr_vendor]
    uses_client_secret = profile.auth_flow == "oauth2_client_credentials"

    print(f"\n-- {profile.name} FHIR connection --")
    if emr_vendor == "epic":
        print(
            "Epic backend services does NOT use a client secret - it uses an RS384\n"
            "JWT signed with a private key whose public half is registered on\n"
            "open.epic.com. If you haven't generated that keypair yet, run this in\n"
            "another terminal first, then come back:\n"
            "    ./scripts/generate_epic_keypair.sh\n"
        )
        fhir_base_url = ask(
            "Epic FHIR R4 base URL for this customer instance "
            "(e.g. https://ehr.example-health.org/interconnect-fhir-oauth/api/FHIR/R4)",
            validator=validate_url,
        )
    elif uses_client_secret:
        print(
            f"{profile.name} authenticates with a client SECRET (plain OAuth2\n"
            "client-credentials) - the one integrated vendor that does not use a\n"
            "signed JWT assertion. The secret is issued when the app is enabled\n"
            "for the practice through the Marketplace, and the base URL is\n"
            "practice-scoped (the practice id is part of it).\n"
        )
        fhir_base_url = ask(
            f"{profile.name} FHIR R4 base URL for this practice",
            validator=validate_url,
        )
    else:
        print(
            f"{profile.name} uses SMART Backend Services auth: an RS384-signed JWT\n"
            "client assertion, no client secret. The public half of your keypair\n"
            "is registered with the vendor (typically as a JWKS) during app\n"
            "registration. If you don't have a keypair yet, run this in another\n"
            "terminal first, then come back:\n"
            "    ./scripts/generate_epic_keypair.sh   # works for any RS384 vendor\n"
        )
        if emr_vendor == "cerner":
            print(
                "Oracle Health note: the tenant id forms part of the base URL\n"
                "(e.g. https://fhir-ehr.cerner.com/r4/{tenant-id}), and token\n"
                "requests carry explicit system/{Type}.read scopes - the platform\n"
                "derives and sends those automatically from the vendor profile.\n"
            )
        fhir_base_url = ask(
            f"{profile.name} FHIR R4 base URL for this instance",
            validator=validate_url,
        )
    fhir_token_url = ask(f"{profile.name} OAuth2 token URL", validator=validate_url)

    if emr_vendor == "epic":
        print(
            "\nEpic issues SEPARATE non-production and production client IDs per app.\n"
            "The non-production ID only works against the R4 sandbox - using it\n"
            "against a live customer base URL (or vice versa) is the most common\n"
            "integration failure. Double-check which one you're entering."
        )
        fhir_client_id = ask("Epic client ID (production or non-production)")
    else:
        fhir_client_id = ask(f"{profile.name} client ID")

    fhir_client_secret = ""
    if uses_client_secret:
        # getpass, so the secret never echoes to the terminal or lands in
        # shell history - it is still written to .env, which is why that
        # file is chmod 600 and gitignored.
        fhir_client_secret = ask(
            f"{profile.name} client secret (input hidden)", secret=True
        )

    if uses_client_secret:
        print(
            "\nA private key file is still required: core/config/settings.py\n"
            "validates the key path at startup for every vendor, and other flows\n"
            "(records delivery to a JWT vendor, a later vendor switch) use it.\n"
            "Generate one with ./scripts/generate_epic_keypair.sh if you have\n"
            "none - for this vendor it is never sent anywhere."
        )
    private_key_path = ask(
        "Path to the RS384 private key file (from generate_epic_keypair.sh)",
        default="./epic_private_key.pem",
    )
    private_key_file = Path(private_key_path).resolve()
    if not private_key_file.is_file():
        print(f"  WARNING: {private_key_file} does not exist yet. You can fix the path in .env later,")
        print("  but install.sh will fail to start until this file is present.")

    jwt_kid = None
    if not uses_client_secret:
        jwt_kid = ask(
            "JWT key ID (kid) - only needed if you registered a JWK Set URL rather than "
            "uploading the public key directly; leave blank otherwise",
            optional=True,
        ) or None

    if not profile.supports_bulk_export:
        print(
            f"\nNote: {profile.name}'s profile records NO FHIR Bulk Data Export\n"
            "($export), so the bulk scheduler will refuse this vendor at startup\n"
            "by design; ingestion runs through the per-resource-type scheduler\n"
            "only. See docs/EMR_CONNECTORS.md."
        )

    print("\n-- Retention policy --")
    print(
        "This is the one point at which retention is decided. The number you give\n"
        "here is written into .env and used to stamp every stored object with a\n"
        "retain-until date.\n"
        "\n"
        "Read this before answering: retention is RECORDED, NOT ENFORCED. This\n"
        "deployment provisions no Object Lock, no Azure immutability policy and no\n"
        "GCS retention lock. Nothing stops a principal holding delete permission\n"
        "from removing stored PHI before the period elapses, and nothing deletes\n"
        "it afterward. What this platform gives you is detection -- versioning, a\n"
        "SHA-256 per object, a hash-chained audit log, and cloud access logs -- not\n"
        "prevention. Enforcement is operational: IAM scoping, monitoring, and a\n"
        "documented disposition procedure (see runbooks/RUNBOOK_DISPOSITION.md).\n"
        "\n"
        "Confirm the correct minimum for your state and data type in\n"
        "docs/COMPLIANCE.md first. Records of minors typically run on a longer\n"
        "clock than adult records. For anything beyond a single flat number, use a\n"
        "retention ruleset file instead -- see runbooks/RUNBOOK_RETENTION_RULES.md."
    )
    retention_years = ask("Retention period in years", default="10", validator=validate_int)

    print("\n-- AI assistant (optional add-on) --")
    print(
        "The PHI AI Platform can include an assistant that helps people implement, operate\n"
        "and use this platform, grounded in this project's runbooks and in your own\n"
        "deployment's configuration.\n"
        "\n"
        "Read this before answering. It is the ONLY component of this system that\n"
        "sends anything outside your deployment. No PHI is sent -- it has no ability\n"
        "to read clinical content, and outbound text is scanned and refused if it\n"
        "looks like PHI -- but the network path is real, and which provider you pick\n"
        "decides whether it leaves your cloud account at all:\n"
        "\n"
        "  bedrock    Claude inside YOUR OWN AWS account, under the AWS BAA you\n"
        "             already rely on for S3 and KMS. Nothing crosses the boundary.\n"
        "  vertex     The same, inside your own GCP project, under the Google BAA.\n"
        "  anthropic  Anthropic's API directly -- a genuine third-party egress path.\n"
        "             The only option on Azure, which has no in-cloud Claude.\n"
        "\n"
        "Skipping this is safe and reversible: the assistant is off by default and\n"
        "nothing else changes. Full detail in runbooks/RUNBOOK_AI_ASSISTANT.md."
    )

    assistant_values: dict[str, str] = {}
    if ask_choice(
        "Enable the AI assistant?",
        ["No - skip it (recommended if you haven't read the runbook yet)", "Yes, configure it"],
    ).startswith("Yes"):
        # Default to the in-cloud option matching the platform's own cloud,
        # since that is the choice with no new vendor and no new egress.
        # Azure has no in-cloud Claude, so it gets the direct API.
        default_provider = {"aws": "bedrock", "gcp": "vertex", "azure": "anthropic"}[provider]
        print(
            f"\nFor a {provider.upper()} deployment the recommended provider is "
            f"'{default_provider}'."
        )
        assistant_provider = ask_choice(
            "Which model provider?", ["bedrock", "vertex", "anthropic"]
        )

        if assistant_provider == "anthropic":
            print(
                "\nThis sends requests outside your cloud account. Anthropic offers a\n"
                "Business Associate Agreement; whether you need one is a question for\n"
                "your privacy officer, and the input to it is that no PHI travels this\n"
                "path by construction."
            )
            key_path = ask(
                "Path to a file containing the Anthropic API key (mounted read-only, "
                "like the Epic private key)",
                optional=True,
            )
            if key_path:
                assistant_values["PHI_AI_ASSISTANT_API_KEY_PATH"] = str(
                    Path(key_path).resolve()
                )
            else:
                print(
                    "  No path given. The assistant will fall back to the "
                    "ANTHROPIC_API_KEY environment variable, which is workable but "
                    "reaches process listings and log capture in ways a mounted file "
                    "does not."
                )
        elif assistant_provider == "bedrock":
            assistant_values["PHI_AI_ASSISTANT_AWS_REGION"] = ask(
                "Bedrock region (Claude is not available in every region)",
                default=region if provider == "aws" else "us-east-1",
            )
            print(
                "  Remember: model access is granted per-account in the Bedrock\n"
                "  console, and the platform's IAM role needs bedrock:InvokeModel.\n"
                "  Until both are done, the first call fails with a permission error."
            )
        else:
            assistant_values["PHI_AI_ASSISTANT_GCP_PROJECT"] = ask(
                "GCP project for Vertex AI",
                default=gcp_project or None,
            )
            print(
                "  Remember: enable the model in Model Garden and grant the service\n"
                "  account the Vertex AI User role."
            )

        assistant_values["PHI_AI_ASSISTANT_PROVIDER"] = assistant_provider
        assistant_values["PHI_AI_ASSISTANT_ENABLED"] = "true"
        # Recorded because the operator was shown the egress explanation
        # above and answered Yes to it. The application refuses to start
        # with ENABLED set and this missing, deliberately - see
        # core/assistant/config.py.
        assistant_values["PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED"] = "true"
    else:
        # Explicitly off rather than merely unasked. This run DID ask, so
        # leaving a previously-enabled value to pass through the merge
        # below would silently keep the assistant on against the answer
        # just given.
        assistant_values["PHI_AI_ASSISTANT_ENABLED"] = "false"

    # H8 fix: merge onto whatever's already in .env (Terraform's
    # env_fragment output, a prior run of this script, or hand-added
    # lines) instead of collecting this run's answers into a fresh dict
    # and overwriting everything - see this module's own docstring.
    out_path = Path(".env")
    merged = _load_existing_env(out_path)

    # Same reasoning as the assistant's explicit-off below: this run DID
    # decide the vendor, so a client secret left over from a previous
    # athenahealth configuration must not silently survive a switch to a
    # JWT vendor - it would be a lingering credential in .env that the
    # answer just given said this deployment does not use.
    if not uses_client_secret:
        merged.pop("PHI_AI_FHIR_CLIENT_SECRET", None)

    new_values: dict[str, str] = {
        "PHI_AI_CLOUD_PROVIDER": provider,
        "PHI_AI_STORAGE_BUCKET": bucket,
        "PHI_AI_STORAGE_REGION": region,
        "PHI_AI_KMS_KEY_ID": kms_key,
        "PHI_AI_AUDIT_BUCKET": audit_bucket,
        "PHI_AI_AUDIT_KMS_KEY_ID": audit_kms_key,
        "PHI_AI_EMR_VENDOR": emr_vendor,
        "PHI_AI_FHIR_BASE_URL": fhir_base_url,
        "PHI_AI_FHIR_TOKEN_URL": fhir_token_url,
        "PHI_AI_FHIR_CLIENT_ID": fhir_client_id,
        "PHI_AI_FHIR_PRIVATE_KEY_PATH": str(private_key_file),
        "PHI_AI_RETENTION_YEARS": retention_years,
    }
    if fhir_client_secret:
        new_values["PHI_AI_FHIR_CLIENT_SECRET"] = fhir_client_secret
    if gcp_project:
        new_values["PHI_AI_GCP_PROJECT"] = gcp_project
    if azure_account_url:
        new_values["PHI_AI_AZURE_ACCOUNT_URL"] = azure_account_url
        new_values["PHI_AI_AZURE_VAULT_URL"] = azure_vault_url
    if jwt_kid:
        new_values["PHI_AI_FHIR_JWT_KID"] = jwt_kid
    new_values.update(assistant_values)

    preserved_keys = sorted(k for k in merged if k not in new_values)
    merged.update(new_values)

    # Emit this run's fields first, in the same stable order .env.example
    # uses, followed by anything preserved from an existing .env this run
    # didn't ask about at all (PHI_AI_DB_*, PHI_AI_PSYCHOTHERAPY_*,
    # PHI_AI_FHIR_GROUP_ID, PHI_AI_RETENTION_RULESET_PATH, a
    # different PHI_AI_RETENTION_YEARS_OVERRIDES, or anything else a
    # human added by hand) - explicit and labeled, not silently merged in
    # with no indication of where it came from.
    ordered_new_keys = [
        "PHI_AI_CLOUD_PROVIDER",
        "PHI_AI_STORAGE_BUCKET",
        "PHI_AI_STORAGE_REGION",
        "PHI_AI_KMS_KEY_ID",
        "PHI_AI_AUDIT_BUCKET",
        "PHI_AI_AUDIT_KMS_KEY_ID",
        "PHI_AI_GCP_PROJECT",
        "PHI_AI_AZURE_ACCOUNT_URL",
        "PHI_AI_AZURE_VAULT_URL",
        "PHI_AI_EMR_VENDOR",
        "PHI_AI_FHIR_BASE_URL",
        "PHI_AI_FHIR_TOKEN_URL",
        "PHI_AI_FHIR_CLIENT_ID",
        "PHI_AI_FHIR_CLIENT_SECRET",
        "PHI_AI_FHIR_PRIVATE_KEY_PATH",
        "PHI_AI_FHIR_JWT_KID",
        "PHI_AI_RETENTION_YEARS",
        "PHI_AI_ASSISTANT_ENABLED",
        "PHI_AI_ASSISTANT_EGRESS_ACKNOWLEDGED",
        "PHI_AI_ASSISTANT_PROVIDER",
        "PHI_AI_ASSISTANT_AWS_REGION",
        "PHI_AI_ASSISTANT_GCP_PROJECT",
        "PHI_AI_ASSISTANT_API_KEY_PATH",
    ]

    lines = ["# Generated by install/installer_chatbot.py -- do not commit this file."]
    for key in ordered_new_keys:
        if key in new_values:
            lines.append(f"{key}={new_values[key]}")
    if preserved_keys:
        lines.append("")
        lines.append("# Preserved from the existing .env - not asked about by this run")
        lines.append("# (e.g. PHI_AI_DB_*/PHI_AI_PSYCHOTHERAPY_* from")
        lines.append("# `terraform output env_fragment`, or a prior manual edit).")
        for key in preserved_keys:
            lines.append(f"{key}={merged[key]}")
    env_contents = "\n".join(lines) + "\n"

    if out_path.exists():
        print(
            f"\n{out_path} already exists. The values above will update matching fields; "
            f"{len(preserved_keys)} other existing line(s) not asked about by this run "
            "will be kept as-is."
        )

    out_path.write_text(env_contents)
    out_path.chmod(0o600)

    print(f"\nWrote {out_path} (permissions restricted to owner-read/write).")
    print("Next steps:")
    print("  1. python -m core.healthcheck        # verify compliance posture")
    print("  2. python scripts/smoke_test_aws.py   # end-to-end test, synthetic data")
    print("  3. ./install/install.sh               # start the service")
    print(
        "Reminder: the private key path above points to a credential file — make "
        "sure your own secrets management practices cover it (file permissions, "
        "backup, rotation). It is never read into this .env file itself."
    )


if __name__ == "__main__":
    main()
# Made by Ryan Gomez & Co. Inc.
