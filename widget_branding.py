"""
widget_branding.py — Fetch & persist a tenant's widget branding (logo /
header text) from its WordPress plugin onto the Tenant row.

Replaces the live HTTP call /widget-config used to make on EVERY widget load
(one outbound request per page view, per tenant — the pattern that drew 429s
from WP.com-hosted stores in the reference implementation). /widget-config is
now a pure read of the Tenant row; this module refreshes that row:

  * right after a successful tenant build   (routes/provisioning.py)
  * on RefreshScheduler's sweep, at most once per 24h per tenant
  * immediately, when the plugin pushes a change (routes/webhook_routes.py)

Deviations from the reference:
  - WooCommerce tenants only. A Shopify tenant has no wp_base_url and no
    wdget-logo-uploader endpoint; it is skipped rather than sent to a
    malformed URL.
  - Free-plan tenants are NOT skipped. The reference skipped them because its
    plugin hides the Branding tab on the free tier; that is a plugin-side
    decision this backend cannot see, and skipping would silently drop
    branding for any free store that does have it. A free store with nothing
    configured just stores empty values once a day.
"""

from datetime import datetime, timezone

import requests as req

from chat_logger import get_logger
from http_profiles import HttpProfileState, send as http_send
from models import db, Tenant
from tenant_crypto import decrypt_secret

logger = get_logger("miraq_chat")

WIDGET_CONFIG_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60  # 24h


def _is_eligible(tenant: Tenant) -> bool:
    return (tenant.ecommerce_backend or "woocommerce") == "woocommerce"


def fetch_and_store_widget_branding(tenant: Tenant) -> bool:
    """
    Fetch logo/header text for one tenant and persist it onto its row.

    Non-fatal on failure: the previous (or empty) values stay in place and
    the next sweep retries. The session is rolled back on failure so a caller
    that commits afterwards (the provisioning build thread does) is not left
    holding a broken transaction.
    """
    if not _is_eligible(tenant):
        return False

    wp_base = (tenant.wp_base_url or "").rstrip("/")
    if not wp_base or not tenant.woo_key or not tenant.woo_secret_encrypted:
        logger.warning(
            f"widget_branding: tenant missing wp_base_url/woo creds — skipping | "
            f"license_id={tenant.license_id}"
        )
        return False

    target_url = f"{wp_base}/wp-json/wdget-logo-uploader/v1/data"
    try:
        # Same header profile as every other call to this store. Prefer the
        # resident loader's live state (it may have switched profiles since
        # the row was read); fall back to the row's stored profile.
        http = _http_state_for(tenant)
        headers = {
            "X-Consumer-Key":    tenant.woo_key,
            "X-Consumer-Secret": decrypt_secret(tenant.woo_secret_encrypted),
        }
        resp = http_send(req.Session(), "GET", target_url, state=http,
                         headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        store_widget_branding(
            tenant,
            image_url=data.get("image_url", ""),
            text=data.get("text", ""),
        )
        logger.info(f"widget_branding: updated | license_id={tenant.license_id}")
        return True
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        logger.warning(
            f"widget_branding: fetch failed | license_id={tenant.license_id} | "
            f"{type(e).__name__}: {e}"
        )
        return False


def _http_state_for(tenant: Tenant) -> HttpProfileState:
    try:
        from store_registry import get_tenant_registry
        registry = get_tenant_registry()
        if registry is not None:
            loader = dict(registry.resident_loaders()).get(str(tenant.tenant_id))
            if loader is not None and getattr(loader, "http", None) is not None:
                return loader.http
    except Exception:
        pass
    return HttpProfileState.from_tenant(tenant)


def store_widget_branding(tenant: Tenant, *, image_url: str, text: str) -> None:
    """Write branding values onto the tenant row and commit."""
    tenant.widget_logo_url = image_url or ""
    tenant.widget_header_text = text or ""
    tenant.widget_config_fetched_at = datetime.now(timezone.utc)
    db.session.commit()


def is_widget_branding_stale(tenant: Tenant) -> bool:
    if not _is_eligible(tenant):
        return False
    if tenant.widget_config_fetched_at is None:
        return True
    age = datetime.now(timezone.utc) - tenant.widget_config_fetched_at
    return age.total_seconds() >= WIDGET_CONFIG_REFRESH_INTERVAL_SECONDS