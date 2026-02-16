"""
Intent Classifier optimized for WGC Tiles Store.
Now includes CATEGORY-AWARE classification and ORDER HISTORY/REORDER.
"""

import re
from typing import Optional
from models import Intent, ExtractedEntities, ClassifiedResult
from store_registry import (
    TAGS, PRODUCT_SERIES, COLOR_MAP, FINISH_MAP,
    VISUAL_MAP, ORIGIN_MAP, SIZE_KEYWORD_MAP, THICKNESS_MAP,
    get_store_loader,
)


def classify(utterance: str) -> ClassifiedResult:
    """Classify user utterance into intent + entities."""
    text = utterance.lower().strip()
    entities = ExtractedEntities()
    intent = Intent.UNKNOWN
    confidence = 0.0

    # ─── Pre-extract common entities ───
    _extract_product_name(text, entities)
    _extract_color(text, entities)
    _extract_finish(text, entities)
    _extract_visual(text, entities)
    _extract_origin(text, entities)
    _extract_size(text, entities)
    _extract_thickness(text, entities)
    _extract_collection_year(text, entities)
    _extract_order_id(text, entities)
    _extract_quantity(text, entities)

    # ★ NEW: Extract category from live WooCommerce data
    _extract_category(text, entities)

    # ★ NEW: Extract order-related entities
    _extract_order_item(text, entities)

    # ─── Intent Classification (priority order) ───

    # ═══════════════════════════════════════════
    # ★ 0. ORDER HISTORY / REORDER (NEW — highest priority for order queries)
    #    Must come BEFORE order tracking/status to avoid misclassification.
    #    "show my last order" → LAST_ORDER (not ORDER_TRACKING)
    #    "repeat my last order" → REORDER
    #    "order this item Ansel" → ORDER_ITEM
    # ═══════════════════════════════════════════

    if re.search(r"\b(repeat|reorder|re-order|order\s*again)\b", text):
        intent, confidence = Intent.REORDER, 0.95
        entities.reorder = True
        entities.order_count = 1

    elif re.search(
        r"\b(order|buy|purchase|want)\b.*\b(this\s+item|this\s+product)?\s*\b",
        text,
    ) and entities.order_item_name and not re.search(
        r"\b(track|tracking|status|where|last|history|previous|past)\b", text
    ):
        intent, confidence = Intent.QUICK_ORDER, 0.93

    # 1. ORDER TRACKING & STATUS (must come before ORDER_HISTORY)
    elif re.search(r"\b(track|tracking)\b.*\border\b|\border\b.*\btrack", text):
        intent, confidence = Intent.ORDER_TRACKING, 0.93

    elif re.search(r"\b(status|where)\b.*\border\b|\border\b.*\bstatus\b", text):
        intent, confidence = Intent.ORDER_STATUS, 0.93

    # 2. ORDER HISTORY & LAST ORDER
    elif re.search(
        r"\b(order\s*history|past\s*orders?|previous\s*orders?)\b", text
    ):
        intent, confidence = Intent.ORDER_HISTORY, 0.92
        entities.order_count = 10

    elif re.search(r"\bwhat\b.*\bordered\b.*\bbefore\b", text):
        intent, confidence = Intent.ORDER_HISTORY, 0.91
        entities.order_count = 10

    elif re.search(r"\b(last|latest|most\s*recent|previous)\b.*\border\b", text):
        intent, confidence = Intent.LAST_ORDER, 0.94
        entities.order_count = 1

    elif re.search(r"\border\b.*\b(last|latest|most\s*recent|previous)\b", text):
        intent, confidence = Intent.LAST_ORDER, 0.94
        entities.order_count = 1

    elif re.search(r"\bwhat\b.*\b(did\s+i|have\s+i)\b.*\border", text):
        intent, confidence = Intent.LAST_ORDER, 0.93
        entities.order_count = 1

    elif re.search(r"\bmy\s+(last|previous|recent)\s+order\b", text):
        intent, confidence = Intent.LAST_ORDER, 0.94
        entities.order_count = 1

    elif re.search(
        r"\b(order|buy|purchase|add to cart|checkout)\b.*\b(this|item|it)\b", text
    ):
        intent, confidence = Intent.PLACE_ORDER, 0.88

    elif re.search(r"\bsave\b.*\blater\b|\bbookmark\b", text):
        intent, confidence = Intent.SAVE_FOR_LATER, 0.87

    elif re.search(r"\bwishlist\b", text):
        intent, confidence = Intent.WISHLIST, 0.91

    # 2. COUPONS & DISCOUNTS
    elif re.search(r"\bcoupon\b|\bpromo\s*code\b|\bdiscount\s*code\b", text):
        intent, confidence = Intent.COUPON_INQUIRY, 0.91

    elif re.search(r"\bbulk\s*discount\b", text):
        intent, confidence = Intent.BULK_DISCOUNT, 0.92

    elif re.search(r"\bclearance\b", text):
        intent, confidence = Intent.CLEARANCE_PRODUCTS, 0.92
        entities.on_sale = True
        entities.tag_slugs.append("clearance")

    elif re.search(r"\b(discount|sale|deals?)\b", text):
        intent, confidence = Intent.DISCOUNT_INQUIRY, 0.88
        entities.on_sale = True

    elif re.search(r"\bpromotions?\b", text):
        intent, confidence = Intent.PROMOTIONS, 0.88
        entities.on_sale = True

    # 3. SAMPLE REQUESTS
    elif re.search(r"\bsample\b", text):
        intent, confidence = Intent.SAMPLE_REQUEST, 0.90

    elif re.search(r"\bchip\s*card\b", text):
        intent, confidence = Intent.CHIP_CARD, 0.92
        entities.tag_slugs.append("chip-card")
        entities.tag_ids.append(48)

    # 4. PRODUCT VARIATIONS
    elif re.search(
        r"\b(colors?|variants?|variations?|options?|finishes)\b.*\b(come|available|does|do)\b",
        text,
    ):
        intent, confidence = Intent.PRODUCT_VARIATIONS, 0.89

    elif entities.product_name and re.search(
        r"\b(colors?|variants?|variations?)\b", text
    ):
        intent, confidence = Intent.PRODUCT_VARIATIONS, 0.89

    # 5. RELATED / YMAL
    elif re.search(
        r"\b(goes?\s*with|pair|complement|match|similar|related|you may also like|ymal)\b",
        text,
    ):
        intent, confidence = Intent.RELATED_PRODUCTS, 0.88

    # 6. QUICK SHIP
    elif re.search(
        r"\bquick\s*ship\b|\bin\s*stock\b|\bavailable\s*now\b|\bimmediate\b", text
    ):
        intent, confidence = Intent.PRODUCT_QUICK_SHIP, 0.91
        entities.quick_ship = True

    # ★ 7. CATEGORY MATCH
    elif entities.category_id is not None:
        intent, confidence = Intent.CATEGORY_BROWSE, 0.94

    # ★ Category listing request
    elif re.search(r"\b(what|list|show|all)\b.*\bcategor(y|ies)\b", text):
        intent, confidence = Intent.CATEGORY_LIST, 0.91

    # 8. ATTRIBUTE FILTERS (only if no category matched)
    elif entities.finish and not entities.product_name:
        intent, confidence = Intent.FILTER_BY_FINISH, 0.89

    elif entities.tile_size:
        intent, confidence = Intent.FILTER_BY_SIZE, 0.90

    elif entities.color_tone and not entities.product_name:
        intent, confidence = Intent.FILTER_BY_COLOR, 0.89

    elif entities.thickness:
        intent, confidence = Intent.FILTER_BY_THICKNESS, 0.88

    elif entities.origin and not entities.product_name:
        intent, confidence = Intent.PRODUCT_BY_ORIGIN, 0.88

    elif entities.application:
        intent, confidence = Intent.FILTER_BY_APPLICATION, 0.87

    # 9. SIZE LIST
    elif re.search(r"\b(what|which)\b.*\bsizes?\b", text):
        intent, confidence = Intent.SIZE_LIST, 0.88

    # 10. VISUAL / LOOK FILTER
    elif entities.visual:
        intent, confidence = Intent.PRODUCT_BY_VISUAL, 0.90

    # 11. COLLECTION YEAR
    elif entities.collection_year:
        intent, confidence = Intent.PRODUCT_BY_COLLECTION, 0.89

    # 12. MOSAIC / TRIM
    elif re.search(r"\bmosaic\b", text):
        intent, confidence = Intent.MOSAIC_PRODUCTS, 0.91
        entities.tag_slugs.append("mosaic-look")

    elif re.search(r"\b(trim|bullnose)\b", text):
        intent, confidence = Intent.TRIM_PRODUCTS, 0.90

    # 13. PRODUCT SEARCH BY NAME
    elif entities.product_name:
        if re.search(r"\b(tell|about|detail|info|specs?|specification)\b", text):
            intent, confidence = Intent.PRODUCT_DETAIL, 0.91
        else:
            intent, confidence = Intent.PRODUCT_SEARCH, 0.92

    # 14. CATALOG / TYPES
    elif re.search(r"\b(catalog|catalogue|collection|range|portfolio)\b", text):
        intent, confidence = Intent.PRODUCT_CATALOG, 0.90

    elif re.search(
        r"\b(types?|kinds?|varieties|categories)\b.*\b(tile|offer|have|sell)\b", text
    ):
        intent, confidence = Intent.PRODUCT_TYPES, 0.89

    # 15. GENERAL PRODUCT LIST (fallback)
    elif re.search(r"\b(show|list|all|sell|have|get|see)\b.*\btiles?\b", text):
        intent, confidence = Intent.PRODUCT_LIST, 0.85

    elif re.search(r"\btiles?\b", text):
        intent, confidence = Intent.PRODUCT_LIST, 0.75

    # ★ NEW: Check if QUICK_ORDER should be used
    # (for queries like "buy Allspice" or "I want to order Waterfall tiles")
    if intent == Intent.UNKNOWN and entities.order_item_name:
        intent, confidence = Intent.QUICK_ORDER, 0.90

    return ClassifiedResult(
        intent=intent,
        entities=entities,
        confidence=confidence,
    )


# ─────────────────────────────────────────────
# ENTITY EXTRACTION HELPERS
# ─────────────────────────────────────────────

def _extract_category(text: str, entities: ExtractedEntities):
    """Check if the user's text matches any WooCommerce category."""
    loader = get_store_loader()
    if loader is None:
        return
    match = loader.get_category_for_text(text)
    if match:
        entities.category_id = match["id"]
        entities.category_name = match["name"]
        entities.category_slug = match.get("slug", "")


def _extract_product_name(text: str, entities: ExtractedEntities):
    for series in PRODUCT_SERIES:
        if series in text:
            entities.product_name = series.title()
            entities.product_slug = series
            if "mosaic" in text:
                entities.product_slug = f"{series}-mosaic"
            elif "chip card" in text:
                entities.product_slug = f"{series}-chip-card"
            elif "ymal" in text:
                entities.product_slug = f"{series}-ymal"
            break


def _extract_color(text: str, entities: ExtractedEntities):
    for keyword, slug in COLOR_MAP.items():
        if keyword in text:
            entities.color_tone = keyword.title()
            entities.tag_slugs.append(slug)
            entities.tag_ids.append(TAGS[slug]["id"])
            break


def _extract_finish(text: str, entities: ExtractedEntities):
    for keyword, slug in FINISH_MAP.items():
        if keyword in text:
            entities.finish = keyword.title()
            entities.tag_slugs.append(slug)
            entities.tag_ids.append(TAGS[slug]["id"])
            break


def _extract_visual(text: str, entities: ExtractedEntities):
    for keyword, slug in VISUAL_MAP.items():
        if keyword in text:
            entities.visual = keyword.title()
            entities.tag_slugs.append(slug)
            entities.tag_ids.append(TAGS[slug]["id"])
            break


def _extract_origin(text: str, entities: ExtractedEntities):
    for keyword, slug in ORIGIN_MAP.items():
        if keyword in text:
            entities.origin = keyword.title()
            entities.tag_slugs.append(slug)
            entities.tag_ids.append(TAGS[slug]["id"])
            break


def _extract_size(text: str, entities: ExtractedEntities):
    size_match = re.search(r'(\d+)\s*[xX×]\s*(\d+)', text)
    if size_match:
        key = f"{size_match.group(1)}x{size_match.group(2)}"
        if key in SIZE_KEYWORD_MAP:
            entities.tile_size = SIZE_KEYWORD_MAP[key]
    for keyword, size_val in SIZE_KEYWORD_MAP.items():
        if keyword in text and not entities.tile_size:
            entities.tile_size = size_val
            break


def _extract_thickness(text: str, entities: ExtractedEntities):
    for keyword, slug in THICKNESS_MAP.items():
        if keyword in text:
            entities.thickness = keyword
            entities.tag_slugs.append(slug)
            entities.tag_ids.append(TAGS[slug]["id"])
            break


def _extract_collection_year(text: str, entities: ExtractedEntities):
    year_match = re.search(r'\b(20[12]\d)\s*(collection|series)?\b', text)
    if year_match:
        year = year_match.group(1)
        entities.collection_year = year
        slug = f"{year}-collection"
        if slug in TAGS:
            entities.tag_slugs.append(slug)
            entities.tag_ids.append(TAGS[slug]["id"])


def _extract_order_id(text: str, entities: ExtractedEntities):
    match = re.search(r'order\s*#?\s*(\d+)', text)
    if match:
        entities.order_id = int(match.group(1))


def _extract_quantity(text: str, entities: ExtractedEntities):
    match = re.search(r'(\d+)\s*(qty|quantity|pcs|pieces|units|boxes|sq\s*ft)', text)
    if match:
        entities.quantity = int(match.group(1))


def _extract_order_item(text: str, entities: ExtractedEntities):
    """
    ★ NEW: Extract a product name from "order/buy/purchase <product>" queries.
    Example: "buy Allspice" → order_item_name = "Allspice"
             "I want to order Waterfall tiles" → order_item_name = "Waterfall"
    """
    # Skip if this is clearly NOT an order query
    if not re.search(r"\b(order|buy|purchase|get|want)\b", text):
        return
    
    # Keywords that indicate order history/tracking rather than new orders
    ORDER_HISTORY_KEYWORDS = r"\b(history|track|tracking|status|before|past|previous|show|tell|about|detail)\b"
    
    # Skip if this is clearly an order history/tracking/show query
    if re.search(ORDER_HISTORY_KEYWORDS, text):
        return
    
    # Match "order/buy/purchase [this item] <product_name>"
    patterns = [
        r"\b(?:order|buy|purchase|get|want)\b.*?\b(?:this\s+item\s+)?([A-Z][a-zA-Z]+)",
        r"\bi\s+want\s+(?:to\s+)?(?:order|buy|purchase|get)\s+(\w+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().lower()
            # Skip common non-product words
            skip_words = {
                "this", "that", "item", "product", "tile", "tiles",
                "some", "the", "a", "an", "my", "again", "more",
                "it", "them", "these", "those",
            }
            if candidate not in skip_words and len(candidate) > 2:
                entities.order_item_name = candidate.title()
                break

    # Also check if a known product series is mentioned WITH order/buy/purchase verbs
    if not entities.order_item_name:
        for series in PRODUCT_SERIES:
            if series in text.lower():
                entities.order_item_name = series.title()
                break