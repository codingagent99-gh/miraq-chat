"""
Data models for the WGC Tiles Store Intent Classifier.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List


class Intent(Enum):
    # Product Discovery
    PRODUCT_LIST           = "product_list"
    PRODUCT_SEARCH         = "product_search"
    PRODUCT_BY_VISUAL      = "product_by_visual"
    PRODUCT_BY_TAG         = "product_by_tag"
    PRODUCT_CATALOG        = "product_catalog"
    PRODUCT_TYPES          = "product_types"
    PRODUCT_BY_COLLECTION  = "product_by_collection"
    PRODUCT_BY_ORIGIN      = "product_by_origin"
    PRODUCT_QUICK_SHIP     = "product_quick_ship"
    PRODUCT_DETAIL         = "product_detail"
    RELATED_PRODUCTS       = "related_products"

    # ──── Category-Based Browsing ────
    CATEGORY_BROWSE        = "category_browse"
    CATEGORY_LIST          = "category_list"

    # Attribute Filtering
    FILTER_BY_FINISH       = "filter_by_finish"
    FILTER_BY_SIZE         = "filter_by_size"
    FILTER_BY_COLOR        = "filter_by_color"
    FILTER_BY_THICKNESS    = "filter_by_thickness"
    FILTER_BY_EDGE         = "filter_by_edge"
    FILTER_BY_APPLICATION  = "filter_by_application"
    FILTER_BY_MATERIAL     = "filter_by_material"
    FILTER_BY_ORIGIN       = "filter_by_origin"
    SIZE_LIST              = "size_list"

    # Product Subtypes
    MOSAIC_PRODUCTS        = "mosaic_products"
    TRIM_PRODUCTS          = "trim_products"
    CHIP_CARD              = "chip_card"

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
    SAMPLE_REQUEST         = "sample_request"

    # ──── NEW: Order History & Reorder ────
    ORDER_HISTORY          = "order_history"
    LAST_ORDER             = "last_order"
    REORDER                = "reorder"
    ORDER_ITEM             = "order_item"
    QUICK_ORDER            = "quick_order"

    # ──── Chit-Chat ────
    GREETING               = "greeting"

    UNKNOWN                = "unknown"


@dataclass
class ExtractedEntities:
    # Product identification
    product_name: Optional[str] = None
    product_id: Optional[int] = None
    product_slug: Optional[str] = None

    # ──── Category fields ────
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    category_slug: Optional[str] = None

    # Attributes
    visual: Optional[str] = None
    finish: Optional[str] = None
    tile_size: Optional[str] = None
    edge: Optional[str] = None
    thickness: Optional[str] = None
    application: Optional[str] = None
    color: Optional[str] = None
    color_tone: Optional[str] = None
    sample_size: Optional[str] = None
    pricing_tier: Optional[str] = None
    origin: Optional[str] = None
    quick_ship: Optional[bool] = None
    variation_level: Optional[str] = None
    collection_year: Optional[str] = None

    # Tags
    tag_slugs: List[str] = field(default_factory=list)
    tag_ids: List[int] = field(default_factory=list)

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

    # ──── NEW: Order history & reorder fields ────
    reorder: Optional[bool] = None
    order_count: Optional[int] = None          # how many past orders to fetch
    order_item_name: Optional[str] = None      # product name for "order this item X"


@dataclass
class WooAPICall:
    method: str
    endpoint: str
    params: dict
    body: Optional[dict] = None
    description: str = ""
    requires_resolution: List[str] = field(default_factory=list)
    is_custom_api: bool = False


@dataclass
class ClassifiedResult:
    intent: Intent
    entities: ExtractedEntities
    confidence: float
    api_calls: List[WooAPICall] = field(default_factory=list)