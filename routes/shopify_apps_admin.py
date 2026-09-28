"""
routes/shopify_apps_admin.py — register custom-distribution Shopify apps.

    GET    /admin/shopify-apps                   list registered custom apps (never the secret)
    GET    /admin/shopify-apps/<client_id>       one app, plus the URLs for its toml
    PUT    /admin/shopify-apps/<client_id>       add an app / update it
           {"client_secret": "...", "label": "...", "shops": ["store.myshopify.com"]}
    DELETE /admin/shopify-apps/<client_id>       remove an app no store runs on

The public app is NOT managed here; it stays in .env (SHOPIFY_CLIENT_ID /
SHOPIFY_CLIENT_SECRET).

AUTH: none, by decision. These routes are open to anyone who can reach the
server. Restrict /admin/shopify-apps at the proxy before this is internet-facing.

What keeps that from handing over a live store:
  * an app only takes over a store's traffic after that store completes a
    Shopify-verified OAuth install through it (installed_shops is written only
    by routes/shopify_oauth.py, never here);
  * once any store runs on an app, its secret can't be changed and it can't be
    deleted here — so an installed store can't be redirected to a secret
    someone else chose;
  * the secret is never returned by any route.
Rotating the secret of an app a store runs on is a deliberate server-side
step (update shopify_apps.client_secret_encrypted with tenant_crypto).
"""

import os
import re

from flask import Blueprint, jsonify, request
from sqlalchemy.orm.attributes import flag_modified

from chat_logger import get_logger
from models import db
from models.shopify_app import ShopifyApp
from tenant_crypto import encrypt_secret

logger = get_logger("miraq_admin")

shopify_apps_admin_bp = Blueprint("shopify_apps_admin", __name__, url_prefix="/admin/shopify-apps")

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")
_FIELDS = {"client_secret", "label", "shops"}


def _base_url() -> str:
    return (os.getenv("SHOPIFY_APP_BASE_URL", "").rstrip("/")
            or request.url_root.rstrip("/"))


def _view(row: ShopifyApp) -> dict:
    base = _base_url()
    return {
        "client_id": row.client_id,
        "label": row.label,
        "shops": list(row.shops or []),
        "installed_shops": list(row.installed_shops or []),
        "locked": bool(row.installed_shops),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        # Paste these into this app's shopify.app.<name>.toml.
        "toml": {
            "application_url": f"{base}/shopify/install/{row.client_id}",
            "auth.redirect_urls": [f"{base}/shopify/auth/callback/{row.client_id}"],
        },
    }


def _clean_shops(value):
    """(list, None) or (None, error)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return None, 'shops must be a list, e.g. ["store.myshopify.com"]'
    shops = [str(s or "").strip().lower() for s in value]
    bad = [s for s in shops if not _SHOP_RE.match(s)]
    if bad:
        return None, f"not a myshopify.com domain: {', '.join(bad)}"
    return list(dict.fromkeys(shops)), None


@shopify_apps_admin_bp.route("", methods=["GET"])
def list_apps():
    rows = ShopifyApp.query.order_by(ShopifyApp.created_at).all()
    return jsonify({"success": True, "apps": [_view(r) for r in rows]}), 200


@shopify_apps_admin_bp.route("/<client_id>", methods=["GET"])
def get_app(client_id):
    row = ShopifyApp.query.get(client_id)
    if row is None:
        return jsonify({"success": False, "error": "unknown app"}), 404
    return jsonify({"success": True, "app": _view(row)}), 200


@shopify_apps_admin_bp.route("/<client_id>", methods=["PUT"])
def put_app(client_id):
    from app_config import SHOPIFY_CLIENT_ID

    if not _CLIENT_ID_RE.match(client_id or ""):
        return jsonify({"success": False, "errors": ["client_id looks wrong (copy it from the app's Client credentials)"]}), 400
    if SHOPIFY_CLIENT_ID and client_id == SHOPIFY_CLIENT_ID:
        return jsonify({"success": False, "errors": ["that is the public app's client id; it is configured in .env, not here"]}), 400

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"success": False, "errors": ["body must be a JSON object"]}), 400
    errors = []
    unknown = set(body) - _FIELDS
    if unknown:
        errors.append(f"unknown field(s): {', '.join(sorted(unknown))}")

    shops = None
    if "shops" in body:
        shops, err = _clean_shops(body["shops"])
        if err:
            errors.append(err)
    secret = body.get("client_secret")
    if secret is not None and (not isinstance(secret, str) or len(secret.strip()) < 8):
        errors.append("client_secret looks wrong (copy it from the app's Client credentials)")
    label = body.get("label")
    if label is not None and not isinstance(label, str):
        errors.append("label must be text")

    row = ShopifyApp.query.get(client_id)
    if row is None and not secret:
        errors.append("client_secret is required when adding an app")
    if errors:
        return jsonify({"success": False, "errors": errors}), 400

    created = row is None
    if created:
        row = ShopifyApp(client_id=client_id, shops=[], installed_shops=[])
        db.session.add(row)
    elif secret and row.installed_shops:
        return jsonify({
            "success": False,
            "errors": [f"locked: {', '.join(row.installed_shops)} already run on this app, "
                       "so its secret can't be changed here"],
        }), 409

    if secret:
        row.client_secret_encrypted = encrypt_secret(secret.strip())
    if label is not None:
        row.label = label.strip() or None
    if shops is not None:
        row.shops = shops
        flag_modified(row, "shops")

    db.session.commit()
    logger.info(
        f"shopify-apps {'added' if created else 'updated'} | client_id={client_id} "
        f"label={row.label!r} shops={row.shops} secret_changed={bool(secret)}"
    )
    return jsonify({"success": True, "created": created, "app": _view(row)}), (201 if created else 200)


@shopify_apps_admin_bp.route("/<client_id>", methods=["DELETE"])
def delete_app(client_id):
    row = ShopifyApp.query.get(client_id)
    if row is None:
        return jsonify({"success": False, "error": "unknown app"}), 404
    if row.installed_shops:
        return jsonify({
            "success": False,
            "error": f"locked: {', '.join(row.installed_shops)} run on this app. "
                     "Uninstall it from those stores and install the public app (or another "
                     "custom app) first.",
        }), 409
    db.session.delete(row)
    db.session.commit()
    logger.info(f"shopify-apps deleted | client_id={client_id}")
    return jsonify({"success": True}), 200
