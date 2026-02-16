"""
Intent Classifier Engine
=========================
Hybrid approach: keyword/regex rules + fuzzy matching.
Replace with an ML model (Rasa, spaCy, Transformers) for production.
"""

import re
from typing import Tuple
from intent_classifier import Intent, ExtractedEntities, ClassifiedResult


# ─────────────────────────────────────────────
# CATEGORY MAPPING (slug → WooCommerce category slug)
# ─────────────────────────────────────────────

CATEGORY_MAP = {
    "floor":     "floor-tiles",
    "wall":      "wall-tiles",
    "bathroom":  "bathroom-tiles",
    "kitchen":   "kitchen-tiles",
    "outdoor":   "outdoor-tiles",
    "pool":      "pool-tiles",
    "mosaic":    "mosaic-tiles",
    "porcelain": "porcelain-tiles",
    "ceramic":   "ceramic-tiles",
}

SIZE_MAP = {
    "small":        {"tag": "small-tiles"},
    "large":        {"tag": "large-tiles"},
    "large format": {"tag": "large-format-tiles"},
    "12x12":        {"attribute": "pa_size", "attribute_term": "12x12"},
    "24x24":        {"attribute": "pa_size", "attribute_term": "24x24"},
    "60x60":        {"attribute": "pa_size", "attribute_term": "60x60"},
}


# ─────────────────────────────────────────────
# CLASSIFICATION RULES
# ─────────────────────────────────────────────

def classify(utterance: str) -> ClassifiedResult:
    """Classify a user utterance into an intent with extracted entities."""
    text = utterance.lower().strip()
    entities = ExtractedEntities()
    intent = Intent.UNKNOWN
    confidence = 0.0

    # ── 1. PRODUCT DISCOVERY ──
    # Category browse (room/type + tiles)
    for keyword, slug in CATEGORY_MAP.items():
        if keyword in text and ("tile" in text or "tiles" in text):
            intent = Intent.PRODUCT_CATEGORY_BROWSE
            entities.category = slug
            entities.product_type = "tiles"
            if keyword in ("bathroom", "kitchen", "outdoor"):
                entities.room = keyword
            confidence = 0.92
            break

    # General product listing
    if intent == Intent.UNKNOWN:
        if re.search(r"\b(show|list|all|sell|have|get)\b.*\btiles?\b", text):
            intent = Intent.PRODUCT_LIST
            entities.product_type = "tiles"
            confidence = 0.88

    # Catalog request
    if intent == Intent.UNKNOWN:
        if re.search(r"\b(catalog|catalogue|collection)\b", text):
            intent = Intent.PRODUCT_CATALOG
            entities.product_type = "tiles"
            confidence = 0.90

    # Product types
    if intent == Intent.UNKNOWN:
        if re.search(r"\b(types?|kinds?|varieties|range)\b.*\b(tile|tiles|offer)\b", text):
            intent = Intent.PRODUCT_TYPES
            entities.product_type = "tiles"
            confidence = 0.89

    # ── 2. SIZE & DIMENSIONS ──
    if intent == Intent.UNKNOWN:
        if re.search(r"\b(size|sizes|dimensions?)\b", text):
            intent = Intent.SIZE_LIST
            entities.product_type = "tiles"
            confidence = 0.88

    if intent == Intent.UNKNOWN:
        for size_key, size_val in SIZE_MAP.items():
            if size_key in text and "tile" in text:
                intent = Intent.SIZE_FILTER
                entities.size = size_key
                entities.product_type = "tiles"
                entities.tag = size_val.get("tag")
                confidence = 0.90
                break

    # ── 3. DISCOUNTS & PROMOTIONS ──
    if intent == Intent.UNKNOWN:
        if re.search(r"\bbulk\s*discount", text):
            intent = Intent.BULK_DISCOUNT
            confidence = 0.91

    if intent == Intent.UNKNOWN:
        if re.search(r"\bclearance\b", text):
            intent = Intent.CLEARANCE_PRODUCTS
            entities.on_sale = True
            entities.tag = "clearance"
            confidence = 0.92

    if intent == Intent.UNKNOWN:
        if re.search(r"\bcoupon\b", text):
            intent = Intent.COUPON_INQUIRY
            confidence = 0.90

    if intent == Intent.UNKNOWN:
        if re.search(r"\b(discount|sale|promo|promotion|deal|offer)\b", text):
            intent = Intent.DISCOUNT_INQUIRY
            entities.on_sale = True
            confidence = 0.87

    # ── 4. ACCOUNT & ORDERING ──
    if intent == Intent.UNKNOWN:
        if re.search(r"\b(save.*later|bookmark)\b", text):
            intent = Intent.SAVE_FOR_LATER
            confidence = 0.85

    if intent == Intent.UNKNOWN:
        if re.search(r"\bwishlist\b", text):
            intent = Intent.WISHLIST
            confidence = 0.90

    if intent == Intent.UNKNOWN:
        if re.search(r"\btrack.*order\b", text):
            intent = Intent.ORDER_TRACKING
            confidence = 0.91

    if intent == Intent.UNKNOWN:
        if re.search(r"\b(status|where).*order\b", text):
            intent = Intent.ORDER_STATUS
            confidence = 0.91

    if intent == Intent.UNKNOWN:
        # Extract order ID if present
        order_match = re.search(r"order\s*#?\s*(\d+)", text)
        if order_match:
            entities.order_id = int(order_match.group(1))

    if intent == Intent.UNKNOWN:
        if re.search(r"\b(order|buy|purchase|add to cart|checkout)\b", text):
            intent = Intent.PLACE_ORDER
            confidence = 0.85
            # Extract product ID if present
            pid_match = re.search(r"product\s*#?\s*(\d+)", text)
            if pid_match:
                entities.product_id = int(pid_match.group(1))
            qty_match = re.search(r"(\d+)\s*(qty|quantity|pcs|pieces|units|boxes)", text)
            if qty_match:
                entities.quantity = int(qty_match.group(1))

    return ClassifiedResult(
        intent=intent,
        entities=entities,
        confidence=confidence,
    )