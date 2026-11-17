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

Instagram rows also keep the Page access token MiraQ received when the
merchant connected (encrypted with tenant_crypto, as
shopify_apps.client_secret_encrypted is). The webhook service fetches it per
account (GET /channel-connections/token) to send replies, so each store
replies from its own account. token_status turns "invalid" when Meta rejects
the token; the admin then asks the merchant to reconnect.

`settings` holds the merchant's per-channel switches (DEFAULT_SETTINGS);
/chat/channel honours them before running a turn.

Columns added after the table first shipped are added at startup by
ensure_columns(): create_all never alters an existing table, and the control
plane has no migration tool.
"""

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from cryptography.fernet import InvalidToken
from sqlalchemy.dialects.postgresql import UUID

from models.db_models import db

# The merchant's switches. Missing keys read as these defaults.
#   ai_replies  False pauses MiraQ on this account: messages get no reply.
#   text_only   Messages with no letters or digits (emoji only) get no reply.
DEFAULT_SETTINGS = {"ai_replies": True, "text_only": True}


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

    # Instagram: the Facebook Page linked to the account, who connected it, and
    # the Page token (ciphertext — use the access_token property).
    page_id                = db.Column(db.String(64),  nullable=True)
    page_name              = db.Column(db.String(255), nullable=True)
    connected_by           = db.Column(db.String(255), nullable=True)
    access_token_encrypted = db.Column(db.Text,        nullable=True)
    token_status           = db.Column(db.String(20),  nullable=True)   # "ok" | "invalid"
    token_updated_at       = db.Column(db.DateTime(timezone=True), nullable=True)

    settings = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    # ── Page token ──

    @property
    def access_token(self):
        """The decrypted Page token, or None (none saved, or it can't be decrypted)."""
        if not self.access_token_encrypted:
            return None
        from tenant_crypto import decrypt_secret
        try:
            return decrypt_secret(self.access_token_encrypted)
        except (InvalidToken, ValueError, TypeError):
            return None

    def set_access_token(self, token: str) -> None:
        from tenant_crypto import encrypt_secret
        self.access_token_encrypted = encrypt_secret(token) if token else None
        self.token_status = "ok" if token else None
        self.token_updated_at = _now()

    @property
    def needs_reconnect(self) -> bool:
        """Instagram can't reply: no token saved, or Meta rejected it."""
        return self.channel == "instagram" and (
            not self.access_token_encrypted or self.token_status == "invalid"
        )

    # ── Settings ──

    def get_settings(self) -> dict:
        saved = self.settings if isinstance(self.settings, dict) else {}
        return {k: bool(saved.get(k, default)) for k, default in DEFAULT_SETTINGS.items()}

    def update_settings(self, changes: dict) -> None:
        """Apply known boolean switches; unknown keys are ignored."""
        merged = self.get_settings()
        for key, value in (changes or {}).items():
            if key in DEFAULT_SETTINGS and isinstance(value, bool):
                merged[key] = value
        self.settings = merged   # a new dict, so SQLAlchemy sees the change

    def to_dict(self) -> dict:
        # Never includes the token.
        return {
            "channel": self.channel,
            "external_account_id": self.external_account_id,
            "status": self.status,
            "display_name": self.display_name,
            "page_id": self.page_id,
            "page_name": self.page_name,
            "has_token": bool(self.access_token_encrypted),
            "token_status": self.token_status,
            "settings": self.get_settings(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return (f"<ChannelConnection {self.channel}:{self.external_account_id} "
                f"tenant={self.tenant_id} status={self.status}>")


# Columns this model gained after the table first shipped (Nov 2026).
_ADDED_COLUMNS = (
    ("page_id", "VARCHAR(64)"),
    ("page_name", "VARCHAR(255)"),
    ("connected_by", "VARCHAR(255)"),
    ("access_token_encrypted", "TEXT"),
    ("token_status", "VARCHAR(20)"),
    ("token_updated_at", "TIMESTAMP WITH TIME ZONE"),
    ("settings", "JSON"),
)


def ensure_columns(engine) -> list:
    """
    Add any of _ADDED_COLUMNS the table lacks. Runs at startup after
    create_all; does nothing once they exist. IF NOT EXISTS keeps several
    workers starting at once safe on PostgreSQL. Returns the names added.
    """
    existing = {c["name"] for c in sa.inspect(engine).get_columns(ChannelConnection.__tablename__)}
    missing = [(name, ddl) for name, ddl in _ADDED_COLUMNS if name not in existing]
    if not missing:
        return []
    if_not_exists = "IF NOT EXISTS " if engine.dialect.name == "postgresql" else ""
    with engine.begin() as conn:
        for name, ddl in missing:
            conn.execute(sa.text(
                f"ALTER TABLE {ChannelConnection.__tablename__} ADD COLUMN {if_not_exists}{name} {ddl}"
            ))
    return [name for name, _ in missing]
