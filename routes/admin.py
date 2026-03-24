import os
from flask import Blueprint, jsonify, request
from store_registry import get_store_loader
from chat_logger import get_logger

# Import the new helper functions from our scanner
from conflict_scanner import simulate_single_term, get_saved_queries, TEST_FILE_PATH

logger = get_logger("miraq_admin")
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

@admin_bp.route("/taxonomies", methods=["GET"])
def get_taxonomies():
    loader = get_store_loader()
    if not loader:
        return jsonify({"success": False, "error": "Store data not currently loaded."}), 500

    categories = list(loader.category_by_id.values()) if hasattr(loader, 'category_by_id') else []
    tags = list(loader.tag_by_id.values()) if hasattr(loader, 'tag_by_id') else []
    attributes = loader.all_attributes_raw if hasattr(loader, 'all_attributes_raw') else []
    
    products = []
    if hasattr(loader, 'product_by_name_lower'):
        for prod in loader.product_by_name_lower.values():
            products.append({"id": prod["id"], "name": prod["name"], "slug": prod.get("slug", "")})

    # ✅ FIX: Run simulate_single_term() on every saved query to build
    # test_results in the correct LiveTestResult shape the frontend expects.
    # Previously this returned loader.conflicts which is a raw internal list
    # and doesn't contain term/intent/confidence/entities/api_endpoint/api_body.
    saved_queries = get_saved_queries()
    test_results = []
    for query in saved_queries:
        result = simulate_single_term(query)
        test_results.append({
            "term": query,
            "locations": result["locations"],
            "auto_resolved": result.get("auto_resolved", []),
            "is_conflict": result["is_conflict"],
            "intent": result["intent"],
            "confidence": result["confidence"],
            "entities": result["entities"],
            "api_endpoint": result["api_endpoint"],
            "api_body": result["api_body"],
        })

    return jsonify({
        "success": True,
        "data": {
            "categories": sorted(categories, key=lambda x: x.get('name', '')),
            "tags": sorted(tags, key=lambda x: x.get('name', '')),
            "attributes": attributes,
            "products": sorted(products, key=lambda x: x.get('name', '')),
            "test_results": test_results,
            "saved_queries": saved_queries,
        }
    }), 200

@admin_bp.route("/test-query", methods=["POST"])
def test_live_query():
    query = request.json.get("query", "").strip()
    if not query:
        return jsonify({"success": False, "error": "No query provided"}), 400
        
    result = simulate_single_term(query)
    
    return jsonify({
        "success": True, 
        "data": {
            "term": query,
            "locations": result["locations"],
            "auto_resolved": result.get("auto_resolved", []), # 🚀 NEW
            "is_conflict": result["is_conflict"],
            "intent": result["intent"],
            "confidence": result["confidence"],
            "entities": result["entities"],
            "api_endpoint": result["api_endpoint"],
            "api_body": result["api_body"]
        }
    }), 200
   
@admin_bp.route("/add-test-query", methods=["POST"])
def add_test_query():
    """Appends a new question to the .txt file permanently."""
    query = request.json.get("query", "").strip()
    if not query:
        return jsonify({"success": False, "error": "No query provided"}), 400
        
    try:
        # Check if it already exists to prevent duplicates
        saved = get_saved_queries()
        if query in saved:
            return jsonify({"success": False, "error": "Query already exists in test suite."}), 400
            
        with open(TEST_FILE_PATH, 'a', encoding='utf-8') as f:
            f.write(f"\n{query}")
            
        return jsonify({"success": True, "message": "Query added to automated tests."}), 200
    except Exception as e:
        logger.error(f"Failed to save query: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@admin_bp.route("/refresh-cache", methods=["POST"])
def force_refresh_cache():
    loader = get_store_loader()
    if not loader: return jsonify({"success": False, "error": "Store loader not active."}), 500
    try:
        loader.load_all() 
        return jsonify({"success": True, "message": "Memory refreshed."}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500