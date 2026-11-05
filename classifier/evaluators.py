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
from classifier.utils import (
    label_word_matches, explicit_kind_near, create_flexible_pattern, message_is_store_name,
)

logger = get_logger("miraq_chat")


class IntentEvaluator(ABC):
    """Abstract base class for all intent evaluators."""

    # Every word this evaluator's regexes match on. Unioned into the fuzzy
    # corrector's protected set at vocab-build time so a shopper typing
    # "bulk order" is never rewritten to the nearest catalog term.
    # Subclasses MUST declare this; audit_keyword_drift() fails the build if
    # a regex literal is missing from it.
    KEYWORDS: frozenset = frozenset()

    @abstractmethod
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        """Returns (Intent, confidence) if a match is found, else (None, 0.0)."""
        pass


# ═══════════════════════════════════════════
# EVALUATOR IMPLEMENTATIONS
# ═══════════════════════════════════════════


class OrderActionEvaluator(IntentEvaluator):
    # Vocabulary this evaluator's regexes key off. Must never be typo-corrected
    # — see classifier/keywords.py. Kept in sync by audit_keyword_drift().
    KEYWORDS = frozenset({
        "about", "add", "after", "again", "all", "apr", "aug", "before", "between",
        "bought", "browse", "buy", "cart", "check", "checkout", "complement",
        "day", "dec", "detail", "details", "did", "display", "during", "feb",
        "fetch", "find", "get", "goes", "had", "have", "history", "info",
        "item", "items", "jan", "jul", "jun", "last", "latest", "list", "look",
        "mar", "match", "may", "month", "most", "my", "nov", "oct", "open", "order",
        "ordered", "orders", "pair", "past", "previous", "previously",
        "product", "products", "provide", "purchase", "purchases", "recent",
        "related", "reorder", "repeat", "search", "see", "sep", "should",
        "show", "similar", "something", "status", "tell", "track", "tracking",
        "view", "want", "week", "what", "where", "which", "year",
    })

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

        _is_tracking_or_info = re.search(r"\b(track|tracking|status|where|last|latest|most\s*recent|history|previous|past|look|show|search|browse|find|see|display|detail|details|info|provide)\b", text)

        if re.search(r"\bwant\s+to\s+(order|buy|purchase)\b|\bi'?d\s+like\s+to\s+(order|buy|purchase)\b|\bplace\s+(an?\s+)?order\b", text) and not _is_tracking_or_info:
            return Intent.QUICK_ORDER, 0.93

        if re.search(r"\b(order|buy|purchase)\b", text) and (entities.order_item_name or entities.product_name) and not _is_tracking_or_info:
            return Intent.QUICK_ORDER, 0.93

        if re.search(r"^(order|buy|purchase)\s*(a\s+product|an\s+item|something|)?$", text.strip()) and not _is_tracking_or_info:
            return Intent.QUICK_ORDER, 0.93

        # "get me [N] X" — add to cart / order intent
        if re.search(r"\bget\s+me\b", text) and not _is_tracking_or_info:
            return Intent.QUICK_ORDER, 0.91

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
        
        # Assertion-style: "I should have orders from June" / "I have orders from last month"
        if re.search(r'\b(?:should\s+have|have|had)\s+(?:an?\s+)?orders?\b', text):
            return Intent.ORDER_HISTORY, 0.92

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
    KEYWORDS = frozenset({
        "bookmark", "later", "save", "wishlist",
    })
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if entities.customer_updates or entities.billing_updates or entities.shipping_updates:
            return Intent.UPDATE_CUSTOMER, 0.93
        if entities.customer_fields_requested:
            return Intent.FETCH_CUSTOMER, 0.93
        return None, 0.0


class DiscountEvaluator(IntentEvaluator):
    KEYWORDS = frozenset({
        "bulk", "clearance", "code", "coupon", "deals", "discount", "promo",
        "promotions", "sale",
    })
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if re.search(r"\bbulk\s*discount\b", text):
            return Intent.BULK_DISCOUNT, 0.92
        # A discount word the shopper called a tag or collection is a catalog
        # filter, not a discount question: "Mosaics Tile Wall collection with
        # Sale tag" wants products tagged Sale in that collection, not the
        # store's on-sale list. Only when every discount word is used that
        # way does this step aside — "sale items in the Sale collection"
        # still asks about sales.
        # The whole message is a store category/tag name ("Sale" from the
        # category list): the shopper named that category — CatalogSearch
        # takes it as a category browse.
        if message_is_store_name(text, get_store_loader()):
            return None, 0.0
        if any(not self._named_as_tag_or_collection(text, m)
               for m in self._DISCOUNT_WORD_RE.finditer(text)):
            entities.on_sale = True
            return Intent.DISCOUNT_INQUIRY, 0.91
        return None, 0.0

    _DISCOUNT_WORD_RE = re.compile(r"\b(clearance|discount|sale|deals?|promotions?)\b")

    @staticmethod
    def _named_as_tag_or_collection(text: str, m: "re.Match") -> bool:
        """True when the discount word at `m` was called a tag or collection:
        directly ("sale tag", "tag sale", "clearance collection"), or as part
        of a longer tag/collection name the shopper called one ("summer sale
        tag" with a "Summer Sale" tag in the store)."""
        if explicit_kind_near(text, m.start(), m.end()):
            return True
        loader = get_store_loader()
        if not loader:
            return False
        stem = m.group(0).rstrip("s")
        names = set(getattr(loader, "tag_by_name_lower", {}) or {}) \
            | set(getattr(loader, "category_by_name_lower", {}) or {})
        for name in names:
            if " " not in name or not re.search(rf"\b{re.escape(stem)}", name):
                continue  # one-word names were covered by the direct check
            try:
                spans = re.finditer(create_flexible_pattern(name), text)
            except re.error:
                continue
            for nm in spans:
                if nm.start() <= m.start() and m.end() <= nm.end() \
                        and explicit_kind_near(text, nm.start(), nm.end()):
                    return True
        return False


class ProductDetailEvaluator(IntentEvaluator):
    KEYWORDS = frozenset({
        "about", "also", "available", "colors", "come", "complement", "does",
        "finishes", "goes", "how", "immediate", "like", "match", "may", "now",
        "options", "pair", "quick", "related", "ship", "similar", "sizes",
        "tell", "variants", "variations", "what", "which",
    })
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
            entities.attributes["quick-ship"] = "yes"  # inject the implied term value
            # If other filters exist, let the combined filter handle it
            has_other_filters = bool(
                entities.product_name
                or entities.tag_slugs
                or getattr(entities, 'target_category_slugs', set())
                or {k: v for k, v in entities.attributes.items() if k != "quick-ship"}
            )
            if has_other_filters:
                return None, 0.0  # fall through to CatalogSearchEvaluator → FILTER_BY_ATTRIBUTE
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


class PopularityEvaluator(IntentEvaluator):
    """
    Detects "most popular" / "best sellers" / "top selling" style requests.

    Runs before CatalogSearchEvaluator so a category/attribute/tag already
    resolved by the extractors (e.g. "most popular tiles in the Aurora
    collection") doesn't get claimed by CATEGORY_BROWSE / FILTER_BY_ATTRIBUTE
    first — those filters are picked up as-is by _build_most_popular via the
    same entities, just ranked by total_sales instead of the default order.
    """
    KEYWORDS = frozenset({
        "best", "highest", "most", "popular", "products", "items",
        "sell", "sellers", "selling", "sells", "sold", "top",
    })

    _POPULARITY_RE = re.compile(
        r"\b(most\s+popular|best[\s-]?sellers?|top[\s-]?sellers?|"
        r"top[\s-]?selling|best[\s-]?selling|most\s+sold|highest[\s-]?selling|"
        r"sells?\s+(?:the\s+)?(?:most|best)|"
        r"popular\s+(?:products?|items?))\b"
    )

    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        # A named product ("is Aurora Marble a best seller?") is a
        # per-product question, not a ranked listing — let it fall through
        # to CatalogSearchEvaluator's product_name-aware PRODUCT_DETAIL /
        # PRODUCT_SEARCH branches instead of claiming it here.
        if entities.product_name:
            return None, 0.0
        if self._POPULARITY_RE.search(text):
            entities.sort_by = "popularity"
            return Intent.MOST_POPULAR, 0.92
        return None, 0.0


class CatalogSearchEvaluator(IntentEvaluator):
    KEYWORDS = frozenset({
        "about", "all", "browse", "categor", "category", "categories", "cost", "detail",
        "get", "how", "info", "list", "more", "much", "price", "products",
        "see", "show", "specification", "specs", "tell", "view", "what",
    })
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        if len(entities.product_ids) >= 2:
            entities.product_id = None
            entities.product_name = None
            entities.product_slug = None
            return Intent.PRODUCT_SEARCH, 0.93

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

        # "view / browse / see categories" were missing, so they fell through to
        # the LLM fallback (≈1s per ask) to land on the same answer.
        if re.search(r"\b(what|list|show|all|view|browse|see)\b.*\bcategor(y|ies)\b", text):
            return Intent.CATEGORY_LIST, 0.91
        if (entities.attributes or getattr(entities, 'attr_tag_or_pairs', [])) and not entities.product_name:
            return Intent.FILTER_BY_ATTRIBUTE, 0.89
        if entities.collection_year:
            return Intent.PRODUCT_BY_COLLECTION, 0.89
        if entities.tag_ids:
            return Intent.PRODUCT_BY_TAG, 0.88
        if re.search(r"\b(show|list|get|see)\b.*\b(more|all)\b.*\bproducts?\b", text):
            return Intent.PRODUCT_LIST, 0.87

        # A resolved product_id with no other filters means the user named a
        # specific product — route to PRODUCT_DETAIL so build_api_calls uses
        # _build_product_detail (which passes product_id correctly) instead of
        # falling through to _build_fallback (which lost its product_id/
        # search_term body injection in the Shopify refactor).
        if entities.product_id:
            return Intent.PRODUCT_DETAIL, 0.90

        # Unresolved name (no product_id yet) — route to PRODUCT_SEARCH so the
        # name is used as a search term rather than hitting the fallback path.
        if entities.product_name:
            return Intent.PRODUCT_SEARCH, 0.85

        return None, 0.0


class GeneralFallbackEvaluator(IntentEvaluator):
    KEYWORDS = frozenset({
        "catalog", "catalogue", "categories", "collection", "have", "kinds",
        "offer", "portfolio", "range", "sell", "types", "varieties",
    })
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

        # `is not None`: in_stock=False ("what's out of stock?") is a real
        # filter too — truthiness dropped it and the question came back UNKNOWN.
        if (entities.attributes or getattr(entities, 'attr_tag_or_pairs', []) or entities.in_stock is not None) and not entities.product_name:
            return Intent.FILTER_BY_ATTRIBUTE, 0.89

        return Intent.UNKNOWN, 0.0


class CartCheckoutEvaluator(IntentEvaluator):
    KEYWORDS = frozenset({
        "add", "cart", "change", "check", "checkout", "complete", "delete",
        "drop", "open", "order", "out", "place", "proceed", "put", "qty",
        "quantity", "remove", "see", "set", "show", "take", "throw", "toss",
        "update", "view",
    })
    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:

        # VIEW_CART
        if re.search(r'\b(view|show|see|check|open|go\s+to)\b.*\bcart\b'
                     r'|\bcart\b.*\b(view|show|see)\b'
                     r'|^(my\s+)?cart\s*[?.]?$', text):
            return Intent.VIEW_CART, 0.95

        # CHECKOUT
        if re.search(r'\b(checkout|check\s*out|complete\s*(the\s+)?order'
                     r'|place\s*(the\s+)?order|proceed\s*to\s*checkout)\b', text):
            return Intent.CHECKOUT, 0.95

        # REMOVE_FROM_CART
        if re.search(r'\b(remove|delete|take\s+out|drop)\b.{0,20}\b(from\s+)?(my\s+)?cart\b', text):
            return Intent.REMOVE_FROM_CART, 0.93

        # UPDATE_CART_QTY
        if re.search(r'\b(change|update|set)\b.{0,20}\b(quantity|qty)\b', text):
            return Intent.UPDATE_CART_QTY, 0.91

        # ADD_TO_CART — explicit phrase only (ambiguous "yes" handled via flow state)
        if re.search(r'\badd\b.{0,20}\b(to\s+(my\s+)?cart)\b', text):
            return Intent.ADD_TO_CART, 0.95
        if re.search(r'\b(put|throw|toss)\b.{0,15}\bin\s+(to\s+)?(my\s+)?cart\b', text):
            return Intent.ADD_TO_CART, 0.92

        return None, 0.0

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
    CartCheckoutEvaluator(),
    OrderActionEvaluator(),
    DiscountEvaluator(),
    ProductDetailEvaluator(),
    AccountActionsEvaluator(),
    PopularityEvaluator(),
    CatalogSearchEvaluator(),
    GeneralFallbackEvaluator(),
]


def get_default_pipeline() -> ClassifierPipeline:
    """Return a ClassifierPipeline with the default evaluator chain."""
    _run_keyword_audit_once()
    return ClassifierPipeline(DEFAULT_EVALUATORS)


_KEYWORD_AUDIT_DONE = False


def _run_keyword_audit_once() -> None:
    """
    Verify every regex literal in this module is declared in its evaluator's
    KEYWORDS, so the typo corrector will leave it alone. Logs an error on
    drift; the test suite calls audit_keyword_drift(strict=True) to fail CI.
    """
    global _KEYWORD_AUDIT_DONE
    if _KEYWORD_AUDIT_DONE:
        return
    _KEYWORD_AUDIT_DONE = True
    try:
        from classifier.keywords import audit_keyword_drift
        audit_keyword_drift()
    except Exception as exc:
        logger.warning(f"Keyword drift audit skipped: {exc}")