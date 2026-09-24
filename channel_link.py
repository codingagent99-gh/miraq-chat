"""
channel_link.py — one-time customer sign-in for channel users (Instagram,
WhatsApp) on Shopify stores.

A channel only tells us the sender's channel ID, never an email, so a guest
who asks for their orders proves who they are ONCE with the store's own
Shopify customer login. MiraQ then remembers channel user → Shopify customer
in that store's database (models/channel_link.py) and keeps no Shopify token.

Flow
  1. Guest asks for orders → create_link_request() → Sign in button with
     {base}/channel-link/start?s=<state>
  2. /channel-link/start  shows "You're linking this chat to <store>" and a
     Continue button to Shopify's authorize endpoint (PKCE, S256).
  3. Shopify → /channel-link/shopify/callback?code&state
     complete_sign_in(): token exchange (public client, no secret), then
     Customer Account API `customer { id emailAddress }`.
  4. save_link() stores the customer ID; the customer's next message starts
     with "Signed in as r***@…. Not you? Reply unlink."

Shopify specifics (Customer Account API docs):
  - Endpoints are discovered from the storefront domain:
    /.well-known/openid-configuration and /.well-known/customer-account-api.
  - An app authenticating with its own client ID (the app config's
    [customer_authentication] redirect_uris) is a PUBLIC client: PKCE, no
    client secret, no refresh token. Fine here — we only need the identity once.
  - The authorize endpoint can't load in an iframe; the Continue button is a
    normal top-level link, which is what Instagram's in-app browser opens.

The state value is "<tenant id hex>.<random>": the tenant part lets the
callback find the right store database; the random part (stored hashed,
single use, 10 minutes) is what authenticates it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import requests as http_requests
from flask import request
from sqlalchemy.exc import IntegrityError

from chat_logger import get_logger

logger = get_logger("miraq_chat")

CHANNEL_LINK_TTL_DAYS = int(os.getenv("CHANNEL_LINK_TTL_DAYS", "90"))
LINK_REQUEST_MINUTES = 10
CUSTOMER_SCOPE = "openid email customer-account-api:full"
CALLBACK_PATH = "/channel-link/shopify/callback"
START_PATH = "/channel-link/start"

# Messages that sign the customer out of this chat (compared lower-cased, trimmed).
UNLINK_COMMANDS = frozenset({"unlink", "log out", "logout", "sign out", "signout"})

_HTTP_TIMEOUT = 10
_DISCOVERY_TTL_S = 3600


class ChannelLinkError(Exception):
    """A sign-in step failed; `user_message` is safe to show the customer."""

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(detail or user_message)
        self.user_message = user_message


def _utcnow():
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def public_base_url() -> str:
    from routes.shopify_oauth import SHOPIFY_APP_BASE_URL
    return SHOPIFY_APP_BASE_URL or request.url_root.rstrip("/")


def callback_url() -> str:
    return public_base_url() + CALLBACK_PATH


def mask_email(email: str) -> str:
    local, sep, domain = (email or "").partition("@")
    if not sep:
        return ""
    return f"{local[:1]}***@{domain}"


# ── Tables in existing store databases ───────────────────────────────────────

_ensured: set = set()
_ensure_lock = threading.Lock()


def ensure_tables(tenant) -> None:
    """Create the two link tables in this store's database if missing.

    New stores get them at provisioning (routes/provisioning._per_tenant_tables);
    stores provisioned earlier get them here, on first use, once per process.
    """
    if not tenant or not tenant.db_name or tenant.db_name in _ensured:
        return
    from models import db
    from models.channel_link import ChannelLink, ChannelLinkRequest
    from store_registry import get_engine_registry
    with _ensure_lock:
        if tenant.db_name in _ensured:
            return
        engine = get_engine_registry().get_engine(tenant.db_name)
        db.metadata.create_all(bind=engine, tables=[ChannelLink.__table__, ChannelLinkRequest.__table__],
                               checkfirst=True)
        _ensured.add(tenant.db_name)


# ── Links ────────────────────────────────────────────────────────────────────

def find_link(channel: str, channel_user_id: str):
    """The valid link for this channel user in the bound store, or None."""
    from models.channel_link import ChannelLink
    link = ChannelLink.query.filter_by(channel=channel, channel_user_id=channel_user_id).first()
    return link if link is not None and link.is_valid() else None


def save_link(channel: str, channel_user_id: str, customer_id: str, email: str):
    """Create or replace this channel user's link (one per user per store)."""
    from models import db
    from models.channel_link import ChannelLink
    now = _utcnow()
    link = ChannelLink.query.filter_by(channel=channel, channel_user_id=channel_user_id).first()
    replaced = link is not None and link.customer_id != customer_id
    if link is None:
        link = ChannelLink(channel=channel, channel_user_id=channel_user_id)
        db.session.add(link)
    link.customer_id = customer_id
    link.email_display = mask_email(email)
    link.linked_at = now
    link.expires_at = now + timedelta(days=CHANNEL_LINK_TTL_DAYS)
    link.notice_pending = True
    try:
        db.session.commit()
    except IntegrityError:
        # A parallel callback for the same user won the insert; update that row.
        db.session.rollback()
        return save_link(channel, channel_user_id, customer_id, email)
    logger.info(
        f"channel_link: linked | {channel} user → customer {customer_id}"
        + (" (replaced a different customer)" if replaced else "")
    )
    return link


def unlink(channel: str, channel_user_id: str) -> bool:
    from models import db
    from models.channel_link import ChannelLink
    n = (ChannelLink.query.filter_by(channel=channel, channel_user_id=channel_user_id)
         .delete(synchronize_session=False))
    db.session.commit()
    if n:
        logger.info(f"channel_link: unlinked | {channel} user")
    return bool(n)


# ── Link requests ────────────────────────────────────────────────────────────

def create_link_request(tenant, channel: str, channel_user_id: str, conversation_id) -> str:
    """Store a pending sign-in and return the Sign in URL for the button."""
    from models import db
    from models.channel_link import ChannelLinkRequest
    from tenant_crypto import encrypt_secret

    state = f"{uuid.UUID(str(tenant.tenant_id)).hex}.{secrets.token_urlsafe(32)}"
    verifier = secrets.token_urlsafe(48)  # 64 chars, within PKCE's 43–128
    db.session.add(ChannelLinkRequest(
        state_hash=_hash(state),
        channel=channel,
        channel_user_id=channel_user_id,
        conversation_id=conversation_id,
        code_verifier_encrypted=encrypt_secret(verifier),
        expires_at=_utcnow() + timedelta(minutes=LINK_REQUEST_MINUTES),
    ))
    db.session.commit()
    return f"{public_base_url()}{START_PATH}?s={state}"


def tenant_from_state(state: str):
    """The store named in the state's first part (not proof of anything by itself)."""
    from models import Tenant
    head, sep, _rest = (state or "").partition(".")
    if not sep:
        return None
    try:
        return Tenant.query.get(uuid.UUID(hex=head))
    except (ValueError, AttributeError):
        return None


def pending_request(state: str):
    """The unused, unexpired request for this state in the bound store, or None."""
    from models.channel_link import ChannelLinkRequest
    row = ChannelLinkRequest.query.filter_by(state_hash=_hash(state)).first()
    if row is None or row.used_at is not None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    return row if expires > _utcnow() else None


def consume_request(state: str):
    """Mark the request used — atomically, so a replayed callback gets nothing."""
    from models import db
    from models.channel_link import ChannelLinkRequest
    now = _utcnow()
    n = (ChannelLinkRequest.query
         .filter(ChannelLinkRequest.state_hash == _hash(state),
                 ChannelLinkRequest.used_at.is_(None),
                 ChannelLinkRequest.expires_at > now)
         .update({ChannelLinkRequest.used_at: now}, synchronize_session=False))
    db.session.commit()
    if n != 1:
        return None
    return ChannelLinkRequest.query.filter_by(state_hash=_hash(state)).first()


# ── Shopify customer login ───────────────────────────────────────────────────

_discovery_cache: dict = {}


def _discover(shop_domain: str, path: str) -> dict:
    key = (shop_domain, path)
    hit = _discovery_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        resp = http_requests.get(f"https://{shop_domain}{path}", timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise ChannelLinkError("We couldn't reach the store's sign-in right now. Please try again.",
                               f"discovery {path} failed for {shop_domain}: {e}")
    _discovery_cache[key] = (time.monotonic() + _DISCOVERY_TTL_S, data)
    return data


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")  # no padding


def authorize_url(tenant, state: str, request_row) -> str:
    from urllib.parse import urlencode
    from app_config import SHOPIFY_CLIENT_ID
    from tenant_crypto import decrypt_secret
    config = _discover(tenant.shopify_domain, "/.well-known/openid-configuration")
    endpoint = config.get("authorization_endpoint")
    if not endpoint or not SHOPIFY_CLIENT_ID:
        raise ChannelLinkError("Sign-in isn't available for this store right now.",
                               "no authorization_endpoint or SHOPIFY_CLIENT_ID")
    params = {
        "client_id": SHOPIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": callback_url(),
        "scope": CUSTOMER_SCOPE,
        "state": state,
        "code_challenge": _code_challenge(decrypt_secret(request_row.code_verifier_encrypted)),
        "code_challenge_method": "S256",
    }
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}"


def complete_sign_in(tenant, request_row, code: str) -> Tuple[str, str]:
    """Exchange the code and read the signed-in customer. Returns (customer_id, email)."""
    from app_config import SHOPIFY_CLIENT_ID
    from tenant_crypto import decrypt_secret

    config = _discover(tenant.shopify_domain, "/.well-known/openid-configuration")
    try:
        token_resp = http_requests.post(config["token_endpoint"], data={
            "grant_type": "authorization_code",
            "client_id": SHOPIFY_CLIENT_ID,
            "redirect_uri": callback_url(),
            "code": code,
            "code_verifier": decrypt_secret(request_row.code_verifier_encrypted),
        }, timeout=_HTTP_TIMEOUT)
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]
    except Exception as e:
        raise ChannelLinkError("Sign-in didn't complete. Please go back to the chat and try again.",
                               f"token exchange failed: {e}")

    api = _discover(tenant.shopify_domain, "/.well-known/customer-account-api")
    try:
        resp = http_requests.post(
            api["graphql_api"],
            json={"query": "query { customer { id emailAddress { emailAddress } } }"},
            headers={"Content-Type": "application/json", "Authorization": access_token},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        customer = ((resp.json() or {}).get("data") or {}).get("customer") or {}
    except Exception as e:
        raise ChannelLinkError("We couldn't read your account. Please try again.",
                               f"customer query failed: {e}")
    # The token is deliberately not kept: the link needs only the verified ID.
    gid = str(customer.get("id") or "")
    customer_id = gid.rsplit("/", 1)[-1]
    if not customer_id.isdigit():
        raise ChannelLinkError("We couldn't read your account. Please try again.",
                               f"unexpected customer id {gid!r}")
    email = ((customer.get("emailAddress") or {}).get("emailAddress")) or ""
    return customer_id, email
