"""
models/shopify_app.py — Shopify apps with CUSTOM distribution.

The public MiraQ app's credentials stay in .env (SHOPIFY_CLIENT_ID /
SHOPIFY_CLIENT_SECRET) and serve every store that installs it. A custom-
distribution app is a separate app in the Partner Dashboard with its OWN
client id and secret, installable only on the store(s) it was created for.
Each one is a row here.

Control-plane table: lives in the MAIN database next to `tenants` (listed in
_CONTROL_PLANE_TABLES so _TenantRoutingSession never routes it to a tenant DB).

    shops            the store(s) the app was set up for in the Partner
                     Dashboard. The install route refuses any other shop.
                     Empty = not restricted here (Shopify still restricts it).
    installed_shops  the store(s) that completed OAuth THROUGH this app. Only
                     these use this app's secret for App Proxy signatures,
                     webhook HMACs, token refresh and customer sign-in. Written
                     only by a Shopify-verified OAuth callback, never by the
                     admin API, so registering an app cannot redirect traffic
                     of a store that is already installed.

The secret is stored Fernet-encrypted (tenant_crypto), like
tenants.woo_secret_encrypted.
"""

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import JSONB

from models.db_models import db


def _now():
    return datetime.now(timezone.utc)


class ShopifyApp(db.Model):
    __tablename__ = "shopify_apps"

    client_id = db.Column(db.String(64), primary_key=True)
    client_secret_encrypted = db.Column(db.Text, nullable=False)
    label = db.Column(db.String(255), nullable=True)

    shops = db.Column(JSONB, nullable=False, default=list)
    installed_shops = db.Column(JSONB, nullable=False, default=list)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    def __repr__(self):
        return f"<ShopifyApp client_id={self.client_id!r} label={self.label!r} installed={self.installed_shops!r}>"
