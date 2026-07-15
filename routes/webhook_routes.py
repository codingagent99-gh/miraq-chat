"""
webhook_routes.py — Receives full catalog PUSHES from a tenant's WordPress
plugin (see class-catalog-push.php) and applies them directly to that
tenant's StoreLoader.

This replaces an earlier notify-then-pull design: this host's WAF
(ModSecurity/SGCaptcha on the tenant's WooCommerce host) blocks
backend-initiated requests to WooCommerce's REST API, so a "something
changed, go fetch it" webhook doesn't actually work here — the backend
still can't reach WooCommerce. Instead, the plugin gathers the full catalog
itself (via an internal REST dispatch that never leaves its own server, so
the WAF never sees it) and POSTs the result here directly.

Auth reuses the same X-Consumer-Key / X-Consumer-Secret credentials the
rest of custom-api/v1/* already uses — no new secret to provision or sync.

No debounce lives here on purpose: the plugin already debounces bursts via
a WordPress transient (see full_push_debounced()) — a transient is stored
in the DB, so it's correctly shared across every PHP worker handling that
site, unlike a Python-process-local dict here, which would silently
misbehave the moment this app runs under multiple Gunicorn/uWSGI workers.
Duplicating that debounce here would just be redundant (and re-introduce
the same multi-worker bug in a different language).
"""

import hmac
import threading

from flask import Blueprint, current_app, jsonify, request

from chat_logger import get_logger
from tenant_crypto import decrypt_secret

logger = get_logger("miraq_chat")

webhook_bp = Blueprint("webhooks", __name__)


def _get_tenant_row(license_id: str):
    """Look up the tenant DB row by license_id.

    TODO: written against an assumed `from models import Tenant` shape with
    a `.query.filter_by(license_id=...)` call, mirroring the fields
    tenant_registry.py's _rehydrate() reads off tenant_row (woo_key,
    woo_secret_encrypted, wp_base_url). Adjust to your actual model/session.
    """
    from models import Tenant  # noqa: adjust import path if different
    return Tenant.query.filter_by(license_id=license_id).first()


def _verify_credentials(tenant_row) -> bool:
    """Timing-safe check against the tenant's stored WooCommerce key/secret —
    the same credentials used elsewhere for this tenant's custom-api calls."""
    key = request.headers.get("X-Consumer-Key", "")
    secret = request.headers.get("X-Consumer-Secret", "")
    if not key or not secret:
        return False

    expected_key = tenant_row.woo_key or ""
    expected_secret = decrypt_secret(tenant_row.woo_secret_encrypted or "")

    return hmac.compare_digest(key, expected_key) and hmac.compare_digest(secret, expected_secret or "")


@webhook_bp.route("/webhooks/woocommerce/<license_id>/catalog-push", methods=["POST"])
def woocommerce_catalog_push(license_id):
    tenant_row = _get_tenant_row(license_id)
    if tenant_row is None:
        logger.warning(f"CatalogPush: unknown tenant | license_id={license_id}")
        return jsonify({"error": "unknown tenant"}), 404

    if not _verify_credentials(tenant_row):
        logger.warning(f"CatalogPush: invalid credentials | tenant={license_id}")
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload or "products" not in payload:
        logger.warning(f"CatalogPush: malformed or empty payload | tenant={license_id}")
        return jsonify({"error": "malformed payload"}), 400

    registry = current_app.config["TENANT_REGISTRY"]  # TODO: adjust to however app.py exposes this

    def _apply():
        try:
            registry.apply_pushed_catalog(tenant_row, payload)
        except Exception as e:
            logger.error(f"CatalogPush: apply failed | tenant={license_id} | {e}", exc_info=True)

    # Building lookups + semantic vectors for a full catalog isn't
    # instant — do it off the request thread so the plugin's push (which
    # is itself fire-and-forget, but still) gets a fast ack.
    threading.Thread(target=_apply, daemon=True).start()

    logger.info(
        f"CatalogPush: accepted | tenant={license_id} "
        f"products={len(payload.get('products', []))} "
        f"categories={len(payload.get('categories', []))} "
        f"tags={len(payload.get('tags', []))} "
        f"attributes={len(payload.get('all_attributes_raw', []))}"
    )
    return jsonify({"status": "accepted"}), 202
