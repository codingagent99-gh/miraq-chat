"""
widget_branding.py — Fetch & persist tenant widget branding (logo/header text)
from the WP plugin's wdget-logo-uploader endpoint, onto the Tenant row.

This replaces a live HTTP call on every /widget-config request (which was
triggering 429s from WP.com staging) with an occasional background pull.
/widget-config becomes a pure DB read.
"""
import requests as req
from datetime import datetime, timezone

from chat_logger import get_logger
from models import db, Tenant
from app_config import BROWSER_HEADERS
from tenant_crypto import decrypt_secret

logger = get_logger("miraq_chat")

WIDGET_CONFIG_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60  # 24h


def fetch_and_store_widget_branding(tenant: Tenant) -> bool:
    """
    Fetch logo/header text for one tenant and persist onto its row.
    Non-fatal on failure — caller just leaves the previous (or empty)
    value in place and tries again next cycle.
    """
    if not tenant.wp_base_url or not tenant.woo_key or not tenant.woo_secret_encrypted:
        logger.warning(
            f"widget_branding: tenant missing wp_base_url/woo creds — skipping | "
            f"license_id={tenant.license_id}"
        )
        return False

    target_url = f"{tenant.wp_base_url}/wp-json/wdget-logo-uploader/v1/data"
    try:
        headers = {
            **BROWSER_HEADERS,
            "X-Consumer-Key":    tenant.woo_key,
            "X-Consumer-Secret": decrypt_secret(tenant.woo_secret_encrypted),
        }
        resp = req.get(target_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        tenant.widget_logo_url        = data.get("image_url", "") or ""
        tenant.widget_header_text     = data.get("text", "") or ""
        tenant.widget_config_fetched_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info(f"widget_branding: updated | license_id={tenant.license_id}")
        return True
    except Exception as e:
        logger.error(
            f"widget_branding: fetch failed | license_id={tenant.license_id} | "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        return False


def is_widget_branding_stale(tenant: Tenant) -> bool:
    if tenant.widget_config_fetched_at is None:
        return True
    age = datetime.now(timezone.utc) - tenant.widget_config_fetched_at
    return age.total_seconds() >= WIDGET_CONFIG_REFRESH_INTERVAL_SECONDS