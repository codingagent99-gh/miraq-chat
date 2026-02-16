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
from services.store_loader import StoreLoader

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
        params["consumer_key"] = WOO_CONSUMER_KEY
        params["consumer_secret"] = WOO_CONSUMER_SECRET

        try:
            if api_call.method == "GET":
                resp = self.session.get(
                    api_call.endpoint,
                    params=params,
                    timeout=30,
                )
            else:
                resp = self.session.request(
                    method=api_call.method,
                    url=api_call.endpoint,
                    params={
                        "consumer_key": WOO_CONSUMER_KEY,
                        "consumer_secret": WOO_CONSUMER_SECRET,
                    },
                    json=api_call.body,
                    timeout=30,
                )
            resp.raise_for_status()
            return {
                "success": True,
                "data": resp.json(),
                "total": resp.headers.get("X-WP-Total"),
                "total_pages": resp.headers.get("X-WP-TotalPages"),
            }
        except Exception as e:
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
    if intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY, Intent.REORDER, Intent.QUICK_ORDER):
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

    # For QUICK_ORDER, show the matched product with order context
    if intent == Intent.QUICK_ORDER and count > 0:
        if count == 1:
            p = products[0]
            msg = f"Perfect! I found **{p['name']}** for you! 🎯\n\n"
            if p.get("price", 0) > 0:
                msg += f"💰 Price: ${p['price']:.2f}\n"
            if p.get("short_description"):
                msg += f"\n{p['short_description']}\n"
            msg += "\nWould you like to add this to your cart?"
            return msg
        else:
            msg = f"I found **{count}** products matching your search! 🔍\n\n"
            for p in products[:5]:
                price_str = f"${p['price']:.2f}" if p.get("price", 0) > 0 else "Contact for price"
                msg += f"• **{p['name']}** — {price_str}\n"
            if count > 5:
                msg += f"\n...and {count - 5} more products."
            msg += "\n\nWhich one would you like to order?"
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

    if not message:
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

    # ─── Step 1: Classify intent ───
    result = classify(message)
    intent = result.intent
    entities = result.entities
    confidence = result.confidence

    # ─── Step 2: Build API calls ───
    api_calls = build_api_calls(result)

    # ─── Step 2.5: Resolve user context placeholders ───
    customer_id = user_context.get("customer_id")
    if customer_id:
        _resolve_user_placeholders(api_calls, customer_id)

    # ─── Step 3: Execute API calls ───
    all_products_raw = []
    order_data = []
    api_responses = woo_client.execute_all(api_calls)

    for resp in api_responses:
        if resp.get("success") and isinstance(resp.get("data"), list):
            if intent in ORDER_INTENTS:
                order_data.extend(resp["data"])
            else:
                all_products_raw.extend(resp["data"])
        elif resp.get("success") and isinstance(resp.get("data"), dict):
            if intent in ORDER_INTENTS:
                order_data.append(resp["data"])
            else:
                all_products_raw.append(resp["data"])

    # ─── Step 4: Format products ───
    products = [format_product(p) for p in all_products_raw]

    # Filter out private/draft products
    products = [p for p in products if p.get("name")]

    # ─── Step 5: Generate bot message ───
    bot_message = generate_bot_message(intent, entities, products, confidence, order_data)

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
    Format a WooCommerce date string to readable format.
    
    Args:
        date_created: ISO format date string from WooCommerce API
        
    Returns:
        Formatted date string (e.g., "Feb 10, 2026") or truncated original if parsing fails
    """
    date_str = date_created[:10] if len(date_created) >= 10 else date_created
    try:
        dt = datetime.fromisoformat(date_created.replace("Z", "+00:00"))
        date_str = dt.strftime("%b %d, %Y")
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
        customer_id: The actual customer ID to substitute for placeholders
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
    """Generate a bot message for order history from raw WooCommerce order data."""
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
        item_names = ", ".join(valid_item_names[:3])
        if len(line_items) > len(valid_item_names[:3]):
            item_names += f" +{len(line_items) - len(valid_item_names[:3])} more"
        
        msg += (
            f"**#{order_number}** — {status} "
            f"— ${total} "
            f"— {_format_order_date(date_created)}\n"
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
    """Load store data from WooCommerce at startup."""
    loader = StoreLoader()
    try:
        loader.load_all()
        set_store_loader(loader)
    except Exception as e:
        print(f"⚠️  Store loader error: {e}")
        print("   Continuing with static registry only.")


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