from flask import Blueprint, jsonify
from store_registry import get_store_loader
from chat_logger import get_logger

logger = get_logger("miraq_admin")
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# The conflict-scanner dashboard is retired. /taxonomies, /test-query and
# /add-test-query went with it -- all three existed only to feed that UI, and
# all three were backed by conflict_scanner (simulate_single_term /
# get_saved_queries / TEST_FILE_PATH), which is now deleted. The module-level
# import lived here, and admin_bp is registered unconditionally in server.py,
# so leaving it behind would have turned a deleted file into a boot failure for
# the whole app rather than a 404 on one route.


@admin_bp.route("/refresh-cache", methods=["POST"])
def force_refresh_cache():
    """Force an immediate catalog reload, bypassing the version poll.

    Kept because it is the manual escape hatch when the catalog-version probe
    is wrong or unavailable. Safe to call on a request thread: build_all_lookups
    stages the rebuild and publishes it in one swap, so concurrent requests read
    the old catalog or the new one, never a half-built index.

    Does not touch _catalog_version. The next poll compares against whatever it
    last observed, so a forced reload here cannot mask a later edit.
    """
    loader = get_store_loader()
    if not loader:
        return jsonify({"success": False, "error": "Store loader not active."}), 500
    try:
        loader.load_all()
        return jsonify({"success": True, "message": "Memory refreshed."}), 200
    except Exception as e:
        logger.error(f"Forced cache refresh failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500