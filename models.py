"""
Data models for the WGC Tiles Store Intent Classifier.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict

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
    PRODUCT_ATTRIBUTE_INFO = "product_attribute_info"  # generic: "what [attr] does X come in?"
    RELATED_PRODUCTS       = "related_products"

    # ──── Category-Based Browsing ────
    CATEGORY_BROWSE        = "category_browse"
    CATEGORY_LIST          = "category_list"

    # ──── Attribute Filtering (THE GENERIC ENGINE) ────
    FILTER_BY_ATTRIBUTE    = "filter_by_attribute"   # generic: any store attribute

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

    # ──── Order History & Reorder ────
    ORDER_HISTORY          = "order_history"
    LAST_ORDER             = "last_order"
    REORDER                = "reorder"
    ORDER_ITEM             = "order_item"
    QUICK_ORDER            = "quick_order"
    HISTORICAL_SEARCH = "HISTORICAL_SEARCH"

    # ──── Chit-Chat ────
    UPDATE_CUSTOMER        = "update_customer"   # update profile fields
    FETCH_CUSTOMER         = "fetch_customer"
    GREETING               = "greeting"

    UNKNOWN                = "unknown"

class AmbiguousFilter:
    """
    Represents a filter term that exists in BOTH a tag and an attribute,
    making it ambiguous which the user intended.

    e.g. "matte finish" matches:
      - tag slug "matte-finish"  (editorial/curated label)
      - pa_finish attribute term "Matte"  (structured product spec)

    The classifier stores this instead of silently picking one, so the
    response layer can ask the user to clarify before querying the API.
    """
    attribute_label: str        # e.g. "finish"
    attribute_term: str         # e.g. "Matte"
    attribute_slug: str         # e.g. "pa_finish"
    attribute_term_id: int      # WooCommerce term ID
    conflicting_tag_slug: str   # e.g. "matte-finish"
    conflicting_tag_id: int     # WooCommerce tag ID
    user_phrase: str            # the phrase in the user's text that triggered this


@dataclass
class ExtractedEntities:
    # Product identification
    product_name: Optional[str] = None
    product_id: Optional[int] = None
    product_slug: Optional[str] = None

    # ──── Category fields ────
    category_name: Optional[str] = None  # Kept ONLY for UI bot responses and text-masking
    target_category_slugs: set = field(default_factory=set)

    # ──── Dynamic attribute matches ────
    # Keyed by attribute_label.lower() from the store's live attribute list.
    # e.g. {"finish": "Matte", "colors": "White", "application": "Interior Wall"}
    # Works for any store — no hardcoded field names needed.
    attributes: Dict[str, str] = field(default_factory=dict)

    # Non-attribute entity fields
    quick_ship: Optional[bool] = None
    collection_year: Optional[str] = None
    pricing_tier: Optional[str] = None
    variation_level: Optional[str] = None

    # Tags
    tag_slugs: List[str] = field(default_factory=list)
    tag_ids: List[int] = field(default_factory=list)
    # tag_operator controls how multiple tag_slugs are combined in the query tree.
    # "AND" → product must have ALL tags (default — matches current behaviour).
    # "OR"  → product must have AT LEAST ONE tag (use when user says "X or Y").
    tag_operator: str = "AND"

    # Exclusion filters — populate when user says "no marble", "without X", etc.
    excluded_tags: List[str] = field(default_factory=list)
    excluded_categories: List[str] = field(default_factory=list)
    excluded_attributes: Dict[str, List[str]] = field(default_factory=dict)  # ← ADDED HERE

    # Attribute term resolution (for WooCommerce attribute=&attribute_term= filtering)
    attribute_slug: Optional[str] = None          # e.g. "pa_tile-size"
    attribute_term_ids: List[int] = field(default_factory=list)  # resolved term IDs

    # Filters
    on_sale: Optional[bool] = None
    in_stock: Optional[bool] = None
    product_type: Optional[str] = None
    search_term: Optional[str] = None

    # Ordering
    order_id: Optional[int] = None
    quantity: Optional[int] = None
    variation_id: Optional[int] = None

    # ──── Order history & reorder fields ────
    reorder: Optional[bool] = None
    order_count: Optional[int] = None          # how many past orders to fetch
    order_item_name: Optional[str] = None      # product name for "order this item X"
    explicit_last_order: bool = False

    # ──── Attribute info query ────
    target_attribute: Optional[str] = None     # attribute the user is asking about, e.g. "size", "finish"

    # ──── Price range fields ────
    min_price: Optional[float] = None   # e.g. "over $50" → 50.0
    max_price: Optional[float] = None   # e.g. "under $40" → 40.0

    # ──── Time range fields (used by ORDER_HISTORY) ────
    date_after: Optional[str] = None           # ISO datetime string e.g. "2026-01-01T00:00:00"
    date_before: Optional[str] = None          # ISO datetime string (reserved for future use)

    # ──── Ambiguous filter conflicts ────
    # Populated when a user phrase matches BOTH a tag and an attribute term.
    # e.g. "matte finish" → tag "matte-finish" AND pa_finish="Matte".
    # When non-empty, the response layer should ask the user to clarify
    # before building the API call, rather than silently picking one.
    ambiguous_filters: List = field(default_factory=list)  # List[AmbiguousFilter]

    # ──── Tag+attribute OR pairs ────
    # Populated when a term matches BOTH a tag slug AND a pa_* attribute term.
    # Rather than picking one silently or asking for disambiguation, api_builder
    # wraps each pair in a nested OR condition so products are found regardless
    # of whether they use the tag or the attribute to express the property.
    # e.g. "glossy finish" → [{"tag_slug": "glossy-finish",
    #                           "attr_taxonomy": "pa_finish",
    #                           "attr_term": "Glossy"}]
    attr_tag_or_pairs: List[dict] = field(default_factory=list)

    # ──── Customer update fields ────
    # Populated for UPDATE_CUSTOMER intent. Only allowed fields — role/email excluded.
    customer_updates: Dict[str, object] = field(default_factory=dict)
    # Structured sub-objects for billing/shipping address updates
    billing_updates: Dict[str, str]  = field(default_factory=dict)
    shipping_updates: Dict[str, str] = field(default_factory=dict)
    customer_fields_requested: List[str] = field(default_factory=list)  # e.g. ["first_name", "phone"]

    # ──── Logical Chunking ────
    # Stores isolated entity groups when a user uses "OR" (e.g. Titan OR Ansel)
    logical_chunks: List[dict] = field(default_factory=list)
    
    # ──── Conversational Disambiguation ────
    # Populated when a user types an unrecognized term that strongly resembles 
    # an active tag or attribute (e.g. "minimalist design" -> "minimalistic look").
    fuzzy_matches: List[dict] = field(default_factory=list)
    
    # ──── Logical Chunking ────
    logical_chunks: List[dict] = field(default_factory=list)
    
@dataclass
class WooAPICall:
    method: str
    endpoint: str
    params: dict
    body: Optional[dict] = None
    description: str = ""
    requires_resolution: List[str] = field(default_factory=list)
    is_custom_api: bool = False
    user_message: str = ""    # logged to api.txt for request context
    session_id: str = ""      # logged to api.txt for request context


@dataclass
class ClassifiedResult:
    intent: Intent
    entities: ExtractedEntities
    confidence: float
    api_calls: List[WooAPICall] = field(default_factory=list)