"""
models/shopify_token.py — Persisted Shopify OAuth token.

One row per store domain. The token manager reads/writes this table
instead of relying on .env or in-memory state, so tokens survive restarts.

ENCRYPTED AT REST
-----------------
access_token and refresh_token open the store's customer and order data, so
they are stored encrypted with tenant_crypto (Fernet, key in TENANT_ENC_KEY),
like the custom-app client secrets and Instagram tokens. The database columns
keep their names but hold ciphertext; code keeps using row.access_token and
row.refresh_token, which encrypt on write and decrypt on read.

Rows written before this change hold plain text. They are still read (a
Fernet token always starts with "gAAAAA", a Shopify token never does) until
encrypt_shopify_tokens.py has rewritten them; any write encrypts.

A value that can't be decrypted (TENANT_ENC_KEY changed, or the value is
damaged) reads as "" — the same as no token — and tenant_crypto logs it. The
existing repair then takes over (the admin page re-runs the token exchange,
see routes/shopify_admin._token_problem) instead of a wrong token being sent
to Shopify.
"""

from datetime import datetime, timezone
from models import db

_FERNET_PREFIX = "gAAAAA"


def is_encrypted(stored) -> bool:
    """True if a stored column value is a Fernet token, not legacy plain text."""
    return bool(stored) and stored.startswith(_FERNET_PREFIX)


def _seal(plaintext):
    if plaintext is None:
        return None
    from tenant_crypto import encrypt_secret
    return encrypt_secret(plaintext)


def _open(stored):
    if not stored or not is_encrypted(stored):
        return stored  # None, "", or a legacy plain-text row
    from cryptography.fernet import InvalidToken
    from tenant_crypto import decrypt_secret
    try:
        return decrypt_secret(stored)
    except InvalidToken:
        # Logged by tenant_crypto. Never hand back a wrong token: read as none.
        return ""


class ShopifyToken(db.Model):
    __tablename__ = "shopify_tokens"

    # Natural PK — one row per store
    store_domain  = db.Column(db.String(255), primary_key=True)

    # Ciphertext; use the access_token property below.
    _access_token = db.Column("access_token", db.Text, nullable=False)
    scope         = db.Column(db.Text,        nullable=True)

    # Timing
    fetched_at    = db.Column(db.DateTime(timezone=True), nullable=False,
                              default=lambda: datetime.now(timezone.utc))
    expires_at    = db.Column(db.DateTime(timezone=True), nullable=False)

    # Expiring offline tokens (authorization code grant with expiring=1).
    # Shopify no longer accepts non-expiring offline tokens on the Admin API,
    # so an OAuth-installed store gets a short-lived access token plus a
    # refresh token that is ROTATED on every refresh — the old one stops
    # working once used, so the new one must be persisted in the same write.
    # NULL for stores whose token comes from the client_credentials grant.
    # Ciphertext; use the refresh_token property below.
    _refresh_token           = db.Column("refresh_token", db.Text, nullable=True)
    refresh_token_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Diagnostics
    refresh_count = db.Column(db.Integer, nullable=False, default=0)
    last_error    = db.Column(db.Text,    nullable=True)

    def __repr__(self):
        return (
            f"<ShopifyToken store={self.store_domain!r} "
            f"expires_at={self.expires_at.isoformat()} "
            f"refreshes={self.refresh_count}>"
        )

    @property
    def access_token(self):
        return _open(self._access_token)

    @access_token.setter
    def access_token(self, value):
        self._access_token = _seal(value)

    @property
    def refresh_token(self):
        return _open(self._refresh_token)

    @refresh_token.setter
    def refresh_token(self, value):
        self._refresh_token = _seal(value)

    @property
    def is_expired(self) -> bool:
        """True if the token has already expired."""
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def seconds_until_expiry(self) -> float:
        """Seconds remaining before the token expires (negative = already expired)."""
        delta = self.expires_at - datetime.now(timezone.utc)
        return delta.total_seconds()

    @property
    def needs_refresh(self) -> bool:
        """
        True if the token should be proactively refreshed.

        The buffer is a quarter of the token's lifetime, capped at 1 hour.
        A flat 1-hour buffer was fine for 24h client_credentials tokens but
        wrong for expiring offline tokens, which can live about an hour: they
        would count as "needs refresh" the moment they were minted, and every
        read would start another refresh. 24h tokens keep the same 1h buffer.
        """
        lifetime = (self.expires_at - self.fetched_at).total_seconds() if self.fetched_at else 86400
        buffer = min(3600.0, max(60.0, lifetime * 0.25))
        return self.seconds_until_expiry < buffer