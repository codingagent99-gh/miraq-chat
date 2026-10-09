"""
routes/instagram_connect.py — a store owner connects their Instagram account
to their Shopify store, so DMs to that account reach that store.

All this flow does is write the same channel_connections row that the manual
POST /channel-connections writes:

    (channel="instagram", external_account_id=<IG account id>) -> tenant

/chat/channel already routes on that row (routes/channel.py). Nothing about
routing changes here.

FLOW (Facebook Login; the IG account must be a professional account linked
to a Facebook Page)

  /installed?shop=&t=           App page. Shows "Connect Instagram" once the
                                browser has proven it is the store owner
                                (merchant_session.py).
  GET  /instagram/connect       Checks that proof, sends the owner to the
                                Facebook Login dialog. `state` carries the shop,
                                signed.
  GET  /instagram/callback      Code -> user token -> the owner's Pages and
                                their linked IG accounts. One account: connect
                                it. Several: show a pick list.
  POST /instagram/choose        The pick list's submit.
  POST /instagram/disconnect    Removes the store's Instagram connection.

"Connect" means two things, in this order:
  1. Subscribe the Page to this Meta app (POST /{page-id}/subscribed_apps).
     Without it Meta never sends that account's DMs to the webhook at all.
  2. Save the channel_connections row.

The Page token is used for step 1 only and never stored; routing needs only
the IG account id.

ONE ACCOUNT PER STORE
  Connecting a new account replaces the store's previous Instagram row.
  An account already connected to a DIFFERENT store is refused, the same rule
  as POST /channel-connections: moving it is a deliberate disconnect first.

CONFIG (.env)
  META_APP_ID, META_APP_SECRET    required; the section is hidden without them
  META_GRAPH_VERSION              default v23.0
  META_LOGIN_SCOPES               default below; must be approved in App Review
                                  for accounts without a role on the app
  META_LOGIN_CONFIG_ID            optional; for a Facebook Login for Business
                                  configuration, sent instead of scope
  META_PAGE_SUBSCRIBED_FIELDS     default messages,messaging_postbacks
  SHOPIFY_APP_BASE_URL            already set for the Shopify install flow

Meta app dashboard: add <SHOPIFY_APP_BASE_URL>/instagram/callback to
Facebook Login -> Valid OAuth Redirect URIs.
"""

import hashlib
import hmac
import html
import json
import os
import time
import urllib.parse

import requests as http_requests
from cryptography.fernet import InvalidToken
from flask import Blueprint, redirect, request

import merchant_session
from chat_logger import get_logger
from models import db, ChannelConnection, Tenant
from tenant_crypto import decrypt_secret, encrypt_secret

logger = get_logger("miraq_chat")

instagram_connect_bp = Blueprint("instagram_connect", __name__)

META_APP_ID = os.getenv("META_APP_ID", "").strip()
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0").strip()
META_LOGIN_SCOPES = os.getenv(
    "META_LOGIN_SCOPES",
    "pages_show_list,pages_manage_metadata,business_management,"
    "instagram_basic,instagram_manage_messages",
).strip()
META_LOGIN_CONFIG_ID = os.getenv("META_LOGIN_CONFIG_ID", "").strip()
META_PAGE_SUBSCRIBED_FIELDS = os.getenv(
    "META_PAGE_SUBSCRIBED_FIELDS", "messages,messaging_postbacks"
).strip()

_BASE_URL = os.getenv("SHOPIFY_APP_BASE_URL", "").rstrip("/")
_GRAPH = "https://graph.facebook.com"
_CHANNEL = "instagram"
_PICK_MAX_AGE = 10 * 60  # seconds a pick list stays valid

_STYLE = (
    "<style>body{font-family:system-ui,sans-serif;max-width:34rem;margin:4rem auto;"
    "padding:0 1rem;line-height:1.6;color:#1a1a1a}"
    ".btn{display:inline-block;background:#1a1a1a;color:#fff;padding:.6rem 1.1rem;"
    "border-radius:8px;text-decoration:none;border:0;font:inherit;cursor:pointer}"
    ".btn.secondary{background:#fff;color:#1a1a1a;border:1px solid #c9c9c9}"
    "li{margin:.3rem 0}label{display:block;margin:.4rem 0}</style>"
)


# ── helpers ──────────────────────────────────────────────────────────────────

def is_configured() -> bool:
    return bool(META_APP_ID and META_APP_SECRET)


def _url(path: str) -> str:
    base = _BASE_URL or request.url_root.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _redirect_uri() -> str:
    return _url("instagram/callback")


def app_page_url(shop: str) -> str:
    """The app page, with a fresh owner token, so the owner can act again."""
    return _url(
        "installed?" + urllib.parse.urlencode({"shop": shop, "t": merchant_session.issue(shop)})
    )


def _page(title: str, body: str, status: int = 200):
    return (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>{_STYLE}{body}"
    ), status


def _back(shop: str) -> str:
    return f"<p><a class='btn secondary' href='{html.escape(app_page_url(shop))}'>Back to MiraQ</a></p>"


def _tenant_for(shop: str):
    tenant = Tenant.query.filter_by(shopify_domain=shop).first()
    if tenant is None or tenant.status == "archived":
        return None
    return tenant


def _current(tenant):
    return (ChannelConnection.query
            .filter_by(tenant_id=tenant.tenant_id, channel=_CHANNEL)
            .order_by(ChannelConnection.updated_at.desc())
            .first())


def _graph(method: str, path: str, **params):
    """Call the Graph API. Returns (ok, json). Adds appsecret_proof whenever a
    token is sent, so the app may require it ("Require App Secret")."""
    token = params.get("access_token")
    if token:
        params["appsecret_proof"] = hmac.new(
            META_APP_SECRET.encode(), token.encode(), hashlib.sha256
        ).hexdigest()
    url = f"{_GRAPH}/{META_GRAPH_VERSION}/{path.lstrip('/')}"
    try:
        resp = http_requests.request(method, url, params=params, timeout=20)
        data = resp.json() if resp.content else {}
    except Exception as e:
        logger.error(f"instagram connect: graph {method} {path} failed | {e}")
        return False, {}
    if resp.status_code >= 400 or "error" in data:
        err = data.get("error") or {}
        logger.warning(
            f"instagram connect: graph {method} {path} -> HTTP {resp.status_code} | "
            f"code={err.get('code')} subcode={err.get('error_subcode')} msg={err.get('message')!r}"
        )
        return False, data
    return True, data


def _owner_shop_from_request():
    """(shop, None) if the request carries a valid owner token, else (None, error page)."""
    shop = (request.values.get("shop") or "").strip().lower()
    token = request.values.get("t") or ""
    if not shop or not merchant_session.verify(token, shop):
        return None, _page(
            "Session expired",
            "<h1>Please open MiraQ again</h1>"
            "<p>This page has expired. Open <strong>MiraQ</strong> from your Shopify admin "
            "(<strong>Apps</strong>) and try again.</p>",
            403,
        )
    return shop, None


# ── the section shown on /installed ──────────────────────────────────────────

def instagram_section_html(shop: str) -> str:
    """
    The Instagram block of the app page. Call only for a shop whose owner
    token has already been verified.
    """
    if not is_configured():
        return ""
    tenant = _tenant_for(shop)
    if tenant is None:
        return ""

    t = merchant_session.issue(shop)
    connect = _url("instagram/connect?" + urllib.parse.urlencode({"shop": shop, "t": t}))
    row = _current(tenant)

    if row is not None and row.is_active:
        name = html.escape(row.display_name or row.external_account_id)
        return (
            "<h2>Instagram</h2>"
            f"<p>✅ Connected as <strong>{name}</strong>. Customers who message this "
            "account get answers from MiraQ.</p>"
            f"<form method='post' action='{html.escape(_url('instagram/disconnect'))}'>"
            f"<input type='hidden' name='shop' value='{html.escape(shop)}'>"
            f"<input type='hidden' name='t' value='{html.escape(t)}'>"
            f"<a class='btn secondary' href='{html.escape(connect)}'>Connect a different account</a> "
            "<button class='btn secondary' type='submit'>Disconnect</button>"
            "</form>"
        )

    return (
        "<h2>Instagram</h2>"
        "<p>Let MiraQ answer customers who message your store on Instagram.</p>"
        "<p>You'll need an Instagram <strong>Business</strong> or <strong>Creator</strong> "
        "account linked to a Facebook Page you manage.</p>"
        f"<p><a class='btn' href='{html.escape(connect)}'>Connect Instagram</a></p>"
    )


# ── GET /instagram/connect ───────────────────────────────────────────────────

@instagram_connect_bp.route("/instagram/connect", methods=["GET"])
def instagram_connect():
    shop, error = _owner_shop_from_request()
    if error:
        return error
    if not is_configured():
        logger.error("instagram connect: META_APP_ID / META_APP_SECRET not set")
        return _page("Not available", "<h1>Instagram isn't available yet</h1>" + _back(shop), 503)
    if _tenant_for(shop) is None:
        return _page("Store not found", "<h1>Store not found</h1><p>Reinstall MiraQ and try again.</p>", 404)

    state = f"{merchant_session.issue(shop, merchant_session.IG_STATE)}:{shop}"
    params = {
        "client_id": META_APP_ID,
        "redirect_uri": _redirect_uri(),
        "state": state,
        "response_type": "code",
    }
    if META_LOGIN_CONFIG_ID:
        params["config_id"] = META_LOGIN_CONFIG_ID
    else:
        params["scope"] = META_LOGIN_SCOPES

    logger.info(f"instagram connect: redirecting to Facebook Login | shop={shop}")
    return redirect(
        f"https://www.facebook.com/{META_GRAPH_VERSION}/dialog/oauth?" + urllib.parse.urlencode(params),
        code=302,
    )


# ── GET /instagram/callback ──────────────────────────────────────────────────

@instagram_connect_bp.route("/instagram/callback", methods=["GET"])
def instagram_callback():
    token, _, shop = (request.args.get("state") or "").partition(":")
    shop = shop.strip().lower()
    if not shop or not merchant_session.verify(token, shop, merchant_session.IG_STATE):
        logger.warning(f"instagram callback: state invalid or expired | shop={shop!r}")
        return _page(
            "Session expired",
            "<h1>That took too long</h1><p>Open <strong>MiraQ</strong> from your Shopify "
            "admin and click <strong>Connect Instagram</strong> again.</p>",
            400,
        )

    tenant = _tenant_for(shop)
    if tenant is None:
        return _page("Store not found", "<h1>Store not found</h1>", 404)

    if request.args.get("error"):
        # The owner cancelled, or declined a permission.
        logger.info(
            f"instagram callback: login not completed | shop={shop} | "
            f"error={request.args.get('error')} reason={request.args.get('error_reason')}"
        )
        return _page(
            "Instagram not connected",
            "<h1>Instagram wasn't connected</h1>"
            "<p>The Facebook login was cancelled. You can try again any time.</p>" + _back(shop),
        )

    code = (request.args.get("code") or "").strip()
    if not code:
        return _page("Instagram not connected", "<h1>Something went wrong</h1>" + _back(shop), 400)

    ok, data = _graph("GET", "oauth/access_token",
                      client_id=META_APP_ID, client_secret=META_APP_SECRET,
                      redirect_uri=_redirect_uri(), code=code)
    user_token = data.get("access_token") if ok else None
    if not user_token:
        return _page("Instagram not connected",
                     "<h1>Facebook login failed</h1><p>Please try again.</p>" + _back(shop), 502)

    ok, data = _graph("GET", "me/accounts",
                      fields="id,name,access_token,instagram_business_account{id,username}",
                      limit=100, access_token=user_token)
    if not ok:
        return _page("Instagram not connected",
                     "<h1>Couldn't read your Facebook Pages</h1><p>Please try again.</p>" + _back(shop), 502)

    pages = [
        {
            "page_id": p["id"],
            "page_name": p.get("name") or "",
            "page_token": p.get("access_token") or "",
            "ig_id": p["instagram_business_account"]["id"],
            "username": p["instagram_business_account"].get("username") or "",
        }
        for p in data.get("data", [])
        if p.get("instagram_business_account", {}).get("id") and p.get("access_token")
    ]
    logger.info(f"instagram callback: {len(pages)} Page(s) with an Instagram account | shop={shop}")

    if not pages:
        return _page(
            "No Instagram account found",
            "<h1>No Instagram account found</h1>"
            "<p>We couldn't find an Instagram account we can connect. Check that:</p><ul>"
            "<li>your Instagram account is a <strong>Business</strong> or <strong>Creator</strong> account,</li>"
            "<li>it is linked to a Facebook Page you manage, and</li>"
            "<li>you selected that Page and that Instagram account when Facebook asked.</li></ul>"
            + _back(shop),
        )

    if len(pages) == 1:
        return _connect_and_render(tenant, shop, pages[0])

    # Several: let the owner pick. The candidates (Page tokens included) go
    # into the form encrypted, so nothing is kept server-side and the form
    # cannot be edited to name an account the owner did not just grant.
    blob = encrypt_secret(json.dumps({"shop": shop, "ts": int(time.time()), "pages": pages}))
    options = "".join(
        f"<label><input type='radio' name='ig_id' value='{html.escape(p['ig_id'])}'"
        f"{' checked' if i == 0 else ''}> "
        f"<strong>@{html.escape(p['username'] or p['ig_id'])}</strong> "
        f"(Page: {html.escape(p['page_name'])})</label>"
        for i, p in enumerate(pages)
    )
    return _page(
        "Choose an Instagram account",
        "<h1>Which Instagram account?</h1>"
        "<p>Pick the account your customers message for this store.</p>"
        f"<form method='post' action='{html.escape(_url('instagram/choose'))}'>"
        f"<input type='hidden' name='blob' value='{html.escape(blob)}'>{options}"
        "<p><button class='btn' type='submit'>Connect</button></p></form>",
    )


# ── POST /instagram/choose ───────────────────────────────────────────────────

@instagram_connect_bp.route("/instagram/choose", methods=["POST"])
def instagram_choose():
    try:
        payload = json.loads(decrypt_secret(request.form.get("blob") or ""))
    except (InvalidToken, ValueError, TypeError):
        logger.warning("instagram choose: pick list could not be decrypted")
        payload = None

    if not payload or time.time() - int(payload.get("ts", 0)) > _PICK_MAX_AGE:
        return _page(
            "Session expired",
            "<h1>That took too long</h1><p>Open <strong>MiraQ</strong> from your Shopify "
            "admin and click <strong>Connect Instagram</strong> again.</p>",
            400,
        )

    shop = payload["shop"]
    tenant = _tenant_for(shop)
    if tenant is None:
        return _page("Store not found", "<h1>Store not found</h1>", 404)

    ig_id = request.form.get("ig_id") or ""
    page = next((p for p in payload["pages"] if p["ig_id"] == ig_id), None)
    if page is None:
        return _page("Instagram not connected", "<h1>Please choose an account</h1>" + _back(shop), 400)

    return _connect_and_render(tenant, shop, page)


# ── POST /instagram/disconnect ───────────────────────────────────────────────

@instagram_connect_bp.route("/instagram/disconnect", methods=["POST"])
def instagram_disconnect():
    shop, error = _owner_shop_from_request()
    if error:
        return error
    tenant = _tenant_for(shop)
    if tenant is None:
        return _page("Store not found", "<h1>Store not found</h1>", 404)

    removed = (ChannelConnection.query
               .filter_by(tenant_id=tenant.tenant_id, channel=_CHANNEL)
               .delete(synchronize_session=False))
    db.session.commit()
    logger.info(f"instagram disconnect: removed {removed} connection(s) | shop={shop} tenant={tenant.tenant_id}")
    return redirect(app_page_url(shop), code=303)


# ── connect ──────────────────────────────────────────────────────────────────

def _connect_and_render(tenant, shop: str, page: dict):
    ig_id = page["ig_id"]
    label = f"@{page['username']}" if page["username"] else ig_id

    # Same rule as POST /channel-connections: never take an account from
    # another store silently.
    existing = ChannelConnection.query.filter_by(channel=_CHANNEL, external_account_id=ig_id).first()
    if existing is not None and existing.tenant_id != tenant.tenant_id:
        logger.warning(
            f"instagram connect: {ig_id} already connected to tenant={existing.tenant_id} | "
            f"refused for shop={shop}"
        )
        return _page(
            "Already connected",
            f"<h1>{html.escape(label)} is connected to another store</h1>"
            "<p>Disconnect it from that store's MiraQ first, then try again.</p>" + _back(shop),
            409,
        )

    # 1. Subscribe the Page, so Meta sends this account's DMs to the webhook.
    ok, data = _graph("POST", f"{page['page_id']}/subscribed_apps",
                      subscribed_fields=META_PAGE_SUBSCRIBED_FIELDS,
                      access_token=page["page_token"])
    if not ok or not data.get("success"):
        return _page(
            "Instagram not connected",
            f"<h1>Couldn't connect {html.escape(label)}</h1>"
            "<p>Facebook didn't allow MiraQ to receive this account's messages. Try again, "
            "and accept all the permissions Facebook asks for.</p>" + _back(shop),
            502,
        )

    # 2. Save the routing row. One Instagram account per store: drop any other.
    (ChannelConnection.query
     .filter(ChannelConnection.tenant_id == tenant.tenant_id,
             ChannelConnection.channel == _CHANNEL,
             ChannelConnection.external_account_id != ig_id)
     .delete(synchronize_session=False))
    row = existing or ChannelConnection(tenant_id=tenant.tenant_id, channel=_CHANNEL, external_account_id=ig_id)
    if existing is None:
        db.session.add(row)
    row.status = "active"
    row.display_name = label
    db.session.commit()

    enabled = (tenant.features or {}).get("channels")
    if isinstance(enabled, list) and _CHANNEL not in enabled:
        logger.warning(f"instagram connect: tenant={tenant.tenant_id} has channels={enabled}; "
                       "/chat/channel will refuse Instagram until it is enabled")

    logger.info(
        f"instagram connect: ✅ connected | shop={shop} tenant={tenant.tenant_id} | "
        f"ig={ig_id} ({label}) page={page['page_id']}"
    )
    return _page(
        "Instagram connected",
        f"<h1>✅ Instagram connected</h1><p><strong>{html.escape(label)}</strong> is now "
        "connected to your store. Customers who message it will get answers from MiraQ.</p>"
        + _back(shop),
    )
