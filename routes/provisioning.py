"""
routes/provisioning.py — Tenant activation endpoint.

POST /provision-tenant — called once by the WordPress plugin, after the
plugin's own local check of the licensing-server signature, to register
(or re-register) a tenant with the MiraQ backend.

Exempt from tenant resolution (see store_registry._EXEMPT_PATHS) — this
endpoint creates the very tenant row that resolution would otherwise need.
Server-to-server only (plugin → backend), so CORS does not apply here.
"""

import hashlib
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from chat_logger import get_logger
from models import db, Tenant
from tenant_crypto import encrypt_secret
from license_verifier import verify_license_payload, LicenseVerificationError
from tenant_db_provisioner import ensure_tenant_database, TenantDBProvisionError
import threading
logger = get_logger("miraq_chat")

provisioning_bp = Blueprint("provisioning", __name__)


def _derive_db_name(license_id: str) -> str:
    """
    Deterministic, always-valid Postgres identifier derived from license_id.
    Hash-based rather than sanitised-passthrough so it's safe by construction —
    no character in license_id can produce an invalid or colliding identifier.
    Phase 4's CREATE DATABASE relies on this already being safe.
    """
    digest = hashlib.sha256(license_id.encode("utf-8")).hexdigest()[:16]
    return f"tenant_{digest}"


def _parse_expiry(raw):
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        logger.warning(f"provision-tenant: unparseable expiresAt={raw!r}")
        return None

def _create_tenant_schema(db_name: str, base_dsn: str) -> None:
    """
    Create all tables in a brand-new tenant database directly from the
    SQLAlchemy models via create_all(). This is always in sync with the
    current model definitions — no migration file needed, no drift possible.

    For future schema changes (ALTER TABLE etc.) after initial provisioning,
    migration_runner.run_migrations_for_all_tenants() handles those separately.
    """
    from flask import Flask
    from migration_runner import build_tenant_dsn
    from models import db as _db

    tenant_dsn = build_tenant_dsn(base_dsn, db_name)

    throwaway = Flask(__name__)
    throwaway.config["SQLALCHEMY_DATABASE_URI"] = tenant_dsn
    throwaway.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    _db.init_app(throwaway)
    with throwaway.app_context():
        _db.create_all()
        logger.info(f"provision-tenant: ✅ schema created via create_all | db={db_name}")

    try:
        with throwaway.app_context():
            _db.engine.dispose()
    except Exception:
        pass
    
@provisioning_bp.route("/provision-tenant", methods=["POST"])
def provision_tenant():
    body = request.get_json(silent=True) or {}

    raw_payload   = body.get("raw_payload")
    signature_b64 = body.get("signature")
    site_domain   = (body.get("site_domain") or "").strip()
    wp_base_url   = (body.get("wp_base_url") or "").strip().rstrip("/")
    woo_key       = (body.get("woo_key") or "").strip()
    woo_secret    = body.get("woo_secret") or ""

    if not raw_payload or not signature_b64:
        return jsonify({"success": False, "error": "missing raw_payload/signature"}), 400
    if not woo_key or not woo_secret:
        return jsonify({"success": False, "error": "missing woo credentials"}), 400

    try:
        claims = verify_license_payload(raw_payload, signature_b64)
    except LicenseVerificationError as e:
        logger.warning(f"provision-tenant: verification failed | {e}")
        return jsonify({"success": False, "error": "invalid signature"}), 401

    # license_id and expiry come ONLY from the verified payload — never from a
    # client-supplied field — so a valid signature for one licence can never
    # be used to write or overwrite a different tenant's row.
    license_id = claims.get("licenseId") or claims.get("license_id")
    if not license_id:
        return jsonify({"success": False, "error": "payload missing licenseId"}), 400

    license_expires_at = _parse_expiry(claims.get("expiresAt") or claims.get("expires_at"))

    tenant = Tenant.query.get(license_id)
    if tenant is None:
        tenant = Tenant(
            license_id=license_id,
            db_name=_derive_db_name(license_id),   # assigned ONCE, never changes
            status="active",
            features={},
        )
        db.session.add(tenant)
        logger.info(f"provision-tenant: new tenant | license_id={license_id} db={tenant.db_name}")
    else:
        logger.info(f"provision-tenant: re-activating tenant | license_id={license_id}")
        if tenant.status == "archived":
            tenant.archived_at = None
        tenant.status = "active"
        # db_name intentionally untouched — Phase 4 may already have created a
        # physical database under the original name.

    tenant.site_domain  = site_domain or tenant.site_domain
    tenant.wp_base_url  = wp_base_url or tenant.wp_base_url
    tenant.woo_key = woo_key
    tenant.woo_secret_encrypted = encrypt_secret(woo_secret)
    if license_expires_at is not None:
        tenant.license_expires_at = license_expires_at

    db.session.commit()
    
    # Refresh CORS immediately so the new tenant's domain is allowed
    # before the background build even starts.
    import cors_manager as _cors
    _cors.add_tenant_origin(tenant.site_domain or "")

    base_dsn = current_app.config["SQLALCHEMY_DATABASE_URI"]
    try:
        ensure_tenant_database(base_dsn, tenant.db_name)
        _create_tenant_schema(tenant.db_name, base_dsn)
        tenant.schema_migrated_at = datetime.now(timezone.utc)
    except (TenantDBProvisionError, Exception) as e:
        logger.error(f"provision-tenant: DB setup failed | license_id={license_id} | {e}")
        tenant.status = "provision_failed"
        tenant.last_build_error = str(e)
        db.session.commit()
        return jsonify({"success": False, "error": f"database setup failed: {e}"}), 500
    
    tenant.status = "warming"
    db.session.commit()

    _start_background_build(tenant.license_id, current_app._get_current_object())

    return jsonify({"success": True, "license_id": tenant.license_id, "status": tenant.status}), 200


_active_builds: set = set()
_active_builds_lock = threading.Lock()

def _start_background_build(license_id: str, app):
    with _active_builds_lock:
        if license_id in _active_builds:
            logger.info(f"_start_background_build: build already running | license_id={license_id}")
            return
        _active_builds.add(license_id)

    def _build():
        logger.info(f"_start_background_build: thread started | license_id={license_id}")
        try:
            with app.app_context():
                logger.info(f"_start_background_build: app context opened | license_id={license_id}")
                from store_registry import get_tenant_registry
                tenant_registry = get_tenant_registry()

                if tenant_registry is None:
                    logger.error(f"_start_background_build: tenant_registry is None | license_id={license_id}")
                    return

                tenant = Tenant.query.get(license_id)
                if tenant is None:
                    logger.error(f"_start_background_build: tenant vanished | license_id={license_id}")
                    return

                if tenant.status == "archived":
                    logger.info(f"_start_background_build: tenant archived, skipping | license_id={license_id}")
                    return

                logger.info(f"_start_background_build: starting build | license_id={license_id} wp_base_url={tenant.wp_base_url}")

                with tenant_registry.get_build_lock(license_id):
                    try:
                        loader = tenant_registry.get_loader(tenant)
                        if loader._degraded:
                            raise RuntimeError("; ".join(loader._degraded_reasons))
                        tenant.status = "active"
                        tenant.last_build_error = None
                        logger.info(f"_start_background_build: ✅ build complete | license_id={license_id}")
                    except Exception as e:
                        logger.error(f"_start_background_build: build failed | license_id={license_id} | {e}", exc_info=True)
                        tenant.status = "provision_failed"
                        tenant.last_build_error = str(e)
                    db.session.commit()
        except Exception as e:
            logger.error(f"_start_background_build: OUTER crash | license_id={license_id} | {e}", exc_info=True)
            try:
                with app.app_context():
                    tenant = Tenant.query.get(license_id)
                    if tenant and tenant.status == "warming":
                        tenant.status = "provision_failed"
                        tenant.last_build_error = f"Background thread crash: {str(e)}"
                        db.session.commit()
            except Exception as inner_e:
                logger.error(f"_start_background_build: could not update status after crash | {inner_e}")
        finally:
            with _active_builds_lock:
                _active_builds.discard(license_id)

    t = threading.Thread(target=_build, daemon=True)
    t.start()
    logger.info(f"_start_background_build: thread launched | license_id={license_id}")
