"""
routes/shopify_oauth.py — Shopify app install (authorization code grant).

This is the Shopify equivalent of /activate-free: it is what CREATES a Shopify
tenant. Until it existed nothing in the backend ever wrote Tenant.shopify_domain,
so a merchant could install the app and still have no tenant row, and every
storefront request would 400.

  GET /shopify/install?shop=<store>.myshopify.com
      Entry point. Shopify opens the app's application_url with ?shop= when a
      merchant installs from the App Store or the Partner dashboard; that URL
      should point here (or redirect here). Sends the merchant to their own
      store's OAuth consent screen.

  GET /shopify/auth/callback?code=...&shop=...&hmac=...&state=...
      Declared in shopify.app.*.toml as the redirect_url. Verifies the query
      HMAC and our own state nonce, exchanges the code for an offline Admin API
      token, creates (or re-activates) the tenant + its database, stores the
      token, and kicks off the catalog build.

  GET /installed
      Plain landing page, so application_url resolves to something after the
      install instead of a 404.

WHY THE CODE GRANT, NOT client_credentials
------------------------------------------
store_loader/shopify_token_manager.py mints tokens with the client_credentials
grant. That grant only works for stores inside your own Partner organisation,
so it cannot serve a distributable app — which is exactly what the app's toml
comment says. The token minted here is an EXPIRING offline token (expiring=1):
Shopify no longer accepts non-expiring ones on the Admin API. It comes with a
rotating refresh token, which the token manager uses to renew it.

SECURITY
--------
Three checks, all required:
  * shop must match the myshopify.com pattern — this value ends up in a URL we
    POST credentials to, so an unvalidated one is an open redirect at best.
  * hmac over the query string, keyed with the app secret. Proves Shopify sent
    the callback.
  * state, a timestamp signed with the same secret. Proves the callback belongs
    to an install WE started, and expires after 10 minutes. Stateless on
    purpose: no session store, and a multi-worker deployment cannot lose it.
"""

import base64
import hashlib
import hmac
import html
import os
import re
import time
import urllib.parse
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone

import requests as http_requests
from flask import Blueprint, current_app, jsonify, redirect, request

from app_config import (
    SHOPIFY_API_VERSION,
    SHOPIFY_CLIENT_ID,
    SHOPIFY_CLIENT_SECRET,
)
from chat_logger import get_logger
from models import db, Tenant
from models.shopify_token import ShopifyToken
from routes.provisioning import (
    _create_tenant_schema,
    _derive_db_name,
    _start_background_build,
)
from tenant_db_provisioner import ensure_tenant_database, TenantDBProvisionError

logger = get_logger("miraq_chat")

shopify_oauth_bp = Blueprint("shopify_oauth", __name__)

# Scopes must match shopify.app.*.toml. Shopify compares the granted scope set
# against what the app declares; a mismatch re-prompts the merchant on every
# request.
SHOPIFY_SCOPES = "read_customers,read_orders,read_products,write_draft_orders"

# Public base URL of THIS backend, exactly as Shopify sees it — e.g.
#   SHOPIFY_APP_BASE_URL=https://silfratech.in/chatbot-shopify-multi/api
#
# Every URL this module hands to Shopify or to the merchant's browser is built
# from it. It cannot be derived from the request: behind the reverse proxy the
# app sees neither the /chatbot-shopify-multi/api prefix nor the https scheme
# (there is no ProxyFix), so request.url_root comes out as something like
# http://127.0.0.1:5009/. That broke two things:
#   * redirect_uri — Shopify requires it to match an entry in the toml's
#     redirect_urls EXACTLY, so every install failed at the consent screen
#     with "redirect_uri is not whitelisted".
#   * the post-install and retry redirects, which were root-relative
#     ("/installed") and so sent the merchant to https://silfratech.in/installed,
#     outside the app entirely.
# Falls back to url_root only so local testing without the variable still
# works; a deployed backend must set it.
SHOPIFY_APP_BASE_URL = os.getenv("SHOPIFY_APP_BASE_URL", "").rstrip("/")

# Handle of the storefront app embed, used for the theme editor deep link:
# the block's file name in extensions/miraq-commerce-agent/blocks/.
_EMBED_BLOCK_HANDLE = "miraq_widget"


def _app_url(path: str) -> str:
    """Absolute URL for one of this module's routes."""
    base = SHOPIFY_APP_BASE_URL or request.url_root.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


_STATE_MAX_AGE_SECONDS = 600
_SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")



def _valid_shop(shop: str) -> bool:
    return bool(_SHOP_RE.match(shop or ""))


def _verify_oauth_hmac(args) -> bool:
    """
    Verify the `hmac` parameter on an OAuth request.

    NOT the same scheme as the App Proxy signature (ecommerce/shopify_proxy.py):
    there the pairs are concatenated with no separator and the parameter is
    called `signature`; here they are joined with '&' and it is called `hmac`.
    Mixing them up produces a mismatch on every request.
    """
    if not SHOPIFY_CLIENT_SECRET:
        logger.error("shopify oauth: SHOPIFY_CLIENT_SECRET is not configured")
        return False

    supplied = args.get("hmac", "")
    if not supplied:
        return False

    pairs = sorted(
        f"{k}={v}"
        for k, v in args.items(multi=False)
        if k not in ("hmac", "signature")
    )
    expected = hmac.new(
        SHOPIFY_CLIENT_SECRET.encode("utf-8"),
        "&".join(pairs).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, supplied)


def _make_state(shop: str) -> str:
    """Signed, stateless nonce: <timestamp>.<hmac(timestamp:shop)>."""
    ts = str(int(time.time()))
    digest = hmac.new(
        SHOPIFY_CLIENT_SECRET.encode("utf-8"),
        f"{ts}:{shop}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{ts}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"


def _valid_state(state: str, shop: str) -> bool:
    try:
        ts, _ = (state or "").split(".", 1)
        age = time.time() - int(ts)
    except (ValueError, AttributeError, TypeError):
        return False
    if age > _STATE_MAX_AGE_SECONDS or age < -60:
        return False
    return hmac.compare_digest(_make_state_for_ts(ts, shop), state)


def _make_state_for_ts(ts: str, shop: str) -> str:
    digest = hmac.new(
        SHOPIFY_CLIENT_SECRET.encode("utf-8"),
        f"{ts}:{shop}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{ts}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"


def _generate_shop_token() -> str:
    """
    A licence id for a Shopify tenant.

    Storefront requests identify themselves with a signed App Proxy `shop`
    parameter rather than this value (see store_registry), so it is not on the
    hot path — but Tenant.license_id is the handle every admin tool, log line
    and diagnostic endpoint uses, and a NULL one would make those useless.
    """
    import secrets
    return "mq_shop_" + secrets.token_urlsafe(24)


@shopify_oauth_bp.route("/shopify/install", methods=["GET"])
def shopify_install():
    """Start the OAuth flow. Point the app's application_url here."""
    shop = (request.args.get("shop") or "").strip().lower()

    if not _valid_shop(shop):
        logger.warning(f"shopify install: invalid shop parameter | shop={shop!r}")
        return jsonify({"error": "invalid shop parameter"}), 400

    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        logger.error("shopify install: app credentials not configured")
        return jsonify({"error": "app not configured"}), 500

    # Shopify signs this request too, but only once the app is known to the
    # store. A first-time install arrives unsigned, so a missing hmac is not
    # an error here — the callback's hmac + state are what actually gate the
    # token exchange.
    if request.args.get("hmac") and not _verify_oauth_hmac(request.args):
        logger.warning(f"shopify install: hmac present but invalid | shop={shop!r}")
        return jsonify({"error": "invalid signature"}), 401

    if not SHOPIFY_APP_BASE_URL:
        logger.warning(
            "shopify install: SHOPIFY_APP_BASE_URL is not set — deriving redirect_uri "
            f"from the request ({request.url_root!r}). Shopify will reject it unless it "
            "matches redirect_urls in the app toml exactly."
        )
    redirect_uri = _app_url("shopify/auth/callback")
    authorize_url = (
        f"https://{shop}/admin/oauth/authorize?"
        + urllib.parse.urlencode({
            "client_id":    SHOPIFY_CLIENT_ID,
            "scope":        SHOPIFY_SCOPES,
            "redirect_uri": redirect_uri,
            "state":        _make_state(shop),
        })
    )
    logger.info(f"shopify install: redirecting to consent screen | shop={shop} redirect_uri={redirect_uri}")
    return redirect(authorize_url, code=302)


@shopify_oauth_bp.route("/shopify/auth/callback", methods=["GET"])
def shopify_auth_callback():
    shop  = (request.args.get("shop") or "").strip().lower()
    code  = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()

    logger.info(f"shopify callback: received | shop={shop!r} code={'present' if code else 'MISSING'}")

    if not _valid_shop(shop):
        logger.warning(f"shopify callback: invalid shop | shop={shop!r}")
        return jsonify({"error": "invalid shop parameter"}), 400
    if not code:
        return jsonify({"error": "missing code"}), 400
    if not _verify_oauth_hmac(request.args):
        logger.warning(f"shopify callback: hmac verification failed | shop={shop}")
        return jsonify({"error": "invalid signature"}), 401
    if not _valid_state(state, shop):
        # Either a replayed/forged callback, or a merchant who left the consent
        # screen open for more than 10 minutes. Both get sent back to the start.
        logger.warning(f"shopify callback: state invalid or expired | shop={shop}")
        return redirect(_app_url(f"shopify/install?shop={urllib.parse.quote(shop)}"), code=302)

    # ── Exchange the code for an EXPIRING offline Admin API token ────────────
    # expiring=1 is required. Without it Shopify issues a non-expiring offline
    # token, which the Admin API now rejects outright with 403 "Non-expiring
    # access tokens are no longer accepted" — the install appears to succeed
    # and every catalog fetch after it fails.
    try:
        resp = http_requests.post(
            f"https://{shop}/admin/oauth/access_token",
            data={
                "client_id":     SHOPIFY_CLIENT_ID,
                "client_secret": SHOPIFY_CLIENT_SECRET,
                "code":          code,
                "expiring":      "1",
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            logger.error(
                f"shopify callback: token exchange refused | shop={shop} "
                f"HTTP {resp.status_code} | body={resp.text[:500]!r}"
            )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.error(f"shopify callback: token exchange failed | shop={shop} | {e}", exc_info=True)
        return jsonify({"error": "token exchange failed"}), 502

    access_token = payload.get("access_token")
    granted_scope = payload.get("scope", "")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    refresh_expires_in = payload.get("refresh_token_expires_in")

    if not refresh_token or not expires_in:
        # Shopify returned a non-expiring token after all. It will be refused
        # by the Admin API, so fail the install loudly here rather than let
        # every catalog fetch 403 afterwards with no hint why.
        logger.error(
            f"shopify callback: expected an expiring token (refresh_token + "
            f"expires_in) but got keys={sorted(payload)} | shop={shop}"
        )
        return jsonify({"error": "shopify did not issue an expiring token"}), 502
    if not access_token:
        logger.error(f"shopify callback: no access_token in response | shop={shop}")
        return jsonify({"error": "token exchange returned no token"}), 502

    logger.info(f"shopify callback: token obtained | shop={shop} scope={granted_scope!r}")

    # ── Tenant row: one per shop domain ──────────────────────────────────────
    # Keyed on shopify_domain, not on a UUID the client supplies: the shop
    # domain is the only stable identifier Shopify gives us, and it is what
    # every webhook and proxied request arrives with. A re-install of the same
    # store must reuse its row and database, history included.
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
        logger.info(f"shopify callback: new tenant | shop={shop} tenant_id={tenant_id} db={tenant.db_name}")
    else:
        logger.info(
            f"shopify callback: re-install | shop={shop} tenant_id={tenant.tenant_id} "
            f"current_status={tenant.status}"
        )
        if tenant.status == "archived":
            # Uninstall archives but keeps the database for the grace period,
            # so a re-install inside that window comes back with its history.
            tenant.archived_at = None
        tenant.ecommerce_backend = "shopify"
        tenant.status = "active"
        tenant.build_attempts = 0
        if not tenant.license_id:
            tenant.license_id = _generate_shop_token()

    db.session.commit()

    # ── Store the token (control-plane table, keyed by domain) ───────────────
    now = datetime.now(timezone.utc)
    token_row = db.session.get(ShopifyToken, shop)
    if token_row is None:
        token_row = ShopifyToken(store_domain=shop, refresh_count=0)
        db.session.add(token_row)
    token_row.access_token  = access_token
    token_row.scope         = granted_scope
    token_row.fetched_at    = now
    token_row.expires_at    = now + timedelta(seconds=int(expires_in))
    token_row.refresh_token = refresh_token
    token_row.refresh_token_expires_at = (
        now + timedelta(seconds=int(refresh_expires_in)) if refresh_expires_in else None
    )
    token_row.refresh_count = (token_row.refresh_count or 0) + 1
    token_row.last_error    = None
    db.session.commit()
    logger.info(
        f"shopify callback: token stored | shop={shop} "
        f"access_expires_in={int(expires_in)}s refresh_expires_in={refresh_expires_in}"
    )

    # ── Database + schema ────────────────────────────────────────────────────
    base_dsn = current_app.config["SQLALCHEMY_DATABASE_URI"]
    try:
        ensure_tenant_database(base_dsn, tenant.db_name)
        _create_tenant_schema(tenant.db_name, base_dsn)
        tenant.schema_migrated_at = datetime.now(timezone.utc)
    except (TenantDBProvisionError, Exception) as e:
        logger.error(f"shopify callback: database setup failed | shop={shop} | {e}", exc_info=True)
        tenant.status = "provision_failed"
        tenant.last_build_error = str(e)
        db.session.commit()
        return jsonify({"error": f"database setup failed: {e}"}), 500

    tenant.status = "warming"
    db.session.commit()

    # Evict any loader already cached for this tenant. On a re-install the
    # registry still holds the loader built with the PREVIOUS token — degraded,
    # if that token stopped working — and the build thread's get_loader() is a
    # cache hit that returns it untouched: no fetch, no use of the token just
    # stored, straight to provision_failed. Evicting forces a real rebuild.
    if reinstall:
        from store_registry import get_tenant_registry
        registry = get_tenant_registry()
        if registry is not None:
            registry.evict(str(tenant.tenant_id))
            logger.info(f"shopify callback: evicted cached loader | tenant_id={tenant.tenant_id}")

    logger.info(f"shopify callback: starting catalog build | shop={shop} tenant_id={tenant.tenant_id}")
    _start_background_build(tenant.tenant_id, current_app._get_current_object())

    logger.info(
        f"shopify callback: ✅ install complete | shop={shop} tenant_id={tenant.tenant_id} "
        f"reinstall={reinstall} api_version={SHOPIFY_API_VERSION}"
    )
    return redirect(_app_url(f"installed?shop={urllib.parse.quote(shop)}"), code=302)


@shopify_oauth_bp.route("/installed", methods=["GET"])
def installed():
    """
    Landing page after install. Exists so application_url resolves to
    something: this app is not embedded, so there is no admin UI to show.
    The catalog build is still running when the merchant lands here.
    """
    shop = (request.args.get("shop") or "").strip().lower()
    # App Store 5.1.3: detailed embed setup, ideally a deep link. This one
    # opens the live theme's editor with the MiraQ app embed ALREADY switched
    # on (context=apps&activateAppId=<client_id>/<embed block handle>); the
    # merchant only reviews and clicks Save. The handle is the block's file
    # name: extensions/miraq-commerce-agent/blocks/miraq_widget.liquid.
    embed_link = (
        f"https://{shop}/admin/themes/current/editor?context=apps"
        f"&activateAppId={SHOPIFY_CLIENT_ID}/{_EMBED_BLOCK_HANDLE}"
        if _valid_shop(shop) and SHOPIFY_CLIENT_ID else ""
    )
    button = (
        f"<p><a class='btn' href='{html.escape(embed_link)}' target='_top'>"
        "Turn on the MiraQ widget</a></p>" if embed_link else ""
    )

    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>MiraQ Commerce Agent</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:34rem;margin:4rem auto;"
        "padding:0 1rem;line-height:1.6;color:#1a1a1a}"
        ".btn{display:inline-block;background:#1a1a1a;color:#fff;padding:.6rem 1.1rem;"
        "border-radius:8px;text-decoration:none}li{margin:.3rem 0}</style>"
        "<h1>MiraQ is installed</h1>"
        "<p>Your catalog is being indexed now. This usually takes a few minutes for "
        "a small store, longer for a large one.</p>"
        "<h2>Finish setup: turn on the chat widget</h2>"
        "<ol>"
        "<li>Click <strong>Turn on the MiraQ widget</strong> below. Your theme editor "
        "opens with the MiraQ app embed already switched on.</li>"
        "<li>Check the widget in the preview, then click <strong>Save</strong>.</li>"
        "<li>Open your store and look for the chat button in the corner.</li>"
        "</ol>"
        + button +
        "<p>To turn it off later: theme editor → <strong>App embeds</strong> → "
        "switch off <strong>Miraq Shopper Agent</strong> → Save.</p>"
    ), 200