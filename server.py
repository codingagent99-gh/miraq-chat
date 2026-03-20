"""
Chat API Backend
Runs on port 5009 with /chat endpoint.

Usage:
    python server.py

Endpoint:
    POST http://localhost:5009/chat
    Body: {"message": "...", "session_id": "...", "user_context": {...}}
"""

import logging

from datetime import datetime, timezone

from flask import Flask, jsonify
from flask_cors import CORS

from app_config import PORT, DEBUG, STORE_NAME
from store_registry import set_store_loader, get_store_loader
from store_loader import StoreLoader, DEV_CACHE_ENABLED
from session_store import sessions
from routes.chat import chat_bp
from routes.admin import admin_bp


# ══════════════════════════════════════��════
# FLASK APP
# ═══════════════════════════════════════════

app = Flask(__name__)
CORS(app)

# Register blueprints
app.register_blueprint(chat_bp)
app.register_blueprint(admin_bp)

# ═══════════════════════════════════════════
# ADDITIONAL ROUTES
# ═══════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """
    Lightweight liveness check.
    Returns 200 OK if the server is running, 503 if store is degraded.
    Use /status for full detail.
    """
    loader = get_store_loader()
    degraded = loader._degraded if loader else True
    status_code = 503 if degraded else 200
    return jsonify({
        "status": "degraded" if degraded else "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "degraded": degraded,
        "degraded_reasons": loader._degraded_reasons if loader else ["store not initialised"],
    }), status_code


@app.route("/status", methods=["GET"])
def status():
    """
    Detailed store status endpoint.
    Returns full load state, per-resource counts, degraded flag, and next retry timing.
    Use this for monitoring dashboards or manual diagnosis.
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
    """Get session history."""
    if session_id in sessions:
        return jsonify({"session": sessions[session_id]})
    return jsonify({"error": "Session not found"}), 404


# ═══════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════

def _print_dev_banner():
    """Print a big obvious DEV MODE banner so you never mistake it for production."""
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
    print(f"  ██╔══██╗██╔═���══╝██║   ██║")
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
    """Load store data from WooCommerce at startup, then start background refresh."""
    loader = StoreLoader()
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

    # Show dev mode banner if data came from cache
    if DEV_CACHE_ENABLED and loader._loaded_from_cache:
        _print_dev_banner()


if __name__ == "__main__":
    print("=" * 60)
    print(f"  {STORE_NAME} — Chat API Server")
    print("=" * 60)
    print()

    # Load store data
    initialize_store()

    print()
    print(f"🚀 Starting server on http://localhost:{PORT}")
    print(f"   POST http://localhost:{PORT}/chat")
    print(f"   GET  http://localhost:{PORT}/health")
    print(f"   GET  http://localhost:{PORT}/status")
    print(f"   GET  http://localhost:{PORT}/categories")
    print()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=DEBUG,
    )