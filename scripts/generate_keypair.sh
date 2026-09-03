#!/usr/bin/env bash
#
# Generates the key pair a SMART Backend Services client assertion needs,
# of the family the vendor's profile algorithm signs with:
#
#   ./scripts/generate_keypair.sh --alg RS384 [OUT_DIR]   # RSA 4096-bit  (RFC 7518 s3.3)
#   ./scripts/generate_keypair.sh --alg ES384 [OUT_DIR]   # EC secp384r1 / P-384 (RFC 7518 s3.4)
#
# Which algorithm a vendor takes is recorded per profile in
# core/fhir/emr_profiles.py (EMRProfile.assertion_algorithm), from that
# vendor's own documentation; core/config/settings.py refuses a key of the
# wrong family at startup, so generate the family the profile names.
#
# Backend services auth uses no client secret. The PUBLIC key is registered
# with the vendor (as a JWK Set URL or an uploaded key, as the vendor's own
# registration flow says - see the vendor's chapter in docs/EMR_CONNECTORS.md,
# 'Setting it up'); the PRIVATE key signs the JWT client assertion the PHI AI
# Platform presents on every token request and never leaves your
# infrastructure.
#
# Output (in OUT_DIR, default .):
#   private_key.pem  - keep this. Never commit it, never email it.
#                      Referenced by PHI_AI_FHIR_PRIVATE_KEY_PATH (PKCS#8).
#   public_key.pem   - register this with the vendor.
#
# scripts/generate_epic_keypair.sh is the older RSA-only script with Epic's
# file names; it still works for any RS384 profile.

set -euo pipefail

ALG=""
OUT_DIR="."
while [[ $# -gt 0 ]]; do
  case "$1" in
    --alg) ALG="${2:-}"; shift 2 ;;
    --alg=*) ALG="${1#--alg=}"; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) OUT_DIR="$1"; shift ;;
  esac
done

if [[ -z "$ALG" ]]; then
  echo "ERROR: --alg is required (RS384 or ES384 - the vendor profile's assertion_algorithm)." >&2
  exit 2
fi

PRIVATE_KEY="${OUT_DIR}/private_key.pem"
PUBLIC_KEY="${OUT_DIR}/public_key.pem"

if [[ -f "$PRIVATE_KEY" ]]; then
  echo "ERROR: $PRIVATE_KEY already exists." >&2
  echo "Overwriting it invalidates every token request signed with the old key," >&2
  echo "and the vendor will reject them until the new public key is registered." >&2
  echo "Move or remove the existing file first if this is intentional." >&2
  exit 1
fi

command -v openssl >/dev/null 2>&1 || { echo "ERROR: openssl is required." >&2; exit 1; }

case "$ALG" in
  RS256|RS384|RS512|PS256|PS384|PS512)
    echo "Generating 4096-bit RSA key pair for $ALG..."
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out "$PRIVATE_KEY" 2>/dev/null
    ;;
  ES256)
    echo "Generating EC P-256 (prime256v1) key pair for $ALG..."
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out "$PRIVATE_KEY" 2>/dev/null
    ;;
  ES384)
    echo "Generating EC P-384 (secp384r1) key pair for $ALG..."
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:secp384r1 -out "$PRIVATE_KEY" 2>/dev/null
    ;;
  ES512)
    echo "Generating EC P-521 (secp521r1) key pair for $ALG..."
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:secp521r1 -out "$PRIVATE_KEY" 2>/dev/null
    ;;
  *)
    echo "ERROR: unknown algorithm '$ALG'. Use the profile's assertion_algorithm (RS384 or ES384)." >&2
    exit 2
    ;;
esac
openssl pkey -in "$PRIVATE_KEY" -pubout -out "$PUBLIC_KEY" 2>/dev/null
chmod 600 "$PRIVATE_KEY"

echo ""
echo "Wrote:"
echo "  $PRIVATE_KEY  (mode 600 - keep this secret; set PHI_AI_FHIR_PRIVATE_KEY_PATH to it)"
echo "  $PUBLIC_KEY   (register this with the vendor as its 'Setting it up' chapter says)"
echo ""
echo "Next: follow the vendor's chapter in docs/EMR_CONNECTORS.md - 'Setting it up',"
echo "step 2 builds the JWK Set from this public key where the vendor registers a JWKS."
# Made by Ryan Gomez & Co. Inc.
