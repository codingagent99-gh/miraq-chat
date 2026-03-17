"""
Response generation module for bot messages, suggestions, and formatting.
"""

from typing import List, Optional
from datetime import datetime

from models import Intent, ExtractedEntities, WooAPICall
from app_config import MAX_DISPLAYED_ITEMS, USER_PLACEHOLDERS, DEFAULT_PAYMENT_METHOD_TITLE, get_currency_symbol


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
    CS = get_currency_symbol()

    if order_data is None:
        order_data = []

    page_count = len(products)
    # Display count: use total_items (real total across all pages) when available,
    # otherwise fall back to the number of products on this page.
    count = total_items if total_items is not None else page_count

    # ── Customer update intent ──
    if intent == Intent.UPDATE_CUSTOMER:
        update_ok = True
        if order_data and isinstance(order_data[0], dict):
            update_ok = order_data[0].get("success", True)
        if update_ok:
            return "Your account details have been updated successfully."
        else:
            return "Sorry, I wasn't able to update your details. Please try again or contact support."

    # ── Greeting intent ──
    if intent == Intent.GREETING:
        return (
            "👋 Hello! Welcome to our store! How can I help you today?\n\n"
            "You can ask me about our products, browse categories, check your orders, or search for something specific."
        )

    # ── Order-specific handling ──
    if intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY, Intent.REORDER, Intent.ORDER_STATUS, Intent.ORDER_TRACKING):
        # Single order detail — ORDER_STATUS/TRACKING with one order, or LAST_ORDER
        if intent in (Intent.ORDER_STATUS, Intent.ORDER_TRACKING) and order_data:
            return format_order_detail(order_data[0])
        elif intent == Intent.ORDER_HISTORY and order_data:
            return _format_order_history_message(order_data, date_after=getattr(entities, "date_after", None))
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
            msg += f"**Total:** {CS}{total}\n\n"

            line_items = order.get("line_items", [])
            if line_items:
                msg += "**Items:**\n"
                for item in line_items:
                    qty = item.get("quantity", 0)
                    name = item.get("name") or "Unknown Item"
                    item_total = item.get("total", "0")
                    msg += f"  • {name} × {qty} — {CS}{item_total}\n"

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
            # date_after present → logged-in user with genuinely no orders in that period
            if getattr(entities, 'date_after', None):
                period = _describe_date_period(entities.date_after)
                if intent == Intent.ORDER_HISTORY:
                    return (
                        f"📭 You don't have any orders from {period}.\n\n"
                        "Would you like to see your full order history instead?"
                    )
                elif intent == Intent.LAST_ORDER:
                    return (
                        f"📭 No orders found from {period}.\n\n"
                        "Would you like to see your most recent order overall?"
                    )
            # No date filter → user likely not logged in
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
                f"**Total:** {CS}{float(total):.2f}\n"
                f"**Payment Mode:** {DEFAULT_PAYMENT_METHOD_TITLE}"
            )

        if page_count > 0:
            p = products[0]
            msg = f"Found **{p['name']}** 🎯\n\n"
            if p.get("price", 0) > 0:
                msg += f"💰 Price: {CS}{p['price']:.2f}\n"
            msg += "\n⚠️ Please log in to place an order."
            return msg

    # ── Product attribute info ──
    if intent == Intent.PRODUCT_ATTRIBUTE_INFO:
        if page_count > 0:
            return _generate_attribute_info_message(products, entities)
        product_name = entities.product_name or "that product"
        target = getattr(entities, 'target_attribute', None) or 'options'
        return (
            f"I couldn't find **{product_name}** in our catalog to check its {target} options. "
            f"Try searching for it by name, or ask: *'Show me {product_name} products'*"
        )

    # ── Sample request ──
    if intent == Intent.SAMPLE_REQUEST:
        product_name = entities.product_name or "that product"

        # Extract requested sample size from entities (e.g. "3x3" from user text)
        requested_size = entities.attributes.get("sample size", "")

        if page_count == 0:
            return (
                f"I couldn't find **{product_name}** in our catalog. 😕\n\n"
                "Try searching for it by name, or browse our product categories."
            )

        # Read the pa_sample-size attribute from the product(s)
        available_sample_sizes = []
        target_product = None
        for p in products:
            for attr in p.get("attributes", []):
                if "sample" in attr.get("name", "").lower() and "size" in attr.get("name", "").lower():
                    opts = attr.get("options", [])
                    if opts:
                        available_sample_sizes = opts
                        target_product = p
                        break
            if available_sample_sizes:
                break

        if not available_sample_sizes:
            return (
                f"I found **{products[0].get('name', product_name)}**, but it doesn't appear "
                f"to have sample sizes listed. 😕\n\n"
                f"You can ask: *'What sizes does {products[0].get('name', product_name)} come in?'*"
            )

        p_name = target_product.get("name", product_name) if target_product else product_name

        # If user asked about a specific size, check if it's available
        if requested_size:
            # Normalize for comparison: strip quotes, spaces
            import re as _re
            req_clean = _re.sub(r'["\'\s]', '', requested_size).lower()
            match_found = any(
                _re.sub(r'["\'\s]', '', s).lower() == req_clean
                for s in available_sample_sizes
            )
            if match_found:
                # Format the requested size nicely from the matched option
                matched_display = next(
                    (s for s in available_sample_sizes
                     if _re.sub(r'["\'\s]', '', s).lower() == req_clean),
                    requested_size
                )
                msg = f"✅ Yes! **{p_name}** is available in a **{matched_display}** sample size.\n\n"
                msg += f"**All available sample sizes:**\n"
                for s in available_sample_sizes:
                    marker = " ✅" if _re.sub(r'["\'\s]', '', s).lower() == req_clean else ""
                    msg += f"  • {s}{marker}\n"
                return msg
            else:
                msg = f"❌ Sorry, **{p_name}** does not come in a **{requested_size}** sample size.\n\n"
                msg += f"**Available sample sizes:**\n"
                for s in available_sample_sizes:
                    msg += f"  • {s}\n"
                return msg

        # No specific size requested — list all available sample sizes
        msg = f"📐 **{p_name}** is available in the following sample sizes:\n\n"
        for s in available_sample_sizes:
            msg += f"  • {s}\n"
        return msg
    
    # ── No products found ──
    if page_count == 0:
        search = (
            entities.product_name or entities.category_name
            or next(iter(entities.attributes.values()), None)
            or entities.search_term
            or "your criteria"
        )
        return (
            f"I couldn't find any products matching **{search}**. 😕\n\n"
            "Try broadening your search or ask me about:\n"
            "• Our product collections\n"
            "• Available categories\n"
            "• Specific finishes or colors"
        )

    # ── Size list for a specific product ──
    if intent == Intent.SIZE_LIST and entities.product_id:
        product_name = entities.product_name or "this product"
        if page_count == 0:
            return (
                f"I couldn't find any size information for **{product_name}**. "
                f"Try asking: *'Show me {product_name} products'*"
            )
        size_map = {}
        for p in products[1:]:
            for attr in p.get("attributes", []):
                attr_name = attr.get("name", "")
                if "size" in attr_name.lower():
                    option = attr.get("option") or attr.get("value", "")
                    if option:
                        size_map.setdefault(attr_name, set()).add(option)

        if not size_map:
            parent = products[0]
            for attr in parent.get("attributes", []):
                attr_name = attr.get("name", "")
                if "size" in attr_name.lower():
                    for opt in attr.get("options", []):
                        size_map.setdefault(attr_name, set()).add(opt)

        if size_map:
            msg = f"📐 **{product_name}** is available in the following sizes:\n\n"
            for attr_name, options in size_map.items():
                sorted_opts = sorted(options)
                msg += f"**{attr_name}:**\n"
                for opt in sorted_opts:
                    msg += f"  • {opt}\n"
            return msg

        return f"I couldn't find specific size options for **{product_name}**."

    # ── Variation results ──
    if intent in (Intent.PRODUCT_SEARCH, Intent.PRODUCT_DETAIL, Intent.PRODUCT_VARIATIONS) and entities.product_id and page_count > 0:
        parent = products[0]
        
        # 1. Extract nested variations from the Custom API if present
        variations = [p for p in products[1:] if p.get("type") == "variation"]
        if not variations and parent.get("variations"):
            variations = parent.get("variations", [])
            
        has_attributes = bool(entities.attributes)

        def _get_var_label(v):
            """Safely build a variation label from either standard or custom API formats."""
            label = v.get("variation_label") or v.get("name", "")
            if not label and v.get("attributes"):
                if isinstance(v["attributes"], dict):
                    label = " / ".join(str(val) for val in v["attributes"].values() if val)
                elif isinstance(v["attributes"], list):
                    label = " / ".join(str(a.get("option", "")) for a in v["attributes"] if a.get("option"))
            return label.strip().title() or f"Variation #{v.get('id', '')}"

        # If we have specific attributes, show the FILTERED specific view!
        if has_attributes:
            attr_desc = " / ".join(filter(None, entities.attributes.values())).title()
            
            if not variations:
                return (
                    f"I found **{parent['name']}** but couldn't find variations matching "
                    f"**{attr_desc}**. 😕\n\n"
                    f"Try asking: *'What variations does {parent['name']} come in?'*"
                )
                
            msg = f"🎯 **{parent['name']}** — {attr_desc}\n\n"
            msg += f"Found **{len(variations)}** matching variation(s):\n\n"
            
            for v in variations[:10]:
                label = _get_var_label(v)
                price_val = float(v.get("price") or 0)
                price_str = f"{CS}{price_val:.2f}" if price_val > 0 else ""
                
                # Custom API doesn't always send in_stock for nested variations, default to True if returned
                stock = "✅ In stock" if v.get("in_stock", True) else "❌ Out of stock"
                
                line = f"• **{label}**"
                if price_str:
                    line += f" — {price_str}"
                line += f" — {stock}"
                msg += line + "\n"
                
            if len(variations) > 10:
                msg += f"\n...and {len(variations) - 10} more."
            return msg

        # Otherwise, generic product variation dump
        else:
            msg = f"🎯 **{parent['name']}**\n"
            if parent.get("price", 0) > 0:
                msg += f"💰 Starting from {CS}{parent['price']:.2f}\n"
            if parent.get("short_description"):
                msg += f"\n{parent['short_description']}\n"
                
            if variations:
                msg += f"\n**Available variations ({len(variations)}):**\n"
                for v in variations[:10]:
                    label = _get_var_label(v)
                    price_val = float(v.get("price") or 0)
                    price_str = f"{CS}{price_val:.2f}" if price_val > 0 else ""
                    stock = "✅" if v.get("in_stock", True) else "❌"
                    
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

    # ── Single product ──
    if page_count == 1:
        p = products[0]
        msg = f"I found the perfect match! 🎯\n\n**{p['name']}**\n"
        if p.get("price", 0) > 0:
            msg += f"💰 Price: {CS}{p['price']:.2f}\n"
        if p.get("on_sale") and p.get("sale_price") and float(p.get("sale_price", 0)) > 0:
            msg += f"🏷️ Sale Price: {CS}{p['sale_price']:.2f}\n"
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
        qualifier = _get_unresolved_category_qualifier(entities)
        if not entities.category_name:
            msg += f"Here are our **{count}** product categories! 📂\n\n"
        elif qualifier:
            msg += (
                f"We don't have a specific **{qualifier} {entities.category_name}** "
                f"sub-category, but here are all **{count}** products in "
                f"**{entities.category_name}** — many of these work great for "
                f"**{qualifier.lower()}** use! 📂\n\n"
            )
        else:
            msg += f"Here are **{count}** products in the **{entities.category_name}** category! 📂\n\n"
    
    elif intent == Intent.PRODUCT_BY_VISUAL:
        msg += f"Found **{count}** products with **{entities.attributes.get('visual', '')}** look! 🎨\n\n"
    elif intent == Intent.FILTER_BY_FINISH:
        msg += f"Here are **{count}** products with **{entities.attributes.get('finish', '')}** finish! ✨\n\n"
    elif intent == Intent.FILTER_BY_COLOR:
        msg += f"Found **{count}** products in **{entities.attributes.get('colors', '')}** tones! 🎨\n\n"
    elif intent == Intent.FILTER_BY_ATTRIBUTE:
        attr_desc = " · ".join(
            f"**{v}** {k}" for k, v in entities.attributes.items() if v
        )
        category_desc = f" in **{entities.category_name}**" if entities.category_name else ""
        msg += f"Found **{count}** products{category_desc} matching {attr_desc}! 🔍\n\n"
    elif intent == Intent.PRODUCT_SEARCH:
        msg += f"Found **{count}** products matching your search! 🔍\n\n"
    elif intent == Intent.CATEGORY_LIST:
        msg += f"Here are our product categories! 📂\n\n"
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
            msg += f"• **{p['name']}** — {CS}{p['price']:.2f}\n"
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
    app = entities.attributes.get("application", "")
    return app.title() if app else ""


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
    elif intent == Intent.PRODUCT_ATTRIBUTE_INFO:
        if products:
            p_name = products[0].get('name', '')
            if p_name:
                suggestions.append(f"Order {p_name}")
                suggestions.append(f"Show all {p_name} products")
        suggestions.append("What categories do you have?")
    elif intent in (Intent.FILTER_BY_FINISH, Intent.FILTER_BY_COLOR, Intent.FILTER_BY_SIZE):
        suggestions.append("Show me all products")
        suggestions.append("What finishes are available?")
        
    elif intent == Intent.SAMPLE_REQUEST:
        if products:
            p_name = products[0].get('name', '')
            if p_name:
                suggestions.append(f"Order {p_name}")
                suggestions.append(f"What sizes does {p_name} come in?")
        suggestions.append("Show me all products")
    elif intent == Intent.UPDATE_CUSTOMER:
        return ["Show me products on sale", "What categories do you have?", "Show me new releases"]
    elif intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY):
        suggestions.append("Reorder my last order")
        suggestions.append("Show me products")
    elif intent == Intent.CATEGORY_LIST:
        if products:
            for cat in products[:3]:
                suggestions.append(f"Show me {cat['name']}")

    return suggestions

# ── Label mapping for API response ──
INTENT_LABELS = {
    Intent.PRODUCT_SEARCH: "search",
    Intent.PRODUCT_LIST: "browse",
    Intent.PRODUCT_DETAIL: "detail",
    Intent.PRODUCT_ATTRIBUTE_INFO: "attribute_info",
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
    Intent.FILTER_BY_ATTRIBUTE: "filter",
    Intent.SIZE_LIST: "sizes",
    Intent.PRODUCT_TYPES: "types",
    Intent.PRODUCT_CATALOG: "catalog",
    Intent.RELATED_PRODUCTS: "related",
    # Intent.MOSAIC_PRODUCTS: "mosaic",
    # Intent.TRIM_PRODUCTS: "trim",
    # Intent.CHIP_CARD: "chip_card",
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
    Intent.UPDATE_CUSTOMER: "update_customer",
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


def _describe_date_period(date_after: str) -> str:
    """Convert a date_after ISO string into a human-readable period description."""
    try:
        from datetime import timezone, timedelta
        dt = datetime.fromisoformat(date_after.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        days = delta.days
        if days <= 1:
            return "the last day"
        elif days <= 14:
            return f"the last {days} days"
        elif days <= 60:
            weeks = round(days / 7)
            return f"the last {weeks} week{'s' if weeks != 1 else ''}"
        elif days <= 400:
            months = round(days / 30)
            return f"the last {months} month{'s' if months != 1 else ''}"
        else:
            years = round(days / 365)
            return f"the last {years} year{'s' if years != 1 else ''}"
    except Exception:
        return "that period"


def _format_order_history_message(orders: List[dict], date_after: str = None) -> str:
    """Return a short header — the frontend renders order cards from the structured orders array."""
    if not orders:
        return "No orders found."
    count = len(orders)
    verb = "is" if count == 1 else "are"
    order_word = "order" if count == 1 else "orders"
    if date_after:
        period = _describe_date_period(date_after)
        return f"📦 Here {verb} your {count} {order_word} from {period}. Tap any to see full details."
    return f"📦 Here {verb} your {count} most recent {order_word}. Tap any to see full details."


def _generate_attribute_info_message(products: List[dict], entities: ExtractedEntities) -> str:
    """
    Build a focused prose answer listing the available options for a specific
    attribute (e.g. size, finish, color) of a named product.
    """
    target_attr = (getattr(entities, 'target_attribute', None) or 'options').lower()

    parent = next((p for p in products if p.get('type') != 'variation'), products[0])
    product_name = parent.get('name', 'This product')

    attrs = parent.get('attributes', [])
    all_attrs = parent.get('raw_attributes', attrs)
    
    matched_attr = None
    for attr in all_attrs:
        attr_name_lower = attr.get('name', '').lower()
        if target_attr in attr_name_lower or attr_name_lower in target_attr:
            matched_attr = attr
            break

    variations = [p for p in products if p.get('type') == 'variation']

    if matched_attr:
        options = matched_attr.get('options', [])
        attr_display_name = matched_attr.get('name', target_attr.title())

        if not options:
            return (
                f"I found **{product_name}** but the {attr_display_name.lower()} "
                f"options aren't listed in our catalog. "
                f"Try asking: *'What variations does {product_name} come in?'*"
            )

        opts_formatted = ", ".join(f"**{o}**" for o in options)
        msg = f"📐 **{product_name}** is available in the following {attr_display_name.lower()}:\n\n{opts_formatted}"

        if variations:
            in_stock_opts = set()
            for v in variations:
                stock_ok = v.get('in_stock') or v.get('stock_status') == 'instock'
                if stock_ok:
                    for a in v.get('attributes', []):
                        if target_attr in a.get('name', '').lower():
                            opt = a.get('option', '')
                            if opt:
                                in_stock_opts.add(opt)
            if in_stock_opts and 0 < len(in_stock_opts) < len(options):
                msg += f"\n\n✅ Currently in stock: {', '.join(sorted(in_stock_opts))}"

        return msg

    if attrs:
        msg = f"📐 Here are the available options for **{product_name}**:\n\n"
        for attr in attrs[:6]:
            opts = ', '.join(attr.get('options', [])[:8])
            msg += f"• **{attr.get('name', '')}:** {opts}\n"
        return msg

    return (
        f"I found **{product_name}** but couldn't retrieve its {target_attr} options from the catalog. "
        f"Try asking: *'What variations does {product_name} come in?'*"
    )


def format_order_detail(order: dict) -> str:
    """Format a single order into a rich detail message."""
    CS = get_currency_symbol()

    if not order:
        return "Order details not available."

    order_number = order.get("number", str(order.get("id", "N/A")))
    status = order.get("status", "unknown").title()
    # Prefer currency_symbol from WooCommerce order response, fall back to configured symbol
    currency = order.get("currency_symbol") or CS
    total = order.get("total", "0")
    subtotal = order.get("subtotal", "")
    date_created = order.get("date_created", "")
    payment_method = order.get("payment_method_title", "N/A")

    STATUS_EMOJI = {
        "pending": "⏳", "processing": "🔄", "on-hold": "⏸️",
        "completed": "✅", "cancelled": "❌", "refunded": "↩️",
        "failed": "❗", "trash": "🗑️",
    }
    status_emoji = STATUS_EMOJI.get(order.get("status", "").lower(), "📦")

    msg = f"{status_emoji} **Order #{order_number}**\n\n"
    msg += f"**Status:** {status}\n"
    msg += f"**Date:** {_format_order_date(date_created)}\n"
    msg += f"**Payment:** {payment_method}\n"

    # Shipping address
    shipping = order.get("shipping", {})
    if shipping and (shipping.get("address_1") or shipping.get("city")):
        addr_parts = [p for p in [
            shipping.get("first_name", "") + " " + shipping.get("last_name", ""),
            shipping.get("address_1", ""),
            shipping.get("address_2", ""),
            shipping.get("city", ""),
            shipping.get("state", ""),
            shipping.get("postcode", ""),
            shipping.get("country", ""),
        ] if p and p.strip()]
        msg += f"**Ships to:** {', '.join(addr_parts)}\n"

    # Line items
    line_items = order.get("line_items", [])
    if line_items:
        msg += f"\n**Items ({len(line_items)}):**\n"
        for item in line_items:
            name = item.get("name") or "Unknown Item"
            qty = item.get("quantity", 1)
            item_total = item.get("total", "0")
            sku = item.get("sku", "")
            msg += f"  • {name}"
            if sku:
                msg += f" _(SKU: {sku})_"
            msg += f" × {qty} — {currency}{item_total}\n"

    # Totals
    msg += f"\n**Order Total:** {currency}{total}"
    shipping_total = order.get("shipping_total", "0")
    if float(shipping_total or 0) > 0:
        msg += f"\n**Shipping:** {currency}{shipping_total}"

    return msg