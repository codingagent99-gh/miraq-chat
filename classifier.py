"""
Intent Classifier — store-agnostic.
All attribute/tag lookups use live StoreLoader data.
All keyword lists come from config/store_config.py — no hardcoded domain terms.
"""

import re
from typing import Optional, List
from models import Intent, ExtractedEntities, ClassifiedResult
from store_registry import get_store_loader
from config.store_config import (
    PRODUCT_TYPE_TERMS,
    ORIGIN_KEYWORDS,
)


def classify(utterance: str) -> ClassifiedResult:
    """Classify user utterance into intent + entities."""
    text = utterance.lower().strip()
    entities = ExtractedEntities()
    intent = Intent.UNKNOWN
    confidence = 0.0

    # ─── Pre-extract common entities ───
    _extract_product_name(text, entities)
    _extract_category(text, entities)      # extract BEFORE attributes so we can mask it

    # Build a scrubbed version of the text with the resolved category name removed.
    # Prevents category words (e.g. "countertop") from being falsely matched as
    # attribute terms (e.g. pa_application value "Countertop") by _extract_attributes.
    attr_text = text
    if entities.category_name:
        attr_text = re.sub(
            rf'\b{re.escape(entities.category_name.lower())}s?\b', ' ', attr_text
        ).strip()
    # Also mask product type terms and store-generic terms so they don't get picked
    # up as attribute values (e.g. pa_product-type: Mosaic) when the user is just
    # describing the product type they want.
    # PRODUCT_TYPE_TERMS covers configured fallback terms (e.g. "tile", "tiles").
    # loader._store_generic_terms covers words auto-derived from category name
    # frequency (e.g. "mosaic" from Mosaics/Tile Floor Mosaics) — no hardcoding needed.
    _loader = get_store_loader()
    _type_mask_terms = set(pt.lower() for pt in PRODUCT_TYPE_TERMS)
    if _loader:
        _type_mask_terms |= _loader._store_generic_terms
    for _pt in _type_mask_terms:
        attr_text = re.sub(rf'\b{re.escape(_pt)}\b', ' ', attr_text).strip()

    _extract_attributes(attr_text, entities)   # runs FIRST — captures structured terms before tag scan
    _extract_tag(text, entities)               # runs SECOND — skips terms already covered by attributes
    _extract_thickness(attr_text, entities)    # numeric fallback only
    _extract_collection_year(text, entities)
    _extract_order_id(text, entities)
    _extract_time_range(text, entities)
    _extract_quantity(text, entities)
    _extract_order_item(text, entities)
    _extract_unresolved_descriptors(text, entities)

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
        r"\b(order|buy|purchase)\b"
        r"|\bwant\s+to\s+(order|buy|purchase|get)\b"
        r"|\bi'?d\s+like\s+to\s+(order|buy|purchase|get)\b",
        text,
    ) and entities.order_item_name and not re.search(
        r"\b(track|tracking|status|where|last|history|previous|past|look|show|search|browse|find|see|display)\b", text
    ):
        intent, confidence = Intent.QUICK_ORDER, 0.93

    # 1. ORDER DETAIL — specific order number mentioned (show me order #1234)
    elif entities.order_id and re.search(
        r"\b(show|view|see|detail|details|info|about|check|open|what\s+is|tell\s+me)\b", text
    ):
        intent, confidence = Intent.ORDER_STATUS, 0.96

    # 1b. ORDER TRACKING & STATUS
    elif re.search(r"\b(track|tracking)\b.*\border\b|\border\b.*\btrack", text):
        intent, confidence = Intent.ORDER_TRACKING, 0.93

    elif re.search(r"\b(status|where)\b.*\border\b|\border\b.*\bstatus\b", text):
        intent, confidence = Intent.ORDER_STATUS, 0.93

    # 2. ORDER HISTORY & LAST ORDER
    # "last 4 orders", "show me 5 orders", "get 3 recent orders"
    elif m := re.search(r"\b(last|recent|past|show|get|fetch|list)\s+(\d+)\s+orders?\b", text):
        intent, confidence = Intent.ORDER_HISTORY, 0.94
        entities.order_count = int(m.group(2))

    # Time-range order history: "orders from last 1 month", "show orders last 3 months"
    elif re.search(r"\border\b", text) and re.search(
        r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text
    ):
        intent, confidence = Intent.ORDER_HISTORY, 0.93

    elif re.search(r"\b(order\s*history|past\s*orders?|previous\s*orders?)\b", text):
        intent, confidence = Intent.ORDER_HISTORY, 0.92

    elif re.search(r"\bwhat\b.*\bordered\b.*\bbefore\b", text):
        intent, confidence = Intent.ORDER_HISTORY, 0.91
        
    # NEW: Catch "check my orders", "show my orders", "view my orders",
    #      "see my orders", "show orders", "view orders"
    #      Allow "last/previous" through only when a time-range phrase is present
    #      (e.g. "show orders from last month" should reach the time-range branch above,
    #       but if it somehow lands here, still classify correctly)
    elif re.search(
        r"\b(check|show|view|see|get|list|display)\b.*\b(my\s+)?orders?\b", text
    ) and not re.search(
        r"\b(track|tracking|status|where)\b", text
    ) and not re.search(
        r"\b(last|latest|most\s+recent|previous)\b", text
    ) or (
        re.search(r"\b(check|show|view|see|get|list|display)\b.*\b(my\s+)?orders?\b", text)
        and re.search(r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text)
    ):
        intent, confidence = Intent.ORDER_HISTORY, 0.92
        
    elif re.search(r"^\s*(my\s+)?orders?\s*[?!.]?\s*$", text):
        intent, confidence = Intent.ORDER_HISTORY, 0.90

    elif re.search(r"\b(last|latest|most\s*recent|previous)\b.*\border\b", text) and not re.search(
        r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text
    ):
        intent, confidence = Intent.LAST_ORDER, 0.94
        entities.order_count = 1

    elif re.search(r"\border\b.*\b(last|latest|most\s*recent|previous)\b", text) and not re.search(
        r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text
    ):
        intent, confidence = Intent.LAST_ORDER, 0.94
        entities.order_count = 1

    elif re.search(r"\bwhat\b.*\b(did\s+i|have\s+i)\b.*\border", text):
        intent, confidence = Intent.LAST_ORDER, 0.93
        entities.order_count = 1

    elif re.search(r"\bmy\s+(last|previous|recent)\s+order\b", text) and not re.search(
        r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text
    ):
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

    elif re.search(r"\b(clearance|discount|sale|deals?|promotions?)\b", text):
        # All sale/discount/clearance/promotion queries share the same WooCommerce
        # on_sale filter — no reason to route them to separate intents.
        intent, confidence = Intent.DISCOUNT_INQUIRY, 0.91
        entities.on_sale = True

    # 3. SAMPLE REQUESTS
    elif re.search(r"\bsample\b", text):
        intent, confidence = Intent.SAMPLE_REQUEST, 0.90

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
    # If user also mentioned a specific product name, treat as product search
    # scoped to category — the category context is preserved in entities for
    # the response. e.g. "show me allspice in countertop" → PRODUCT_SEARCH
    
    
    elif entities.category_id is not None:
        if entities.product_name:
            intent, confidence = Intent.PRODUCT_SEARCH, 0.95
        elif entities.attributes:
            # category + attribute filter (e.g. "exterior tiles in 7/16 thick", "exterior pavers")
            # route to FILTER_BY_ATTRIBUTE with category scope, not plain CATEGORY_BROWSE
            intent, confidence = Intent.FILTER_BY_ATTRIBUTE, 0.92
        elif entities.tag_slugs:  # ← add this branch
            intent, confidence = Intent.FILTER_BY_ATTRIBUTE, 0.92
        else:
            intent, confidence = Intent.CATEGORY_BROWSE, 0.94

    elif re.search(r"\b(what|list|show|all)\b.*\bcategor(y|ies)\b", text):
        intent, confidence = Intent.CATEGORY_LIST, 0.91

    # 8. ATTRIBUTE FILTERS
    # Fully dynamic: works for ANY store attribute without hardcoded label names.
    # FILTER_BY_SIZE and PRODUCT_BY_ORIGIN are the only special cases kept
    # separate because they have custom extraction logic (numeric pattern and
    # demonym synonyms respectively). Everything else collapses into one intent.
    elif entities.attributes.get("origin") and not entities.product_name:
        intent, confidence = Intent.PRODUCT_BY_ORIGIN, 0.88

    elif entities.attributes and not entities.product_name:
        # Single generic handler for ALL other attribute matches — finish, color,
        # colors-2, thickness, application, material, visual, or any future
        # attribute added to the store. api_builder reads e.attribute_slug and
        # e.attributes dynamically, so no code change is needed when the store
        # configuration changes.
        intent, confidence = Intent.FILTER_BY_ATTRIBUTE, 0.89

    # 9. SIZE LIST
    elif re.search(r"\b(what|which)\b.*\bsizes?\b", text):
        intent, confidence = Intent.SIZE_LIST, 0.88

    # 10. COLLECTION YEAR
    elif entities.collection_year:
        intent, confidence = Intent.PRODUCT_BY_COLLECTION, 0.89

    # 11.5. PRODUCT BY TAG — generic tag match
    # Fires when _extract_tag() populated tag_ids but no domain-specific
    # attribute (finish/color/visual/origin/collection/thickness/size/application)
    # was found (those intents already fired above via the elif chain).
    elif entities.tag_ids:
        intent, confidence = Intent.PRODUCT_BY_TAG, 0.88

    # 12. EXPLICIT "show me more/all products" RULE
    # Must be BEFORE product_name check to override generic product matches
    # Catches patterns like "show me more products" even if product_name was extracted
    elif re.search(r"\b(show|list|get|see)\b.*\b(more|all)\b.*\bproducts?\b", text):
        intent, confidence = Intent.PRODUCT_LIST, 0.87

    # 13. PRODUCT SEARCH BY NAME
    elif entities.product_name:
        if re.search(r"\b(tell|about|detail|info|specs?|specification|price|cost|how\s+much)\b", text):
            intent, confidence = Intent.PRODUCT_DETAIL, 0.91
        else:
            intent, confidence = Intent.PRODUCT_SEARCH, 0.92

    # 14. CATALOG / TYPES
    elif re.search(r"\b(catalog|catalogue|collection|range|portfolio)\b", text):
        intent, confidence = Intent.PRODUCT_CATALOG, 0.90

    elif re.search(
        r"\b(types?|kinds?|varieties|categories)\b.*\b(offer|have|sell)\b", text
    ):
        intent, confidence = Intent.PRODUCT_TYPES, 0.89

    # 15. GENERAL PRODUCT LIST (fallback) — uses store-configured product type terms
    else:
        for _pt in PRODUCT_TYPE_TERMS:
            _pt_esc = re.escape(_pt)
            if re.search(rf"\b(show|list|all|sell|have|get|see)\b.*\b{_pt_esc}\b", text):
                intent, confidence = Intent.PRODUCT_LIST, 0.85
                break
            elif re.search(rf"\b{_pt_esc}\b", text):
                intent, confidence = Intent.PRODUCT_LIST, 0.75
                break

    # Final fallback: QUICK_ORDER if order_item_name extracted but nothing matched
    if intent == Intent.UNKNOWN and entities.order_item_name:
        intent, confidence = Intent.QUICK_ORDER, 0.90

    # ── Prevent product_id from hijacking category-scoped searches ──────────
    # When both a category and a product name are resolved, the intent is a
    # filtered catalog search, NOT a "fetch this specific product's variations"
    # call. Clear product_id so api_builder routes to the category-scoped path.
    if entities.category_id is not None and entities.product_id is not None:
        entities.product_id = None

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
            generic_words = {"product", "products", "item", "items"} | set(PRODUCT_TYPE_TERMS)
            if match["name"].lower().strip() in generic_words:
                return
            # Guard: if the only reason this product matched is a token that
            # is also a live tag name, skip it and let _extract_tag() handle it.
            # e.g. "mosaics under the Wilde tag" — "wilde" is a tag, not a product name.
            matched_name_lower = match["name"].lower()
            if matched_name_lower not in text:
                # Matched via token fallback — check if every matching token is a tag name
                text_words = set(re.split(r'[\s\-_/]+', text.lower()))
                product_tokens = set(re.split(r'[\s\-_/]+', matched_name_lower))
                overlapping_tokens = text_words & product_tokens
                tag_names_lower = set(loader.tag_by_name_lower.keys())
                tag_words = {
                    t
                    for tag_name in tag_names_lower
                    for t in re.split(r'[\s\-_/]+', tag_name)
                    if t and len(t) > 2
                }
                if not overlapping_tokens or overlapping_tokens.issubset(tag_words):
                    return  # No overlap, or all matching tokens are tag words — skip product match
            entities.product_name = match["name"]
            entities.product_slug = match.get("slug", "")
            entities.product_id = match.get("id")
            if "mosaic" in text:
                entities.product_slug = f"{match['slug']}-mosaic"
            elif "chip card" in text:
                entities.product_slug = f"{match['slug']}-chip-card"
            elif "ymal" in text:
                entities.product_slug = f"{match['slug']}-ymal"


def _normalize_for_tag_compare(s: str) -> set:
    """
    Strip punctuation/symbols and return a token set for fuzzy slug ↔ term-name comparison.
    e.g. '7/16" thick' → {'7', '16', 'thick'}
         '7-16-thick'  → {'7', '16', 'thick'}
         'white tones' → {'white', 'tones'}
    """
    return set(re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split())


def _extract_attributes(text: str, entities: ExtractedEntities):
    """
    Dynamically match user text against ALL live attribute terms from the store.

    Iterates loader.all_attributes_raw (fetched from /custom-api/v1/all-attributes
    at startup). For each attribute, scans its terms against the user text.
    On match, populates:
      - entities.attributes[label] = matched term name
      - entities.attribute_slug    = attribute taxonomy (e.g. "pa_finish")
      - entities.attribute_term_ids = [term_id]

    Special cases:
      - "origin" attributes: also resolved via ORIGIN_KEYWORDS demonym synonyms
        since WooCommerce terms don't contain "italian", "spanish" etc.
      - Size attributes: numeric pattern "NxM" matched first, then term scan.

    No hardcoded keyword lists. Works for any store.
    """
    loader = get_store_loader()
    if not loader or not loader.all_attributes_raw:
        return

    for attr in loader.all_attributes_raw:
        label = attr.get("attribute_label", "").lower().strip()
        taxonomy = attr.get("taxonomy", "")
        terms = attr.get("terms", [])

        if not label or not taxonomy or not terms:
            continue

        # ── Special case: size attributes — try numeric pattern first ──
        if "size" in label:
            size_match = re.search(r'(\d+)\s*"?\s*(?:x|by|×|X)\s*(\d+)', text)
            if size_match:
                w, h = size_match.group(1), size_match.group(2)
                size_str = f"{w}x{h}"
                term_ids = loader.get_attribute_term_ids(taxonomy, size_str)
                if not term_ids:
                    term_ids = loader.get_attribute_term_ids(taxonomy, f'{w}"x{h}"')
                if term_ids:
                    entities.attributes[label] = f'{w}"x{h}"'
                    entities.attribute_slug = taxonomy
                    entities.attribute_term_ids = term_ids
                    continue

        # ── Special case: origin — resolve demonym synonyms first ──
        if "origin" in label:
            for keyword, normalized in ORIGIN_KEYWORDS.items():
                if re.search(rf"\b{re.escape(keyword)}\b", text):
                    tag_ids = loader.get_tag_ids_for_keyword(normalized)
                    if not tag_ids:
                        tag_ids = loader.get_tag_ids_for_keyword(f"made in {normalized}")
                    entities.attributes[label] = normalized.title()
                    entities.tag_ids.extend(tag_ids)
                    for tid in tag_ids:
                        tag = loader.tag_by_id.get(tid)
                        if tag:
                            entities.tag_slugs.append(tag["slug"])
                    break
            continue

        # ── General case: scan all terms for this attribute ──
        for term in terms:
            term_name = term.get("name", "")
            term_name_lower = term_name.lower().strip()
            if not term_name_lower or len(term_name_lower) < 3:
                continue
            try:
                # Also match plural forms: "pavers" matches term "Paver", "tiles" matches "Tile"
                matched = (
                    re.search(rf"\b{re.escape(term_name_lower)}\b", text)
                    or re.search(rf"\b{re.escape(term_name_lower)}s\b", text)
                    or (
                        len(term_name_lower) > 4
                        and re.search(rf"\b{re.escape(term_name_lower[:-1])}\b", text)
                    )
                )
                if matched:
                    # Suppress this attribute term if it is a strict token subset of a
                    # longer tag name that itself matches the user text.
                    # e.g. term="Black" (tokens: {black}) is a strict subset of tag
                    # "Black Tones" (tokens: {black, tones}) which matches the text
                    # → skip the attribute, let _extract_tag() capture the full tag.
                    # NOTE: entities.tag_slugs is empty at this point (tag extraction
                    # runs after attributes), so we must check live tag store data.
                    term_tokens = _normalize_for_tag_compare(term_name_lower)
                    covered_by_tag = False
                    if term_tokens and loader:
                        for tag_name_lower, tag_entry in loader.tag_by_name_lower.items():
                            if tag_entry.get("count", 0) == 0:
                                continue
                            tag_tokens = _normalize_for_tag_compare(tag_name_lower)
                            # Also match singular form: tag "Gray Tones" suppresses
                            # attribute term "Gray" even when user wrote "gray tone".
                            tag_name_singular = tag_name_lower[:-1] if tag_name_lower.endswith("s") else None
                            if (term_tokens < tag_tokens and (
                                re.search(rf"\b{re.escape(tag_name_lower)}\b", text)
                                or (tag_name_singular and len(tag_name_singular) > 3
                                    and re.search(rf"\b{re.escape(tag_name_singular)}\b", text))
                            )):
                                covered_by_tag = True
                                break
                    if covered_by_tag:
                        break
                    entities.attributes[label] = term_name
                    entities.attribute_slug = taxonomy
                    entities.attribute_term_ids = [term["id"]]
                    break  # first match per attribute wins
            except re.error:
                pass


def _extract_thickness(text: str, entities: ExtractedEntities):
    """
    Thickness is handled by _extract_attributes via live attribute terms.
    This stub exists only as a fallback for numeric patterns not in term names.

    Runs AFTER _extract_tag — skips any numeric thickness value that is already
    represented by a matched tag slug (e.g. tag "7/16\" thick" → slug "7-16-thick").
    This prevents double-filtering where the thickness is stored as a tag rather
    than as a pa_thickness attribute term on some products.
    """
    THICKNESS_PATTERNS = [
        r'(\d+(?:\.\d+)?\s*mm)',
        r'(\d+(?:\.\d+)?\s*cm)',
        r'(\d+/\d+\s*"?\s*(?:inch(?:es)?|in\.?|thick)?)',  # "7/16"", "3/8 inch"
        r'(\d+(?:\.\d+)?(?:\s*"|\s*inch(?:es)?|\s*in\.?))',  # decimal inches: 0.5", 1.25 inch
    ]
    loader = get_store_loader()
    for pattern in THICKNESS_PATTERNS:
        match = re.search(pattern, text)
        if match and "thickness" not in entities.attributes:
            raw = match.group(1).strip()

            # ── Tag-coverage guard ────────────────────────────────────────────
            # If the numeric value extracted here is already captured by a tag
            # slug (e.g. raw="7/16"" → digits="716", slug "7-16-thick" →
            # normalised "716thick"), don't also add it as a pa_thickness filter.
            # The tag is the canonical representation for this product; adding an
            # attribute filter on top would exclude products that use the tag
            # instead of the attribute term to express thickness.
            raw_digits = re.sub(r'[^0-9]', '', raw)  # "7/16"" → "716", "3/8"" → "38"
            already_a_tag = any(
                raw_digits in re.sub(r'[^0-9]', '', slug)
                for slug in entities.tag_slugs
            )
            if already_a_tag:
                return
            # ─────────────────────────────────────────────────────────────────

            entities.attributes["thickness"] = raw
            # find the taxonomy for thickness from live attributes
            if loader:
                for attr in loader.all_attributes_raw:
                    if "thickness" in attr.get("attribute_label", "").lower():
                        entities.attribute_slug = attr["taxonomy"]
                        term_ids = loader.get_attribute_term_ids(attr["taxonomy"], raw)
                        if term_ids:
                            entities.attribute_term_ids = term_ids
                        break
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


def _extract_time_range(text: str, entities: ExtractedEntities):
    """Extract date_after from time range phrases like 'past month', 'last 3 months', 'this year'."""
    from datetime import datetime, timezone, timedelta
    from dateutil.relativedelta import relativedelta
    now = datetime.now(timezone.utc)

    # "last N days/weeks/months/years"
    m = re.search(r'(?:last|past)\s+(\d+)\s+(day|week|month|year)s?', text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == 'day':
            entities.date_after = (now - timedelta(days=n)).strftime('%Y-%m-%dT00:00:00')
        elif unit == 'week':
            entities.date_after = (now - timedelta(weeks=n)).strftime('%Y-%m-%dT00:00:00')
        elif unit == 'month':
            entities.date_after = (now - relativedelta(months=n)).strftime('%Y-%m-%dT00:00:00')
        elif unit == 'year':
            entities.date_after = (now - relativedelta(years=1)).strftime('%Y-%m-%dT00:00:00')
        return

    # "past month" / "last month" (no number)
    if re.search(r'(?:last|past)\s+month', text):
        entities.date_after = (now - relativedelta(months=1)).strftime('%Y-%m-%dT00:00:00')
        return
    if re.search(r'(?:last|past)\s+week', text):
        entities.date_after = (now - timedelta(weeks=1)).strftime('%Y-%m-%dT00:00:00')
        return
    if re.search(r'(?:last|past)\s+year', text):
        entities.date_after = (now - relativedelta(years=1)).strftime('%Y-%m-%dT00:00:00')
        return

    # "this month" / "this year"
    if re.search(r'this\s+month', text):
        entities.date_after = now.replace(day=1).strftime('%Y-%m-%dT00:00:00')
        return
    if re.search(r'this\s+year', text):
        entities.date_after = now.replace(month=1, day=1).strftime('%Y-%m-%dT00:00:00')
        return

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


def _extract_tag(text: str, entities: ExtractedEntities):
    """
    Generic tag extractor: matches user text against all live tags.
    Runs AFTER _extract_attributes — skips any tag whose tokens are already
    covered by a resolved attribute value, preventing double-filtering.
    Uses word-boundary matching to reduce false positives.

    Matches tag name as words, slug-as-words, or raw hyphenated slug
    (so "mosaic-look" in user text matches the "Mosaic Look" tag).

    Collects all matches first, then deduplicates: drops any tag whose token
    set is a strict subset of another matched tag's tokens.
    e.g. "mosaic" {mosaic} ⊂ "mosaic look" {mosaic, look} → "mosaic" dropped.
    """
    loader = get_store_loader()
    if not loader:
        return

    existing_ids = set(entities.tag_ids)

    resolved_attr_token_sets = [
        _normalize_for_tag_compare(v)
        for v in entities.attributes.values()
        if v
    ]

    # Pass 1: collect all candidates
    candidates = []  # list of (tag dict, name_lower)
    for name_lower, tag in loader.tag_by_name_lower.items():
        if tag["id"] in existing_ids:
            continue
        if tag.get("count", 0) == 0:
            continue
        if len(name_lower) < 4:
            continue

        # Suppress if tokens fully covered by a resolved attribute value
        tag_tokens = _normalize_for_tag_compare(name_lower)
        if tag_tokens and any(tag_tokens <= attr_tokens for attr_tokens in resolved_attr_token_sets):
            continue

        matched = False
        # 1. Tag name as words: "mosaic look" matches "mosaic look" in text
        try:
            if re.search(rf'\b{re.escape(name_lower)}\b', text):
                matched = True
        except re.error:
            pass
        # 2. Slug-as-words: "quick-ship" → "quick ship" matches text
        if not matched:
            slug_words = tag["slug"].replace("-", " ")
            if slug_words != name_lower and len(slug_words) >= 4:
                try:
                    if re.search(rf'\b{re.escape(slug_words)}\b', text):
                        matched = True
                except re.error:
                    pass
        # 3. Raw slug: "mosaic-look" in text matches slug "mosaic-look"
        if not matched:
            slug = tag["slug"]
            if len(slug) >= 4:
                try:
                    if re.search(rf'(?<![\w]){re.escape(slug)}(?![\w])', text):
                        matched = True
                except re.error:
                    pass
        # 4. Singular form: "white tone" matches tag "White Tones", "black tone" → "Black Tones"
        # Strips trailing 's' from the tag name and tries again.
        if not matched and name_lower.endswith("s") and len(name_lower) > 4:
            singular = name_lower[:-1]
            try:
                if re.search(rf'\b{re.escape(singular)}\b', text):
                    matched = True
            except re.error:
                pass
        if matched:
            candidates.append((tag, name_lower))

    # Pass 2: deduplicate — drop any tag whose tokens are a strict subset
    # of another matched tag's tokens.
    all_token_sets = [_normalize_for_tag_compare(n) for _, n in candidates]
    for i, (tag, name_lower) in enumerate(candidates):
        my_tokens = all_token_sets[i]
        shadowed = my_tokens and any(
            my_tokens < other_tokens
            for j, other_tokens in enumerate(all_token_sets) if j != i
        )
        if not shadowed:
            entities.tag_ids.append(tag["id"])
            entities.tag_slugs.append(tag["slug"])

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
                "this", "that", "item", "product", "item", "items",
                "some", "the", "a", "an", "my", "again", "more",
                "it", "them", "these", "those", "for", "to", "of",
            } | set(PRODUCT_TYPE_TERMS)
            if candidate not in skip_words and len(candidate) > 2:
                entities.order_item_name = candidate.title()
                return


# ─────────────────────────────────────────────
# UNRESOLVABLE DESCRIPTOR EXTRACTION
# ─────────────────────────────────────────────

# Words that users say but don't map to any store attribute, tag, or category.
# Captured into entities.search_hints so the response generator can acknowledge
# them ("I couldn't filter by 'durable' specifically — here are the closest matches").
# Extend this list as needed — no other file changes required.
_UNRESOLVABLE_DESCRIPTORS = [
    "durable", "heavy-duty", "heavy duty", "premium", "luxury",
    "rustic", "modern", "classic", "natural", "affordable", "budget",
]

def _extract_unresolved_descriptors(text: str, entities: ExtractedEntities):
    """Capture descriptive words that didn't map to any attribute/tag/category."""
    hints = []
    for descriptor in _UNRESOLVABLE_DESCRIPTORS:
        if re.search(rf'\b{re.escape(descriptor.lower())}\b', text):
            already_covered = (
                descriptor.lower() in {v.lower() for v in entities.attributes.values()}
                or any(descriptor.lower() in slug.replace("-", " ") for slug in entities.tag_slugs)
            )
            if not already_covered:
                hints.append(descriptor)
    if hints and hasattr(entities, 'search_hints'):
        entities.search_hints = hints