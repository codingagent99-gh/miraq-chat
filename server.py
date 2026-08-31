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
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from app_config import PORT, DEBUG, STORE_NAME, USE_RELOADER
from store_registry import set_store_loader, get_store_loader
from store_loader import StoreLoader, DEV_CACHE_ENABLED
from models import db, Conversation

from routes.chat import chat_bp
from routes.admin import admin_bp
from routes.products import products_bp
from routes.shopify import shopify_bp
import urllib.parse
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from routes.sales_rep import sales_rep_bp
# ═══════════════════════════════════════════
# FLASK APP & DATABASE
# ═══════════════════════════════════════════

app = Flask(__name__)
CORS(app,
    origins=[
        "https://wgc.net.in",
        "https://silfradigital.com",
        "https://silfratech.in",
        "http://localhost:5173",
        "http://localhost:5174",
        "https://staging-91e4-ecom-solutions9857d536fc-ugaqb.wpcomstaging.com",
        "https://silfra-store-4680.myshopify.com"
    ],
    supports_credentials=True
)
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

# Create Tables on Startup
with app.app_context():
    # Import ShopifyToken here so SQLAlchemy registers it before create_all()
    from models.shopify_token import ShopifyToken  # noqa: F401
    db.create_all()

# Register blueprints
app.register_blueprint(chat_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(products_bp)
app.register_blueprint(shopify_bp)
app.register_blueprint(sales_rep_bp)

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

    loader = get_store_loader()
    store_degraded = loader._degraded if loader else True
    store_reasons = (
        loader._degraded_reasons if loader else ["store not initialised"]
    )
    upstream = upstream_health()

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
            "store": "degraded" if store_degraded else "ok",
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
    from models.shopify_token import ShopifyToken
    from store_loader.config import SHOPIFY_STORE_DOMAIN

    row = ShopifyToken.query.get(SHOPIFY_STORE_DOMAIN)
    if not row:
        return jsonify({
            "status": "missing",
            "store_domain": SHOPIFY_STORE_DOMAIN,
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
    import requests as req
    from app_config import WOO_CONSUMER_KEY, _WP_BASE, WOO_CONSUMER_SECRET, BROWSER_HEADERS

    logger = get_logger("miraq_chat")
    target_url = f"{_WP_BASE}/wp-json/wdget-logo-uploader/v1/data"

    try:
        headers = {
            **BROWSER_HEADERS,
            "X-Consumer-Key":    WOO_CONSUMER_KEY,
            "X-Consumer-Secret": WOO_CONSUMER_SECRET,
        }
        resp = req.get(target_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return jsonify({
            "image_url": data.get("image_url", ""),
            "text":      data.get("text", ""),
        })
    except Exception as e:
        logger.error(f"widget_config: Failed — {type(e).__name__}: {e}", exc_info=True)
        return jsonify({"image_url": "", "text": ""}), 200

@app.route("/debug-plan")
def debug_plan():
    from models.chat_usage import CustomerPlan
    from models import db
    try:
        raw = db.session.execute(db.text("SELECT * FROM customer_plans WHERE id = 1")).fetchone()
        raw_result = str(raw)
    except Exception as e:
        raw_result = str(e)
    try:
        db_name = db.session.execute(db.text("SELECT current_database()")).scalar()
    except Exception as e:
        db_name = str(e)
    plan = CustomerPlan.query.filter_by(id=1).first()
    return {
        "connected_database": db_name,
        "database_url_from_config": app.config.get("SQLALCHEMY_DATABASE_URI", "not set"),
        "raw_sql_result": raw_result,
        "plan_exists": plan is not None,
        "is_premium": getattr(plan, "is_premium", None),
        "is_active_premium": getattr(plan, "is_active_premium", None),
    }
   
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
    Load store data from WooCommerce/Shopify at startup,
    then start background refresh (and Shopify token manager if applicable).

    The Flask app instance is passed into StoreLoader so the token manager
    can open app contexts from its background thread.
    """
    loader = StoreLoader(app=app)   # ← pass app so token manager can use DB from threads
    try:
        loader.load_all()
    except Exception as e:
        logging.getLogger("miraq_chat").error(
            f"Store loader error at startup: {e}", exc_info=True
        )
        logging.getLogger("miraq_chat").warning(
            "Server will respond with limited functionality until store data loads."
        )

    # Always register and always start background refresh
    set_store_loader(loader)
    loader.start_background_refresh()

    if DEV_CACHE_ENABLED and loader._loaded_from_cache:
        _print_dev_banner()


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