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

from flask import g, has_request_context
from flask_sqlalchemy.session import Session as FSASession

class _TenantRoutingSession(FSASession):
    def get_bind(self, *args, **kwargs):
        if has_request_context():
            engine = g.__dict__.get("db_engine")
            if engine is not None:
                return engine
        return super().get_bind(*args, **kwargs)

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
    """
    __tablename__ = "tenants"

    tenant_id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    license_id = db.Column(db.String(128), nullable=True, unique=True, index=True)
    plan = db.Column(db.String(20), nullable=False, default="free", index=True)
    db_name = db.Column(db.String(63), nullable=False, unique=True)
    site_domain = db.Column(db.String(255), nullable=True, index=True)

    woo_key = db.Column(db.String(255), nullable=True)
    woo_secret_encrypted = db.Column(db.Text, nullable=True)

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
    
    
class CatalogSnapshot(db.Model):
    """
    Control-plane row, one per catalog push received from the WordPress
    plugin (see handlers/catalog_push.py). Lives in the MAIN miraq_chat DB
    alongside Tenant — NOT in the per-tenant databases.

    Phase 1: rows are written here and nothing else reads them. A later
    phase teaches TenantRegistry to prefer the latest row here over a live
    WooCommerce pull (TenantRegistry.apply_pushed_catalog() already exists
    for that — this table just isn't wired to it yet).
    """
    __tablename__ = "catalog_snapshots"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("tenants.tenant_id"),
        nullable=False,
        index=True,
    )

    payload = db.Column(JSONB, nullable=False)
    product_count = db.Column(db.Integer, nullable=True)
    payload_bytes = db.Column(db.Integer, nullable=True)

    received_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def __repr__(self):
        return (
            f"<CatalogSnapshot tenant_id={self.tenant_id!r} "
            f"received_at={self.received_at!r} products={self.product_count}>"
        )