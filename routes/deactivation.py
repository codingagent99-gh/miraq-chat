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

    tenant = Tenant.query.get(license_id)
    if tenant is None:
        # Already gone — idempotent 200.
        logger.info(f"deactivate-tenant: tenant not found (already removed?) | license_id={license_id}")
        return jsonify({"success": True, "license_id": license_id, "status": "not_found"}), 200

    if tenant.status == "archived":
        logger.info(f"deactivate-tenant: already archived | license_id={license_id}")
        return jsonify({"success": True, "license_id": license_id, "status": "archived"}), 200

    db_name = tenant.db_name
    logger.info(f"deactivate-tenant: starting teardown | license_id={license_id} db={db_name}")

    # ── 1. Evict from in-memory registries FIRST ──────────────────────────────
    # This prevents any in-flight or new request from acquiring a connection
    # to the database we're about to drop.
    from store_registry import get_tenant_registry, get_engine_registry
    registry = get_tenant_registry()
    if registry:
        registry.evict(license_id)

    engine_registry = get_engine_registry()
    if engine_registry:
        engine_registry.dispose_for(db_name)

    # ── 2. Drop the physical database ─────────────────────────────────────────
    base_dsn = current_app.config["SQLALCHEMY_DATABASE_URI"]
    try:
        drop_tenant_database(base_dsn, db_name)
    except TenantDBProvisionError as e:
        logger.error(f"deactivate-tenant: DROP failed | license_id={license_id} | {e}")
        # Re-insert the loader into the registry isn't feasible here — the
        # engine was already disposed. Log and return 500; the operator will
        # need to drop manually. The tenant row is NOT archived so a retry works.
        return jsonify({"success": False, "error": f"database drop failed: {e}"}), 500

    # ── 3. Mark archived in the control-plane row ─────────────────────────────
    # Keep the row for audit — db_name is preserved so we know which DB was
    # dropped, even though the database itself no longer exists.
    tenant.status = "archived"
    tenant.archived_at = datetime.now(timezone.utc)
    db.session.commit()

    logger.info(f"deactivate-tenant: ✅ teardown complete | license_id={license_id}")
    return jsonify({
        "success":    True,
        "license_id": license_id,
        "status":     "archived",
    }), 200