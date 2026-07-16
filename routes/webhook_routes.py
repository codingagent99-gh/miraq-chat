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
from flask import g
from flask import Blueprint, current_app, jsonify, request
from models import db, Tenant
from chat_logger import get_logger
from tenant_crypto import decrypt_secret

logger = get_logger("miraq_chat")

webhook_bp = Blueprint("webhooks", __name__)


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
    # _resolve_tenant (in store_registry.py) already looked up the tenant
    # and stashed it in g.tenant — reuse that instead of doing a duplicate
    # query, which was going through _TenantRoutingSession and hitting the
    # tenant's own DB (where the 'tenants' table doesn't exist).
    tenant_row = g.tenant
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

    from store_registry import get_tenant_registry
    registry = get_tenant_registry()

    # Captured here, in the request thread, where current_app resolves —
    # the background thread below has no request/app context of its own,
    # so it needs the real app object to push one before touching db.session.
    app = current_app._get_current_object()

    def _apply():
        try:
            registry.apply_pushed_catalog(tenant_row, payload)
        except Exception as e:
            logger.error(f"CatalogPush: apply failed | tenant={license_id} | {e}", exc_info=True)
            return

        # Tenant-row lifecycle lives here at the endpoint layer, not inside
        # TenantRegistry.apply_pushed_catalog (that method only owns loader
        # state). Only runs once the apply above has actually succeeded.
        #
        # Runs in a background thread with no app context of its own, so
        # db.session needs one pushed explicitly — and the row is re-queried
        # fresh here rather than reusing `tenant_row` from the request
        # thread's session, which this thread has no binding to. Re-querying
        # inside the same transaction as the update also makes the
        # provision_failed precondition check race-safe against a second
        # concurrent push (plugin retry / debounce edge case) landing at
        # nearly the same time — whichever commits first wins, the second
        # sees status already 'active' and no-ops.
        with app.app_context():
            try:
                fresh_tenant = Tenant.query.filter_by(license_id=license_id).first()
                if fresh_tenant is None:
                    logger.error(f"CatalogPush: tenant disappeared before status flip | tenant={license_id}")
                    return
                if fresh_tenant.status != "provision_failed":
                    # Already active (or warming/archived) — nothing to recover.
                    # Also the idempotency guard: a second push after the
                    # first already flipped this to 'active' lands here and
                    # no-ops.
                    return

                fresh_tenant.status = "active"
                fresh_tenant.last_build_error = None
                # RefreshScheduler's stuck-tenant retry loop gives up once
                # build_attempts hits 5. A push-recovered tenant's prior
                # failures are no longer relevant — reset so future
                # degradation gets its own full retry budget.
                fresh_tenant.build_attempts = 0
                db.session.commit()
                logger.info(f"CatalogPush: recovered tenant from provision_failed | tenant={license_id}")
            except Exception as e:
                # Deliberately separate from the apply's own try/except above:
                # the push already succeeded and the loader is populated, so
                # a failure here shouldn't surface as an error to the plugin
                # (which would just retry the whole catalog send). Log loudly
                # and move on — the loader is doing its job either way.
                db.session.rollback()
                logger.error(
                    f"CatalogPush: failed to flip tenant status to active | tenant={license_id} | {e}",
                    exc_info=True,
                )

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