#!/usr/bin/env bash
#
# Generates the RSA keypair Epic's backend services auth requires.
#
#   ./scripts/generate_epic_keypair.sh
#
# RSA (RS384) only, with Epic's file names. For any profile whose
# assertion_algorithm is not RS384 (core/fhir/emr_profiles.py records ES384
# where a vendor documents only that), use scripts/generate_keypair.sh
# --alg <algorithm>, which generates the family the algorithm signs with.
#
# Epic backend services does not use a client secret. Instead you generate
# a keypair, register the PUBLIC key with Epic on open.epic.com under your
# client ID, and keep the PRIVATE key yourself - it signs the JWT client
# assertion the PHI AI Platform presents on every token request, and it
# never leaves your infrastructure.
#
# Output:
#   epic_private_key.pem  - keep this. Never commit it, never email it.
#                            Referenced by PHI_AI_FHIR_PRIVATE_KEY_PATH.
#   epic_public_key.pem   - upload this to open.epic.com when registering
#                            or updating your client ID.
#
# Epic accepts 2048 or 4096-bit RSA keys; this generates 4096-bit for
# margin, since key rotation on a live production client ID means
# coordinating a change with every connected customer instance - a good
# key is worth the extra few bytes.

set -euo pipefail

OUT_DIR="${1:-.}"
PRIVATE_KEY="${OUT_DIR}/epic_private_key.pem"
PUBLIC_KEY="${OUT_DIR}/epic_public_key.pem"

if [[ -f "$PRIVATE_KEY" ]]; then
  echo "ERROR: $PRIVATE_KEY already exists." >&2
  echo "Overwriting it invalidates every token request signed with the old key," >&2
  echo "and Epic will reject them until the new public key is registered and" >&2
  echo "propagated. Move or remove the existing file first if this is intentional." >&2
  exit 1
fi

command -v openssl >/dev/null 2>&1 || { echo "ERROR: openssl is required." >&2; exit 1; }

echo "Generating 4096-bit RSA keypair..."
openssl genrsa -out "$PRIVATE_KEY" 4096 2>/dev/null
openssl rsa -in "$PRIVATE_KEY" -pubout -out "$PUBLIC_KEY" 2>/dev/null
chmod 600 "$PRIVATE_KEY"

echo ""
echo "Wrote:"
echo "  $PRIVATE_KEY  (mode 600 - keep this secret; set PHI_AI_FHIR_PRIVATE_KEY_PATH to it)"
echo "  $PUBLIC_KEY   (upload this file's contents at open.epic.com under your client ID)"
echo ""
echo "Next steps:"
echo "  1. Register a client ID at https://fhir.epic.com if you haven't already,"
echo "     selecting 'Backend Services' as the app type."
echo "  2. Upload $PUBLIC_KEY as the client's public key."
echo "  3. Note: Epic issues SEPARATE non-production and production client IDs."
echo "     The non-production ID only works against the R4 sandbox; mixing the"
echo "     two up against the wrong base URL is the most common integration"
echo "     failure reported by teams doing this for the first time."
echo "  4. Generate a second keypair for production when you're ready to go"
echo "     live with a real customer - do not reuse the sandbox key."
# Made by Ryan Gomez & Co. Inc.
