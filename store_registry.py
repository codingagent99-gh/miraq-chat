"""
store_registry.py — Tenant resolution, per-request loader + DB-engine binding.

Resolution order on each request (before_request):
  1. Exempt path?             → skip tenant binding entirely.
  2. X-MiraQ-License-Id set?  → look up tenant, bind loader + engine, or 4xx.
  3. Header absent            → 400. No fallback to a default tenant — every
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
# /events/app-uninstalled (Stage 2, routes/shopify.py) are Shopify-native mechanisms — App Proxy and Events
# webhooks — that carry their own signed tenant identifier (a `shop` query
# param or a Shopify-Shop-Domain header) and verify it themselves. Shopify
# has no way to send X-MiraQ-License-Id, so these must not be gated behind
# it either.
#
# No Shopify OAuth callback path is listed because this project has none to
# exempt: store_loader/shopify_token_manager.py uses the client_credentials
# grant (server-to-server, POST straight to /admin/oauth/access_token), not
# the authorization-code flow's browser-redirect callback. If Stage 2 adds a
# tenant-facing Shopify app install flow with a real callback route, it goes
# here then.
_EXEMPT_PATHS = {
    "/provision-tenant", "/activate-free", "/deactivate-tenant",
    "/customer-addresses", "/events/product-update", "/events/order-paid",
    "/events/app-uninstalled",
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

def _bind_optional_tenant(license_id: str) -> None:
    g.store_loader = None
    if not license_id:
        return
    try:
        from models import Tenant
        tenant = Tenant.query.filter_by(license_id=license_id).first()
        if tenant is None:
            return
        g.tenant = tenant
        g.tenant_features = dict(tenant.features or {})
        g.ecommerce_backend = tenant.ecommerce_backend
        if tenant.status in ("active", "provision_failed") and _tenant_registry is not None:
            resident = dict(_tenant_registry.resident_loaders())
            g.store_loader = resident.get(str(tenant.tenant_id))
    except Exception as e:
        logger.warning(f"_bind_optional_tenant: could not bind | license_id={license_id[:8]!r} | {e}")
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

        # ── No header ─────────────────────────────────────────────────────────
        if not license_id:
            logger.warning(f"Tenant header missing on {path} → 400")
            return jsonify({"success": False, "error": "missing tenant"}), 400

        # ── Header present — resolve the tenant ──────────────────────────────
        from models import Tenant
        tenant = Tenant.query.filter_by(license_id=license_id).first()
        if tenant is None:
            logger.warning(f"Unknown license_id={license_id!r} on {path} → 404")
            return jsonify({"success": False, "error": "unknown tenant"}), 404

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