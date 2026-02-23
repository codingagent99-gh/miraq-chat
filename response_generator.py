"""
Response generation module for bot messages, suggestions, and formatting.
"""

from typing import List, Optional
from datetime import datetime

from models import Intent, ExtractedEntities, WooAPICall
from app_config import MAX_DISPLAYED_ITEMS, USER_PLACEHOLDERS


def generate_bot_message(
    intent: Intent,
    entities: ExtractedEntities,
    products: List[dict],
    confidence: float,
    order_data: List[dict] = None,
    total_items: Optional[int] = None,
) -> str:
    """Generate a natural language bot response.

    Args:
        total_items: The real total number of matching products across all pages
                     (from X-WP-Total header). When provided, this is shown in
                     the message instead of len(products) which is just the
                     current page size.
    """

    if order_data is None:
        order_data = []

    page_count = len(products)
    # Display count: use total_items (real total across all pages) when available,
    # otherwise fall back to the number of products on this page.
    count = total_items if total_items is not None else page_count

    # ── Greeting intent ──
    if intent == Intent.GREETING:
        return (
            "👋 Hello! Welcome to our store! How can I help you today?\n\n"
            "You can ask me about our tiles, browse categories, check your orders, or search for specific products."
        )

    # ── Order-specific handling ──
    if intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY, Intent.REORDER):
        if intent == Intent.ORDER_HISTORY and order_data:
            return _format_order_history_message(order_data)
        elif intent == Intent.LAST_ORDER and order_data:
            order = order_data[0]
            order_id = order.get("id", "")
            order_number = order.get("number", str(order_id))
            status = order.get("status", "unknown").title()
            total = order.get("total", "0")
            date_created = order.get("date_created", "")

            msg = f"📦 **Your Last Order** (#{order_number})\n\n"
            msg += f"**Status:** {status}\n"
            msg += f"**Date:** {_format_order_date(date_created)}\n"
            msg += f"**Total:** ${total}\n\n"

            line_items = order.get("line_items", [])
            if line_items:
                msg += "**Items:**\n"
                for item in line_items:
                    qty = item.get("quantity", 0)
                    name = item.get("name") or "Unknown Item"
                    item_total = item.get("total", "0")
                    msg += f"  • {name} × {qty} — ${item_total}\n"

            return msg

        elif intent == Intent.REORDER and order_data:
            source_order = order_data[0]
            source_number = source_order.get("number", str(source_order.get("id", "")))
            line_items = source_order.get("line_items", [])
            new_order = order_data[1] if len(order_data) > 1 else None

            msg = f"🔄 **Reorder placed** (based on order #{source_number})\n\n"
            if line_items:
                msg += "**Items reordered:**\n"
                for item in line_items:
                    qty = item.get("quantity", 1)
                    name = item.get("name") or "Unknown Item"
                    msg += f"  • {name} × {qty}\n"

            if new_order and new_order.get("id"):
                new_number = new_order.get("number", str(new_order.get("id", "")))
                msg += f"\n✅ New order **#{new_number}** created successfully with status **Processing**."
            else:
                msg += "\n⚠️ Items identified — but the new order could not be created automatically. Please place the order manually or contact support."

            return msg

        if page_count == 0 and not order_data:
            if intent == Intent.LAST_ORDER:
                return (
                    "I can show you your most recent order! 📦\n\n"
                    "Please make sure you're logged in so I can retrieve your order history."
                )
            elif intent == Intent.ORDER_HISTORY:
                return (
                    "I'd love to show your order history! 📦\n\n"
                    "Please make sure you're logged in so I can retrieve your orders."
                )
            elif intent == Intent.REORDER:
                return (
                    "I can reorder from your last purchase! 🔄\n\n"
                    "Please make sure you're logged in first."
                )

    # ── Quick order / Order item / Place order ──
    if intent in (Intent.QUICK_ORDER, Intent.ORDER_ITEM, Intent.PLACE_ORDER):
        if order_data:
            placed = order_data[-1]
            order_number = placed.get("number") or placed.get("id", "N/A")
            if products:
                p_name = products[0]["name"]
            elif placed.get("line_items"):
                p_name = placed["line_items"][0].get("name") or "your item"
            else:
                p_name = "your item"
            total = placed.get("total", "0.00")
            if float(total) == 0.0 and placed.get("line_items"):
                line_total = sum(float(item.get("total", "0") or "0") for item in placed["line_items"])
                if line_total > 0:
                    total = str(line_total)

            # Extract quantity from line_items or entities
            quantity = 1
            if placed.get("line_items"):
                quantity = sum(item.get("quantity", 1) for item in placed["line_items"])
            elif hasattr(entities, 'quantity') and entities.quantity:
                quantity = entities.quantity
            return (
                f"✅ **Order #{order_number} placed successfully!**\n\n"
                f"**Product:** {p_name}\n"
                f"**Quantity:** {quantity}\n"
                f"**Total:** ${float(total):.2f}\n"
                f"**Payment Mode:** Cash on Delivery"
            )

        if page_count > 0:
            p = products[0]
            msg = f"Found **{p['name']}** 🎯\n\n"
            if p.get("price", 0) > 0:
                msg += f"💰 Price: ${p['price']:.2f}\n"
            msg += "\n⚠️ Please log in to place an order."
            return msg

    # ── No products found ──
    if page_count == 0:
        search = (
            entities.product_name or entities.category_name
            or entities.visual or entities.finish
            or entities.color_tone or entities.search_term
            or "your criteria"
        )
        return (
            f"I couldn't find any products matching **{search}**. 😕\n\n"
            "Try broadening your search or ask me about:\n"
            "• Our tile collections\n"
            "• Available categories\n"
            "• Specific finishes or colors"
        )

    # ── Variation results ──
    if intent in (Intent.PRODUCT_SEARCH, Intent.PRODUCT_DETAIL, Intent.PRODUCT_VARIATIONS) \
            and entities.product_id and page_count > 0:
        parent = products[0]
        variations = [p for p in products[1:] if p.get("type") == "variation"]
        has_attributes = any([
            entities.finish, entities.color_tone, entities.tile_size,
            entities.thickness, entities.visual, entities.origin,
        ])

        if intent == Intent.PRODUCT_VARIATIONS or (not has_attributes):
            msg = f"🎯 **{parent['name']}**\n"
            if parent.get("price", 0) > 0:
                msg += f"💰 Starting from ${parent['price']:.2f}\n"
            if parent.get("short_description"):
                msg += f"\n{parent['short_description']}\n"
            if variations:
                msg += f"\n**Available variations ({len(variations)}):**\n"
                for v in variations[:10]:
                    label = v.get("variation_label") or v.get("name", "")
                    price_str = f"${v['price']:.2f}" if v.get("price", 0) > 0 else ""
                    stock = "✅" if v.get("in_stock") else "❌"
                    line = f"  {stock} {label}"
                    if price_str:
                        line += f" — {price_str}"
                    msg += line + "\n"
                if len(variations) > 10:
                    msg += f"  ...and {len(variations) - 10} more variations.\n"
            elif parent.get("attributes"):
                msg += "\n**Available options:**\n"
                for attr in parent["attributes"][:4]:
                    opts = ", ".join(attr["options"][:6])
                    msg += f"  • **{attr['name']}:** {opts}\n"
            return msg

        else:
            attr_desc = " / ".join(filter(None, [
                entities.finish, entities.tile_size,
                entities.color_tone, entities.thickness,
            ]))
            if not variations:
                return (
                    f"I found **{parent['name']}** but couldn't find variations matching "
                    f"**{attr_desc}**. 😕\n\n"
                    f"Try asking: *'What variations does {parent['name']} come in?'*"
                )
            msg = f"🎯 **{parent['name']}** — {attr_desc}\n\n"
            msg += f"Found **{len(variations)}** matching variation(s):\n\n"
            for v in variations[:10]:
                label = v.get("variation_label") or v.get("name", "")
                price_str = f"${v['price']:.2f}" if v.get("price", 0) > 0 else ""
                stock = "✅ In stock" if v.get("in_stock") else "❌ Out of stock"
                line = f"• **{label}** — {stock}"
                if price_str:
                    line = f"• **{label}** — {price_str} — {stock}"
                msg += line + "\n"
            if len(variations) > 10:
                msg += f"\n...and {len(variations) - 10} more."
            return msg

    # ── Single product ──
    if page_count == 1:
        p = products[0]
        msg = f"I found the perfect match! 🎯\n\n**{p['name']}**\n"
        if p.get("price", 0) > 0:
            msg += f"💰 Price: ${p['price']:.2f}\n"
        if p.get("on_sale") and p.get("sale_price") and float(p.get("sale_price", 0)) > 0:
            msg += f"🏷️ Sale Price: ${p['sale_price']:.2f}\n"
        if p.get("short_description"):
            msg += f"\n{p['short_description']}\n"
        if p.get("attributes"):
            for attr in p["attributes"][:3]:
                opts = ", ".join(attr["options"][:5])
                msg += f"• **{attr['name']}:** {opts}\n"
        return msg

    # ── Multiple products ──
    msg = ""

    if intent == Intent.CATEGORY_BROWSE:
        # ── FIX: Detect unresolved qualifiers the API couldn't filter on ──
        qualifier = _get_unresolved_category_qualifier(entities)
        if qualifier:
            msg += (
                f"We don't have a specific **{qualifier} {entities.category_name}** "
                f"sub-category, but here are all **{count}** products in "
                f"**{entities.category_name}** — many of these work great for "
                f"**{qualifier.lower()}** use! 📂\n\n"
            )
        else:
            msg += f"Here are **{count}** products in the **{entities.category_name}** category! 📂\n\n"
    elif intent == Intent.PRODUCT_BY_VISUAL:
        msg += f"Found **{count}** products with **{entities.visual}** look! \n\n"
    elif intent == Intent.FILTER_BY_FINISH:
        msg += f"Here are **{count}** products with **{entities.finish}** finish! \n\n"
    elif intent == Intent.FILTER_BY_COLOR:
        msg += f"Found **{count}** products in **{entities.color_tone}** tones! \n\n"
    elif intent == Intent.PRODUCT_SEARCH:
        msg += f"Found **{count}** products matching your search! \n\n"
    elif intent == Intent.CHIP_CARD:
        msg += f"Here are **{count}** chip cards available! \n\n"
    elif intent == Intent.MOSAIC_PRODUCTS:
        msg += f"Found **{count}** mosaic products! \n\n"
    elif intent == Intent.CATEGORY_LIST:
        msg += f"Here are our product categories! \n\n"
        for p in products[:MAX_DISPLAYED_ITEMS]:
            count_val = p.get('count', 0)
            count_str = f"({count_val} products)" if count_val > 0 else ""
            msg += f"• **{p['name']}** {count_str}\n"
        if len(products) > MAX_DISPLAYED_ITEMS:
            msg += f"\n...and {len(products) - MAX_DISPLAYED_ITEMS} more categories."
        return msg
    else:
        msg += f"Here are **{count}** products I found! 🛍️\n\n"

    for p in products[:5]:
        if p.get("price", 0) > 0:
            msg += f"• **{p['name']}** — ${p['price']:.2f}\n"
        else:
            msg += f"• **{p['name']}**\n"

    if count > 5:
        msg += f"\n...and {count - 5} more products."

    return msg


def _get_unresolved_category_qualifier(entities: ExtractedEntities) -> str:
    """
    Check if entities contain filter attributes (like application) that
    the API couldn't resolve, so we can mention them in the response.
    """
    if entities.application:
        return entities.application.title()
    return ""


def generate_suggestions(
    intent: Intent,
    entities: ExtractedEntities,
    products: List[dict],
) -> List[str]:
    """Generate follow-up suggestions based on context."""
    suggestions = []

    if intent == Intent.GREETING:
        suggestions = [
            "Show me all products",
            "What categories do you have?",
            "Show me what's on sale",
        ]
    elif intent in (Intent.PRODUCT_SEARCH, Intent.PRODUCT_LIST, Intent.CATEGORY_BROWSE):
        if products:
            suggestions.append("Show me what's on sale")
            suggestions.append("Show me quick ship products")
            suggestions.append("What categories do you have?")
    elif intent == Intent.PRODUCT_DETAIL:
        if products:
            suggestions.append(f"Order {products[0]['name']}")
            suggestions.append("Show me similar products")
    elif intent in (Intent.FILTER_BY_FINISH, Intent.FILTER_BY_COLOR, Intent.FILTER_BY_SIZE):
        suggestions.append("Show me all products")
        suggestions.append("What finishes are available?")
    elif intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY):
        suggestions.append("Reorder my last order")
        suggestions.append("Show me products")
    elif intent == Intent.CATEGORY_LIST:
        if products:
            for cat in products[:3]:
                suggestions.append(f"Show me {cat['name']}")

    return suggestions


def build_filters(
    intent: Intent,
    entities: ExtractedEntities,
    api_calls: List[WooAPICall],
) -> dict:
    """Build the filters_applied dict for the response."""
    filters = {
        "search": None,
        "category": None,
        "tag": None,
        "on_sale": None,
        "min_price": None,
        "max_price": None,
        "orderby": None,
        "order": None,
    }

    for call in api_calls:
        p = call.params
        if "search" in p:
            filters["search"] = p["search"]
        if "category" in p:
            filters["category"] = p["category"]
        if "tag" in p:
            filters["tag"] = p["tag"]
        if "on_sale" in p:
            filters["on_sale"] = p["on_sale"]
        if "min_price" in p:
            filters["min_price"] = p["min_price"]
        if "max_price" in p:
            filters["max_price"] = p["max_price"]
        if "orderby" in p:
            filters["orderby"] = p["orderby"]
        if "order" in p:
            filters["order"] = p["order"]

    return filters


# ── Label mapping for API response ──
INTENT_LABELS = {
    Intent.PRODUCT_SEARCH: "search",
    Intent.PRODUCT_LIST: "browse",
    Intent.PRODUCT_DETAIL: "detail",
    Intent.CATEGORY_BROWSE: "category",
    Intent.CATEGORY_LIST: "categories",
    Intent.PRODUCT_BY_COLLECTION: "collection",
    Intent.PRODUCT_BY_TAG: "tag",
    Intent.PRODUCT_QUICK_SHIP: "quick_ship",
    Intent.PRODUCT_BY_VISUAL: "visual",
    Intent.FILTER_BY_FINISH: "filter",
    Intent.FILTER_BY_SIZE: "filter",
    Intent.FILTER_BY_COLOR: "filter",
    Intent.FILTER_BY_APPLICATION: "filter",
    Intent.FILTER_BY_MATERIAL: "filter",
    Intent.FILTER_BY_ORIGIN: "filter",
    Intent.SIZE_LIST: "sizes",
    Intent.PRODUCT_TYPES: "types",
    Intent.PRODUCT_CATALOG: "catalog",
    Intent.RELATED_PRODUCTS: "related",
    Intent.MOSAIC_PRODUCTS: "mosaic",
    Intent.TRIM_PRODUCTS: "trim",
    Intent.CHIP_CARD: "chip_card",
    Intent.SAMPLE_REQUEST: "sample",
    Intent.PRODUCT_VARIATIONS: "variations",
    Intent.PRODUCT_BY_ORIGIN: "origin",
    Intent.LAST_ORDER: "order",
    Intent.ORDER_HISTORY: "order_history",
    Intent.REORDER: "reorder",
    Intent.ORDER_ITEM: "order",
    Intent.QUICK_ORDER: "order",
    Intent.PLACE_ORDER: "order",
    Intent.ORDER_TRACKING: "order",
    Intent.ORDER_STATUS: "order",
    Intent.DISCOUNT_INQUIRY: "discount",
    Intent.CLEARANCE_PRODUCTS: "clearance",
    Intent.BULK_DISCOUNT: "bulk",
    Intent.PROMOTIONS: "promotions",
    Intent.COUPON_INQUIRY: "coupon",
    Intent.SAVE_FOR_LATER: "wishlist",
    Intent.GREETING: "greeting",
}


def _resolve_user_placeholders(api_calls: List[WooAPICall], customer_id: int):
    """Replace CURRENT_USER_ID / CURRENT_USER placeholders."""
    for call in api_calls:
        for key, val in list(call.params.items()):
            if isinstance(val, str) and val in USER_PLACEHOLDERS:
                call.params[key] = str(customer_id)


def _format_order_date(date_created: str) -> str:
    """Format an ISO date string to a readable format."""
    if not date_created:
        return "Unknown date"
    try:
        dt = datetime.fromisoformat(date_created.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y at %I:%M %p")
    except (ValueError, TypeError):
        return date_created


def _format_order_history_message(orders: List[dict]) -> str:
    """Format multiple orders into a readable message."""
    if not orders:
        return "No orders found."

    msg = f"📦 **Your Recent Orders** ({len(orders)} orders)\n\n"
    for order in orders[:10]:
        order_number = order.get("number", str(order.get("id", "")))
        status = order.get("status", "unknown").title()
        total = order.get("total", "0")
        date_created = order.get("date_created", "")
        msg += f"• **#{order_number}** — {status} — ${total} — {_format_order_date(date_created)}\n"
    if len(orders) > 10:
        msg += f"\n...and {len(orders) - 10} more orders."
    return msg