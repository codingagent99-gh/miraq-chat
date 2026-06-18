"""
Response generation module for bot messages, suggestions, and formatting.
"""

from typing import List, Optional
from datetime import datetime

from models import Intent, ExtractedEntities, WooAPICall
from app_config import MAX_DISPLAYED_ITEMS, USER_PLACEHOLDERS, DEFAULT_PAYMENT_METHOD_TITLE, get_currency_symbol
from store_registry import get_store_loader

_SECONDARY_ATTRIBUTE_SUFFIX = " 2"


def _attribute_key_candidates(attr_key: str) -> list[str]:
    # Some parser flows emit synthetic fallback keys like "colors 2".
    key = str(attr_key or "").replace("attribute_", "").replace(_SECONDARY_ATTRIBUTE_SUFFIX, "").strip().lower()
    if key.startswith("pa_"):
        key = key[3:]
    candidates = [key]
    if " " in key:
        candidates.append(key.replace(" ", "-"))
    if "-" in key:
        candidates.append(key.replace("-", " "))
    return [c for c in dict.fromkeys(candidates) if c]

def _resolve_attribute_taxonomy(attr_key: str) -> str:
    try:
        l = get_store_loader()
    except Exception:
        l = None
    if l:
        for candidate in _attribute_key_candidates(attr_key):
            attr = l.resolve_attribute(candidate)
            if attr:
                return attr.backend_ref.get("taxonomy", "")
    return ""

def _resolve_attribute_label(attr_key: str) -> str:
    try:
        loader = get_store_loader()
    except Exception:
        loader = None

    if loader:
        for candidate in _attribute_key_candidates(attr_key):
            attr = loader.resolve_attribute(candidate)
            if attr and attr.label:
                return attr.label

    return str(attr_key or "").replace(_SECONDARY_ATTRIBUTE_SUFFIX, "").replace("-", " ").title()


def _resolve_attribute_term_name(attr_key: str, term_value) -> str:
    raw_value = str(term_value or "")
    if not raw_value:
        return ""

    try:
        loader = get_store_loader()
    except Exception:
        loader = None

    if loader:
        for candidate in _attribute_key_candidates(attr_key):
            term = loader.resolve_attribute_term(candidate, raw_value)
            if term and term.name:
                return term.name

    return raw_value.replace("-", " ").title()


def _resolve_tag_name(tag_key: str) -> str:
    try:
        loader = get_store_loader()
    except Exception:
        loader = None

    if loader and tag_key:
        tag = loader.resolve_tag(str(tag_key).lower().strip())
        if tag and tag.name:
            return tag.name
    return str(tag_key or "").replace("-", " ").title()


def _resolve_category_name(category_key: str) -> str:
    try:
        loader = get_store_loader()
    except Exception:
        loader = None

    if loader and category_key:
        category = loader.resolve_category(str(category_key).lower().strip())
        if category and category.name:
            return category.name
    return str(category_key or "").replace("-", " ").title()


def _build_attribute_value_summary(attributes: dict) -> str:
    values = []
    for attr_key, attr_val in (attributes or {}).items():
        if attr_val:
            values.append(_resolve_attribute_term_name(attr_key, attr_val))
    return " / ".join(values)

def _build_search_context_string(entities: ExtractedEntities,  or_pair_breakdown: dict = None) -> str:
    desc_parts = []

    consumed_cat_slugs, consumed_tag_slugs, consumed_attr = set(), set(), set()

    for group in (or_pair_breakdown or []):
        head_label, head_value, suffix_bits = None, None, []
        branch_summaries = []
        for branch in group:
            terms = branch["terms"]
            if branch["role"] == "category":
                label = "Category"
                value = ", ".join(_resolve_category_name(t) for t in terms)
                consumed_cat_slugs.update(t.lower() for t in terms)
            elif branch["role"] == "tag":
                label = "Tag"
                value = ", ".join(_resolve_tag_name(t) for t in terms)
                consumed_tag_slugs.update(t.lower() for t in terms)
            else:
                attr_key = branch["taxonomy"].removeprefix("pa_")
                label = _resolve_attribute_label(attr_key)
                value = ", ".join(_resolve_attribute_term_name(attr_key, t) for t in terms)
                consumed_attr.update((branch["taxonomy"], t.lower()) for t in terms)

            if head_label is None:
                head_label, head_value = label, value
            branch_summaries.append((label.lower(), value))
            suffix_bits.append(f"{label}: {branch['count']}")

        if len(group) > 1:
            parts = ", ".join(f"{lbl} {val}" for lbl, val in branch_summaries)
            note = f" — Note: Same product may have {parts}; in such cases it is counted once only"
        else:
            note = ""
        desc_parts.append(f"**{head_value}** *({' • '.join(suffix_bits)}{note})*")

    if getattr(entities, 'product_name', None):
        desc_parts.append(f"Product: **{entities.product_name}**")

    cat_name = getattr(entities, 'category_name', None)
    cat_slugs = getattr(entities, 'target_category_slugs', None) or set()
    if not (cat_slugs and all(s.lower() in consumed_cat_slugs for s in cat_slugs)):
        if not cat_name and cat_slugs:
            cat_name = ", ".join(_resolve_category_name(s) for s in sorted(cat_slugs))
        if cat_name:
            desc_parts.append(f"Category: **{cat_name}**")

    if getattr(entities, 'attributes', None):
        for attr_name, attr_val in entities.attributes.items():
            if not attr_val:
                continue
            taxonomy = _resolve_attribute_taxonomy(attr_name)
            val_slug = str(attr_val).lower().replace(" ", "-")
            if taxonomy and (taxonomy, val_slug) in consumed_attr:
                continue
            clean_name = _resolve_attribute_label(attr_name)
            clean_val = _resolve_attribute_term_name(attr_name, attr_val)
            desc_parts.append(f"{clean_name}: **{clean_val}**")
            
    if getattr(entities, 'tag_slugs', None):
        remaining_tags = [t for t in entities.tag_slugs if t.lower() not in consumed_tag_slugs]
        if remaining_tags:
            clean_tags = [_resolve_tag_name(t) for t in remaining_tags]
            if len(clean_tags) == 1:
                tag_str = clean_tags[0]
            else:
                tag_str = ", ".join(clean_tags[:-1]) + " & " + clean_tags[-1]
            desc_parts.append(f"Tag: **{tag_str}**")

    # EXCLUSIONS
    if getattr(entities, 'excluded_tags', None):
        for tag in entities.excluded_tags:
            clean_tag = _resolve_tag_name(tag)
            desc_parts.append(f"Excluding Tag: **{clean_tag}**")
            
    if getattr(entities, 'excluded_categories', None):
        for cat in entities.excluded_categories:
            clean_cat = _resolve_category_name(cat)
            desc_parts.append(f"Excluding Category: **{clean_cat}**")
            
    if getattr(entities, 'excluded_attributes', None):
        for attr_name, attr_vals in entities.excluded_attributes.items():
            clean_name = _resolve_attribute_label(attr_name)
            for val in attr_vals:
                clean_val = _resolve_attribute_term_name(attr_name, val)
                desc_parts.append(f"Excluding {clean_name}: **{clean_val}**")
            
    return " · ".join(desc_parts)

def generate_bot_message(
    intent: Intent,
    entities: ExtractedEntities,
    products: List[dict],
    confidence: float,
    order_data: List[dict] = None,
    total_items: Optional[int] = None,
    page: int = 1,
    customer_id=None,
    or_pair_breakdown: Optional[dict] = None,
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
                    )
                elif intent == Intent.LAST_ORDER:
                    return (
                        f"📭 No orders found from {period}.\n\n"
                    )
            # No date filter — distinguish logged-in-no-orders vs not logged in
            if customer_id:
                if intent == Intent.LAST_ORDER:
                    return (
                        "It looks like you haven't placed any orders yet! 🛍️\n\n"
                        "Browse our products and place your first order."
                    )
                elif intent == Intent.ORDER_HISTORY:
                    return (
                        "You don't have any orders yet! 🛍️\n\n"
                        "Browse our products and place your first order."
                    )
                elif intent == Intent.REORDER:
                    return (
                        "You don't have any previous orders to reorder from yet! 🛍️\n\n"
                        "Browse our products and place your first order."
                    )
            else:
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

    # ── No products found ──
    if page_count == 0:
        if intent == Intent.RELATED_PRODUCTS:
            target = entities.product_name or "that"
            if not entities.product_name:
                return (
                    "I'd love to show you similar items! 🔍\n\n"
                    "Which specific product would you like to find alternatives for?"
                )
            return (
                f"I couldn't find any direct alternatives for **{target}**. 😕\n\n"
                "Try browsing its category to see all our other options!"
            )

        # If they asked for a specific product AND specific attributes (e.g. Ansel in Charcoal)
        if entities.product_name and entities.attributes:
            attr_desc = _build_attribute_value_summary(entities.attributes)
            return (
                f"I couldn't find **{entities.product_name}** matching **{attr_desc}**. 😕\n\n"
                f"Try asking: *'What variations does {entities.product_name} come in?'* to see all available options."
            )
            
        # Standard fallback for general generic searches
        context = _build_search_context_string(entities)
        search_desc = context if context else entities.product_name or entities.category_name or "your criteria"
        return (
            f"I couldn't find any products matching {search_desc}. 😕\n\n"
            "Try broadening your search or ask me about:\n"
            "• Our product collections\n"
            "• Available categories\n"
            "• Specific finishes or colors"
        )

    # ── Variation results ──
    if intent in (Intent.PRODUCT_SEARCH, Intent.PRODUCT_DETAIL, Intent.PRODUCT_VARIATIONS) and entities.product_id and page_count > 0:
        parent = products[0]
        print("products", products)
        
        # 1. Extract nested variations from the Custom API if present
        variations = [p for p in products[1:] if p.get("type") == "variation"]
        if not variations and parent.get("variations"):
            variations = parent.get("variations", [])
            
        has_attributes = bool(entities.attributes)
        has_stock_filter = getattr(entities, 'in_stock', None) is not None
        
        def _get_var_label(v):
            """Safely build a variation label from either standard or custom API formats."""
            label = v.get("variation_label") or v.get("name", "")
            if not label and v.get("attributes"):
                if isinstance(v["attributes"], dict):
                    label = " / ".join(str(val) for val in v["attributes"].values() if val)
                elif isinstance(v["attributes"], list):
                    label = " / ".join(str(a.get("option", "")) for a in v["attributes"] if a.get("option"))
            return label.strip().title() or f"Variation #{v.get('id', '')}"

        # Filter variations natively if stock status was requested
        if has_stock_filter:
            variations = [
                v for v in variations 
                if v.get("in_stock", v.get("stock_status") == "instock") == entities.in_stock
            ]

        # If we have specific attributes or stock filters, show the FILTERED specific view!
        if has_attributes or has_stock_filter:
            attr_desc = _build_attribute_value_summary(entities.attributes)
            if has_stock_filter:
                stock_desc = "In Stock" if entities.in_stock else "Out of Stock"
                attr_desc = f"{attr_desc} ({stock_desc})" if attr_desc else stock_desc
            
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
                
                stock = "✅ In stock" if v.get("in_stock", v.get("stock_status") == "instock") else "❌ Out of stock"
                
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
                msg += f"\n**Available variations ({len(variations)}):** *(✅ In Stock | ❌ Out of Stock)*\n"
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
        
        # 🚀 FIX: Use the new context builder to explicitly announce tags/categories/attributes
        search_context = _build_search_context_string(entities, or_pair_breakdown)
        match_intro = f"I found the perfect match for {search_context}! 🎯" if search_context else "I found the perfect match! 🎯"

        msg = f"{match_intro}\n\n**{p['name']}**\n"
        
        if p.get("price", 0) > 0:
            msg += f"💰 Price: {CS}{p['price']:.2f}\n"
        if p.get("on_sale") and p.get("sale_price") and float(p.get("sale_price", 0)) > 0:
            msg += f"🏷️ Sale Price: {CS}{p['sale_price']:.2f}\n"
        if p.get("short_description"):
            msg += f"\n{p['short_description']}\n"
            
        # 🚀 FIX: Display more attributes to match the rich visual requested
        if p.get("attributes"):
            for attr in p["attributes"][:8]:  # Show up to 8 attributes instead of just 3
                opts = ", ".join(attr["options"][:10])
                msg += f"• **{attr['name']}:** {opts}\n"
        return msg

    # ── Multiple products ──
    msg = ""
    search_context = _build_search_context_string(entities, or_pair_breakdown)

    if intent == Intent.CATEGORY_BROWSE:
        qualifier = _get_unresolved_category_qualifier(entities)
        if not entities.category_name:
            msg += f"Here are our **{count}** product categories! 📂\n\n"
        elif qualifier:
            msg += (
                f"We don't have a specific **{qualifier} {entities.category_name}** "
                f"sub-category, but here are all **{count}** products for {search_context} — many of these work great for "
                f"**{qualifier.lower()}** use! 📂\n\n"
            )
        else:
            msg += f"Here are **{count}** products for {search_context}! 📂\n\n"
            
    elif intent in (Intent.FILTER_BY_ATTRIBUTE, Intent.PRODUCT_SEARCH, Intent.PRODUCT_BY_TAG, Intent.PRODUCT_BY_COLLECTION):
        if search_context:
            _qualifier = " in total" if or_pair_breakdown else ""
            msg += f"Found **{count}** products{_qualifier} for {search_context}! ✨\n\n"
        else:
            msg += f"Found **{count}** products! 🛍️\n\n"

    elif intent == Intent.RELATED_PRODUCTS:
        p_name = entities.product_name or "this item"
        msg += f"Here are some products similar to **{p_name}** that you might like! ✨\n\n"

    elif intent == Intent.CATEGORY_LIST:
        msg += f"Here are our product categories! 📂\n\n"
        for p in products[:MAX_DISPLAYED_ITEMS]:
            count_val = p.get('count', 0)
            count_str = f"({count_val} products)" if count_val > 0 else ""
            msg += f"• **{p['name']}** {count_str}\n"
        if len(products) > MAX_DISPLAYED_ITEMS:
            msg += f"\n...and {len(products) - MAX_DISPLAYED_ITEMS} more categories."
        return msg

    elif intent == Intent.HISTORICAL_SEARCH:
        p_name = entities.product_name or "that item"
        msg += f"I see you previously ordered **{p_name}**! Here are some options that pair with it:\n\n"
        
    else:
        msg += f"Here are **{count}** products I found! 🛍️\n\n"

    # 1. Determine exactly how many items are on this specific page
    displayed_count = len(products)

    # 2. Render those specific items
    for p in products:
        if p.get("price", 0) > 0:
            msg += f"• **{p['name']}** — {CS}{p['price']:.2f}\n"
        else:
            msg += f"• **{p['name']}**\n"

    # 3. Calculate the exact remainder using the CURRENT page offset!
    if total_items is not None:
        # Since your API requests 4 items per page, we multiply past pages by 4
        items_shown_so_far = ((page - 1) * 4) + displayed_count
        remaining = total_items - items_shown_so_far
        
        if remaining > 0:
            plural = "s" if remaining > 1 else ""
            msg += f"\n...and {remaining} more product{plural}."

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
            "View my orders",
        ]
    elif intent in (Intent.PRODUCT_SEARCH, Intent.PRODUCT_LIST, Intent.CATEGORY_BROWSE):
        if products:
            suggestions.append("Show me all products")
            suggestions.append("View my orders")
    elif intent == Intent.PRODUCT_DETAIL:
        if products:
            suggestions.append(f"Order {products[0]['name']}")
    elif intent == Intent.PRODUCT_ATTRIBUTE_INFO:
        if products:
            p_name = products[0].get('name', '')
            if p_name:
                suggestions.append(f"Order {p_name}")
                suggestions.append(f"Show all {p_name} products")
        suggestions.append("Show me all products")
    
    elif intent == Intent.FILTER_BY_ATTRIBUTE:
        suggestions.append("Show me all products")
        suggestions.append("View my orders")
        
    elif intent == Intent.UPDATE_CUSTOMER:
        return ["Show me all products", "View my orders"]
    elif intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY, Intent.ORDER_STATUS, Intent.ORDER_TRACKING):
        suggestions.append("Reorder my last order")
        suggestions.append("Show me my recent orders")
    elif intent == Intent.CATEGORY_LIST:
        if products:
            for cat in products[:3]:
                suggestions.append(f"Show me {cat['name']}")

    return suggestions

def _resolve_user_placeholders(api_calls: List[WooAPICall], customer_id: int):
    """Replace CURRENT_USER_ID / CURRENT_USER placeholders in params and body."""
    for call in api_calls:
        # Resolve in params (values are always strings in query params)
        for key, val in list(call.params.items()):
            if isinstance(val, str) and val in USER_PLACEHOLDERS:
                call.params[key] = str(customer_id)

        # Resolve in body (customer_id should be int in JSON body)
        if isinstance(call.body, dict):
            for key, val in list(call.body.items()):
                if isinstance(val, str) and val in USER_PLACEHOLDERS:
                    call.body[key] = customer_id


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
    Build a focused answer listing options for ALL requested attributes.
    Handles multi-target queries like 'what sizes and sample sizes are available?'
    """
    # Build target list — prefer the new list, fall back to legacy single string
    raw_targets = getattr(entities, 'target_attributes', None) or []
    if not raw_targets:
        single = getattr(entities, 'target_attribute', None)
        if single:
            raw_targets = [single]
    target_attrs = [a.lower() for a in raw_targets] or ['options']

    parent = next((p for p in products if p.get('type') != 'variation'), products[0])
    product_name = parent.get('name', 'This product')
    attrs = parent.get('attributes', [])
    all_attrs = parent.get('raw_attributes', attrs)
    variations = [p for p in products if p.get('type') == 'variation']

    def _find_attr(target: str, already_claimed: set):
        """Exact match first, then partial — never reuse a claimed attr."""
        # Pass 1: exact
        for attr in all_attrs:
            name_lower = attr.get('name', '').lower()
            if name_lower == target and name_lower not in already_claimed:
                return attr
        # Pass 2: partial
        for attr in all_attrs:
            name_lower = attr.get('name', '').lower()
            if name_lower not in already_claimed:
                if target in name_lower or name_lower in target:
                    return attr
        return None

    def _in_stock_for(target: str):
        in_stock_opts = set()
        for v in variations:
            if v.get('in_stock') or v.get('stock_status') == 'instock':
                for a in v.get('attributes', []):
                    if target in a.get('name', '').lower():
                        opt = a.get('option', '')
                        if opt:
                            in_stock_opts.add(opt)
        return in_stock_opts

    claimed = set()
    msg_parts = []

    for target in target_attrs:
        matched = _find_attr(target, claimed)
        if not matched:
            continue
        claimed.add(matched.get('name', '').lower())

        options = matched.get('options', [])
        display_name = matched.get('name', target.title())

        if not options:
            msg_parts.append(
                f"• **{display_name}**: options not listed in catalog"
            )
            continue

        opts_str = ", ".join(f"**{o}**" for o in options)
        line = f"• **{display_name}**: {opts_str}"

        in_stock = _in_stock_for(target)
        if in_stock and 0 < len(in_stock) < len(options):
            line += f" ✅ (In stock: {', '.join(sorted(in_stock))})"

        msg_parts.append(line)

    if msg_parts:
        return f"**{product_name}** — available options:\n\n" + "\n".join(msg_parts)

    # Fallback: no targeted attributes matched — show everything
    if attrs:
        msg = f"Here are the available options for **{product_name}**:\n\n"
        for attr in attrs[:6]:
            opts = ', '.join(attr.get('options', [])[:8])
            msg += f"• **{attr.get('name', '')}:** {opts}\n"
        return msg

    return (
        f"I found **{product_name}** but couldn't retrieve its "
        f"{', '.join(target_attrs)} options. "
        f"Try: *'What variations does {product_name} come in?'*"
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