"""
models/instagram_connect_attempt.py — the outcome of an Instagram connect
started from the embedded admin page.

Facebook Login can't run inside the Shopify admin iframe, so the embedded page
opens it in a pop-up. Shopify rejects embedded apps that send merchants to a
standalone version of themselves, so the pop-up does only the Facebook step:
the callback writes the outcome here and closes, and the admin page reads it
(GET /shopify/admin/instagram/attempt) and shows the result, or the account
list to choose from, inside the admin.

Why the server and not a message from the pop-up: Facebook's pages can cut the
pop-up's link back to the window that opened it (window.opener), and the
admin iframe's storage is partitioned apart from the pop-up's, so this table
is the one place both sides reach reliably.

One row per shop. Each attempt the page starts gets its own attempt_id; a new
attempt overwrites the row, and an outcome is only written onto the row whose
attempt_id it carries, so a stale pop-up can never overwrite a newer attempt.

Control-plane table: lives in the MAIN database next to `tenants` (listed in
_CONTROL_PLANE_TABLES so _TenantRoutingSession never routes it to a tenant DB).

candidates_encrypted holds the account list for a "choose" outcome, Page
tokens included, encrypted with tenant_crypto. It is cleared once the merchant
chooses, and when the attempt is seen expired.
"""

from datetime import datetime, timezone

from models.db_models import db


def _now():
    return datetime.now(timezone.utc)


class InstagramConnectAttempt(db.Model):
    __tablename__ = "instagram_connect_attempts"

    shop = db.Column(db.String(255), primary_key=True)
    attempt_id = db.Column(db.String(64), nullable=False)

    # waiting | connected | choose | cancelled | no_account | taken | failed | error
    status = db.Column(db.String(20), nullable=False, default="waiting")

    # "@handle" for connected / taken / failed; never used for lookup.
    account_label = db.Column(db.String(255), nullable=True)

    candidates_encrypted = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    def __repr__(self):
        return f"<InstagramConnectAttempt {self.shop} {self.attempt_id} status={self.status}>"
