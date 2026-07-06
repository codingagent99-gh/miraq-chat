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
import threading
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app

from chat_logger import get_logger
from models import db, Tenant
from tenant_crypto import encrypt_secret
from license_verifier import verify_license_payload, LicenseVerificationError
from tenant_db_provisioner import ensure_tenant_database, TenantDBProvisionError

logger = get_logger("miraq_chat")

provisioning_bp = Blueprint("provisioning", __name__)

# Tracks license_ids with an active background build thread.
# Prevents duplicate threads from firing for the same tenant (scheduler retry
# + provisioning call overlap, etc.)
_active_builds: set = set()
_active_builds_lock = threading.Lock()


def _derive_db_name(license_id: str) -> str:
    """
    Deterministic, always-valid Postgres identifier derived from license_id.
    Hash-based so no character in license_id can produce an invalid identifier.
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
        logger.info(f"provision-tenant: ✅ tables created | db={db_name}")

    try:
        with throwaway.app_context():
            _db.engine.dispose()
    except Exception:
        pass

    # Stamp alembic_version directly via psycopg2 — avoids Flask-SQLAlchemy
    # connection pool hanging on the throwaway app's stamp() call.
    _stamp_alembic_version(tenant_dsn, db_name)


def _stamp_alembic_version(tenant_dsn: str, db_name: str) -> None:
    """Write the current alembic head revision directly via psycopg2."""
    import psycopg2
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    import os

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
    )

    try:
        # Get the current head revision from the migration scripts
        alembic_cfg = Config()
        alembic_cfg.set_main_option("script_location", migrations_dir)
        script = ScriptDirectory.from_config(alembic_cfg)
        head = script.get_current_head()

        if not head:
            logger.warning(f"provision-tenant: no alembic head found — skipping stamp | db={db_name}")
            return

        conn = psycopg2.connect(tenant_dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS alembic_version (
                version_num VARCHAR(32) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )
        """)
        cur.execute("DELETE FROM alembic_version")
        cur.execute("INSERT INTO alembic_version (version_num) VALUES (%s)", (head,))
        conn.close()
        logger.info(f"provision-tenant: ✅ schema created + stamped at {head} | db={db_name}")
    except Exception as e:
        logger.error(f"provision-tenant: stamp failed | db={db_name} | {e}", exc_info=True)
        # Non-fatal — tables are created, stamp is just for future migration tracking

@provisioning_bp.route("/provision-tenant", methods=["POST"])
def provision_tenant():
    body = request.get_json(silent=True) or {}

    raw_payload   = body.get("raw_payload")
    signature_b64 = body.get("signature")
    site_domain   = (body.get("site_domain") or "").strip()
    wp_base_url   = (body.get("wp_base_url") or "").strip().rstrip("/")
    woo_key       = (body.get("woo_key") or "").strip()
    woo_secret    = body.get("woo_secret") or ""

    logger.info(
        f"provision-tenant: received request | "
        f"site_domain={site_domain!r} wp_base_url={wp_base_url!r} "
        f"woo_key={'present' if woo_key else 'MISSING'} "
        f"woo_secret={'present' if woo_secret else 'MISSING'} "
        f"raw_payload={'present' if raw_payload else 'MISSING'} "
        f"signature={'present' if signature_b64 else 'MISSING'}"
    )

    if not raw_payload or not signature_b64:
        return jsonify({"success": False, "error": "missing raw_payload/signature"}), 400
    if not woo_key or not woo_secret:
        return jsonify({"success": False, "error": "missing woo credentials"}), 400

    try:
        claims = verify_license_payload(raw_payload, signature_b64)
    except LicenseVerificationError as e:
        logger.warning(f"provision-tenant: verification failed | {e}")
        return jsonify({"success": False, "error": "invalid signature"}), 401

    license_id = claims.get("licenseId") or claims.get("license_id")
    if not license_id:
        return jsonify({"success": False, "error": "payload missing licenseId"}), 400

    logger.info(f"provision-tenant: license_id={license_id}")

    license_expires_at = _parse_expiry(claims.get("expiresAt") or claims.get("expires_at"))

    tenant = Tenant.query.get(license_id)
    if tenant is None:
        tenant = Tenant(
            license_id=license_id,
            db_name=_derive_db_name(license_id),
            status="active",
            features={},
        )
        db.session.add(tenant)
        logger.info(f"provision-tenant: new tenant | license_id={license_id} db={tenant.db_name}")
    else:
        logger.info(f"provision-tenant: re-activating tenant | license_id={license_id} current_status={tenant.status}")
        if tenant.status == "archived":
            tenant.archived_at = None
        tenant.status = "active"

    tenant.site_domain  = site_domain or tenant.site_domain
    tenant.wp_base_url  = wp_base_url or tenant.wp_base_url
    tenant.woo_key = woo_key
    tenant.woo_secret_encrypted = encrypt_secret(woo_secret)
    if license_expires_at is not None:
        tenant.license_expires_at = license_expires_at

    db.session.commit()
    logger.info(f"provision-tenant: tenant row committed | license_id={license_id}")

    import cors_manager as _cors
    _cors.add_tenant_origin(tenant.site_domain or "")

    base_dsn = current_app.config["SQLALCHEMY_DATABASE_URI"]

    logger.info(f"provision-tenant: ensuring database exists | db={tenant.db_name}")
    try:
        ensure_tenant_database(base_dsn, tenant.db_name)
    except TenantDBProvisionError as e:
        logger.error(f"provision-tenant: ensure_tenant_database failed | license_id={license_id} | {e}", exc_info=True)
        tenant.status = "provision_failed"
        tenant.last_build_error = str(e)
        db.session.commit()
        return jsonify({"success": False, "error": f"database setup failed: {e}"}), 500

    logger.info(f"provision-tenant: creating schema | db={tenant.db_name}")
    try:
        _create_tenant_schema(tenant.db_name, base_dsn)
        tenant.schema_migrated_at = datetime.now(timezone.utc)
    except Exception as e:
        logger.error(f"provision-tenant: schema creation failed | license_id={license_id} | {e}", exc_info=True)
        tenant.status = "provision_failed"
        tenant.last_build_error = str(e)
        db.session.commit()
        return jsonify({"success": False, "error": f"schema creation failed: {e}"}), 500

    tenant.status = "warming"
    db.session.commit()
    logger.info(f"provision-tenant: status=warming | license_id={license_id} — starting background build")

    _start_background_build(tenant.license_id, current_app._get_current_object())

    return jsonify({"success": True, "license_id": tenant.license_id, "status": tenant.status}), 200


def _start_background_build(license_id: str, app):
    """
    Kick off the catalog build in a background thread.
    _active_builds prevents duplicate threads for the same tenant.
    get_loader() handles its own internal build lock — do NOT acquire
    get_build_lock() here, that causes a deadlock (same lock, same thread).
    """
    with _active_builds_lock:
        if license_id in _active_builds:
            logger.info(f"_start_background_build: build already running — skipping | license_id={license_id}")
            return
        _active_builds.add(license_id)
        logger.info(f"_start_background_build: registered in active builds | license_id={license_id} | active={len(_active_builds)}")

    def _build():
        logger.info(f"_start_background_build: thread started | license_id={license_id}")
        try:
            with app.app_context():
                logger.info(f"_start_background_build: app context opened | license_id={license_id}")

                from store_registry import get_tenant_registry
                tenant_registry = get_tenant_registry()

                if tenant_registry is None:
                    logger.error(f"_start_background_build: tenant_registry is None — registries not initialized yet | license_id={license_id}")
                    return

                logger.info(f"_start_background_build: fetching tenant row | license_id={license_id}")
                tenant = Tenant.query.get(license_id)

                if tenant is None:
                    logger.error(f"_start_background_build: tenant row not found in DB | license_id={license_id}")
                    return

                if tenant.status == "archived":
                    logger.info(f"_start_background_build: tenant is archived — skipping build | license_id={license_id}")
                    return

                logger.info(
                    f"_start_background_build: starting build | "
                    f"license_id={license_id} | "
                    f"wp_base_url={tenant.wp_base_url!r} | "
                    f"woo_key={'present' if tenant.woo_key else 'MISSING'} | "
                    f"woo_secret={'present' if tenant.woo_secret_encrypted else 'MISSING'}"
                )

                # ── NOTE: do NOT wrap this in get_build_lock() ──────────────────
                # get_loader() acquires the build lock internally. Acquiring it
                # here too causes a deadlock (same non-reentrant lock, same thread).
                # _active_builds above is the duplicate-build guard; the lock
                # inside get_loader() is the single-flight rehydration guard.
                try:
                    logger.info(f"_start_background_build: calling get_loader() | license_id={license_id}")
                    loader = tenant_registry.get_loader(tenant)
                    logger.info(
                        f"_start_background_build: get_loader() returned | "
                        f"license_id={license_id} | "
                        f"degraded={loader._degraded} | "
                        f"products={len(loader.products)} | "
                        f"categories={len(loader.categories)}"
                    )
                    if loader._degraded:
                        reasons = "; ".join(loader._degraded_reasons)
                        logger.error(f"_start_background_build: loader degraded | license_id={license_id} | reasons={reasons}")
                        raise RuntimeError(f"Loader degraded: {reasons}")
                    tenant.status = "active"
                    tenant.last_build_error = None
                    logger.info(f"_start_background_build: ✅ build complete | license_id={license_id}")
                except Exception as e:
                    logger.error(
                        f"_start_background_build: build failed | license_id={license_id} | {e}",
                        exc_info=True
                    )
                    tenant.status = "provision_failed"
                    tenant.last_build_error = str(e)

                logger.info(f"_start_background_build: committing status={tenant.status} | license_id={license_id}")
                db.session.commit()
                logger.info(f"_start_background_build: status committed | license_id={license_id}")

        except Exception as e:
            logger.error(
                f"_start_background_build: OUTER crash (outside app context) | license_id={license_id} | {e}",
                exc_info=True
            )
            try:
                with app.app_context():
                    tenant = Tenant.query.get(license_id)
                    if tenant and tenant.status == "warming":
                        tenant.status = "provision_failed"
                        tenant.last_build_error = f"Background thread crash: {str(e)}"
                        db.session.commit()
                        logger.info(f"_start_background_build: status set to provision_failed after outer crash | license_id={license_id}")
            except Exception as inner_e:
                logger.error(f"_start_background_build: could not update status after outer crash | license_id={license_id} | {inner_e}")
        finally:
            with _active_builds_lock:
                _active_builds.discard(license_id)
            logger.info(f"_start_background_build: thread finished, removed from active builds | license_id={license_id}")

    t = threading.Thread(target=_build, daemon=True, name=f"build-{license_id[:8]}")
    t.start()
    logger.info(f"_start_background_build: thread launched | license_id={license_id} | thread={t.name}")