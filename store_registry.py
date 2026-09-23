"""
store_registry.py — Tenant resolution, per-request loader + DB-engine binding.

Resolution order on each request (before_request):
  1. Exempt path?             → skip tenant binding entirely.
  2. X-MiraQ-License-Id set?  → look up tenant, bind loader + engine, or 4xx.
  3. Signed App Proxy request → resolve by the signed `shop` parameter. This
     is how a Shopify STOREFRONT identifies itself: the theme app extension
     has no licence id to send (see extensions/.../miraq_widget.liquid — it
     passes only shop domain and the proxy path), and Shopify signs `shop`,
     `timestamp` and `signature` into every proxied request. Without this
     every chat message from a Shopify store 400s.
  4. Neither                  → 400. No fallback to a default tenant — every
      request must identify itself once this is registered.

get_store_loader() returns g.store_loader within a request, else None
(startup / background threads have no request context).
"""

from __future__ import annotations
import os
from datetime import datetime, timezone
from flask import g, request, jsonify

from chat_logger import get_logger
from models.db_models import db

logger = get_logger("miraq_chat")

_LICENSE_HEADER = "X-MiraQ-License-Id"

# Routes exempt from tenant resolution — no X-MiraQ-License-Id required.
# These are server-level or pre-tenant endpoints.
#
# /provision-tenant, /activate-free, /deactivate-tenant create or tear down
# the very tenant row that resolution would otherwise need — see
# routes/provisioning.py and routes/deactivation.py. Each verifies its own
# licence signature instead (or is deliberately unauthenticated for now, in
# activate-free's case — see that route's docstring).
# /customer-addresses, /events/product-update, /events/order-paid and
# /events/app-uninstalled (Stage 2, routes/shopify.py) are Shopify-native mechanism
# webhooks — that carry their own signed tenant identifier (a `shop` query
# param or a Shopify-Shop-Domain header) and verify it themselves. Shopify
# has no way to send X-MiraQ-License-Id, so these must not be gated behind
# it either.
#
# The Shopify install flow (routes/shopify_oauth.py) is exempt for a different
# reason: it runs BEFORE a tenant exists — the callback is what creates it —
# and authenticates with the app-level query hmac plus a signed state nonce.
# The client_credentials grant in store_loader/shopify_token_manager.py cannot
# reach stores outside our own Partner organisation, which is why a
# distributable app needs this authorization-code callback at all.
_EXEMPT_PATHS = {
    "/provision-tenant", "/activate-free", "/deactivate-tenant",
    "/customer-addresses", "/events/product-update", "/events/order-paid",
    "/events/app-uninstalled",
    # Shopify install flow (routes/shopify_oauth.py). These run BEFORE a
    # tenant exists — the callback is what creates it — and authenticate with
    # the app-level hmac + state nonce instead.
    "/shopify/install", "/shopify/auth/callback", "/installed",
    # Messaging channels (routes/channel.py). Called server-to-server by the
    # webhook service, which has no licence id to send: /chat/channel resolves
    # its tenant from the WhatsApp/Instagram account the customer messaged
    # (channel_connections) and binds it with bind_tenant_db, the same way the
    # Shopify webhooks above do. Both authenticate with X-MiraQ-Channel-Key.
    "/chat/channel", "/channel-connections",
}


# Paths where the tenant header is OPTIONAL. With it, the tenant is bound
# (so /health and /status describe THAT store); without it, or with a tenant
# that cannot be bound, the request still goes through unbound — a health
# probe must never 4xx. Previously these were fully exempt, so the loader was
# always None and /health reported every store as "down" (503, blocking).
#
# /shopify-token-status is intentionally NOT here any more: it is a per-store
# diagnostic and now requires the header like any other tenant route.
_OPTIONAL_TENANT_PATHS = {"/health", "/status"}
_EXEMPT_PREFIXES = ("/static/",)

_tenant_registry = None
_engine_registry = None


def get_store_loader():
    """Request-scoped loader. Returns None outside a request context."""
    try:
        return g.store_loader
    except (RuntimeError, AttributeError):
        return None


def set_store_loader(loader):
    """
    No-op shim kept during the refactor in case anything outside this
    module's own call sites still imports it. server.py's initialize_store()
    no longer calls this — loaders are resolved per-request via
    get_loader() instead. Delete once nothing references it.
    """
    pass


def get_tenant_features() -> dict:
    try:
        tenant = g.__dict__.get("tenant")
        if tenant is not None:
            # Access features eagerly and store as plain dict on g
            # to avoid SQLAlchemy lazy-load failures after session operations
            features = g.__dict__.get("tenant_features")
            if features is None:
                features = dict(tenant.features or {})
                g.tenant_features = features
            return features
    except (RuntimeError, Exception):
        pass
    return {}


# ── Sales tools (store-specific: rep ordering, bulk orders, order reporting) ──
# Built for one store's workflow — its custom checkout fields (Order Type,
# Project Name, Your Rep), orders credited to reps via _billing_project_rep,
# and "every product is a sample" counting. Off unless the tenant row opts in:
#   UPDATE tenants SET features = features || '{"sales_tools": true}'::jsonb
#   WHERE license_id = '<that store>';
# Off means: no bulk-order / order-report routing, and staff roles are served
# as a customer (see effective_role), so no store gets another store's flows.

_CUSTOMER_ROLES = frozenset({"", "customer", "guest"})


def sales_tools_enabled() -> bool:
    return bool(get_tenant_features().get("sales_tools"))


def effective_role(role) -> str:
    """The role the chat should act on for this tenant.

    With sales tools off, any staff role (administrator, sales_rep, cs_rep, …)
    is served as "customer": every rep/admin chat flow — ordering on behalf of
    someone, store-wide order lists, reports — belongs to those tools. The
    person's real WordPress role is untouched; this only decides which chat
    features they get.
    """
    role = (role or "").strip()
    if role in _CUSTOMER_ROLES or sales_tools_enabled():
        return role
    return "customer"


def init_registries(tenant_registry, engine_registry) -> None:
    """Called once at startup, after the registries are constructed."""
    global _tenant_registry, _engine_registry
    _tenant_registry = tenant_registry
    _engine_registry = engine_registry


def get_engine_registry():
    return _engine_registry


def get_tenant_registry():
    """Public accessor for the process-wide TenantRegistry, set at startup."""
    return _tenant_registry


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)

def bind_tenant_db(tenant) -> None:
    """
    Bind a tenant resolved by some OTHER means than X-MiraQ-License-Id (e.g.
    a Shopify webhook's signed shop domain) so that per-tenant models
    (Conversation, Message, ChatUsage, ShopifyOrderConfirmation) are read and
    written in THAT tenant's database.

    Exempt routes skip _resolve_tenant, so without this g.db_engine is unset
    and _TenantRoutingSession falls back to the control-plane database —
    which is how /events/order-paid used to write confirmations where
    /chat/order-status (tenant DB) could never see them.

    Does not build a StoreLoader: webhook handlers that need one should call
    get_tenant_registry().get_loader(tenant) themselves.
    """
    g.tenant = tenant
    g.tenant_features = dict(tenant.features or {})
    g.ecommerce_backend = tenant.ecommerce_backend
    g.db_engine = _engine_registry.get_engine(tenant.db_name)

def _tenant_from_app_proxy():
    """
    Resolve a tenant from a Shopify App Proxy request, or None.

    The `shop` parameter alone is attacker-suppliable, so it only decides WHOSE
    signature to check; the signature itself — HMAC-SHA256 over the query
    string, keyed with the app-level client secret — is the authentication.
    Same scheme /customer-addresses already uses, applied to every route.

    Returns None (never an error response) when this is not a proxied request,
    so the caller can fall through to the usual missing-header 400.
    """
    args = request.args
    if not args.get("signature") or not args.get("shop"):
        return None

    from app_config import SHOPIFY_CLIENT_SECRET, SHOPIFY_PROXY_MAX_AGE
    from ecommerce.shopify_proxy import verify_app_proxy_signature

    ok, reason = verify_app_proxy_signature(
        args.to_dict(flat=True),
        SHOPIFY_CLIENT_SECRET,
        max_age_seconds=SHOPIFY_PROXY_MAX_AGE,
    )
    if not ok:
        logger.warning(f"App Proxy signature rejected | shop={args.get('shop')!r} | reason={reason}")
        return None

    from models import Tenant
    shop = (args.get("shop") or "").strip().lower()
    tenant = Tenant.query.filter_by(shopify_domain=shop).first()
    if tenant is None:
        logger.warning(f"App Proxy request for an unknown shop | shop={shop!r}")
        return None

    logger.info(f"Tenant resolved via App Proxy | shop={shop} tenant_id={tenant.tenant_id}")
    return tenant


def _bind_optional_tenant(license_id: str) -> None:
    """
    Best-effort binding for _OPTIONAL_TENANT_PATHS. Never returns an error
    response and never triggers a catalog rehydrate — a health poll must not
    be the thing that makes a cold tenant spend seconds rebuilding. Uses the
    tenant's loader only if it is already resident.
    """
    g.store_loader = None
    try:
        from models import Tenant
        if license_id:
            tenant = Tenant.query.filter_by(license_id=license_id).first()
        else:
            # A Shopify storefront polls /health through the App Proxy, so it
            # identifies itself by signed `shop` rather than a header — same
            # as every other route. Without this its health checks were never
            # bound and always reported the store as "unknown".
            tenant = _tenant_from_app_proxy()
        if tenant is None:
            return
        g.tenant = tenant
        g.tenant_features = dict(tenant.features or {})
        g.ecommerce_backend = tenant.ecommerce_backend
        if tenant.status in ("active", "provision_failed") and _tenant_registry is not None:
            resident = dict(_tenant_registry.resident_loaders())
            g.store_loader = resident.get(str(tenant.tenant_id))
    except Exception as e:
        logger.warning(f"_bind_optional_tenant: could not bind | license_id={(license_id or '')[:8]!r} | {e}")
        g.store_loader = None

def register_before_request(app) -> None:
    @app.before_request
    def _resolve_tenant():
        if request.method == "OPTIONS":
            return None  # handled by cors_manager's own before_request handler,
                         # registered before this one in server.py

        path = request.path

        if _is_exempt(path):
            g.store_loader = None
            return None

        license_id = request.headers.get(_LICENSE_HEADER, "").strip()

        if path in _OPTIONAL_TENANT_PATHS:
            _bind_optional_tenant(license_id)
            return None

        logger.info(f"_resolve_tenant: path={path} | license_id={'present:'+license_id[:8] if license_id else 'MISSING'}")

        # ── Resolve the tenant: licence header, else signed App Proxy ────────
        if license_id:
            from models import Tenant
            tenant = Tenant.query.filter_by(license_id=license_id).first()
            if tenant is None:
                logger.warning(f"Unknown license_id={license_id!r} on {path} → 404")
                return jsonify({"success": False, "error": "unknown tenant"}), 404
        else:
            tenant = _tenant_from_app_proxy()
            if tenant is None:
                logger.warning(f"No tenant header and no valid App Proxy signature on {path} → 400")
                return jsonify({"success": False, "error": "missing tenant"}), 400
            license_id = tenant.license_id or ""

        # Auto-mark expired tenants before checking is_active.
        if (tenant.status == "active"
                and tenant.license_expires_at is not None
                and datetime.now(timezone.utc) >= tenant.license_expires_at):
            tenant.plan = "free"
            tenant.license_expires_at = None
            db.session.commit()
            logger.info(f"Tenant license expired — downgraded to free | license_id={license_id!r}")

        if tenant.status == "warming":
            return jsonify({
                "success": False,
                "status": "warming",
                "bot_message": "We're still setting up your store — this usually takes a few minutes. Please try again shortly.",
                "intent": "warming",
                "products": [],
                "suggestions": [],
                "metadata": {},
            }), 503

        if tenant.status == "provision_failed":
            # Allow through with whatever loader is available — the tenant DB
            # exists and chat history/messages work. Catalog may be empty but
            # that's better than a hard 403.
            logger.warning(f"Tenant provision_failed — allowing through degraded | license_id={license_id!r}")
            g.tenant = tenant
            g.tenant_features = dict(tenant.features or {})
            g.ecommerce_backend = tenant.ecommerce_backend
            try:
                g.store_loader = _tenant_registry.get_loader(tenant)
            except Exception as e:
                logger.error(
                    f"provision_failed tenant — get_loader() raised | license_id={license_id!r} | {e}",
                    exc_info=True,
                )
                g.store_loader = None
            g.db_engine = _engine_registry.get_engine(tenant.db_name)
            return None

        if not tenant.is_active:
            logger.warning(f"Inactive tenant={license_id!r} ({tenant.status}) → 403")
            return jsonify({"success": False, "error": "tenant inactive",
                            "status": tenant.status}), 403

        # Bind loader (rehydrate on miss) and the per-tenant DB engine.
        g.tenant = tenant
        g.tenant_features = dict(tenant.features or {})
        g.ecommerce_backend = tenant.ecommerce_backend
        g.store_loader = _tenant_registry.get_loader(tenant)
        g.db_engine = _engine_registry.get_engine(tenant.db_name)
        return None