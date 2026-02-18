"""
WGC Tiles Store — Chat API Backend
Runs on port 5009 with /chat endpoint.

Usage:
    python server.py

Endpoint:
    POST http://localhost:5009/chat
    Body: {"message": "...", "session_id": "...", "user_context": {...}}
"""

import os
import json
import time
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as http_requests

# ─── Internal imports ───
from models import Intent, ExtractedEntities, ClassifiedResult, WooAPICall
from classifier import classify
from api_builder import build_api_calls
from store_registry import set_store_loader, get_store_loader
from store_loader import StoreLoader
from conversation_flow import (
    FlowState, ConversationContext,
    should_disambiguate, get_disambiguation_message,
    handle_flow_state, LOW_CONFIDENCE_THRESHOLD,
)
from chat_logger import get_logger, sanitize_url

# ─── Initialize logger ───
logger = get_logger("miraq_chat")
# ═══════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════

WOO_BASE_URL = os.getenv("WOO_BASE_URL", "https://wgc.net.in/hn/wp-json/wc/v3")
WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
PORT = int(os.getenv("PORT", 5009))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ═══════════════════════════════════════════
# SESSION STORE (in-memory for now)
# ═══════════════════════════════════════════

sessions: Dict[str, Dict] = {}

# ═══════════════════════════════════════════
# WOOCOMMERCE API CLIENT
# ═══════════════════════════════════════════

class WooClient:
    """Executes WooCommerce API calls with browser UA + query-string auth."""

    def __init__(self):
        self.session = http_requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    def execute(self, api_call: WooAPICall) -> dict:
        """Execute a single API call and return raw response."""
        params = dict(api_call.params)
        
        # Only add auth params for standard WooCommerce API, not for custom API
        is_custom_api = "/custom-api/" in api_call.endpoint
        if not is_custom_api:
            params["consumer_key"] = WOO_CONSUMER_KEY
            params["consumer_secret"] = WOO_CONSUMER_SECRET

        # Log API call (sanitize sensitive data)
        sanitized_endpoint = sanitize_url(api_call.endpoint)
        logger.info(f"WooCommerce API call: {api_call.method} {sanitized_endpoint}")

        try:
            if api_call.method == "GET":
                resp = self.session.get(
                    api_call.endpoint,
                    params=params,
                    timeout=30,
                )
            else:
                # For non-GET methods, only add auth if not custom API
                auth_params = {} if is_custom_api else {
                    "consumer_key": WOO_CONSUMER_KEY,
                    "consumer_secret": WOO_CONSUMER_SECRET,
                }
                resp = self.session.request(
                    method=api_call.method,
                    url=api_call.endpoint,
                    params=auth_params,
                    json=api_call.body,
                    timeout=30,
                )
            resp.raise_for_status()
            logger.info(f"WooCommerce API response: status={resp.status_code}, success=True")
            return {
                "success": True,
                "data": resp.json(),
                "total": resp.headers.get("X-WP-Total"),
                "total_pages": resp.headers.get("X-WP-TotalPages"),
            }
        except Exception as e:
            logger.error(f"WooCommerce API error: {api_call.method} {sanitized_endpoint} | error={str(e)}", exc_info=True)
            return {"success": False, "data": [], "error": str(e)}

    def execute_all(self, api_calls: List[WooAPICall]) -> List[dict]:
        results = []
        for call in api_calls:
            result = self.execute(call)
            results.append(result)
        return results


woo_client = WooClient()

# ═══════════════════════════════════════════
# PRODUCT FORMATTER
# ═══════════════════════════════════════════

def format_product(raw: dict) -> dict:
    """Convert raw WooCommerce product to clean response format."""
    images = raw.get("images", [])
    image_urls = [img.get("src", "") for img in images if img.get("src")]

    categories = raw.get("categories", [])
    cat_names = [c.get("name", "") for c in categories]

    tags = raw.get("tags", [])
    tag_names = [t.get("name", "") for t in tags]

    # Parse prices safely
    price = _safe_float(raw.get("price", ""))
    regular_price = _safe_float(raw.get("regular_price", ""))
    sale_price_raw = raw.get("sale_price", "")
    sale_price = _safe_float(sale_price_raw) if sale_price_raw else None

    return {
        "id": raw.get("id"),
        "name": raw.get("name", ""),
        "slug": raw.get("slug", ""),
        "sku": raw.get("sku", ""),
        "permalink": raw.get("permalink", ""),
        "price": price,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "on_sale": raw.get("on_sale", False),
        "in_stock": raw.get("stock_status") == "instock",
        "stock_status": raw.get("stock_status", ""),
        "total_sales": raw.get("total_sales", 0),
        "description": _clean_html(raw.get("description", "")),
        "short_description": _clean_html(raw.get("short_description", "")),
        "categories": cat_names,
        "tags": tag_names,
        "images": image_urls,
        "average_rating": raw.get("average_rating", "0.00"),
        "rating_count": raw.get("rating_count", 0),
        "weight": raw.get("weight", ""),
        "dimensions": raw.get("dimensions", {"length": "", "width": "", "height": ""}),
        "attributes": _format_attributes(raw.get("attributes", [])),
        "variations": raw.get("variations", []),
        "type": raw.get("type", "simple"),
    }


def _format_attributes(attrs: list) -> list:
    """Format product attributes for response."""
    result = []
    for attr in attrs:
        if attr.get("visible", False):
            result.append({
                "name": attr.get("name", ""),
                "options": attr.get("options", []),
            })
    return result


def format_custom_product(raw: dict) -> dict:
    """Convert raw custom API product to clean response format."""
    # Images are already a list of URLs (not objects like standard WC)
    image_urls = raw.get("images", [])
    
    # Categories are already a list of strings (not objects)
    cat_names = raw.get("categories", [])
    
    # Parse prices safely
    price = _safe_float(raw.get("price", ""))
    regular_price = _safe_float(raw.get("regular_price", ""))
    sale_price_raw = raw.get("sale_price", "")
    sale_price = _safe_float(sale_price_raw) if sale_price_raw else None
    
    # Derive on_sale from sale_price being non-empty
    on_sale = bool(sale_price_raw and sale_price_raw != "")
    
    # Attributes come as a dict {slug: {}} rather than a list
    # Convert to list format for consistency
    attributes_dict = raw.get("attributes", {})
    attributes = []
    for slug, attr_data in attributes_dict.items():
        if isinstance(attr_data, dict):
            # Extract options if available, otherwise empty list
            options = attr_data.get("options", []) if attr_data else []
            # Convert slug to readable name (e.g., pa_finish -> Finish)
            name = slug.replace("pa_", "").replace("-", " ").title()
            attributes.append({
                "name": name,
                "options": options,
            })
    
    return {
        "id": raw.get("id"),
        "name": raw.get("name", ""),
        "slug": raw.get("slug", ""),
        "sku": raw.get("sku", ""),
        "permalink": raw.get("permalink", ""),
        "price": price,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "on_sale": on_sale,
        "in_stock": raw.get("stock_status") == "instock",
        "stock_status": raw.get("stock_status", ""),
        "total_sales": 0,  # Not provided by custom API
        "description": _clean_html(raw.get("description", "")),
        "short_description": _clean_html(raw.get("short_description", "")),
        "categories": cat_names,
        "tags": [],  # Not provided by custom API
        "images": image_urls,
        "average_rating": "0.00",  # Not provided by custom API
        "rating_count": 0,  # Not provided by custom API
        "weight": "",  # Not provided by custom API
        "dimensions": {"length": "", "width": "", "height": ""},  # Not provided by custom API
        "attributes": attributes,
        "variations": [],  # Not provided by custom API
        "type": "simple",  # Not provided by custom API
    }


def format_variation(raw: dict, parent: dict = None) -> dict:
    """Convert a raw WooCommerce variation to clean response format."""
    price = _safe_float(raw.get("price", ""))
    regular_price = _safe_float(raw.get("regular_price", ""))
    sale_price_raw = raw.get("sale_price", "")
    sale_price = _safe_float(sale_price_raw) if sale_price_raw else None

    # Build attribute label from variation attributes e.g. "Matte / 24x48 / Grey"
    attrs = raw.get("attributes", [])
    attr_label = " / ".join(
        a.get("option", "") for a in attrs if a.get("option")
    )
    parent_name = parent.get("name", "") if parent else ""
    name = f"{parent_name} — {attr_label}" if attr_label else parent_name

    images = raw.get("image", {})
    image_url = images.get("src", "") if isinstance(images, dict) else ""

    return {
        "id": raw.get("id"),
        "parent_id": raw.get("parent_id") or (parent.get("id") if parent else None),
        "name": name,
        "slug": raw.get("slug", ""),
        "sku": raw.get("sku", ""),
        "permalink": parent.get("permalink", "") if parent else "",
        "price": price,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "on_sale": raw.get("on_sale", False),
        "in_stock": raw.get("stock_status") == "instock",
        "stock_status": raw.get("stock_status", ""),
        "images": [image_url] if image_url else (parent.get("images", []) if parent else []),
        "attributes": attrs,
        "type": "variation",
        "variation_label": attr_label,
    }


def _filter_variations_by_entities(
    variations: List[dict], entities: ExtractedEntities
) -> List[dict]:
    """
    Filter variation list by the attributes the user specified.
    Each variation has attributes like:
      [{"name": "Finish", "option": "Matte"}, {"name": "Tile Size", "option": '24"x48"'}]
    """
    # Build a set of (attr_name_lower, option_lower) pairs the user asked for
    filters: List[tuple] = []

    if entities.finish:
        filters.append(("finish", entities.finish.lower()))
        # Common synonyms handled by normalising both sides to lowercase
        FINISH_SYNONYMS = {"matt": "matte", "glossy": "polished", "gloss": "polished"}
        normalized = FINISH_SYNONYMS.get(entities.finish.lower(), entities.finish.lower())
        if normalized != entities.finish.lower():
            filters.append(("finish", normalized))

    if entities.color_tone:
        filters.append(("colors", entities.color_tone.lower()))
        filters.append(("colors 2", entities.color_tone.lower()))

    if entities.tile_size:
        filters.append(("tile size", entities.tile_size.lower()))

    if entities.thickness:
        filters.append(("thickness", entities.thickness.lower()))

    if entities.origin:
        filters.append(("origin", entities.origin.lower()))

    if entities.visual:
        filters.append(("visual", entities.visual.lower()))

    if not filters:
        return variations

    matched = []
    for var in variations:
        var_attrs = {
            a.get("name", "").lower(): a.get("option", "").lower()
            for a in var.get("attributes", [])
        }
        # Variation matches if ALL specified filters are satisfied
        if all(
            any(f_val in var_attrs.get(f_name, "") for f_name in var_attrs if f_name == attr_name or f_name.startswith(attr_name))
            or any(f_val in opt for opt in var_attrs.values())
            for attr_name, f_val in filters
        ):
            matched.append(var)

    return matched if matched else variations  # if nothing matched, return all (don't blank out)


def _safe_float(val) -> float:
    """Safely convert to float."""
    try:
        return float(val) if val not in ("", None) else 0.0
    except (ValueError, TypeError):
        return 0.0


def _clean_html(html: str) -> str:
    """Strip HTML tags from description."""
    if not html:
        return ""
    clean = re.sub(r'<[^>]+>', '', html)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean

# ═══════════════════════════════════════════
# BOT MESSAGE GENERATOR
# ═══════════════════════════════════════════

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

# ═══════════════════════════════════════════
# SUGGESTION GENERATOR
# ═══════════════════════════════════════════

def generate_suggestions(
    intent: Intent,
    entities: ExtractedEntities,
    products: List[dict],
) -> List[str]:
    """Generate follow-up suggestions based on context."""
    suggestions = []

    # Order-specific suggestions
    if intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY, Intent.REORDER):
        suggestions.append("Show my order history")
        suggestions.append("Reorder my last purchase")
        suggestions.append("Track my order")
        suggestions.append("Show me what's on sale")
        return suggestions[:4]

    if intent == Intent.QUICK_ORDER:
        suggestions.append("Show me all products")
        suggestions.append("What categories do you have?")
        suggestions.append("Show me quick ship products")
        suggestions.append("What's on sale?")
        return suggestions[:4]

    # Product-specific suggestions
    if products and len(products) == 1:
        p = products[0]
        name = p.get("name", "")
        base_name = name.split(" ")[0] if name else ""

        if "Chip Card" not in name:
            suggestions.append(f"Show me {base_name} Chip Card")
        if "Mosaic" not in name:
            suggestions.append(f"Show me {base_name} Mosaic")
        suggestions.append(f"What colors does {base_name} come in?")
        suggestions.append(f"What goes with {base_name}?")

    elif products and len(products) > 1:
        # Suggest browsing related
        if intent == Intent.CATEGORY_BROWSE and entities.category_name:
            suggestions.append(f"Show me more {entities.category_name} products")
        suggestions.append("Show me what's on sale")
        suggestions.append("Show me quick ship products")

    # General suggestions
    if intent == Intent.PRODUCT_SEARCH:
        suggestions.append("Show me all chip cards")
    if intent not in (Intent.CATEGORY_LIST, Intent.PRODUCT_CATALOG):
        suggestions.append("What categories do you have?")

    # Always include a fallback
    if not suggestions:
        suggestions = [
            "Show me all products",
            "What categories do you have?",
            "Show me what's on sale",
            "Quick ship tiles",
        ]

    return suggestions[:4]  # Max 4 suggestions

# ═══════════════════════════════════════════
# INTENT → SEARCH FILTERS MAPPER
# ═══════════════════════════════════════════

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
        "min_price": None,
        "max_price": None,
        "on_sale": None,
        "orderby": "date",
        "order": "desc",
    }

    # Extract from API call params
    if api_calls:
        params = api_calls[0].params
        filters["search"] = params.get("search")
        filters["category"] = params.get("category")
        filters["tag"] = params.get("tag")
        filters["on_sale"] = params.get("on_sale")
        if params.get("orderby"):
            filters["orderby"] = params["orderby"]
        if params.get("order"):
            filters["order"] = params["order"]

    # Override with entity data if more specific
    if entities.category_name and not filters["category"]:
        filters["category"] = entities.category_name
    if entities.on_sale:
        filters["on_sale"] = True

    return filters

# ═══════════════════════════════════════════
# INTENT → SIMPLE LABEL
# ═══════════════════════════════════════════

INTENT_LABELS = {
    Intent.PRODUCT_LIST:          "browse",
    Intent.PRODUCT_SEARCH:        "search",
    Intent.PRODUCT_DETAIL:        "detail",
    Intent.PRODUCT_CATALOG:       "catalog",
    Intent.PRODUCT_TYPES:         "catalog",
    Intent.PRODUCT_BY_COLLECTION: "browse",
    Intent.PRODUCT_BY_ORIGIN:     "filter",
    Intent.PRODUCT_BY_VISUAL:     "filter",
    Intent.PRODUCT_QUICK_SHIP:    "filter",
    Intent.RELATED_PRODUCTS:      "related",
    Intent.CATEGORY_BROWSE:       "category",
    Intent.CATEGORY_LIST:         "categories",
    Intent.FILTER_BY_FINISH:      "filter",
    Intent.FILTER_BY_SIZE:        "filter",
    Intent.FILTER_BY_COLOR:       "filter",
    Intent.FILTER_BY_THICKNESS:   "filter",
    Intent.FILTER_BY_EDGE:        "filter",
    Intent.FILTER_BY_APPLICATION: "filter",
    Intent.FILTER_BY_MATERIAL:    "filter",
    Intent.FILTER_BY_ORIGIN:      "filter",
    Intent.SIZE_LIST:             "info",
    Intent.MOSAIC_PRODUCTS:       "search",
    Intent.TRIM_PRODUCTS:         "search",
    Intent.CHIP_CARD:             "search",
    Intent.DISCOUNT_INQUIRY:      "deals",
    Intent.BULK_DISCOUNT:         "deals",
    Intent.CLEARANCE_PRODUCTS:    "deals",
    Intent.PROMOTIONS:            "deals",
    Intent.COUPON_INQUIRY:        "deals",
    Intent.SAVE_FOR_LATER:        "account",
    Intent.WISHLIST:              "account",
    Intent.ORDER_TRACKING:        "order",
    Intent.ORDER_STATUS:          "order",
    Intent.PLACE_ORDER:           "order",
    Intent.ORDER_HISTORY:         "order",
    Intent.LAST_ORDER:            "order",
    Intent.REORDER:               "order",
    Intent.ORDER_ITEM:            "order",
    Intent.QUICK_ORDER:           "order",
    Intent.PRODUCT_VARIATIONS:    "variations",
    Intent.SAMPLE_REQUEST:        "sample",
    Intent.UNKNOWN:               "unknown",
}

# ═══════════════════════════════════════════
# CONSTANTS FOR ORDER & USER HANDLING
# ═══════════════════════════════════════════

ORDER_INTENTS = {
    Intent.ORDER_HISTORY,
    Intent.LAST_ORDER,
    Intent.REORDER,
    Intent.ORDER_TRACKING,
    Intent.ORDER_STATUS,
}

USER_PLACEHOLDERS = {
    "CURRENT_USER_ID",
    "CURRENT_USER",
    "current_user_id",
    "current_user",
}

# Order message formatting constants
MAX_DISPLAYED_ITEMS = 3  # Maximum number of items to show before truncating with '+N more'

# Default payment method used when none is specified in the request.
# Change to "bacs" (bank transfer) or "stripe" etc. as needed.
DEFAULT_PAYMENT_METHOD = "cod"
DEFAULT_PAYMENT_METHOD_TITLE = "Cash on Delivery"

# ═══════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════

app = Flask(__name__)
CORS(app)


@app.route("/chat", methods=["POST"])
def chat():
    """
    Main chat endpoint.

    Request:
        POST /chat
        {
            "message": "show affogato chip card",
            "session_id": "session_xxx",
            "user_context": {
                "customer_id": 130,
                "email": "user@example.com"
            }
        }

    Response:
        {
            "success": true,
            "bot_message": "...",
            "intent": "search",
            "products": [...],
            "filters_applied": {...},
            "suggestions": [...],
            "session_id": "...",
            "metadata": {...}
        }
    """
    start_time = time.time()

    # ─── Parse request ───
    body = request.get_json(silent=True)
    if not body:
        logger.warning("POST /chat | Invalid JSON body")
        return jsonify({
            "success": False,
            "bot_message": "Invalid request. Send JSON with 'message' field.",
            "intent": "error",
            "products": [],
            "filters_applied": {},
            "suggestions": ["Show me all products", "What categories do you have?"],
            "session_id": "",
            "metadata": {"error": "Invalid JSON body"},
        }), 400

    message = body.get("message", "").strip()
    session_id = body.get("session_id", "")
    user_context = body.get("user_context", {})
    
    # Log incoming request
    truncated_msg = message[:100] + "..." if len(message) > 100 else message
    customer_id = user_context.get("customer_id")
    flow_state = user_context.get("flow_state", "idle")
    logger.info(f'POST /chat | session={session_id} | message="{truncated_msg}" | customer_id={customer_id} | flow_state={flow_state}')

    if not message:
        logger.warning(f"POST /chat | session={session_id} | Empty message")
        return jsonify({
            "success": False,
            "bot_message": "Please type a message! Try asking about our tiles, categories, or products.",
            "intent": "error",
            "products": [],
            "filters_applied": {},
            "suggestions": [
                "Show me all products",
                "What categories do you have?",
                "Show me marble look tiles",
                "Quick ship tiles",
            ],
            "session_id": session_id,
            "metadata": {"error": "Empty message"},
        }), 400

    # ─── Update session ───
    if session_id:
        if session_id not in sessions:
            sessions[session_id] = {
                "history": [],
                "user_context": user_context,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        sessions[session_id]["history"].append({
            "role": "user",
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ─── Step 0: Check conversation flow state ───
    flow_state_str = user_context.get("flow_state", "idle")
    try:
        current_flow_state = FlowState(flow_state_str)
    except ValueError:
        current_flow_state = FlowState.IDLE
    
    logger.info(f"Step 0: Flow state={current_flow_state.value}")

    # Build context dict from user_context for the flow handler
    flow_context = {
        "pending_product_name": user_context.get("pending_product_name"),
        "pending_product_id": user_context.get("pending_product_id"),
        "pending_quantity": user_context.get("pending_quantity"),
    }

    # If we're in a multi-turn flow, let the flow handler try first
    if current_flow_state != FlowState.IDLE:
        flow_result = handle_flow_state(
            state=current_flow_state,
            message=message,
            entities=flow_context,
            confidence=0.0,
        )
        if flow_result and not flow_result.get("pass_through"):
            # Flow handler consumed the message — return immediately
            logger.info(f"Step 0: Flow handler consumed message | new_state={flow_result.get('flow_state', 'idle')}")
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": flow_result["bot_message"],
                "intent": "guided_flow",
                "products": [],
                "filters_applied": {},
                "suggestions": flow_result.get("suggestions", []),
                "session_id": session_id,
                "metadata": {
                    "flow_state": flow_result.get("flow_state", "idle"),
                    "response_time_ms": round((time.time() - start_time) * 1000),
                    "provider": "conversation_flow",
                },
                "flow_state": flow_result.get("flow_state", "idle"),
            }), 200

        elif flow_result and flow_result.get("override_message"):
            # Flow wants to redirect to a different utterance
            message = flow_result["override_message"]

        elif flow_result and flow_result.get("create_order"):
            # Flow confirmed order — build order with pending context
            pass  # Falls through to normal pipeline with order intent

    # ─── Step 1: Classify intent ───
    result = classify(message)
    intent = result.intent
    entities = result.entities
    confidence = result.confidence
    
    # Log classification result with key entities
    entity_summary = {
        k: v for k, v in {
            "product_name": entities.product_name,
            "category_name": entities.category_name,
            "product_id": entities.product_id,
            "order_item_name": entities.order_item_name,
            "quantity": entities.quantity,
        }.items() if v is not None
    }
    logger.info(f"Step 1: Classified intent={intent.value} | confidence={confidence:.2f} | entities={entity_summary}")

    # ─── Step 1.5: Disambiguation check ───
    if should_disambiguate(intent.value, confidence):
        disambig = get_disambiguation_message()
        elapsed = time.time() - start_time
        logger.info(f"Step 1.5: Low confidence, returning disambiguation | confidence={confidence:.2f}")
        return jsonify({
            "success": True,
            "bot_message": disambig["bot_message"],
            "intent": "disambiguation",
            "products": [],
            "filters_applied": {},
            "suggestions": disambig["suggestions"],
            "session_id": session_id,
            "metadata": {
                "flow_state": disambig["flow_state"],
                "confidence": round(confidence, 2),
                "original_intent": intent.value,
                "response_time_ms": round((time.time() - start_time) * 1000),
                "provider": "conversation_flow",
            },
            "flow_state": disambig["flow_state"],
        }), 200

    # ─── Step 2: Build API calls ───
    api_calls = build_api_calls(result)
    logger.info(f"Step 2: Built {len(api_calls)} API call(s) | endpoints={[f'{c.method} {c.endpoint.split('/')[-1]}' for c in api_calls]}")

    # ─── Step 2.5: Resolve user context placeholders ───
    customer_id = user_context.get("customer_id")
    if customer_id:
        logger.info(f"Step 2.5: Resolved customer_id={customer_id}")
        _resolve_user_placeholders(api_calls, customer_id)

    # ─── Step 2.6: Extract last_product context (for "order this" resolution) ───
    # The frontend sends the last displayed product so vague order phrases
    # like "order this" / "buy it" resolve correctly without a product search.
    last_product_ctx = user_context.get("last_product")  # {id, name} or None
    
    if last_product_ctx and last_product_ctx.get("id"):
        logger.info(f"Step 2.6: last_product_ctx found: id={last_product_ctx.get('id')}, name=\"{last_product_ctx.get('name')}\"")
    else:
        logger.info("Step 2.6: No last_product_ctx")

    # ─── Step 3: Execute API calls ───
    all_products_raw = []
    order_data = []
    api_responses = woo_client.execute_all(api_calls)

    for resp in api_responses:
        if resp.get("success"):
            data = resp.get("data")
            # Handle custom API response format (has "products" key)
            if isinstance(data, dict) and "products" in data:
                if intent in ORDER_INTENTS:
                    order_data.extend(data["products"])
                else:
                    all_products_raw.extend(data["products"])
            elif isinstance(data, list):
                if intent in ORDER_INTENTS:
                    order_data.extend(data)
                else:
                    all_products_raw.extend(data)
            elif isinstance(data, dict):
                if intent in ORDER_INTENTS:
                    order_data.append(data)
                else:
                    all_products_raw.append(data)
        else:
            # Log API call failure
            logger.warning(f"Step 3: API call failed | error={resp.get('error', 'Unknown')}")
    
    logger.info(f"Step 3: API execution complete | all_products_raw count={len(all_products_raw)} | order_data count={len(order_data)}")

    # ─── Step 3.5: REORDER step 2 — create new order from last order's line_items ───
    if intent == Intent.REORDER and order_data:
        source_order = order_data[0]
        source_line_items = source_order.get("line_items", [])
        logger.info(f"Step 3.5: Reorder attempt | source_order_id={source_order.get('id')} | line_items_count={len(source_line_items)}")
        if source_line_items and customer_id:
            new_line_items = [
                {
                    "product_id": item["product_id"],
                    "quantity": item.get("quantity", 1),
                    **({"variation_id": item["variation_id"]} if item.get("variation_id") else {}),
                }
                for item in source_line_items
                if item.get("product_id")
            ]
            if new_line_items:
                reorder_call = WooAPICall(
                    method="POST",
                    endpoint=f"{WOO_BASE_URL}/orders",
                    params={},
                    body={
                        "status": "processing",
                        "customer_id": customer_id,
                        "payment_method": DEFAULT_PAYMENT_METHOD,
                        "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
                        "set_paid": False,
                        "line_items": new_line_items,
                    },
                    description="Create reorder from last order line items (COD, on-hold)",
                )
                reorder_resp = woo_client.execute(reorder_call)
                if reorder_resp.get("success") and isinstance(reorder_resp.get("data"), dict):
                    order_data.append(reorder_resp["data"])
                    new_order = reorder_resp["data"]
                    logger.info(f"Step 3.5: Reorder created successfully | order_id={new_order.get('id')} | order_number={new_order.get('number')}")
                else:
                    logger.warning(f"Step 3.5: Reorder failed | error={reorder_resp.get('error', 'Unknown')}")

    # ─── Step 3.6: QUICK_ORDER / ORDER_ITEM / PLACE_ORDER — create order from matched product ───
    if intent in (Intent.QUICK_ORDER, Intent.ORDER_ITEM, Intent.PLACE_ORDER) and customer_id:
        # Resolve which product to order:
        # Priority 1 — product returned by the search API call this turn
        # Priority 2 — last_product sent by the frontend (handles "order this" / "buy it")
        _order_product_id = None
        _order_product_name = None

        if all_products_raw:
            _p = all_products_raw[0]
            _order_product_id = _p.get("id")
            _order_product_name = _p.get("name", str(_order_product_id))
            logger.info(f"Step 3.6: Using all_products_raw → product_id={_order_product_id}, product_name=\"{_order_product_name}\"")
        elif last_product_ctx and last_product_ctx.get("id"):
            _order_product_id = last_product_ctx["id"]
            _order_product_name = last_product_ctx.get("name", str(last_product_ctx["id"]))
            logger.info(f"Step 3.6: Using last_product_ctx → product_id={_order_product_id}, product_name=\"{_order_product_name}\"")
            # Inject a minimal product dict so bot message and response include the product info
            all_products_raw.append({
                "id": _order_product_id,
                "name": _order_product_name,
                "price": "",
                "regular_price": "",
                "sale_price": "",
                "slug": "",
                "sku": "",
                "permalink": "",
                "on_sale": False,
                "stock_status": "instock",
                "total_sales": 0,
                "description": "",
                "short_description": "",
                "images": [],
                "categories": [],
                "tags": [],
                "attributes": [],
                "variations": [],
                "type": "simple",
                "average_rating": "0.00",
                "rating_count": 0,
                "weight": "",
                "dimensions": {"length": "", "width": "", "height": ""},
            })
            logger.info(f"Step 3.6: Injected minimal product dict into all_products_raw (count={len(all_products_raw)})")
        else:
            logger.warning("Step 3.6: No product found to order (all_products_raw empty, no last_product_ctx)")

        if _order_product_id:
            logger.info(f"Step 3.6: Creating WooCommerce order | product_id={_order_product_id} | quantity={entities.quantity or 1} | customer_id={customer_id}")
            order_call = WooAPICall(
                method="POST",
                endpoint=f"{WOO_BASE_URL}/orders",
                params={},
                body={
                    "status": "processing",
                    "customer_id": customer_id,
                    "payment_method": DEFAULT_PAYMENT_METHOD,
                    "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
                    "set_paid": False,
                    "line_items": [{"product_id": _order_product_id, "quantity": entities.quantity or 1}],
                },
                description=f"Create order for product '{_order_product_name}' (COD, processing)",
            )
            order_resp = woo_client.execute(order_call)
            if order_resp.get("success") and isinstance(order_resp.get("data"), dict):
                order_data.append(order_resp["data"])
                created_order = order_resp["data"]
                line_items_summary = [
                    f"{item.get('name', 'Unknown')} x{item.get('quantity', 1)}"
                    for item in created_order.get("line_items", [])
                ]
                logger.info(
                    f"Step 3.6: WooCommerce order created | order_id={created_order.get('id')} | "
                    f"order_number={created_order.get('number')} | total=${created_order.get('total', '0.00')} | "
                    f"line_items={line_items_summary}"
                )
            else:
                logger.error(f"Step 3.6: WooCommerce order creation failed | error={order_resp.get('error', 'Unknown')}")
        else:
            logger.warning("Step 3.6: Skipped order creation (no product_id resolved)")

    # ─── Step 3.7: Variation product handling ───
    # When api_builder issued GET /products/{id} + GET /products/{id}/variations,
    # we need to separate, filter, and format them properly.
    VARIATION_INTENTS = {Intent.PRODUCT_SEARCH, Intent.PRODUCT_DETAIL, Intent.PRODUCT_VARIATIONS}

    if intent in VARIATION_INTENTS and entities.product_id:
        parent_product_raw = None
        variations_raw = []

        for resp in api_responses:
            if not resp.get("success"):
                continue
            data = resp.get("data")
            if isinstance(data, dict) and data.get("id") == entities.product_id:
                parent_product_raw = data
            elif isinstance(data, list) and data and data[0].get("parent_id") is not None:
                variations_raw = data

        if parent_product_raw:
            parent_formatted = format_product(parent_product_raw)
            has_attributes = any([
                entities.finish, entities.color_tone, entities.tile_size,
                entities.thickness, entities.visual, entities.origin,
            ])

            if variations_raw and has_attributes:
                # Filter variations to only those matching user's attributes
                filtered_vars = _filter_variations_by_entities(variations_raw, entities)
                variation_products = [format_variation(v, parent_product_raw) for v in filtered_vars]
                products = [parent_formatted] + variation_products
            elif variations_raw:
                # No attribute filter — return parent + all variations
                variation_products = [format_variation(v, parent_product_raw) for v in variations_raw]
                products = [parent_formatted] + variation_products
            else:
                # Simple product or variations call not made
                products = [parent_formatted]

            bot_message = generate_bot_message(intent, entities, products, confidence, order_data)
            suggestions = generate_suggestions(intent, entities, products)
            filters = build_filters(intent, entities, api_calls)
            elapsed = time.time() - start_time
            metadata = {
                "confidence": round(confidence, 2),
                "products_count": len(products),
                "provider": "wgc_intent_classifier",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "response_time_ms": round(elapsed * 1000),
                "intent_raw": intent.value,
                "entities": _entities_to_dict(entities),
                "variations_found": len(variations_raw),
                "variations_matched": len(products) - 1 if variations_raw else 0,
            }
            if session_id and session_id in sessions:
                sessions[session_id]["history"].append({
                    "role": "bot", "message": bot_message, "intent": intent.value,
                    "products_count": len(products),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            return jsonify({
                "success": True,
                "bot_message": bot_message,
                "intent": INTENT_LABELS.get(intent, "unknown"),
                "products": products,
                "filters_applied": filters,
                "suggestions": suggestions,
                "session_id": session_id,
                "metadata": metadata,
            }), 200

    # ─── Step 4: Format products ───
    products = []
    for p in all_products_raw:
        # Detect custom API format by checking for "featured_image" key
        if "featured_image" in p:
            products.append(format_custom_product(p))
        else:
            products.append(format_product(p))

    # Filter out private/draft products
    products = [p for p in products if p.get("name")]
    logger.info(f"Step 4: Formatted {len(products)} products")

    # ─── Step 5: Generate bot message ───
    bot_message = generate_bot_message(intent, entities, products, confidence, order_data)
    
    # Log product name and total for order intents (critical for debugging "your item" / $0.00 issues)
    ORDER_CREATE_INTENTS = {Intent.QUICK_ORDER, Intent.ORDER_ITEM, Intent.PLACE_ORDER}
    if intent in ORDER_CREATE_INTENTS and order_data:
        placed_order = order_data[-1]
        # Extract product name used in bot message
        if products:
            used_product_name = products[0]["name"]
        elif placed_order.get("line_items"):
            used_product_name = placed_order["line_items"][0].get("name") or "your item"
        else:
            used_product_name = "your item"
        
        # Extract total
        total = placed_order.get("total", "0.00")
        if float(total) == 0.0 and placed_order.get("line_items"):
            line_total = sum(float(item.get("total", "0") or "0") for item in placed_order["line_items"])
            if line_total > 0:
                total = str(line_total)
                logger.warning(f"Step 5: Order total was $0.00, used line_item total=${line_total:.2f} instead")
        
        logger.info(f"Step 5: Bot message generated | product_name=\"{used_product_name}\" | total=${float(total):.2f}")
        
        # Warn if fallback to "your item" was used
        if used_product_name == "your item":
            logger.warning("Step 5: Used fallback 'your item' - no product name available from products[] or line_items[]")
        
        # Warn if $0.00 total detected
        if float(total) == 0.0:
            logger.warning("Step 5: Order total is $0.00 - possible pricing issue")

    # ─── Step 6: Generate suggestions ───
    suggestions = generate_suggestions(intent, entities, products)

    # ─── Step 7: Build filters ───
    filters = build_filters(intent, entities, api_calls)

    # ─── Step 8: Build metadata ───
    elapsed = time.time() - start_time
    metadata = {
        "confidence": round(confidence, 2),
        "products_count": len(products),
        "provider": "wgc_intent_classifier",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_time_ms": round(elapsed * 1000),
        "intent_raw": intent.value,
        "entities": _entities_to_dict(entities),
    }

    # ─── Step 9: Update session history ───
    if session_id and session_id in sessions:
        sessions[session_id]["history"].append({
            "role": "bot",
            "message": bot_message,
            "intent": intent.value,
            "products_count": len(products),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ─── Step 10: Build response ───
    response = {
        "success": True,
        "bot_message": bot_message,
        "intent": INTENT_LABELS.get(intent, "unknown"),
        "products": products,
        "filters_applied": filters,
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": metadata,
    }

    # ─── Step 5.5: Detect when quantity is needed for ordering ───
    ORDER_CREATE_INTENTS = {Intent.QUICK_ORDER, Intent.ORDER_ITEM, Intent.PLACE_ORDER}
    if intent in ORDER_CREATE_INTENTS and not entities.quantity and products:
        # We found the product but no quantity — ask
        product = products[0]
        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": f"Sure, I can order **{product['name']}** for you! How many do you need? 🛒",
            "intent": INTENT_LABELS.get(intent, "order"),
            "products": products[:1],
            "filters_applied": {},
            "suggestions": ["1", "5", "10", "25"],
            "session_id": session_id,
            "metadata": {
                "flow_state": FlowState.AWAITING_QUANTITY.value,
                "pending_product_name": product["name"],
                "pending_product_id": product.get("id"),
                "response_time_ms": round((time.time() - start_time) * 1000),
            },
            "flow_state": FlowState.AWAITING_QUANTITY.value,
        }), 200

    # ─── Step 10.5: After successful response, add "anything else?" flow ───
    # Append to the response dict before returning:
    if intent in ORDER_CREATE_INTENTS and order_data:
        response["flow_state"] = FlowState.AWAITING_ANYTHING_ELSE.value
    else:
        response["flow_state"] = FlowState.IDLE.value
    
    # ─── Step 10: Log final response summary ───
    logger.info(
        f"Step 10: Response sent | intent={INTENT_LABELS.get(intent, 'unknown')} | "
        f"products_count={len(products)} | response_time_ms={metadata['response_time_ms']} | "
        f"flow_state={response['flow_state']}"
    )
        
    return jsonify(response), 200


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    loader = get_store_loader()
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "store": {
            "categories_loaded": len(loader.categories) if loader else 0,
            "tags_loaded": len(loader.tags) if loader else 0,
            "attributes_loaded": len(loader.attributes) if loader else 0,
        },
    })


@app.route("/categories", methods=["GET"])
def list_categories():
    """List all loaded categories."""
    loader = get_store_loader()
    if not loader or not loader.categories:
        return jsonify({"categories": [], "message": "No categories loaded"})

    cats = []
    for cat in loader.categories:
        if cat.get("slug") != "uncategorized":
            cats.append({
                "id": cat["id"],
                "name": cat.get("name", ""),
                "slug": cat.get("slug", ""),
                "count": cat.get("count", 0),
                "parent": cat.get("parent", 0),
            })
    return jsonify({"categories": cats})


@app.route("/session/<session_id>", methods=["GET"])
def get_session(session_id):
    """Get session history."""
    if session_id in sessions:
        return jsonify({"session": sessions[session_id]})
    return jsonify({"error": "Session not found"}), 404


# ═══════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════

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


def _resolve_user_placeholders(api_calls: List[WooAPICall], customer_id: int):
    """
    Replace CURRENT_USER_ID placeholders with actual customer ID.
    
    Modifies api_calls in-place, replacing any placeholder strings in params or body
    with the provided customer_id (converted to string for API compatibility).
    
    Args:
        api_calls: List of WooAPICall objects to process
        customer_id: The actual customer ID (integer) to substitute for placeholders.
                     Will be converted to string internally for WooCommerce API compatibility.
    """
    customer_id_str = str(customer_id)
    for call in api_calls:
        if isinstance(call.params, dict):
            for key in call.params:
                if isinstance(call.params[key], str) and call.params[key] in USER_PLACEHOLDERS:
                    call.params[key] = customer_id_str
        if isinstance(call.body, dict):
            for key in call.body:
                if isinstance(call.body[key], str) and call.body[key] in USER_PLACEHOLDERS:
                    call.body[key] = customer_id_str


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


def _entities_to_dict(entities: ExtractedEntities) -> dict:
    """Convert entities to a clean dict for metadata."""
    d = {}
    if entities.product_name:    d["product_name"] = entities.product_name
    if entities.product_slug:    d["product_slug"] = entities.product_slug
    if entities.category_id:     d["category_id"] = entities.category_id
    if entities.category_name:   d["category_name"] = entities.category_name
    if entities.visual:          d["visual"] = entities.visual
    if entities.finish:          d["finish"] = entities.finish
    if entities.tile_size:       d["tile_size"] = entities.tile_size
    if entities.color_tone:      d["color_tone"] = entities.color_tone
    if entities.thickness:       d["thickness"] = entities.thickness
    if entities.origin:          d["origin"] = entities.origin
    if entities.collection_year: d["collection_year"] = entities.collection_year
    if entities.quick_ship:      d["quick_ship"] = True
    if entities.on_sale:         d["on_sale"] = True
    if entities.tag_slugs:       d["tags"] = entities.tag_slugs
    if entities.order_id:        d["order_id"] = entities.order_id
    if entities.order_count:     d["order_count"] = entities.order_count
    if entities.reorder:         d["reorder"] = entities.reorder
    if entities.order_item_name: d["order_item_name"] = entities.order_item_name
    return d

# ═══════════════════���═══════════════════════
# STARTUP
# ═══════════════════════════════════════════

def initialize_store():
    """Load store data from WooCommerce at startup, then start background refresh."""
    loader = StoreLoader()
    try:
        loader.load_all()
        set_store_loader(loader)
        # Start background refresh every 6 hours so data stays current
        loader.start_background_refresh()
    except Exception as e:
        print(f"⚠️  Store loader error: {e}")
        print("   Server will respond with limited functionality until store data loads.")
        # Still register the (partially loaded) loader so StoreLoader methods work
        set_store_loader(loader)


if __name__ == "__main__":
    print("=" * 60)
    print("  WGC Tiles Store — Chat API Server")
    print("=" * 60)
    print()

    # Load store data
    initialize_store()

    print()
    print(f"🚀 Starting server on http://localhost:{PORT}")
    print(f"   POST http://localhost:{PORT}/chat")
    print(f"   GET  http://localhost:{PORT}/health")
    print(f"   GET  http://localhost:{PORT}/categories")
    print()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=DEBUG,
    )