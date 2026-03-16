"""
Intent Classifier — store-agnostic.
Implements a Chain of Responsibility (Pipeline) for Intent Resolution.
"""

import re
from abc import ABC, abstractmethod
from typing import Optional, List, Tuple
from models import Intent, ExtractedEntities, ClassifiedResult
from store_registry import get_store_loader
from config.store_config import (
    PRODUCT_TYPE_TERMS,
    ORIGIN_KEYWORDS,
    GENERIC_NOISE_WORDS
)
from chat_logger import get_logger

logger = get_logger("miraq_chat")

# ─────────────────────────────────────────────
# INTENT EVALUATOR PIPELINE (Chain of Responsibility)
# ─────────────────────────────────────────────

class IntentEvaluator(ABC):
    """Abstract base class for all intent evaluators."""
    @abstractmethod
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        """Returns (Intent, confidence) if a match is found, else (None, 0.0)."""
        pass

class OrderActionEvaluator(IntentEvaluator):
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if re.search(r"\b(repeat|reorder|re-order|order\s*again)\b", text):
            entities.reorder = True
            entities.order_count = 1
            return Intent.REORDER, 0.95
            
        if re.search(r"\b(order|buy|purchase)\b|\bwant\s+to\s+(order|buy|purchase|get)\b|\bi'?d\s+like\s+to\s+(order|buy|purchase|get)\b", text) and \
           entities.order_item_name and not re.search(r"\b(track|tracking|status|where|last|history|previous|past|look|show|search|browse|find|see|display)\b", text):
            return Intent.QUICK_ORDER, 0.93

        if entities.order_id and re.search(r"\b(show|view|see|detail|details|info|about|check|open|what\s+is|tell\s+me)\b", text):
            return Intent.ORDER_STATUS, 0.96

        if re.search(r"\b(track|tracking)\b.*\border\b|\border\b.*\btrack", text):
            return Intent.ORDER_TRACKING, 0.93

        if re.search(r"\b(status|where)\b.*\border\b|\border\b.*\bstatus\b", text):
            return Intent.ORDER_STATUS, 0.93

        if m := re.search(r"\b(last|recent|past|show|get|fetch|list)\s+(\d+)\s+orders?\b", text):
            entities.order_count = int(m.group(2))
            return Intent.ORDER_HISTORY, 0.94

        if re.search(r"\border\b", text) and re.search(r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text):
            return Intent.ORDER_HISTORY, 0.93

        if re.search(r"\b(order\s*history|past\s*orders?|previous\s*orders?)\b", text):
            return Intent.ORDER_HISTORY, 0.92

        if re.search(r"\bwhat\b.*\bordered\b.*\bbefore\b", text):
            return Intent.ORDER_HISTORY, 0.91
            
        if re.search(r"\b(check|show|view|see|get|list|display)\b.*\b(my\s+)?orders?\b", text) and not re.search(r"\b(track|tracking|status|where)\b", text) and not re.search(r"\b(last|latest|most\s+recent|previous)\b", text) or \
           (re.search(r"\b(check|show|view|see|get|list|display)\b.*\b(my\s+)?orders?\b", text) and re.search(r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text)):
            return Intent.ORDER_HISTORY, 0.92
            
        if re.search(r"^\s*(my\s+)?orders?\s*[?!.]?\s*$", text):
            return Intent.ORDER_HISTORY, 0.90

        if re.search(r"\b(last|latest|most\s*recent|previous)\b.*\border\b", text) and not re.search(r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text):
            entities.order_count = 1
            return Intent.LAST_ORDER, 0.94

        if re.search(r"\border\b.*\b(last|latest|most\s*recent|previous)\b", text) and not re.search(r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text):
            entities.order_count = 1
            return Intent.LAST_ORDER, 0.94

        if re.search(r"\bwhat\b.*\b(did\s+i|have\s+i)\b.*\border", text):
            entities.order_count = 1
            return Intent.LAST_ORDER, 0.93

        if re.search(r"\bmy\s+(last|previous|recent)\s+order\b", text) and not re.search(r"\b(last|past)\s+\d*\s*(day|week|month|year)s?\b", text):
            entities.order_count = 1
            return Intent.LAST_ORDER, 0.94

        if re.search(r"\b(order|buy|purchase|add to cart|checkout)\b.*\b(this|item|it)\b", text):
            return Intent.PLACE_ORDER, 0.88
            
        return None, 0.0


class AccountActionsEvaluator(IntentEvaluator):
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if re.search(r"\bsave\b.*\blater\b|\bbookmark\b", text):
            return Intent.SAVE_FOR_LATER, 0.87
        if re.search(r"\bwishlist\b", text):
            return Intent.WISHLIST, 0.91
        if entities.customer_updates or entities.billing_updates or entities.shipping_updates:
            return Intent.UPDATE_CUSTOMER, 0.93
        if entities.customer_fields_requested:
            return Intent.FETCH_CUSTOMER, 0.93
        return None, 0.0


class DiscountEvaluator(IntentEvaluator):
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if re.search(r"\bcoupon\b|\bpromo\s*code\b|\bdiscount\s*code\b", text):
            return Intent.COUPON_INQUIRY, 0.91
        if re.search(r"\bbulk\s*discount\b", text):
            return Intent.BULK_DISCOUNT, 0.92
        if re.search(r"\b(clearance|discount|sale|deals?|promotions?)\b", text):
            entities.on_sale = True
            return Intent.DISCOUNT_INQUIRY, 0.91
        return None, 0.0


class ProductDetailEvaluator(IntentEvaluator):
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if re.search(r"\bsamples?\b", text) and (entities.product_name or re.search(r"\bsample\s+size\b", text) or re.search(r'\d+\s*"?\s*(?:x|by|×)\s*\d+', text)):
            return Intent.SAMPLE_REQUEST, 0.93
            
        if entities.product_name and re.search(r"\bsamples?\b", text):
            entities.target_attribute = "sample size"
            return Intent.PRODUCT_ATTRIBUTE_INFO, 0.93

        # PRODUCT ATTRIBUTE INFO
        if entities.product_name and re.search(r"\b(what|which)\b.*\b(available|come|have|does|do|offer)\b", text):
            _loader_ref = get_store_loader()
            if _loader_ref and _loader_ref.all_attributes_raw:
                _matched_label = None
                for _attr in _loader_ref.all_attributes_raw:
                    _label = _attr.get("attribute_label", "").lower().strip()
                    if not _label: continue
                    _words = _label.split()
                    if len(_words) > 1:
                        if re.search(r"\b" + r"\s+".join(re.escape(w) for w in _words) + r"s?\b", text):
                            _matched_label = _label
                            break
                        if _words[-1].endswith("s") and len(_words[-1]) > 3:
                            if re.search(r"\b" + r"\s+".join(re.escape(w) for w in _words[:-1]) + r"\s+" + re.escape(_words[-1][:-1]) + r"\b", text):
                                _matched_label = _label
                                break
                    else:
                        if _label_word_matches(_label, text):
                            _matched_label = _label
                            break
                if not _matched_label:
                    for _attr in _loader_ref.all_attributes_raw:
                        _label = _attr.get("attribute_label", "").lower().strip()
                        if not _label: continue
                        for _word in _label.split():
                            if len(_word) >= 4 and _label_word_matches(_word, text):
                                _matched_label = _label
                                break
                        if _matched_label: break
                if _matched_label:
                    entities.target_attribute = _matched_label
                    return Intent.PRODUCT_ATTRIBUTE_INFO, 0.91

        # PRODUCT VARIATIONS
        if re.search(r"\b(colors?|variants?|variations?|options?|finishes|sizes)\b.*\b(come|available|does|do)\b", text):
            return Intent.PRODUCT_VARIATIONS, 0.89
        if entities.product_name and re.search(r"\b(colors?|variants?|variations?|sizes)\b", text):
            return Intent.PRODUCT_VARIATIONS, 0.89
            
        # RELATED / YMAL
        if re.search(r"\b(goes?\s*with|pair|complement|match|similar|related|you may also like|ymal)\b", text):
            return Intent.RELATED_PRODUCTS, 0.88
            
        # QUICK SHIP
        if re.search(r"\bquick\s*ship\b|\bin\s*stock\b|\bavailable\s*now\b|\bimmediate\b", text):
            entities.quick_ship = True
            return Intent.PRODUCT_QUICK_SHIP, 0.91
            
        return None, 0.0


class CatalogSearchEvaluator(IntentEvaluator):
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        # If we have a product AND an attribute (like 'Ansel' + '3x3'), it's a specific product search
        if entities.product_id and entities.attributes:
            return Intent.PRODUCT_SEARCH, 0.93
        
        # Use the new target_category_slugs set
        if getattr(entities, 'target_category_slugs', set()):
            if entities.product_name:
                if re.search(r"\b(tell|about|detail|info|specs?|specification|price|cost|how\s+much)\b", text):
                    return Intent.PRODUCT_DETAIL, 0.91
                else:
                    return Intent.PRODUCT_SEARCH, 0.92
            elif entities.attributes or entities.tag_slugs:
                return Intent.FILTER_BY_ATTRIBUTE, 0.92
            else:
                return Intent.CATEGORY_BROWSE, 0.94

        if re.search(r"\b(what|list|show|all)\b.*\bcategor(y|ies)\b", text):
            return Intent.CATEGORY_LIST, 0.91

        if entities.attributes.get("origin") and not entities.product_name:
            return Intent.PRODUCT_BY_ORIGIN, 0.88

        if entities.attributes and not entities.product_name:
            return Intent.FILTER_BY_ATTRIBUTE, 0.89

        if re.search(r"\b(what|which)\b.*\bsizes?\b", text):
            return Intent.SIZE_LIST, 0.88

        if entities.collection_year:
            return Intent.PRODUCT_BY_COLLECTION, 0.89

        if entities.tag_ids:
            return Intent.PRODUCT_BY_TAG, 0.88

        if re.search(r"\b(show|list|get|see)\b.*\b(more|all)\b.*\bproducts?\b", text):
            return Intent.PRODUCT_LIST, 0.87

        return None, 0.0


class GeneralFallbackEvaluator(IntentEvaluator):
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if re.search(r"\b(catalog|catalogue|collection|range|portfolio)\b", text):
            return Intent.PRODUCT_CATALOG, 0.90

        if re.search(r"\b(types?|kinds?|varieties|categories)\b.*\b(offer|have|sell)\b", text):
            return Intent.PRODUCT_TYPES, 0.89

        for _pt in PRODUCT_TYPE_TERMS:
            _pt_esc = re.escape(_pt)
            if re.search(rf"\b(show|list|all|sell|have|get|see)\b.*\b{_pt_esc}\b", text):
                return Intent.PRODUCT_LIST, 0.85
            elif re.search(rf"\b{_pt_esc}\b", text):
                return Intent.PRODUCT_LIST, 0.75

        if entities.order_item_name:
            return Intent.QUICK_ORDER, 0.90

        return Intent.UNKNOWN, 0.0


class ClassifierPipeline:
    """Manages the execution of Intent Evaluators in priority sequence."""
    def __init__(self, evaluators: List[IntentEvaluator]):
        self.evaluators = evaluators

    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Intent, float]:
        logger.debug(f"ClassifierPipeline: Starting evaluation for text={text!r}")
        
        for evaluator in self.evaluators:
            evaluator_name = evaluator.__class__.__name__
            
            # Evaluate the text
            intent, confidence = evaluator.evaluate(text, entities)
            
            if intent is not None:
                logger.info(
                    f"ClassifierPipeline: 🎯 Match found by {evaluator_name} "
                    f"-> intent={intent.value} (conf={confidence})"
                )
                return intent, confidence
            else:
                logger.debug(f"ClassifierPipeline: ⏭️ {evaluator_name} passed.")
                
        logger.warning(
            f"ClassifierPipeline: ⚠️ Chain exhausted with no match. "
            f"Defaulting to UNKNOWN for text={text!r}"
        )
        return Intent.UNKNOWN, 0.0

def _normalize_dimension(val: str) -> str:
    """Strips quotes, spaces, and unit strings to get the raw dimensional number."""
    clean = re.sub(r'["\'\s]', '', val.lower())
    clean = re.sub(r'(mm|cm|inch|inches|in\.?|thick|weight|lbs?|oz|kg|g)$', '', clean)
    return clean

# ─────────────────────────────────────────────
# MAIN CLASSIFY FUNCTION
# ─────────────────────────────────────────────

def classify(utterance: str) -> ClassifiedResult:
    """Classify user utterance into intent + entities using the Evaluation Pipeline."""
    text = utterance.lower().strip()
    entities = ExtractedEntities()
    
    # --- 1. NEGATIVE CONSTRAINTS (Positional Masking) ---
    text = _extract_exclusions(text, entities)
    
    # ═════════════════════════════════════════════════════════
    # 1.5 NOISE REDUCTION (Generic Word Masking)
    # ═════════════════════════════════════════════════════════
    entity_text = text
    for gw in GENERIC_NOISE_WORDS:
        entity_text = re.sub(rf'\b{re.escape(gw)}\b', ' ', entity_text, flags=re.IGNORECASE)

    # ─── 2. PRE-EXTRACT COMMON ENTITIES ───
    _extract_product_name(entity_text, entities)
    _extract_category(entity_text, entities)

    attr_text = entity_text

    _loader = get_store_loader()
    _type_mask_terms = set(pt.lower() for pt in PRODUCT_TYPE_TERMS)
    if _loader:
        _type_mask_terms |= _loader._store_generic_terms
    for _pt in _type_mask_terms:
        attr_text = re.sub(rf'\b{re.escape(_pt)}\b', ' ', attr_text).strip()

    # --- 3. POSITIVE CONSTRAINTS (Non-Destructive Extraction) ---
    _extract_attributes(attr_text, entities)
    _extract_tag(attr_text, entities)               
    
    # --- 4. SECONDARY EXTRACTIONS ---
    _extract_collection_year(text, entities)
    _extract_order_id(text, entities)
    _extract_time_range(text, entities)
    _extract_quantity(text, entities)
    _extract_order_item(text, entities)
    _extract_unresolved_descriptors(text, entities)
    _extract_price_range(text, entities)       
    _extract_customer_updates(text, entities)  
    _detect_tag_operator(text, entities)       
    _extract_customer_fetch(text, entities)

    # ─── 5. EXECUTE INTENT PIPELINE ───
    pipeline = ClassifierPipeline([
        OrderActionEvaluator(),
        DiscountEvaluator(),
        ProductDetailEvaluator(),
        AccountActionsEvaluator(),
        CatalogSearchEvaluator(),
        GeneralFallbackEvaluator()
    ])

    intent, confidence = pipeline.evaluate(text, entities)

    # ─── 6. HIJACK PREVENTION ───
    PRODUCT_SPECIFIC_INTENTS = {
        Intent.PRODUCT_VARIATIONS, Intent.PRODUCT_DETAIL, Intent.PRODUCT_SEARCH,
        Intent.SIZE_LIST, Intent.PRODUCT_ATTRIBUTE_INFO, Intent.QUICK_ORDER,
        Intent.PLACE_ORDER, Intent.ORDER_ITEM, Intent.SAMPLE_REQUEST
    }
    
    if getattr(entities, 'target_category_slugs', set()) and entities.product_id is not None:
        if intent in PRODUCT_SPECIFIC_INTENTS:
            entities.target_category_slugs.clear()
            entities.category_name = None
        else:
            entities.product_id = None
    
    if entities.tag_ids and entities.product_id:
        is_series = any("series" in slug or "collection" in slug for slug in entities.tag_slugs)
        if is_series:
            logger.info(f"Conflict detected: Series tag found. Dropping product_id {entities.product_id}")
            if not getattr(entities, 'target_category_slugs', set()):
                _extract_category(text, entities)
                if entities.category_name:
                    logger.info(f"Restored category '{entities.category_name}' after product drop.")
            
            entities.product_id = None
            entities.product_name = None
            entities.product_slug = None
            
    # ══════════════════════════════════════════════════════════════════
    # 7. ENTITY CONSOLIDATION (The "OR" Enabler)
    # ══════════════════════════════════════════════════════════════════
    if entities.attr_tag_or_pairs:
        loader = get_store_loader()
        
        # Deduplicate Tags
        handled_tags = {pair.get("tag_slug") for pair in entities.attr_tag_or_pairs if pair.get("tag_slug")}
        if handled_tags:
            entities.tag_slugs = [slug for slug in entities.tag_slugs if slug not in handled_tags]
            entities.tag_ids = [tid for tid in entities.tag_ids if loader and loader.tag_by_id.get(tid, {}).get("slug") not in handled_tags]

        # Deduplicate Categories
        handled_cats = {pair.get("attr_term") for pair in entities.attr_tag_or_pairs if pair.get("attr_taxonomy") == "product_cat"}
        if handled_cats and getattr(entities, 'target_category_slugs', set()).intersection(handled_cats):
            entities.target_category_slugs.clear()
            entities.category_name = None
            
    # Check for Category vs Attribute overlaps explicitly
    if getattr(entities, 'target_category_slugs', set()) and entities.attributes:
        loader = get_store_loader()
        for attr_label, attr_slug in list(entities.attributes.items()):
            # Did the attribute specifically overlap with one of our category slugs?
            if attr_slug in entities.target_category_slugs or attr_slug == (entities.category_name or "").lower():
                
                # Resolve the correct taxonomy for this attribute
                actual_tax = ""
                if loader and loader.all_attributes_raw:
                    for a in loader.all_attributes_raw:
                        if a.get("attribute_label", "").lower().strip() == attr_label:
                            actual_tax = a.get("taxonomy", "")
                            break
                            
                if actual_tax:
                    # Bundle ONLY the overlapping slug into an OR pair!
                    entities.attr_tag_or_pairs.append({
                        "cat_slugs": [attr_slug],  # Just the overlapping word!
                        "attr_taxonomy": actual_tax,
                        "attr_term": attr_slug
                    })
                    
                    # Remove ONLY the overlapping slug from the strict category set
                    if attr_slug in entities.target_category_slugs:
                        entities.target_category_slugs.remove(attr_slug)
                        
                    # If that was the only category, clear the UI name
                    if not entities.target_category_slugs:
                        entities.category_name = None
                        
                    del entities.attributes[attr_label]

    # ─── NEW: ECLIPSED ENTITY CLEANUP ───
    # If we extracted an exact tag (like "gray-tones"), we must delete any smaller 
    # OR pairs or attributes (like "gray") that are fully eclipsed by it.
    if entities.tag_slugs:
        loader = get_store_loader()
        exact_tag_tokens = []
        if loader:
            for tslug in entities.tag_slugs:
                tag_obj = loader.tag_by_slug.get(tslug)
                if tag_obj:
                    exact_tag_tokens.append(_normalize_for_tag_compare(tag_obj.get("name", "")))
        
        if exact_tag_tokens:
            # 1. Clean eclipsed OR pairs (e.g. "gray-look")
            if entities.attr_tag_or_pairs:
                valid_pairs = []
                for pair in entities.attr_tag_or_pairs:
                    attr_term_tokens = _normalize_for_tag_compare(pair.get("attr_term", "").replace("-", " "))
                    # If the term (e.g. {"gray"}) is a subset of the exact tag (e.g. {"gray", "tones"}), drop it!
                    if not any(attr_term_tokens <= exact_tokens for exact_tokens in exact_tag_tokens):
                        valid_pairs.append(pair)
                entities.attr_tag_or_pairs = valid_pairs
                
            # 2. Clean eclipsed standard attributes (e.g. "gray")
            if entities.attributes:
                for attr_label, attr_slug in list(entities.attributes.items()):
                    attr_term_tokens = _normalize_for_tag_compare(attr_slug.replace("-", " "))
                    if any(attr_term_tokens <= exact_tokens for exact_tokens in exact_tag_tokens):
                        del entities.attributes[attr_label]

    return ClassifiedResult(intent=intent, entities=entities, confidence=confidence)

# ─────────────────────────────────────────────
# ENTITY EXTRACTION HELPERS 
# ─────────────────────────────────────────────
def _extract_category(text: str, entities: ExtractedEntities) -> str:
    loader = get_store_loader()
    if not loader or not loader.category_by_slug: 
        return text

    extracted_cats = []
    longest_match = ""

    # 1. Scan for all matching category names
    for slug, cat in loader.category_by_slug.items():
        name_lower = cat.get("name", "").lower().strip()
        if len(name_lower) < 3: 
            continue

        pattern = rf'\b{re.escape(name_lower)}\b'
        try:
            if re.search(pattern, text, re.IGNORECASE):
                # Keep track of the longest match strictly for the UI bot message
                if len(name_lower) > len(longest_match):
                    longest_match = name_lower
                    entities.category_name = cat.get("name")
                    
                extracted_cats.append(cat)
        except re.error: 
            pass

    if not extracted_cats:
        return text

    # ══════════════════════════════════════════════════════════════
    # 2. HIERARCHY RESOLUTION (The Category Graph)
    # ══════════════════════════════════════════════════════════════
    extracted_ids = {c.get("id") for c in extracted_cats}
    linked_children = []

    # Check if any extracted category is a direct child of another extracted category
    for cat in extracted_cats:
        if cat.get("parent") in extracted_ids:
            linked_children.append(cat)

    if not hasattr(entities, 'target_category_slugs'):
        entities.target_category_slugs = set()

    if linked_children:
        # SCENARIO A: User typed "Exterior Floor". We drop the broad Parent 
        # and the orphan Floors, keeping ONLY the specific child branch!
        for child in linked_children:
            entities.target_category_slugs.add(child.get("slug"))
    else:
        # SCENARIO B: User typed "Floor". Bundle ALL duplicate slugs together.
        for cat in extracted_cats:
            entities.target_category_slugs.add(cat.get("slug"))

    return text

def _extract_product_name(text: str, entities: ExtractedEntities):
    loader = get_store_loader()
    if loader:
        match = loader.get_product_for_text(text)
        if match:
            generic_words = {"product", "products", "item", "items"} | set(PRODUCT_TYPE_TERMS)
            if match["name"].lower().strip() in generic_words: return
            matched_name_lower = match["name"].lower()
            if matched_name_lower not in text:
                text_words = set(re.split(r'[\s\-_/]+', text.lower()))
                product_tokens = set(re.split(r'[\s\-_/]+', matched_name_lower))
                overlapping_tokens = text_words & product_tokens
                tag_names_lower = set(loader.tag_by_name_lower.keys())
                tag_words = {t for tag_name in tag_names_lower for t in re.split(r'[\s\-_/]+', tag_name) if t and len(t) > 2}
                if not overlapping_tokens or overlapping_tokens.issubset(tag_words): return  
            entities.product_name = match["name"]
            entities.product_slug = match.get("slug", "")
            entities.product_id = match.get("id")
            if "mosaic" in text: entities.product_slug = f"{match['slug']}-mosaic"
            elif "chip card" in text: entities.product_slug = f"{match['slug']}-chip-card"
            elif "ymal" in text: entities.product_slug = f"{match['slug']}-ymal"

def _normalize_for_tag_compare(s: str) -> set:
    return set(re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split())

def _extract_attributes(text: str, entities: ExtractedEntities) -> str:
    loader = get_store_loader()
    if not loader or not loader.all_attributes_raw: return text
    
    masked_text = text
    
    for attr in loader.all_attributes_raw:
        label = attr.get("attribute_label", "").lower().strip()
        taxonomy = attr.get("taxonomy", "")
        terms = attr.get("terms", [])
        if not label or not taxonomy or not terms: continue

        # Dynamically detect if this attribute is a measurement
        is_dimensional = any(kw in label for kw in ['size', 'thickness', 'weight', 'width', 'length', 'depth', 'dimension'])

        # --- Origin Logic (Kept exactly as is) ---
        if "origin" in label:
            matched_origin = False
            for keyword, normalized in ORIGIN_KEYWORDS.items():
                if re.search(rf"\b{re.escape(keyword)}\b", masked_text):
                    tag_ids = loader.get_tag_ids_for_keyword(normalized)
                    if not tag_ids: tag_ids = loader.get_tag_ids_for_keyword(f"made in {normalized}")
                    term_slug = loader.get_attribute_term_slug(taxonomy, normalized) or normalized
                    entities.attributes[label] = term_slug
                    entities.tag_ids.extend(tag_ids)
                    for tid in tag_ids:
                        tag = loader.tag_by_id.get(tid)
                        if tag: entities.tag_slugs.append(tag["slug"])
                    matched_origin = True
                    break
            if matched_origin: continue

        _product_name_lower = (entities.product_name or "").lower()
        
        for term in terms:
            term_name = term.get("name", "")
            term_name_lower = term_name.lower().strip()
            if not term_name_lower or len(term_name_lower) < 1: continue
            if _product_name_lower and term_name_lower in _product_name_lower: continue
            
            try:
                matched_pattern = None
                
                # --- 1. GENERIC DIMENSIONAL MATCHER ---
                if is_dimensional:
                    term_dim = _normalize_dimension(term_name)
                    # Make sure there is actually a number to match
                    if term_dim and re.search(r'\d', term_dim):
                        escaped_dim = re.escape(term_dim)
                        # If it's a 2D size (e.g., 60x120), allow flexible separators like "by" or "X"
                        if 'x' in escaped_dim:
                            escaped_dim = escaped_dim.replace('x', r'\s*"?\s*(?:x|by|×)\s*')
                            
                        # Look for the raw number, optionally followed by any unit type
                        dim_pattern = rf"(?<!\d){escaped_dim}\s*(?:\"|'|mm|cm|inch(?:es)?|in\.?|thick|lbs?|oz|kg|g)?(?!\d)"
                        if re.search(dim_pattern, masked_text, re.IGNORECASE):
                            matched_pattern = dim_pattern

                # --- 2. STANDARD WORD MATCHER ---
                if not matched_pattern:
                    if re.search(rf"\b{re.escape(term_name_lower)}\b", masked_text):
                        matched_pattern = rf"\b{re.escape(term_name_lower)}\b"
                    elif re.search(rf"\b{re.escape(term_name_lower)}s\b", masked_text):
                        matched_pattern = rf"\b{re.escape(term_name_lower)}s\b"
                    elif len(term_name_lower) > 4 and re.search(rf"\b{re.escape(term_name_lower[:-1])}\b", masked_text):
                        matched_pattern = rf"\b{re.escape(term_name_lower[:-1])}\b"

                # --- 3. APPLY MATCH ---
                if matched_pattern:
                    covered_by_tag = False
                    covering_tag_slug = None
                    covering_tag_id = None
                    exact_tag_matched = False
                    
                    if loader:
                        import html
                        # Clean the text of all messy punctuation (quotes, hyphens) for perfect matching
                        norm_text = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9/.]', ' ', html.unescape(text))).strip()
                        
                        for tag_name_lower, tag_entry in loader.tag_by_name_lower.items():
                            if tag_entry.get("count", 0) == 0: continue
                            
                            # If it's dimensional, check if the raw digits match
                            if is_dimensional:
                                tag_digits = re.sub(r'[^0-9]', '', tag_entry.get("slug", ""))
                                term_digits = re.sub(r'[^0-9]', '', term_name_lower)
                                if tag_digits and term_digits and tag_digits == term_digits:
                                    covered_by_tag = True
                                    covering_tag_slug = tag_entry.get("slug", "")
                                    covering_tag_id = tag_entry.get("id")
                                    
                                    # EXACT MATCH CHECK 1: Punctuation-stripped string (solves quotes/measurements)
                                    norm_tag = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9/.]', ' ', html.unescape(tag_name_lower))).strip()
                                    if norm_tag and re.search(rf'\b{re.escape(norm_tag)}\b', norm_text):
                                        exact_tag_matched = True
                                    # EXACT MATCH CHECK 2: Flexible regex (solves singular/plural like "tones")
                                    elif re.search(_create_flexible_pattern(tag_name_lower), text):
                                        exact_tag_matched = True
                                    break
                            else:
                                # Standard overlap check: Is "black" a subset of "black tones"?
                                term_tokens = _normalize_for_tag_compare(term_name_lower)
                                tag_tokens = _normalize_for_tag_compare(tag_name_lower)
                                
                                if term_tokens and term_tokens <= tag_tokens:
                                    covered_by_tag = True
                                    covering_tag_slug = tag_entry.get("slug", "")
                                    covering_tag_id = tag_entry.get("id")
                                    
                                    # EXACT MATCH CHECK 1: Punctuation-stripped string
                                    norm_tag = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9/.]', ' ', html.unescape(tag_name_lower))).strip()
                                    if norm_tag and re.search(rf'\b{re.escape(norm_tag)}\b', norm_text):
                                        exact_tag_matched = True
                                    # EXACT MATCH CHECK 2: Flexible regex
                                    elif re.search(_create_flexible_pattern(tag_name_lower), text):
                                        exact_tag_matched = True
                                    break
                                        
                    # --- THE OVERRIDE LOGIC ---
                    if exact_tag_matched:
                        # User typed the EXACT tag. Add to strict Tag list and DO NOT assign an attribute!
                        if covering_tag_id not in entities.tag_ids:
                            entities.tag_ids.append(covering_tag_id)
                            entities.tag_slugs.append(covering_tag_slug)
                            
                    elif covered_by_tag:
                        # User was generic. Build the broad OR pair.
                        entities.attr_tag_or_pairs.append({
                            "tag_slug": covering_tag_slug, 
                            "attr_taxonomy": taxonomy, 
                            "attr_term": term.get("slug", term_name)
                        })
                        
                    else:
                        # Standard attribute assignment
                        term_slug = term.get("slug", term_name)
                        entities.attributes[label] = term_slug
                        entities.attribute_slug = taxonomy
                        entities.attribute_term_ids = [term["id"]]
                    
                    break # Move to the next term
                
            except re.error: pass
            
    return masked_text

def _extract_customer_fetch(text: str, entities: ExtractedEntities):
    FETCH_RE = r"\b(?:show|get|what(?:'?s| is)|display|tell me)\b"
    FIELD_PHRASES = {"first name": "first_name", "last name": "last_name", "username": "username", "name": "full_name", "billing phone": "billing.phone", "billing address": "billing.address_1", "billing city": "billing.city", "billing email": "billing.email", "shipping address": "shipping.address_1", "shipping city": "shipping.city", "shipping phone": "shipping.phone"}
    for phrase, field_key in FIELD_PHRASES.items():
        m = re.search(rf"{FETCH_RE}[^.]*?\bmy\b[^.]*?\b{re.escape(phrase)}\b", text, re.IGNORECASE)
        if m: entities.customer_fields_requested.append(field_key)

def _extract_customer_updates(text: str, entities: ExtractedEntities):
    TOP_LEVEL_FIELDS = {"first name": "first_name", "last name": "last_name", "username": "username", "first_name": "first_name", "last_name": "last_name"}
    BILLING_FIELDS = {"billing first name": "first_name", "billing last name": "last_name", "billing company": "company", "billing address": "address_1", "billing address 1": "address_1", "billing address 2": "address_2", "billing city": "city", "billing state": "state", "billing postcode": "postcode", "billing zip": "postcode", "billing country": "country", "billing phone": "phone", "billing email": "email"}
    SHIPPING_FIELDS = {"shipping first name": "first_name", "shipping last name": "last_name", "shipping company": "company", "shipping address": "address_1", "shipping address 1": "address_1", "shipping address 2": "address_2", "shipping city": "city", "shipping state": "state", "shipping postcode": "postcode", "shipping zip": "postcode", "shipping country": "country", "shipping phone": "phone"}
    _UPDATE_RE = r"\b(?:change|update|set|edit|modify)\b"
    def _extract_value(phrase):
        m = re.search(rf"{_UPDATE_RE}[^.]*?\b{re.escape(phrase)}\b[^.]*?\bto\b\s+(.+?)(?:\s*[.,]|$)", text, re.IGNORECASE)
        if m: return m.group(1).strip().strip("\"'")
        m = re.search(rf"\bmy\b[^.]*?\b{re.escape(phrase)}\b[^.]*?\b(?:is|should be|will be)\b\s+(.+?)(?:\s*[.,]|$)", text, re.IGNORECASE)
        if m: return m.group(1).strip().strip("\"'")
        return None
    for phrase, field_key in TOP_LEVEL_FIELDS.items():
        val = _extract_value(phrase)
        if val: entities.customer_updates[field_key] = val
    if not entities.customer_updates.get("first_name"):
        m = re.search(r"\b(?:change|update|set|edit|modify)\b[^.]*?\bmy\s+name\b[^.]*?\bto\b\s+(.+?)(?:\s*[.,]|$)", text, re.IGNORECASE)
        if m:
            parts = m.group(1).strip().strip("\"'").split()
            if len(parts) >= 2:
                entities.customer_updates["first_name"] = parts[0]
                entities.customer_updates["last_name"]  = " ".join(parts[1:])
            elif len(parts) == 1: entities.customer_updates["first_name"] = parts[0]
    for phrase, field_key in BILLING_FIELDS.items():
        val = _extract_value(phrase)
        if val: entities.billing_updates[field_key] = val
    for phrase, field_key in SHIPPING_FIELDS.items():
        val = _extract_value(phrase)
        if val: entities.shipping_updates[field_key] = val

def _extract_collection_year(text: str, entities: ExtractedEntities):
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
                if tag: entities.tag_slugs.append(tag["slug"])

def _extract_order_id(text: str, entities: ExtractedEntities):
    match = re.search(r'order\s*#?\s*(\d+)', text)
    if match: entities.order_id = int(match.group(1))

def _extract_time_range(text: str, entities: ExtractedEntities):
    from datetime import datetime, timezone, timedelta
    from dateutil.relativedelta import relativedelta
    now = datetime.now(timezone.utc)
    m = re.search(r'(?:last|past)\s+(\d+)\s+(day|week|month|year)s?', text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == 'day': entities.date_after = (now - timedelta(days=n)).strftime('%Y-%m-%dT00:00:00')
        elif unit == 'week': entities.date_after = (now - timedelta(weeks=n)).strftime('%Y-%m-%dT00:00:00')
        elif unit == 'month': entities.date_after = (now - relativedelta(months=n)).strftime('%Y-%m-%dT00:00:00')
        elif unit == 'year': entities.date_after = (now - relativedelta(years=1)).strftime('%Y-%m-%dT00:00:00')
        return
    if re.search(r'(?:last|past)\s+month', text): entities.date_after = (now - relativedelta(months=1)).strftime('%Y-%m-%dT00:00:00'); return
    if re.search(r'(?:last|past)\s+week', text): entities.date_after = (now - timedelta(weeks=1)).strftime('%Y-%m-%dT00:00:00'); return
    if re.search(r'(?:last|past)\s+year', text): entities.date_after = (now - relativedelta(years=1)).strftime('%Y-%m-%dT00:00:00'); return
    if re.search(r'this\s+month', text): entities.date_after = now.replace(day=1).strftime('%Y-%m-%dT00:00:00'); return
    if re.search(r'this\s+year', text): entities.date_after = now.replace(month=1, day=1).strftime('%Y-%m-%dT00:00:00'); return

def _extract_quantity(text: str, entities: ExtractedEntities):
    match = re.search(r'(\d+)\s*(qty|quantity|pcs|pieces|units|boxes|sq\s*ft)', text)
    if match: entities.quantity = int(match.group(1)); return
    match = re.search(r'\b(?:order|buy|purchase|place\s+(?:an?\s+)?order)(?:\s+for)?\s+(\d+)\b', text)
    if match: entities.quantity = int(match.group(1)); return
    match = re.search(r'\b(\d+)\s+of\s+(?:this|these|them|it|the)\b', text)
    if match: entities.quantity = int(match.group(1))

def _label_word_matches(word, text):
    w = re.escape(word)
    if re.search(rf"\b{w}s?\b", text) or re.search(rf"\b{w}es?\b", text): return True
    if word.endswith("s") and len(word) > 3:
        if re.search(rf"\b{re.escape(word[:-1])}\b", text): return True
    return False

def _create_flexible_pattern(phrase: str) -> str:
    """Create a regex pattern that handles optional plurals for each word."""
    parts = []
    for w in phrase.split():
        if w.endswith('s') and len(w) > 3:
            parts.append(rf'\b{re.escape(w[:-1])}s?\b')
        else:
            parts.append(rf'\b{re.escape(w)}s?\b')
    return r'\s+'.join(parts)

def _extract_tag(text: str, entities: ExtractedEntities) -> str:
    loader = get_store_loader()
    if not loader: return text
    
    masked_text = text
    existing_ids = set(entities.tag_ids)
    resolved_attr_token_sets = [_normalize_for_tag_compare(v) for v in entities.attributes.values() if v]
    
    _cat_base_words = set()
    _all_cat_names = []
    if entities.category_name: 
        _all_cat_names.append(entities.category_name.lower())
        
    if hasattr(entities, 'target_category_slugs'):
        for slug in entities.target_category_slugs:
            _cat = loader.category_by_slug.get(slug)
            if _cat and _cat.get("name"): 
                _all_cat_names.append(_cat["name"].lower())
                
    for _cname in _all_cat_names:
        _cat_base_words.add(_cname)
        if _cname.endswith("s") and len(_cname) > 3: _cat_base_words.add(_cname[:-1])

    candidates = []  
    for name_lower, tag in loader.tag_by_name_lower.items():
        if tag["id"] in existing_ids or tag.get("count", 0) == 0 or len(name_lower) < 4 or name_lower in _cat_base_words: continue
        tag_tokens = _normalize_for_tag_compare(name_lower)
        if tag_tokens and any(tag_tokens <= attr_tokens for attr_tokens in resolved_attr_token_sets): continue
        
        matched_pattern = None
        
        try:
            if re.search(rf'\b{re.escape(name_lower)}\b', masked_text): 
                matched_pattern = rf'\b{re.escape(name_lower)}\b'
        except re.error: pass
        
        if not matched_pattern:
            pattern = _create_flexible_pattern(name_lower)
            if pattern != rf'\b{re.escape(name_lower)}\b':
                try:
                    if re.search(pattern, masked_text): matched_pattern = pattern
                except re.error: pass
        
        if not matched_pattern:
            slug_words = tag["slug"].replace("-", " ")
            if slug_words != name_lower and len(slug_words) >= 4:
                pattern = _create_flexible_pattern(slug_words)
                try:
                    if re.search(pattern, masked_text): matched_pattern = pattern
                except re.error: pass
                
        if not matched_pattern:
            if len(tag["slug"]) >= 4:
                try:
                    if re.search(rf'(?<![\w]){re.escape(tag["slug"])}(?![\w])', masked_text): 
                        matched_pattern = rf'(?<![\w]){re.escape(tag["slug"])}(?![\w])'
                except re.error: pass
                
        if matched_pattern: 
            candidates.append((tag, name_lower, matched_pattern))

    all_token_sets = [_normalize_for_tag_compare(n) for _, n, _ in candidates]
    for i, (tag, name_lower, matched_pattern) in enumerate(candidates):
        if not (all_token_sets[i] and any(all_token_sets[i] < other for j, other in enumerate(all_token_sets) if j != i)):
            entities.tag_ids.append(tag["id"])
            entities.tag_slugs.append(tag["slug"])

    return masked_text

def _extract_order_item(text: str, entities: ExtractedEntities):
    if not re.search(r"\b(order|buy|purchase|get|want)\b", text): return
    if re.search(r"\b(history|track|tracking|status|before|past|previous|show|tell|about|detail)\b", text): return
    loader = get_store_loader()
    if loader:
        match = loader.get_product_for_text(text)
        if match:
            entities.order_item_name = match["name"]
            return
    for pattern in [r"\b(?:order|buy|purchase|get|want)\b.*?\b(?:this\s+item\s+)?([A-Z][a-zA-Z]+)", r"\bi\s+want\s+(?:to\s+)?(?:order|buy|purchase|get)\s+(\w+)"]:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().lower()
            if candidate not in ({"this", "that", "item", "product", "items", "some", "the", "a", "an", "my", "again", "more", "it", "them", "these", "those", "for", "to", "of"} | set(PRODUCT_TYPE_TERMS)) and len(candidate) > 2:
                entities.order_item_name = candidate.title()
                return

_UNRESOLVABLE_DESCRIPTORS = ["durable", "heavy-duty", "heavy duty", "premium", "luxury", "rustic", "modern", "classic", "natural", "affordable", "budget"]

def _extract_unresolved_descriptors(text: str, entities: ExtractedEntities):
    hints = []
    for descriptor in _UNRESOLVABLE_DESCRIPTORS:
        if re.search(rf'\b{re.escape(descriptor.lower())}\b', text):
            if not (descriptor.lower() in {v.lower() for v in entities.attributes.values()} or any(descriptor.lower() in slug.replace("-", " ") for slug in entities.tag_slugs)):
                hints.append(descriptor)
    if hints and hasattr(entities, 'search_hints'): entities.search_hints = hints

def _detect_tag_operator(text: str, entities: ExtractedEntities):
    if len(entities.tag_slugs) < 2 or not re.search(r'\bor\b|\beither\b', text): return
    slug_word_sets = [set(slug.replace("-", " ").split()) for slug in entities.tag_slugs]
    text_words = set(text.split())
    if sum(1 for words in slug_word_sets if words & text_words) >= 2:
        entities.tag_operator = "OR"

_NEGATION_PATTERNS = [r'\bwithout\s+(.+?)(?:\s+(?:tiles?|products?|ones?)|$)', r'\bno\s+(.+?)(?:\s+(?:tiles?|products?|ones?)|$)', r'\bnot\s+(.+?)(?:\s+(?:tiles?|products?|ones?)|$)', r'\bexclude\s+(.+?)(?:\s+(?:tiles?|products?|ones?)|$)', r'\bavoid\s+(.+?)(?:\s+(?:tiles?|products?|ones?)|$)', r'\bdon\'?t\s+(?:want|include|show)\s+(.+?)(?:\s+(?:tiles?|products?|ones?)|$)']

def _extract_exclusions(text: str, entities: ExtractedEntities) -> str:
    loader = get_store_loader()
    if not loader: 
        return text
        
    masked_text = text
    
    for pattern in _NEGATION_PATTERNS:
        for match in re.finditer(pattern, masked_text, re.IGNORECASE):
            phrase = match.group(1).strip().lower()
            full_match = match.group(0)
            
            if not phrase or len(phrase) < 2: 
                continue
                
            _resolved = False
            
            # 1. Resolve Tags
            for candidate in [phrase] + phrase.split():
                if len(candidate) < 3: continue
                tag_entry = loader.tag_by_name_lower.get(candidate) or loader.tag_by_slug.get(candidate.replace(" ", "-"))
                if tag_entry and tag_entry.get("slug"):
                    slug = tag_entry["slug"]
                    if slug not in entities.excluded_tags and slug not in entities.tag_slugs:
                        entities.excluded_tags.append(slug)
                        _resolved = True
                    break
                    
            # 2. Resolve Categories
            if not _resolved:
                cat = loader.category_by_name_lower.get(phrase) or (loader.category_by_id.get(loader.get_category_id(phrase)) if loader.get_category_id(phrase) else None)
                if cat and cat.get("slug") and cat["slug"] != "uncategorized":
                    if cat["slug"] not in entities.excluded_categories:
                        entities.excluded_categories.append(cat["slug"])
                        _resolved = True
                        
            # 3. Resolve Attributes
            if not _resolved and loader.all_attributes_raw:
                for attr in loader.all_attributes_raw:
                    for term in attr.get("terms", []):
                        if phrase == term.get("name", "").lower() or phrase == term.get("slug", "").replace("-", " "):
                            if not hasattr(entities, 'excluded_attributes'): entities.excluded_attributes = {}
                            if attr.get("taxonomy") not in entities.excluded_attributes: entities.excluded_attributes[attr.get("taxonomy")] = []
                            if term.get("slug", "") not in entities.excluded_attributes[attr.get("taxonomy")]:
                                entities.excluded_attributes[attr.get("taxonomy")].append(term.get("slug", ""))
                            _resolved = True
                            break
                    if _resolved: break
            
            if _resolved:
                masked_text = masked_text.replace(full_match, " ")
                
    return masked_text

def _extract_price_range(text: str, entities: ExtractedEntities):
    for pattern in [r'between\s+\$?(\d+(?:\.\d+)?)\s+(?:and|to|-)\s+\$?(\d+(?:\.\d+)?)', r'\$(\d+(?:\.\d+)?)\s+to\s+\$?(\d+(?:\.\d+)?)', r'\$(\d+(?:\.\d+)?)\s*[-–]\s*\$?(\d+(?:\.\d+)?)']:
        m = re.search(pattern, text)
        if m:
            entities.min_price, entities.max_price = float(m.group(1)), float(m.group(2))
            return
    for pattern in [r'(?:under|below|less\s+than|cheaper\s+than|max(?:imum)?|at\s+most|up\s+to)\s+\$?(\d+(?:\.\d+)?)', r'\$(\d+(?:\.\d+)?)\s+(?:or\s+)?(?:less|under|below)']:
        m = re.search(pattern, text)
        if m:
            entities.max_price = float(m.group(1))
            return
    for pattern in [r'(?:over|above|more\s+than|at\s+least|min(?:imum)?)\s+\$?(\d+(?:\.\d+)?)', r'\$(\d+(?:\.\d+)?)\s+(?:or\s+)?(?:more|above|over)']:
        m = re.search(pattern, text)
        if m:
            entities.min_price = float(m.group(1))
            return