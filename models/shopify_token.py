"""
models/shopify_token.py — Persisted Shopify OAuth token.

One row per store domain. The token manager reads/writes this table
instead of relying on .env or in-memory state, so tokens survive restarts.
"""

from datetime import datetime, timezone
from models import db


class ShopifyToken(db.Model):
    __tablename__ = "shopify_tokens"

    # Natural PK — one row per store
    store_domain  = db.Column(db.String(255), primary_key=True)

    access_token  = db.Column(db.Text,        nullable=False)
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
    refresh_token            = db.Column(db.Text, nullable=True)
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