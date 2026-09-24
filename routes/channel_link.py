"""
routes/channel_link.py — the two web pages of the channel sign-in
(see channel_link.py for the whole flow).

  GET /channel-link/start?s=<state>
      "You're linking this chat to <store>" + Continue → Shopify sign-in.
  GET /channel-link/shopify/callback?code&state   (or ?error=…&state)
      Finishes the sign-in, saves the link, says "go back to the chat".

Both are exempt from licence-based tenant lookup (store_registry): the store
comes from the state value, and the state is what authenticates the request.

Every page is sent with Referrer-Policy: no-referrer, because the state is in
the URL and must not leak to Shopify's page in a Referer header, and with
X-Frame-Options: DENY and no-store caching.
"""

import html

from flask import Blueprint, request

import channel_link as cl
from chat_logger import get_logger
from store_registry import bind_tenant_db

logger = get_logger("miraq_chat")

channel_link_bp = Blueprint("channel_link", __name__)


def _page(title: str, body_html: str, status: int = 200):
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:28rem;margin:3rem auto;"
        "padding:0 1.25rem;line-height:1.55;color:#1a1a1a}h1{font-size:1.4rem}"
        ".btn{display:inline-block;background:#1a1a1a;color:#fff;padding:.75rem 1.25rem;border-radius:10px;"
        "text-decoration:none;font-weight:600}.muted{color:#5d6273;font-size:.9rem}</style>"
        f"</head><body>{body_html}</body></html>"
    )
    return doc, status, {
        "Content-Type": "text/html; charset=utf-8",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Cache-Control": "no-store",
    }


def _error_page(message: str, status: int = 400):
    return _page("Sign-in", f"<h1>Sign-in didn't work</h1><p>{html.escape(message)}</p>"
                            "<p class='muted'>Go back to the chat and ask again to get a new link.</p>",
                 status)


def _store_label(tenant) -> str:
    domain = (tenant.shopify_domain or "").strip()
    return domain[: -len(".myshopify.com")] if domain.endswith(".myshopify.com") else domain or "this store"


def _load(state: str):
    """(tenant, None) for a Shopify store whose database is bound, else (None, error page)."""
    tenant = cl.tenant_from_state(state)
    if tenant is None or (tenant.ecommerce_backend or "") != "shopify" or not tenant.is_active:
        return None, _error_page("This sign-in link isn't valid.", 404)
    bind_tenant_db(tenant)
    cl.ensure_tables(tenant)
    return tenant, None


@channel_link_bp.route(cl.START_PATH, methods=["GET"])
def channel_link_start():
    state = (request.args.get("s") or "").strip()
    tenant, err = _load(state)
    if err:
        return err
    row = cl.pending_request(state)
    if row is None:
        return _error_page("This sign-in link has expired or was already used.", 410)
    try:
        url = cl.authorize_url(tenant, state, row)
    except cl.ChannelLinkError as e:
        logger.error(f"channel_link: start failed | tenant={tenant.tenant_id} | {e}")
        return _error_page(e.user_message, 502)

    channel = html.escape(row.channel.capitalize())
    store = html.escape(_store_label(tenant))
    return _page("Sign in", (
        f"<h1>Link your {channel} chat</h1>"
        f"<p>You're linking this {channel} chat to your account at <strong>{store}</strong>, "
        "so the assistant can show your orders.</p>"
        "<p>Next you'll sign in with the store's own login. We never see your password.</p>"
        f"<p><a class='btn' href='{html.escape(url)}'>Continue to sign in</a></p>"
        "<p class='muted'>Didn't ask for this, or this isn't your chat? Just close this page.</p>"
    ))


@channel_link_bp.route(cl.CALLBACK_PATH, methods=["GET"])
def channel_link_callback():
    state = (request.args.get("state") or "").strip()
    tenant, err = _load(state)
    if err:
        return err

    # Consume first: a replayed or doubled callback finds nothing to use.
    row = cl.consume_request(state)
    if row is None:
        return _error_page("This sign-in link has expired or was already used.", 410)

    if request.args.get("error"):
        logger.info(f"channel_link: sign-in cancelled | tenant={tenant.tenant_id} | "
                    f"error={request.args.get('error')!r}")
        return _page("Sign-in cancelled", "<h1>Sign-in cancelled</h1>"
                     "<p>No problem. You can go back to the chat and ask again any time.</p>")

    code = (request.args.get("code") or "").strip()
    if not code:
        return _error_page("The store didn't send a sign-in code.")

    try:
        customer_id, email = cl.complete_sign_in(tenant, row, code)
    except cl.ChannelLinkError as e:
        logger.error(f"channel_link: callback failed | tenant={tenant.tenant_id} | {e}")
        return _error_page(e.user_message, 502)

    link = cl.save_link(row.channel, row.channel_user_id, customer_id, email)
    channel = html.escape(row.channel.capitalize())
    return _page("Signed in", (
        "<h1>You're signed in</h1>"
        f"<p>Your {channel} chat is now linked to <strong>{html.escape(link.email_display or 'your account')}</strong>.</p>"
        f"<p>Go back to {channel} and send your question again, for example “show my orders”.</p>"
        "<p class='muted'>To sign out of the chat later, reply “unlink”.</p>"
    ))
