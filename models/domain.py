"""
models.py — Domain dataclasses and enums for the intent classifier pipeline.

This file contains ONLY pure-Python models with zero framework dependencies.
SQLAlchemy ORM models live in db_models.py.

For backward compatibility, db_models symbols are re-exported below so that
existing `from models import db, Conversation, Message` statements continue
to work without any import changes across the codebase.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Literal, Optional, List, Dict
from chat_logger import get_logger


logger = get_logger("miraq_chat")


# ══════════════════════════════════════════════════════════════
# BACKWARD-COMPAT RE-EXPORTS (from db_models.py)
# ══════════════════════════════════════════════════════════════
# Every file that does `from models import db, Conversation, Message`
# will keep working. New code should import from db_models directly.

from models.db_models import db, DEFAULT_CONTEXT, Conversation, Message  # noqa: F401


# ══════════════════════════════════════════════════════════════
# INTENT ENUM
# ══════════════════════════════════════════════════════════════

class Intent(Enum):
    # Product Discovery
    PRODUCT_LIST           = "product_list"
    PRODUCT_SEARCH         = "product_search"
    PRODUCT_BY_TAG         = "product_by_tag"
    PRODUCT_CATALOG        = "product_catalog"
    PRODUCT_TYPES          = "product_types"
    PRODUCT_BY_COLLECTION  = "product_by_collection"
    PRODUCT_QUICK_SHIP     = "product_quick_ship"
    PRODUCT_DETAIL         = "product_detail"
    PRODUCT_ATTRIBUTE_INFO = "product_attribute_info"
    RELATED_PRODUCTS       = "related_products"

    # Category-Based Browsing
    CATEGORY_BROWSE        = "category_browse"
    CATEGORY_LIST          = "category_list"

    # Attribute Filtering
    FILTER_BY_ATTRIBUTE    = "filter_by_attribute"
    MOST_POPULAR           = "most_popular"

    # Discounts & Promotions
    DISCOUNT_INQUIRY       = "discount_inquiry"
    BULK_DISCOUNT          = "bulk_discount"
    CLEARANCE_PRODUCTS     = "clearance_products"
    PROMOTIONS             = "promotions"
    COUPON_INQUIRY         = "coupon_inquiry"

    # Account & Ordering
    SAVE_FOR_LATER         = "save_for_later"
    WISHLIST               = "wishlist"
    ORDER_TRACKING         = "order_tracking"
    ORDER_STATUS           = "order_status"
    PLACE_ORDER            = "place_order"

    # Variations
    PRODUCT_VARIATIONS     = "product_variations"

    # Order History & Reorder
    ORDER_HISTORY          = "order_history"
    LAST_ORDER             = "last_order"
    REORDER                = "reorder"
    ORDER_ITEM             = "order_item"
    QUICK_ORDER            = "quick_order"
    HISTORICAL_SEARCH      = "HISTORICAL_SEARCH"  # kept as-is for DB compat

    # Customer
    UPDATE_CUSTOMER        = "update_customer"
    FETCH_CUSTOMER         = "fetch_customer"
    GREETING               = "greeting"

    UNKNOWN                = "unknown"
    
    # Cart
    ADD_TO_CART       = "add_to_cart"
    VIEW_CART         = "view_cart"
    REMOVE_FROM_CART  = "remove_from_cart"
    UPDATE_CART_QTY   = "update_cart_qty"
    CHECKOUT          = "checkout"
    BULK_ORDER        = "bulk_order"

# ══════════════════════════════════════════════════════════════
# OR PAIR — typed replacement for List[dict]
# ══════════════════════════════════════════════════════════════

@dataclass
class OrPair:
    """
    A single cross-taxonomy OR group: the user's term might live in
    a tag, a category, or an attribute — wrap all candidates so the
    API matches any of them.

    Example: "black" → tag "black-look" OR Color="black"
      OrPair(tag_slug="black-look", attr_key="color", attr_term="black")
    """
    tag_slug: Optional[str] = None
    cat_slugs: List[str] = field(default_factory=list)
    attr_key: Optional[str] = None        # e.g. "color"
    attr_taxonomy: Optional[str] = None   # DEPRECATED alias for attr_key (e.g. "pa_color")
    attr_term: Optional[str] = None       # neutral term key (e.g. "red")

    def __post_init__(self):
        if self.attr_taxonomy and not self.attr_key:
            self.attr_key = self.attr_taxonomy.removeprefix("pa_")
            logger.warning("OrPair.attr_taxonomy is deprecated; use attr_key")

    @property
    def branches(self) -> int:
        """How many OR branches this pair produces."""
        count = 0
        if self.tag_slug:
            count += 1
        if self.cat_slugs:
            count += 1
        if (self.attr_key or self.attr_taxonomy) and self.attr_term:
            count += 1
        return count

    @property
    def is_valid(self) -> bool:
        """An OR pair needs at least 2 branches to be meaningful."""
        return self.branches >= 2

# ══════════════════════════════════════════════════════════════
# EXTRACTED ENTITIES
# ══════════════════════════════════════════════════════════════

@dataclass
class ExtractedEntities:

    # ──── Product identification ────
    product_name: Optional[str] = None
    product_id: Optional[int] = None
    product_slug: Optional[str] = None

    # ──── Category fields ────
    category_name: Optional[str] = None
    target_category_slugs: set = field(default_factory=set)
    category_groups: List[set] = field(default_factory=list)

    # ──── Dynamic attribute matches ────
    # Keyed by attribute_label.lower() from the store's live attribute list.
    # e.g. {"finish": "Matte", "colors": "White", "application": "Interior Wall"}
    attributes: Dict[str, str] = field(default_factory=dict)

    # Non-attribute entity fields
    quick_ship: Optional[bool] = None
    collection_year: Optional[str] = None
    pricing_tier: Optional[str] = None
    variation_level: Optional[str] = None

    # ──── Tags ────
    tag_slugs: List[str] = field(default_factory=list)
    tag_ids: List[int] = field(default_factory=list, metadata={"llm_exclude": "redundant"})  # duplicates tag_slugs
    # "AND" → product must have ALL tags (default).
    # "OR"  → product must have AT LEAST ONE tag.
    tag_operator: str = "AND"

    # ──── Exclusion filters ────
    excluded_tags: List[str] = field(default_factory=list)
    excluded_categories: List[str] = field(default_factory=list)
    excluded_attributes: Dict[str, List[str]] = field(default_factory=dict)
    excluded_search_term: Optional[str] = None

    # ──── Attribute term resolution ────
    attribute_slug: Optional[str] = None
    attribute_term_ids: List[int] = field(default_factory=list, metadata={"llm_exclude": "redundant"})  # duplicates attributes

    # ──── Filters ────
    on_sale: Optional[bool] = None
    in_stock: Optional[bool] = None
    product_type: Optional[str] = None
    search_term: Optional[str] = None

    # ──── Sorting ────
    # Set by PopularityEvaluator when the shopper asks for "most popular" /
    # "best sellers" / etc. Only "popularity" is supported today (ranks by
    # WooCommerce's all-time `total_sales` meta) — no time-boxed window.
    sort_by: Optional[str] = None

    # ──── Ordering ────
    order_id: Optional[int] = None
    quantity: Optional[int] = None
    variation_id: Optional[int] = None

    # ──── Order history & reorder ────
    reorder: Optional[bool] = None
    order_count: Optional[int] = None
    order_item_name: Optional[str] = None
    explicit_last_order: bool = False
    lookup_email: Optional[str] = None

    # ──── Attribute info query ────
    target_attribute: Optional[str] = None
    target_attributes: List[str] = field(default_factory=list)

    # ──── Price range ────
    min_price: Optional[float] = None
    max_price: Optional[float] = None

    # ──── Time range (ORDER_HISTORY) ────
    date_after: Optional[str] = None
    date_before: Optional[str] = None

    # ──── Tag+attribute OR pairs ────
    # Populated when a term matches BOTH a tag slug AND a pa_* attribute term.
    # api_builder wraps each pair in a nested OR condition so products are found
    # regardless of whether they use the tag or the attribute.
    attr_tag_or_pairs: List[dict] = field(default_factory=list)

    # ──── Customer update fields ────
    customer_updates: Dict[str, str] = field(default_factory=dict, metadata={"llm_exclude": "pii"})
    billing_updates: Dict[str, str] = field(default_factory=dict, metadata={"llm_exclude": "pii"})
    shipping_updates: Dict[str, str] = field(default_factory=dict, metadata={"llm_exclude": "pii"})
    customer_fields_requested: List[str] = field(default_factory=list)

    # ──── Logical Chunking ────
    logical_chunks: List[dict] = field(default_factory=list, metadata={"llm_exclude": "internal"})  # NLP-internal, no user-facing meaning


    # ──── Semantic Resolution ────
    semantic_matches: List = field(default_factory=list, metadata={"llm_exclude": "internal"})  # embedding-internal, no user-facing meaning

    # ──── Search hints (unresolvable descriptors like "premium", "rustic") ────
    search_hints: List[str] = field(default_factory=list)

    # ──── Cart ────
    cart_items: List[dict] = field(default_factory=list)
    # Populated by chat.py before build_api_calls is called for CHECKOUT.
    # Each item: {product_id, variation_id, qty, name}
    
    # ──── Semantic auto-materialize marker ────
    # Set to True only by _auto_materialize() (catalog_parser.py) when a
    # semantic-match candidate scored >= AUTO_APPLY_THRESHOLD and was
    # written directly into attributes/tags/categories THIS turn. Used by
    # _merge_phase_entities (chat.py) to safely upgrade an UNKNOWN intent
    # without risking a false positive from carryover state — this field
    # is never set by any carryover-restoration path, only by a fresh
    # same-turn semantic resolution.
    semantic_auto_applied: bool = field(default=False, metadata={"llm_exclude": "internal"})

    # ──── Helper methods ────

    def get_or_pairs(self) -> list:
        result = []
        for p in self.attr_tag_or_pairs:
            if isinstance(p, OrPair):
                result.append(p)
            elif isinstance(p, dict):
                result.append(OrPair(
                    tag_slug=p.get("tag_slug"),
                    cat_slugs=p.get("cat_slugs", []),
                    attr_key=p.get("attr_key"),
                    attr_taxonomy=p.get("attr_taxonomy"),
                    attr_term=p.get("attr_term"),
                ))
        return result

    def get_filter_kwargs(self) -> dict:
        """
        Common filter kwargs shared by most intent builders in api_builder.
        Centralizes the repeated boilerplate so intent builders can do:
            build_advanced_filter_call(**e.get_filter_kwargs(), page=page, ...)
        """
        return {
            "tags": list(self.tag_slugs) if self.tag_slugs else None,
            "categories": self.target_category_slugs or None,
            "category_groups": [list(g) for g in self.category_groups] or None,
            "or_pairs": self.get_or_pairs() or None,
            "excluded_tags": list(self.excluded_tags) if self.excluded_tags else None,
            "excluded_categories": list(self.excluded_categories) if self.excluded_categories else None,
            "excluded_attributes": self.excluded_attributes or None,
            "tag_operator": self.tag_operator,
            "min_price": self.min_price,
            "max_price": self.max_price,
        }
    def add_category_group(self, slugs) -> None:
        """Register one resolved category mention as its own OR'd group,
        while keeping target_category_slugs as the flat union for any
        existing code that reads it directly."""
        slugs = set(slugs)
        if not slugs:
            return
        self.category_groups.append(slugs)
        self.target_category_slugs.update(slugs)

    def clear_categories(self) -> None:
        self.target_category_slugs.clear()
        self.category_groups = []
        self.category_name = None


# ══════════════════════════════════════════════════════════════
# API CALL MODEL
# ══════════════════════════════════════════════════════════════

_VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


@dataclass
class WooAPICall:
    method: str
    endpoint: str
    params: Dict[str, object] = field(default_factory=dict)
    body: Optional[dict] = None
    description: str = ""
    requires_resolution: List[str] = field(default_factory=list)
    surface: Literal[
        "admin",            # WooCommerce REST (wc/v3)
        "custom_plugin",    # MiraQ WordPress plugin endpoints (custom-api/v1)
        "shopify_graphql",  # ShopifyGraphQLExecutor (product search)
        "shopify_orders",   # ShopifyOrdersExecutor (order intents)
        "shopify_admin",    # ShopifyEndpoints stubs — NOT dispatched yet (Phase 2 guards these)
        "loader_memory",    # Served from the in-memory StoreLoader, no HTTP call
    ] = "admin"
    user_message: str = ""
    session_id: str = ""

    def __post_init__(self):
        self.method = self.method.upper()
        if self.method not in _VALID_METHODS:
            raise ValueError(
                f"WooAPICall.method must be one of {_VALID_METHODS}, got {self.method!r}"
            )


# ══════════════════════════════════════════════════════════════
# CLASSIFIED RESULT
# ══════════════════════════════════════════════════════════════

@dataclass
class ClassifiedResult:
    intent: Intent
    entities: ExtractedEntities
    confidence: float
    api_calls: List[WooAPICall] = field(default_factory=list)
    phase1_entities: Optional[ExtractedEntities] = None
    phase1_intent: Optional[Intent] = None
    phase1_confidence: Optional[float] = None