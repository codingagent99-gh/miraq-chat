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
import threading
from datetime import datetime, timezone
from chat_logger import get_logger

from flask import Flask, jsonify, request
import cors_manager as _cors_manager
from werkzeug.exceptions import HTTPException

from app_config import PORT, DEBUG, STORE_NAME, USE_RELOADER
from store_registry import get_store_loader, register_before_request
from models import db, Conversation
from routes.webhook_routes import webhook_bp
from routes.shopify_oauth import shopify_oauth_bp
from routes.chat import chat_bp
from routes.admin import admin_bp
from routes.products import products_bp
from routes.shopify import shopify_bp
from routes.provisioning import provisioning_bp
from routes.deactivation import deactivation_bp
import urllib.parse
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from routes.sales_rep import sales_rep_bp
from routes.channel import channel_bp
from routes.channel_link import channel_link_bp
# ═══════════════════════════════════════════
# FLASK APP & DATABASE
# ═══════════════════════════════════════════

app = Flask(__name__)


@app.after_request
def _apply_cors(response):
    origin = request.headers.get("Origin", "")
    if origin and _cors_manager.is_allowed(origin):
        _cors_manager.apply_cors(response, origin)
    return response


@app.before_request
def _handle_options_preflight():
    # Registered BEFORE register_before_request(app) below, so an OPTIONS
    # preflight is answered here and never reaches tenant resolution at all
    # — it carries no X-MiraQ-License-Id and shouldn't need one.
    if request.method == "OPTIONS":
        origin = request.headers.get("Origin", "")
        resp = jsonify({})
        if origin and _cors_manager.is_allowed(origin):
            _cors_manager.apply_cors(resp, origin)
        return resp, 200


from flask_migrate import Migrate
migrate = Migrate(app, db)

# Configure Database Connection
database_uri = os.getenv('DATABASE_URL', 'postgresql://postgres:admin@localhost:5432/miraq_chat')
app.config['SQLALCHEMY_DATABASE_URI'] = database_uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Postgres (or anything between us and it) drops connections that have sat
# idle. Without pre_ping, SQLAlchemy hands a dead connection straight to the
# first request after a quiet spell and it fails with "server closed the
# connection unexpectedly" — a 500 the user sees, on a request that was
# perfectly valid. pre_ping costs one trivial round trip per checkout and
# transparently reconnects; recycle retires connections before the far end
# is likely to have done it for us.
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,

    # Pool sizing. SQLAlchemy's defaults are pool_size=5 / max_overflow=10,
    # i.e. 15 connections per process -- never enough for concurrent chat
    # traffic. A 20-user load test (Sep 2026) hit the ceiling immediately and
    # threw "QueuePool limit of size 5 overflow 10 reached, connection timed
    # out, timeout 30.00", which surfaced to users as a 500 and dragged
    # median latency to 30-45s (requests parked on the 30s pool_timeout).
    #
    # 20 + 30 = 50 connections max from this process. Sized for gunicorn
    # later: N workers use up to N*50, so check Postgres's own
    # max_connections (default 100) before raising worker count -- 2 workers
    # already saturates a default Postgres.
    "pool_size": 20,
    "max_overflow": 30,

    # Fail fast instead of parking a request for 30s. If the pool is
    # genuinely exhausted, a quick error is more useful than a request that
    # looks hung -- and it makes pool pressure visible in load tests rather
    # than hiding it as latency.
    "pool_timeout": 10,
}

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

# Tenant resolution — binds g.tenant / g.store_loader / g.db_engine per
# request from X-MiraQ-License-Id. Registered here so it applies to every
# route below, including ones registered later in this file.
register_before_request(app)

# Create CONTROL-PLANE tables on startup
with app.app_context():
    from models import Tenant
    from models.shopify_token import ShopifyToken
    from models.channel_connection import ChannelConnection
    db.metadata.create_all(
        bind=db.engine,
        tables=[Tenant.__table__, ShopifyToken.__table__, ChannelConnection.__table__],
    )
    _cors_manager.refresh_from_db()   # seed dynamic origins from existing tenants

# Register blueprints
app.register_blueprint(chat_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(products_bp)
app.register_blueprint(shopify_bp)
app.register_blueprint(sales_rep_bp)
app.register_blueprint(provisioning_bp)
app.register_blueprint(deactivation_bp)
app.register_blueprint(webhook_bp)
app.register_blueprint(shopify_oauth_bp)
app.register_blueprint(channel_bp)
app.register_blueprint(channel_link_bp)

# ── Request timing instrumentation ───────────────────────────────────────────
# Writes plain text to logs/<date>/timing.txt, separate from chat.txt and
# api.txt. Two lines per request: START with the message, DONE with the
# duration. Off unless TIMING_LOG_ENABLED=true.
import timing_logger
timing_logger.init_app(app, db)

# ═══════════════════════════════════════════
# GLOBAL ERROR HANDLER
# ═══════════════════════════════════════════

@app.errorhandler(Exception)
def handle_global_exception(e):
    """
    Catches ALL unhandled exceptions across the entire Flask app.
    Forces the full traceback into our daily chat.txt log file,
    and prevents the frontend chatbot from receiving a broken HTML 500 page.

    HTTPExceptions are deliberately let through. Flask's _find_error_handler
    walks the class MRO, and werkzeug's NotFound/MethodNotAllowed/BadRequest
    all inherit from HTTPException -> Exception, so without this check an
    ordinary 404 for an unrouted URL ends up here: logged as CRITICAL with a
    routing traceback, and answered with a 500 body telling the caller the
    server broke. It did not — the URL simply does not exist, and the client
    needs the real status code to behave correctly.
    """
    logger = get_logger("miraq_chat")

    # The method and path are logged in BOTH branches. Without them a 404 line
    # says only that *something* hit a bad URL, which is not enough to tell a
    # frontend bug from a bot probe.
    where = f"{request.method} {request.path}"

    if isinstance(e, HTTPException):
        logger.warning(f"HTTP {e.code} | {where} | {e.name}")
        return e  # Flask renders the response the exception already carries

    logger.critical(f"🔥 UNHANDLED CRASH: {where} | {str(e)}", exc_info=True)

    try:
        db.session.rollback()
    except Exception:
        pass

    return jsonify({
        "success": False,
        "bot_message": "Oops! Something went wrong. Please try again in a moment.",
        "intent": "error",
        "products": [],
        "suggestions": ["Start over", "Show me all products"],
        "metadata": {"error": "Internal Server Error"}
    }), 500

# ═══════════════════════════════════════════
# ADDITIONAL ROUTES
# ═══════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """
    Lightweight liveness check.
    Returns 200 OK if the server is running, 503 if store is degraded.
    """
    from woo_client import upstream_health
    # Tenant is bound only if the probe sent X-MiraQ-License-Id AND that
    # tenant's loader is already resident (see store_registry
    # _OPTIONAL_TENANT_PATHS). No tenant / not resident = "unknown", which is
    # NOT a failure: this used to report store_degraded=True for every
    # header-less or cold probe, i.e. 503 + blocking for every tenant.
    from flask import g

    loader = get_store_loader()
    
    tenant = g.__dict__.get("tenant")
    store_degraded = bool(loader._degraded) if loader else False
    store_reasons = list(loader._degraded_reasons or []) if loader else []
    if loader is not None:
        store_component = "degraded" if store_degraded else "ok"
    elif tenant is not None and tenant.status == "warming":
        store_component = "warming"
    else:
        store_component = "unknown"
    upstream = upstream_health(str(tenant.tenant_id) if tenant is not None else None)
    

    # Three states:
    #   down     — block the UI. This backend is up (it answered), but the
    #              store it depends on is not usable, so letting someone type
    #              an order only produces a failure later.
    #   degraded — keep working, surface nothing or a soft notice. Salvaged
    #              bodies land here: the data was correct, so blacking out the
    #              widget would be a worse outcome than the fault itself.
    #   ok       — normal.
    #
    # If the backend is unreachable the client never gets a reply at all —
    # that is the client's own "down" signal and needs no representation here.
    if store_degraded or upstream["status"] == "down":
        overall = "down"
    elif upstream["status"] == "degraded":
        overall = "degraded"
    else:
        overall = "ok"

    reasons = list(store_reasons or []) if store_degraded else []
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
            "store": store_component,
            "upstream": upstream["status"],
        },
        "upstream": upstream,
        # Poll interval hint so the client does not have to hard-code one and
        # does not hammer a struggling server while it recovers.
        "retry_after_seconds": 5 if overall == "down" else 30,
    }), (503 if overall == "down" else 200)


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
    
    # Per-tenant: requires X-MiraQ-License-Id and reports
    # the token for THAT tenant's shop, not a process-wide env domain.
    from flask import g
    from models.shopify_token import ShopifyToken
    tenant = g.__dict__.get("tenant")
    shop_domain = (tenant.shopify_domain or "") if tenant is not None else ""
    
    if tenant is None or tenant.ecommerce_backend != "shopify" or not shop_domain:
        return jsonify({
            "status": "not_applicable",
            "message": "This tenant is not a Shopify store."
        }), 404
        
    row = ShopifyToken.query.get(shop_domain)
    if not row:
        return jsonify({
            "status": "missing",
            "store_domain": shop_domain,
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
    """
    Pure read of the tenant row's cached branding. The values are kept fresh
    by widget_branding.py (post-build fetch, 24h scheduler sweep, and the
    plugin's branding-push). This used to make a live call to the tenant's
    WordPress site on every widget load — one outbound request per page view,
    and a malformed URL for Shopify tenants (empty wp_base_url).
    """
    from flask import g
    tenant = g.__dict__.get("tenant")
    if tenant is None:
        return jsonify({"image_url": "", "text": "", "currency_symbol": ""}), 200
    # The store's currency, for the widget's cart/checkout fallbacks. Read
    # from the resident loader (already bound for this request) — no store call.
    _loader = get_store_loader()
    return jsonify({
        "image_url":       tenant.widget_logo_url or "",
        "text":            tenant.widget_header_text or "",
        "currency_symbol": getattr(_loader, "currency_symbol", "") or "",
    })

@app.route("/debug-plan")
def debug_plan():
    # Development only. License ids are visible in the browser, so anything
    # reachable with just that header is effectively public: this 404s unless
    # DEBUG is on, and never returns the DSN's password.
    if not DEBUG:
        return jsonify({"error": "not found"}), 404
    from flask import g
    from models import db
    from sqlalchemy.engine import make_url
    try:
        db_name = db.session.execute(db.text("SELECT current_database()")).scalar()
    except Exception as e:
        db_name = str(e)
    tenant = g.__dict__.get("tenant")
    return {
        "connected_database": db_name,
        "database_url_from_config": (
            make_url(app.config["SQLALCHEMY_DATABASE_URI"]).render_as_string(hide_password=True)
            if app.config.get("SQLALCHEMY_DATABASE_URI") else "not set"
        ),
        "tenant_id": str(tenant.tenant_id) if tenant else None,
        "plan": tenant.plan if tenant else None,
        "features": dict(tenant.features or {}) if tenant else {},
        "license_expires_at": (
            tenant.license_expires_at.isoformat()
            if tenant and tenant.license_expires_at else None
        ),
    }
   
# ═══════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════

def _print_dev_banner():
    # Currently uncalled: it fired from initialize_store()'s old eager
    # single-tenant boot (loader._loaded_from_cache), which no longer exists
    # now that loaders build lazily per-tenant. Left defined in case Phase 4/5
    # wants to reattach it to a per-tenant dev-cache-loaded signal.
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
    Start the tenant/engine registries and the shared refresh scheduler. No
    default tenant is loaded — every request must carry X-MiraQ-License-Id
    (register_before_request handles resolution). A tenant's StoreLoader is
    built lazily, on that tenant's first request, by TenantRegistry.get_loader().
    """
    from tenant_registry import TenantRegistry
    from models.db_engine_registry import DBEngineRegistry
    from refresh_scheduler import RefreshScheduler
    from store_registry import init_registries

    tenant_registry = TenantRegistry(app=app)
    engine_registry = DBEngineRegistry(base_dsn=database_uri)
    init_registries(tenant_registry, engine_registry)

    scheduler = RefreshScheduler(registry=tenant_registry, app=app)
    scheduler.start()

    logging.getLogger("miraq_chat").info(
        "initialize_store: registries ready — tenants served from DB, "
        "loaders built lazily per-request"
    )


# ═══════════════════════════════════════════
# STORE INITIALISATION (module level)
# ═══════════════════════════════════════════
# Runs on IMPORT, not just under __main__, because a WSGI server
# (gunicorn/waitress) imports this module rather than executing it as a
# script -- so anything inside `if __name__ == "__main__"` never runs and
# every worker would serve an empty catalog.
#
# Guarded so it happens exactly once per process:
#
#   - _store_init_lock: gunicorn's gthread worker and Werkzeug's threaded
#     mode can both import/serve concurrently; without the lock two threads
#     could each build a StoreLoader and one would silently win.
#   - _store_initialised: makes a second call a no-op rather than a second
#     full catalogue fetch.
#   - WERKZEUG_RUN_MAIN: with the Werkzeug reloader active the module is
#     imported twice (parent + reloaded child). Skipping the parent avoids
#     the duplicate live fetch that showed up in the load-test logs as two
#     "Initialization Complete" blocks.
#
# Set MIRAQ_SKIP_STORE_INIT=true to import this module without loading the
# catalogue (useful for migrations, shell scripts and tests).

_store_init_lock = threading.Lock()
_store_initialised = False


def init_store_once():
    """Idempotent, thread-safe wrapper around initialize_store()."""
    global _store_initialised
    if _store_initialised:
        return
    with _store_init_lock:
        if _store_initialised:
            return
        initialize_store()
        _store_initialised = True


def _should_init_store() -> bool:
    if os.getenv("MIRAQ_SKIP_STORE_INIT", "").strip().lower() in ("true", "1", "yes"):
        return False
    # Under the Werkzeug reloader the parent process only watches files; the
    # child (WERKZEUG_RUN_MAIN=true) is the one that serves requests.
    if USE_RELOADER and DEBUG and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        return False
    return True


if _should_init_store():
    init_store_once()


if __name__ == "__main__":
    print("=" * 60)
    print(f"  {STORE_NAME} — Chat API Server")
    print("=" * 60)
    print()

    # Already done at import time above unless explicitly skipped; this is a
    # no-op in the normal case and a safety net if it was skipped.
    init_store_once()

    print()
    print(f"🚀 Starting server on http://localhost:{PORT}")
    print(f"   POST http://localhost:{PORT}/chat")
    print(f"   GET  http://localhost:{PORT}/health")
    print(f"   GET  http://localhost:{PORT}/status")
    print(f"   GET  http://localhost:{PORT}/categories")
    print(f"   GET  http://localhost:{PORT}/shopify-token-status")
    print()

    # threaded=True: without this, Werkzeug's dev server handles exactly
    # ONE request at a time regardless of concurrent connections. This is
    # NOT implied by debug=True. Confirmed via load test Sep 2026 -- every
    # query type showed near-identical 6-8s latency regardless of actual
    # processing cost, and aggregate throughput stayed flat for the whole
    # run -- both signatures of requests queued behind a single serial
    # slot rather than genuine concurrency. This is still a dev server,
    # not a production WSGI server -- gunicorn is the real next step.
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=DEBUG,
        use_reloader= USE_RELOADER,
        threaded=True,
    )