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

    # NOTE: scope ("my orders" vs "all orders") is NOT set here. It is
    # extracted in classifier/extractors.py::extract_order_scope, which runs
    # on every message before evaluation — this evaluator does not claim
    # every phrasing that reaches ORDER_HISTORY (the LLM fallback resolves
    # some), and scope set here would be missing on exactly those.

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
    KEYWORDS = frozenset({
        "bulk", "clearance", "code", "coupon", "deals", "discount", "promo",
        "promotions", "sale",
    })
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


class OrderStatsEvaluator(IntentEvaluator):
    """
    Detects order/sample reporting: "how many samples did <rep> order this
    quarter", "who ordered how many last month", "month to date order list",
    and now "show me orders by <rep>" / "order list for <rep>" (list mode —
    actual order cards for one named rep, not a count).

    Runs BEFORE the product evaluators. "How many samples were ordered by
    sale_rep_1" is full of catalog-shaped words ("samples", "ordered") that
    CatalogSearchEvaluator/ProductDetailEvaluator would otherwise claim,
    turning a reporting question into a product search. Also runs BEFORE
    OrderActionEvaluator so a named-rep "orders by/for <rep>" phrasing is
    claimed here rather than falling through to plain ORDER_HISTORY, which
    has no concept of a named person.

    Only sets the intent — it does NOT check permissions. Gating happens in
    the handler (and again in the plugin), so an unauthorized user gets an
    explicit refusal instead of a silently empty product list.
    """
    KEYWORDS = frozenset({
        "bought", "did", "how", "items", "list", "many", "mtd", "much",
        "of", "order", "ordered", "orders", "ordering", "pieces", "placed",
        "qtd", "quarter", "rep", "reps", "report", "samples", "sold",
        "summary", "who", "ytd",
    })

    # "how many samples/orders …", "how many did X order"
    _COUNT_RE = re.compile(
        r'\bhow\s+many\b.{0,40}?\b(samples?|orders?|items?|pieces?)\b'
        r'|\bhow\s+many\b.{0,40}?\border(?:ed|s)?\b'
    )
    # "who ordered how many", "who placed the most orders"
    _WHO_RE = re.compile(
        r'\bwho\b.{0,30}?\b(order(?:ed|s)?|placed|bought)\b'
    )
    # "order list", "list of orders" — genuinely wants order ROWS. Split out
    # of the old combined _LIST_RE: this half changed to mode="list" (§6.3),
    # the report/summary half below stayed mode="count".
    _LIST_RE = re.compile(
        r'\border\s+list\b|\blist\s+of\s+orders\b'
    )
    # "orders report", "orders summary" — list-shaped words, but this has
    # always meant an aggregate report, not order cards. Stays mode="count".
    _REPORT_RE = re.compile(
        r'\borders?\s+(?:report|summary)\b'
    )
    # "orders by <rep>", "orders for <rep>", "order list for <rep>", "list of
    # orders for <rep>" — the new named-rep list-mode phrasings (§6.2).
    # Anchored on order(s)/ordered immediately before by/for (small gap for
    # "list of"/"list for" in between), never a bare \bby\b or \bfor\b —
    # both are overloaded elsewhere ("sort by", "orders for the week") and
    # the bulk parser had to drop a bare \bon\b as a company marker for the
    # same reason.
    #
    # PLURAL/past-tense only — a bare singular "order" is the imperative VERB
    # that starts a bulk order: "Order Harmony for Kiki" is placing an order
    # for a person, not asking to see her order history. Allowing singular
    # here classified exactly that message as order_stats_by_rep whenever
    # only one product was named (two or more products are claimed earlier by
    # BulkOrderEvaluator, which is what hid this).
    _NAMED_LIST_RE = re.compile(
        r'\border(?:s|ed)\b.{0,20}?\b(?:by|for)\s+(?:rep\s+)?[a-z0-9._@\-]+',
        re.I,
    )
    # "by <rep>" / "for <rep>" / "did <rep> order". The char class includes @
    # so an email identifier is captured whole — the plugin accepts either an
    # email or a display name.
    _REP_RE = re.compile(
        r'\b(?:ordered|placed)\s+by\s+([a-z0-9._@\-]+(?:\s+[a-z0-9._@\-]+){0,2})'
        r'|\bdid\s+([a-z0-9._@\-]+(?:\s+[a-z0-9._@\-]+){0,2})\s+order\b'
        r'|\bby\s+(?:rep\s+)([a-z0-9._@\-]+(?:\s+[a-z0-9._@\-]+){0,2})'
        # "orders by/for <rep>", "order list for <rep>" — the same shape
        # _NAMED_LIST_RE triggers on, captured here so the name extraction
        # and the mode trigger stay in sync. Plural/past-tense only, for the
        # reason given on _NAMED_LIST_RE.
        r'|\border(?:s|ed)\b.{0,20}?\b(?:by|for)\s+(?:rep\s+)?([a-z0-9._@\-]+(?:\s+[a-z0-9._@\-]+){0,2})'
        # "order list for <rep>" — singular "order", but the following "list"
        # makes it unambiguously a request to SEE orders, not the imperative
        # verb that opens a bulk order. Spelled out separately so the plural
        # rule above can stay strict.
        r'|\border\s+list\s+(?:for|by)\s+(?:rep\s+)?([a-z0-9._@\-]+(?:\s+[a-z0-9._@\-]+){0,2})',
        re.I,
    )

    _STOP = {"i", "we", "you", "they", "anyone", "someone", "each", "every", "the", "a", "an"}

    # Trailing words that belong to the DATE phrase, not the rep's name. The
    # capture allows up to 3 words (real names have surnames), which otherwise
    # swallows "…by sale_rep_1 this year" into the name and fails the lookup.
    _TAIL_STOP = {
        "a", "an", "at", "between", "day", "days", "during", "far", "for",
        "from", "in", "last", "month", "months", "mtd", "on", "over", "past",
        "qtd", "quarter", "quarters", "since", "so", "the", "this", "to",
        "today", "week", "weeks", "within", "year", "years", "yesterday", "ytd",
    }

    def _extract_rep(self, text: str) -> Optional[str]:
        """Pull a rep name out of the query, or None if there isn't one.

        Returns None (not "") when the captured span turns out to be a date
        phrase or a pronoun — "orders for this week" captures "this week",
        which _TAIL_STOP then trims away to nothing.
        """
        m_rep = self._REP_RE.search(text)
        if not m_rep:
            return None
        raw = next((g for g in m_rep.groups() if g), "").strip(" .,?")
        tokens = raw.split()
        while tokens and tokens[-1].lower() in self._TAIL_STOP:
            tokens.pop()
        # Strip punctuation AFTER the tail trim, not before: trimming
        # "this" off "ram r. this" exposes a new final token whose
        # trailing period would otherwise survive, and "Ram R." then
        # fails a lookup that "Ram R" passes.
        raw = " ".join(tokens).strip(" .,?!;:")
        # "how many did I order" is self-scoped, not a named rep — leave
        # target_rep_name unset so the handler scopes to the caller.
        if raw and raw.lower() not in self._STOP:
            return raw
        return None

    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:
        is_count_trigger  = bool(self._COUNT_RE.search(text) or self._WHO_RE.search(text))
        is_list_trigger   = bool(self._LIST_RE.search(text))
        is_report_trigger = bool(self._REPORT_RE.search(text))
        has_named_shape   = bool(self._NAMED_LIST_RE.search(text))

        if not (is_count_trigger or is_list_trigger or is_report_trigger or has_named_shape):
            return None, 0.0

        rep = self._extract_rep(text)

        # The "orders by/for X" SHAPE alone does not make a query ours —
        # "show me all orders for the week" has exactly that shape with a
        # date phrase where the name would be, and claiming it here would
        # steal plain order-history queries (which is what an earlier
        # version of this rule did). Only a resolved rep name counts. With
        # no rep and no other trigger, fall through to OrderActionEvaluator
        # and let it answer as ORDER_HISTORY.
        if has_named_shape and not rep and not (
            is_count_trigger or is_list_trigger or is_report_trigger
        ):
            return None, 0.0

        if rep:
            entities.target_rep_name = rep
            entities.scope = "person"

        # Precedence: an explicit count/ranking phrase always wins, even
        # when show/list wording is also present — "show me how many orders
        # Jennifer placed" is a count, not a card list (§6.4). List mode also
        # requires an actual rep: the no-rep branch in the plugin is a SQL
        # GROUP BY with no order objects behind it, so "order list" on its
        # own stays a count exactly as it always has.
        if is_count_trigger:
            entities.mode = "count"
        elif rep and (is_list_trigger or has_named_shape):
            entities.mode = "list"
        else:
            entities.mode = "count"

        return Intent.ORDER_STATS_BY_REP, 0.9


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
        "best", "highest", "most", "popular", "sell", "sellers", "selling",
        "sells", "sold", "top",
    })

    _POPULARITY_RE = re.compile(
        r"\b(most\s+popular|best[\s-]?sellers?|top[\s-]?sellers?|"
        r"top[\s-]?selling|best[\s-]?selling|most\s+sold|highest[\s-]?selling|"
        r"sells?\s+(?:the\s+)?(?:most|best))\b"
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
        "about", "all", "categor", "category", "categories", "cost", "detail",
        "get", "how", "info", "list", "more", "much", "price", "products",
        "see", "show", "specification", "specs", "tell", "what",
    })
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

        if (entities.attributes or getattr(entities, 'attr_tag_or_pairs', []) or entities.in_stock) and not entities.product_name:
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

class BulkOrderEvaluator(IntentEvaluator):
    KEYWORDS = frozenset({
        "bulk", "buy", "buying", "order", "ordering", "place", "purchase",
        "reorder",
        # Shared-quantity phrasing — "2 each of Harmony, Adams for Kiki" is a
        # bulk order with no order verb anywhere in it.
        "each", "one", "two", "three", "four", "five", "six", "seven",
        "eight", "nine", "ten", "eleven", "twelve",
    })
    _ORDER_VERBS = re.compile(r'\b(order|buy|purchase|reorder|re-order)\b', re.I)
    _BULK_TRIGGER = re.compile(
        r'\bbulk\s+(?:order|ordering|buy|purchase|buying)\b'
        r'|\bplace\s+(?:a\s+)?bulk\b',
        re.I
    )
    # "N each of A, B" / "one each of A and B" — a single count governing
    # several products. Digits and spelled-out numbers both, matching what
    # bulk_order_parser distributes across the lines.
    _EACH_QTY = re.compile(
        r'\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten'
        r'|eleven|twelve)\b[^,]*\beach\b',
        re.I,
    )

    def _count_catalog_products(self, text: str) -> int:
        """How many DISTINCT catalog product names the text mentions.

        Longest names first: if "aurora mosaic" matches, a shorter name that
        is purely a substring of that same span (e.g. "aurora") shouldn't
        count as a second, separate product mention — it's the same word,
        just a shorter catalog entry.
        """
        loader = get_store_loader()
        if not loader or not loader.products:
            return 0
        _name_set = {
            p["name"].lower() for p in loader.products if p.get("name")
        }
        sorted_names = sorted(_name_set, key=len, reverse=True)
        claimed_spans: list[tuple[int, int]] = []
        resolved_count = 0
        for name in sorted_names:
            match = re.search(r'\b' + re.escape(name) + r'\b', text, re.I)
            if not match:
                continue
            start, end = match.span()
            if any(start < c_end and end > c_start for c_start, c_end in claimed_spans):
                continue
            claimed_spans.append((start, end))
            resolved_count += 1
        return resolved_count

    def evaluate(self, text: str, entities: ExtractedEntities) -> Tuple[Optional[Intent], float]:

        # ── Check 0: Explicit bulk intent phrase ──
        if self._BULK_TRIGGER.search(text):
            return Intent.BULK_ORDER, 0.93
        
        # ── Check 1: comma fragments with quantities (existing, unchanged) ──
        fragments = [f.strip() for f in text.split(',') if f.strip()]
        if len(fragments) >= 2:
            qualified = sum(
                1 for f in fragments
                if re.search(r'\d', f) and (
                    re.search(r'\bfor\b', f, re.I) or self._ORDER_VERBS.search(f)
                )
            )
            if qualified >= 2:
                return Intent.BULK_ORDER, 0.92

        # ── Check 1.5: shared "N each" quantity + 2+ catalog products ──
        # "2 each of Harmony, Adams for Kiki" reaches neither check above: the
        # digit sits on the first fragment and the "for" on the second, so
        # Check 1 qualifies nothing, and there is no order verb for Check 2.
        # It went to CatalogSearchEvaluator instead and came back as a product
        # search — with the leading "2" read as a 2cm thickness filter.
        #
        # A count governing several named products is an order, verb or not.
        # Still requires 2+ resolvable products, so "2 each of these please"
        # does not qualify.
        if self._EACH_QTY.search(text) and self._count_catalog_products(text) >= 2:
            return Intent.BULK_ORDER, 0.92

        # ── Check 2: order trigger + 2+ resolvable catalog products ──
        # Handles all separators: comma-only, "and"-only, "A, B and C",
        # comma+email, any mix — no digit requirement.
        if self._ORDER_VERBS.search(text):
            if self._count_catalog_products(text) >= 2:
                return Intent.BULK_ORDER, 0.92

        return None, 0.0

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
    CartCheckoutEvaluator(),
    BulkOrderEvaluator(),
    # Before OrderActionEvaluator: "how many samples did <rep> order" is a
    # reporting question, not a request to show someone's order history, and
    # before the product evaluators because it is full of catalog-shaped
    # words ("samples", "ordered") they would otherwise claim.
    OrderStatsEvaluator(),
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