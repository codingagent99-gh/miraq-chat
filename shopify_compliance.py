"""
shopify_compliance.py — Shopify's mandatory privacy (compliance) webhooks.

Every App Store app must answer these three, or it is rejected in review:

  customers/data_request  A customer asked the store what data it holds on
                          them. We gather what MiraQ holds (their chat
                          conversations and messages, and chat order
                          confirmations) into an export file for the merchant.
  customers/redact        Delete a customer's data. We delete their
                          conversations, messages and chat order confirmations
                          from the store's own database.
  shop/redact             Sent 48 hours after the app is uninstalled. We
                          delete everything MiraQ holds for the shop: its
                          database, Admin API token, channel connections,
                          catalog snapshot and widget branding.

What MiraQ holds per customer, and where
  conversations.customer_id  the Shopify customer ID (numeric, or a GID)
  messages                   everything said in those conversations
  shopify_order_confirmations  order IDs matched to a chat session
  channel_links              Instagram/WhatsApp users signed in as this customer
  Guest conversations carry no customer ID, so they cannot be tied to a
  person and are not part of a customer's data here.

Signature verification and the 401 on a bad HMAC happen in the route
(routes/shopify.py /events/compliance) before any of this runs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from flask import current_app

from chat_logger import get_logger

logger = get_logger("miraq_chat")

# Where customers/data_request exports are written for the merchant. Personal
# data: kept outside any web-served directory, files created owner-read-only.
COMPLIANCE_EXPORT_DIR = os.getenv("COMPLIANCE_EXPORT_DIR", "compliance_exports")


# ── helpers ──────────────────────────────────────────────────────────────────

def _customer_ids(payload: dict) -> set:
    """Every form a customer ID can take in conversations.customer_id."""
    cid = str(((payload.get("customer") or {}).get("id")) or "").strip()
    if not cid:
        return set()
    numeric = cid.rsplit("/", 1)[-1]
    return {numeric, f"gid://shopify/Customer/{numeric}"}


def _order_ids(raw_ids) -> set:
    ids = set()
    for raw in raw_ids or []:
        numeric = str(raw).rsplit("/", 1)[-1].strip()
        if numeric:
            ids.update({numeric, f"gid://shopify/Order/{numeric}"})
    return ids


def _tenant_database_exists(tenant) -> bool:
    """False once the store's database has been dropped (after uninstall)."""
    from tenant_db_provisioner import list_existing_databases
    base_dsn = current_app.config["SQLALCHEMY_DATABASE_URI"]
    return bool(tenant.db_name) and tenant.db_name in list_existing_databases(base_dsn)


def _bind(tenant) -> bool:
    """Point per-store models at this store's database; False if it's gone."""
    if not _tenant_database_exists(tenant):
        return False
    from store_registry import bind_tenant_db
    import channel_link
    bind_tenant_db(tenant)
    channel_link.ensure_tables(tenant)  # older stores: link tables may not exist yet
    return True


# ── customers/data_request ───────────────────────────────────────────────────

def export_customer_data(tenant, payload: dict) -> dict:
    """Write everything MiraQ holds on this customer to an export file.

    Shopify expects the data to reach the merchant within 30 days. The file
    is written now, and an ERROR-level log line tells support to send it.
    """
    from models import db, Conversation, Message
    from models.shopify_order_confirmation import ShopifyOrderConfirmation

    ids = _customer_ids(payload)
    request_id = str(((payload.get("data_request") or {}).get("id")) or "unknown")
    export = {
        "shop_domain": tenant.shopify_domain,
        "data_request_id": request_id,
        "customer_id": sorted(ids)[0] if ids else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "conversations": [],
        "order_confirmations": [],
        "channel_links": [],
    }

    if ids and _bind(tenant):
        conversations = Conversation.query.filter(Conversation.customer_id.in_(ids)).all()
        for conv in conversations:
            messages = (Message.query.filter_by(conversation_id=conv.id)
                        .order_by(Message.created_at.asc()).all())
            export["conversations"].append({
                "session_id": str(conv.id),
                "started_at": conv.created_at.isoformat() if getattr(conv, "created_at", None) else None,
                "messages": [
                    {"role": m.role, "content": m.content,
                     "at": m.created_at.isoformat() if getattr(m, "created_at", None) else None}
                    for m in messages
                ],
            })
        sessions = [str(c.id) for c in conversations]
        order_ids = _order_ids(payload.get("orders_requested"))
        rows = ShopifyOrderConfirmation.query.filter(
            (ShopifyOrderConfirmation.session_id.in_(sessions)) |
            (ShopifyOrderConfirmation.order_id.in_(order_ids))
        ).all() if (sessions or order_ids) else []
        export["order_confirmations"] = [
            {"session_id": r.session_id, "order_id": r.order_id, "order_number": r.order_number}
            for r in rows
        ]
        from models.channel_link import ChannelLink
        export["channel_links"] = [
            {"channel": l.channel, "channel_user_id": l.channel_user_id,
             "linked_at": l.linked_at.isoformat() if l.linked_at else None,
             "expires_at": l.expires_at.isoformat() if l.expires_at else None}
            for l in ChannelLink.query.filter(ChannelLink.customer_id.in_(ids)).all()
        ]
        db.session.rollback()  # read-only; release the connection cleanly

    folder = os.path.join(COMPLIANCE_EXPORT_DIR, tenant.shopify_domain or "unknown-shop")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = os.path.join(folder, f"data_request_{request_id}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)

    counts = {"conversations": len(export["conversations"]),
              "order_confirmations": len(export["order_confirmations"]),
              "channel_links": len(export["channel_links"])}
    # ERROR level on purpose: a person must send this file to the merchant
    # within 30 days. No personal data in the log line itself.
    logger.error(
        f"compliance: customers/data_request export ready — send to the merchant "
        f"within 30 days | shop={tenant.shopify_domain} | request={request_id} | "
        f"file={path} | {counts}"
    )
    return {"exported": counts, "file": path}


# ── customers/redact ─────────────────────────────────────────────────────────

def redact_customer(tenant, payload: dict) -> dict:
    """Delete this customer's conversations, messages and order confirmations."""
    from models import db, Conversation, Message
    from models.shopify_order_confirmation import ShopifyOrderConfirmation

    ids = _customer_ids(payload)
    if not ids or not _bind(tenant):
        logger.info(f"compliance: customers/redact — nothing held | shop={tenant.shopify_domain}")
        return {"deleted": {"conversations": 0, "messages": 0, "order_confirmations": 0, "channel_links": 0}}

    try:
        conversations = Conversation.query.filter(Conversation.customer_id.in_(ids)).all()
        sessions = [str(c.id) for c in conversations]
        conv_ids = [c.id for c in conversations]

        messages = (Message.query.filter(Message.conversation_id.in_(conv_ids))
                    .delete(synchronize_session=False)) if conv_ids else 0
        order_ids = _order_ids(payload.get("orders_to_redact"))
        confirmations = (ShopifyOrderConfirmation.query.filter(
            (ShopifyOrderConfirmation.session_id.in_(sessions)) |
            (ShopifyOrderConfirmation.order_id.in_(order_ids))
        ).delete(synchronize_session=False)) if (sessions or order_ids) else 0
        from models.channel_link import ChannelLink
        links = (ChannelLink.query.filter(ChannelLink.customer_id.in_(ids))
                 .delete(synchronize_session=False))
        for conv in conversations:
            db.session.delete(conv)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    deleted = {"conversations": len(conversations), "messages": messages,
               "order_confirmations": confirmations, "channel_links": links}
    logger.info(f"compliance: customers/redact done | shop={tenant.shopify_domain} | {deleted}")
    return {"deleted": deleted}


# ── shop/redact ──────────────────────────────────────────────────────────────

def redact_shop(tenant) -> dict:
    """Delete everything MiraQ holds for an uninstalled shop.

    Only for an ARCHIVED tenant: shop/redact follows an uninstall, and an
    active tenant means the merchant reinstalled — its live data must stay.
    The tenant row itself is kept, stripped of branding and settings, as the
    audit record that the redaction happened.
    """
    from models import db, ChannelConnection
    from models.shopify_token import ShopifyToken
    from store_registry import get_engine_registry
    from tenant_db_provisioner import drop_tenant_database
    from tenant_snapshot_store import snapshot_store

    if tenant.status != "archived":
        logger.warning(
            f"compliance: shop/redact for a tenant that is {tenant.status!r} "
            f"(reinstalled?) — nothing deleted | shop={tenant.shopify_domain}"
        )
        return {"skipped": f"tenant_{tenant.status}"}

    done = {}

    # 1. The store's own database: conversations, messages, usage, confirmations.
    if _tenant_database_exists(tenant):
        registry = get_engine_registry()
        if registry is not None:
            registry.dispose_for(tenant.db_name)  # no pooled connections during DROP
        drop_tenant_database(current_app.config["SQLALCHEMY_DATABASE_URI"], tenant.db_name)
        done["database"] = "dropped"
    else:
        done["database"] = "already gone"

    # 2. Control-plane rows and the catalog snapshot.
    try:
        done["admin_tokens"] = (ShopifyToken.query
                                .filter_by(store_domain=tenant.shopify_domain)
                                .delete(synchronize_session=False))
        done["channel_connections"] = (ChannelConnection.query
                                       .filter_by(tenant_id=tenant.tenant_id)
                                       .delete(synchronize_session=False))
        tenant.widget_logo_url = None
        tenant.widget_header_text = None
        tenant.features = {"shop_redacted_at": datetime.now(timezone.utc).isoformat()}
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    snapshot_store.delete(str(tenant.tenant_id))
    done["snapshot"] = "deleted"

    logger.info(f"compliance: shop/redact done | shop={tenant.shopify_domain} | {done}")
    return {"deleted": done}
