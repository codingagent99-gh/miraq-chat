"""
classifier/evaluators.py — Intent evaluator classes implementing
the Chain of Responsibility pattern for intent resolution.
"""

import re
from abc import ABC, abstractmethod
from typing import Optional, List, Tuple

from models import Intent, ExtractedEntities
from store_registry import get_store_loader
from config.store_config import PRODUCT_TYPE_TERMS, GENERIC_NOISE_WORDS
from chat_logger import get_logger
from classifier.utils import label_word_matches

logger = get_logger("miraq_chat")


class IntentEvaluator(ABC):
    """Abstract base class for all intent evaluators."""

    @abstractmethod
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        """Returns (Intent, confidence) if a match is found, else (None, 0.0)."""
        pass


# ═══════════════════════════════════════════
# EVALUATOR IMPLEMENTATIONS
# ═══════════════════════════════════════════


class OrderActionEvaluator(IntentEvaluator):
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if re.search(r"\b(repeat|reorder|re-order|order\s*again)\b", text):
            entities.reorder = True
            entities.order_count = 1
            if re.search(r"\b(last|recent|previous)\b", text) and not re.search(r"past orders?", text):
                entities.explicit_last_order = True
            return Intent.REORDER, 0.95

        has_filters = bool(
            entities.attributes or entities.tag_slugs
            or getattr(entities, 'target_category_slugs', set())
            or entities.product_name
        )
        is_past_order_query = (
            (re.search(r"\b(previous(?:ly)?|past|last|before)\b", text) and re.search(r"\b(purchases?|orders?|bought|buy|ordered)\b", text))
            or re.search(r"\b(?:from|in|of)\s+(?:my\s+)?orders?\b", text)
            or (re.search(r"\bmy\s+orders?\b", text) and has_filters)
        )
        is_match_query = re.search(r"\b(match|similar|related|goes\s*with|pair|complement)\b", text) and is_past_order_query
        asks_for_products = re.search(r"\b(what|which|show|list|tell)\b.*\b(products?|items?)\b", text)

        if is_match_query or (is_past_order_query and (has_filters or asks_for_products)):
            if re.search(r"\blast\b", text) and not re.search(r"past orders?", text):
                entities.order_count = 1
            return Intent.HISTORICAL_SEARCH, 0.96

        _is_tracking_or_info = re.search(r"\b(track|tracking|status|where|last|history|previous|past|look|show|search|browse|find|see|display)\b", text)

        if re.search(r"\bwant\s+to\s+(order|buy|purchase)\b|\bi'?d\s+like\s+to\s+(order|buy|purchase)\b|\bplace\s+(an?\s+)?order\b", text) and not _is_tracking_or_info:
            return Intent.QUICK_ORDER, 0.93

        if re.search(r"\b(order|buy|purchase)\b", text) and (entities.order_item_name or entities.product_name) and not _is_tracking_or_info:
            return Intent.QUICK_ORDER, 0.93

        if re.search(r"^(order|buy|purchase)\s*(a\s+product|an\s+item|something|)?$", text.strip()) and not _is_tracking_or_info:
            return Intent.QUICK_ORDER, 0.93

        if entities.order_id:
            if re.search(r"\b(what|which|show|list|tell)\b.*\b(products?|items?)\b", text):
                return Intent.HISTORICAL_SEARCH, 0.97
            if re.search(r"\b(show|view|see|detail|details|info|about|check|open|what|which|tell)\b", text):
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

        if re.search(r"\b(order\s*history|past\s*orders?|previous\s*orders?)\b", text) or \
           (re.search(r"\bordered\b", text) and re.search(r"\b(in\s+the\s+past|previously|before)\b", text)):
            return Intent.ORDER_HISTORY, 0.92

        if re.search(r"\bwhat\b.*\bordered\b.*\bbefore\b", text):
            return Intent.ORDER_HISTORY, 0.91

        if re.search(r"\b(check|show|view|see|get|list|display)\b.*\b(my\s+)?orders?\b", text) and not re.search(r"\b(track|tracking|status|where)\b", text) and not re.search(r"\b(last|latest|most\s*recent)\b", text) or \
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
            has_date_context = bool(re.search(
                r"\b(on|from|in|during|between|after|before)\b.{1,30}\b(\d{1,2}[\w]*|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                text, re.IGNORECASE,
            ))
            if has_date_context:
                return Intent.ORDER_HISTORY, 0.93
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
        if entities.product_name and re.search(r"\b(what|which|how|tell|about)\b", text):
            loader = get_store_loader()
            if loader and loader.all_attributes_raw:
                matched = self._match_attribute_label(text, loader, entities)
                if matched:
                    return Intent.PRODUCT_ATTRIBUTE_INFO, 0.91

        if re.search(r"\b(colors?|variants?|variations?|options?|finishes|sizes)\b.*\b(come|available|does|do)\b", text):
            return Intent.PRODUCT_VARIATIONS, 0.89
        if entities.product_name and re.search(r"\b(colors?|variants?|variations?|sizes)\b", text):
            return Intent.PRODUCT_VARIATIONS, 0.89
        if entities.product_name and re.search(r"\b(goes?\s*with|pair|complement|match|similar|related|you may also like|ymal)\b", text):
            return Intent.RELATED_PRODUCTS, 0.88
        if re.search(r"\bquick\s*ship\b|\bavailable\s*now\b|\bimmediate\b", text):
            entities.quick_ship = True
            return Intent.PRODUCT_QUICK_SHIP, 0.91
        return None, 0.0

    @staticmethod
    def _match_attribute_label(text: str, loader, entities: ExtractedEntities) -> bool:
        """Try to match an attribute label in the text for PRODUCT_ATTRIBUTE_INFO."""
        matched_label = None

        for attr in loader.all_attributes_raw:
            label = attr.get("attribute_label", "").lower().strip()
            if not label:
                continue
            words = label.split()
            if len(words) > 1:
                if re.search(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"s?\b", text):
                    matched_label = label
                    break
                if words[-1].endswith("s") and len(words[-1]) > 3:
                    if re.search(r"\b" + r"\s+".join(re.escape(w) for w in words[:-1]) + r"\s+" + re.escape(words[-1][:-1]) + r"\b", text):
                        matched_label = label
                        break
            else:
                if label_word_matches(label, text):
                    matched_label = label
                    break

        if not matched_label:
            for attr in loader.all_attributes_raw:
                label = attr.get("attribute_label", "").lower().strip()
                if not label:
                    continue
                for word in label.split():
                    if len(word) >= 4 and label_word_matches(word, text):
                        matched_label = label
                        break
                if matched_label:
                    break

        if matched_label:
            entities.target_attribute = matched_label
            return True
        return False


class CatalogSearchEvaluator(IntentEvaluator):
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if entities.product_id and entities.attributes:
            return Intent.PRODUCT_VARIATIONS, 0.93
        if entities.product_id and (entities.attributes or getattr(entities, 'in_stock', None) is not None):
            return Intent.PRODUCT_VARIATIONS, 0.93

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
        if entities.attributes and not entities.product_name:
            return Intent.FILTER_BY_ATTRIBUTE, 0.89
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

        for pt in PRODUCT_TYPE_TERMS:
            pt_esc = re.escape(pt)
            if re.search(rf"\b{pt_esc}s?\b", text):
                has_filters = any([
                    entities.product_name,
                    getattr(entities, 'target_category_slugs', None),
                    entities.attributes,
                    entities.tag_slugs,
                ])
                if not has_filters:
                    is_pure_generic = bool(re.search(
                        rf"^(show|list|all|sell|have|get|see|browse|what)\s+(me\s+)?(all\s+)?(your\s+)?(the\s+)?{pt_esc}s?[.?!]*$",
                        text.strip(),
                    ))
                    if is_pure_generic or re.search(rf"^{pt_esc}s?(?:\s+please)?[.?!]*$", text.strip()):
                        return Intent.PRODUCT_LIST, 0.85
                    else:
                        if not entities.search_term:
                            entities.search_term = text.replace("?", "").strip()
                        return Intent.PRODUCT_SEARCH, 0.80
                return Intent.PRODUCT_LIST, 0.75

        if (entities.attributes or entities.in_stock) and not entities.product_name:
            return Intent.FILTER_BY_ATTRIBUTE, 0.89

        return Intent.UNKNOWN, 0.0


# ═══════════════════════════════════════════
# PIPELINE RUNNER
# ═══════════════════════════════════════════

class ClassifierPipeline:
    """Manages the execution of Intent Evaluators in priority sequence."""

    def __init__(self, evaluators: List[IntentEvaluator]):
        self.evaluators = evaluators

    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Intent, float]:
        logger.debug(f"ClassifierPipeline: Starting evaluation for text={text!r}")
        for evaluator in self.evaluators:
            name = evaluator.__class__.__name__
            intent, confidence = evaluator.evaluate(text, entities)
            if intent is not None:
                logger.info(f"ClassifierPipeline: 🎯 {name} -> intent={intent.value} (conf={confidence})")
                return intent, confidence
            else:
                logger.debug(f"ClassifierPipeline: ⏭️ {name} passed.")
        logger.warning(f"ClassifierPipeline: ⚠️ Chain exhausted — UNKNOWN for text={text!r}")
        return Intent.UNKNOWN, 0.0


# ─── Default pipeline factory ───

DEFAULT_EVALUATORS = [
    OrderActionEvaluator(),
    DiscountEvaluator(),
    ProductDetailEvaluator(),
    AccountActionsEvaluator(),
    CatalogSearchEvaluator(),
    GeneralFallbackEvaluator(),
]


def get_default_pipeline() -> ClassifierPipeline:
    """Return a ClassifierPipeline with the default evaluator chain."""
    return ClassifierPipeline(DEFAULT_EVALUATORS)