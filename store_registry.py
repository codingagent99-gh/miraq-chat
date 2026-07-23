"""
store_registry.py — Tenant resolution, per-request loader + DB-engine binding.

Resolution order on each request (before_request):
  1. Exempt route?           → skip tenant binding entirely.
  2. X-MiraQ-License-Id set? → look up tenant, bind loader + engine, or 4xx.
  3. Header absent:
       MULTI_TENANT_STRICT=true  → 400 (end-state).
       MULTI_TENANT_STRICT=false → default loader (Phase-1/transition behaviour).

get_store_loader() returns g.store_loader within a request, else the default
(startup / background threads).
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
_EXEMPT_PATHS = {
    "/health", "/status",
    "/provision-tenant",
    "/activate-free",
    "/deactivate-tenant",
}
_EXEMPT_PREFIXES = ("/static/",)

_tenant_registry = None
_engine_registry = None


def get_store_loader():
    """Request-scoped loader. Returns None outside a request context."""
    try:
        return g.store_loader
    except (RuntimeError, AttributeError):
        return None
    
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

def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)

    
def get_tenant_registry():
    """Public accessor for the process-wide TenantRegistry, set at startup."""
    return _tenant_registry

def register_before_request(app) -> None:
    @app.before_request
    def _resolve_tenant():
        if request.method == "OPTIONS":
            return None  # handled by _handle_options in server.py
        path = request.path

        if _is_exempt(path):
            g.store_loader = None
            return None

        license_id = request.headers.get(_LICENSE_HEADER, "").strip()
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
        g.store_loader = _tenant_registry.get_loader(tenant)
        g.db_engine = _engine_registry.get_engine(tenant.db_name)
        return None