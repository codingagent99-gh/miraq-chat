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

# ═══════════════════════════════════════════
# LLM FALLBACK CONFIGURATION
# ═══════════════════════════════════════════

# LLM Provider settings
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "copilot")  # copilot, openai, anthropic, azure_openai
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5.2")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_API_BASE_URL = os.getenv("LLM_API_BASE_URL", "")
COPILOT_API_TOKEN = os.getenv("COPILOT_API_TOKEN", "")

# LLM behavior settings
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "500"))
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "10"))

# Feature flags
LLM_FALLBACK_ENABLED = os.getenv("LLM_FALLBACK_ENABLED", "true").lower() == "true"
LLM_RETRY_ON_EMPTY_RESULTS = os.getenv("LLM_RETRY_ON_EMPTY_RESULTS", "true").lower() == "true"

# Cost estimation (USD per 1000 tokens)
LLM_COST_PER_1K_INPUT = float(os.getenv("LLM_COST_PER_1K_INPUT", "0.002"))
LLM_COST_PER_1K_OUTPUT = float(os.getenv("LLM_COST_PER_1K_OUTPUT", "0.008"))
