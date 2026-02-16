"""
Tiles Store Intent Classifier for WooCommerce
==============================================
Maps natural language queries to WooCommerce REST API calls.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────
# 1. INTENT DEFINITIONS
# ─────────────────────────────────────────────

class Intent(Enum):
    # Product Discovery & Search
    PRODUCT_LIST            = "product_list"
    PRODUCT_SEARCH          = "product_search"
    PRODUCT_CATEGORY_BROWSE = "product_category_browse"
    PRODUCT_CATALOG         = "product_catalog"
    PRODUCT_TYPES           = "product_types"

    # Size & Dimensions
    SIZE_FILTER             = "size_filter"
    SIZE_LIST               = "size_list"

    # Discounts & Promotions
    DISCOUNT_INQUIRY        = "discount_inquiry"
    BULK_DISCOUNT           = "bulk_discount"
    CLEARANCE_PRODUCTS      = "clearance_products"
    PROMOTIONS              = "promotions"
    COUPON_INQUIRY          = "coupon_inquiry"

    # Account & Ordering
    SAVE_FOR_LATER          = "save_for_later"
    WISHLIST                = "wishlist"
    ORDER_TRACKING          = "order_tracking"
    ORDER_STATUS            = "order_status"
    PLACE_ORDER             = "place_order"

    # Fallback
    UNKNOWN                 = "unknown"


# ─────────────────────────────────────────────
# 2. ENTITY / SLOT DEFINITIONS
# ─────────────────────────────────────────────

@dataclass
class ExtractedEntities:
    """All possible entities extracted from a user utterance."""
    category: Optional[str] = None          # e.g. "floor", "wall", "bathroom", "kitchen"
    product_type: Optional[str] = None      # e.g. "tiles", "mosaic", "porcelain"
    size: Optional[str] = None              # e.g. "large", "small", "12x12", "24x24"
    room: Optional[str] = None              # e.g. "bathroom", "kitchen", "living room"
    material: Optional[str] = None          # e.g. "ceramic", "porcelain", "marble"
    color: Optional[str] = None             # e.g. "white", "grey", "blue"
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    on_sale: Optional[bool] = None
    coupon_code: Optional[str] = None
    order_id: Optional[int] = None
    product_id: Optional[int] = None
    quantity: Optional[int] = None
    search_term: Optional[str] = None
    tag: Optional[str] = None               # e.g. "clearance", "bestseller"


# ─────────────────────────────────────────────
# 3. CLASSIFIED RESULT
# ─────────────────────────────────────────────

@dataclass
class ClassifiedResult:
    intent: Intent
    entities: ExtractedEntities
    confidence: float
    api_endpoint: str = ""
    api_method: str = "GET"
    api_params: dict = field(default_factory=dict)