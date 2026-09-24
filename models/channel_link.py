"""
models/channel_link.py — which store customer a channel user (Instagram,
WhatsApp) has proven to be, and the sign-ins in flight.

Both tables live in each store's OWN database (not control-plane), so a link
made at Store A doesn't exist for Store B. See channel_link.py for the flow.

  channel_links          One row per channel user per store. Set only after
                         the customer signs in with the store's Shopify login;
                         read on every channel message to decide who is asking.
  channel_link_requests  A pending sign-in: created when the bot sends the
                         Sign in button, used once by the Shopify callback,
                         valid for a few minutes. The state value is stored
                         hashed and the PKCE verifier encrypted.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import UUID

from models.db_models import db


def _utcnow():
    return datetime.now(timezone.utc)


class ChannelLink(db.Model):
    __tablename__ = "channel_links"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel = db.Column(db.String(32), nullable=False)
    # The sender's ID on that channel (Instagram-scoped user ID, WhatsApp number).
    channel_user_id = db.Column(db.String(128), nullable=False)
    # Numeric Shopify customer ID, the same form conversations.customer_id holds.
    customer_id = db.Column(db.String(64), nullable=False, index=True)
    # Masked (r***@gmail.com) — shown back to the customer, never used to look up.
    email_display = db.Column(db.String(255), nullable=True)
    linked_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    # Shown once on the customer's next message ("Linked to r***@…"), then cleared.
    notice_pending = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (
        db.UniqueConstraint("channel", "channel_user_id", name="uq_channel_links_user"),
    )

    def is_valid(self, now=None) -> bool:
        now = now or _utcnow()
        expires = self.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires is not None and expires > now


class ChannelLinkRequest(db.Model):
    __tablename__ = "channel_link_requests"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    state_hash = db.Column(db.String(64), nullable=False, unique=True)
    channel = db.Column(db.String(32), nullable=False)
    channel_user_id = db.Column(db.String(128), nullable=False)
    conversation_id = db.Column(UUID(as_uuid=True), nullable=True)
    code_verifier_encrypted = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
