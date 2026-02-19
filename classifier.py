"""
Intent Classifier optimized for WGC Tiles Store.
All attribute/tag lookups use live StoreLoader data — no hardcoded maps.
"""

import re
from typing import Optional, List
from models import Intent, ExtractedEntities, ClassifiedResult
from store_registry import get_store_loader


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
    _extract_sample_size(text, entities)  # BEFORE _extract_size()
    _extract_size(text, entities)
    _extract_thickness(text, entities)
    _extract_application(text, entities)
    _extract_collection_year(text, entities)
    _extract_order_id(text, entities)
    _extract_quantity(text, entities)
    _extract_category(text, entities)
    _extract_order_item(text, entities)

    # ─── Intent Classification (priority order) ───

    # PRIORITY 1: GREETINGS (short unambiguous phrases)
    if re.search(r"^\s*(hi|hello|hey|hiya|howdy|yo|sup)\s*[!.]?\s*$", text):
        intent, confidence = Intent.GREETING, 0.99
    elif re.search(r"^\s*good\s+(morning|afternoon|evening|day)\s*[!.]?\s*$", text):
        intent, confidence = Intent.GREETING, 0.99
    elif re.search(r"^\s*(how\s+are\s+you|how'?s\s+it\s+going|what'?s\s+up)\s*[?!.]?\s*$", text):
        intent, confidence = Intent.GREETING, 0.99
    elif re.search(r"^\s*hi\s+there\s*[!.]?\s*$", text):
        intent, confidence = Intent.GREETING, 0.99
    elif re.search(r"^\s*hey\s+there\s*[!.]?\s*$", text):
        intent, confidence = Intent.GREETING, 0.99

    # PRIORITY 2: ORDER HISTORY / REORDER
    elif re.search(r"\b(repeat|reorder|re-order|order\s*again)\b", text):
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

    # 1. ORDER TRACKING & STATUS
    elif re.search(r"\b(track|tracking)\b.*\border\b|\border\b.*\btrack", text):
        intent, confidence = Intent.ORDER_TRACKING, 0.93

    elif re.search(r"\b(status|where)\b.*\border\b|\border\b.*\bstatus\b", text):
        intent, confidence = Intent.ORDER_STATUS, 0.93

    # 2. ORDER HISTORY & LAST ORDER
    elif re.search(r"\b(order\s*history|past\s*orders?|previous\s*orders?)\b", text):
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

    elif re.search(r"\b(discount|sale|deals?)\b", text):
        intent, confidence = Intent.DISCOUNT_INQUIRY, 0.88
        entities.on_sale = True

    elif re.search(r"\bpromotions?\b", text):
        intent, confidence = Intent.PROMOTIONS, 0.88
        entities.on_sale = True

    # 3. SAMPLE REQUESTS
    elif re.search(r"\bsample\b", text):
        intent, confidence = Intent.SAMPLE_REQUEST, 0.90

    elif re.search(r"\bchip\s*cards?\b", text):
        intent, confidence = Intent.CHIP_CARD, 0.92
        loader = get_store_loader()
        if loader:
            tid = loader.get_chip_card_tag_id()
            if tid:
                entities.tag_ids.append(tid)
                entities.tag_slugs.append("chip-card")

    # 4. MOSAIC / TRIM
    elif re.search(r"\bmosaics?\b", text):
        intent, confidence = Intent.MOSAIC_PRODUCTS, 0.91

    elif re.search(r"\b(trim|bullnose)\b", text):
        intent, confidence = Intent.TRIM_PRODUCTS, 0.90

    # 4b. PRODUCT VARIATIONS
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

    # 7. CATEGORY MATCH
    elif entities.category_id is not None:
        has_attributes = any([
            entities.finish, entities.tile_size, entities.sample_size,
            entities.color_tone, entities.thickness, entities.visual,
            entities.origin, entities.application,
        ])
        if entities.product_name and has_attributes:
            intent, confidence = Intent.PRODUCT_SEARCH_IN_CATEGORY, 0.96
        elif entities.product_name:
            intent, confidence = Intent.PRODUCT_SEARCH_IN_CATEGORY, 0.95
        elif has_attributes:
            intent, confidence = Intent.CATEGORY_BROWSE_FILTERED, 0.95
        else:
            intent, confidence = Intent.CATEGORY_BROWSE, 0.94

    elif re.search(r"\b(what|list|show|all)\b.*\bcategor(y|ies)\b", text):
        intent, confidence = Intent.CATEGORY_LIST, 0.91

    # 8. ATTRIBUTE FILTERS
    elif entities.finish and not entities.product_name:
        intent, confidence = Intent.FILTER_BY_FINISH, 0.89

    elif entities.tile_size:
        intent, confidence = Intent.FILTER_BY_SIZE, 0.90

    elif entities.sample_size:
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

    # 12. EXPLICIT "show me more/all products" RULE
    # Must be BEFORE product_name check to override generic product matches
    # Catches patterns like "show me more products" even if product_name was extracted
    elif re.search(r"\b(show|list|get|see)\b.*\b(more|all)\b.*\bproducts?\b", text):
        intent, confidence = Intent.PRODUCT_LIST, 0.87

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

    # Final fallback: QUICK_ORDER if order_item_name extracted but nothing matched
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
    loader = get_store_loader()
    if not loader:
        return
    match = loader.get_category_for_text(text)
    if match:
        entities.category_id = match["id"]
        entities.category_name = match["name"]
        entities.category_slug = match.get("slug", "")


def _extract_product_name(text: str, entities: ExtractedEntities):
    loader = get_store_loader()
    if loader:
        match = loader.get_product_for_text(text)
        if match:
            # Skip generic words that shouldn't match as product names
            generic_words = {"product", "products", "tile", "tiles", "item", "items"}
            if match["name"].lower().strip() in generic_words:
                return
            entities.product_name = match["name"]
            entities.product_slug = match.get("slug", "")
            entities.product_id = match.get("id")
            if "mosaic" in text:
                entities.product_slug = f"{match['slug']}-mosaic"
            elif "chip card" in text:
                entities.product_slug = f"{match['slug']}-chip-card"
            elif "ymal" in text:
                entities.product_slug = f"{match['slug']}-ymal"


def _extract_color(text: str, entities: ExtractedEntities):
    """
    Match color keywords against live tags.
    Looks for tags whose name contains color tone words.
    e.g. "gray" → finds "Gray Tones" tag, "white" → "White Tones" tag
    """
    # Color keyword → search term for live tag lookup
    COLOR_KEYWORDS = [
        "white", "grey", "gray", "beige", "black", "brown",
        "taupe", "multi", "cream", "ivory", "blue", "green",
        "red", "yellow", "pink", "orange", "purple",
    ]
    loader = get_store_loader()
    for keyword in COLOR_KEYWORDS:
        if re.search(rf"\b{keyword}\b", text):
            entities.color_tone = keyword.title()
            if loader:
                # Find matching tag IDs from live data
                tag_ids = loader.get_tag_ids_for_keyword(keyword)
                entities.tag_ids.extend(tag_ids)
                # Also record slugs for any matched tags
                for tid in tag_ids:
                    tag = loader.tag_by_id.get(tid)
                    if tag:
                        entities.tag_slugs.append(tag["slug"])
            break


def _extract_finish(text: str, entities: ExtractedEntities):
    """
    Match finish keywords against live pa_finish attribute terms.
    Falls back to tag search if attribute terms not found.
    """
    FINISH_KEYWORDS = {
        "matte": "matte", "matt": "matte", "matte finish": "matte",
        "polished": "polished", "glossy": "polished", "gloss": "polished",
        "honed": "honed", "satin": "satin", "lappato": "lappato",
        "structured": "structured", "textured": "textured",
        "natural": "natural", "brushed": "brushed",
    }
    loader = get_store_loader()
    for keyword, normalized in FINISH_KEYWORDS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            entities.finish = normalized.title()
            entities.attribute_slug = "pa_finish"
            if loader:
                term_ids = loader.get_attribute_term_ids("pa_finish", normalized)
                if term_ids:
                    entities.attribute_term_ids = term_ids
                else:
                    # Fallback: tag search
                    tag_ids = loader.get_tag_ids_for_keyword(keyword)
                    entities.tag_ids.extend(tag_ids)
            break


def _extract_visual(text: str, entities: ExtractedEntities):
    """Match visual/look keywords against live pa_visual attribute terms and tags."""
    VISUAL_KEYWORDS = {
        "stone": "stone", "marble": "marble", "mosaic": "mosaic",
        "terrazzo": "terrazzo", "gauge": "gauge panel",
        "pattern": "pattern", "decor": "decor", "shape": "shapes",
        "metallic": "metallic", "concrete": "concrete", "wood": "wood",
        "travertine": "travertine", "slate": "slate",
    }
    loader = get_store_loader()
    for keyword, normalized in VISUAL_KEYWORDS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            entities.visual = normalized.title()
            if loader:
                # Try attribute terms first
                term_ids = loader.get_attribute_term_ids("pa_visual", normalized)
                if term_ids:
                    entities.attribute_slug = "pa_visual"
                    entities.attribute_term_ids = term_ids
                else:
                    # Fall back to tag search
                    tag_ids = loader.get_tag_ids_for_keyword(keyword)
                    entities.tag_ids.extend(tag_ids)
                    for tid in tag_ids:
                        tag = loader.tag_by_id.get(tid)
                        if tag:
                            entities.tag_slugs.append(tag["slug"])
            break


def _extract_origin(text: str, entities: ExtractedEntities):
    """Match origin keywords against live tags."""
    ORIGIN_KEYWORDS = {
        "italy": "italy", "italian": "italy",
        "turkey": "turkey", "turkish": "turkey",
        "spain": "spain", "spanish": "spain",
        "china": "china", "chinese": "china",
        "india": "india", "indian": "india",
        "portugal": "portugal", "portuguese": "portugal",
    }
    loader = get_store_loader()
    for keyword, normalized in ORIGIN_KEYWORDS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            entities.origin = normalized.title()
            if loader:
                tag_ids = loader.get_tag_ids_for_keyword(normalized)
                # Also try "made in X"
                if not tag_ids:
                    tag_ids = loader.get_tag_ids_for_keyword(f"made in {normalized}")
                entities.tag_ids.extend(tag_ids)
                for tid in tag_ids:
                    tag = loader.tag_by_id.get(tid)
                    if tag:
                        entities.tag_slugs.append(tag["slug"])
            break


def _extract_sample_size(text: str, entities: ExtractedEntities):
    """
    Extract sample size from user text when 'sample' keyword is present.
    Resolves to pa_sample-size attribute.
    Handles: "sample size 12x24", "12x24 sample", "small sample", "large sample"
    """
    # Only trigger if 'sample' is in the text
    if not re.search(r'\bsample\b', text):
        return
    
    loader = get_store_loader()

    # 1. Numeric size pattern: "12x24", "12 x 24", "12 by 24"
    size_match = re.search(r'(\d+)\s*(?:x|by|×|X)\s*(\d+)', text)
    if size_match:
        w, h = size_match.group(1), size_match.group(2)
        size_str = f"{w}x{h}"
        entities.sample_size = f'{w}"x{h}"' if len(w) > 1 else size_str
        entities.attribute_slug = "pa_sample-size"
        if loader:
            term_ids = loader.get_attribute_term_ids("pa_sample-size", size_str)
            if not term_ids:
                # Try with quotes e.g. "12\"x24\""
                term_ids = loader.get_attribute_term_ids("pa_sample-size", f'{w}"x{h}"')
            entities.attribute_term_ids = term_ids
        return

    # 2. Descriptive sample size keywords
    SAMPLE_SIZE_KEYWORDS = {
        "small sample": ["small", "6x", "3x"],
        "large sample": ["large", "12x", "18x"],
        "medium sample": ["medium", "9x"],
    }
    if loader:
        all_terms = loader.get_all_attribute_terms("pa_sample-size")
        for phrase, hints in SAMPLE_SIZE_KEYWORDS.items():
            if re.search(rf"\b{re.escape(phrase)}\b", text):
                matched_ids = []
                for term in all_terms:
                    term_name = term.get("name", "").lower()
                    if any(h in term_name for h in hints):
                        matched_ids.append(term["id"])
                if matched_ids:
                    entities.sample_size = phrase.title()
                    entities.attribute_slug = "pa_sample-size"
                    entities.attribute_term_ids = matched_ids
                    return


def _extract_size(text: str, entities: ExtractedEntities):
    """
    Extract tile size from user text and resolve to live pa_tile-size term IDs.
    Handles: "24x48", "24 by 48", "24x48 tiles", "large format", "large", "small"
    Skips extraction if sample_size is already populated.
    """
    # Skip if sample_size is already set
    if entities.sample_size:
        return
    
    loader = get_store_loader()

    # 1. Numeric size pattern: "24x48", "24 x 48", "24 by 48", "24×48"
    size_match = re.search(r'(\d+)\s*(?:x|by|×|X)\s*(\d+)', text)
    if size_match:
        w, h = size_match.group(1), size_match.group(2)
        size_str = f"{w}x{h}"
        entities.tile_size = f'{w}"x{h}"' if len(w) > 1 else size_str
        entities.attribute_slug = "pa_tile-size"
        if loader:
            term_ids = loader.get_attribute_term_ids("pa_tile-size", size_str)
            if not term_ids:
                # Try with quotes e.g. "24\"x48\""
                term_ids = loader.get_attribute_term_ids("pa_tile-size", f'{w}"x{h}"')
            entities.attribute_term_ids = term_ids
        return

    # 2. Descriptive size keywords — search live terms
    SIZE_KEYWORDS = {
        "large format": ["large", "48", "110"],
        "large": ["48x48", "48x110", "large"],
        "small": ["small", "12x", "mosaic"],
        "extra large": ["extra large", "large format"],
        "medium": ["medium", "24x"],
    }
    if loader:
        all_terms = loader.get_all_attribute_terms("pa_tile-size")
        for phrase, hints in SIZE_KEYWORDS.items():
            if re.search(rf"\b{re.escape(phrase)}\b", text):
                matched_ids = []
                for term in all_terms:
                    term_name = term.get("name", "").lower()
                    if any(h in term_name for h in hints):
                        matched_ids.append(term["id"])
                if matched_ids:
                    entities.tile_size = phrase.title()
                    entities.attribute_slug = "pa_tile-size"
                    entities.attribute_term_ids = matched_ids
                    return


def _extract_thickness(text: str, entities: ExtractedEntities):
    """Match thickness values against live pa_thickness attribute terms."""
    THICKNESS_PATTERNS = [
        r'(\d+(?:\.\d+)?\s*mm)',
        r'(\d+/\d+"?)',   # e.g. "7/16" or "11/32""
        r'(\d+(?:\.\d+)?\s*cm)',
    ]
    loader = get_store_loader()
    for pattern in THICKNESS_PATTERNS:
        match = re.search(pattern, text)
        if match:
            raw = match.group(1).strip()
            entities.thickness = raw
            entities.attribute_slug = "pa_thickness"
            if loader:
                term_ids = loader.get_attribute_term_ids("pa_thickness", raw)
                if term_ids:
                    entities.attribute_term_ids = term_ids
                else:
                    # Also search live tags for thickness
                    tag_ids = loader.get_tag_ids_for_keyword(raw)
                    entities.tag_ids.extend(tag_ids)
            return


def _extract_application(text: str, entities: ExtractedEntities):
    """
    NEW: Match application/use keywords against live pa_application attribute terms.
    e.g. "interior wall", "floor", "outdoor", "countertop"
    """
    APPLICATION_KEYWORDS = [
        "interior wall", "exterior wall",
        "interior floor", "exterior floor",
        "wall and floor", "floor and wall",
        "countertop", "counter top",
        "bathroom", "kitchen", "outdoor",
        "interior", "exterior",
        "floor", "wall",
        "pool", "shower", "backsplash",
    ]
    loader = get_store_loader()
    # Try longest match first
    for keyword in APPLICATION_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            entities.application = keyword.title()
            entities.attribute_slug = "pa_application"
            if loader:
                term_ids = loader.get_attribute_term_ids("pa_application", keyword)
                if term_ids:
                    entities.attribute_term_ids = term_ids
            return


def _extract_collection_year(text: str, entities: ExtractedEntities):
    """Match collection year against live tags."""
    loader = get_store_loader()
    year_match = re.search(r'\b(20[12]\d)\s*(collection|series)?\b', text)
    if year_match:
        year = year_match.group(1)
        entities.collection_year = year
        if loader:
            tag_ids = loader.get_tag_ids_for_keyword(year)
            entities.tag_ids.extend(tag_ids)
            for tid in tag_ids:
                tag = loader.tag_by_id.get(tid)
                if tag:
                    entities.tag_slugs.append(tag["slug"])


def _extract_order_id(text: str, entities: ExtractedEntities):
    match = re.search(r'order\s*#?\s*(\d+)', text)
    if match:
        entities.order_id = int(match.group(1))


def _extract_quantity(text: str, entities: ExtractedEntities):
    # Primary: number + unit keyword
    match = re.search(r'(\d+)\s*(qty|quantity|pcs|pieces|units|boxes|sq\s*ft)', text)
    if match:
        entities.quantity = int(match.group(1))
        return
    # Fallback: "order/buy/purchase for N" or "place an order for N"
    match = re.search(r'\b(?:order|buy|purchase|place\s+(?:an?\s+)?order)(?:\s+for)?\s+(\d+)\b', text)
    if match:
        entities.quantity = int(match.group(1))
        return
    # Fallback: "N of this/these/them/it"
    match = re.search(r'\b(\d+)\s+of\s+(?:this|these|them|it|the)\b', text)
    if match:
        entities.quantity = int(match.group(1))


def _extract_order_item(text: str, entities: ExtractedEntities):
    """Extract a product name from order/buy/purchase queries."""
    if not re.search(r"\b(order|buy|purchase|get|want)\b", text):
        return

    ORDER_HISTORY_KEYWORDS = r"\b(history|track|tracking|status|before|past|previous|show|tell|about|detail)\b"
    if re.search(ORDER_HISTORY_KEYWORDS, text):
        return

    # First, try to match against known products from StoreLoader
    loader = get_store_loader()
    if loader:
        match = loader.get_product_for_text(text)
        if match:
            entities.order_item_name = match["name"]
            return

    # Fallback: extract product name from patterns
    patterns = [
        r"\b(?:order|buy|purchase|get|want)\b.*?\b(?:this\s+item\s+)?([A-Z][a-zA-Z]+)",
        r"\bi\s+want\s+(?:to\s+)?(?:order|buy|purchase|get)\s+(\w+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().lower()
            skip_words = {
                "this", "that", "item", "product", "tile", "tiles",
                "some", "the", "a", "an", "my", "again", "more",
                "it", "them", "these", "those", "for", "to", "of",
            }
            if candidate not in skip_words and len(candidate) > 2:
                entities.order_item_name = candidate.title()
                return