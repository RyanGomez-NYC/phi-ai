#!/usr/bin/env bash
# credential_process for running the web interface under the RESTORE role.
#
# The store bucket denies s3:GetObject to any session without a
# PurposeOfUse principal tag (deploy/aws/s3_store.tf,
# DenyReadWithoutPurposeOfUse), so the web app must run as a tagged
# assumed-role session rather than as a raw IAM principal. AWS profiles
# cannot attach session tags to a role_arn profile, but they CAN call a
# credential_process - this script - which assumes the role WITH the tag
# and emits the JSON shape botocore expects. botocore re-invokes it
# automatically when the session expires, so the app never dies at the
# top of the hour.
#
# The tag value is the service-level session purpose; the application
# still validates and records a per-request purpose of use on every
# clinical read (see core/web/app.py) - IAM checks presence, the audit
# trail records specifics.
set -euo pipefail

# Set PHI_AI_RESTORE_ROLE_ARN to your own account's restore role, e.g.
#   arn:aws:iam::<YOUR_AWS_ACCOUNT_ID>:role/phi-ai-restore
ROLE_ARN="${PHI_AI_RESTORE_ROLE_ARN:?set PHI_AI_RESTORE_ROLE_ARN to your restore role ARN}"

# Run the assume-role call on the machine's own base credentials, not on
# the profile that invoked this script - otherwise the profile would
# recursively invoke itself.
env -u AWS_PROFILE -u AWS_CONFIG_FILE aws sts assume-role \
  --role-arn "$ROLE_ARN" \
  --role-session-name phi-ai-web \
  --tags Key=PurposeOfUse,Value=operations \
  --duration-seconds 3600 \
  --output json \
| python3 -c "
import json, sys
c = json.load(sys.stdin)['Credentials']
print(json.dumps({
    'Version': 1,
    'AccessKeyId': c['AccessKeyId'],
    'SecretAccessKey': c['SecretAccessKey'],
    'SessionToken': c['SessionToken'],
    'Expiration': c['Expiration'],
}))"
# Made by Ryan Gomez & Co. Inc.
