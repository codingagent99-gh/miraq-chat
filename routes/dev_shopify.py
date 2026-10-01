"""
routes/dev_shopify.py — one-step Shopify tenant provisioning for LOCAL DEV.

The only production path that creates a Shopify tenant is the OAuth install
(routes/shopify_oauth.py). That needs a public HTTPS URL for Shopify to
redirect back to, so on localhost there was no way in short of creating a
WooCommerce tenant and hand-editing its row. This does what the OAuth
callback does, minus the consent screen:

    POST   /dev/shopify/tenant   {"shop": "x.myshopify.com",
                                  "admin_token": "shpat_..."        (optional),
                                  "client_id": "<custom app>",      (optional)
                                  "client_secret": "<its secret>",  (optional)
                                  "force": false}                   (optional)
    GET    /dev/shopify/tenant?shop=x.myshopify.com
    DELETE /dev/shopify/tenant?shop=x.myshopify.com

POST — idempotent. Obtains an Admin API token and checks it against the
store, creates (or re-activates) the tenant row, stores the token where the
token manager looks for it, creates the tenant database + schema, evicts any
cached loader and starts the catalog build. Same steps, same helpers, same
order as shopify_auth_callback — only where the token comes from differs:

  * default      client_credentials grant with the app's credentials (.env's
                 SHOPIFY_CLIENT_ID/SECRET, or the custom app named by
                 client_id). Shopify only grants this for a store in the
                 SAME organisation as the app, with the app installed on it.
                 The token manager renews it the same way afterwards.
  * client_id + client_secret
                 same grant, with an app you name here. Registered (or
                 checked against its existing registration) in shopify_apps
                 first. This is the way in for a PUBLISHED store: an app
                 created in that store's own organisation (Dev Dashboard),
                 installed on it.
  * admin_token  an Admin API token you already have for the store. Stored
                 with a far-future expiry and no refresh token, so the token
                 manager uses it as-is and never tries to renew it.

PUBLISHED STORES
Everything this backend sends to Shopify is a read (catalog, products,
orders for order status) — order creation on Shopify is a stub and the cart
lives in the shopper's browser — so pointing a local backend at a live store
changes nothing in that store. What CAN go wrong is on our side: if this
database already holds the store's real OAuth install (a refresh token),
overwriting it would strand that install once its access token expires. POST
refuses that case unless "force" is set.

GET — tenant row, token bookkeeping and whether its loader is resident.
DELETE — the same teardown the app/uninstalled webhook runs (archive + evict;
the database is kept for the grace period, and a later POST brings it back).
Local only: nothing is sent to Shopify, the app stays installed in the store.

ACCESS: every route 404s unless DEV_PROVISIONING_ENABLED is true. Requests
from this machine (loopback, not proxied) are then allowed as-is; anything
else — including requests through nginx, which arrive from 127.0.0.1 but
carry X-Forwarded-For — must send X-Dev-Provisioning-Key matching
DEV_PROVISIONING_KEY. With no key configured, only local requests work.
"""

import hmac
import os
import re
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone

import requests as http_requests
from flask import Blueprint, current_app, jsonify, request

from app_config import SHOPIFY_API_VERSION
from chat_logger import get_logger
from models import db, Tenant
from models.shopify_token import ShopifyToken

logger = get_logger("miraq_chat")

dev_shopify_bp = Blueprint("dev_shopify", __name__, url_prefix="/dev/shopify")

_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")

# A token supplied by hand has no expiry we can learn and nothing to renew it
# with. Far enough out that ShopifyToken.needs_refresh never fires.
_MANUAL_TOKEN_LIFETIME = timedelta(days=3650)


def _enabled() -> bool:
    return os.getenv("DEV_PROVISIONING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded")


def _is_local_request() -> bool:
    """Sent from this machine directly. A reverse proxy also connects from
    loopback, so any proxy header means the real client is somewhere else."""
    if request.remote_addr not in _LOOPBACK:
        return False
    return not any(request.headers.get(h) for h in _PROXY_HEADERS)


@dev_shopify_bp.before_request
def _gate():
    if not _enabled():
        # 404, not 403: don't advertise that the route exists.
        return jsonify({"error": "not found"}), 404
    if _is_local_request():
        return None
    key = os.getenv("DEV_PROVISIONING_KEY", "").strip()
    supplied = request.headers.get("X-Dev-Provisioning-Key", "").strip()
    if key and supplied and hmac.compare_digest(key, supplied):
        return None
    logger.warning(
        f"dev provision: refused non-local request | remote={request.remote_addr} "
        f"key={'wrong' if supplied else 'missing'} configured={bool(key)}"
    )
    return jsonify({
        "error": "dev provisioning is only open to local requests unless "
                 "DEV_PROVISIONING_KEY is set and sent as X-Dev-Provisioning-Key",
    }), 403


@dev_shopify_bp.errorhandler(Exception)
def _report_crash(e):
    """This is a dev tool: say what actually broke instead of the app-wide
    "Oops! Something went wrong", so the script can print it. The full
    traceback still goes to the log."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    import traceback
    logger.critical(f"dev provision: crash on {request.method} {request.path} | {e}", exc_info=True)
    try:
        db.session.rollback()
    except Exception:
        pass
    frames = traceback.extract_tb(e.__traceback__)
    where = f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno} in {frames[-1].name}" if frames else "?"
    return jsonify({
        "success": False,
        "step": "crash",
        "error": f"{type(e).__name__}: {e}",
        "where": where,
    }), 500


def _shop_arg(value) -> str:
    shop = str(value or "").strip().lower()
    shop = re.sub(r"^https?://", "", shop).rstrip("/")
    if shop and "." not in shop:
        shop = f"{shop}.myshopify.com"   # allow the bare store handle
    return shop


def _json_or_reason(resp, what: str):
    """(dict, None), or (None, reason) when Shopify answered with something
    that isn't a JSON object — typically an HTML page after a redirect (a
    wrong or renamed shop domain, a password-protected storefront, a login
    page). resp.url shows where the redirect ended up."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        return body, None
    ctype = resp.headers.get("Content-Type", "?")
    moved = f" (redirected to {resp.url})" if getattr(resp, "history", None) else ""
    return None, (
        f"{what} answered HTTP {resp.status_code} with {ctype}, not JSON{moved}. "
        f"Check the shop is the store's exact myshopify.com domain. "
        f"Start of body: {resp.text[:200]!r}"
    )


def _check_token(shop: str, token: str):
    """(shop_name, None) if the token works against this store's Admin API,
    else (None, reason)."""
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    try:
        resp = http_requests.post(
            url,
            json={"query": "{ shop { name } products(first: 1) { edges { node { id } } } }"},
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            timeout=20,
        )
    except Exception as e:
        return None, f"could not reach {shop}: {type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return None, f"Admin API refused the token: HTTP {resp.status_code} {resp.text[:300]}"
    body, err = _json_or_reason(resp, "Admin API")
    if err:
        return None, err
    if body.get("errors"):
        # Usually a missing scope (read_products) on the app.
        return None, f"Admin API error: {str(body['errors'])[:300]}"
    return ((body.get("data") or {}).get("shop") or {}).get("name") or shop, None


def _client_credentials_token(shop: str, app):
    """(payload, None) or (None, reason). Same grant the token manager uses."""
    try:
        resp = http_requests.post(
            f"https://{shop}/admin/oauth/access_token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     app.client_id,
                "client_secret": app.client_secret,
            },
            timeout=20,
        )
    except Exception as e:
        return None, f"could not reach {shop}: {type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return None, (
            f"client_credentials refused for app {app.client_id}: HTTP {resp.status_code} "
            f"{resp.text[:300]} — Shopify only grants this when the app belongs to the "
            "same organisation as the store and is installed on it. For a published store: "
            "use an app created in that store's own organisation (pass its client_id + "
            "client_secret), or pass an admin token for the store."
        )
    payload, err = _json_or_reason(resp, "token endpoint")
    if err:
        return None, err
    if not payload.get("access_token"):
        return None, f"client_credentials returned no access_token (keys={sorted(payload)})"
    return payload, None


def _tenant_view(tenant, token_row=None) -> dict:
    from store_registry import get_tenant_registry
    registry = get_tenant_registry()
    loader = None
    if registry is not None:
        loader = dict(registry.resident_loaders()).get(str(tenant.tenant_id))
    return {
        "shop":             tenant.shopify_domain,
        "tenant_id":        str(tenant.tenant_id),
        "license_id":       tenant.license_id,
        "db_name":          tenant.db_name,
        "status":           tenant.status,
        "ecommerce_backend": tenant.ecommerce_backend,
        "last_build_error": tenant.last_build_error,
        "token": None if token_row is None else {
            # Only a hand-supplied token is stored with a years-long lifetime.
            "source":     "manual admin token"
                          if token_row.fetched_at and
                          (token_row.expires_at - token_row.fetched_at) > timedelta(days=365)
                          else "renewed automatically",
            "expires_at": token_row.expires_at.isoformat() if token_row.expires_at else None,
            "last_error": token_row.last_error,
        },
        "loader": None if loader is None else {
            "products":   len(loader.products or []),
            "categories": len(loader.categories or []),
            "degraded":   bool(loader._degraded),
            "reasons":    list(loader._degraded_reasons or []),
        },
    }


def _register_app(client_id: str, client_secret: str, shop: str):
    """Make sure `client_id` is usable for `shop`: register it if new (same
    table /admin/shopify-apps writes), or check the secret matches if it is
    already registered. Returns an error string, or None."""
    from sqlalchemy.orm.attributes import flag_modified
    from app_config import SHOPIFY_CLIENT_ID
    from models.shopify_app import ShopifyApp
    from tenant_crypto import decrypt_secret, encrypt_secret

    if SHOPIFY_CLIENT_ID and client_id == SHOPIFY_CLIENT_ID:
        if client_secret != (os.getenv("SHOPIFY_CLIENT_SECRET") or "").strip():
            return "that is the public app's client id, and the secret doesn't match .env's SHOPIFY_CLIENT_SECRET"
        return None

    row = ShopifyApp.query.get(client_id)
    if row is None:
        row = ShopifyApp(
            client_id=client_id,
            client_secret_encrypted=encrypt_secret(client_secret),
            label=f"dev: {shop}",
            shops=[shop],
            installed_shops=[],
        )
        db.session.add(row)
        db.session.commit()
        logger.info(f"dev provision: registered custom app {client_id} for {shop}")
        return None

    try:
        same = hmac.compare_digest(decrypt_secret(row.client_secret_encrypted), client_secret)
    except Exception:
        same = False
    if not same:
        return (f"app {client_id} is already registered with a different secret — "
                "fix it under /admin/shopify-apps, or omit client_secret to use the stored one")
    if row.shops and shop not in row.shops:
        row.shops = list(row.shops) + [shop]
        flag_modified(row, "shops")
        db.session.commit()
    return None


@dev_shopify_bp.route("/tenant", methods=["POST"])
def provision():
    from routes.provisioning import _create_tenant_schema, _derive_db_name, _start_background_build
    from routes.shopify_oauth import _generate_shop_token
    from shopify_apps import app_for_install, record_install
    from tenant_db_provisioner import ensure_tenant_database

    body = request.get_json(silent=True) or {}
    shop = _shop_arg(body.get("shop"))
    admin_token = (body.get("admin_token") or "").strip()
    client_id = (body.get("client_id") or "").strip() or None
    client_secret = (body.get("client_secret") or "").strip()
    force = bool(body.get("force"))

    if not _SHOP_RE.match(shop):
        return jsonify({"success": False, "error": f"invalid shop {shop!r} — expected <store>.myshopify.com"}), 400
    if client_secret and not client_id:
        return jsonify({"success": False, "error": "client_secret needs its client_id"}), 400

    # ── 0. Never clobber a real OAuth install ────────────────────────────────
    # A refresh token here means the store was installed through the OAuth
    # flow against THIS database. Overwriting the row drops that refresh
    # token, and the install dies when its current access token expires
    # (about an hour) — the store would need re-installing. Checked before
    # any token is minted, so a refusal changes nothing anywhere.
    existing_token = db.session.get(ShopifyToken, shop)
    if existing_token is not None and existing_token.refresh_token and not force:
        return jsonify({
            "success": False,
            "step": "guard",
            "error": (
                f"{shop} is installed through the real OAuth flow in this database. "
                "Provisioning over it would discard its refresh token and break that "
                "install once the current access token expires. Use a separate local "
                "database for dev, or pass force to overwrite anyway."
            ),
        }), 409

    if client_id and client_secret:
        err = _register_app(client_id, client_secret, shop)
        if err:
            return jsonify({"success": False, "step": "app", "error": err}), 400

    app = app_for_install(client_id)
    if app is None:
        return jsonify({"success": False, "error": f"unknown custom app client_id={client_id!r}"}), 404
    if not admin_token and not app.configured:
        return jsonify({
            "success": False,
            "error": "no app credentials — set SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET in .env, "
                     "or pass an admin token",
        }), 400

    # ── 1. Token: obtain it and prove it works BEFORE touching any rows ──────
    now = datetime.now(timezone.utc)
    if admin_token:
        access_token, scope, expires_at = admin_token, None, now + _MANUAL_TOKEN_LIFETIME
        token_source = "manual admin token"
    else:
        payload, err = _client_credentials_token(shop, app)
        if err:
            logger.warning(f"dev provision: {err} | shop={shop}")
            return jsonify({"success": False, "step": "token", "error": err}), 502
        access_token = payload["access_token"]
        scope = payload.get("scope")
        expires_at = now + timedelta(seconds=int(payload.get("expires_in") or 86399))
        token_source = "client_credentials"

    shop_name, err = _check_token(shop, access_token)
    if err:
        logger.warning(f"dev provision: token check failed | shop={shop} | {err}")
        return jsonify({"success": False, "step": "token_check", "error": err}), 502

    # ── 2. Tenant row (same shape as shopify_auth_callback) ──────────────────
    tenant = Tenant.query.filter_by(shopify_domain=shop).first()
    reinstall = tenant is not None
    if tenant is None:
        tenant_id = uuid_mod.uuid4()
        tenant = Tenant(
            tenant_id=tenant_id,
            license_id=_generate_shop_token(),
            db_name=_derive_db_name(str(tenant_id)),
            plan="free",
            status="active",
            features={},
            ecommerce_backend="shopify",
            shopify_domain=shop,
            site_domain=shop,
        )
        db.session.add(tenant)
    else:
        if tenant.status == "archived":
            tenant.archived_at = None
        tenant.ecommerce_backend = "shopify"
        tenant.status = "active"
        tenant.build_attempts = 0
        tenant.last_build_error = None
        if not tenant.license_id:
            tenant.license_id = _generate_shop_token()
    db.session.commit()

    # ── 3. Token row — where ShopifyTokenManager reads it from ───────────────
    token_row = db.session.get(ShopifyToken, shop)
    if token_row is None:
        token_row = ShopifyToken(store_domain=shop, refresh_count=0)
        db.session.add(token_row)
    token_row.access_token = access_token
    token_row.scope = scope
    token_row.fetched_at = now
    token_row.expires_at = expires_at
    # A leftover OAuth refresh token would make the manager try the
    # refresh_token grant against a token it didn't come from.
    token_row.refresh_token = None
    token_row.refresh_token_expires_at = None
    token_row.refresh_count = (token_row.refresh_count or 0) + 1
    token_row.last_error = None
    db.session.commit()

    # Bind the store to the app whose credentials renew its token.
    if not admin_token:
        record_install(shop, app)

    # ── 4. Database + schema ─────────────────────────────────────────────────
    base_dsn = current_app.config["SQLALCHEMY_DATABASE_URI"]
    try:
        ensure_tenant_database(base_dsn, tenant.db_name)
        _create_tenant_schema(tenant.db_name, base_dsn)
        tenant.schema_migrated_at = datetime.now(timezone.utc)
    except Exception as e:
        logger.error(f"dev provision: database setup failed | shop={shop} | {e}", exc_info=True)
        tenant.status = "provision_failed"
        tenant.last_build_error = str(e)
        db.session.commit()
        return jsonify({"success": False, "step": "database", "error": str(e)}), 500

    tenant.status = "warming"
    db.session.commit()

    # ── 5. Fresh catalog build (no restart needed on a re-run) ───────────────
    from store_registry import get_tenant_registry
    registry = get_tenant_registry()
    if registry is not None:
        registry.evict(str(tenant.tenant_id))
    _start_background_build(tenant.tenant_id, current_app._get_current_object())

    logger.info(
        f"dev provision: ✅ shop={shop} tenant_id={tenant.tenant_id} reinstall={reinstall} "
        f"token={token_source} — catalog build started"
    )
    return jsonify({
        "success":      True,
        "reinstall":    reinstall,
        "shop_name":    shop_name,
        "token_source": token_source,
        **_tenant_view(tenant, token_row),
    }), 200


@dev_shopify_bp.route("/tenant", methods=["GET"])
def tenant_status():
    shop = _shop_arg(request.args.get("shop"))
    tenant = Tenant.query.filter_by(shopify_domain=shop).first() if shop else None
    if tenant is None:
        return jsonify({"success": False, "error": f"no tenant for shop={shop!r}"}), 404
    return jsonify({"success": True, **_tenant_view(tenant, db.session.get(ShopifyToken, shop))}), 200


@dev_shopify_bp.route("/tenant", methods=["DELETE"])
def remove():
    from routes.deactivation import teardown_tenant

    shop = _shop_arg(request.args.get("shop"))
    tenant = Tenant.query.filter_by(shopify_domain=shop).first() if shop else None
    if tenant is None:
        return jsonify({"success": False, "error": f"no tenant for shop={shop!r}"}), 404
    if tenant.status == "archived":
        return jsonify({"success": True, "already_archived": True}), 200
    result = teardown_tenant(tenant, log_prefix="dev teardown")
    return jsonify(result), 200 if result.get("success") else 500
