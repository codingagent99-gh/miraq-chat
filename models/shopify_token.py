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
        We refresh 1 hour before expiry so the server is never caught
        with a stale token mid-request.
        """
        return self.seconds_until_expiry < 3600