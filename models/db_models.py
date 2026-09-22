"""
models/db_models.py — SQLAlchemy ORM models for PostgreSQL persistence.

Separated from domain dataclasses so that importing ExtractedEntities,
Intent, WooAPICall, etc. does NOT pull in Flask, SQLAlchemy, or psycopg2.
"""

import uuid
import copy
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect

from flask import g, has_request_context
from flask_sqlalchemy.session import Session as FSASession

_CONTROL_PLANE_TABLES = frozenset({"tenants", "shopify_tokens", "channel_connections"})

def _targets_control_plane(mapper) -> bool:
    """True if this ORM operation is for a control-plane model"""
    if mapper is None:
        return False
    try:
        m= sa_inspect(mapper)
        return any(t.name in _CONTROL_PLANE_TABLES for t in m.tables)
    except Exception:
        return False

class _TenantRoutingSession(FSASession):
    """
    Routes reads/writes to g.db_engine (the tenant DB bound by
    store_registry.register_before_request) when inside a request context.
    Falls back to the default bind otherwise — background threads (the
    refresh scheduler, migration_runner's throwaway apps) have no request
    context and no g, so they get the engine SQLAlchemy was configured with
    (the control-plane DB), which is exactly where Tenant itself lives.
    """
    def get_bind(self, mapper=None, clause=None, **kwargs):
        if has_request_context() and not _targets_control_plane(mapper):
            engine = g.__dict__.get("db_engine")
            if engine is not None:
                return engine
        return super().get_bind(mapper, clause=clause, **kwargs)


db = SQLAlchemy(session_options={"class_": _TenantRoutingSession})

DEFAULT_CONTEXT = {
    "schema_version": "1.0",
    "carryover_product_id": None,
    "carryover_product_name": None,
    "carryover_search_term": None,
    "carryover_tags": [],
    "carryover_attributes": {},
    "carryover_excluded_tags": [],
    "carryover_excluded_categories": [],
    "carryover_excluded_attributes": {},
    "carryover_quantity": None,
    "carryover_order_id": None,
    "carryover_collection_year": None,
    "carryover_in_stock": None,
    "carryover_on_sale": None,
    "carryover_min_price": None,
    "carryover_max_price": None,
    "cart": [],   # List of {product_id, variation_id, qty, name, price}
}


class Conversation(db.Model):
    __tablename__ = "conversations"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id = db.Column(db.String, nullable=True, index=True)

    flow_state = db.Column(db.String(50), nullable=False, default="idle")
    context_data = db.Column(
        JSONB, nullable=False, default=lambda: copy.deepcopy(DEFAULT_CONTEXT)
    )

    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    messages = db.relationship(
        "Message",
        backref="conversation",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


@event.listens_for(Conversation, "before_update")
def receive_before_update(mapper, connection, target):
    target.updated_at = datetime.now(timezone.utc)


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    conversation_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("conversations.id"),
        nullable=False,
        index=True,
    )

    role = db.Column(db.String(10), nullable=False)
    content = db.Column(db.Text, nullable=False)
    intent = db.Column(db.String(50), nullable=True)
    metadata_json = db.Column(JSONB, nullable=True, default=dict)

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class Tenant(db.Model):
    """
    Control-plane row, one per licensed store. Lives in the MAIN miraq_chat DB
    (NOT in the per-tenant databases). Resolved on every request by license_id.
    woo_secret_encrypted holds a Fernet token (tenant_crypto.py); plaintext
    secret is never persisted.

    ecommerce_backend and shopify_domain exist from Stage 1 onward (all Woo
    tenants at first) so Stage 2 only has to populate them and wire up
    DynamicEndpointsRouter._determine_backend() — not add a migration.

    There are deliberately NO per-tenant Shopify client credentials here.
    SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET identify the MiraQ *app*, not
    the store: one pair issued once in the Partner Dashboard, shared by every
    merchant who installs it, and the key used to verify App Proxy signatures
    and webhook HMACs for all of them. They live in app_config as
    process-wide config. Storing them per tenant would be N copies of one
    secret — rotating it would mean rewriting every row, and any row that had
    not been populated would fail signature verification outright.

    What IS per-tenant is shopify_domain (the `shop` value from the OAuth
    callback) and the access token, which lives in models/shopify_token.py
    keyed by that domain.
    """
    __tablename__ = "tenants"

    tenant_id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    license_id = db.Column(db.String(128), nullable=True, unique=True, index=True)
    plan = db.Column(db.String(20), nullable=False, default="free", index=True)
    db_name = db.Column(db.String(63), nullable=False, unique=True)
    site_domain = db.Column(db.String(255), nullable=True, index=True)

    woo_key = db.Column(db.String(255), nullable=True)
    woo_secret_encrypted = db.Column(db.Text, nullable=True)

    ecommerce_backend = db.Column(db.String(20), nullable=False, default="woocommerce")
    shopify_domain = db.Column(db.String(255), nullable=True, index=True)

    license_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    features = db.Column(JSONB, nullable=False, default=dict)

    last_build_error = db.Column(db.Text, nullable=True)
    schema_migrated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    build_attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    archived_at = db.Column(db.DateTime(timezone=True), nullable=True)
    wp_base_url = db.Column(db.String(500), nullable=True)

    # ── Widget branding (logo/header text) — cached, not fetched live ──
    widget_logo_url = db.Column(db.Text, nullable=True)
    widget_header_text = db.Column(db.Text, nullable=True)
    widget_config_fetched_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return (
            f"<Tenant tenant_id={self.tenant_id!r} license_id={self.license_id!r} "
            f"db_name={self.db_name!r} status={self.status!r}>"
        )

    @property
    def is_active(self) -> bool:
        if self.status != "active":
            return False
        if self.license_expires_at is None:
            return True
        return datetime.now(timezone.utc) < self.license_expires_at