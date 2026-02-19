"""
Application configuration module for WGC Tiles Store Chat API.
Contains environment variables, constants, and settings.
Named app_config to avoid conflict with the existing config/ directory.
"""

import os
from dotenv import load_dotenv
from models import Intent

load_dotenv()

# ═══════════════════════════════════════════
# ENVIRONMENT VARIABLES
# ═══════════════════════════════════════════

WOO_BASE_URL = os.getenv("WOO_BASE_URL", "https://wgc.net.in/hn/wp-json/wc/v3")
WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
PORT = int(os.getenv("PORT", 5009))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ═══════════════════════════════════════════
# HTTP HEADERS
# ═══════════════════════════════════════════

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ═══════════════════════════════════════════
# ORDER & USER HANDLING CONSTANTS
# ═══════════════════════════════════════════

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
