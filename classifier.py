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


# class GreetingEvaluator(IntentEvaluator):
#     def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
#         if re.search(r"^\s*(hi|hello|hey|hiya|howdy|yo|sup)\s*[!.]?\s*$", text) or \
#            re.search(r"^\s*good\s+(morning|afternoon|evening|day)\s*[!.]?\s*$", text) or \
#            re.search(r"^\s*(how\s+are\s+you|how'?s\s+it\s+going|what'?s\s+up)\s*[?!.]?\s*$", text) or \
#            re.search(r"^\s*hi\s+there\s*[!.]?\s*$", text) or \
#            re.search(r"^\s*hey\s+there\s*[!.]?\s*$", text):
#             return Intent.GREETING, 0.99
#         return None, 0.0


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
        
        if entities.category_id is not None:
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
                # Log that this evaluator didn't match and passed it down the chain
                logger.debug(f"ClassifierPipeline: ⏭️ {evaluator_name} passed.")
                
        # If it gets through the whole chain without hitting the FallbackEvaluator
        logger.warning(
            f"ClassifierPipeline: ⚠️ Chain exhausted with no match. "
            f"Defaulting to UNKNOWN for text={text!r}"
        )
        return Intent.UNKNOWN, 0.0

# ─────────────────────────────────────────────
# MAIN CLASSIFY FUNCTION
# ─────────────────────────────────────────────

def classify(utterance: str) -> ClassifiedResult:
    """Classify user utterance into intent + entities using the Evaluation Pipeline."""
    text = utterance.lower().strip()
    entities = ExtractedEntities()

    # ─── Pre-extract common entities ───
    _extract_product_name(text, entities)
    _extract_category(text, entities)

    attr_text = text
    if entities.category_name:
        _cat_base = entities.category_name.lower()
        if _cat_base.endswith("s") and len(_cat_base) > 3:
            _cat_base = _cat_base[:-1]
        attr_text = re.sub(rf'\b{re.escape(_cat_base)}s?\b', ' ', attr_text).strip()

    _loader = get_store_loader()
    _type_mask_terms = set(pt.lower() for pt in PRODUCT_TYPE_TERMS)
    if _loader:
        _type_mask_terms |= _loader._store_generic_terms
    for _pt in _type_mask_terms:
        attr_text = re.sub(rf'\b{re.escape(_pt)}\b', ' ', attr_text).strip()

    _extract_tag(text, entities)               
    _extract_attributes(attr_text, entities)   
    
    _attr_added_tag_ids   = list(entities.tag_ids)
    _attr_added_tag_slugs = list(entities.tag_slugs)
    _preserved_or_pairs   = list(entities.attr_tag_or_pairs)
    
    entities.tag_ids   = []
    entities.tag_slugs = []
    _extract_tag(text, entities)            
    
    for tid in _attr_added_tag_ids:
        if tid not in entities.tag_ids:
            entities.tag_ids.append(tid)
    for slug in _attr_added_tag_slugs:
        if slug not in entities.tag_slugs:
            entities.tag_slugs.append(slug)
            
    entities.attr_tag_or_pairs = _preserved_or_pairs
    _extract_thickness(attr_text, entities)    
    _extract_collection_year(text, entities)
    _extract_order_id(text, entities)
    _extract_time_range(text, entities)
    _extract_quantity(text, entities)
    _extract_order_item(text, entities)
    _extract_unresolved_descriptors(text, entities)
    _extract_price_range(text, entities)       
    _extract_customer_updates(text, entities)  
    _detect_tag_operator(text, entities)       
    _extract_exclusions(text, entities)        
    _extract_customer_fetch(text, entities)

    # ─── Execute Intent Pipeline ───
    pipeline = ClassifierPipeline([
        # GreetingEvaluator(),
        OrderActionEvaluator(),
        DiscountEvaluator(),
        ProductDetailEvaluator(),
        AccountActionsEvaluator(),
        CatalogSearchEvaluator(),
        GeneralFallbackEvaluator()
    ])

    intent, confidence = pipeline.evaluate(text, entities)

    # ── Prevent product_id from hijacking category-scoped searches ──────────
    PRODUCT_SPECIFIC_INTENTS = {
        Intent.PRODUCT_VARIATIONS,
        Intent.PRODUCT_DETAIL,
        Intent.PRODUCT_SEARCH,
        Intent.SIZE_LIST,
        Intent.PRODUCT_ATTRIBUTE_INFO,
        Intent.QUICK_ORDER,
        Intent.PLACE_ORDER,
        Intent.ORDER_ITEM,
        Intent.SAMPLE_REQUEST
    }
    if entities.category_id is not None and entities.product_id is not None:
        if intent in PRODUCT_SPECIFIC_INTENTS:
            entities.category_id = None
            entities.category_name = None
            entities.category_slug = None
        else:
            entities.product_id = None
            
    return ClassifiedResult(
        intent=intent,
        entities=entities,
        confidence=confidence,
    )


# ─────────────────────────────────────────────
# ENTITY EXTRACTION HELPERS (Keep Existing)
# ─────────────────────────────────────────────
# [All your `_extract_*` functions remain here unchanged]
def _extract_category(text: str, entities: ExtractedEntities):
    loader = get_store_loader()
    if not loader: return
    matches = loader.get_all_categories_for_text(text)
    if not matches: return
    matched_ids = {m["id"] for m in matches}
    child_parent_ids = {loader.category_by_id.get(m["id"], {}).get("parent", 0) for m in matches}
    pruned = [m for m in matches if m["id"] not in (child_parent_ids & matched_ids)]
    if not pruned: pruned = matches
    entities.category_id   = pruned[0]["id"]
    entities.category_name = pruned[0]["name"]
    entities.category_slug = pruned[0].get("slug", "")
    entities.extra_category_ids = [m["id"] for m in pruned[1:]]

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

def _extract_attributes(text: str, entities: ExtractedEntities):
    loader = get_store_loader()
    if not loader or not loader.all_attributes_raw: return
    for attr in loader.all_attributes_raw:
        label = attr.get("attribute_label", "").lower().strip()
        taxonomy = attr.get("taxonomy", "")
        terms = attr.get("terms", [])
        if not label or not taxonomy or not terms: continue
        if "size" in label:
            def _size_qualifier(attr_label_str): return " ".join(w for w in attr_label_str.lower().split() if w != "size").strip()
            size_attrs_in_store = [a for a in loader.all_attributes_raw if "size" in a.get("attribute_label", "").lower()]
            my_qualifier = _size_qualifier(label)
            other_qualifiers = [_size_qualifier(a.get("attribute_label", "")) for a in size_attrs_in_store if a.get("taxonomy") != taxonomy]
            my_matches = bool(my_qualifier and re.search(rf"\b{re.escape(my_qualifier)}s?\b", text))
            other_matches = any(bool(q and re.search(rf"\b{re.escape(q)}s?\b", text)) for q in other_qualifiers)
            if other_matches and not my_matches: continue
            size_match = re.search(r'(\d+)\s*"?\s*(?:x|by|\xd7|X)\s*(\d+)', text)
            if size_match:
                w, h = size_match.group(1), size_match.group(2)
                size_str = f"{w}x{h}"
                term_ids = loader.get_attribute_term_ids(taxonomy, size_str)
                if not term_ids: term_ids = loader.get_attribute_term_ids(taxonomy, f'{w}"x{h}"')
                if term_ids:
                    term_slug = loader.get_attribute_term_slug(taxonomy, f'{w}x{h}') or f'{w}x{h}'
                    entities.attributes[label] = term_slug
                    entities.attribute_slug = taxonomy
                    entities.attribute_term_ids = term_ids
                    continue
        if "origin" in label:
            matched_origin = False
            for keyword, normalized in ORIGIN_KEYWORDS.items():
                if re.search(rf"\b{re.escape(keyword)}\b", text):
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
            if not term_name_lower or len(term_name_lower) < 3: continue
            if _product_name_lower and term_name_lower in _product_name_lower: continue
            try:
                matched = (re.search(rf"\b{re.escape(term_name_lower)}\b", text) or re.search(rf"\b{re.escape(term_name_lower)}s\b", text) or (len(term_name_lower) > 4 and re.search(rf"\b{re.escape(term_name_lower[:-1])}\b", text)))
                if matched:
                    term_tokens = _normalize_for_tag_compare(term_name_lower)
                    covered_by_tag = False
                    covering_tag_slug = None
                    if term_tokens and loader:
                        for tag_name_lower, tag_entry in loader.tag_by_name_lower.items():
                            if tag_entry.get("count", 0) == 0: continue
                            tag_tokens = _normalize_for_tag_compare(tag_name_lower)
                            if term_tokens <= tag_tokens:  # Changed to <= to allow exact matches
                                flex_pattern = _create_flexible_pattern(tag_name_lower)
                                if re.search(flex_pattern, text):
                                    covered_by_tag = True
                                    covering_tag_slug = tag_entry.get("slug", "")
                                    break
                    if covered_by_tag:
                        label_words = {w for w in label.split() if len(w) > 2}
                        slug_words  = set(covering_tag_slug.replace("-", " ").split())
                        is_origin   = "origin" in label
                        # Force an OR pair if it is an exact match (e.g. term="white" vs tag="white")
                        use_or_pair = bool(label_words & slug_words) or is_origin or (term_tokens == slug_words)
                        if covering_tag_slug and use_or_pair:
                            entities.attr_tag_or_pairs.append({"tag_slug": covering_tag_slug, "attr_taxonomy": taxonomy, "attr_term": term.get("slug", term_name)})
                        break
                    term_slug = term.get("slug", term_name)
                    
                    entities.attributes[label] = term_slug
                    entities.attribute_slug = taxonomy
                    entities.attribute_term_ids = [term["id"]]
                    if "origin" in label:
                        tag_ids = loader.get_tag_ids_for_keyword(term_name_lower)
                        if not tag_ids: tag_ids = loader.get_tag_ids_for_keyword(f"made in {term_name_lower}")
                        entities.tag_ids.extend(tag_ids)
                        for tid in tag_ids:
                            tag = loader.tag_by_id.get(tid)
                            if tag: entities.tag_slugs.append(tag["slug"])
                    break 
            except re.error: pass

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

def _extract_thickness(text: str, entities: ExtractedEntities):
    if re.search(r'\d+\s*"?\s*(?:x|×|by)\s*\d+', text): return
    THICKNESS_PATTERNS = [r'(\d+(?:\.\d+)?\s*mm)', r'(\d+(?:\.\d+)?\s*cm)', r'(\d+/\d+\s*"?\s*(?:inch(?:es)?|in\.?|thick)?)', r'(\d+(?:\.\d+)?(?:\s*"|\s*inch(?:es)?|\s*in\.?))']
    loader = get_store_loader()
    for pattern in THICKNESS_PATTERNS:
        match = re.search(pattern, text)
        if match and "thickness" not in entities.attributes:
            raw = match.group(1).strip()
            raw_digits = re.sub(r'[^0-9]', '', raw)  
            if any(raw_digits in re.sub(r'[^0-9]', '', slug) for slug in entities.tag_slugs): return
            entities.attributes["thickness"] = raw
            if loader:
                for attr in loader.all_attributes_raw:
                    if "thickness" in attr.get("attribute_label", "").lower():
                        entities.attribute_slug = attr["taxonomy"]
                        term_ids = loader.get_attribute_term_ids(attr["taxonomy"], raw)
                        if term_ids: entities.attribute_term_ids = term_ids
                        break
            return

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

def _extract_tag(text: str, entities: ExtractedEntities):
    loader = get_store_loader()
    if not loader: return
    existing_ids = set(entities.tag_ids)
    resolved_attr_token_sets = [_normalize_for_tag_compare(v) for v in entities.attributes.values() if v]
    _cat_base_words = set()
    _all_cat_names = []
    if entities.category_name: _all_cat_names.append(entities.category_name.lower())
    for _cid in entities.extra_category_ids:
        _cat = loader.category_by_id.get(_cid)
        if _cat: _all_cat_names.append(_cat["name"].lower())
    for _cname in _all_cat_names:
        _cat_base_words.add(_cname)
        if _cname.endswith("s") and len(_cname) > 3: _cat_base_words.add(_cname[:-1])

    candidates = []  
    for name_lower, tag in loader.tag_by_name_lower.items():
        if tag["id"] in existing_ids or tag.get("count", 0) == 0 or len(name_lower) < 4 or name_lower in _cat_base_words: continue
        tag_tokens = _normalize_for_tag_compare(name_lower)
        if tag_tokens and any(tag_tokens <= attr_tokens for attr_tokens in resolved_attr_token_sets): continue
        matched = False
        
        try:
            if re.search(rf'\b{re.escape(name_lower)}\b', text): matched = True
        except re.error: pass
        
        if not matched:
            pattern = _create_flexible_pattern(name_lower)
            if pattern != rf'\b{re.escape(name_lower)}\b':
                try:
                    if re.search(pattern, text): matched = True
                except re.error: pass
        
        if not matched:
            slug_words = tag["slug"].replace("-", " ")
            if slug_words != name_lower and len(slug_words) >= 4:
                pattern = _create_flexible_pattern(slug_words)
                try:
                    if re.search(pattern, text): matched = True
                except re.error: pass
                
        if not matched:
            if len(tag["slug"]) >= 4:
                try:
                    if re.search(rf'(?<![\w]){re.escape(tag["slug"])}(?![\w])', text): matched = True
                except re.error: pass
                
        if matched: candidates.append((tag, name_lower))

    all_token_sets = [_normalize_for_tag_compare(n) for _, n in candidates]
    for i, (tag, name_lower) in enumerate(candidates):
        if not (all_token_sets[i] and any(all_token_sets[i] < other for j, other in enumerate(all_token_sets) if j != i)):
            entities.tag_ids.append(tag["id"])
            entities.tag_slugs.append(tag["slug"])

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

def _extract_exclusions(text: str, entities: ExtractedEntities):
    loader = get_store_loader()
    if not loader: return
    for pattern in _NEGATION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            phrase = match.group(1).strip().lower()
            if not phrase or len(phrase) < 2: continue
            _resolved = False
            for candidate in [phrase] + phrase.split():
                if len(candidate) < 3: continue
                tag_entry = loader.tag_by_name_lower.get(candidate) or loader.tag_by_slug.get(candidate.replace(" ", "-"))
                if tag_entry and tag_entry.get("slug"):
                    slug = tag_entry["slug"]
                    if slug not in entities.excluded_tags and slug not in entities.tag_slugs:
                        entities.excluded_tags.append(slug)
                        _resolved = True
                    break
            if _resolved: continue
            cat = loader.category_by_name_lower.get(phrase) or (loader.category_by_id.get(loader.get_category_id(phrase)) if loader.get_category_id(phrase) else None)
            if cat and cat.get("slug") and cat["slug"] != "uncategorized":
                if cat["slug"] not in entities.excluded_categories:
                    entities.excluded_categories.append(cat["slug"])
                    _resolved = True
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