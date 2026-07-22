"""
routes/catalog_push.py — Catalog receiver endpoint (Phase 1: dormant).

POST /tenant-catalog-push — called by the WordPress plugin's
class-catalog-push.php to push a fresh catalog snapshot instead of the
backend live-pulling from WooCommerce (some hosts' WAF blocks backend-
initiated pulls — see TenantRegistry.apply_pushed_catalog docstring).

Phase 1 scope: this route ONLY writes a CatalogSnapshot row. It does not
call TenantRegistry.apply_pushed_catalog() and does not touch any resident
loader — that wiring is a later phase.

NOT added to store_registry._EXEMPT_PATHS: this route rides the normal
tenant-resolution middleware (X-MiraQ-License-Id required), so g.tenant is
already set and validated (active/warming/etc.) by the time this view runs.
"""

from flask import Blueprint, request, jsonify, g

from chat_logger import get_logger
from models import db, CatalogSnapshot

logger = get_logger("miraq_chat")

catalog_push_bp = Blueprint("catalog_push", __name__)

# Generous but bounded — guards against a malformed/malicious payload tying
# up a worker on JSON parsing or blowing up the JSONB column.
_MAX_PAYLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


@catalog_push_bp.route("/tenant-catalog-push", methods=["POST"])
def tenant_catalog_push():
    tenant = g.__dict__.get("tenant")
    if tenant is None:
        # Should be unreachable — _resolve_tenant() runs first and would
        # already have 400/404/403'd. Defensive only.
        logger.error("tenant-catalog-push: no tenant on g — resolution middleware didn't run?")
        return jsonify({"success": False, "error": "tenant not resolved"}), 400

    content_length = request.content_length or 0
    if content_length > _MAX_PAYLOAD_BYTES:
        logger.warning(
            f"tenant-catalog-push: payload too large | license_id={tenant.license_id} "
            f"| content_length={content_length}"
        )
        return jsonify({"success": False, "error": "payload too large"}), 413

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "invalid or missing JSON body"}), 400

    products = data.get("products")
    if not isinstance(products, list) or not products:
        return jsonify({"success": False, "error": "payload missing non-empty 'products' list"}), 400

    snapshot = CatalogSnapshot(
        tenant_id=tenant.tenant_id,
        payload=data,
        product_count=len(products),
        payload_bytes=content_length,
    )
    db.session.add(snapshot)
    db.session.flush()  # assign snapshot.id before pruning

    # Keep only the latest snapshot per tenant. Nothing reads history here
    # (see module docstring) — the eventual read path only ever wants "this
    # tenant's current catalog." Without this, every push accumulates a new
    # JSONB row up to 25MB, forever, for every active tenant.
    pruned = (
        CatalogSnapshot.query
        .filter(CatalogSnapshot.tenant_id == tenant.tenant_id, CatalogSnapshot.id != snapshot.id)
        .delete(synchronize_session=False)
    )
    if pruned:
        logger.info(f"tenant-catalog-push: pruned {pruned} older snapshot(s) | license_id={tenant.license_id}")

    db.session.commit()

    logger.info(
        f"tenant-catalog-push: stored | license_id={tenant.license_id} "
        f"| products={len(products)} | bytes={content_length} | snapshot_id={snapshot.id}"
    )

    return jsonify({
        "success": True,
        "license_id": tenant.license_id,
        "tenant_id": str(tenant.tenant_id),
        "snapshot_id": snapshot.id,
        "product_count": len(products),
    }), 200