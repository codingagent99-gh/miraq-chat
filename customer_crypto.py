"""
customer_crypto.py - application-level encryption at rest for shopper data.

Shopify's Level 1 protected-customer-data requirement is "Encrypt data at
rest and in transit". The server disk is not encrypted, so the backend
encrypts shopper data itself before it is written: chat messages, the
conversation context, customer and order IDs, log lines and export files.

The switch
----------
ENCRYPT_CUSTOMER_DATA=1   new values are written encrypted (production)
ENCRYPT_CUSTOMER_DATA=0   new values are written as plain text (default)

The switch only decides how NEW values are written. Reading always handles
both forms, so flipping it never breaks existing data; it needs a restart
only because .env is read at start.

Every encrypted value carries a marker prefix, so plain and encrypted values
can sit side by side:
    enc1:<Fernet token>     text and JSON (random: same input, new output)
    cid1:<AES-SIV token>    IDs (deterministic: same input, same output,
                            so they can still be looked up and indexed)

The key
-------
CUSTOMER_DATA_KEY is a Fernet key, or a comma-separated list of keys with the
newest first: the first key encrypts, every key decrypts. Generate one with
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
The ID key is derived from the same key (HKDF), so there is one secret to keep.
Keep a copy outside the server: without it, encrypted data cannot be read.

Column types
------------
EncryptedText / EncryptedString   free text (random encryption)
EncryptedJSON                     JSONB holding one encrypted JSON string
EncryptedID                       IDs used in lookups (deterministic); its
                                  == / != / in_ / not_in match BOTH the
                                  encrypted and the plain form

None and "" are never encrypted: guests are stored as NULL or "" and the
guest filters depend on that.

Nothing here needs the key at import time, only when a value is actually
encrypted or decrypted, so scripts that never touch encrypted values (the
nightly purge) keep working without it.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Iterable, List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import String, Text, type_coerce
from sqlalchemy import types as sa_types
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import operators

try:  # scripts import this module directly; make sure .env is loaded
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a hard dependency elsewhere
    pass

# Not chat_logger: chat_logger uses this module for encrypted log lines.
logger = logging.getLogger("miraq_chat")

SWITCH_ENV = "ENCRYPT_CUSTOMER_DATA"
KEY_ENV = "CUSTOMER_DATA_KEY"

ENC_PREFIX = "enc1:"
ID_PREFIX = "cid1:"

_TRUE = {"1", "true", "yes", "on"}
_ID_KDF_INFO = b"miraq-customer-id-v1"
_CANARY = "miraq-customer-data-canary-v1"


class CustomerCryptoError(RuntimeError):
    """Raised for a missing or wrong key. Never swallowed."""


# ── Switch and keys ─────────────────────────────────────────────────────────

def encryption_on() -> bool:
    """Read on every write, so tests and scripts see the current value."""
    return os.getenv(SWITCH_ENV, "0").strip().lower() in _TRUE


def _raw_keys() -> str:
    return os.getenv(KEY_ENV, "").strip()


def key_configured() -> bool:
    return bool(_raw_keys())


@lru_cache(maxsize=4)
def _keyring(raw: str):
    """(MultiFernet, [AESSIV, ...]) for this key string. Newest key first."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise CustomerCryptoError(
            f"{KEY_ENV} is not set. Generate one with: python -c \"from "
            f"cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    fernets, sivs = [], []
    for i, part in enumerate(parts, 1):
        try:
            fernets.append(Fernet(part.encode()))
            master = base64.urlsafe_b64decode(part.encode())
        except (ValueError, TypeError, binascii.Error) as e:
            raise CustomerCryptoError(
                f"{KEY_ENV} key #{i} is not a valid Fernet key "
                f"(urlsafe-base64, 32 bytes): {type(e).__name__}"
            ) from None
        siv_key = HKDF(algorithm=hashes.SHA256(), length=64, salt=None,
                       info=_ID_KDF_INFO).derive(master)
        sivs.append(AESSIV(siv_key))
    return MultiFernet(fernets), sivs


def _keys():
    return _keyring(_raw_keys())


# ── Text ────────────────────────────────────────────────────────────────────

def encrypt_text(value: Optional[str], force: bool = False) -> Optional[str]:
    """Encrypt when the switch is ON (or force=True). None and "" pass through.

    A plain value that happens to start with a marker (a shopper typing
    "enc1:...") is encrypted too, so it always reads back exactly.
    """
    if value is None or value == "":
        return value
    if not (force or encryption_on()):
        return value
    multi, _ = _keys()
    return ENC_PREFIX + multi.encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt_text(value: Optional[str]) -> Optional[str]:
    """Decrypt a value written by encrypt_text. Anything else passes through.

    Without a key, nothing can be ours: server startup refuses to run without
    the key once data has been encrypted (check_startup), so a marker-looking
    value here is something a shopper typed while encryption was off.
    """
    if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
        return value
    if not key_configured():
        return value
    multi, _ = _keys()
    try:
        return multi.decrypt(value[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        # Not one of ours (check_startup proved the key is right).
        return value


def is_encrypted(value) -> bool:
    return isinstance(value, str) and (value.startswith(ENC_PREFIX) or value.startswith(ID_PREFIX))


# ── IDs (deterministic) ─────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _siv_encrypt(siv: AESSIV, value: str) -> str:
    return ID_PREFIX + _b64(siv.encrypt(value.encode("utf-8"), None))


def encrypt_id(value: Optional[str], force: bool = False) -> Optional[str]:
    """Same input, same output, so the result can be looked up.
    None and "" pass through (guests)."""
    if value is None or value == "":
        return value
    value = str(value)
    if not (force or encryption_on()):
        return value
    _, sivs = _keys()
    return _siv_encrypt(sivs[0], value)


def decrypt_id(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str) or not value.startswith(ID_PREFIX):
        return value
    if not key_configured():
        return value
    _, sivs = _keys()
    try:
        blob = _unb64(value[len(ID_PREFIX):])
    except (binascii.Error, ValueError):
        return value
    for siv in sivs:
        try:
            return siv.decrypt(blob, None).decode("utf-8")
        except (InvalidTag, ValueError, UnicodeError):
            continue
    return value


def id_forms(value) -> List[str]:
    """Every stored form this ID can have: plain, and encrypted under each key.

    Lookups use all of them, so rows written while the switch was OFF, or
    under an older key, are still found.
    """
    if value is None:
        return []
    value = str(value)
    if value == "" or not key_configured():
        return [value]
    _, sivs = _keys()
    forms = [value] + [_siv_encrypt(siv, value) for siv in sivs]
    return list(dict.fromkeys(forms))


# ── JSON ────────────────────────────────────────────────────────────────────

def encrypt_json(value, force: bool = False):
    if value is None:
        return None
    if not (force or encryption_on()):
        return value
    return encrypt_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False), force=True)


def decrypt_json(value):
    if isinstance(value, str) and value.startswith(ENC_PREFIX):
        plain = decrypt_text(value)
        if plain is value:  # not ours, keep as stored
            return value
        return json.loads(plain)
    return value


# ── Column types ────────────────────────────────────────────────────────────

class _EncryptedTextMixin:
    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_text(str(value))

    def process_result_value(self, value, dialect):
        return decrypt_text(value)


class EncryptedText(_EncryptedTextMixin, sa_types.TypeDecorator):
    """TEXT column, randomly encrypted. Use for free text (message content)."""
    impl = Text
    cache_ok = True


class EncryptedString(_EncryptedTextMixin, sa_types.TypeDecorator):
    """VARCHAR column, randomly encrypted. Keep the length >= 255:
    an encrypted value is ~1.4x the plain one plus ~110 characters."""
    impl = String
    cache_ok = True


class EncryptedJSON(sa_types.TypeDecorator):
    """JSONB column. When ON, the whole value is stored as one encrypted JSON
    string. Nothing may query inside it in SQL; read it in Python."""
    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_json(value)

    def process_result_value(self, value, dialect):
        return decrypt_json(value)


class EncryptedID(sa_types.TypeDecorator):
    """VARCHAR column for IDs that are looked up: customer, order and channel
    user IDs. Deterministic, so equality and indexes still work.

    == / != / in_ / not_in compare against every stored form (id_forms), so a
    lookup finds the row whether it was written encrypted or plain.
    session.get() by primary key does NOT go through these comparisons: use
    a query with == for primary-key lookups (see services/whats_new.py).
    """
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_id(str(value))

    def process_result_value(self, value, dialect):
        return decrypt_id(value)

    class comparator_factory(sa_types.TypeDecorator.Comparator):
        def _plain(self):
            # The same column, but compared as plain VARCHAR: the values we
            # pass are already in their stored form, so no bind processing.
            return type_coerce(self.expr, String())

        @staticmethod
        def _scalar(value) -> bool:
            return isinstance(value, (str, int))

        def _forms(self, values: Iterable) -> List[str]:
            out: List[str] = []
            for v in values:
                out.extend(id_forms(v))
            return list(dict.fromkeys(out))

        def operate(self, op, *other, **kwargs):
            if other:
                target = other[0]
                if op is operators.eq and self._scalar(target):
                    return self._plain().in_(self._forms([target]))
                if op is operators.ne and self._scalar(target):
                    return self._plain().not_in(self._forms([target]))
                if op in (operators.in_op, operators.not_in_op) and \
                        isinstance(target, (list, tuple, set, frozenset)) and \
                        all(self._scalar(v) for v in target):
                    forms = self._forms(target)
                    return (self._plain().in_(forms) if op is operators.in_op
                            else self._plain().not_in(forms))
            return super().operate(op, *other, **kwargs)


# ── Startup check ───────────────────────────────────────────────────────────

def new_canary() -> str:
    """The value stored in customer_data_encryption.canary (needs the key)."""
    return encrypt_text(_CANARY, force=True)


def canary_ok(stored: Optional[str]) -> bool:
    """True if `stored` was written by new_canary() with one of today's keys."""
    return key_configured() and decrypt_text(stored) == _CANARY


def check_startup(session, model) -> str:
    """Refuse to start when shopper data would be unreadable or written plain.

    `model` is the single-row control-plane table that remembers encryption
    has been used (models/customer_data_encryption.py). Returns a short
    status line for the startup log. Raises CustomerCryptoError to stop the
    server.

      switch ON,  no key                       -> refuse
      key malformed                            -> refuse
      encryption used before, no key           -> refuse (data unreadable)
      encryption used before, canary fails     -> refuse (wrong key)
      switch ON, first time                    -> record the canary
      switch OFF after encryption was used     -> warn every start
    """
    on = encryption_on()
    has_key = key_configured()

    if on and not has_key:
        raise CustomerCryptoError(
            f"{SWITCH_ENV}=1 but {KEY_ENV} is not set. Refusing to start "
            f"rather than write shopper data as plain text."
        )
    if has_key:
        _keys()  # raises CustomerCryptoError when malformed

    row = session.get(model, 1)
    if row is not None:
        if not has_key:
            raise CustomerCryptoError(
                f"Shopper data has been encrypted since {row.first_enabled_at:%Y-%m-%d}, "
                f"but {KEY_ENV} is not set, so it cannot be read. Restore the key "
                f"from the password manager. Refusing to start."
            )
        if not canary_ok(row.canary):
            raise CustomerCryptoError(
                f"{KEY_ENV} does not match the key shopper data was encrypted with "
                f"(the stored check value does not decrypt). If you rotated the key, "
                f"keep the old one after the new one: NEW_KEY,OLD_KEY. Refusing to start."
            )
    elif on:
        row = model(id=1, canary=new_canary(),
                    first_enabled_at=datetime.now(timezone.utc))
        session.add(row)
        session.commit()

    if on:
        return "customer data encryption: ON"
    if row is not None:
        logger.warning(
            f"customer data encryption: OFF ({SWITCH_ENV}=0). New shopper data is "
            f"being stored as plain text; the Shopify 'encrypted at rest' answer "
            f"does not hold for it. Existing encrypted data is still readable."
        )
        return "customer data encryption: OFF (warning: was ON before)"
    return "customer data encryption: OFF"
