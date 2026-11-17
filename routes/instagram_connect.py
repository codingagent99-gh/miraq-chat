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

EMBEDDED ADMIN (public app)
  The pages above are for the custom-distribution apps, which are not
  embedded. Shopify rejects an embedded app that sends merchants to a
  standalone version of itself, so for the public app only the Facebook step
  leaves the admin:

  1. The admin page opens /instagram/connect?...&attempt=<id> in a pop-up.
     That starts a connect attempt for the shop (instagram_connect_attempts).
  2. The callback writes the outcome onto that attempt instead of rendering
     it, and shows only "you can close this window".
  3. The admin page reads the outcome (GET /shopify/admin/instagram/attempt)
     and shows it; with several accounts the merchant chooses there
     (POST /shopify/admin/instagram/choose). See attempt_view() and
     choose_for_attempt() below, and models/instagram_connect_attempt.py.

"Connect" means two things, in this order:
  1. Subscribe the Page to this Meta app (POST /{page-id}/subscribed_apps).
     Without it Meta never sends that account's DMs to the webhook at all.
  2. Save the channel_connections row, with the Page token (encrypted).

PAGE TOKEN
  The login code gives a short-lived user token; it is exchanged for a
  long-lived one first, so the Page tokens read with it don't expire. The
  webhook service fetches the saved token per account to send replies
  (GET /channel-connections/token). Disconnecting, replacing the account and
  shop redact unsubscribe the Page with its token (unsubscribe(), best
  effort) before the row is deleted, so Meta stops sending its messages.

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
import re
import time
import urllib.parse
from datetime import datetime, timezone

import requests as http_requests
from cryptography.fernet import InvalidToken
from flask import Blueprint, redirect, request

import merchant_session
from chat_logger import get_logger
from models import db, ChannelConnection, InstagramConnectAttempt, Tenant
from shopify_apps import admin_app_url, app_for_shop, is_embedded
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
_ATTEMPT_MAX_AGE = 10 * 60  # seconds an admin connect attempt stays open
_ATTEMPT_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

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
    """
    Where "Back to MiraQ" goes. Embedded app: its page inside the Shopify
    admin. Custom app: /installed, with a fresh owner token so the owner can
    act again.
    """
    app = app_for_shop(shop)
    if is_embedded(app):
        return admin_app_url(shop, app)
    return _url(
        "installed?" + urllib.parse.urlencode({"shop": shop, "t": merchant_session.issue(shop)})
    )


def _page(title: str, body: str, status: int = 200):
    return (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>{_STYLE}{body}"
    ), status


def _back(shop: str) -> str:
    link = f"<a class='btn secondary' href='{html.escape(app_page_url(shop))}'>Back to MiraQ</a>"
    if is_embedded(app_for_shop(shop)):
        # Opened in a new tab from the embedded page, which refreshes itself
        # when the merchant returns to it.
        return f"<p>You can close this tab and go back to MiraQ in your Shopify admin.</p><p>{link}</p>"
    return f"<p>{link}</p>"


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


# ── connect attempts (embedded admin) ────────────────────────────────────────

def attempt_param(value) -> str:
    """The attempt id if it is well-formed, else ""."""
    value = str(value or "").strip()
    return value if _ATTEMPT_RE.match(value) else ""


def _age_seconds(row) -> float:
    created = row.created_at
    if created is None:
        return float("inf")
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


def _start_attempt(shop: str, attempt: str) -> None:
    """Make `attempt` the shop's current one, dropping any earlier outcome."""
    row = db.session.get(InstagramConnectAttempt, shop)
    if row is None:
        row = InstagramConnectAttempt(shop=shop)
        db.session.add(row)
    row.attempt_id = attempt
    row.status = "waiting"
    row.account_label = None
    row.candidates_encrypted = None
    row.created_at = datetime.now(timezone.utc)
    db.session.commit()


def _record(shop: str, attempt: str, status: str, label=None, pages=None) -> None:
    """Write an outcome onto the shop's attempt — only if it is still the current one."""
    row = db.session.get(InstagramConnectAttempt, shop)
    if row is None or row.attempt_id != attempt:
        logger.info(f"instagram connect: outcome for a superseded attempt dropped | shop={shop} status={status}")
        return
    row.status = status
    row.account_label = label
    row.candidates_encrypted = encrypt_secret(json.dumps(pages)) if pages else None
    db.session.commit()
    logger.info(f"instagram connect: attempt outcome | shop={shop} status={status}")


def _closing_page(status: str):
    """What the pop-up shows once its outcome is recorded; it closes itself where the browser allows."""
    if status == "connected":
        heading, text = "Instagram connected", "You can close this window."
    else:
        heading, text = "Back to MiraQ", "Close this window and finish in MiraQ in your Shopify admin."
    return _page(
        heading,
        f"<h1>{html.escape(heading)}</h1><p>{html.escape(text)}</p>"
        "<script>setTimeout(function(){window.close();},1200);</script>",
    )


def _outcome(shop: str, attempt: str, status: str, custom_page, label=None, pages=None):
    """
    Embedded admin attempt: record the outcome and show the closing page.
    Custom app (no attempt): show `custom_page`, as before.
    """
    if attempt:
        _record(shop, attempt, status, label=label, pages=pages)
        return _closing_page(status)
    return custom_page


def _attempt_pages(row):
    try:
        return json.loads(decrypt_secret(row.candidates_encrypted or ""))
    except (InvalidToken, ValueError, TypeError):
        logger.warning(f"instagram connect: attempt account list could not be decrypted | shop={row.shop}")
        return None


def _label(page: dict) -> str:
    return f"@{page['username']}" if page.get("username") else page["ig_id"]


def attempt_view(shop: str, attempt: str) -> dict:
    """
    What GET /shopify/admin/instagram/attempt answers. Never includes tokens:
    a "choose" outcome lists account labels and ids only.
    """
    row = db.session.get(InstagramConnectAttempt, shop)
    if row is None or row.attempt_id != attempt:
        return {"status": "waiting"}  # the pop-up hasn't reached /instagram/connect yet
    if row.status in ("waiting", "choose") and _age_seconds(row) > _ATTEMPT_MAX_AGE:
        if row.candidates_encrypted:
            row.candidates_encrypted = None  # don't keep unused Page tokens around
            db.session.commit()
        return {"status": "expired"}
    view = {"status": row.status, "account": row.account_label}
    if row.status == "choose":
        pages = _attempt_pages(row)
        if not pages:
            return {"status": "error", "account": None}
        view["accounts"] = [
            {"id": p["ig_id"], "label": _label(p), "page": p.get("page_name") or ""}
            for p in pages
        ]
    return view


def choose_for_attempt(shop: str, attempt: str, ig_id: str) -> dict:
    """
    POST /shopify/admin/instagram/choose: connect the account the merchant
    chose from a "choose" outcome. Only accounts the owner just granted can be
    chosen — the list comes from the attempt, never from the request.
    """
    row = db.session.get(InstagramConnectAttempt, shop)
    if row is None or row.attempt_id != attempt or row.status != "choose":
        return {"status": "expired", "account": None}
    if _age_seconds(row) > _ATTEMPT_MAX_AGE:
        row.candidates_encrypted = None
        db.session.commit()
        return {"status": "expired", "account": None}

    tenant = _tenant_for(shop)
    pages = _attempt_pages(row)
    page = next((p for p in pages or [] if p["ig_id"] == ig_id), None)
    if tenant is None or page is None:
        return {"status": "error", "account": None}

    status, label = _connect(tenant, shop, page)
    row.status = status
    row.account_label = label
    row.candidates_encrypted = None
    db.session.commit()
    return {"status": status, "account": label}


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
        status = (
            f"<p>⚠️ <strong>{name}</strong> is connected, but MiraQ can't reply there right now. "
            "Connect it again to fix this.</p>"
            if row.needs_reconnect else
            f"<p>✅ Connected as <strong>{name}</strong>. Customers who message this "
            "account get answers from MiraQ.</p>"
        )
        return (
            "<h2>Instagram</h2>"
            + status +
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
        "account linked to a Facebook Page you manage, with <strong>Allow access to "
        "messages</strong> switched on in Instagram (Settings → Messages and story replies → "
        "Message controls → Connected tools).</p>"
        f"<p><a class='btn' href='{html.escape(connect)}'>Connect Instagram</a></p>"
    )


# ── GET /instagram/connect ───────────────────────────────────────────────────

@instagram_connect_bp.route("/instagram/connect", methods=["GET"])
def instagram_connect():
    shop, error = _owner_shop_from_request()
    if error:
        return error
    # Opened from the embedded admin page: the outcome is shown there, not
    # here (EMBEDDED ADMIN above). Custom apps never send an attempt.
    attempt = attempt_param(request.args.get("attempt")) if is_embedded(app_for_shop(shop)) else ""
    if attempt:
        _start_attempt(shop, attempt)
    if not is_configured():
        logger.error("instagram connect: META_APP_ID / META_APP_SECRET not set")
        return _outcome(shop, attempt, "error",
                        _page("Not available", "<h1>Instagram isn't available yet</h1>" + _back(shop), 503))
    if _tenant_for(shop) is None:
        return _outcome(shop, attempt, "error",
                        _page("Store not found", "<h1>Store not found</h1><p>Reinstall MiraQ and try again.</p>", 404))

    # The attempt id rides after the shop; the signed part (token + shop) is unchanged.
    state = f"{merchant_session.issue(shop, merchant_session.IG_STATE)}:{shop}"
    if attempt:
        state += f":{attempt}"
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
    token, _, rest = (request.args.get("state") or "").partition(":")
    shop, _, attempt = rest.partition(":")
    shop = shop.strip().lower()
    attempt = attempt_param(attempt)
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
        return _outcome(shop, attempt, "error", _page("Store not found", "<h1>Store not found</h1>", 404))

    if request.args.get("error"):
        # The owner cancelled, or declined a permission.
        logger.info(
            f"instagram callback: login not completed | shop={shop} | "
            f"error={request.args.get('error')} reason={request.args.get('error_reason')}"
        )
        return _outcome(shop, attempt, "cancelled", _page(
            "Instagram not connected",
            "<h1>Instagram wasn't connected</h1>"
            "<p>The Facebook login was cancelled. You can try again any time.</p>" + _back(shop),
        ))

    code = (request.args.get("code") or "").strip()
    if not code:
        return _outcome(shop, attempt, "error",
                        _page("Instagram not connected", "<h1>Something went wrong</h1>" + _back(shop), 400))

    ok, data = _graph("GET", "oauth/access_token",
                      client_id=META_APP_ID, client_secret=META_APP_SECRET,
                      redirect_uri=_redirect_uri(), code=code)
    user_token = data.get("access_token") if ok else None
    if user_token:
        # Long-lived user token, so the Page tokens read with it never expire.
        ok, data = _graph("GET", "oauth/access_token", grant_type="fb_exchange_token",
                          client_id=META_APP_ID, client_secret=META_APP_SECRET,
                          fb_exchange_token=user_token)
        user_token = data.get("access_token") if ok else None
    if not user_token:
        return _outcome(shop, attempt, "error",
                        _page("Instagram not connected",
                              "<h1>Facebook login failed</h1><p>Please try again.</p>" + _back(shop), 502))

    ok, data = _graph("GET", "me/accounts",
                      fields="id,name,access_token,instagram_business_account{id,username}",
                      limit=100, access_token=user_token)
    if not ok:
        return _outcome(shop, attempt, "error",
                        _page("Instagram not connected",
                              "<h1>Couldn't read your Facebook Pages</h1><p>Please try again.</p>" + _back(shop), 502))

    # Who is connecting, shown on the admin page ("Connected by ..."). Best effort.
    ok_me, me = _graph("GET", "me", fields="name", access_token=user_token)
    connected_by = (me.get("name") or "") if ok_me else ""

    pages = [
        {
            "page_id": p["id"],
            "page_name": p.get("name") or "",
            "page_token": p.get("access_token") or "",
            "ig_id": p["instagram_business_account"]["id"],
            "username": p["instagram_business_account"].get("username") or "",
            "connected_by": connected_by,
        }
        for p in data.get("data", [])
        if p.get("instagram_business_account", {}).get("id") and p.get("access_token")
    ]
    logger.info(f"instagram callback: {len(pages)} Page(s) with an Instagram account | shop={shop}")

    if not pages:
        return _outcome(shop, attempt, "no_account", _page(
            "No Instagram account found",
            "<h1>No Instagram account found</h1>"
            "<p>We couldn't find an Instagram account we can connect. Check that:</p><ul>"
            "<li>your Instagram account is a <strong>Business</strong> or <strong>Creator</strong> account,</li>"
            "<li>it is linked to a Facebook Page you manage,</li>"
            "<li>\"Allow access to messages\" is switched on in its Instagram settings, and</li>"
            "<li>you selected that Page and that Instagram account when Facebook asked.</li></ul>"
            + _back(shop),
        ))

    if len(pages) == 1:
        status, label = _connect(tenant, shop, pages[0])
        return _outcome(shop, attempt, status, _render_connect(shop, status, label), label=label)

    if attempt:
        # Several accounts: the merchant chooses in the admin page
        # (choose_for_attempt). The list, Page tokens included, is kept
        # encrypted on the attempt and cleared once used or expired.
        return _outcome(shop, attempt, "choose", None, pages=pages)

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

    removed = disconnect_store(tenant)
    logger.info(f"instagram disconnect: removed {removed} connection(s) | shop={shop} tenant={tenant.tenant_id}")
    return redirect(app_page_url(shop), code=303)


def unsubscribe(row) -> None:
    """
    Best effort: tell Meta to stop sending this Page's messages to our app,
    using the Page token saved with the row. Rows without a token (added by
    hand, or saved before tokens were kept) are skipped. Never raises.
    """
    if row.channel != _CHANNEL or not row.page_id or not is_configured():
        return
    token = row.access_token
    if not token:
        return
    ok, _ = _graph("DELETE", f"{row.page_id}/subscribed_apps", access_token=token)
    logger.info(f"instagram: page {row.page_id} unsubscribed={ok} | ig={row.external_account_id}")


def remove_connections(rows) -> int:
    """Unsubscribe each row's Page, then delete the rows. Caller commits."""
    for row in rows:
        unsubscribe(row)
        db.session.delete(row)
    return len(rows)


def disconnect_store(tenant) -> int:
    """Remove the store's Instagram connection(s), unsubscribing first. Commits."""
    rows = ChannelConnection.query.filter_by(tenant_id=tenant.tenant_id, channel=_CHANNEL).all()
    removed = remove_connections(rows)
    db.session.commit()
    return removed


# ── connect ──────────────────────────────────────────────────────────────────

def _connect_and_render(tenant, shop: str, page: dict):
    status, label = _connect(tenant, shop, page)
    return _render_connect(shop, status, label)


def _connect(tenant, shop: str, page: dict):
    """
    Subscribe the Page and save the routing row. Returns (status, label):
    "connected", "taken" (the account belongs to another store) or "failed"
    (Facebook refused the subscription). Renders nothing, so both the custom
    app pages and the embedded admin attempt use it.
    """
    ig_id = page["ig_id"]
    label = _label(page)

    # Same rule as POST /channel-connections: never take an account from
    # another store silently.
    existing = ChannelConnection.query.filter_by(channel=_CHANNEL, external_account_id=ig_id).first()
    if existing is not None and existing.tenant_id != tenant.tenant_id:
        logger.warning(
            f"instagram connect: {ig_id} already connected to tenant={existing.tenant_id} | "
            f"refused for shop={shop}"
        )
        return "taken", label

    # 1. Subscribe the Page, so Meta sends this account's DMs to the webhook.
    ok, data = _graph("POST", f"{page['page_id']}/subscribed_apps",
                      subscribed_fields=META_PAGE_SUBSCRIBED_FIELDS,
                      access_token=page["page_token"])
    if not ok or not data.get("success"):
        return "failed", label

    # 2. Save the routing row and the Page token. One Instagram account per
    #    store: any other is unsubscribed and removed.
    remove_connections(
        ChannelConnection.query
        .filter(ChannelConnection.tenant_id == tenant.tenant_id,
                ChannelConnection.channel == _CHANNEL,
                ChannelConnection.external_account_id != ig_id)
        .all()
    )
    row = existing or ChannelConnection(tenant_id=tenant.tenant_id, channel=_CHANNEL, external_account_id=ig_id)
    if existing is None:
        db.session.add(row)
    row.status = "active"
    row.display_name = label
    row.page_id = page["page_id"]
    row.page_name = page.get("page_name") or None
    row.connected_by = page.get("connected_by") or None
    row.set_access_token(page["page_token"])
    db.session.commit()

    enabled = (tenant.features or {}).get("channels")
    if isinstance(enabled, list) and _CHANNEL not in enabled:
        logger.warning(f"instagram connect: tenant={tenant.tenant_id} has channels={enabled}; "
                       "/chat/channel will refuse Instagram until it is enabled")

    logger.info(
        f"instagram connect: ✅ connected | shop={shop} tenant={tenant.tenant_id} | "
        f"ig={ig_id} ({label}) page={page['page_id']}"
    )
    return "connected", label


def _render_connect(shop: str, status: str, label: str):
    """The custom-app page for a _connect() result."""
    if status == "taken":
        return _page(
            "Already connected",
            f"<h1>{html.escape(label)} is connected to another store</h1>"
            "<p>Disconnect it from that store's MiraQ first, then try again.</p>" + _back(shop),
            409,
        )
    if status == "failed":
        return _page(
            "Instagram not connected",
            f"<h1>Couldn't connect {html.escape(label)}</h1>"
            "<p>Facebook didn't allow MiraQ to receive this account's messages. Try again, "
            "and accept all the permissions Facebook asks for.</p>" + _back(shop),
            502,
        )
    return _page(
        "Instagram connected",
        f"<h1>✅ Instagram connected</h1><p><strong>{html.escape(label)}</strong> is now "
        "connected to your store. Customers who message it will get answers from MiraQ.</p>"
        + _back(shop),
    )
