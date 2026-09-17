"""
license_verifier.py — Ed25519 signature verification for licence activation.

Verifies over the RAW signed bytes the plugin forwards verbatim from the
licensing server — never a re-encoded/re-serialised JSON object, since
re-encoding (key order, whitespace, float formatting) can silently change
the bytes and break verification, or worse, mask a real tamper.

Public-key transport is NOT finalised by the licensing-server team yet
(Phase 0). This ships behind LICENSE_VERIFICATION_ENABLED so Phase 3 can be
built, deployed, and exercised end-to-end (plugin → /provision-tenant →
tenant row) before that dependency lands. Swap _load_public_key()'s body for
the real transport (static PEM vs fetched-and-cached) with zero changes to
call sites.
"""

from __future__ import annotations
import os
import json
import base64
from functools import lru_cache

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

from chat_logger import get_logger

logger = get_logger("miraq_chat")

_VERIFY_ENABLED = os.getenv("LICENSE_VERIFICATION_ENABLED", "false").lower() == "true"
_PUBLIC_KEY_ENV = "LICENSE_PUBLIC_KEY_PEM"


class LicenseVerificationError(Exception):
    pass


@lru_cache(maxsize=1)
def _load_public_key() -> Ed25519PublicKey:
    """
    PHASE-0 SEAM: today this reads a static PEM from an env var. If the
    licensing server instead exposes a public-key endpoint, replace this
    function's body with a fetch+cache — verify_license_payload() and every
    call site stay unchanged.
    """
    pem = os.getenv(_PUBLIC_KEY_ENV, "").strip()
    if not pem:
        raise LicenseVerificationError(
            f"{_PUBLIC_KEY_ENV} not set — cannot verify licence signatures. "
            "Set LICENSE_VERIFICATION_ENABLED=false to run without verification "
            "until the licensing server's public-key transport is finalised."
        )
    try:
        key = load_pem_public_key(pem.encode("utf-8"))
    except Exception as e:
        raise LicenseVerificationError(f"Invalid {_PUBLIC_KEY_ENV}: {e}")
    if not isinstance(key, Ed25519PublicKey):
        raise LicenseVerificationError(f"{_PUBLIC_KEY_ENV} is not an Ed25519 public key")
    return key


def verify_license_payload(raw_payload: str, signature_b64: str) -> dict:
    """
    Verify `signature_b64` (base64 Ed25519 sig) over the EXACT UTF-8 bytes of
    `raw_payload` (the string the plugin forwarded verbatim from the licensing
    server). On success, parses and returns the payload's claims as a dict.

    If LICENSE_VERIFICATION_ENABLED=false, signature checking is skipped and
    the payload is parsed and trusted as-is — ONLY for pre-Phase-0 testing.
    A loud warning is logged on every call so this can't go unnoticed in prod.
    """
    if not _VERIFY_ENABLED:
        logger.warning(
            "license_verifier: LICENSE_VERIFICATION_ENABLED=false — "
            "signature NOT checked. Do not run this in production."
        )
    else:
        try:
            signature = base64.b64decode(signature_b64)
        except Exception as e:
            raise LicenseVerificationError(f"signature is not valid base64: {e}")

        public_key = _load_public_key()
        try:
            public_key.verify(signature, raw_payload.encode("utf-8"))
        except InvalidSignature:
            raise LicenseVerificationError("signature does not match payload")

    try:
        return json.loads(raw_payload)
    except Exception as e:
        raise LicenseVerificationError(f"payload is not valid JSON: {e}")