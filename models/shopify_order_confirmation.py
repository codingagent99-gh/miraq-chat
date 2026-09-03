"""
models/shopify_order_confirmation.py — Bridges the Shopify orders/paid
webhook (routes/shopify.py: shopify_order_paid_event) to the widget's
polling endpoint (routes/chat.py: handle_order_status).

One row per widget session that has completed a Shopify checkout. The
webhook writes it; /chat/order-status reads it, writes the chat Message on
first read, and flips `delivered` so a second poll (or a page refresh)
doesn't duplicate the confirmation message in the conversation.
"""

from datetime import datetime, timezone
from models import db


class ShopifyOrderConfirmation(db.Model):
    __tablename__ = "shopify_order_confirmations"

    # Natural PK — the widget's session_id (same UUID string used
    # everywhere else, e.g. Conversation.id / X-MiraQ-Session).
    session_id   = db.Column(db.String(255), primary_key=True)

    order_id     = db.Column(db.String(255), nullable=False)
    order_number = db.Column(db.String(255), nullable=True)

    # False until /chat/order-status has consumed it and written the
    # Message row — prevents re-posting the same confirmation on repeat
    # polls or if the shopper reloads the page after seeing it once.
    delivered    = db.Column(db.Boolean, nullable=False, default=False)

    created_at   = db.Column(db.DateTime(timezone=True), nullable=False,
                              default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return (
            f"<ShopifyOrderConfirmation session_id={self.session_id!r} "
            f"order_id={self.order_id!r} delivered={self.delivered}>"
        )