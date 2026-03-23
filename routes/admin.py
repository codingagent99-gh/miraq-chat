from flask import Blueprint, jsonify
from store_registry import get_store_loader
from chat_logger import get_logger

logger = get_logger("miraq_admin")
admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

@admin_bp.route("/taxonomies", methods=["GET"])
def get_taxonomies():
    """
    Returns all cached categories, tags, and attributes instantly.
    No need to query WordPress!
    """
    loader = get_store_loader()
    if not loader:
        logger.error("Admin API: StoreLoader is not initialized.")
        return jsonify({"success": False, "error": "Store data not currently loaded."}), 500

    # Extract the lists from the cached dictionaries
    categories = list(loader.category_by_id.values()) if hasattr(loader, 'category_by_id') else []
    tags = list(loader.tag_by_id.values()) if hasattr(loader, 'tag_by_id') else []
    attributes = loader.all_attributes_raw if hasattr(loader, 'all_attributes_raw') else []

    # Sort them alphabetically for the dashboard
    categories.sort(key=lambda x: x.get('name', ''))
    tags.sort(key=lambda x: x.get('name', ''))
    
    return jsonify({
        "success": True,
        "data": {
            "categories": categories,
            "tags": tags,
            "attributes": attributes
        }
    }), 200