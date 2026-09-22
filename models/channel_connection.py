"""
models/channel_connection.py — Which store a WhatsApp number / Instagram
account belongs to.

Control-plane table: lives in the MAIN database next to `tenants` (listed in
_CONTROL_PLANE_TABLES so _TenantRoutingSession never routes it to a tenant DB).

One row per connected messaging account. POST /chat/channel resolves its
tenant through this table, the same way a storefront request resolves through
the signed Shopify `shop` param — the store is identified by the account the
customer messaged, never by anything the caller asserts about a tenant.

    channel               external_account_id is...
    whatsapp              value.metadata.phone_number_id from the webhook
    instagram             entry[].id — the IG professional account messaged

Meta access tokens are deliberately NOT stored here yet: whether this backend
or the webhook service holds them is still an open decision. If it lands here,
add an encrypted column using tenant_crypto, as tenants.woo_secret_encrypted does.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import UUID

from models.db_models import db


def _now():
    return datetime.now(timezone.utc)


class ChannelConnection(db.Model):
    __tablename__ = "channel_connections"
    __table_args__ = (
        # An account belongs to exactly one store.
        db.UniqueConstraint("channel", "external_account_id", name="uq_channel_account"),
    )

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    tenant_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    channel             = db.Column(db.String(20),  nullable=False)   # "whatsapp" | "instagram"
    external_account_id = db.Column(db.String(128), nullable=False)
    status              = db.Column(db.String(20),  nullable=False, default="active", index=True)

    # Human label for admin screens and logs only — e.g. "+91 98765 43210" or
    # "@wgctiles". Never used for lookup.
    display_name = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "external_account_id": self.external_account_id,
            "status": self.status,
            "display_name": self.display_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return (f"<ChannelConnection {self.channel}:{self.external_account_id} "
                f"tenant={self.tenant_id} status={self.status}>")