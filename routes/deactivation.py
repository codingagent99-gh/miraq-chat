"""
routes/deactivation.py — Tenant teardown endpoint.

POST /deactivate-tenant — called by the WordPress plugin's uninstall hook
when the plugin is deleted (not just deactivated).

TWO WAYS TO AUTHENTICATE, because two kinds of tenant have to be able to
leave:

  1. Signed licence payload (raw_payload + signature). Paid tenants, as
     before. Resolves the tenant by the licenceId inside the verified claims.

  2. tenant_uuid + the store's own WooCommerce credentials, sent as
     X-Consumer-Key / X-Consumer-Secret. This is the free-tier path: a free
     activation never receives a licence, so it has nothing to sign with and
     previously could not be torn down at all — its row stayed active and its
     database was never reclaimed. The credentials are the same ones the
     branding-push webhook already authenticates with, and they are checked
     the same way (timing-safe, against the stored pair). Paid tenants may use
     this path too; the plugin falls back to it when a signed call is rejected
     after a licence rotation.

     tenant_uuid ALONE is deliberately not enough. It identifies, it does not
     authorise — it sits in wp_options, travels in site exports, and cannot be
     rotated if it leaks (the db_name is derived from it).

Lifecycle after this call:
  tenants.status = "archived"   ← row is kept for audit; db_name preserved
  loader evicted from TenantRegistry
  engine disposed from DBEngineRegistry
  snapshot deleted
  physical database LEFT IN PLACE — dropped later by RefreshScheduler's
  archived-tenant sweep, TENANT_ARCHIVE_GRACE_DAYS after archived_at

Idempotent: safe to call twice (already archived = 200, not an error).
"""

import hmac
from datetime import datetime, timezone
import json
import uuid as uuid_mod
from flask import Blueprint, request, jsonify

from chat_logger import get_logger
from models import db, Tenant
from license_verifier import verify_license_payload, LicenseVerificationError
from tenant_crypto import decrypt_secret

logger = get_logger("miraq_chat")

deactivation_bp = Blueprint("deactivation", __name__)

def _verify_store_credentials(tenant) -> bool:
    """
    Timing-safe check of X-Consumer-Key / X-Consumer-Secret against the
    tenant's stored WooCommerce credentials. Same check as
    routes/webhook_routes.py::_verify_credentials — the store proving it is
    the store, with a credential that CAN be rotated if it leaks.
    """
    key = request.headers.get("X-Consumer-Key", "")
    secret = request.headers.get("X-Consumer-Secret", "")
    if not key or not secret:
        return False

    expected_key = tenant.woo_key or ""
    try:
        expected_secret = decrypt_secret(tenant.woo_secret_encrypted or "")
    except Exception as e:
        logger.error(f"deactivate-tenant: could not decrypt stored secret | tenant_id={tenant.tenant_id} | {e}")
        return False
    if not expected_key or not expected_secret:
        return False

    # Both comparisons always run, so timing cannot reveal which half matched.
    key_ok = hmac.compare_digest(key, expected_key)
    secret_ok = hmac.compare_digest(secret, expected_secret)
    return key_ok and secret_ok



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

    tenant_uuid_raw = (body.get("tenant_uuid") or "").strip()

    logger.info(
        f"deactivate-tenant: raw_payload={'present' if raw_payload else 'MISSING'} | "
        f"signature={'present' if signature_b64 else 'MISSING'} | "
        f"tenant_uuid={tenant_uuid_raw or 'MISSING'}"
    )

    # ── Path 2: no signature — tenant_uuid + store credentials ───────────────
    if not raw_payload or not signature_b64:
        if not tenant_uuid_raw:
            return jsonify({"success": False, "error": "missing raw_payload/signature or tenant_uuid"}), 400
        return _deactivate_by_credentials(tenant_uuid_raw)

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

def _deactivate_by_credentials(tenant_uuid_raw: str):
    """Path 2 — see this module's docstring."""
    try:
        tenant_uuid = uuid_mod.UUID(tenant_uuid_raw)
    except (ValueError, AttributeError, TypeError):
        return jsonify({"success": False, "error": "tenant_uuid is not a valid UUID"}), 400

    tenant = db.session.get(Tenant, tenant_uuid)

    # Unknown tenant returns the same 401 as bad credentials, on purpose: a
    # "this UUID exists but your credentials are wrong" answer would turn this
    # endpoint into a way to test whether a given UUID is a real tenant.
    if tenant is None or not _verify_store_credentials(tenant):
        logger.warning(f"deactivate-tenant: credential auth rejected | tenant_uuid={tenant_uuid_raw}")
        return jsonify({"success": False, "error": "unauthorized"}), 401

    if tenant.status == "archived":
        logger.info(f"deactivate-tenant: already archived | tenant_id={tenant_uuid}")
        return jsonify({"success": True, "tenant_id": str(tenant_uuid), "status": "archived"}), 200

    result = teardown_tenant(tenant, log_prefix="deactivate-tenant(creds)")
    if not result["success"]:
        return jsonify({"success": False, "error": result["error"]}), 500

    return jsonify({
        "success":   True,
        "tenant_id": str(tenant_uuid),
        "status":    "archived",
    }), 200

def teardown_tenant(tenant, *, log_prefix: str = "teardown") -> dict:
    """
    Tear a tenant down: evict from registries, archive the control-plane row,
    delete its snapshot. The physical database is deliberately LEFT IN PLACE.

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

    The database is dropped later, by RefreshScheduler's archived-tenant
    sweep, TENANT_ARCHIVE_GRACE_DAYS after archived_at. Teardown is triggered
    by a credential that lives on a WordPress site, so the destructive half is
    made reversible: for the length of the grace period a reprovision with the
    same tenant_uuid clears archived_at, reuses the same db_name, and the
    tenant is back with its history. After the sweep runs there is nothing to
    come back to, which is why it is a sweep and not part of this call.

    Ordering:
      1. evict registries first — a live engine holds open connections, and
         they must be gone before the eventual DROP DATABASE
      2. archive the row (kept for audit; db_name preserved so the sweep and
         any reprovision both know which database this was)
      3. delete the snapshot, best-effort. It is a rebuildable catalog cache,
         not tenant data, so there is nothing to preserve for the grace period.

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

    # ── 2. Mark archived in the control-plane row ─────────────────────────────
    try:
        tenant.status = "archived"
        tenant.archived_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info(f"{log_prefix}: archived — database {db_name} kept for the grace period | {label}")
    except Exception as e:
        logger.error(f"{log_prefix}: failed to archive row | {label} | {e}", exc_info=True)
        return {"success": False, "error": f"archive failed: {e}"}

    # ── 3. Best-effort snapshot cleanup ───────────────────────────────────────
    # delete() is self-guarding (logs and returns on failure), so a leftover
    # snapshot never blocks a completed teardown. Keyed by tenant_id, matching
    # how snapshots are stored since the re-key.
    from tenant_snapshot_store import snapshot_store
    snapshot_store.delete(str(tenant.tenant_id))

    return {"success": True}