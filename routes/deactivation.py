"""
routes/deactivation.py — Tenant teardown endpoint.

POST /deactivate-tenant — called by the WordPress plugin's uninstall hook
when the plugin is deleted (not just deactivated). Verifies the licence
signature, marks the tenant archived, evicts it from both registries, and
drops the physical database.

Lifecycle after this call:
  tenants.status = "archived"   ← row is kept for audit; db_name preserved
  loader evicted from TenantRegistry
  engine disposed from DBEngineRegistry
  physical database DROPPED

Idempotent: safe to call twice (DB already gone = not an error).
"""

from datetime import datetime, timezone
import json
from flask import Blueprint, request, jsonify, current_app

from chat_logger import get_logger
from models import db, Tenant
from license_verifier import verify_license_payload, LicenseVerificationError
from tenant_db_provisioner import drop_tenant_database, TenantDBProvisionError

logger = get_logger("miraq_chat")

deactivation_bp = Blueprint("deactivation", __name__)


@deactivation_bp.route("/deactivate-tenant", methods=["POST"])
def deactivate_tenant():
    body = request.get_json(silent=True) or {}

    raw_payload   = body.get("raw_payload")
    signature_b64 = body.get("signature")

    # raw_payload might be the full licensing-server response wrapper
    # ({kid, payload, signature}) rather than just the inner payload string.
    # Unwrap it if so.
    if raw_payload:
        try:
            parsed = json.loads(raw_payload)
            if "payload" in parsed and "licenseId" not in parsed:
                raw_payload = json.dumps(
                    parsed["payload"],
                    separators=(",", ":"),
                    ensure_ascii=False
                )
        except Exception:
            pass

    logger.info(f"deactivate-tenant: raw_payload={'present' if raw_payload else 'MISSING'} | signature={'present' if signature_b64 else 'MISSING'}")

    if not raw_payload or not signature_b64:
        return jsonify({"success": False, "error": "missing raw_payload/signature"}), 400

    # Verify the signature — same check as /provision-tenant.
    # This prevents a third party from triggering teardown by guessing a licenseId.
    try:
        claims = verify_license_payload(raw_payload, signature_b64)
    except LicenseVerificationError as e:
        logger.warning(f"deactivate-tenant: verification failed | {e}")
        return jsonify({"success": False, "error": "invalid signature"}), 401

    license_id = claims.get("licenseId") or claims.get("license_id")
    if not license_id:
        return jsonify({"success": False, "error": "payload missing licenseId"}), 400

    tenant = Tenant.query.filter_by(license_id=license_id).first()

    if tenant is None:
        # Already gone — idempotent 200.
        logger.info(f"deactivate-tenant: tenant not found (already removed?) | license_id={license_id}")
        return jsonify({"success": True, "license_id": license_id, "status": "not_found"}), 200

    if tenant.status == "archived":
        logger.info(f"deactivate-tenant: already archived | license_id={license_id}")
        return jsonify({"success": True, "license_id": license_id, "status": "archived"}), 200

    result = teardown_tenant(tenant, log_prefix="deactivate-tenant")
    if not result["success"]:
        return jsonify({"success": False, "error": result["error"]}), 500

    return jsonify({
        "success":    True,
        "license_id": license_id,
        "status":     "archived",
    }), 200


def teardown_tenant(tenant, *, log_prefix: str = "teardown") -> dict:
    """
    Tear a tenant down: evict from registries, drop its database, archive the
    control-plane row, delete its snapshot.

    Extracted from deactivate_tenant() so the Shopify app/uninstalled webhook
    can reuse it. The two callers authenticate completely differently — a
    signed licence payload versus a Shopify body HMAC — but everything AFTER
    "we know which tenant, and we're allowed to remove it" is identical, and
    duplicating it would mean one path eventually drifting (forgetting the
    snapshot delete, or dropping the DB before evicting the engine and
    hitting "database is being accessed by other users").

    The caller is responsible for authenticating the request and for the
    already-archived / not-found early returns, since their response shapes
    differ.

    Ordering is load-bearing:
      1. evict registries first — a live engine holds connections, and
         DROP DATABASE fails while any session is open
      2. drop the physical database
      3. archive the row (kept for audit; db_name preserved)
      4. delete the snapshot, best-effort

    Returns {"success": True} or {"success": False, "error": str}.
    """
    db_name = tenant.db_name
    label = f"license_id={tenant.license_id}" if tenant.license_id else f"shop={tenant.shopify_domain}"
    logger.info(f"{log_prefix}: starting teardown | {label} db={db_name}")

    # ── 1. Evict from in-memory registries FIRST ──────────────────────────────
    try:
        from store_registry import get_tenant_registry, get_engine_registry
        registry = get_tenant_registry()
        if registry:
            registry.evict(str(tenant.tenant_id))
            logger.info(f"{log_prefix}: loader evicted | {label}")

        engine_registry = get_engine_registry()
        if engine_registry:
            engine_registry.dispose_for(db_name)
            logger.info(f"{log_prefix}: engine disposed | {label}")
    except Exception as e:
        logger.error(f"{log_prefix}: registry eviction failed | {label} | {e}", exc_info=True)
        # Non-fatal — continue with DB drop even if registry eviction fails

    # ── 2. Drop the physical database ─────────────────────────────────────────
    base_dsn = current_app.config["SQLALCHEMY_DATABASE_URI"]
    try:
        drop_tenant_database(base_dsn, db_name)
        logger.info(f"{log_prefix}: database dropped | {label} db={db_name}")
    except TenantDBProvisionError as e:
        logger.error(f"{log_prefix}: DROP failed | {label} | {e}", exc_info=True)
        return {"success": False, "error": f"database drop failed: {e}"}

    # ── 3. Mark archived in the control-plane row ─────────────────────────────
    try:
        tenant.status = "archived"
        tenant.archived_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info(f"{log_prefix}: teardown complete | {label}")
    except Exception as e:
        logger.error(f"{log_prefix}: failed to archive row | {label} | {e}", exc_info=True)
        return {"success": False, "error": f"archive failed: {e}"}

    # ── 4. Best-effort snapshot cleanup ───────────────────────────────────────
    # delete() is self-guarding (logs and returns on failure), so a leftover
    # snapshot never blocks a completed teardown. Keyed by tenant_id, matching
    # how snapshots are stored since the re-key.
    from tenant_snapshot_store import snapshot_store
    snapshot_store.delete(str(tenant.tenant_id))

    return {"success": True}