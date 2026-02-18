"""
Store configuration — loads credentials from .env file.
"""

import os
from dotenv import load_dotenv
from models import Intent

# Load .env from project root
load_dotenv()

# ─────────────────────────────────────────────
# WooCommerce API
# ─────────────────────────────────────────────
WOO_BASE_URL = os.getenv("WOO_BASE_URL", "https://wgc.net.in/hn/wp-json/wc/v3")
WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")

# ─────────────────────────────────────────────
# API Defaults
# ─────────────────────────────────────────────
DEFAULT_PER_PAGE = 20
DEFAULT_STATUS = "publish"
DEFAULT_STOCK_STATUS = "instock"
REQUEST_TIMEOUT = 30  # seconds

# ─────────────────────────────────────────────
# App Settings
# ─────────────────────────────────────────────
PORT = int(os.getenv("PORT", 5009))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ─────────────────────────────────────────────
# HTTP Headers
# ─────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────
# Intent Labels
# ─────────────────────────────────────────────
INTENT_LABELS = {
    Intent.PRODUCT_SEARCH:        "search",
    Intent.PRODUCT_DETAIL:        "details",
    Intent.PRODUCT_CATALOG:       "catalog",
    Intent.QUICK_SHIP:            "quick_ship",
    Intent.ON_SALE:               "sale",
    Intent.NEW_ARRIVALS:          "new",
    Intent.RELATED_PRODUCTS:      "related",
    Intent.CATEGORY_BROWSE:       "category",
    Intent.CATEGORY_LIST:         "categories",
    Intent.FILTER_BY_FINISH:      "filter",
    Intent.FILTER_BY_SIZE:        "filter",
    Intent.FILTER_BY_COLOR:       "filter",
    Intent.FILTER_BY_THICKNESS:   "filter",
    Intent.FILTER_BY_EDGE:        "filter",
    Intent.FILTER_BY_APPLICATION: "filter",
    Intent.FILTER_BY_MATERIAL:    "filter",
    Intent.FILTER_BY_ORIGIN:      "filter",
    Intent.SIZE_LIST:             "info",
    Intent.MOSAIC_PRODUCTS:       "search",
    Intent.TRIM_PRODUCTS:         "search",
    Intent.CHIP_CARD:             "search",
    Intent.DISCOUNT_INQUIRY:      "deals",
    Intent.BULK_DISCOUNT:         "deals",
    Intent.CLEARANCE_PRODUCTS:    "deals",
    Intent.PROMOTIONS:            "deals",
    Intent.COUPON_INQUIRY:        "deals",
    Intent.SAVE_FOR_LATER:        "account",
    Intent.WISHLIST:              "account",
    Intent.ORDER_TRACKING:        "order",
    Intent.ORDER_STATUS:          "order",
    Intent.PLACE_ORDER:           "order",
    Intent.ORDER_HISTORY:         "order",
    Intent.LAST_ORDER:            "order",
    Intent.REORDER:               "order",
    Intent.ORDER_ITEM:            "order",
    Intent.QUICK_ORDER:           "order",
    Intent.PRODUCT_VARIATIONS:    "variations",
    Intent.SAMPLE_REQUEST:        "sample",
    Intent.UNKNOWN:               "unknown",
}

# ─────────────────────────────────────────────
# Order & User Handling
# ─────────────────────────────────────────────
ORDER_INTENTS = {
    Intent.ORDER_HISTORY,
    Intent.LAST_ORDER,
    Intent.REORDER,
    Intent.ORDER_TRACKING,
    Intent.ORDER_STATUS,
}

ORDER_CREATE_INTENTS = {
    Intent.QUICK_ORDER,
    Intent.ORDER_ITEM,
    Intent.PLACE_ORDER,
}

USER_PLACEHOLDERS = {
    "CURRENT_USER_ID",
    "CURRENT_USER",
    "current_user_id",
    "current_user",
}

# Order message formatting constants
MAX_DISPLAYED_ITEMS = 3  # Maximum number of items to show before truncating with '+N more'

# Default payment method used when none is specified in the request.
# Change to "bacs" (bank transfer) or "stripe" etc. as needed.
DEFAULT_PAYMENT_METHOD = "cod"
DEFAULT_PAYMENT_METHOD_TITLE = "Cash on Delivery"
