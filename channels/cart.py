"""
Cart support for messaging channels (Shopify).

WHY THIS EXISTS
    A Shopify cart is a browser cookie, and the widget's SHOPIFY_ADD_TO_CART
    action runs in that browser. A shopper on Instagram/WhatsApp has no such
    session, so the action goes nowhere and a plain {store}/cart link would
    open an EMPTY cart.

    Instead the channel layer keeps the shopper's lines (variant id + quantity)
    in the conversation's context_data and hands them to Shopify in a cart
    permalink when the shopper taps "View cart" / "Checkout":

        {store}/cart/{variant}:{qty},...                    -> checkout
        {store}/cart/{variant}:{qty},...?storefront=true    -> cart page

    No Shopify cart is created server-side; the store builds it when the link
    is opened. Pure functions only: no Flask, no DB, no import of
    channels.common (common imports this module).

    Opening the same link twice in one browser adds the items twice (Shopify
    permalinks add to the existing cart). Accepted; noted in the plan.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import quote

CART_CONTEXT_KEY = "channel_cart"
ADD_ACTION       = "SHOPIFY_ADD_TO_CART"
CART_ACTIONS     = frozenset({"OPEN_CART_PANEL", "OPEN_CHECKOUT_PANEL"})

# A permalink must stay well under Instagram's 2000-char web_url limit.
MAX_LINES = 50
MAX_QTY   = 999
_SUMMARY_LINES = 5

EMPTY_CART_TEXT = "Your cart is empty. Tell me what you'd like to add and I'll put it in."
CHECKOUT_TEXT   = "Tap below to review your order and pay on the store's secure checkout."
CART_FULL_TEXT  = "Your cart is full ({max} different items), so I couldn't add: {names}."


@dataclass
class ChannelCart:
    lines: List[dict] = field(default_factory=list)     # [{"v": "123", "q": 2, "n": "Name"}]
    rejected: List[str] = field(default_factory=list)   # names that did not fit this turn


def normalize(raw) -> List[dict]:
    """Clean lines read back from context_data; anything malformed is dropped."""
    lines = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        v = str(item.get("v") or "").strip()
        try:
            q = int(item.get("q"))
        except (TypeError, ValueError):
            continue
        if v.isdigit() and q >= 1:
            lines.append({"v": v, "q": min(q, MAX_QTY), "n": str(item.get("n") or "")[:120]})
    return lines[:MAX_LINES]


def add_lines(lines: List[dict], actions) -> Tuple[bool, List[str]]:
    """
    Merge SHOPIFY_ADD_TO_CART actions into `lines` (in place).
    Returns (changed, rejected_names). An add that cannot be recorded is
    reported rather than dropped silently.
    """
    changed, rejected = False, []
    for action in actions or []:
        if (action or {}).get("type") != ADD_ACTION:
            continue
        payload = action.get("payload") or {}
        variant = str(payload.get("variant_numeric_id") or "").strip()
        name = str(payload.get("name") or "").strip()
        if not variant.isdigit():
            rejected.append(name or "an item")
            continue
        try:
            qty = max(1, int(payload.get("quantity") or 1))
        except (TypeError, ValueError):
            qty = 1
        existing = next((l for l in lines if l["v"] == variant), None)
        if existing:
            existing["q"] = min(existing["q"] + qty, MAX_QTY)
        elif len(lines) < MAX_LINES:
            lines.append({"v": variant, "q": min(qty, MAX_QTY), "n": name[:120]})
        else:
            rejected.append(name or "an item")
            continue
        changed = True
    return changed, rejected


def cart_url(store_url: str, lines: List[dict], *, checkout: bool,
             session_id: Optional[str] = None) -> str:
    """Cart permalink. "" when there is nothing to open."""
    if not store_url or not lines:
        return ""
    path = ",".join(f"{l['v']}:{l['q']}" for l in lines)
    params = [] if checkout else ["storefront=true"]
    if session_id:
        # Becomes an order note attribute; /events/order-paid uses it to post
        # "order placed" back into this chat (same as the reorder link).
        params.append("attributes%5Bmiraq_session_id%5D=" + quote(str(session_id)))
    url = f"{store_url.rstrip('/')}/cart/{path}"
    return url + ("?" + "&".join(params) if params else "")


def _line_text(l: dict) -> str:
    return f"• {l['n'] or 'Item'} ×{l['q']}"


def cart_summary(lines: List[dict]) -> str:
    out = [_line_text(l) for l in lines[:_SUMMARY_LINES]]
    if len(lines) > _SUMMARY_LINES:
        out.append(f"…and {len(lines) - _SUMMARY_LINES} more")
    return "\n".join(out)


def added_text(actions) -> str:
    """What the shopper is told after an add (replaces 'Adding … to your cart…')."""
    parts = []
    for a in actions or []:
        if (a or {}).get("type") != ADD_ACTION:
            continue
        p = a.get("payload") or {}
        parts.append(f"{p.get('name') or 'Item'} ×{p.get('quantity') or 1}")
    if not parts:
        return ""
    return ("Added to your cart: " + ", ".join(parts) +
            ".\nSay \"view cart\" or \"checkout\" when you're ready.")