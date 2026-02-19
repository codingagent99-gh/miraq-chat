"""
WGC Tiles Store — Chat API Backend
Runs on port 5009 with /chat endpoint.

Usage:
    python server.py

Endpoint:
    POST http://localhost:5009/chat
    Body: {"message": "...", "session_id": "...", "user_context": {...}}
"""

from datetime import datetime, timezone

from flask import Flask, jsonify
from flask_cors import CORS

from app_config import PORT, DEBUG
from store_registry import set_store_loader, get_store_loader
from store_loader import StoreLoader
from session_store import sessions
from routes.chat import chat_bp

# ═══════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════

app = Flask(__name__)
CORS(app)

# Register blueprints
app.register_blueprint(chat_bp)


# ═══════════════════════════════════════════
# ADDITIONAL ROUTES
# ═══════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    loader = get_store_loader()
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "store": {
            "categories_loaded": len(loader.categories) if loader else 0,
            "tags_loaded": len(loader.tags) if loader else 0,
            "attributes_loaded": len(loader.attributes) if loader else 0,
        },
    })


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

def initialize_store():
    """Load store data from WooCommerce at startup, then start background refresh."""
    loader = StoreLoader()
    try:
        loader.load_all()
        set_store_loader(loader)
        # Start background refresh every 6 hours so data stays current
        loader.start_background_refresh()
    except Exception as e:
        print(f"⚠️  Store loader error: {e}")
        print("   Server will respond with limited functionality until store data loads.")
        # Still register the (partially loaded) loader so StoreLoader methods work
        set_store_loader(loader)


if __name__ == "__main__":
    print("=" * 60)
    print("  WGC Tiles Store — Chat API Server")
    print("=" * 60)
    print()

    # Load store data
    initialize_store()

    print()
    print(f"🚀 Starting server on http://localhost:{PORT}")
    print(f"   POST http://localhost:{PORT}/chat")
    print(f"   GET  http://localhost:{PORT}/health")
    print(f"   GET  http://localhost:{PORT}/categories")
    print()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=DEBUG,
    )
