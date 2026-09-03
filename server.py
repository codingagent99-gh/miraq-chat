"""
Chat API Backend
Runs on port 5009 with /chat endpoint.

Usage:
    python server.py

Endpoint:
    POST http://localhost:5009/chat
    Body: {"message": "...", "session_id": "...", "user_context": {...}}
"""

import os
import logging
from datetime import datetime, timezone
from chat_logger import get_logger

from flask import Flask, jsonify, request
import cors_manager as _cors_manager

from app_config import PORT, DEBUG, STORE_NAME, USE_RELOADER
from store_registry import get_store_loader, register_before_request
from models import db, Conversation
from store_loader import load_vector_model
from routes.chat import chat_bp
from routes.admin import admin_bp
from routes.products import products_bp
from routes.shopify import shopify_bp
import urllib.parse
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from routes.sales_rep import sales_rep_bp
from routes.provisioning import provisioning_bp
from routes.deactivation import deactivation_bp
from routes.catalog_push import catalog_push_bp
from routes.test_fuzzy import test_fuzzy_bp
# ═══════════════════════════════════════════
# FLASK APP & DATABASE
# ═══════════════════════════════════════════
logger = get_logger(__name__)
app = Flask(__name__)
@app.after_request
def _apply_cors(response):
    from flask import request as _req
    origin = _req.headers.get("Origin", "")
    if origin and _cors_manager.is_allowed(origin):
        _cors_manager.apply_cors(response, origin)
    return response

@app.before_request
def _handle_options():
    from flask import request as _req, jsonify
    if _req.method == "OPTIONS":
        origin = _req.headers.get("Origin", "")
        resp = jsonify({})
        if origin and _cors_manager.is_allowed(origin):
            _cors_manager.apply_cors(resp, origin)
        return resp, 200

 
from flask_migrate import Migrate
migrate = Migrate(app, db)
register_before_request(app)

# Configure Database Connection
database_uri = os.getenv('DATABASE_URL', 'postgresql://postgres:admin@localhost:5432/miraq_chat')
app.config['SQLALCHEMY_DATABASE_URI'] = database_uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

def ensure_database_exists(db_uri):
    """
    Connects to the default 'postgres' database to check if our target
    database exists. If not, it creates it.
    """
    result = urllib.parse.urlparse(db_uri)
    username = result.username
    password = result.password
    hostname = result.hostname
    port = result.port
    database_name = result.path[1:]

    try:
        conn = psycopg2.connect(
            dbname='postgres',
            user=username,
            password=password,
            host=hostname,
            port=port
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = conn.cursor()

        cursor.execute(f"SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{database_name}'")
        exists = cursor.fetchone()

        if not exists:
            print(f"📦 Database '{database_name}' not found. Creating it now...")
            cursor.execute(f"CREATE DATABASE {database_name}")
            print(f"✅ Database '{database_name}' created successfully!")

        cursor.close()
        conn.close()

    except Exception as e:
        print(f"⚠️ Could not verify or create database automatically: {e}")
        print("Make sure your PostgreSQL server is running and the credentials are correct.")

ensure_database_exists(database_uri)

# Bind Database to App
db.init_app(app)

# Seed dynamic CORS origins on startup. Schema creation/updates are owned
# entirely by Alembic (`flask db upgrade`) — do NOT call db.create_all()
# here, since it races ahead of migrations on every CLI invocation
# (including `flask db upgrade` itself, because Flask's CLI imports this
# module to find `app` before running the migration command).
with app.app_context():
    _cors_manager.refresh_from_db()   # seed dynamic origins from existing tenants


# Register blueprints
app.register_blueprint(chat_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(products_bp)
app.register_blueprint(shopify_bp)
app.register_blueprint(sales_rep_bp)
app.register_blueprint(provisioning_bp)
app.register_blueprint(deactivation_bp)
app.register_blueprint(catalog_push_bp)
app.register_blueprint(test_fuzzy_bp)
try:
    from routes.webhook_routes import webhook_bp
    app.register_blueprint(webhook_bp)
    logger.info("Webhook routes (plugin catalog push) blueprint registered")
except ImportError as e:
    logger.warning(f"Webhook routes blueprint not available: {e}")
   
# ═══════════════════════════════════════════
# GLOBAL ERROR HANDLER
# ═══════════════════════════════════════════

@app.errorhandler(Exception)
def handle_global_exception(e):
    """
    Catches ALL unhandled exceptions across the entire Flask app.
    Forces the full traceback into our daily chat.txt log file,
    and prevents the frontend chatbot from receiving a broken HTML 500 page.
    """
    logger = get_logger("miraq_chat")
    logger.critical(f"🔥 UNHANDLED CRASH: {str(e)}", exc_info=True)

    try:
        db.session.rollback()
    except Exception:
        pass

    return jsonify({
        "success": False,
        "bot_message": "Oops! I encountered an unexpected Error.",
        "intent": "error",
        "products": [],
        "suggestions": ["Start over", "Browse Products"],
        "metadata": {"error": "Internal Server Error"}
    }), 500

# ═══════════════════════════════════════════
# ADDITIONAL ROUTES
# ═══════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """
    Liveness check, and the signal the widget's down-overlay is driven from.

    Three states:
      down     — block the UI. This backend is up (it answered), but the store
                 it depends on is not usable, so letting someone type an order
                 only produces a failure later.
      degraded — keep working, surface nothing or a soft notice. Salvaged
                 response bodies land here: the data was correct, so blacking
                 out the widget would be a worse outcome than the fault itself.
      ok       — normal.

    If the backend is unreachable the client never gets a reply at all — that
    is the client's own "down" signal and needs no representation here.

    TENANT RESOLUTION (multi-store): /health is in _EXEMPT_PATHS, so the
    before_request hook leaves g.store_loader as None and get_store_loader()
    returns None on EVERY call — which used to make this endpoint report
    "degraded" unconditionally. Exempt is still the right default: /health must
    answer even for an unknown or missing license, since proving the backend is
    reachable is half its job. So the tenant is resolved HERE, optionally: with
    a valid license header we report that tenant's store health, and without one
    we report only what is knowable process-wide.
    """
    from woo_client import upstream_health

    loader = get_store_loader()
    store_known = False

    if loader is None:
        loader, store_known = _health_loader_for_request()
    else:
        store_known = True

    store_degraded = bool(loader._degraded) if loader is not None else False
    store_reasons = list(getattr(loader, "_degraded_reasons", []) or []) if loader is not None else []

    if store_known and loader is None:
        # A license was supplied and named a tenant we could not load. That is
        # a real fault for this caller, not an unknown, so it blocks.
        store_degraded = True
        store_reasons = ["store not initialised for this tenant"]

    upstream = upstream_health()

    if store_degraded or upstream["status"] == "down":
        overall = "down"
    elif upstream["status"] == "degraded":
        overall = "degraded"
    else:
        overall = "ok"

    reasons = list(store_reasons) if store_degraded else []
    reasons += upstream["reasons"]

    return jsonify({
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # `degraded` kept as-is for existing callers: it has always meant
        # "the store loader is unhealthy" and other code reads it.
        "degraded": store_degraded,
        "degraded_reasons": reasons or store_reasons,
        "blocking": overall == "down",
        "components": {
            "backend": "ok",   # reaching this line proves it
            # "unknown", not "ok": with no license header there is no store to
            # speak for, and claiming health we cannot observe is worse than
            # admitting we cannot see it.
            "store": ("degraded" if store_degraded else "ok") if store_known else "unknown",
            "upstream": upstream["status"],
        },
        "upstream": upstream,
        # Poll interval hint so the client does not have to hard-code one and
        # does not hammer a struggling server while it recovers.
        "retry_after_seconds": 5 if overall == "down" else 30,
    }), (503 if overall == "down" else 200)


def _health_loader_for_request():
    """Best-effort tenant loader for /health only.

    Returns (loader, store_known). store_known is False when the caller sent no
    license header — there is simply no store to report on, which is different
    from a store that is down. Never raises: /health answering at all is the
    point, so any failure here degrades to (None, ...) rather than a 500.
    """
    license_id = request.headers.get("X-MiraQ-License-Id", "").strip()
    if not license_id:
        return None, False
    try:
        from models import Tenant
        from store_registry import get_tenant_registry

        tenant = Tenant.query.filter_by(license_id=license_id).first()
        if tenant is None:
            return None, True
        registry = get_tenant_registry()
        if registry is None:
            return None, True
        return registry.get_loader(tenant), True
    except Exception as e:
        logger.warning(f"/health: could not resolve tenant loader | {e}")
        return None, True


@app.route("/status", methods=["GET"])
def status():
    """
    Detailed store status endpoint.
    """
    loader = get_store_loader()
    if not loader:
        return jsonify({
            "status": "unavailable",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "store": None,
        }), 503

    store_status = loader.get_status()
    http_code = 503 if store_status["degraded"] else 200
    return jsonify({
        "status": "degraded" if store_status["degraded"] else "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "store": store_status,
    }), http_code


@app.route("/shopify-token-status", methods=["GET"])
def shopify_token_status():
    """
    Diagnostic endpoint — shows the current state of the Shopify OAuth token
    stored in Postgres. Safe to expose internally; does NOT return the token value.

    Returns:
        200  — token is healthy
        206  — token is near expiry (< 1 h) but still valid
        503  — token is expired or missing
    """
    from models.shopify_token import ShopifyToken
    loader = get_store_loader()
    shopify_domain = loader.shopify_domain if loader else ""

    row = ShopifyToken.query.get(shopify_domain)
    if not row:
        return jsonify({
            "status": "missing",
            "store_domain": shopify_domain,
            "message": "No token found in DB. Has the server started with valid credentials?",
        }), 503

    hours_remaining = row.seconds_until_expiry / 3600

    if row.is_expired:
        http_code = 503
        status_label = "expired"
    elif row.needs_refresh:
        http_code = 206
        status_label = "near_expiry"
    else:
        http_code = 200
        status_label = "healthy"

    return jsonify({
        "status":          status_label,
        "store_domain":    row.store_domain,
        "scope":           row.scope,
        "fetched_at":      row.fetched_at.isoformat(),
        "expires_at":      row.expires_at.isoformat(),
        "hours_remaining": round(hours_remaining, 2),
        "refresh_count":   row.refresh_count,
        "last_error":      row.last_error,
    }), http_code


@app.route("/categories", methods=["GET"])
def list_categories():
    """List all loaded categories."""
    loader = get_store_loader()
    if not loader or not loader.categories:
        return jsonify({"categories": [], "message": "No categories loaded"})

    cats = []
    for cat in loader.categories:
        if cat.get("slug") != "uncategorized":
            cats.append({
                "id": cat["id"],
                "name": cat.get("name", ""),
                "slug": cat.get("slug", ""),
                "count": cat.get("count", 0),
                "parent": cat.get("parent", 0),
            })
    return jsonify({"categories": cats})


@app.route("/session/<session_id>", methods=["GET"])
def get_session(session_id):
    """Get session history from Postgres."""
    import uuid
    try:
        session_uuid = uuid.UUID(session_id)
        conversation = db.session.get(Conversation, session_uuid)

        if conversation:
            return jsonify({
                "session": {
                    "id": str(conversation.id),
                    "flow_state": conversation.flow_state,
                    "context_data": conversation.context_data,
                    "created_at": conversation.created_at.isoformat(),
                    "updated_at": conversation.updated_at.isoformat(),
                    "message_count": len(conversation.messages)
                }
            })
    except ValueError:
        pass

    return jsonify({"error": "Session not found"}), 404

@app.route("/widget-config", methods=["GET"])
def widget_config():
    from flask import g
    tenant = g.__dict__.get("tenant")
    if not tenant:
        return jsonify({"image_url": "", "text": ""}), 200
    return jsonify({
        "image_url": tenant.widget_logo_url or "",
        "text":      tenant.widget_header_text or "",
    })

# ═══════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════

def _print_dev_banner():
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    DIM = "\033[2m"

    print()
    print(f"{RED}{BOLD}{'━' * 60}")
    print(f"{'':>10}🚧  D E V   M O D E  🚧")
    print(f"{'━' * 60}{RESET}")
    print(f"{YELLOW}{BOLD}")
    print(f"  ██████╗ ███████╗██╗   ██╗")
    print(f"  ██╔══██╗██╔════╝██║   ██║")
    print(f"  ██║  ██║█████╗  ██║   ██║")
    print(f"  ██║  ██║██╔══╝  ╚██╗ ██╔╝")
    print(f"  ██████╔╝███████╗ ╚████╔╝ ")
    print(f"  ╚═════╝ ╚══════╝  ╚═══╝  ")
    print(f"{RESET}")
    print(f"{YELLOW}  Store data loaded from LOCAL CACHE (not live API)")
    print(f"  Cache file: .dev_cache/store_data.json")
    print()
    print(f"  {DIM}• Data may be stale — prices/stock not real-time{RESET}")
    print(f"  {DIM}• To refresh: set DEV_CACHE_BUST=true or delete .dev_cache/{RESET}")
    print(f"  {DIM}• To disable: remove DEV_CACHE=true from .env{RESET}")
    print(f"{RED}{BOLD}{'━' * 60}{RESET}")
    print()


def initialize_store():
    """
    Start the shared vector model, registries, and refresh scheduler.
    No default tenant is loaded — every request must carry X-MiraQ-License-Id.
    All tenant config (URLs, credentials) lives in the tenants table.
    """
    vector_model = load_vector_model()

    from tenant_registry import TenantRegistry
    from db_engine_registry import DBEngineRegistry
    from refresh_scheduler import RefreshScheduler
    from store_registry import init_registries

    tenant_registry = TenantRegistry(vector_model=vector_model, app=app)
    engine_registry = DBEngineRegistry(base_dsn=database_uri)
    init_registries(tenant_registry, engine_registry)

    scheduler = RefreshScheduler(registry=tenant_registry, app=app)
    scheduler.start()

    logging.getLogger("miraq_chat").info(
        "initialize_store: registries ready — all tenants served from DB"
    )

if __name__ == "__main__":
    print("=" * 60)
    print(f"  {STORE_NAME} — Chat API Server")
    print("=" * 60)
    print()

    initialize_store()

    print()
    print(f"🚀 Starting server on http://localhost:{PORT}")
    print(f"   POST http://localhost:{PORT}/chat")
    print(f"   GET  http://localhost:{PORT}/health")
    print(f"   GET  http://localhost:{PORT}/status")
    print(f"   GET  http://localhost:{PORT}/categories")
    print(f"   GET  http://localhost:{PORT}/shopify-token-status")
    print()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=DEBUG,
        use_reloader= USE_RELOADER,
    )