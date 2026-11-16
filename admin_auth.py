"""
admin_auth.py — the key for the operator-only admin routes:

    /admin/translation/...   (routes/translation_admin.py)
    /admin/shopify-apps/...  (routes/shopify_apps_admin.py)

Set MIRAQ_ADMIN_KEY in .env (a long random string) and send it on every call:

    curl -H "Authorization: Bearer $MIRAQ_ADMIN_KEY" .../admin/shopify-apps

With MIRAQ_ADMIN_KEY unset these routes answer 404, so they are closed by
default. They used to be open to anyone who could reach the server, which let
a stranger read a store's tenant id, change its settings, or register a
custom app whose secret they chose.
"""

import hmac
import os

from flask import jsonify, request

from chat_logger import get_logger

logger = get_logger("miraq_admin")


def require_admin_key():
    """before_request hook: None to let the request through, else an error response."""
    key = os.getenv("MIRAQ_ADMIN_KEY", "").strip()
    if not key:
        return jsonify({"error": "not found"}), 404
    scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), key.encode()):
        logger.warning(f"admin route refused: bad or missing admin key | path={request.path}")
        return jsonify({"error": "unauthorized"}), 401
    return None
