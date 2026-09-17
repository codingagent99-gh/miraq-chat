"""
tenant_crypto.py — Encryption-at-rest for tenant WooCommerce credentials.

Only the WooCommerce consumer SECRET is encrypted (stored as
Tenant.woo_secret_encrypted). The consumer KEY is not secret on its own —
it's the secret that grants write access, so that's what we protect.

Key source: a urlsafe-base64 Fernet key in the TENANT_ENC_KEY environment
variable. Generate one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Rotation note: Fernet supports multi-key decryption via MultiFernet. When you
rotate, prepend the new key and keep the old one for decrypt-only until every
row is re-encrypted. This ships single-key; the seam is marked below.
"""

from __future__ import annotations
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from chat_logger import get_logger

logger = get_logger("miraq_chat")

_ENC_KEY_ENV = "TENANT_ENC_KEY"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """
    Build the Fernet instance once. Raises at first use (not import) if the
    key is missing or malformed, so a misconfigured deploy fails loudly the
    first time a credential is encrypted/decrypted rather than silently.
    """
    raw = os.getenv(_ENC_KEY_ENV, "").strip()
    if not raw:
        raise RuntimeError(
            f"{_ENC_KEY_ENV} is not set. Generate one with: "
            f"python -c \"from cryptography.fernet import Fernet; "
            f"print(Fernet.generate_key().decode())\" and add it to .env"
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as e:
        raise RuntimeError(
            f"{_ENC_KEY_ENV} is not a valid Fernet key (expected urlsafe-base64, "
            f"32 bytes). {type(e).__name__}: {e}"
        )
    # Phase-rotation seam: swap Fernet(...) for
    #   MultiFernet([Fernet(new), Fernet(old)])
    # to decrypt old rows while encrypting new ones with the new key.


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a WooCommerce consumer secret for storage. Returns a str token."""
    if plaintext is None:
        plaintext = ""
    token = _fernet().encrypt(plaintext.encode())
    return token.decode()


def decrypt_secret(token: str) -> str:
    """
    Decrypt a stored secret back to plaintext. Raises InvalidToken if the
    ciphertext was tampered with or encrypted under a different key — we let
    that propagate rather than returning a wrong/empty secret silently.
    """
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error(
            "tenant_crypto: decrypt failed — token invalid or wrong key. "
            "Has TENANT_ENC_KEY changed since this row was written?"
        )
        raise