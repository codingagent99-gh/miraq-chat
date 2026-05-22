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

db = SQLAlchemy()

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