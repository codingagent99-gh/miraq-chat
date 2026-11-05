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
# BASE URLs — REMOVED (Phase 2)
# ═══════════════════════════════════════════
#
# _WP_BASE / WOO_BASE_URL / WOO_STORE_API_URL / CUSTOM_API_BASE_URL used to
# live here as single-store globals read from the environment at import (and
# WOO_CONSUMER_KEY / WOO_CONSUMER_SECRET, before the backend stopped using a
# WooCommerce key at all).
#
# They are now per-tenant: resolved from the tenants table into TenantConfig
# by tenant_registry._rehydrate(), carried on the request-scoped StoreLoader,
# and read at call time via get_store_loader() — see woo_client.execute().
# A module-level global cannot express "whichever
# store this request belongs to", so keeping them would only invite a call
# site to silently use the wrong tenant's settings.
#
# Equivalents on the loader:
#   _WP_BASE            -> loader.wp_base_url
#   WOO_BASE_URL        -> loader.base
#   WOO_STORE_API_URL   -> loader.store_api_base
#   CUSTOM_API_BASE_URL -> loader.custom_api_base

PORT = int(os.getenv("PORT", 5009))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
USE_RELOADER = os.getenv("USE_RELOADER", "false").lower() == "true"
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

# WooCommerce My Account pages the chat links to. The chat never edits an
# account itself; these are WooCommerce's stock paths under the WordPress root.
WOO_EDIT_ADDRESS_PATH = "/my-account/edit-address/"
WOO_EDIT_ACCOUNT_PATH = "/my-account/edit-account/"

# ═══════════════════════════════════════════════════════════════
# ECOMMERCE BACKEND
# ═══════════════════════════════════════════════════════════════

# Supported values: "woocommerce", "shopify".
#
# Read and VALIDATED once in platform_config, then re-exported here so every
# existing `from app_config import ECOMMERCE_BACKEND` call site keeps working
# unchanged. Previously this line did its own os.getenv with a silent
# "woocommerce" fallback, as did store_loader/config.py and
# ecommerce/__init__.py -- three independent reads, three independent chances
# to quietly resolve to the wrong platform. See platform_config's docstring.
from platform_config import ECOMMERCE_BACKEND  # noqa: F401  (re-export)

# ── Shopify customer identity ─────────────────────────────────────────────
#   "app_proxy" (default, required in production)
#       Trust only Shopify's App Proxy: verify the HMAC signature and use the
#       signed `logged_in_customer_id`. A browser-supplied customer_id is
#       ignored entirely.
#
#   "insecure_client_claim" (development only)
#       Trust the customer_id the browser sends. This lets ANY caller read ANY
#       customer's orders and saved addresses. It exists so the widget can be
#       developed before the Shopify app proxy is configured, and it must be
#       chosen explicitly — the default is never the insecure one.
SHOPIFY_CUSTOMER_AUTH = os.getenv("SHOPIFY_CUSTOMER_AUTH", "app_proxy").lower()

# Replay window for signed proxy requests, in seconds (0 disables the check).
SHOPIFY_PROXY_MAX_AGE = int(os.getenv("SHOPIFY_PROXY_MAX_AGE", "900"))

# ── Shopify APP credentials — process-wide, NOT per-tenant ──────────────────
#
# These identify the MiraQ Shopify app itself, not any individual store. One
# pair is issued once in the Partner Dashboard (SHOPIFY_CLIENT_ID is the same
# value as `client_id` in shopify.app.miraq-commerce-agent.toml) and is shared
# by every merchant who installs the app, however many that becomes.
#
# SHOPIFY_CLIENT_SECRET is used for three things, all of them app-wide:
#   1. exchanging an OAuth `code` for a per-store access token at install
#   2. verifying App Proxy request signatures  (ecommerce/shopify_proxy.py)
#   3. verifying webhook / Events API body HMACs  (routes/shopify.py)
#
# They are therefore NOT on the Tenant row. A per-tenant copy would be N
# duplicates of one secret: rotating it would mean rewriting every row, and
# any row left unpopulated would fail signature verification outright.
#
# What IS per-tenant: Tenant.shopify_domain (captured from the `shop` param at
# install) and the access token in models/shopify_token.py keyed by domain.
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")

# Admin GraphQL version this codebase targets. App-wide: one version for every
# store, bumped deliberately when the schema is verified against it.
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10")

if SHOPIFY_CUSTOMER_AUTH != "app_proxy":
    import logging as _logging
    _logging.getLogger("miraq_chat").error(
        "⚠️  SHOPIFY_CUSTOMER_AUTH=%s — customer identity is NOT verified. "
        "Any caller can impersonate any customer and read their orders and "
        "addresses. This must never be used in production.",
        SHOPIFY_CUSTOMER_AUTH,
    )

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

# First month of the fiscal year (1 = January = calendar quarters). Drives what
# "Q1" and "this quarter" mean. Set FISCAL_YEAR_START_MONTH to e.g. 4 for an
# April-start fiscal year.
# NOTE: pending Diya's confirmation — defaults to CALENDAR quarters.
FISCAL_YEAR_START_MONTH: int = max(1, min(12, int(os.getenv("FISCAL_YEAR_START_MONTH", "1"))))


# "Most popular" ranking window and size (Shopify orders-based path).
MOST_POPULAR_TOP_N = int(os.getenv("MOST_POPULAR_TOP_N", "5"))
MOST_POPULAR_WINDOW_DAYS = int(os.getenv("MOST_POPULAR_WINDOW_DAYS", "30"))
MOST_POPULAR_MAX_GROUPS = int(os.getenv("MOST_POPULAR_MAX_GROUPS", "6"))

LLM_API_BASE_URL: str = _resolve_llm_api_base_url()

# LLM behavior settings
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "500"))
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "10"))
# Reasoning models (e.g. mistral-large-4) think by default. Set to "none" to skip
# the thinking step (fewer tokens, lower latency). Empty = parameter not sent.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "").strip()

# Feature flags
LLM_FALLBACK_ENABLED = os.getenv("LLM_FALLBACK_ENABLED", "true").lower() == "true"
LLM_RETRY_ON_EMPTY_RESULTS = os.getenv("LLM_RETRY_ON_EMPTY_RESULTS", "true").lower() == "true"


# Cost estimation (USD per 1000 tokens)
LLM_COST_PER_1K_INPUT = float(os.getenv("LLM_COST_PER_1K_INPUT", "0.002"))
LLM_COST_PER_1K_OUTPUT = float(os.getenv("LLM_COST_PER_1K_OUTPUT", "0.008"))


# ═══════════════════════════════════════════
# MESSAGING CHANNELS (WhatsApp / Instagram)
# ═══════════════════════════════════════════
# Shared secret the webhook service sends as X-MiraQ-Channel-Key on
# /chat/channel and /channel-connections. Empty = both reject every request.
CHANNEL_API_KEY = os.getenv("CHANNEL_API_KEY", "").strip()
# Product cards sent per turn. Each card is a separate message on WhatsApp.
CHANNEL_MAX_PRODUCT_CARDS = int(os.getenv("CHANNEL_MAX_PRODUCT_CARDS", "5"))


# ═══════════════════════════════════════════
# TRANSLATION (per-tenant; see translation/)
# ═══════════════════════════════════════════
# WHICH provider and WHICH languages a tenant uses live on its row
# (tenants.features["translation"], edited via /admin/translation/...).
# Only where each provider runs is process-wide config.
#
# LIBRE_TRANSLATE_PORT is the old single-store setting; still honoured as the
# default port so an existing .env keeps working.
LIBRETRANSLATE_URL = os.getenv(
    "LIBRETRANSLATE_URL",
    f"http://localhost:{os.getenv('LIBRE_TRANSLATE_PORT', '5012')}",
).rstrip("/")
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()
INDICTRANS2_URL = os.getenv("INDICTRANS2_URL", "http://localhost:5013").rstrip("/")

# Short connect timeout: a provider that is not listening must cost ~1s once,
# then the per-provider circuit breaker skips it for the cooldown.
TRANSLATION_CONNECT_TIMEOUT = float(os.getenv("TRANSLATION_CONNECT_TIMEOUT", "1.0"))
TRANSLATION_READ_TIMEOUT = float(os.getenv("TRANSLATION_READ_TIMEOUT", "30"))
TRANSLATION_BREAKER_COOLDOWN = float(os.getenv("TRANSLATION_BREAKER_COOLDOWN", "60"))