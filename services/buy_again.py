"""
services/buy_again.py — the welcome screen's "Buy again" (requirements FR-5).

Up to 6 distinct products from the shopper's last 3 orders, most recent
first, each with the exact cart-add the widget should perform for the same
variant they bought, quantity 1.

Ownership (FR-5.6), same rules the chat's order lookups follow:
  - WooCommerce: GET wc/v3/orders?customer=<verified id>, then every order is
    re-checked against that id (_only_own_orders in routes/chat.py does the
    same) — the call uses the store's admin keys, so the filter is the only
    thing scoping it.
  - Shopify: orders are read through the customer node
    (ShopifyOrdersExecutor), so only that customer's orders can come back;
    re-checked here too.

Current availability comes from the in-memory catalog only — no per-item
store calls, so the screen stays fast:
  - product gone / unpublished / hidden / out of stock → item left out;
  - the bought variant no longer exists → needs_options (the widget sends the
    shopper to the chat's variant flow instead of adding blind).
WooCommerce variation STOCK is not in the catalog (only the parent's), so a
sold-out variation of an in-stock product can still be offered; the Store API
then refuses the add and the widget shows the "choose options" route.

No LLM, no chat turn, nothing written to the conversation.
"""

from typing import Dict, List, Optional

from chat_logger import get_logger

logger = get_logger("miraq_chat")

BUY_AGAIN_LIMIT = 6
ORDERS_CONSIDERED = 3
# Fetched before status filtering, so a cancelled order does not use up one
# of the three.
_ORDERS_FETCHED = 5

# Orders whose items were not actually bought.
#   WooCommerce: "pending" = awaiting payment (COD goes straight to processing).
#   Shopify: "pending" is how COD / manual-payment orders look, so it stays.
_SKIP_STATUS_WOO = frozenset({"pending", "cancelled", "failed", "refunded", "checkout-draft", "trash"})
_SKIP_STATUS_SHOPIFY = frozenset({"cancelled", "refunded"})


def _tail(val) -> str:
    """Numeric tail of a GID, or the value itself: "gid://shopify/X/12" → "12"."""
    return str(val).split("/")[-1] if val not in (None, "") else ""


# ── Orders ───────────────────────────────────────────────────────────────────

def _fetch_orders_woo(loader, customer_id: str) -> List[dict]:
    # customer=0 (or a non-number) would make wc/v3 return GUEST orders —
    # everyone's. Only a real WordPress user id goes in the filter.
    if not str(customer_id).isdigit() or int(customer_id) <= 0:
        return []
    from ecommerce import endpoints
    from woo_client import woo_client
    call = endpoints.list_customer_orders(
        customer_id=int(customer_id), page=1, per_page=_ORDERS_FETCHED,
        description="Welcome screen: buy again",
    )
    resp = woo_client.execute(call, loader=loader)
    data = resp.get("data") if resp.get("success") else None
    orders = data if isinstance(data, list) else []
    own = str(customer_id)
    return [o for o in orders
            if isinstance(o, dict)
            and str(o.get("customer_id") or "") == own
            and (o.get("status") or "") not in _SKIP_STATUS_WOO]


def _fetch_orders_shopify(customer_id: str) -> List[dict]:
    from api_builder.shopify_order_calls import build_order_history_call
    from api_builder.shopify_orders_executor import ShopifyOrdersExecutor
    call = build_order_history_call(customer_id, page=1, per_page=_ORDERS_FETCHED,
                                    description="Welcome screen: buy again")
    result = ShopifyOrdersExecutor().execute(call) or {}
    own = _tail(customer_id)
    return [o for o in (result.get("orders") or [])
            if isinstance(o, dict)
            and _tail(o.get("customer_id")) == own
            and (o.get("status") or "") not in _SKIP_STATUS_SHOPIFY]


# ── Catalog matching ─────────────────────────────────────────────────────────

def _visible(raw: dict) -> bool:
    status = raw.get("status")
    if status is not None and status not in ("publish", "active"):
        return False
    return raw.get("catalog_visibility") != "hidden"


def _first_image(raw: dict) -> str:
    for img in raw.get("images") or []:
        src = img.get("src") if isinstance(img, dict) else img
        if src:
            return src
    return ""


def _woo_variation_payload(line: dict, raw: dict) -> List[Dict[str, str]]:
    """Store API "variation" array from the order line's own attribute meta.

    WooCommerce stores the chosen attributes on the line item as meta: global
    attributes under their taxonomy ("pa_finish": "glossy" — the term slug,
    exactly what the Store API matches on), custom ones under the sanitized
    attribute name. This includes the values for "Any …" axes, which the
    variation itself does not carry and the Store API still requires.
    """
    custom = {
        str(a.get("name") or "").strip().lower().replace(" ", "-")
        for a in (raw.get("attributes") or [])
        if isinstance(a, dict) and a.get("variation") and not a.get("id")
    }
    out = []
    for m in line.get("meta_data") or []:
        if not isinstance(m, dict):
            continue
        key, value = str(m.get("key") or ""), m.get("value")
        if not key or key.startswith("_") or not isinstance(value, str) or not value:
            continue
        if key.startswith("pa_") or key.lower() in custom:
            out.append({"attribute": key, "value": value})
    return out


def _woo_variant_label(line: dict) -> Optional[str]:
    parts = []
    for m in line.get("meta_data") or []:
        if not isinstance(m, dict) or str(m.get("key") or "").startswith("_"):
            continue
        shown = m.get("display_value")
        if isinstance(shown, str) and shown.strip():
            parts.append(shown.strip())
    return " / ".join(parts) or None


def _woo_item(line: dict, raw: dict) -> Optional[dict]:
    from core.actions import build_add_to_cart
    from formatters import format_product
    fp = format_product(raw)
    if not fp.get("in_stock") or not fp.get("purchasable", True):
        return None

    pid = int(raw.get("id"))
    vid = int(line.get("variation_id") or 0)
    is_variable = (raw.get("type") == "variable")
    current_variations = {int(v) for v in (raw.get("variations") or []) if str(v).isdigit()}

    needs_options = False
    add = None
    price = None
    if is_variable:
        if vid and vid in current_variations:
            add = build_add_to_cart(pid, 1, name=fp.get("name"), variation_id=vid,
                                    variation=_woo_variation_payload(line, raw) or None)
        else:
            needs_options = True      # variant removed, or product became variable
        # Variation prices are not in the catalog; the parent's is a range
        # minimum, which would be wrong for this variant — so no price.
    else:
        add = build_add_to_cart(pid, 1, name=fp.get("name"))
        price = fp.get("sale_price") if fp.get("on_sale") and fp.get("sale_price") else fp.get("price")

    line_img = line.get("image") if isinstance(line.get("image"), dict) else {}
    return {
        "key":             f"{pid}:{vid}",
        "product_id":      pid,
        "variation_id":    vid or None,
        "name":            fp.get("name") or line.get("name") or "",
        "variant":         _woo_variant_label(line) if vid else None,
        "image":           line_img.get("src") or _first_image(raw),
        "price":           price or None,
        "currency_symbol": fp.get("currency_symbol") or "",
        "permalink":       fp.get("permalink") or "",
        "add":             add,
        "needs_options":   needs_options,
    }


def _shopify_item(line: dict, raw: dict) -> Optional[dict]:
    from core.actions import build_shopify_add_to_cart
    from formatters import format_product
    fp = format_product(raw)
    if not fp.get("in_stock"):
        return None

    want = _tail(line.get("variation_id"))
    variant = next((v for v in (raw.get("variations") or [])
                    if want and _tail(v.get("_shopify_gid") or v.get("id")) == want), None)
    if variant is not None and not variant.get("in_stock"):
        return None                   # that exact variant is sold out
    gid = (variant or {}).get("_shopify_gid") or (variant or {}).get("id")

    label = " / ".join(str(a.get("option")) for a in (variant or {}).get("attributes") or []
                       if isinstance(a, dict) and a.get("option")
                       and str(a.get("option")).lower() != "default title")
    return {
        "key":             f"{_tail(raw.get('_shopify_gid') or raw.get('id'))}:{want}",
        "product_id":      raw.get("_shopify_gid") or raw.get("id"),
        "variation_id":    gid or None,
        "name":            fp.get("name") or line.get("name") or "",
        "variant":         label or None,
        "image":           (variant or {}).get("image") or _first_image(raw),
        "price":           (variant or {}).get("price") or None,
        "currency_symbol": fp.get("currency_symbol") or "",
        "permalink":       fp.get("permalink") or "",
        "add":             build_shopify_add_to_cart(gid, 1, name=fp.get("name")) if gid else None,
        "needs_options":   gid is None,
    }


# ── Public ───────────────────────────────────────────────────────────────────

def buy_again(loader, customer_id: str, limit: int = BUY_AGAIN_LIMIT) -> List[dict]:
    """Buy-again items for this verified customer; [] when there are none."""
    if not customer_id or loader is None:
        return []
    shopify = getattr(loader, "ecommerce_backend", "") == "shopify"
    orders = _fetch_orders_shopify(customer_id) if shopify else _fetch_orders_woo(loader, customer_id)
    orders = orders[:ORDERS_CONSIDERED]     # already newest first
    if not orders:
        return []

    if shopify:
        catalog = {_tail(p.get("_shopify_gid") or p.get("id")): p for p in (loader.products or [])}
    else:
        catalog = {str(p.get("id")): p for p in (loader.products or [])}

    items, seen = [], set()
    for order in orders:
        for line in order.get("line_items") or []:
            if not isinstance(line, dict):
                continue
            pid = _tail(line.get("product_id"))
            dedupe = (pid, _tail(line.get("variation_id")) or "0")
            if not pid or dedupe in seen:
                continue
            seen.add(dedupe)
            raw = catalog.get(pid)
            if raw is None or not _visible(raw):
                continue                  # deleted or unpublished since
            item = _shopify_item(line, raw) if shopify else _woo_item(line, raw)
            if item is not None:
                items.append(item)
                if len(items) >= limit:
                    return items
    return items
