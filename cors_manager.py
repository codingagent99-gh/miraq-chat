"""
cors_manager.py — Dynamic CORS origin registry.

The hardcoded flask-cors origins list is replaced with this module because:
  - New tenants provision at runtime, and their site_domain needs to be an
    allowed origin without a server restart.
  - flask-cors doesn't natively support a callable for origins that can be
    updated after startup.

Architecture: a module-level set (_allowed_origins) seeded at startup from
the static list + the tenants table, then expanded by add_tenant_origin()
whenever /provision-tenant commits a new or updated site_domain.

server.py registers apply_cors() as an after_request handler and
_handle_options_preflight() as a before_request handler, replacing
flask-cors entirely.
"""

from __future__ import annotations
import threading
from typing import Set

from chat_logger import get_logger

logger = get_logger("miraq_chat")

# ── Static origins — always allowed regardless of tenant table ────────────────

_STATIC_ORIGINS: Set[str] = {
    "https://wgc.net.in",
    "https://silfradigital.com",
    "https://silfratech.in",
    "https://silfra-store-4680.myshopify.com",
    "https://staging-91e4-ecom-solutions9857d536fc-ugaqb.wpcomstaging.com",
    "http://localhost:5173",
    "http://localhost:5174",
}

_ALLOW_HEADERS = (
    "Content-Type, X-MiraQ-Session, X-MiraQ-License-Id, "
    "X-WC-Session, X-WP-Nonce, Authorization"
)
_ALLOW_METHODS = "GET, POST, PUT, DELETE, PATCH, OPTIONS"

_dynamic_origins: Set[str] = set()
_lock = threading.Lock()


def _normalise(domain: str) -> Set[str]:
    """
    Given a raw site_domain value (may or may not include a scheme),
    return both https:// and http:// variants of the bare hostname.
    """
    domain = domain.strip().rstrip("/")
    if domain.startswith("http://") or domain.startswith("https://"):
        # Strip any existing scheme then re-add both.
        bare = domain.split("://", 1)[1]
    else:
        bare = domain
    return {f"https://{bare}", f"http://{bare}"}


def add_tenant_origin(site_domain: str) -> None:
    """
    Add a tenant's site_domain to the allowed-origins set.
    Called from /provision-tenant after the tenants row is committed.
    Thread-safe — /provision-tenant runs in a background thread.
    """
    if not site_domain:
        return
    origins = _normalise(site_domain)
    with _lock:
        _dynamic_origins.update(origins)
    logger.info(f"CORSManager: added tenant origins {origins}")


def refresh_from_db() -> None:
    """
    Seed _dynamic_origins from all active tenant site_domain values.
    Called once at startup, after db.create_all().
    """
    try:
        from models import Tenant
        rows = Tenant.query.filter(
            Tenant.site_domain.isnot(None),
            Tenant.status.in_(["active", "warming"]),
        ).with_entities(Tenant.site_domain).all()

        with _lock:
            for (domain,) in rows:
                if domain:
                    _dynamic_origins.update(_normalise(domain))

        logger.info(f"CORSManager: seeded {len(rows)} tenant origins from DB")
    except Exception as e:
        logger.warning(f"CORSManager: refresh_from_db failed (DB may not be ready): {e}")


def is_allowed(origin: str) -> bool:
    with _lock:
        return origin in _STATIC_ORIGINS or origin in _dynamic_origins


def apply_cors(response, origin: str):
    """
    Stamp CORS headers onto a response for a known-good origin.
    Caller is responsible for checking is_allowed() first.
    """
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = _ALLOW_HEADERS
    response.headers["Access-Control-Allow-Methods"] = _ALLOW_METHODS
    response.headers["Vary"] = "Origin"
    return response