"""
routes/webhook_routes.py — Pushes FROM a tenant's WordPress plugin.

POST /webhooks/woocommerce/<license_id>/branding-push
    The plugin's Branding tab fires this right after a successful save, so a
    logo/header change reaches the widget immediately instead of waiting up
    to 24h for RefreshScheduler's branding sweep (widget_branding.py). The
    sweep stays in place as the fallback, so a failed push still self-heals.

Catalog push (the reference's /catalog-push) is deliberately NOT here — per
the conversion plan's D2 it is adopted only if a tenant's host WAFs
backend-initiated WooCommerce calls; the catalog-version probe covers
freshness otherwise.

Tenant resolution rides the normal middleware (X-MiraQ-License-Id), so
g.tenant is already set and validated. The <license_id> path segment must
match that header — otherwise a caller holding tenant A's credentials could
post to tenant B's URL and have it silently applied to A. Auth then reuses
the tenant's own X-Consumer-Key / X-Consumer-Secret, compared timing-safe,
so no new secret has to be provisioned or synced.
"""

import hmac

from flask import Blueprint, g, jsonify, request

from chat_logger import get_logger
from models import db
from tenant_crypto import decrypt_secret

logger = get_logger("miraq_chat")

webhook_bp = Blueprint("webhooks", __name__)

def _verify_credentials(tenant_row) -> bool:
    """Timing-safe check against the tenant's stored WooCommerce key/secret."""
    key = request.headers.get("X-Consumer-Key", "")
    secret = request.headers.get("X-Consumer-Secret", "")
    if not key or not secret:
        return False

    expected_key = tenant_row.woo_key or ""
    expected_secret = decrypt_secret(tenant_row.woo_secret_encrypted or "")
    if not expected_key or not expected_secret:
        return False

    # Evaluate both comparisons (no short-circuit) so timing does not reveal
    # which half matched.
    key_ok = hmac.compare_digest(key, expected_key)
    secret_ok = hmac.compare_digest(secret, expected_secret)
    return key_ok and secret_ok


@webhook_bp.route("/webhooks/woocommerce/<license_id>/branding-push", methods=["POST"])
def woocommerce_branding_push(license_id):
    tenant_row = g.__dict__.get("tenant")
    if tenant_row is None:
        # Unreachable in practice — _resolve_tenant already 400/404'd.
        return jsonify({"error": "unknown tenant"}), 404

    if license_id != tenant_row.license_id:
        logger.warning(
            f"BrandingPush: path/header tenant mismatch | path={license_id!r} "
            f"header={tenant_row.license_id!r}"
        )
        return jsonify({"error": "tenant mismatch"}), 400

    if not _verify_credentials(tenant_row):
        logger.warning(f"BrandingPush: invalid credentials | tenant={license_id}")
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        logger.warning(f"BrandingPush: malformed payload | tenant={license_id}")
        return jsonify({"error": "malformed payload"}), 400

    image_url = payload.get("image_url", "") or ""
    text = payload.get("text", "") or ""

    # Synchronous: one small row write. Tenant is a control-plane model, so
    # _TenantRoutingSession sends this commit to the main DB even though the
    # request has the tenant's own engine bound.
    from widget_branding import store_widget_branding
    try:
        store_widget_branding(tenant_row, image_url=image_url, text=text)
    except Exception as e:
        db.session.rollback()
        logger.error(f"BrandingPush: write failed | tenant={license_id} | {e}", exc_info=True)
        return jsonify({"error": "write failed"}), 500

    logger.info(f"BrandingPush: updated | tenant={license_id}")
    return jsonify({"status": "ok"}), 200