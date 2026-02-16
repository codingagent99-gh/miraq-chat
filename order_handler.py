"""
Order Handler for WGC Tiles Store
==================================
Handles order history, reorder, and direct product ordering.

Supported intents:
  - ORDER_HISTORY:    "show my last order", "my past orders"
  - ORDER_LAST:       "what did I order last?"
  - REORDER:          "repeat my last order", "reorder"
  - ORDER_ITEM:       "order this item Ansel", "buy Allspice"
  - ORDER_TRACKING:   "track my order #1234"
  - ORDER_STATUS:     "status of order #1234"
"""

import re
from typing import List, Optional, Dict, Any
from models import Intent, ExtractedEntities, ClassifiedResult, WooAPICall

BASE = "https://wgc.net.in/hn/wp-json/wc/v3"


# ═══════════════════════════════════════════
# INTENT CLASSIFICATION HELPERS
# ═══════════════════════════════════════════

# Patterns grouped by intent
REORDER_PATTERNS = [
    r"\b(repeat|reorder|re-order|order\s*again)\b.*\b(last|previous|recent|same)\b",
    r"\b(last|previous|recent|same)\b.*\b(repeat|reorder|re-order|order\s*again)\b",
    r"\b(repeat|reorder|re-order)\b.*\border\b",
    r"\border\b.*\b(again|repeat|same)\b",
    r"\b(reorder|re-order)\b",
]

ORDER_LAST_PATTERNS = [
    r"\bwhat\b.*\b(did|have)\b.*\border(ed)?\b.*\b(last|recently|before)\b",
    r"\bwhat\b.*\b(was|were)\b.*\b(last|previous|recent)\b.*\border\b",
    r"\bmy\s+last\s+order\b",
    r"\bprevious\s+order\b",
    r"\brecent\s+order\b",
    r"\blast\s+order\b",
]

ORDER_HISTORY_PATTERNS = [
    r"\b(show|view|see|get|list)\b.*\border\s*(history|log|record)\b",
    r"\border\s*(history|log|record)\b",
    r"\b(past|previous|all|my)\b.*\borders\b",
    r"\bwhat\b.*\b(have|did)\b.*\bordered\b.*\bbefore\b",
    r"\bordered\b.*\b(before|previously|in the past)\b",
    r"\bmy\s+orders\b",
    r"\bshow\b.*\b(my|all)\b.*\borders\b",
]

ORDER_ITEM_PATTERNS = [
    r"\b(order|buy|purchase|get)\b.*\b(this|the|a|an)?\s*(item|product|tile)?\s*",
    r"\bi\s*(want|need|would like)\s*to\s*(order|buy|purchase|get)\b",
    r"\b(add|put)\b.*\b(to\s*cart|in\s*cart)\b",
    r"\bcan\s*i\s*(order|buy|purchase|get)\b",
]


def classify_order_intent(text: str, entities: ExtractedEntities) -> Optional[tuple]:
    """
    Classify order-related intents from user text.
    Returns (Intent, confidence) or None if not order-related.

    Priority:
      1. Reorder (must check before order_last to avoid collision)
      2. Order last / show last order
      3. Order history
      4. Order item by name
    """
    text_lower = text.lower().strip()

    # 1. REORDER — "repeat my last order", "reorder", "order again"
    for pattern in REORDER_PATTERNS:
        if re.search(pattern, text_lower):
            entities.reorder = True
            entities.order_count = 1
            return (Intent.REORDER, 0.93)

    # 2. ORDER LAST — "show my last order", "what did I order last?"
    for pattern in ORDER_LAST_PATTERNS:
        if re.search(pattern, text_lower):
            entities.order_count = 1
            return (Intent.ORDER_LAST, 0.92)

    # 3. ORDER HISTORY — "show my order history", "my past orders"
    for pattern in ORDER_HISTORY_PATTERNS:
        if re.search(pattern, text_lower):
            # Extract count if specified: "show my last 5 orders"
            count_match = re.search(r'\b(\d+)\s*orders?\b', text_lower)
            if count_match:
                entities.order_count = int(count_match.group(1))
            else:
                entities.order_count = 10  # default
            return (Intent.ORDER_HISTORY, 0.91)

    # 4. ORDER ITEM — "order this item Ansel", "buy Allspice"
    for pattern in ORDER_ITEM_PATTERNS:
        if re.search(pattern, text_lower):
            # Extract product name from the utterance
            _extract_order_item_name(text_lower, entities)
            if entities.order_item_name or entities.product_name:
                return (Intent.ORDER_ITEM, 0.90)

    return None


def _extract_order_item_name(text: str, entities: ExtractedEntities):
    """
    Extract the product name the user wants to order.
    Handles patterns like:
      - "order this item Ansel"
      - "buy Allspice"
      - "I want to order Waterfall tiles"
    """
    # Known product names from the store (extend as catalog grows)
    KNOWN_PRODUCTS = [
        "allspice", "ansel", "ansel mosaic", "cairo", "cairo mosaic",
        "cord", "divine", "waterfall", "s.s.s.", "sss",
        "affogato", "akard", "adams",
    ]

    text_lower = text.lower()

    # First try to match known product names
    for product in sorted(KNOWN_PRODUCTS, key=len, reverse=True):
        if product in text_lower:
            entities.order_item_name = product.title()
            if not entities.product_name:
                entities.product_name = product.title()
                entities.product_slug = product.replace(" ", "-").replace(".", "")
            return

    # Fallback: extract the noun phrase after the verb
    # "order this item <NAME>", "buy <NAME>", "order <NAME> tiles"
    extraction_patterns = [
        r'\b(?:order|buy|purchase|get)\s+(?:this\s+)?(?:item\s+)?(?:the\s+)?(.+?)(?:\s+tiles?)?$',
        r'\b(?:want|need|like)\s+to\s+(?:order|buy|purchase|get)\s+(.+?)(?:\s+tiles?)?$',
    ]

    for pattern in extraction_patterns:
        match = re.search(pattern, text_lower)
        if match:
            name = match.group(1).strip()
            # Remove common noise words
            noise = {"some", "a", "an", "the", "this", "that", "those", "these", "it", "item", "product"}
            words = [w for w in name.split() if w not in noise]
            if words:
                clean_name = " ".join(words).title()
                entities.order_item_name = clean_name
                if not entities.product_name:
                    entities.product_name = clean_name
                return


# ═══════════════════════════════════════════
# API CALL BUILDERS
# ═══════════════════════════════════════════

def build_order_api_calls(intent: Intent, entities: ExtractedEntities) -> List[WooAPICall]:
    """
    Build WooCommerce API calls for order-related intents.
    Returns list of WooAPICall objects.
    """
    calls = []

    if intent == Intent.ORDER_LAST:
        # GET /orders — fetch the most recent order
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params={
                "customer": "CURRENT_USER_ID",
                "per_page": 1,
                "orderby": "date",
                "order": "desc",
            },
            description="Fetch the customer's most recent order",
            requires_resolution=["CURRENT_USER_ID"],
        ))

    elif intent == Intent.REORDER:
        # Step 1: Fetch the last order to get line items
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params={
                "customer": "CURRENT_USER_ID",
                "per_page": 1,
                "orderby": "date",
                "order": "desc",
            },
            description="Fetch last order for reorder (step 1: get line items)",
            requires_resolution=["CURRENT_USER_ID", "REORDER_LINE_ITEMS"],
        ))
        # Step 2: Create new order with same line items
        # (This will be built dynamically after step 1 resolves)
        calls.append(WooAPICall(
            method="POST",
            endpoint=f"{BASE}/orders",
            params={},
            body={
                "status": "pending",
                "customer_id": "CURRENT_USER_ID",
                "line_items": "REORDER_LINE_ITEMS",
            },
            description="Create new order with same items as last order (step 2)",
            requires_resolution=["CURRENT_USER_ID", "REORDER_LINE_ITEMS"],
        ))

    elif intent == Intent.ORDER_HISTORY:
        count = entities.order_count or 10
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params={
                "customer": "CURRENT_USER_ID",
                "per_page": min(count, 100),
                "orderby": "date",
                "order": "desc",
            },
            description=f"Fetch last {count} orders for customer",
            requires_resolution=["CURRENT_USER_ID"],
        ))

    elif intent == Intent.ORDER_ITEM:
        # Step 1: Search for the product by name
        search_name = entities.order_item_name or entities.product_name or ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={
                "search": search_name,
                "status": "publish",
                "per_page": 5,
            },
            description=f"Search for product '{search_name}' to get product ID",
            requires_resolution=["PRODUCT_ID_FOR_ORDER"],
        ))
        # Step 2: Create order with the found product
        line_items = []
        if entities.product_id:
            item = {"product_id": entities.product_id, "quantity": entities.quantity or 1}
            if entities.variation_id:
                item["variation_id"] = entities.variation_id
            line_items.append(item)

        calls.append(WooAPICall(
            method="POST",
            endpoint=f"{BASE}/orders",
            params={},
            body={
                "status": "pending",
                "customer_id": "CURRENT_USER_ID",
                "line_items": line_items if line_items else "RESOLVE_FROM_SEARCH",
            },
            description=f"Create order for '{search_name}'",
            requires_resolution=["CURRENT_USER_ID", "PRODUCT_ID_FOR_ORDER"],
        ))

    elif intent == Intent.ORDER_TRACKING:
        if entities.order_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/orders/{entities.order_id}",
                params={},
                description=f"Track order #{entities.order_id}",
            ))
        else:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/orders",
                params={
                    "customer": "CURRENT_USER_ID",
                    "per_page": 5,
                    "orderby": "date",
                    "order": "desc",
                },
                description="List recent orders (no order ID provided for tracking)",
                requires_resolution=["CURRENT_USER_ID"],
            ))

    elif intent == Intent.ORDER_STATUS:
        if entities.order_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/orders/{entities.order_id}",
                params={},
                description=f"Get status of order #{entities.order_id}",
            ))
        else:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/orders",
                params={
                    "customer": "CURRENT_USER_ID",
                    "per_page": 5,
                    "orderby": "date",
                    "order": "desc",
                },
                description="List recent orders (no order ID provided for status)",
                requires_resolution=["CURRENT_USER_ID"],
            ))

    return calls


# ═══════════════════════════════════════════
# ORDER RESPONSE FORMATTER
# ═══════════════════════════════════════════

def format_order(order: dict) -> dict:
    """Format a raw WooCommerce order into a clean response dict."""
    line_items = []
    for item in order.get("line_items", []):
        line_items.append({
            "product_id": item.get("product_id"),
            "variation_id": item.get("variation_id"),
            "name": item.get("name", ""),
            "quantity": item.get("quantity", 0),
            "price": item.get("price", "0"),
            "subtotal": item.get("subtotal", "0"),
            "total": item.get("total", "0"),
            "sku": item.get("sku", ""),
            "image_url": item.get("image", {}).get("src", "") if item.get("image") else "",
        })

    return {
        "order_id": order.get("id"),
        "order_number": order.get("number", order.get("id")),
        "status": order.get("status", "unknown"),
        "date_created": order.get("date_created", ""),
        "date_modified": order.get("date_modified", ""),
        "total": order.get("total", "0"),
        "currency": order.get("currency", "INR"),
        "payment_method": order.get("payment_method_title", ""),
        "line_items": line_items,
        "item_count": sum(i.get("quantity", 0) for i in line_items),
        "billing": {
            "first_name": order.get("billing", {}).get("first_name", ""),
            "last_name": order.get("billing", {}).get("last_name", ""),
            "email": order.get("billing", {}).get("email", ""),
        },
        "shipping": {
            "first_name": order.get("shipping", {}).get("first_name", ""),
            "last_name": order.get("shipping", {}).get("last_name", ""),
            "city": order.get("shipping", {}).get("city", ""),
            "state": order.get("shipping", {}).get("state", ""),
        },
    }


def generate_order_bot_message(
    intent: Intent,
    entities: ExtractedEntities,
    orders: List[dict],
) -> str:
    """Generate natural language bot response for order-related intents."""

    if not orders:
        if intent == Intent.REORDER:
            return (
                "I couldn't find any previous orders to reorder. 😕\n\n"
                "Would you like to browse our products instead?"
            )
        elif intent == Intent.ORDER_ITEM:
            product_name = entities.order_item_name or entities.product_name or "that product"
            return (
                f"I couldn't find **{product_name}** in our catalog. 😕\n\n"
                "Please check the product name and try again, or browse our collections."
            )
        elif intent in (Intent.ORDER_TRACKING, Intent.ORDER_STATUS):
            return (
                "I couldn't find any orders matching your request. 😕\n\n"
                "Please provide your order number (e.g., 'status of order #1234')."
            )
        else:
            return (
                "No order history found. 📭\n\n"
                "Looks like you haven't placed any orders yet!\n"
                "Browse our tile collection to get started."
            )

    # ── SINGLE ORDER (last order / tracking / status) ──
    if intent in (Intent.ORDER_LAST, Intent.ORDER_TRACKING, Intent.ORDER_STATUS):
        order = orders[0]
        msg = _format_single_order_message(order, intent)
        return msg

    # ── REORDER ──
    if intent == Intent.REORDER:
        order = orders[0]
        msg = "🔄 **Reordering your last order!**\n\n"
        msg += _format_order_items(order)
        msg += (
            "\n📝 A new order has been created with the same items.\n"
            f"Total: **₹{order.get('total', '0')}**\n\n"
            "Please confirm to proceed with payment."
        )
        return msg

    # ── ORDER ITEM ──
    if intent == Intent.ORDER_ITEM:
        product_name = entities.order_item_name or entities.product_name or "the product"
        msg = f"🛒 **Adding {product_name} to your order!**\n\n"
        if orders:
            order = orders[0]
            msg += f"A new order has been created.\n"
            msg += f"Status: **{order.get('status', 'pending').title()}**\n"
            msg += f"Total: **₹{order.get('total', '0')}**\n\n"
            msg += "Please confirm to proceed with payment."
        return msg

    # ── ORDER HISTORY ──
    msg = f"📋 **Your Order History** ({len(orders)} orders)\n\n"
    for i, order in enumerate(orders[:10], 1):
        status_emoji = _status_emoji(order.get("status", ""))
        date = order.get("date_created", "")[:10]
        total = order.get("total", "0")
        order_num = order.get("order_number", order.get("order_id", "?"))
        item_count = order.get("item_count", 0)

        msg += f"{i}. {status_emoji} **Order #{order_num}** — {date}\n"
        msg += f"   Items: {item_count} | Total: ₹{total} | Status: {order.get('status', '?').title()}\n"

        # Show first 2 line items
        for item in order.get("line_items", [])[:2]:
            msg += f"   • {item.get('name', '?')} (×{item.get('quantity', 1)})\n"
        if len(order.get("line_items", [])) > 2:
            remaining = len(order["line_items"]) - 2
            msg += f"   ...and {remaining} more item(s)\n"
        msg += "\n"

    if len(orders) > 10:
        msg += f"\n_Showing 10 of {len(orders)} orders._"

    return msg


def _format_single_order_message(order: dict, intent: Intent) -> str:
    """Format a single order for display."""
    status_emoji = _status_emoji(order.get("status", ""))
    order_num = order.get("order_number", order.get("order_id", "?"))
    date = order.get("date_created", "")[:10]

    if intent == Intent.ORDER_LAST:
        msg = f"📦 **Your Last Order** (#{order_num})\n\n"
    elif intent == Intent.ORDER_TRACKING:
        msg = f"🚚 **Tracking Order #{order_num}**\n\n"
    else:
        msg = f"📋 **Order #{order_num} Status**\n\n"

    msg += f"• **Date:** {date}\n"
    msg += f"• **Status:** {status_emoji} {order.get('status', '?').title()}\n"
    msg += f"• **Total:** ₹{order.get('total', '0')}\n"
    if order.get("payment_method"):
        msg += f"• **Payment:** {order['payment_method']}\n"
    msg += f"• **Items:** {order.get('item_count', 0)}\n"

    msg += "\n**Order Items:**\n"
    msg += _format_order_items(order)

    return msg


def _format_order_items(order: dict) -> str:
    """Format line items of an order."""
    msg = ""
    for item in order.get("line_items", []):
        name = item.get("name", "Unknown")
        qty = item.get("quantity", 1)
        total = item.get("total", "0")
        msg += f"  • **{name}** × {qty} — ₹{total}\n"
    return msg


def _status_emoji(status: str) -> str:
    """Map order status to emoji."""
    return {
        "pending": "⏳",
        "processing": "⚙️",
        "on-hold": "⏸️",
        "completed": "✅",
        "cancelled": "❌",
        "refunded": "💸",
        "failed": "🚫",
        "trash": "🗑️",
    }.get(status, "📦")


def generate_order_suggestions(
    intent: Intent,
    entities: ExtractedEntities,
    orders: List[dict],
) -> List[str]:
    """Generate follow-up suggestions for order-related intents."""
    suggestions = []

    if intent == Intent.ORDER_LAST:
        suggestions.append("Repeat my last order")
        suggestions.append("Show my order history")
        if orders and orders[0].get("line_items"):
            first_item = orders[0]["line_items"][0].get("name", "").split(" ")[0]
            if first_item:
                suggestions.append(f"Show me {first_item} tiles")
        suggestions.append("Track my order")

    elif intent == Intent.REORDER:
        suggestions.append("Show my last order")
        suggestions.append("Show my order history")
        suggestions.append("Browse all products")

    elif intent == Intent.ORDER_HISTORY:
        suggestions.append("Show my last order")
        suggestions.append("Repeat my last order")
        suggestions.append("Browse all products")
        suggestions.append("What's on sale?")

    elif intent == Intent.ORDER_ITEM:
        suggestions.append("Show my orders")
        suggestions.append("Browse all products")
        suggestions.append("What categories do you have?")

    elif intent in (Intent.ORDER_TRACKING, Intent.ORDER_STATUS):
        suggestions.append("Show my last order")
        suggestions.append("Show my order history")
        suggestions.append("Browse all products")

    else:
        suggestions.append("Show my last order")
        suggestions.append("Browse all products")

    return suggestions[:4]