from typing import List
from datetime import datetime
from models import Intent, ExtractedEntities
from config.settings import MAX_DISPLAYED_ITEMS


def generate_bot_message(
    intent: Intent,
    entities: ExtractedEntities,
    products: List[dict],
    confidence: float,
    order_data: List[dict] = None,
) -> str:
    """Generate a natural language bot response."""

    if order_data is None:
        order_data = []
    
    count = len(products)

    # ── Order-specific handling ──
    # For order intents (LAST_ORDER, ORDER_HISTORY, REORDER), handle order data first
    if intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY, Intent.REORDER):
        # If we have actual order data, format it
        if intent == Intent.ORDER_HISTORY and order_data:
            return _format_order_history_message(order_data)
        elif intent == Intent.LAST_ORDER and order_data:
            # Format last order message
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
            # Show what was reordered and confirm the new order was placed
            source_order = order_data[0]
            source_number = source_order.get("number", str(source_order.get("id", "")))
            line_items = source_order.get("line_items", [])

            # order_data[1] is the newly created order (if step 2 succeeded)
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
        
        # Fallback messages when no order data
        if count == 0:
            if intent == Intent.LAST_ORDER:
                return (
                    "I can show you your most recent order! 📦\n\n"
                    "Please make sure you're logged in so I can retrieve your order history."
                )
            elif intent == Intent.ORDER_HISTORY:
                return (
                    f"I can show you your order history! 📋\n\n"
                    "Please make sure you're logged in so I can retrieve your past orders."
                )
            elif intent == Intent.REORDER:
                return (
                    "I can help you reorder your last purchase! 🔄\n\n"
                    "Please make sure you're logged in so I can access your order history."
                )
            elif intent == Intent.QUICK_ORDER:
                search_term = entities.order_item_name or entities.product_name or "that item"
                return (
                    f"I couldn't find a product matching **{search_term}**. 😕\n\n"
                    "Try searching by a different name or browse our categories."
                )

    # For QUICK_ORDER / ORDER_ITEM / PLACE_ORDER — confirm order if placed, else show product
    if intent in (Intent.QUICK_ORDER, Intent.ORDER_ITEM, Intent.PLACE_ORDER):
        # Order was successfully created — show confirmation
        if order_data:
            placed = order_data[-1]
            order_number = placed.get("number") or placed.get("id", "N/A")
            # Try products list first, then fall back to order line_items, then "your item"
            if products:
                p_name = products[0]["name"]
            elif placed.get("line_items"):
                p_name = placed["line_items"][0].get("name") or "your item"
            else:
                p_name = "your item"
            total = placed.get("total", "0.00")
            # If order total is zero but line items have totals, use line item total
            if float(total) == 0.0 and placed.get("line_items"):
                # Use "or '0'" to handle None or empty string from WooCommerce response
                line_total = sum(float(item.get("total", "0") or "0") for item in placed["line_items"])
                if line_total > 0:
                    total = str(line_total)
            return (
                f"✅ **Order #{order_number} placed successfully!**\n\n"
                f"**Product:** {p_name}\n"
                f"**Total:** ${float(total):.2f}\n"
                f"**Payment Mode:** Cash on Delivery\n"
                f"**Status:** Processing"
            )
        # Product found but no customer — prompt login
        if count > 0:
            p = products[0]
            msg = f"Found **{p['name']}** 🎯\n\n"
            if p.get("price", 0) > 0:
                msg += f"💰 Price: ${p['price']:.2f}\n"
            msg += "\n⚠️ Please log in to place an order."
            return msg

    # ── No products found ──
    if count == 0:
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

    # ── Variation results (parent + filtered variations) ──
    if intent in (Intent.PRODUCT_SEARCH, Intent.PRODUCT_DETAIL, Intent.PRODUCT_VARIATIONS) \
            and entities.product_id and count > 0:
        parent = products[0]
        variations = [p for p in products[1:] if p.get("type") == "variation"]
        has_attributes = any([
            entities.finish, entities.color_tone, entities.tile_size,
            entities.thickness, entities.visual, entities.origin,
        ])

        if intent == Intent.PRODUCT_VARIATIONS or (not has_attributes):
            # "What variations does Lager have?" or plain "show me lager"
            msg = f"🎯 **{parent['name']}**\n"
            if parent.get("price", 0) > 0:
                msg += f"💰 Starting from ${parent['price']:.2f}\n"
            if parent.get("short_description"):
                msg += f"\n{parent['short_description']}\n"
            if variations:
                msg += f"\n**Available variations ({len(variations)}):**\n"
                for v in variations[:10]:
                    label = v.get("variation_label") or v.get("name", "")
                    price_str = f"${v['price']:.2f}" if v.get("price", 0) > 0 else "Contact for price"
                    stock = "✅" if v.get("in_stock") else "❌"
                    msg += f"  {stock} {label} — {price_str}\n"
                if len(variations) > 10:
                    msg += f"  ...and {len(variations) - 10} more variations.\n"
            elif parent.get("attributes"):
                msg += "\n**Available options:**\n"
                for attr in parent["attributes"][:4]:
                    opts = ", ".join(attr["options"][:6])
                    msg += f"  • **{attr['name']}:** {opts}\n"
            return msg

        else:
            # User asked with attributes e.g. "lager matte 24x48"
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
                price_str = f"${v['price']:.2f}" if v.get("price", 0) > 0 else "Contact for price"
                stock = "✅ In stock" if v.get("in_stock") else "❌ Out of stock"
                msg += f"• **{label}** — {price_str} — {stock}\n"
            if len(variations) > 10:
                msg += f"\n...and {len(variations) - 10} more."
            return msg

    # ── Single product found ──
    if count == 1:
        p = products[0]
        msg = f"I found the perfect match! 🎯\n\n**{p['name']}**\n"
        if p.get("price", 0) > 0:
            msg += f"💰 Price: ${p['price']:.2f}\n"
        else:
            msg += f"💰 Price: $0.00\n"
        if p.get("on_sale") and p.get("sale_price"):
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
        msg += f"Here are **{count}** products in the **{entities.category_name}** category! 📂\n\n"
    elif intent == Intent.PRODUCT_BY_VISUAL:
        msg += f"Found **{count}** products with **{entities.visual}** look! 🎨\n\n"
    elif intent == Intent.FILTER_BY_FINISH:
        msg += f"Here are **{count}** products with **{entities.finish}** finish! ✨\n\n"
    elif intent == Intent.FILTER_BY_COLOR:
        msg += f"Found **{count}** products in **{entities.color_tone}** tones! 🎨\n\n"
    elif intent == Intent.PRODUCT_SEARCH:
        msg += f"Found **{count}** products matching your search! 🔍\n\n"
    elif intent == Intent.CHIP_CARD:
        msg += f"Here are **{count}** chip cards available! 🃏\n\n"
    elif intent == Intent.MOSAIC_PRODUCTS:
        msg += f"Found **{count}** mosaic products! 🧩\n\n"
    elif intent == Intent.CATEGORY_LIST:
        msg += f"Here are our product categories! 📂\n\n"
    else:
        msg += f"Here are **{count}** products I found! 🛍️\n\n"

    # List first 5 products
    for p in products[:5]:
        price_str = f"${p['price']:.2f}" if p.get("price", 0) > 0 else "Contact for price"
        msg += f"• **{p['name']}** — {price_str}\n"

    if count > 5:
        msg += f"\n...and {count - 5} more products."

    return msg


def _format_order_date(date_created: str) -> str:
    """
    Format a WooCommerce date string to readable date + time format.

    Args:
        date_created: ISO format date string from WooCommerce API

    Returns:
        Formatted string e.g. "Feb 10, 2026 at 3:45 PM" or fallback if parsing fails
    """
    date_str = date_created[:10] if len(date_created) >= 10 else date_created
    try:
        dt = datetime.fromisoformat(date_created.replace("Z", "+00:00"))
        date_str = dt.strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    except (ValueError, AttributeError):
        pass
    return date_str


def _format_order_history_message(orders: List[dict]) -> str:
    """
    Generate a bot message for order history from raw WooCommerce order data.
    
    Args:
        orders: List of WooCommerce order dictionaries. Each order should contain:
            - id (int): Order ID
            - number (str): Order number
            - status (str): Order status (e.g., 'completed', 'processing')
            - total (str): Order total amount
            - date_created (str): ISO format date string
            - line_items (list): List of items with 'name', 'quantity', 'total'
    
    Returns:
        str: Formatted message showing order history or empty message if no orders
    """
    if not orders:
        return (
            "You don't have any orders yet. 📦\n\n"
            "Browse our collection and place your first order!"
        )
    
    msg = f"📋 **Your Order History** ({len(orders)} orders)\n\n"
    
    for order in orders:
        order_id = order.get("id", "")
        order_number = order.get("number", str(order_id))
        status = order.get("status", "unknown").title()
        total = order.get("total", "0")
        date_created = order.get("date_created", "")
        
        # Get item names with accurate count
        line_items = order.get("line_items", [])
        valid_item_names = [item.get("name") for item in line_items if item.get("name")]
        item_names = ", ".join(valid_item_names[:MAX_DISPLAYED_ITEMS])
        if len(valid_item_names) > MAX_DISPLAYED_ITEMS:
            item_names += f" +{len(valid_item_names) - MAX_DISPLAYED_ITEMS} more"
        
        msg += (
            f"**#{order_number}** — {status} — ${total}\n"
            f"  🕐 {_format_order_date(date_created)}\n"
            f"  Items: {item_names}\n\n"
        )
    
    return msg
