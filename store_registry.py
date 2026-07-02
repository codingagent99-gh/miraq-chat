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
_STRICT = os.getenv("MULTI_TENANT_STRICT", "false").lower() == "true"

# Routes that must work WITHOUT a resolved tenant.
_EXEMPT_PATHS = {
    "/health", "/status", "/widget-config", "/shopify-token-status",
    "/provision-tenant",            # Phase 4 — onboarding, pre-tenant by definition
    "/debug-plan",
}
_EXEMPT_PREFIXES = ("/static/",)

# Wired once at startup by init_registries() in server.py.
_default_loader = None
_tenant_registry = None
_engine_registry = None


def set_store_loader(loader) -> None:
    """Phase-1 compatible: register the single default (WGC) loader."""
    global _default_loader
    _default_loader = loader
    if _tenant_registry is not None:
        _tenant_registry.set_default_loader(loader)


def get_store_loader():
    """Request-scoped loader, else the process default (startup/background)."""
    try:
        return g.store_loader
    except (RuntimeError, AttributeError):
        return _default_loader
    
def get_tenant_features() -> dict:
    """
    Return the feature flags for the current request's tenant.

    Falls back to all-features-enabled for the default (WGC) tenant
    so existing behaviour is fully preserved when no licenseId is sent.
    """
    try:
        tenant = g.__dict__.get("tenant")
        if tenant is not None:
            return tenant.features or {}
    except RuntimeError:
        pass
    # Default tenant (WGC) or outside request context — all features on.
    return {
        "bulk_order":       True,
        "cs_rep":           True,
        "thwma_addresses":  True,
        "thwcfe_fields":    True,
    }


def init_registries(tenant_registry, engine_registry) -> None:
    """Called once at startup, after the registries are constructed."""
    global _tenant_registry, _engine_registry
    _tenant_registry = tenant_registry
    _engine_registry = engine_registry


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
            # Exempt routes use the default loader (e.g. /widget-config reads it).
            g.store_loader = _default_loader
            return None

        license_id = request.headers.get(_LICENSE_HEADER, "").strip()

        # ── No header ─────────────────────────────────────────────────────────
        if not license_id:
            if _STRICT:
                logger.warning(f"Tenant header missing on {path} (strict) → 400")
                return jsonify({"success": False, "error": "missing tenant"}), 400
            # Transition: serve the single default tenant (Phase-1 behaviour).
            g.store_loader = _default_loader
            return None

        # ── Header present — resolve the tenant ──────────────────────────────
        from models import Tenant
        tenant = Tenant.query.get(license_id)
        if tenant is None:
            logger.warning(f"Unknown license_id={license_id!r} on {path} → 404")
            return jsonify({"success": False, "error": "unknown tenant"}), 404
        # Auto-mark expired tenants before checking is_active.
        if (tenant.status == "active"
                and tenant.license_expires_at is not None
                and datetime.now(timezone.utc) >= tenant.license_expires_at):
            tenant.status = "expired"
            db.session.commit()
            logger.warning(f"Tenant licence expired — auto-marked | license_id={license_id!r}")

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

        if not tenant.is_active:
            logger.warning(f"Inactive tenant={license_id!r} ({tenant.status}) → 403")
            return jsonify({"success": False, "error": "tenant inactive",
                            "status": tenant.status}), 403

        # Bind loader (rehydrate on miss) and the per-tenant DB engine.
        g.tenant = tenant
        g.store_loader = _tenant_registry.get_loader(tenant)
        g.db_engine = _engine_registry.get_engine(tenant.db_name)
        return None
