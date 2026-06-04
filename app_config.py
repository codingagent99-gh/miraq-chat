"""
Application configuration module for WGC Tiles Store Chat API.
Contains environment variables, constants, and settings.
Named app_config to avoid conflict with the existing config/ directory.
"""

import os
import re
import logging
from dotenv import load_dotenv
from models import Intent

load_dotenv()

# ═══════════════════════════════════════════
# BASE URLs
# ═══════════════════════════════════════════

_WP_BASE = os.getenv("WP_BASE_URL", "https://wgc.net.in/hn")

# WooCommerce admin REST API  (/wc/v3 — products, orders, customers)
WOO_BASE_URL = os.getenv(
    "WOO_BASE_URL",
    f"{_WP_BASE}/wp-json/wc/v3",
)

# WooCommerce Store API  (/wc/store/v1 — cart, checkout, session-aware)
WOO_STORE_API_URL = os.getenv(            # ← was reading WOO_BASE_URL by mistake
    "WOO_STORE_API_URL",
    f"{_WP_BASE}/wp-json/wc/store/v1",
)

# Custom plugin API  (/custom-api/v1 — nonce refresh, etc.)
CUSTOM_API_BASE_URL = os.getenv(
    "CUSTOM_API_BASE_URL",
    f"{_WP_BASE}/wp-json/custom-api/v1",
)

WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
PORT = int(os.getenv("PORT", 5009))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ═══════════════════════════════════════════════════════════════
# STORE IDENTITY
# ═══════════════════════════════════════════════════════════════

# Human-readable store name used in LLM prompts and system messages.
STORE_NAME = os.getenv("STORE_NAME", "WGC Tiles Store")

# Bot/assistant name shown in closing messages.
BOT_NAME = os.getenv("BOT_NAME", "MiraQ")

# Singular and plural forms of the primary product type.
# Used in classifier fallback regexes and user-facing copy.
PRODUCT_TYPE_SINGULAR = os.getenv("PRODUCT_TYPE_SINGULAR", "item")
PRODUCT_TYPE_PLURAL   = os.getenv("PRODUCT_TYPE_PLURAL",   "items")

# Short identifier string embedded in API response metadata.
# Useful for distinguishing traffic in logs when hosting multiple stores.
CLASSIFIER_PROVIDER_TAG = os.getenv("CLASSIFIER_PROVIDER_TAG", "wgc_intent_classifier")

# ═══════════════════════════════════════════
# CURRENCY
# ═══════════════════════════════════════════

# Static fallback symbol used before StoreLoader has fetched the live value.
# Prefer get_currency_symbol() at runtime for the dynamically-fetched symbol.
CURRENCY_SYMBOL = "$"


def get_currency_symbol() -> str:
    """Get the currency symbol dynamically from the StoreLoader.
    Falls back to '$' if StoreLoader hasn't loaded yet.
    """
    from store_registry import get_store_loader
    loader = get_store_loader()
    if loader and hasattr(loader, "currency_symbol"):
        return loader.currency_symbol
    return "$"

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
    Intent.HISTORICAL_SEARCH,
}

CART_INTENTS = {
    Intent.ADD_TO_CART,
    Intent.VIEW_CART,
    Intent.REMOVE_FROM_CART,
    Intent.UPDATE_CART_QTY,
    Intent.CHECKOUT,
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

# ═══════════════════════════════════════════════════════════════
# ECOMMERCE BACKEND
# ═══════════════════════════════════════════════════════════════

# Supported values: "woocommerce", "shopify"
ECOMMERCE_BACKEND = os.getenv("ECOMMERCE_BACKEND", "woocommerce").lower()

# ═══════════════════════════════════════════
# API DEFAULTS  (move from settings.py)
# ═══════════════════════════════════════════
DEFAULT_PER_PAGE        = int(os.getenv("CHAT_PER_PAGE", "4"))
DEFAULT_ORDER_PER_PAGE  = int(os.getenv("CHAT_ORDERS_PER_PAGE", "5"))
DEFAULT_STATUS          = "publish"
DEFAULT_STOCK_STATUS    = "instock"
REQUEST_TIMEOUT         = int(os.getenv("REQUEST_TIMEOUT", "30"))
LOG_LEVEL               = os.getenv("LOG_LEVEL", "INFO")

# ═══════════════════════════════════════════
# LLM FALLBACK CONFIGURATION
# ═══════════════════════════════════════════

# LLM Provider settings
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mistral")  # mistral, copilot, openai, anthropic, azure_openai
LLM_MODEL = os.getenv("LLM_MODEL", "mistral-large-latest")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
COPILOT_API_TOKEN = os.getenv("COPILOT_API_TOKEN", "")

# Per-provider canonical base URLs used when LLM_API_BASE_URL is not set in .env.
# azure_openai has no universal endpoint — it must always be supplied explicitly.
_LLM_PROVIDER_DEFAULT_URLS: dict = {
    "mistral":      "https://api.mistral.ai/v1/chat/completions",
    "openai":       "https://api.openai.com/v1/chat/completions",
    "anthropic":    "https://api.anthropic.com/v1/messages",
    "copilot":      "https://api.githubcopilot.com/chat/completions",
    "azure_openai": "",
}

# Matches accidental markdown link formatting: [https://...](https://...)
_MARKDOWN_LINK_RE = re.compile(r'^\[.*?\]\((https?://[^)]+)\)$')


def _resolve_llm_api_base_url() -> str:
    """
    Resolve the LLM API base URL with two layers of protection:

    1. Provider-aware default — if LLM_API_BASE_URL is absent from .env the
       correct canonical URL for the configured provider is used automatically,
       so the LLM works out of the box without any .env entry for this field.

    2. Markdown-strip guard — if the value was accidentally copied from rendered
       documentation as [https://...](https://...), the bare URL is extracted
       and a startup warning is emitted so the misconfiguration is visible in
       logs immediately rather than surfacing as a cryptic requests error.
    """
    raw = os.getenv("LLM_API_BASE_URL", "").strip()

    if not raw:
        # Nothing in .env — fall back to the provider-specific canonical URL.
        url = _LLM_PROVIDER_DEFAULT_URLS.get(LLM_PROVIDER.lower(), "")
        if url:
            logging.getLogger("miraq_chat").debug(
                f"app_config: LLM_API_BASE_URL not set — using provider default "
                f"for '{LLM_PROVIDER}': {url}"
            )
        return url

    # Strip accidental markdown link formatting, e.g.:
    #   [https://api.mistral.ai/...](https://api.mistral.ai/...)
    #   → https://api.mistral.ai/...
    m = _MARKDOWN_LINK_RE.match(raw)
    if m:
        cleaned = m.group(1)
        logging.getLogger("miraq_chat").warning(
            f"app_config: LLM_API_BASE_URL contains markdown link formatting — "
            f"auto-corrected | raw={raw!r} -> cleaned={cleaned!r}"
        )
        return cleaned

    return raw

# Roles that use the custom orders API (POST /custom-api/v1/orders)
# instead of the standard WooCommerce GET /wc/v3/orders endpoint.
# Override via env var CUSTOM_ORDER_ROLES_JSON (JSON array).
import json as _json

def _load_custom_order_roles() -> frozenset:
    raw = os.getenv("CUSTOM_ORDER_ROLES_JSON", "")
    if raw:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                return frozenset(parsed)
        except Exception:
            pass
    return frozenset({"cs_rep", "project_manager", "cs_project_manager"})

CUSTOM_ORDER_ROLES: frozenset = _load_custom_order_roles()
BULK_ORDER_ROLES: frozenset = CUSTOM_ORDER_ROLES | frozenset({"sales_rep"})

LLM_API_BASE_URL: str = _resolve_llm_api_base_url()

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